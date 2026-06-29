"""Camera routes: list, add, edit, validate, protocol detection, discovery,
live query, stream-profile and PTZ-preset pickers, and deletion."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ...cameras.base import DiscoveredCamera, PTZPresetsResult, StreamProfileResult
from ...db.models import Camera, User
from ...render import settings as render_settings
from ...runtime import get_context
from ...security.camera_defaults_service import (
    resolve_default_credentials,
)
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ._shared import (
    _audit,
    _enumerate_ptz_presets,
    _enumerate_stream_profiles,
    _hostname_source,
    _parse_coordinate,
)
from ._viewmodels import (
    _camera_view,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_DETECT_TIMEOUT_SECONDS = 20.0


_DISCOVER_ENRICH_TIMEOUT_SECONDS = 30.0


@router.get("/cameras", response_class=HTMLResponse)
def cameras_page(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Render the registered-cameras table."""
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()
    return templates.TemplateResponse(
        request,
        "cameras.html",
        deps.base_context(
            request, db, user, cameras=[_camera_view(c) for c in cameras]
        ),
    )


@router.post("/cameras")
def create_camera(
    request: Request, db: DbDep, user: OperatorUser, form: FormDep
) -> Response:
    """Create a camera from the submitted form, then redirect to the cameras page.

    The address passes through the same host-resolution chokepoint the API uses.
    Credentials are stored as-is in the camera's credential document and are
    never echoed back to any template.
    """
    from ...cameras import resolve_camera_host
    from ...security.crypto import encrypt_credentials
    from ...security.ssrf import SsrfError

    name = form.get("name", "")
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    protocol = form.get("protocol") or None
    address = form.get("address") or None
    username = form.get("username") or None
    password = form.get("password") or None
    try:
        resolved = resolve_camera_host(address) if address else None
    except SsrfError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"address rejected: {exc}",
        ) from exc
    # When the camera inherits the global default, it carries no credentials of
    # its own -- otherwise those would always win over the default. The per-camera
    # inputs are disabled in that mode (so the browser submits nothing), but the
    # server enforces it too: a tampered or scripted submission cannot smuggle in
    # own credentials alongside the inherit flag.
    inherit_default = bool(form.get("credentials_inherit_default"))
    credentials: dict[str, Any] | None = None
    if not inherit_default and (username or password):
        credentials = {"username": username, "password": password}

    # Optional geolocation. Coordinates are kept only when both are supplied; a
    # half-pair or a bad/out-of-range value is rejected with a 400 (the create
    # handler's validation idiom). The source records how they were obtained.
    latitude, err = _parse_coordinate(form.get("latitude"), "Latitude", limit=90)
    if err is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)
    longitude, err = _parse_coordinate(form.get("longitude"), "Longitude", limit=180)
    if err is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)
    geo_source: str | None = (
        form.get("geo_source")
        if form.get("geo_source") in ("camera", "manual")
        else "manual"
    )
    if latitude is None or longitude is None:
        latitude = longitude = None
        geo_source = None

    # Optional network hostname. Stored only when a value is supplied; the source
    # records how it was obtained, mirroring the geolocation source.
    device_hostname = (form.get("device_hostname") or "").strip() or None
    device_hostname_source = _hostname_source(form) if device_hostname else None

    camera = Camera(
        name=name,
        protocol=protocol or None,
        address=resolved,
        credentials=encrypt_credentials(credentials),
        credentials_inherit_default=inherit_default,
        snapshot_uri=form.get("snapshot_uri") or None,
        stream_uri=form.get("stream_uri") or None,
        geolocation_latitude=latitude,
        geolocation_longitude=longitude,
        geolocation_source=geo_source,
        device_hostname=device_hostname,
        device_hostname_source=device_hostname_source,
    )
    db.add(camera)
    db.flush()
    _audit(
        db,
        scope="camera",
        scope_id=camera.id,
        actor_user_id=user.id,
        message=f"camera {camera.id} created",
    )
    return RedirectResponse(url="/cameras?created=1", status_code=303)


