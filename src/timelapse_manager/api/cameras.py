"""Camera management and capture endpoints.

Covers camera CRUD, a connection-validation probe, network discovery, a manual
single-capture trigger, and per-camera capture status. Capture itself is
per-project (a frame must belong to a project), so the manual-capture endpoint
requires a project id and goes through the same one-shot path the exact-time
anchors use (#69 CAM-R10): the camera's cached session, its capture slot, the
project's stream and PTZ position, the capture timeout, the disk gate and the
shared frame writer.

Every address that enters the system on camera creation passes through the
single host-resolution chokepoint, so the later SSRF-hardening seam covers this
path.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..cameras import (
    DetectionOutcome,
    DiscoveredCamera,
    GeoLocation,
    GeolocationAccessDenied,
    build_adapter,
    check_scan_range,
    detect_protocols,
    discover_onvif,
    get_camera_geolocation,
    resolve_camera_host,
    resolve_discovered_uris,
    scan_range,
)
from ..cameras.base import (
    CameraMissingError,
    CaptureError,
    ValidationFailure,
    valid_coordinates,
)
from ..cameras.clients import contained_client
from ..capture import CaptureSupervisor
from ..capture.one_shot import capture_one_now
from ..capture.supervisor import CaptureTarget
from ..db.models import Camera, Project
from ..db.session import DbSessionDep
from ..logging import redact_text
from ..runtime import get_context
from ..security import Principal, require_operator_or_admin_principal
from ..security.camera_defaults_service import resolve_default_credentials
from ..security.credential_binding import (
    DEFAULT_WITHHELD_HINT,
    bound_default_credentials,
    camera_credential_targets,
    split_userinfo,
)
from ..security.crypto import encrypt_credentials
from ..security.ssrf import SsrfError, resolve_and_check_async
from ..services import cameras as camera_service
from ..services._base import Actor, run_effects
from ..services.cameras import CameraSpec

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cameras", tags=["cameras"])

_PROTOCOLS = ("onvif", "rtsp", "http", "vapix")

# Overall wall-clock cap for a detect request, comfortably above the per-probe
# budget so the concurrent probes can all finish but a wedged call still returns.
_DETECT_TIMEOUT_SECONDS = 20.0

# Headroom over the capture timeout for the validate probe's outer bound: the
# adapter's per-request timeouts are per httpx operation, not a total (#69
# CAM-R10).
_VALIDATE_MARGIN_SECONDS = 5.0

# How long a manual capture waits for a free capture slot on its camera: not at
# all. A manual capture takes a slot only when one is free now, so it never
# queues ahead of a scheduled anchor; a busy camera is a 503 to retry.
_MANUAL_SLOT_WAIT_SECONDS = 0.0

# Overall wall-clock cap for enriching discovery results with media URIs. Above
# the per-camera budget so a batch can finish, but bounded so a wedged enrichment
# cannot hang the discovery response -- on timeout the un-enriched result returns.
_DISCOVER_ENRICH_TIMEOUT_SECONDS = 30.0


def _validate_bound_seconds() -> float:
    """The validate probe's outer bound: the capture timeout plus headroom."""
    return get_context().settings.capture.timeout_seconds + _VALIDATE_MARGIN_SECONDS


async def _resolve_camera_host_async(address: str) -> None:
    """Validate ``address`` against the camera deny-list without blocking the loop.

    Mirrors the synchronous ``resolve_camera_host`` chokepoint exactly -- same
    admin opt-in for private subnets and the same "unresolvable at add time is
    allowed through, deferred to the fetch/probe path" rule -- but performs the
    DNS resolution on a worker thread so a slow or wedged resolver cannot stall
    the event loop (which would stall every concurrent request). Returns nothing;
    raises :class:`SsrfError` when the address (or a name it resolves to) is
    denied, matching the synchronous chokepoint's contract.
    """
    if not address:
        return

    ssrf = get_context().settings.ssrf
    try:
        await resolve_and_check_async(
            address,
            allow_private=True,
            allowed_private_subnets=ssrf.allowed_private_subnets,
        )
    except socket.gaierror:
        # Unresolvable at add time: defer enforcement to the fetch/probe path,
        # which re-validates fail-closed before connecting.
        return


