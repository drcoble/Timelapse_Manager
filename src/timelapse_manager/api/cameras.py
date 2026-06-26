"""Camera management and capture endpoints.

Covers camera CRUD, a connection-validation probe, network discovery, a manual
single-capture trigger, and per-camera capture status. Capture itself is
per-project (a frame must belong to a project), so the manual-capture endpoint
requires a project id and routes through the same atomic frame writer the
background supervisor uses.

Every address that enters the system on camera creation passes through the
single host-resolution chokepoint, so the later SSRF-hardening seam covers this
path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..cameras import (
    DetectionOutcome,
    DiscoveredCamera,
    GeoLocation,
    build_adapter,
    check_scan_range,
    detect_protocols,
    discover_onvif,
    get_camera_geolocation,
    resolve_camera_host,
    resolve_discovered_uris,
    scan_range,
)
from ..capture import CaptureSupervisor
from ..db.models import Camera, Project
from ..db.session import get_session
from ..runtime import get_context
from ..security import require_operator_or_admin_principal
from ..security.camera_defaults_service import resolve_default_credentials
from ..security.crypto import encrypt_credentials
from ..security.ssrf import SsrfError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cameras", tags=["cameras"])

_PROTOCOLS = ("onvif", "rtsp", "http", "vapix")

# Overall wall-clock cap for a detect request, comfortably above the per-probe
# budget so the concurrent probes can all finish but a wedged call still returns.
_DETECT_TIMEOUT_SECONDS = 20.0

# Overall wall-clock cap for enriching discovery results with media URIs. Above
# the per-camera budget so a batch can finish, but bounded so a wedged enrichment
# cannot hang the discovery response -- on timeout the un-enriched result returns.
_DISCOVER_ENRICH_TIMEOUT_SECONDS = 30.0


class CameraCreate(BaseModel):
    """Request body for creating a camera."""

    name: str = Field(min_length=1)
    address: str | None = None
    protocol: str | None = None
    credentials: dict[str, Any] | None = None
    # Whether a camera with no credentials of its own falls back to the global
    # default credentials. Defaults to on for cameras created through the API.
    credentials_inherit_default: bool = True
    snapshot_uri: str | None = None
    stream_uri: str | None = None
    default_resolution: str | None = None
    geolocation_latitude: float | None = None
    geolocation_longitude: float | None = None
    geolocation_source: str | None = None


class CameraOut(BaseModel):
    """Camera representation returned to clients (no secrets leaked)."""

    id: int
    name: str
    address: str | None
    protocol: str | None
    # The inherit-default flag is safe to expose; the credentials themselves
    # (and any password) are never part of this representation.
    credentials_inherit_default: bool
    snapshot_uri: str | None
    stream_uri: str | None
    default_resolution: str | None
    geolocation_latitude: float | None
    geolocation_longitude: float | None
    geolocation_source: str | None


class ValidationOut(BaseModel):
    """Outcome of a connection-validation probe."""

    ok: bool
    reason: str | None
    message: str


class CaptureRequest(BaseModel):
    """Body for a manual single capture; the project owns the frame."""

    project_id: int


class CaptureOut(BaseModel):
    """The frame produced by a manual capture."""

    frame_id: int
    project_id: int
    sequence_index: int
    file_path: str
    width: int
    height: int
    file_size_bytes: int
    captured_at: str


class DiscoverRequest(BaseModel):
    """Body for discovery; an address range scans that range, else ONVIF."""

    range: str | None = None


class DiscoveredOut(BaseModel):
    """A camera surfaced by discovery."""

    address: str
    protocol: str
    snapshot_uri: str | None
    stream_uri: str | None
    vendor: str | None


class DetectCredentials(BaseModel):
    """Optional credentials supplied with a detect request."""

    username: str | None = None
    password: str | None = None


class DetectRequest(BaseModel):
    """Body for a protocol-detection probe of an address."""

    address: str = Field(min_length=1)
    credentials: DetectCredentials | None = None


class ProtocolCandidateOut(BaseModel):
    """One protocol's detection result (never carries credentials)."""

    protocol: str
    ok: bool
    snapshot_uri: str | None
    stream_uri: str | None
    confidence: str
    detail: str