# NOTE: ``GET /cameras/add-form`` is registered BEFORE the parametrised
# ``/cameras/{camera_id:int}/...`` routes and the literal ``add-form`` segment
# cannot be mistaken for an integer id, so a request for the add form is never
# shadowed onto a ``{camera_id}`` route. (The historical 405 was a method
# mismatch against ``DELETE /cameras/{camera_id}``; registering this GET resolves
# it, and the explicit ordering documents the intent.)


@router.get("/cameras/add-form", response_class=HTMLResponse)
def camera_add_form(request: Request, db: DbDep, user: OperatorUser) -> Response:
    """Return the inline create-camera form fragment for HTMX.

    Rendered into the cameras page's form container. Open to operators and
    admins, like every camera mutation. The fragment posts to ``POST /cameras``
    (a normal form submit that redirects), matching the existing create handler.
    """
    return templates.TemplateResponse(
        request,
        "_partials/camera_form.html",
        deps.base_context(
            request,
            db,
            user,
            camera=None,
            default_username=_default_credentials_username(db),
        ),
    )


@router.get("/cameras/{camera_id:int}/edit-form", response_class=HTMLResponse)
def camera_edit_form(
    request: Request, db: DbDep, user: OperatorUser, camera_id: int
) -> Response:
    """Return the inline edit-camera form fragment, prefilled, for HTMX.

    The stored credential password is never rendered back; only the (plaintext)
    username is prefilled. The fragment ``hx-post``s to the edit-apply route and
    swaps the camera's row on success.
    """
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return templates.TemplateResponse(
        request,
        "_partials/camera_form.html",
        deps.base_context(
            request,
            db,
            user,
            camera=_camera_view(camera),
            camera_username=_camera_username(camera),
            default_username=_default_credentials_username(db),
        ),
    )


def _camera_username(camera: Camera) -> str:
    """Return the stored credential username for prefill, or an empty string.

    The username is a non-secret field stored in clear in the credential
    document, so no decryption is needed; the password is never read back.
    """
    credentials = camera.credentials or {}
    value = credentials.get("username")
    return str(value) if value else ""


def _default_credentials_username(db: DbSession) -> str:
    """Return the active default-credentials username for the form's info line.

    Surfaced (non-secret) so the camera form can show which login an inheriting
    camera will fall back to. Empty when the fallback is disabled or no username
    is configured. The default password is never read here.
    """
    creds = resolve_default_credentials(db)
    return creds[0] if creds is not None else ""


def _camera_row_response(
    request: Request, db: DbSession, user: User, camera: Camera
) -> Response:
    """Render the single camera-row fragment after an edit (outerHTML swap)."""
    return templates.TemplateResponse(
        request,
        "_partials/camera_row.html",
        deps.base_context(request, db, user, camera=_camera_view(camera)),
    )


def _camera_form_error(
    request: Request,
    db: DbSession,
    user: User,
    camera: Camera | None,
    message: str,
) -> Response:
    """Re-render the camera form fragment with an inline error, at 200.

    Returned at 200 (not 4xx) on purpose: HTMX only swaps successful responses,
    so an error fragment must be a 200 to replace the form and show the message.
    The form is re-rendered in the same mode (create when ``camera`` is ``None``,
    edit otherwise) so the user can correct and resubmit.
    """
    return templates.TemplateResponse(
        request,
        "_partials/camera_form.html",
        deps.base_context(
            request,
            db,
            user,
            camera=_camera_view(camera) if camera is not None else None,
            camera_username=_camera_username(camera) if camera is not None else "",
            default_username=_default_credentials_username(db),
            flash_messages=[{"type": "error", "message": message}],
        ),
    )