class CameraCreate(BaseModel):
    """Request body for creating a camera."""

    name: str = Field(min_length=1)
    address: str | None = None
    protocol: str | None = None
    credentials: dict[str, Any] | None = None
    # Whether a camera with no credentials of its own falls back to the global
    # default credentials. Defaults to on for cameras created through the API.
    credentials_inherit_default: bool = True
    # Send HTTP Basic over plain http:// when the camera offers nothing else
    # (#69 CAM-S1, owner decision OD-3). Off by default: Basic exposes the
    # password on the network, so it is otherwise used only over https://.
    allow_basic_over_http: bool = False
    snapshot_uri: str | None = None
    stream_uri: str | None = None
    default_resolution: str | None = None
    # Range- and finite-checked in the route (:func:`_coordinate_error`), not
    # with Field bounds: a field error echoes the ``NaN`` input into the 422
    # body, which the default handler cannot serialise (it would become a 500).
    geolocation_latitude: float | None = None
    geolocation_longitude: float | None = None
    geolocation_source: str | None = None


def _coordinate_error(latitude: float | None, longitude: float | None) -> str | None:
    """Return why a submitted coordinate is impossible, or None when it is fine.

    ``json.loads`` accepts ``NaN`` and ``Infinity`` literals and Pydantic floats
    allow them, so each supplied value is checked for being finite and in range.
    An absent value (``None``) is fine.
    """
    if latitude is not None and not valid_coordinates(latitude, 0.0):
        return "geolocation_latitude must be a finite number between -90 and 90"
    if longitude is not None and not valid_coordinates(0.0, longitude):
        return "geolocation_longitude must be a finite number between -180 and 180"
    return None


class CameraOut(BaseModel):
    """Camera representation returned to clients (no secrets leaked)."""

    id: int
    name: str
    address: str | None
    protocol: str | None
    # The inherit-default flag is safe to expose; the credentials themselves
    # (and any password) are never part of this representation.
    credentials_inherit_default: bool
    allow_basic_over_http: bool
    snapshot_uri: str | None
    stream_uri: str | None
    default_resolution: str | None
    geolocation_latitude: float | None
    geolocation_longitude: float | None
    geolocation_source: str | None
    # SHA-256 fingerprint of the certificate the camera is pinned to (colon
    # form), or None when it is not pinned. Never the certificate itself.
    tls_pinned_fingerprint: str | None = None


GeolocationStatus = Literal["ok", "none", "access_denied"]


class ValidationOut(BaseModel):
    """Outcome of a connection-validation probe.

    ``geolocation_status`` says why a location was or was not stored alongside
    the probe: ``"ok"`` (a location resolved -- the manual override, or a
    device fix that was persisted), ``"none"`` (the device reported none, or
    the lookup failed), or ``"access_denied"`` (the device refused the read for
    this camera's account; Axis serves it to an Administrator only). Optional,
    so existing clients are unaffected.
    """

    ok: bool
    reason: str | None
    message: str
    geolocation_status: GeolocationStatus | None = None


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
    camera_id: int | None = Field(
        default=None,
        description=(
            "An existing camera the probe is for. Its certificate pin, when it "
            "has one, is the only trust the probe uses (a pinned camera fails "
            "TLS against the shared trust)."
        ),
    )


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
    # Set when the saved default login was held back because the address is
    # outside the allowed private subnets (owner decision OD-1).
    credentials_hint: str | None = None


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
    # Runner heartbeat (#6): when the loop last started a cycle, and how often
    # the supervisor has had to relaunch it (dead or wedged) since start-up.
    last_cycle_at: str | None = None
    restarts: int = 0


class CaptureStatusOut(BaseModel):
    """Per-camera capture status: one entry per backed project."""

    camera_id: int
    projects: list[CaptureStatusEntry]


def _without_userinfo(value: str | None) -> str | None:
    """Return an address or URI without any ``user:pass@`` part (defence in depth)."""
    return split_userinfo(value)[0] if value else value