class DetectOut(BaseModel):
    """The full multi-protocol detection result for an address.

    Lists every protocol probed (responders and non-responders) plus a
    recommended primary. Credentials are never echoed back in this model.
    """

    candidates: list[ProtocolCandidateOut]
    recommended_primary: str | None


class CaptureStatusEntry(BaseModel):
    """Live capture status for one project backed by the camera."""

    project_id: int
    camera_id: int
    state: str
    last_success_at: str | None
    last_error_at: str | None
    last_error: str | None
    frames_captured: int
    attempt_count: int
    next_retry_at: str | None


class CaptureStatusOut(BaseModel):
    """Per-camera capture status: one entry per backed project."""

    camera_id: int
    projects: list[CaptureStatusEntry]


def _to_out(camera: Camera) -> CameraOut:
    """Project a camera row onto its safe public representation."""
    return CameraOut(
        id=camera.id,
        name=camera.name,
        address=camera.address,
        protocol=camera.protocol,
        credentials_inherit_default=bool(camera.credentials_inherit_default),
        snapshot_uri=camera.snapshot_uri,
        stream_uri=camera.stream_uri,
        default_resolution=camera.default_resolution,
        geolocation_latitude=camera.geolocation_latitude,
        geolocation_longitude=camera.geolocation_longitude,
        geolocation_source=camera.geolocation_source,
    )


def _credentials_from_request(
    creds: DetectCredentials | None,
) -> tuple[str, str] | None:
    """Turn request credentials into a ``(user, pass)`` pair, or None if blank.

    Returning None when no username is given lets the caller fall back to the
    saved global default -- the same precedence the capture/validate paths use.
    """
    if creds is None or not creds.username:
        return None
    return (creds.username, creds.password or "")


def _detect_out(outcome: DetectionOutcome) -> DetectOut:
    """Project a :class:`DetectionOutcome` onto its safe public model."""
    return DetectOut(
        candidates=[
            ProtocolCandidateOut(
                protocol=candidate.protocol,
                ok=candidate.ok,
                snapshot_uri=candidate.snapshot_uri,
                stream_uri=candidate.stream_uri,
                confidence=candidate.confidence.value,
                detail=candidate.detail,
            )
            for candidate in outcome.candidates
        ],
        recommended_primary=outcome.recommended_primary,
    )


def _get_camera_or_404(session: Session, camera_id: int) -> Camera:
    """Return a camera row or raise a 404."""
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera {camera_id} not found",
        )
    return camera


def _supervisor() -> CaptureSupervisor:
    """Return the running capture supervisor or raise a 503 if absent."""
    supervisor = get_context().capture_supervisor
    if supervisor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="capture engine is not available",
        )
    return supervisor


@router.post(
    "",
    response_model=CameraOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
def create_camera(
    payload: CameraCreate,
    session: Annotated[Session, Depends(get_session)],
) -> CameraOut:
    """Create a camera. The address passes through the host-resolution seam.

    The address is validated against the outbound-request deny-list (with the
    admin private opt-in); a denied address is rejected as a ``422`` rather than
    stored.
    """
    if payload.protocol is not None and payload.protocol not in _PROTOCOLS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported protocol: {payload.protocol!r}",
        )
    address = payload.address
    if address is not None:
        # Single SSRF-hardening chokepoint: validate-and-reject, never rewrite.
        try:
            address = resolve_camera_host(address)
        except SsrfError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    camera = Camera(
        name=payload.name,
        address=address,
        protocol=payload.protocol,
        credentials=encrypt_credentials(payload.credentials),
        credentials_inherit_default=payload.credentials_inherit_default,
        snapshot_uri=payload.snapshot_uri,
        stream_uri=payload.stream_uri,
        default_resolution=payload.default_resolution,
        geolocation_latitude=payload.geolocation_latitude,
        geolocation_longitude=payload.geolocation_longitude,
        geolocation_source=payload.geolocation_source,
    )
    session.add(camera)
    session.flush()
    return _to_out(camera)


@router.get("", response_model=list[CameraOut])
def list_cameras(
    session: Annotated[Session, Depends(get_session)],
) -> list[CameraOut]:
    """Return all configured cameras."""
    cameras = session.execute(select(Camera).order_by(Camera.id)).scalars().all()
    return [_to_out(camera) for camera in cameras]