@router.post("/cameras/{camera_id:int}/edit", response_class=HTMLResponse)
def edit_camera(
    request: Request, db: DbDep, user: OperatorUser, camera_id: int, form: FormDep
) -> Response:
    """Apply an edit to a camera and return its refreshed row fragment.

    Credential preservation: when the password field is left blank the stored
    credentials are kept intact -- only the (non-secret) username is refreshed on
    the existing document -- so an edit that does not retype the password never
    wipes it. A supplied password re-encrypts the whole credential document. The
    address is re-validated through the same host-resolution chokepoint the
    create path uses whenever it changes. Validation runs before the row is
    mutated, and the JSON credential column is reassigned (never mutated in
    place) so the change is detected.
    """
    from sqlalchemy.exc import IntegrityError

    from ...cameras import resolve_camera_host
    from ...security.crypto import encrypt_credentials
    from ...security.ssrf import SsrfError

    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    name = form.get("name", "").strip()
    if not name:
        return _camera_form_error(request, db, user, camera, "Camera name is required.")
    protocol = form.get("protocol") or None
    address = form.get("address") or None
    username = form.get("username") or None
    password = form.get("password") or None

    # Pre-check the unique name (excluding this camera's own row) before mutating,
    # so the error re-render's reads cannot autoflush a poisoned name and 500.
    existing = db.execute(
        select(Camera.id).where(Camera.name == name).where(Camera.id != camera_id)
    ).scalar_one_or_none()
    if existing is not None:
        return _camera_form_error(
            request, db, user, camera, f"A camera named {name!r} already exists."
        )

    # Re-validate the address through the SSRF chokepoint only when it changed,
    # mirroring the create path's contract (validate-and-reject, never rewrite).
    resolved = camera.address
    if address != camera.address:
        try:
            resolved = resolve_camera_host(address) if address else None
        except SsrfError as exc:
            return _camera_form_error(
                request, db, user, camera, f"Address rejected: {exc}"
            )

    # Geolocation. Parse and range-check before mutating the row so an error
    # re-render's reads cannot autoflush a half-mutated camera. The fields are
    # touched only when present in the submission (mirroring the snapshot/stream
    # URIs below): a form that omits them entirely leaves the stored location
    # intact, while a present-but-blank pair clears it.
    geo_in_form = "latitude" in form or "longitude" in form or "geo_source" in form
    geo_latitude: float | None = None
    geo_longitude: float | None = None
    geo_source: str | None = None
    if geo_in_form:
        geo_latitude, err = _parse_coordinate(
            form.get("latitude"), "Latitude", limit=90
        )
        if err is not None:
            return _camera_form_error(request, db, user, camera, err)
        geo_longitude, err = _parse_coordinate(
            form.get("longitude"), "Longitude", limit=180
        )
        if err is not None:
            return _camera_form_error(request, db, user, camera, err)
        geo_source = (
            form.get("geo_source")
            if form.get("geo_source") in ("camera", "manual")
            else "manual"
        )
        if geo_latitude is None or geo_longitude is None:
            geo_latitude = geo_longitude = None
            geo_source = None

    # When the camera inherits the global default it carries no credentials of
    # its own -- clear any stored ones so the default actually applies (own creds
    # would otherwise always win). The per-camera inputs are disabled in that
    # mode (so the browser submits nothing), but the server enforces it too.
    inherit_default = bool(form.get("credentials_inherit_default"))
    credentials: dict[str, Any] | None
    if inherit_default:
        credentials = None
    # Credential preservation. Reassign the whole JSON document so SQLAlchemy
    # detects the change; never re-encrypt the already-encrypted stored password.
    elif password:
        credentials = encrypt_credentials({"username": username, "password": password})
    else:
        existing_creds = dict(camera.credentials or {})
        if username or existing_creds:
            existing_creds["username"] = username
            credentials = existing_creds or None
        else:
            credentials = None

    camera.name = name
    camera.protocol = protocol or None
    camera.address = resolved
    camera.credentials = credentials
    camera.credentials_inherit_default = inherit_default
    # Snapshot/stream URIs round-trip through the form's inputs. Update only when
    # the field is actually present in the submission, so a form (or caller) that
    # omits them leaves the stored values intact; a present-but-empty value
    # clears them.
    if "snapshot_uri" in form:
        camera.snapshot_uri = form.get("snapshot_uri") or None
    if "stream_uri" in form:
        camera.stream_uri = form.get("stream_uri") or None
    # Network hostname round-trips through the form's input. Touch the columns only
    # when the field is present in the submission (mirroring the URIs above): a
    # form that omits it leaves the stored hostname intact; a present-but-blank
    # value clears it. The source records how the value was obtained.
    if "device_hostname" in form:
        new_hostname = (form.get("device_hostname") or "").strip() or None
        camera.device_hostname = new_hostname
        camera.device_hostname_source = _hostname_source(form) if new_hostname else None
    if geo_in_form:
        camera.geolocation_latitude = geo_latitude
        camera.geolocation_longitude = geo_longitude
        camera.geolocation_source = geo_source
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        camera = db.get(Camera, camera_id)
        return _camera_form_error(
            request, db, user, camera, f"A camera named {name!r} already exists."
        )

    _audit(
        db,
        scope="camera",
        scope_id=camera_id,
        actor_user_id=user.id,
        message=f"camera {camera_id} updated",
    )
    db.flush()
    return _camera_row_response(request, db, user, camera)