def _to_out(camera: Camera) -> CameraOut:
    """Project a camera row onto its safe public representation."""
    return CameraOut(
        id=camera.id,
        name=camera.name,
        address=_without_userinfo(camera.address),
        protocol=camera.protocol,
        credentials_inherit_default=bool(camera.credentials_inherit_default),
        allow_basic_over_http=bool(camera.allow_basic_over_http),
        tls_pinned_fingerprint=camera_service.pinned_fingerprint(camera),
        snapshot_uri=_without_userinfo(camera.snapshot_uri),
        stream_uri=_without_userinfo(camera.stream_uri),
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


def _detect_out(
    outcome: DetectionOutcome, credentials_hint: str | None = None
) -> DetectOut:
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
        credentials_hint=credentials_hint,
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
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> CameraOut:
    """Create a camera. The address passes through the host-resolution seam.

    The protocol is validated against the supported set and the address against
    the outbound-request deny-list (with the admin private opt-in) here in the
    adapter -- both are network/transport concerns -- so the service receives an
    already-resolved address and an already-encrypted credential document. A
    denied address is rejected as a ``422`` rather than stored.
    """
    if payload.protocol is not None and payload.protocol not in _PROTOCOLS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported protocol: {payload.protocol!r}",
        )
    coordinate_error = _coordinate_error(
        payload.geolocation_latitude, payload.geolocation_longitude
    )
    if coordinate_error is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=coordinate_error,
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

    spec = CameraSpec(
        name=payload.name,
        address=address,
        protocol=payload.protocol,
        credentials=encrypt_credentials(payload.credentials),
        credentials_inherit_default=payload.credentials_inherit_default,
        allow_basic_over_http=payload.allow_basic_over_http,
        snapshot_uri=payload.snapshot_uri,
        stream_uri=payload.stream_uri,
        default_resolution=payload.default_resolution,
        geolocation_latitude=payload.geolocation_latitude,
        geolocation_longitude=payload.geolocation_longitude,
        geolocation_source=payload.geolocation_source,
    )
    result = camera_service.create_camera(session, Actor.of(principal), spec)
    out = _to_out(result.value)
    session.commit()
    run_effects(result.effects, get_context())
    return out


@router.get("", response_model=list[CameraOut])
def list_cameras(
    session: DbSessionDep,
) -> list[CameraOut]:
    """Return all configured cameras."""
    cameras = session.execute(select(Camera).order_by(Camera.id)).scalars().all()
    return [_to_out(camera) for camera in cameras]