@router.get("/{camera_id}", response_model=CameraOut)
def get_camera(
    camera_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> CameraOut:
    """Return a single camera by id."""
    return _to_out(_get_camera_or_404(session, camera_id))


@router.delete(
    "/{camera_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
def delete_camera(
    camera_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    """Delete a camera by id."""
    camera = _get_camera_or_404(session, camera_id)
    session.delete(camera)


@router.post(
    "/{camera_id}/validate",
    response_model=ValidationOut,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
async def validate_camera(camera_id: int) -> ValidationOut:
    """Build the camera's adapter and probe reachability/authentication."""
    config, default_credentials = await asyncio.to_thread(
        _load_camera_config, camera_id
    )
    supervisor = _supervisor()
    try:
        adapter = build_adapter(
            config,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    try:
        result = await adapter.validate_connection()
        # Best-effort: resolve geolocation (manual override wins, else device)
        # and persist a device-reported fix so later reads have it without a
        # round-trip. The resolver swallows its own lookup errors.
        geo = await get_camera_geolocation(adapter, camera=config)
    finally:
        await adapter.close()
    if geo is not None and geo.source == "camera":
        await asyncio.to_thread(_persist_device_geolocation, camera_id, geo)
    reason = result.reason.value if result.reason is not None else None
    return ValidationOut(ok=result.ok, reason=reason, message=result.message)


@router.post(
    "/{camera_id}/capture",
    response_model=CaptureOut,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
async def capture_camera(
    camera_id: int,
    payload: Annotated[CaptureRequest, Body()],
) -> CaptureOut:
    """Capture a single frame now and store it under the given project.

    A frame must belong to a project, so a ``project_id`` is required. The frame
    is written through the same atomic writer the background supervisor uses.
    """
    config, default_credentials = await asyncio.to_thread(
        _load_camera_config, camera_id
    )
    await asyncio.to_thread(_assert_project_for_camera, payload.project_id, camera_id)
    supervisor = _supervisor()
    try:
        adapter = build_adapter(
            config,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    try:
        captured = await adapter.capture()
    finally:
        await adapter.close()
    written = await asyncio.to_thread(
        supervisor.frame_writer.write, payload.project_id, captured
    )
    return CaptureOut(
        frame_id=written.frame_id,
        project_id=written.project_id,
        sequence_index=written.sequence_index,
        file_path=written.file_path,
        width=written.width,
        height=written.height,
        file_size_bytes=written.file_size_bytes,
        captured_at=written.captured_at.isoformat(),
    )


@router.post(
    "/discover",
    response_model=list[DiscoveredOut],
    dependencies=[Depends(require_operator_or_admin_principal)],
)
async def discover_cameras(
    session: Annotated[Session, Depends(get_session)],
    payload: Annotated[DiscoverRequest | None, Body()] = None,
) -> list[DiscoveredOut]:
    """Discover cameras: scan an address range if given, else ONVIF on the LAN.

    An oversized scan range (beyond the configured cap) is rejected as a ``422``
    with an actionable message instead of being silently truncated.

    Discovered ONVIF cameras are then best-effort enriched with their snapshot/
    stream URIs, resolved with the saved global default credential through the
    running capture supervisor's shared HTTP client. Enrichment never makes
    discovery fail: if the supervisor is unavailable the basic result is returned
    unchanged, and the whole enrichment is time-boxed so a wedged device cannot
    hang the response.
    """
    address_range = payload.range if payload is not None else None
    found: list[DiscoveredCamera]
    if address_range:
        # Validate, count, and cap-check before scanning -- the same shared seam
        # the web path uses -- so an oversized range is refused up front rather
        # than enumerated. A malformed range or an over-cap range both surface as
        # a 422 with an actionable message.
        try:
            check_scan_range(address_range, get_context().settings.ssrf.max_scan_hosts)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        found = await scan_range(address_range)
    else:
        found = await discover_onvif()

    # Best-effort enrichment: only when the capture engine is up (it owns the
    # shared HTTP client and ffmpeg binary). Its absence -- or a timeout -- simply
    # returns the basic discovery result; discovery never newly requires it.
    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        credentials = resolve_default_credentials(session)
        try:
            found = await asyncio.wait_for(
                resolve_discovered_uris(
                    found,
                    credentials,
                    supervisor.http_client,
                    ffmpeg_binary=supervisor.ffmpeg_binary,
                ),
                timeout=_DISCOVER_ENRICH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # Enrichment mutates in place, so any partial result is kept; the
            # un-enriched remainder simply keeps its None URIs.
            logger.warning("discovery URI enrichment timed out; returning basics")

    return [
        DiscoveredOut(
            address=camera.address,
            protocol=camera.protocol,
            snapshot_uri=camera.snapshot_uri,
            stream_uri=camera.stream_uri,
            vendor=camera.vendor,
        )
        for camera in found
    ]


@router.post(
    "/detect-protocol",
    response_model=DetectOut,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
async def detect_protocol(
    payload: DetectRequest,
    session: Annotated[Session, Depends(get_session)],
) -> DetectOut:
    """Probe an address across all protocols and return every responder.

    The supplied address is validated through the same host-resolution
    chokepoint camera creation uses *before* any probe runs; a denied address is
    rejected as a ``422`` and no probe is attempted. When the request carries no
    credentials, the saved global default (if any) is substituted, matching the
    capture/validate paths. The shared HTTP client and ffmpeg binary come from
    the running capture supervisor -- this path never opens its own client. The
    response never echoes credentials back.
    """
    # SSRF chokepoint first: validate-and-reject before probing anything.
    try:
        resolve_camera_host(payload.address)
    except SsrfError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    credentials = _credentials_from_request(payload.credentials)
    if credentials is None:
        credentials = resolve_default_credentials(session)

    supervisor = _supervisor()
    outcome = await asyncio.wait_for(
        detect_protocols(
            payload.address,
            credentials,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
        ),
        timeout=_DETECT_TIMEOUT_SECONDS,
    )
    return _detect_out(outcome)


@router.get("/{camera_id}/capture-status", response_model=CaptureStatusOut)
def capture_status(
    camera_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> CaptureStatusOut:
    """Return live capture status for every project backed by the camera."""
    _get_camera_or_404(session, camera_id)
    supervisor = _supervisor()
    entries = [
        CaptureStatusEntry(
            project_id=state.project_id,
            camera_id=state.camera_id,
            state=state.state,
            last_success_at=(
                state.last_success_at.isoformat()
                if state.last_success_at is not None
                else None
            ),
            last_error_at=(
                state.last_error_at.isoformat()
                if state.last_error_at is not None
                else None
            ),
            last_error=state.last_error,
            frames_captured=state.frames_captured,
            attempt_count=state.attempt_count,
            next_retry_at=(
                state.next_retry_at.isoformat()
                if state.next_retry_at is not None
                else None
            ),
        )
        for state in supervisor.states_for_camera(camera_id)
    ]
    return CaptureStatusOut(camera_id=camera_id, projects=entries)


def _load_camera_config(camera_id: int) -> tuple[Camera, tuple[str, str] | None]:
    """Load a camera row and the resolved default credentials for adapter build.

    Returns the (detached) camera row alongside the global fallback
    ``(username, password)`` resolved once in the same session (or ``None`` when
    no fallback applies). Adapter construction only reads plain attributes on the
    expunged row, so no lazy loads occur. Resolving the default here -- the single
    config-load seam both the validate and capture paths share -- keeps their
    credential behaviour identical. Synchronous; call via a thread executor.
    """
    context = get_context()
    with context.session_factory() as session:
        camera = session.get(Camera, camera_id)
        if camera is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"camera {camera_id} not found",
            )
        default_credentials = resolve_default_credentials(session)
        session.expunge(camera)
        return camera, default_credentials


def _persist_device_geolocation(camera_id: int, geo: GeoLocation) -> None:
    """Store a device-reported geolocation, never overwriting a manual override.

    Synchronous; call via a thread executor. Silently returns if the camera is
    gone or already carries a manual override.
    """
    context = get_context()
    with context.session_factory() as session:
        camera = session.get(Camera, camera_id)
        if camera is None or camera.geolocation_source == "manual":
            return
        camera.geolocation_latitude = geo.latitude
        camera.geolocation_longitude = geo.longitude
        camera.geolocation_source = "camera"
        session.commit()


def _assert_project_for_camera(project_id: int, camera_id: int) -> None:
    """Ensure the project exists and is bound to the camera, or raise.

    Synchronous; call via a thread executor.
    """
    context = get_context()
    with context.session_factory() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"project {project_id} not found",
            )
        if project.camera_id != camera_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"project {project_id} is not bound to camera {camera_id}"),
            )