@router.post("/cameras/{camera_id}/validate", response_class=HTMLResponse)
async def validate_camera(
    request: Request, db: DbDep, user: OperatorUser, camera_id: int
) -> Response:
    """Probe a camera's reachability and return a short HTML result fragment."""
    from ...cameras import build_adapter

    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return HTMLResponse("<span>Capture engine unavailable.</span>")
    # Resolve the global fallback once before detaching the row so the manual
    # validate probe uses the same effective credentials a real capture would.
    default_credentials = resolve_default_credentials(db)
    db.expunge(camera)
    try:
        adapter = build_adapter(
            camera,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError as exc:
        return HTMLResponse(f"<span>Invalid configuration: {exc}</span>")
    try:
        result = await adapter.validate_connection()
    finally:
        await adapter.close()
    text = "OK" if result.ok else "Failed"
    return HTMLResponse(f"<span>{text}: {result.message}</span>")


@router.get("/cameras/{camera_id:int}/geolocation", response_class=HTMLResponse)
async def camera_geolocation(
    request: Request, db: DbDep, user: OperatorUser, camera_id: int
) -> Response:
    """Poll a saved camera for its reported geolocation and return a fragment.

    Read-only; gated to operators and admins like the rest of the camera form.
    Swapped inline into the edit form's poll-result slot so the operator can copy
    the reported coordinates into the form. Every reachability problem -- no
    capture engine, an SSRF-rejected address, an invalid adapter configuration, or
    an unreachable device -- folds into ``error="unreachable"``; a camera that is
    reachable but reports no position yields ``error="no_location"``. The probe
    never raises to the caller, so an offline camera cannot 500 the page. The
    camera's address is re-resolved through the SSRF chokepoint here (the guard is
    check-time), the row is detached before the probe, and the adapter is always
    closed -- matching the stream-profile and validate paths.
    """
    from ...cameras import build_adapter, resolve_camera_host
    from ...security.ssrf import SsrfError

    def _result(
        *,
        lat: float | None,
        lon: float | None,
        error: str | None,
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "_partials/camera_geo_poll_result.html",
            deps.base_context(
                request, db, user, fetched_lat=lat, fetched_lon=lon, error=error
            ),
        )

    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return _result(lat=None, lon=None, error="unreachable")

    if camera.address:
        try:
            resolve_camera_host(camera.address)
        except SsrfError:
            return _result(lat=None, lon=None, error="unreachable")

    default_credentials = resolve_default_credentials(db)
    db.expunge(camera)
    try:
        adapter = build_adapter(
            camera,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError:
        return _result(lat=None, lon=None, error="unreachable")
    try:
        location = await adapter.get_geolocation()
    except Exception:  # noqa: BLE001 -- an offline camera must never 500 the form.
        return _result(lat=None, lon=None, error="unreachable")
    finally:
        await adapter.close()

    if location is None:
        return _result(lat=None, lon=None, error="no_location")
    return _result(lat=location.latitude, lon=location.longitude, error=None)


@router.post("/cameras/detect-protocol", response_class=HTMLResponse)
async def detect_camera_protocol(
    request: Request, db: DbDep, user: OperatorUser, form: FormDep
) -> Response:
    """Probe the entered address across all protocols and return a result list.

    Reads the address and (optional) credentials from the same form the camera
    add/edit fragment posts. The address is validated through the SSRF
    host-resolution chokepoint *before* any probe; a denied address re-renders
    the fragment with an error and never probes. Blank credentials fall back to
    the saved global default, matching the validate/capture paths. The rendered
    fragment lists every responder as a radio option (the recommended primary
    pre-selected) and never echoes any credential.
    """
    import asyncio

    from ...cameras import detect_protocols, resolve_camera_host
    from ...security.ssrf import SsrfError

    address = (form.get("address") or "").strip()
    if not address:
        return templates.TemplateResponse(
            request,
            "_partials/camera_detect_results.html",
            deps.base_context(request, db, user, error="Enter an address to detect."),
        )

    # SSRF chokepoint first: validate-and-reject before probing anything.
    try:
        resolve_camera_host(address)
    except SsrfError as exc:
        return templates.TemplateResponse(
            request,
            "_partials/camera_detect_results.html",
            deps.base_context(request, db, user, error=f"Address rejected: {exc}"),
        )

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return templates.TemplateResponse(
            request,
            "_partials/camera_detect_results.html",
            deps.base_context(request, db, user, error="Capture engine unavailable."),
        )

    # Blank form credentials fall back to the saved global default, exactly as
    # the validate/capture paths resolve effective credentials.
    username = form.get("username") or None
    credentials: tuple[str, str] | None = None
    if username:
        credentials = (username, form.get("password") or "")
    else:
        credentials = resolve_default_credentials(db)

    outcome = await asyncio.wait_for(
        detect_protocols(
            address,
            credentials,
            supervisor.http_client,
            ffmpeg_binary=supervisor.ffmpeg_binary,
        ),
        timeout=_DETECT_TIMEOUT_SECONDS,
    )
    ok_count = sum(1 for c in outcome.candidates if c.ok)
    return templates.TemplateResponse(
        request,
        "_partials/camera_detect_results.html",
        deps.base_context(
            request,
            db,
            user,
            candidates=outcome.candidates,
            recommended_primary=outcome.recommended_primary,
            ok_count=ok_count,
            error=None,
        ),
    )


@router.post("/cameras/query", response_class=HTMLResponse)
async def query_camera_endpoint(
    request: Request, db: DbDep, user: OperatorUser, form: FormDep
) -> Response:
    """Probe an address for protocols, hostname, and geolocation in one action.

    Accepts the same address + (optional) credential fields the detect-protocol
    route reads from the camera add/edit form. The address is validated through
    the SSRF host-resolution chokepoint *before* any probe; a denied address
    re-renders the panel with an error and never probes. Blank credentials fall
    back to the saved global default (the credential inputs are disabled while
    inheriting, so the browser submits nothing). The rendered panel surfaces the
    detected protocols, the discovered hostname, and the reported location, each
    degrading independently; no credential is ever echoed.
    """
    from ...cameras import resolve_camera_host
    from ...cameras.autoquery import query_camera
    from ...security.ssrf import SsrfError

    def _panel(**ctx: Any) -> Response:
        return templates.TemplateResponse(
            request,
            "_partials/camera_query_results.html",
            deps.base_context(request, db, user, **ctx),
        )

    address = (form.get("address") or "").strip()
    if not address:
        return _panel(error="Enter an address to query.")

    # SSRF chokepoint first: validate-and-reject before probing anything.
    try:
        resolve_camera_host(address)
    except SsrfError as exc:
        return _panel(error=f"Address rejected: {exc}")

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        # Mirror detect-protocol: a missing capture engine re-renders the panel
        # with an inline notice at 200 (so HTMX still swaps it in), not a status
        # error that would leave the panel blank.
        return _panel(error="Capture engine unavailable.")

    # The camera's own credentials are the document the form supplied (when not
    # inheriting); the global default is passed separately as the fallback pair,
    # exactly the way the capture path resolves effective credentials.
    username = form.get("username") or None
    own_credentials: dict[str, Any] | None = None
    if username:
        own_credentials = {"username": username, "password": form.get("password") or ""}

    result = await query_camera(
        address=address,
        credentials=own_credentials,
        http_client=supervisor.http_client,
        default_credentials=resolve_default_credentials(db),
        ffmpeg_binary=supervisor.ffmpeg_binary,
    )
    return _panel(
        candidates=result.candidates,
        recommended_primary=result.recommended_primary,
        ok_count=result.ok_count,
        discovered_hostname=result.discovered_hostname,
        fetched_lat=result.fetched_lat,
        fetched_lon=result.fetched_lon,
        error_protocol=result.error_protocol,
        error_hostname=result.error_hostname,
        error_geo=result.error_geo,
        auth_rejected=result.auth_rejected,
        error=None,
    )


@router.get("/cameras/stream-profiles", response_class=HTMLResponse)
async def camera_stream_profiles(
    request: Request, db: DbDep, user: OperatorUser
) -> Response:
    """Return the stream-profile picker fragment for a camera.

    Read-only; gated to operators and admins like the project forms. The camera is
    named by a ``camera_id`` query parameter (the project forms ``hx-include`` the
    camera ``<select>``). The camera's streams are enumerated best-effort: any
    problem -- a missing or unknown camera, an unreachable device, an
    SSRF-rejected address, or a camera that reports no selectable streams --
    renders the partial's inline notice. The response is always HTTP 200 so HTMX
    swaps it in; the only non-200 outcome is the role gate, which a viewer hits
    before this handler runs.
    """
    raw_camera_id = request.query_params.get("camera_id") or ""
    camera: Camera | None = None
    try:
        camera = db.get(Camera, int(raw_camera_id))
    except (TypeError, ValueError):
        camera = None

    if camera is None:
        result = StreamProfileResult(profiles=[], ok=False, message="no camera")
    else:
        result = await _enumerate_stream_profiles(db, camera)

    return templates.TemplateResponse(
        request,
        "_partials/stream_profile_select.html",
        deps.base_context(
            request,
            db,
            user,
            profiles=result.profiles,
            profiles_ok=result.ok,
            selected_stream_id=None,
        ),
    )


@router.get("/cameras/ptz-presets", response_class=HTMLResponse)
async def camera_ptz_presets(
    request: Request, db: DbDep, user: OperatorUser
) -> Response:
    """Return the per-project PTZ preset/position picker fragment for a camera.

    Read-only; gated to operators and admins like the project forms. The camera is
    named by a ``camera_id`` query parameter (the project forms ``hx-include`` the
    camera ``<select>``). The presets are enumerated best-effort: any problem -- a
    missing or unknown camera, an unreachable device, an SSRF-rejected address, or
    an adapter that exposes no PTZ -- renders the partial's inline state. The
    response is always HTTP 200 so HTMX swaps it in; the only non-200 outcome is
    the role gate, which a viewer hits before this handler runs.
    """
    raw_camera_id = request.query_params.get("camera_id") or ""
    camera: Camera | None = None
    try:
        camera = db.get(Camera, int(raw_camera_id))
    except (TypeError, ValueError):
        camera = None

    if camera is None:
        result = PTZPresetsResult(
            presets=[], ptz_supported=False, ok=False, message="no camera"
        )
    else:
        result = await _enumerate_ptz_presets(db, camera)

    return templates.TemplateResponse(
        request,
        "_partials/ptz_preset_select.html",
        deps.base_context(
            request,
            db,
            user,
            presets=result.presets,
            presets_ok=result.ok,
            ptz_supported=result.ptz_supported,
            selected_preset_id=None,
            ptz_pan=None,
            ptz_tilt=None,
            ptz_zoom=None,
        ),
    )


@router.get("/renders/combo-check", response_class=HTMLResponse)
def render_combo_check(request: Request, user: OperatorUser) -> Response:
    """Return the advisory fragment for the chosen encoder/container pair.

    Reads the chosen ``render_encoder`` and ``render_container`` from the query
    string (the edit form ``hx-include``s its ``render_*`` controls) and stacks up
    to two advisories in the ``#render-combo-warning`` region:

    * an ``.alert warning`` when the combination cannot be muxed (the same rule
      the save handler enforces, so the live warning and the server's refusal
      never disagree), and
    * an ``.alert info`` "download only" notice when a muxable combination will
      not play inline in the browser and so is offered as a download (e.g. AV1).

    A combination that is both muxable and browser-streamable returns an empty
    element so HTMX clears any prior advisory. Always HTTP 200.
    """
    from html import escape

    from ...encode.browser_streamable import is_browser_streamable

    encoder = request.query_params.get("render_encoder") or ""
    container = request.query_params.get("render_container") or ""

    parts: list[str] = []
    warning = render_settings.combination_warning(encoder, container)
    muxable = render_settings.is_supported_combination(encoder, container)
    if warning is not None:
        parts.append(f'<div class="alert warning">{escape(warning)}</div>')
    # Only advise "download only" for a combination that can actually be produced;
    # an unmuxable pair already shows the warning above and would never be encoded.
    if muxable and not is_browser_streamable(encoder, container):
        parts.append(
            '<div class="alert info">Download only — this encoder won’t '
            "preview inline in the browser.</div>"
        )
    if not parts:
        return HTMLResponse("")
    return HTMLResponse(f'<div class="form-stack">{"".join(parts)}</div>')


@router.delete("/cameras/{camera_id}", response_class=HTMLResponse)
def delete_camera(
    request: Request, db: DbDep, user: OperatorUser, camera_id: int
) -> Response:
    """Delete a camera and return an empty fragment so HTMX removes the row."""
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    db.delete(camera)
    _audit(
        db,
        scope="camera",
        scope_id=camera_id,
        actor_user_id=user.id,
        message=f"camera {camera_id} deleted",
    )
    return HTMLResponse("")


def _render_discovery_results(found: list[DiscoveredCamera]) -> str:
    """Render the discovered-camera list fragment shared by both scan paths.

    The multicast and range scans surface the same shape of result, so they
    render through this one function and stay byte-identical.
    """
    from html import escape

    rows: list[str] = []
    for camera in found:
        label = f"{escape(camera.address)} ({escape(camera.protocol)})"
        if camera.snapshot_uri:
            label += f" &mdash; snapshot: {escape(camera.snapshot_uri)}"
        if camera.stream_uri:
            label += f" &mdash; stream: {escape(camera.stream_uri)}"
        rows.append(f"<li>{label}</li>")
    if rows:
        return f"<ul class='scan-results'>{''.join(rows)}</ul>"
    return "<p>No cameras found.</p>"


@router.post("/cameras/discover", response_class=HTMLResponse)
async def discover_cameras(
    request: Request, db: DbDep, user: OperatorUser, form: FormDep
) -> Response:
    """Discover cameras and return a result fragment.

    With no scan range entered, this listens for ONVIF cameras on the local
    segment (multicast). With a CIDR or dotted IP range entered, it instead
    probes each host in that range -- after validating the range and confirming
    it fits within the host cap, so a malformed range returns an error fragment
    and an oversized range returns a warning fragment, neither of which scans.

    Discovered ONVIF cameras are best-effort enriched with their snapshot/stream
    URIs, resolved with the saved global default credential through the running
    capture supervisor's shared HTTP client, so the operator can see scanning
    resolved them. Enrichment never makes discovery fail: without the supervisor
    the basic addresses are shown, and the whole enrichment is time-boxed.
    """
    import asyncio
    from html import escape

    from ...cameras import (
        InvalidScanRange,
        ScanRangeTooLarge,
        check_scan_range,
        discover_onvif,
        resolve_discovered_uris,
        scan_range,
    )

    scan_range_input = (form.get("scan_range") or "").strip()
    if scan_range_input:
        max_hosts = get_context().settings.ssrf.max_scan_hosts
        try:
            # Count and cap-check before scanning, so an oversized range is
            # refused without being enumerated.
            check_scan_range(scan_range_input, max_hosts)
        except ScanRangeTooLarge as exc:
            return HTMLResponse(
                '<div class="alert warning">'
                f"That range covers {exc.host_count} hosts, over the limit of "
                f"{exc.max_hosts}. Narrow the range and scan again."
                "</div>"
            )
        except InvalidScanRange:
            return HTMLResponse(
                '<div class="alert error">'
                f"{escape(scan_range_input)} is not a valid range. Enter a CIDR "
                "(for example 192.168.1.0/24), a dotted range (for example "
                "192.168.1.10-192.168.1.40), or a single address."
                "</div>"
            )
        found = await scan_range(scan_range_input)
    else:
        found = await discover_onvif()

    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        credentials = resolve_default_credentials(db)
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
            logger.warning("discovery URI enrichment timed out; returning basics")

    return HTMLResponse(_render_discovery_results(found))