@router.get("/{camera_id}", response_model=CameraOut)
def get_camera(
    camera_id: int,
    session: DbSessionDep,
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
    session: DbSessionDep,
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> None:
    """Delete a camera by id. A missing camera is a ``404``."""
    result = camera_service.delete_camera(session, Actor.of(principal), camera_id)
    session.commit()
    run_effects(result.effects, get_context())


@router.post(
    "/{camera_id}/validate",
    response_model=ValidationOut,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
async def validate_camera(camera_id: int) -> ValidationOut:
    """Build the camera's adapter and probe reachability/authentication."""
    config, default_credentials = await run_in_threadpool(
        _load_camera_config, camera_id
    )
    supervisor = _supervisor()
    try:
        adapter = build_adapter(
            config,
            supervisor.client_for("probe", config.tls_pinned_cert_pem),
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    geolocation_status: GeolocationStatus
    # Each step is bounded: the adapter's own timeouts are per request, so a
    # camera that trickles bytes could otherwise hold the request open (#69
    # CAM-R10).
    bound = _validate_bound_seconds()
    try:
        try:
            async with asyncio.timeout(bound):
                result = await adapter.validate_connection()
        except TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"camera did not answer within {bound:.0f}s",
            ) from None
        # Best-effort: resolve geolocation (manual override wins, else device)
        # and persist a device-reported fix so later reads have it without a
        # round-trip. The resolver swallows ordinary lookup errors; a refusal
        # for this account is reported, never fatal to the validation itself,
        # and neither is a read that overruns its bound.
        try:
            async with asyncio.timeout(bound):
                geo = await get_camera_geolocation(adapter, camera=config)
        except GeolocationAccessDenied:
            geo = None
            geolocation_status = "access_denied"
        except TimeoutError:
            geo = None
            geolocation_status = "none"
        else:
            geolocation_status = "ok" if geo is not None else "none"
    finally:
        await adapter.close()
    if geo is not None and geo.source == "camera":
        await run_in_threadpool(_persist_device_geolocation, camera_id, geo)
    reason = result.reason.value if result.reason is not None else None
    # A validation failure message can be the last line of ffmpeg/adapter stderr,
    # which may embed the probed URL including any user:pass@ credential. Redact
    # it before returning it to the operator, mirroring the web validate path.
    return ValidationOut(
        ok=result.ok,
        reason=reason,
        message=redact_text(result.message),
        geolocation_status=geolocation_status,
    )


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

    A frame must belong to a project, so a ``project_id`` is required. The
    capture goes through the one-shot path the exact-time anchors use (#69
    CAM-R10): the camera's cached session, one of its capture slots, the
    project's stream and PTZ position, the capture timeout, the scene-metadata
    step, the disk gate and the shared frame writer. The frame is tagged
    ``capture_reason="manual"`` and one project event records the outcome.

    It never waits for a slot: when the camera is busy (an anchor is
    capturing) the request fails at once with a ``503``, so a manual capture
    can never queue ahead of a scheduled one.

    Status codes: ``404`` unknown camera or project (or a camera deleted
    mid-capture); ``400`` the project is not on this camera, or the camera
    configuration is unusable; ``409`` the project is at its frame cap;
    ``503`` no capture engine, or a local failure such as a busy camera;
    ``504`` the camera timed out; ``502`` any other camera failure; ``507``
    skipped, free disk space below the low watermark. A camera's error text is
    redacted before it is returned.
    """
    supervisor = _supervisor()
    target = await run_in_threadpool(
        _target_for_project, supervisor, payload.project_id, camera_id
    )
    try:
        written = await capture_one_now(
            supervisor,
            target,
            reason="manual",
            slot_wait_seconds=_MANUAL_SLOT_WAIT_SECONDS,
        )
    except CameraMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera {camera_id} not found",
        ) from exc
    except CaptureError as exc:
        raise _capture_error_response(exc) from exc
    except ValueError as exc:
        # build_adapter's refusal of an unusable configuration.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=redact_text(str(exc))
        ) from exc
    if written is None:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="capture skipped: low disk",
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


def _capture_error_response(exc: CaptureError) -> HTTPException:
    """Map a failed manual capture to its HTTP error (#69 CAM-R10).

    A local failure (no free slot, no pooled connection, an undecryptable
    credential) is this server's, not the camera's: ``503``. A camera timeout
    is ``504``; any other camera failure ``502``. The message is redacted,
    since adapter text can carry a probed URL's userinfo.
    """
    message = redact_text(str(exc) or type(exc).__name__)[:300]
    if exc.local:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif exc.reason is ValidationFailure.TIMEOUT:
        code = status.HTTP_504_GATEWAY_TIMEOUT
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=code, detail=f"capture failed: {message}")


@router.post(
    "/discover",
    response_model=list[DiscoveredOut],
    dependencies=[Depends(require_operator_or_admin_principal)],
)
async def discover_cameras(
    payload: Annotated[DiscoverRequest | None, Body()] = None,
) -> list[DiscoveredOut]:
    """Discover cameras: scan an address range if given, else ONVIF on the LAN.

    An oversized scan range (beyond the configured cap) is rejected as a ``422``
    with an actionable message instead of being silently truncated.

    Discovered ONVIF cameras are then best-effort enriched with their snapshot/
    stream URIs through the running capture supervisor's shared HTTP client,
    **without any credentials** (the saved default login is never sent to a
    scanned device; #67-S3). Enrichment never makes
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
        # Enrichment carries no credentials (#67-S3, CAM-S4).
        try:
            found = await asyncio.wait_for(
                resolve_discovered_uris(
                    found,
                    supervisor.client_for("probe"),
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
) -> DetectOut:
    """Probe an address across all protocols and return every responder.

    The supplied address is validated through the same host-resolution
    chokepoint camera creation uses *before* any probe runs; a denied address is
    rejected as a ``422`` and no probe is attempted. When the request carries no
    credentials, the saved global default (if any) is substituted, matching the
    capture/validate paths -- but only for an address inside the allowed private
    subnets (owner decision OD-1); elsewhere the probe runs without it and the
    response's ``credentials_hint`` says so. The shared HTTP client and ffmpeg
    binary come from the running capture supervisor -- this path never opens its
    own client. The response never echoes credentials back.
    """
    # SSRF chokepoint first: validate-and-reject before probing anything.
    try:
        await _resolve_camera_host_async(payload.address)
    except SsrfError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    credentials = _credentials_from_request(payload.credentials)
    typed_credentials = credentials is not None
    credentials_hint: str | None = None
    if credentials is None:
        # Offload the synchronous default-credentials read (and the name
        # resolution that binds it to the address) to a worker thread so it
        # never blocks the event loop.
        credentials, withheld = await run_in_threadpool(
            _resolve_bound_default_credentials_threadsafe, payload.address
        )
        credentials_hint = DEFAULT_WITHHELD_HINT if withheld else None

    supervisor = _supervisor()
    pinned_cert_pem = (
        await run_in_threadpool(_pinned_cert_of, payload.camera_id)
        if payload.camera_id is not None
        else None
    )
    probe_client = supervisor.client_for("probe", pinned_cert_pem)
    if credentials is not None and not typed_credentials:
        # The saved default login stays inside the allowed private subnets at
        # connect time too, not only at the binding check (#268).
        probe_client = contained_client(probe_client)
    try:
        async with asyncio.timeout(_DETECT_TIMEOUT_SECONDS):
            outcome = await detect_protocols(
                payload.address,
                credentials,
                probe_client,
                ffmpeg_binary=supervisor.ffmpeg_binary,
            )
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"detection timed out after {_DETECT_TIMEOUT_SECONDS:.0f}s",
        ) from None
    return _detect_out(outcome, credentials_hint)


@router.get("/{camera_id}/capture-status", response_model=CaptureStatusOut)
def capture_status(
    camera_id: int,
    session: DbSessionDep,
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
            last_error=(
                redact_text(state.last_error) if state.last_error is not None else None
            ),
            frames_captured=state.frames_captured,
            attempt_count=state.attempt_count,
            next_retry_at=(
                state.next_retry_at.isoformat()
                if state.next_retry_at is not None
                else None
            ),
            last_cycle_at=(
                state.last_cycle_at.isoformat()
                if state.last_cycle_at is not None
                else None
            ),
            restarts=state.restarts,
        )
        for state in supervisor.states_for_camera(camera_id)
    ]
    return CaptureStatusOut(camera_id=camera_id, projects=entries)


def _pinned_cert_of(camera_id: int) -> str | None:
    """Return the certificate pin of camera ``camera_id``, or ``None`` (#233).

    ``None`` too for an unknown id: the probe then runs on the shared trust,
    like any ad-hoc probe. The pin is public data. Opens its own session so it
    can run in a worker thread. Synchronous; call via a thread executor.
    """
    with get_context().session_factory() as session:
        pin = session.execute(
            select(Camera.tls_pinned_cert_pem).where(Camera.id == camera_id)
        ).scalar_one_or_none()
    return pin or None


def _resolve_bound_default_credentials_threadsafe(
    address: str,
) -> tuple[tuple[str, str] | None, bool]:
    """Resolve the default credentials bound to ``address``, in a fresh session.

    Returns ``(credentials, withheld)``: the default when ``address`` resolves
    inside the allowed private subnets (owner decision OD-1), else ``None``
    with ``withheld`` true if a default exists but was held back. The default
    lookup is a synchronous SQLite read; this opens its own session so it can be
    dispatched to a worker thread via ``run_in_threadpool`` without sharing the
    request session across threads, and releases it before resolving the name.
    Synchronous; call via a thread executor.
    """
    context = get_context()
    with context.session_factory() as session:
        default = resolve_default_credentials(session)
    bound = bound_default_credentials([address], default)
    return bound, default is not None and bound is None


def _load_camera_config(camera_id: int) -> tuple[Camera, tuple[str, str] | None]:
    """Load a camera row and the resolved default credentials for adapter build.

    Returns the (detached) camera row alongside the global fallback
    ``(username, password)`` resolved once in the same session (or ``None`` when
    no fallback applies). Adapter construction only reads plain attributes on the
    expunged row, so no lazy loads occur. The validate path's config load; the
    manual capture goes through the camera's cached capture session instead.
    Synchronous; call via a thread executor.
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
        targets = camera_credential_targets(camera)
        session.expunge(camera)
    # Bound once the session is released: the check resolves names (OD-1).
    return camera, bound_default_credentials(targets, default_credentials)


def _persist_device_geolocation(camera_id: int, geo: GeoLocation) -> None:
    """Store a device-reported geolocation, never overwriting a manual override.

    Synchronous; call via a thread executor. Silently returns if the camera is
    gone or already carries a manual override. A non-finite or out-of-range
    coordinate is never stored: the adapters already drop one, and this is the
    last check before the value reaches the solar-anchor maths.
    """
    if not valid_coordinates(geo.latitude, geo.longitude):
        logger.warning(
            "camera %s: ignoring device geolocation outside the valid range",
            camera_id,
        )
        return
    context = get_context()
    with context.session_factory() as session:
        camera = session.get(Camera, camera_id)
        if camera is None or camera.geolocation_source == "manual":
            return
        camera.geolocation_latitude = geo.latitude
        camera.geolocation_longitude = geo.longitude
        camera.geolocation_source = "camera"
        session.commit()


def _target_for_project(
    supervisor: CaptureSupervisor, project_id: int, camera_id: int
) -> CaptureTarget:
    """Build the manual capture's target for a project on a camera, or raise.

    ``404`` for an unknown camera or project, ``400`` when the project is not
    bound to the camera, ``409`` when the project is already at its frame cap
    (a manual frame would overshoot it). The target is the supervisor's own
    mapping of the rows (:meth:`CaptureSupervisor.target_from_rows`), so the
    project's stream, PTZ position and storage path apply. Returns plain
    values. Synchronous; call via a thread executor.
    """
    context = get_context()
    with context.session_factory() as session:
        camera = session.get(Camera, camera_id)
        if camera is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"camera {camera_id} not found",
            )
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
        cap = project.max_frame_count
        if cap is not None and project.frame_count >= cap:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"project {project_id} is at its frame cap ({cap} frames)",
            )
        return supervisor.target_from_rows(project, camera)
