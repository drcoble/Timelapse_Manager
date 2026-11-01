"""Camera routes: list, add, edit, validate, discovery,
live query, stream-profile and PTZ-preset pickers, and deletion."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from ...cameras.base import (
    DiscoveredCamera,
    GeoLocation,
    GeolocationAccessDenied,
    PTZPresetsResult,
    StreamProfileResult,
    valid_coordinates,
)
from ...cameras.tls import (
    CertificateSummary,
    fetch_peer_certificate,
    fingerprints_match,
    summarize_certificate,
)
from ...cameras.vapix import GEOLOCATION_REQUIRED_ROLE
from ...capture.camera_config import has_own_credentials
from ...capture.camera_sessions import describe_ptz_position, ptz_position_of
from ...db.models import Camera, Project, User
from ...render import settings as render_settings
from ...runtime import get_context
from ...security.attempt_limiter import AttemptLimiter
from ...security.camera_defaults_service import (
    resolve_default_credentials,
)
from ...security.credential_binding import (
    DEFAULT_WITHHELD_HINT,
    bound_default_credentials,
    camera_credential_targets,
    introduces_new_host,
    target_host,
)
from ...services import cameras as camera_service
from ...services._base import Actor, run_effects
from ...services.cameras import CameraSpec
from ...services.errors import AppError, ConflictError, NotFoundError, ValidationError
from .. import dependencies as deps
from ..dependencies import (
    AdminUser,
    CurrentUser,
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ._shared import (
    _audit,
    _default_credentials_detaching,
    _enumerate_ptz_presets,
    _enumerate_stream_profiles,
    _hostname_source,
    _parse_coordinate,
    _release_request_connection,
    _resolve_camera_host_async,
)
from ._viewmodels import (
    _camera_view,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# Shown when an edit moved a camera to a new host and so dropped its stored
# login (#67-S3, owner decision OD-1).
_CREDENTIALS_CLEARED_HINT = (
    "Saved. The address changed, so the stored login was removed: "
    "re-enter the password to use it with the new address."
)

# Overall wall-clock cap for the one-action camera query: the protocol probe,
# then the metadata reads. A wedged probe renders an inline notice instead of
# leaving the request (and the HTMX spinner) hanging (#69 CAM-P5).
_QUERY_TIMEOUT_SECONDS = 20.0

# Headroom over the capture timeout for the validate probe's outer bound: the
# adapter's per-request timeouts are per httpx operation, not a total.
_VALIDATE_MARGIN_SECONDS = 5.0


_DISCOVER_ENRICH_TIMEOUT_SECONDS = 30.0


def _validate_bound_seconds() -> float:
    """The validate probe's outer bound: the capture timeout plus headroom."""
    return get_context().settings.capture.timeout_seconds + _VALIDATE_MARGIN_SECONDS


# The camera probe routes below are ``async def`` because they await camera
# network I/O, so their database work (the camera row, the global default
# credential, and the template context, whose CSRF token reads the session row)
# runs in these sync helpers via ``run_in_threadpool``, never on the event loop
# (#50). A row comes back with its columns loaded and is detached before any
# probe, so nothing on the loop can trigger a lazy load afterwards. The last
# helper before a network await also releases the request's connection
# (``_release_request_connection``), so no pooled connection or SQLite lock is
# held while the route waits on a camera (#66 C1/C11).


def _load_probe_camera(
    db: DbSession, camera_id: int
) -> tuple[Camera, tuple[str, str] | None]:
    """Return the detached camera and default credential, or raise a 404.

    The fallback is read before the row is detached so a probe uses the same
    effective credentials a real capture would; it is never logged.
    """
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return camera, _default_credentials_detaching(db, camera)


def _resolve_bound_defaults_and_release(
    db: DbSession, address: str
) -> tuple[tuple[str, str] | None, bool]:
    """Return the default credential bound to ``address``, and whether it was withheld.

    For the address-based probe routes (query), whose only database
    read before their network awaits is the fallback credential. The default is
    released only when ``address`` resolves inside the allowed private subnets
    (owner decision OD-1); otherwise the probe runs with what the operator
    typed, and the second element tells the route to say so. The request's
    connection is released **before** the name is resolved.
    """
    default = resolve_default_credentials(db)
    _release_request_connection(db)
    bound = bound_default_credentials([address], default)
    return bound, default is not None and bound is None


def _probe_pin_for(db: DbSession, raw_camera_id: object) -> str | None:
    """Return the certificate pin of the camera a Detect/Query probe is for (#233).

    ``raw_camera_id`` is the form's ``probe_camera_id`` (set when the camera
    being edited posts the form); a missing, malformed or unknown id means an
    ad-hoc probe of a new address, which uses the shared trust. A pinned camera
    trusts its pin alone, so its probe must use the pinned client or TLS fails
    against its self-signed certificate. The pin is public data. Synchronous;
    call via ``run_in_threadpool`` before the request's connection is released.
    """
    try:
        camera_id = int(str(raw_camera_id).strip())
    except (TypeError, ValueError):
        return None
    pin = db.execute(
        select(Camera.tls_pinned_cert_pem).where(Camera.id == camera_id)
    ).scalar_one_or_none()
    return pin or None


def _load_picker_camera(db: DbSession, raw_camera_id: str) -> Camera | None:
    """Return the picker's camera, or ``None`` for a bad or unknown id.

    ``raw_camera_id`` is the unvalidated ``camera_id`` query parameter; a
    missing, malformed or unknown id yields ``None`` so the picker renders its
    inline notice rather than an error. The row's columns are loaded by the
    ``get``; the enumerate helper detaches it (off the loop) before probing.
    """
    try:
        return db.get(Camera, int(raw_camera_id))
    except (TypeError, ValueError):
        return None


def _load_ptz_picker_reads(
    db: DbSession, raw_camera_id: str, exclude_project_id: int | None = None
) -> tuple[Camera | None, list[str]]:
    """Return the picker's camera and the PTZ positions others use on it.

    The camera is :func:`_load_picker_camera`'s. The list describes each active
    project on that camera with a PTZ position, ``"name (preset 'yard')"``, so
    the picker can warn before a project is added to a camera another project
    moves (#151; the settings form's check is
    :func:`~.projects._ptz_conflicts`). The picker resets the position to
    "none", so every positioned project is a conflict. ``exclude_project_id``
    is the project whose settings form is open: it is not a conflict with itself
    (#167). Synchronous; runs in the threadpool.
    """
    camera = _load_picker_camera(db, raw_camera_id)
    if camera is None:
        return None, []
    query = select(Project).where(
        Project.camera_id == camera.id, Project.lifecycle_state == "active"
    )
    if exclude_project_id is not None:
        query = query.where(Project.id != exclude_project_id)
    others = db.execute(query.order_by(Project.name)).scalars()
    conflicts: list[str] = []
    for other in others:
        position = ptz_position_of(other)
        if position is not None:
            conflicts.append(f"{other.name} ({describe_ptz_position(position)})")
    return camera, conflicts


def _coordinate_pair_error(
    latitude: float | None, longitude: float | None
) -> str | None:
    """Return an operator message when a parsed coordinate pair is not a position.

    :func:`_parse_coordinate` already bounds each value (and its comparison
    rejects ``nan``), so today this is a backstop: it keeps the finite and range
    rule in the single :func:`valid_coordinates` predicate the device parsers and
    the API persist site share. A blank pair (both ``None``) is valid.
    """
    if latitude is None or longitude is None:
        return None
    if valid_coordinates(latitude, longitude):
        return None
    return "Latitude and longitude must be finite numbers within range."


def _render_partial(
    request: Request, db: DbSession, user: User, name: str, **extra: Any
) -> Response:
    """Render a partial with the common template context (call off the loop)."""
    return templates.TemplateResponse(
        request, name, deps.base_context(request, db, user, **extra)
    )


# Outcomes of the no-JS ``POST /cameras/{id}/delete``, carried back to the
# cameras page in its query string (see ``_delete_notice``).
_DELETE_ERROR_CODES = frozenset({"in_use", "not_found"})


def _delete_notice(request: Request) -> dict[str, Any] | None:
    """Return the banner for a no-JS camera delete, from the query string.

    ``?deleted=1`` is a success; ``?delete_error=in_use&n=N`` and
    ``?delete_error=not_found`` are refusals. Only the known codes and a
    positive integer ``n`` are used; anything else is ignored, so the query
    string cannot put arbitrary text on the page.
    """
    params = request.query_params
    if params.get("deleted") == "1":
        return {"kind": "deleted"}
    code = params.get("delete_error")
    if code not in _DELETE_ERROR_CODES:
        return None
    raw_count = params.get("n", "")
    count = int(raw_count) if raw_count.isdigit() and int(raw_count) > 0 else None
    return {"kind": code, "count": count}


def _default_scan_range() -> str:
    """Return the range to prefill the Discover form with, or ``""``.

    Multicast discovery does not cross routed VLANs, so on a segmented network
    the useful scan is a range scan. The administrator already named the
    private subnets cameras may live in (``ssrf.allowed_private_subnets``); the
    first of them that fits ``ssrf.max_scan_hosts`` is offered. Nothing is
    offered when none fits (an IPv6 prefix, a /16 over the cap) or none is
    configured, and the form then falls back to the local-segment scan.
    """
    from ...cameras import InvalidScanRange, check_scan_range

    ssrf = get_context().settings.ssrf
    for subnet in ssrf.allowed_private_subnets:
        try:
            check_scan_range(subnet, ssrf.max_scan_hosts)
        except (InvalidScanRange, ValueError):
            continue
        return subnet
    return ""


@router.get("/cameras", response_class=HTMLResponse)
def cameras_page(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Render the registered-cameras table.

    The Discover modal's range field is prefilled from the allowed private
    subnets (see :func:`_default_scan_range`).
    """
    cameras = db.execute(select(Camera).order_by(Camera.id)).scalars().all()
    return templates.TemplateResponse(
        request,
        "cameras.html",
        deps.base_context(
            request,
            db,
            user,
            cameras=[_camera_view(c) for c in cameras],
            delete_notice=_delete_notice(request),
            scan_range_default=_default_scan_range(),
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
    # With no usable default configured there is nothing to inherit: the form
    # renders no checkbox then, and a crafted POST of the flag is ignored.
    inherit_default = bool(form.get("credentials_inherit_default")) and bool(
        _default_credentials_username(db)
    )
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
    err = _coordinate_pair_error(latitude, longitude)
    if err is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)

    # Optional network hostname. Stored only when a value is supplied; the source
    # records how it was obtained, mirroring the geolocation source.
    device_hostname = (form.get("device_hostname") or "").strip() or None
    device_hostname_source = _hostname_source(form) if device_hostname else None

    # The adapter has already resolved the address (SSRF chokepoint), parsed the
    # coordinates and encrypted the credential document; hand the normalised values
    # to the service, which owns the insert + audit. A duplicate name surfaces as
    # the DB's IntegrityError (an uncaught 500), preserving the pre-service create
    # behaviour (only the edit path performed a friendly uniqueness pre-check).
    spec = CameraSpec(
        name=name,
        protocol=protocol or None,
        address=resolved,
        credentials=encrypt_credentials(credentials),
        credentials_inherit_default=inherit_default,
        allow_basic_over_http=bool(form.get("allow_basic_over_http")),
        snapshot_uri=form.get("snapshot_uri") or None,
        stream_uri=form.get("stream_uri") or None,
        geolocation_latitude=latitude,
        geolocation_longitude=longitude,
        geolocation_source=geo_source,
        device_hostname=device_hostname,
        device_hostname_source=device_hostname_source,
    )
    result = camera_service.create_camera(db, Actor.of(user), spec)
    db.commit()
    run_effects(result.effects, get_context())
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
            camera_allow_basic_over_http=bool(camera.allow_basic_over_http),
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


def _edit_inherit_default(db: DbSession, camera: Camera, form: dict[str, str]) -> bool:
    """Return the inherit-default flag an edit submission should store.

    Defaults configured: the submitted checkbox decides. No usable default (the
    form renders no checkbox then, and a crafted POST of the flag is ignored): a
    camera that was set to inherit keeps the setting, so re-adding defaults later
    applies them again, unless the submission brings credentials of its own.
    Anything else stays off.
    """
    if _default_credentials_username(db):
        return bool(form.get("credentials_inherit_default"))
    if form.get("username") or form.get("password"):
        return False
    return bool(camera.credentials_inherit_default)


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
            camera_allow_basic_over_http=(
                bool(camera.allow_basic_over_http) if camera is not None else False
            ),
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

    # The hosts that could receive this camera's stored credentials before the
    # edit (read now, before anything is mutated).
    hosts_before = camera_credential_targets(camera)

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
        err = _coordinate_pair_error(geo_latitude, geo_longitude)
        if err is not None:
            return _camera_form_error(request, db, user, camera, err)

    # When the camera inherits the global default it carries no credentials of
    # its own -- clear any stored ones so the default actually applies (own creds
    # would otherwise always win). The per-camera inputs are disabled in that
    # mode (so the browser submits nothing), but the server enforces it too.
    inherit_default = _edit_inherit_default(db, camera, form)
    credentials: dict[str, Any] | None
    credentials_cleared = False
    if inherit_default:
        credentials = None
    # Credential preservation. Reassign the whole JSON document so SQLAlchemy
    # detects the change; never re-encrypt the already-encrypted stored password.
    elif password:
        credentials = encrypt_credentials({"username": username, "password": password})
    elif has_own_credentials(camera.credentials) and introduces_new_host(
        hosts_before,
        [
            resolved,
            (
                (form.get("snapshot_uri") or None)
                if "snapshot_uri" in form
                else camera.snapshot_uri
            ),
            (
                (form.get("stream_uri") or None)
                if "stream_uri" in form
                else camera.stream_uri
            ),
        ],
    ):
        # Stored credentials are bound to the hosts they were typed for (#67-S3,
        # OD-1): an edit that points the camera at a host it did not have, without
        # a retyped password, drops them rather than carrying them there.
        credentials = None
        credentials_cleared = True
    else:
        existing_creds = dict(camera.credentials or {})
        if username or existing_creds:
            existing_creds["username"] = username
            credentials = existing_creds or None
        else:
            credentials = None

    # The adapter has normalised every field (address resolved, credential document
    # chosen, coordinates parsed); build a partial-update spec naming exactly the
    # fields to touch and hand it to the service, which owns the uniqueness rule,
    # the flush and the audit. ``name``/``protocol``/``address``/``credentials``/
    # ``credentials_inherit_default``/``allow_basic_over_http`` are always
    # submitted by this form (an unticked checkbox sends nothing, which means
    # off); the snapshot/stream URIs, hostname and geolocation ride only when
    # their fields are present, preserving the "leave omitted fields untouched"
    # semantics.
    fields_set = {
        "name",
        "protocol",
        "address",
        "credentials",
        "credentials_inherit_default",
        "allow_basic_over_http",
    }
    spec_values: dict[str, Any] = {
        "name": name,
        "protocol": protocol or None,
        "address": resolved,
        "credentials": credentials,
        "credentials_inherit_default": inherit_default,
        "allow_basic_over_http": bool(form.get("allow_basic_over_http")),
    }
    if "snapshot_uri" in form:
        spec_values["snapshot_uri"] = form.get("snapshot_uri") or None
        fields_set.add("snapshot_uri")
    if "stream_uri" in form:
        spec_values["stream_uri"] = form.get("stream_uri") or None
        fields_set.add("stream_uri")
    if "device_hostname" in form:
        new_hostname = (form.get("device_hostname") or "").strip() or None
        spec_values["device_hostname"] = new_hostname
        spec_values["device_hostname_source"] = (
            _hostname_source(form) if new_hostname else None
        )
        fields_set.add("device_hostname")
        fields_set.add("device_hostname_source")
    if geo_in_form:
        spec_values["geolocation_latitude"] = geo_latitude
        spec_values["geolocation_longitude"] = geo_longitude
        spec_values["geolocation_source"] = geo_source
        fields_set.add("geolocation_latitude")
        fields_set.add("geolocation_longitude")
        fields_set.add("geolocation_source")

    spec = CameraSpec(**spec_values, fields_set=frozenset(fields_set))
    try:
        result = camera_service.update_camera(db, Actor.of(user), camera_id, spec)
    except AppError as exc:
        # A uniqueness conflict (or any domain rule) rolls back the service's
        # partial mutation and re-renders the form fragment with the error at 200,
        # exactly as the pre-service handler did on a duplicate-name flush.
        db.rollback()
        camera = db.get(Camera, camera_id)
        return _camera_form_error(request, db, user, camera, exc.detail)

    db.commit()
    run_effects(result.effects, get_context())
    if credentials_cleared:
        # Saved, but say why the login is gone: the form comes back open on the
        # camera with the instruction, instead of a row that would simply start
        # failing authentication.
        return _camera_form_error(
            request, db, user, result.value, _CREDENTIALS_CLEARED_HINT
        )
    return _camera_row_response(request, db, user, result.value)


@router.post("/cameras/{camera_id}/validate", response_class=HTMLResponse)
async def validate_camera(
    request: Request, db: DbDep, user: OperatorUser, camera_id: int
) -> Response:
    """Probe a camera's reachability and return a short HTML result fragment.

    The adapter's diagnostic strings (a configuration error, or a probe's outcome
    message) can echo the camera address -- and an address may embed
    ``user:pass@host`` userinfo. Every value reflected into the fragment is first
    run through :func:`redact_text` to strip any embedded URL credential, then HTML
    -escaped, so a credentialed address is neither disclosed nor able to inject
    markup into the operator's page.
    """
    from markupsafe import escape

    from ...cameras import build_adapter
    from ...logging import redact_text

    def _safe(value: str) -> str:
        """Redact any embedded URL credential, then HTML-escape for the fragment."""
        return str(escape(redact_text(value)))

    # Read the row and resolve the global fallback off the loop, detaching the
    # row so the manual validate probe uses the same effective credentials a
    # real capture would.
    camera, default_credentials = await run_in_threadpool(
        _load_probe_camera, db, camera_id
    )
    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return HTMLResponse("<span>Capture engine unavailable.</span>")
    try:
        adapter = build_adapter(
            camera,
            supervisor.client_for("probe", camera.tls_pinned_cert_pem),
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError as exc:
        return HTMLResponse(f"<span>Invalid configuration: {_safe(str(exc))}</span>")
    bound = _validate_bound_seconds()
    try:
        async with asyncio.timeout(bound):
            result = await adapter.validate_connection()
    except TimeoutError:
        return HTMLResponse(
            f"<span>Failed: the camera did not answer within {bound:.0f}s.</span>"
        )
    finally:
        await adapter.close()
    text = "OK" if result.ok else "Failed"
    return HTMLResponse(f"<span>{text}: {_safe(result.message)}</span>")


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
    reachable but reports no position yields ``error="no_location"``; a camera
    that refuses the read for this account (Axis serves the position to an
    Administrator only) yields ``error="access_denied"`` with the role that can
    read it, and the fragment offers the one-time admin read. The probe
    never raises to the caller, so an offline camera cannot 500 the page. The
    camera's address is re-resolved through the SSRF chokepoint here (the guard is
    check-time), the row is detached before the probe, and the adapter is always
    closed -- matching the stream-profile and validate paths.
    """
    from ...cameras import build_adapter
    from ...security.ssrf import SsrfError

    async def _result(
        *,
        lat: float | None,
        lon: float | None,
        error: str | None,
        required_role: str | None = None,
    ) -> Response:
        return await run_in_threadpool(
            _render_partial,
            request,
            db,
            user,
            _GEO_POLL_RESULT_TEMPLATE,
            camera_id=camera_id,
            fetched_lat=lat,
            fetched_lon=lon,
            error=error,
            required_role=required_role,
        )

    camera, default_credentials = await run_in_threadpool(
        _load_probe_camera, db, camera_id
    )

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return await _result(lat=None, lon=None, error="unreachable")

    if camera.address:
        try:
            await _resolve_camera_host_async(camera.address)
        except SsrfError:
            return await _result(lat=None, lon=None, error="unreachable")

    try:
        adapter = build_adapter(
            camera,
            supervisor.client_for("probe", camera.tls_pinned_cert_pem),
            ffmpeg_binary=supervisor.ffmpeg_binary,
            default_credentials=default_credentials,
        )
    except ValueError:
        return await _result(lat=None, lon=None, error="unreachable")
    try:
        location = await adapter.get_geolocation()
    except GeolocationAccessDenied as exc:
        return await _result(
            lat=None, lon=None, error="access_denied", required_role=exc.required_role
        )
    except Exception:  # noqa: BLE001 -- an offline camera must never 500 the form.
        return await _result(lat=None, lon=None, error="unreachable")
    finally:
        await adapter.close()

    if location is None:
        return await _result(lat=None, lon=None, error="no_location")
    return await _result(lat=location.latitude, lon=location.longitude, error=None)


# ---------------------------------------------------------------------------
# One-time "Read with admin account" geolocation lookup (#68)
# ---------------------------------------------------------------------------
#
# Axis serves a camera's position to an Administrator only, while capture needs
# no more than an operator account. Rather than widen the saved capture account,
# the operator can read the position once with an administrator login that is
# used for that single request and then dropped: it is never persisted, logged,
# echoed, audited or attached to an event. The form fields are deliberately named
# apart from the camera credential fields (``username``/``password``) so they can
# never be mistaken for, or leak into, the camera's saved credentials.
#
# Guessing an admin password through this form is rate-limited by its own
# in-memory limiter -- never the login throttle, whose username namespace any
# unauthenticated client can write to. Each attempt is reserved, before the
# probe, against three keys: the camera's resolved IP (so aliases, case and
# trailing-dot spellings of one device share a budget), the camera row, and
# the acting user. Only a 2xx from ``get.cgi`` with the typed login releases the
# reservation; every other outcome counts.

_GEO_POLL_RESULT_TEMPLATE = "_partials/camera_geo_poll_result.html"
GEO_ADMIN_USERNAME_FIELD = "geo_admin_username"
GEO_ADMIN_PASSWORD_FIELD = "geo_admin_password"

# Unreleased attempts allowed per key (device IP, camera, user) in the window.
GEO_ADMIN_MAX_ATTEMPTS = 5
GEO_ADMIN_WINDOW_SECONDS = 300.0

_geo_admin_limiter = AttemptLimiter(
    max_attempts=GEO_ADMIN_MAX_ATTEMPTS, window_seconds=GEO_ADMIN_WINDOW_SECONDS
)

# Cameras with an admin read in progress (#70 WP-70-D). A second read of the
# same camera while one runs -- a double click, a second tab, another user --
# answers ``admin_busy`` without reserving an attempt or probing, so one
# gesture never spends two attempts of the budget or sends the login twice.
# Only touched on the event loop, with no await between check and add.
_geo_admin_inflight: set[int] = set()


def _geo_admin_keys(
    *, device_ip: str, camera_id: int, user_id: int
) -> tuple[tuple[str, str | int], ...]:
    """Return the limiter keys one admin read is counted against."""
    return (("device", device_ip), ("camera", camera_id), ("user", user_id))


def _load_admin_read_camera(
    db: DbSession, camera_id: int, user: User
) -> tuple[Camera, int]:
    """Return the detached camera and the acting user's id, or raise a 404.

    The user id is read here, off the loop, so nothing on the loop can trigger a
    lazy load of the user row. This is the last database hop before the read's
    network awaits, so it releases the request's connection.
    """
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    db.expunge(camera)
    user_id = int(user.id)
    _release_request_connection(db)
    return camera, user_id


async def _resolve_admin_read_target(address: str) -> str | None:
    """Return the validated IP the admin read will key on, or None to refuse.

    The host is the one :func:`admin_read_host` extracts with the adapter's own
    parser; an ambiguous address is refused. The host then goes through the
    camera SSRF chokepoint (same policy as Poll camera), which also returns the
    validated addresses. Unlike the add-time check, an unresolvable host fails
    closed here, since the read is about to connect. The lowest validated IP is
    the key, so a multi-address name maps to one stable key.
    """
    import socket

    from ...cameras.vapix import admin_read_host
    from ...security.ssrf import SsrfError, resolve_and_check_async

    try:
        host = admin_read_host(address)
    except ValueError:
        return None
    ssrf = get_context().settings.ssrf
    try:
        addresses = await resolve_and_check_async(
            host,
            allow_private=True,
            allowed_private_subnets=ssrf.allowed_private_subnets,
        )
    except (SsrfError, socket.gaierror):
        return None
    if not addresses:
        return None
    return min(str(ip) for ip in addresses)


def _finish_admin_read(
    request: Request,
    db: DbSession,
    user: User,
    *,
    camera_id: int,
    outcome: str,
    location: GeoLocation | None,
    required_role: str | None,
) -> Response:
    """Write the audit line and render the poll-result fragment (call off the loop).

    ``outcome`` is the fragment's error code, or ``"ok"`` for a location. The
    audit line records who read which camera and how it ended -- never the
    administrator username or password. It is info-level whatever the outcome:
    a mistyped password must not raise an alert.
    """
    _audit(
        db,
        scope="camera",
        scope_id=camera_id,
        actor_user_id=user.id,
        message=(
            "camera location read with a one-time administrator account "
            f"(outcome: {outcome})"
        ),
    )
    return _render_partial(
        request,
        db,
        user,
        _GEO_POLL_RESULT_TEMPLATE,
        camera_id=camera_id,
        fetched_lat=location.latitude if location is not None else None,
        fetched_lon=location.longitude if location is not None else None,
        error=None if outcome == "ok" else outcome,
        required_role=required_role,
        admin_read=True,
    )


@dataclass(frozen=True)
class _AdminReadResult:
    """How a one-time admin read ended.

    :param outcome: ``"ok"``, ``"no_location"``, ``"admin_denied"`` or
        ``"unreachable"``.
    :param auth_ok: True only when ``get.cgi`` answered 2xx with the typed login.
    :param location: the position read, when ``outcome`` is ``"ok"``.
    """

    outcome: str
    auth_ok: bool = False
    location: GeoLocation | None = None


async def _read_geolocation_once(
    address: str,
    credentials: tuple[str, str],
    pinned_cert_pem: str | None = None,
) -> _AdminReadResult:
    """Read a VAPIX camera's position from ``get.cgi`` with throwaway credentials.

    Builds a short-lived :class:`VapixAdapter` that exists only for this call
    and is always closed; the credentials go no further than its request
    headers. ``address`` is used exactly as stored, so an ``https://`` camera is
    read over HTTPS, and a pinned camera (``pinned_cert_pem``) only ever trusts
    its pinned certificate, so the typed login never reaches an impostor. Only
    the Geolocation API is read -- no legacy fallback -- so
    ``auth_ok`` is set exactly when the device accepted the login.
    """
    from ...cameras.base import AuthCaptureError
    from ...cameras.vapix import VapixAdapter

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        return _AdminReadResult("unreachable")
    adapter = VapixAdapter(
        supervisor.client_for("probe", pinned_cert_pem),
        address=address,
        credentials=credentials,
    )
    try:
        location = await adapter.read_geolocation_api()
    except AuthCaptureError:
        return _AdminReadResult("admin_denied")
    except Exception:  # noqa: BLE001 -- an offline camera must never 500 the form.
        return _AdminReadResult("unreachable")
    finally:
        await adapter.close()
    if location is None:
        return _AdminReadResult("no_location", auth_ok=True)
    return _AdminReadResult("ok", auth_ok=True, location=location)


@router.post(
    "/cameras/{camera_id:int}/geolocation/admin-read", response_class=HTMLResponse
)
async def camera_geolocation_admin_read(
    request: Request, db: DbDep, user: OperatorUser, form: FormDep, camera_id: int
) -> Response:
    """Read a saved camera's position once with a one-time administrator login.

    Operator-gated and CSRF-protected like every camera form. Reads only the
    ``geo_admin_username`` / ``geo_admin_password`` fields -- never the camera's
    own credential fields -- and uses them for this one request: they are not
    saved, logged, echoed in the response, or put in the audit line. The camera
    address must be unambiguous and pass the SSRF chokepoint before anything is
    reserved or probed, and attempts are rate-limited per device IP, camera and
    user (see the section comment above). Returns the same poll-result fragment
    as Poll camera, so a location is applied to the form with the same "Use
    these values" button. Only VAPIX (Axis) cameras are supported: they are the
    ones whose position is restricted to an administrator.
    """

    async def _finish(outcome: str, result: _AdminReadResult | None = None) -> Response:
        return await run_in_threadpool(
            _finish_admin_read,
            request,
            db,
            user,
            camera_id=camera_id,
            outcome=outcome,
            location=result.location if result is not None else None,
            required_role=(
                GEOLOCATION_REQUIRED_ROLE if outcome == "admin_denied" else None
            ),
        )

    camera, user_id = await run_in_threadpool(
        _load_admin_read_camera, db, camera_id, user
    )
    # Check and add with no await in between: atomic on the event loop. The
    # ``finally`` covers every return below, and runs on cancellation too.
    if camera_id in _geo_admin_inflight:
        return await _finish("admin_busy")
    _geo_admin_inflight.add(camera_id)
    try:
        if camera.protocol != "vapix":
            return await _finish("admin_unsupported")
        username = (form.get(GEO_ADMIN_USERNAME_FIELD) or "").strip()
        if not username:
            return await _finish("admin_missing")
        if not camera.address:
            return await _finish("unreachable")

        device_ip = await _resolve_admin_read_target(camera.address)
        if device_ip is None:
            return await _finish("unreachable")

        # Check and reserve in one synchronous call: atomic on the event loop.
        reservation = _geo_admin_limiter.try_reserve(
            _geo_admin_keys(device_ip=device_ip, camera_id=camera_id, user_id=user_id)
        )
        if reservation is None:
            return await _finish("admin_throttled")
        result = await _read_geolocation_once(
            camera.address,
            (username, form.get(GEO_ADMIN_PASSWORD_FIELD) or ""),
            camera.tls_pinned_cert_pem,
        )
        if result.auth_ok:
            _geo_admin_limiter.release(reservation)
        return await _finish(result.outcome, result)
    finally:
        _geo_admin_inflight.discard(camera_id)


# ---------------------------------------------------------------------------
# Certificate pinning (#69 CAM-S1, the pinning half)
# ---------------------------------------------------------------------------
#
# A camera whose certificate no CA vouches for (a factory self-signed one) can
# be pinned to that exact certificate. Pinning is opt-in and explicit: nothing
# here runs on its own, on upgrade or at capture time. An administrator fetches
# the certificate (a display-only handshake that sends no request and no
# credentials), compares the fingerprint, and confirms. The pin route then
# fetches the certificate AGAIN and stores it only if it still has the
# fingerprint that was confirmed, so what is stored is what was approved and the
# form never carries a certificate. Failures are fixed sentences: nothing from a
# handshake or a certificate is echoed back.

_TLS_SECTION_TEMPLATE = "_partials/camera_tls_section.html"
_TLS_FETCH_TIMEOUT_SECONDS = 10.0
_TLS_FINGERPRINT_FIELD = "fingerprint"


def _tls_sni(host: str) -> str | None:
    """Return the SNI name for ``host``: the name itself, ``None`` for an IP literal."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return None


async def _fetch_camera_certificate(
    camera: Camera,
) -> tuple[CertificateSummary | None, str | None]:
    """Fetch the certificate a camera presents, for an operator to confirm.

    Returns ``(summary, None)`` or ``(None, reason)``; the reason is a fixed
    sentence safe to show. The target is the camera's pin URL (see
    :func:`~timelapse_manager.services.cameras.pin_target_url`), its host is
    extracted by the adapter's own parser (an ambiguous address is refused) and
    resolved through the camera SSRF chokepoint, and the handshake is dialled to
    the validated address. An unresolvable host fails closed.
    """
    import socket

    from ...cameras.vapix import admin_read_host
    from ...security.ssrf import SsrfError, resolve_and_check_pinned_async

    try:
        url = camera_service.pin_target_url(
            camera.protocol,
            camera.address,
            camera.snapshot_uri,
            camera.stream_uri,
        )
    except ValidationError as exc:
        return None, exc.detail
    # Dial what the camera's own client will dial: the HTTP adapter reads its
    # snapshot URI with httpx (which also accepts a query string), the VAPIX and
    # ONVIF adapters read the address with the adapter's own parser. Either
    # parser refusing the value (backslash, userinfo, whitespace, a malformed
    # port) refuses the fetch, so the two can never disagree about the host.
    try:
        if camera.protocol == "http":
            parsed = httpx.URL(url)
            host = target_host(url, allow_userinfo=False) or ""
            port = parsed.port or 443
        else:
            host = admin_read_host(url)
            port = urlsplit(url).port or 443
    except (ValueError, httpx.InvalidURL):
        host, port = "", 0
    if not host:
        return None, "The camera's address is ambiguous; correct it first."
    ssrf = get_context().settings.ssrf
    try:
        dial = await resolve_and_check_pinned_async(
            host,
            allow_private=True,
            allowed_private_subnets=ssrf.allowed_private_subnets,
        )
    except SsrfError:
        return None, "The camera's address is not allowed."
    except socket.gaierror:
        return None, "The camera's address did not resolve."
    try:
        der = await fetch_peer_certificate(
            dial,
            port,
            server_hostname=_tls_sni(host),
            timeout=_TLS_FETCH_TIMEOUT_SECONDS,
        )
        return summarize_certificate(der), None
    except (TimeoutError, OSError, ValueError):
        return None, "Could not read a certificate from the camera over TLS."


def _render_tls_section(
    request: Request,
    db: DbSession,
    user: User,
    camera_id: int,
    *,
    cert: CertificateSummary | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> Response:
    """Render the camera's certificate-pin section (call off the loop)."""
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return _render_partial(
        request,
        db,
        user,
        _TLS_SECTION_TEMPLATE,
        camera=_camera_view(camera),
        cert=cert,
        notice=notice,
        error=error,
    )


@router.post(
    "/cameras/{camera_id:int}/tls/fetch-certificate", response_class=HTMLResponse
)
async def camera_tls_fetch_certificate(
    request: Request, db: DbDep, user: AdminUser, camera_id: int
) -> Response:
    """Show the certificate a saved camera presents, for the administrator to confirm.

    Display only: the handshake is unverified and sends no request and no
    credentials, and nothing is stored. The result carries the SHA-256
    fingerprint, subject, issuer and validity, and a button that pins exactly
    that fingerprint.
    """
    camera, _user_id = await run_in_threadpool(
        _load_admin_read_camera, db, camera_id, user
    )
    cert, error = await _fetch_camera_certificate(camera)
    return await run_in_threadpool(
        _render_tls_section, request, db, user, camera_id, cert=cert, error=error
    )


def _store_pin(
    db: DbSession, user: User, camera_id: int, cert: CertificateSummary
) -> tuple[tuple[Any, ...], str | None]:
    """Pin the camera and commit (call off the loop); return ``(effects, error)``."""
    try:
        result = camera_service.pin_camera_certificate(
            db,
            Actor.of(user),
            camera_id,
            cert.pem,
            expected_fingerprint=cert.fingerprint,
        )
    except AppError as exc:
        db.rollback()
        return (), exc.detail
    db.commit()
    return result.effects, None


@router.post("/cameras/{camera_id:int}/tls/pin", response_class=HTMLResponse)
async def camera_tls_pin(
    request: Request, db: DbDep, user: AdminUser, form: FormDep, camera_id: int
) -> Response:
    """Pin a saved camera to the certificate whose fingerprint was confirmed.

    The administrator-only, CSRF-protected confirmation step. The certificate is
    fetched again and stored only when its SHA-256 fingerprint equals the
    submitted one, in any spelling (case, ``:`` separators); if the camera now
    presents something else, nothing is stored and the new certificate is shown
    to be confirmed afresh.
    """
    camera, _user_id = await run_in_threadpool(
        _load_admin_read_camera, db, camera_id, user
    )
    cert, error = await _fetch_camera_certificate(camera)
    if cert is None:
        return await run_in_threadpool(
            _render_tls_section, request, db, user, camera_id, error=error
        )
    if not fingerprints_match(form.get(_TLS_FINGERPRINT_FIELD, ""), cert.fingerprint):
        return await run_in_threadpool(
            _render_tls_section,
            request,
            db,
            user,
            camera_id,
            cert=cert,
            error=(
                "The camera now presents a different certificate than the one "
                "you confirmed. Check the fingerprint again."
            ),
        )
    effects, error = await run_in_threadpool(_store_pin, db, user, camera_id, cert)
    run_effects(effects, get_context())
    return await run_in_threadpool(
        _render_tls_section,
        request,
        db,
        user,
        camera_id,
        error=error,
        notice=None if error else "Certificate pinned.",
    )


@router.post("/cameras/{camera_id:int}/tls/unpin", response_class=HTMLResponse)
def camera_tls_unpin(
    request: Request, db: DbDep, user: AdminUser, camera_id: int
) -> Response:
    """Remove a camera's certificate pin; it verifies against the CA bundle again."""
    try:
        result = camera_service.unpin_camera_certificate(db, Actor.of(user), camera_id)
    except NotFoundError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from None
    db.commit()
    run_effects(result.effects, get_context())
    return _render_tls_section(
        request, db, user, camera_id, notice="Certificate pin removed."
    )


@router.post("/cameras/query", response_class=HTMLResponse)
async def query_camera_endpoint(
    request: Request, db: DbDep, user: OperatorUser, form: FormDep
) -> Response:
    """Probe an address for protocols, hostname, and geolocation in one action.

    Accepts the same address + (optional) credential fields the removed detect-protocol
    route reads from the camera add/edit form. The address is validated through
    the SSRF host-resolution chokepoint *before* any probe; a denied address
    re-renders the panel with an error and never probes. Blank credentials fall
    back to the saved global default (the credential inputs are disabled while
    inheriting, so the browser submits nothing). The rendered panel surfaces the
    detected protocols, the discovered hostname, and the reported location, each
    degrading independently; no credential is ever echoed.
    """
    from ...cameras.autoquery import query_camera
    from ...security.ssrf import SsrfError

    async def _panel(**ctx: Any) -> Response:
        return await run_in_threadpool(
            _render_partial,
            request,
            db,
            user,
            "_partials/camera_query_results.html",
            **ctx,
        )

    address = (form.get("address") or "").strip()
    if not address:
        return await _panel(error="Enter an address to query.")

    # SSRF chokepoint first: validate-and-reject before probing anything.
    try:
        await _resolve_camera_host_async(address)
    except SsrfError as exc:
        return await _panel(error=f"Address rejected: {exc}")

    supervisor = get_context().capture_supervisor
    if supervisor is None:
        # Mirror the other probe routes: a missing capture engine re-renders the panel
        # with an inline notice at 200 (so HTMX still swaps it in), not a status
        # error that would leave the panel blank.
        return await _panel(error="Capture engine unavailable.")

    # An existing camera's pin selects the pinned client (#233).
    pinned_cert_pem = await run_in_threadpool(
        _probe_pin_for, db, form.get("probe_camera_id")
    )

    # The camera's own credentials are the document the form supplied (when not
    # inheriting); the global default is passed separately as the fallback pair,
    # exactly the way the capture path resolves effective credentials.
    username = form.get("username") or None
    own_credentials: dict[str, Any] | None = None
    if username:
        own_credentials = {"username": username, "password": form.get("password") or ""}

    default_credentials, withheld = await run_in_threadpool(
        _resolve_bound_defaults_and_release, db, address
    )
    credentials_hint = (
        DEFAULT_WITHHELD_HINT if withheld and own_credentials is None else None
    )
    try:
        async with asyncio.timeout(_QUERY_TIMEOUT_SECONDS):
            result = await query_camera(
                address=address,
                credentials=own_credentials,
                http_client=supervisor.client_for("probe", pinned_cert_pem),
                default_credentials=default_credentials,
                ffmpeg_binary=supervisor.ffmpeg_binary,
                allow_basic_over_http=bool(form.get("allow_basic_over_http")),
            )
    except TimeoutError:
        return await _panel(
            error=(
                f"The camera query timed out after {_QUERY_TIMEOUT_SECONDS:.0f}s. "
                "Check the address and try again."
            ),
            credentials_hint=credentials_hint,
        )
    return await _panel(
        credentials_hint=credentials_hint,
        candidates=result.candidates,
        recommended_primary=result.recommended_primary,
        ok_count=result.ok_count,
        discovered_hostname=result.discovered_hostname,
        fetched_lat=result.fetched_lat,
        fetched_lon=result.fetched_lon,
        error_protocol=result.error_protocol,
        error_hostname=result.error_hostname,
        error_geo=result.error_geo,
        geo_required_role=result.geo_required_role,
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
    camera = await run_in_threadpool(_load_picker_camera, db, raw_camera_id)

    if camera is None:
        result = StreamProfileResult(profiles=[], ok=False, message="no camera")
    else:
        result = await _enumerate_stream_profiles(db, camera)

    return await run_in_threadpool(
        _render_partial,
        request,
        db,
        user,
        "_partials/stream_profile_select.html",
        profiles=result.profiles,
        profiles_ok=result.ok,
        selected_stream_id=None,
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

    Above the picker it warns when other active projects on the camera point it
    at a PTZ position (#151), as the project settings form does: the camera then
    moves between their captures, and captures on it run one at a time. The
    settings form also sends ``project_id`` so the project being edited is not
    listed as a conflict with itself (#167).
    """
    raw_camera_id = request.query_params.get("camera_id") or ""
    raw_project_id = request.query_params.get("project_id") or ""
    exclude_project_id = int(raw_project_id) if raw_project_id.isdigit() else None
    camera, ptz_conflicts = await run_in_threadpool(
        _load_ptz_picker_reads, db, raw_camera_id, exclude_project_id
    )

    if camera is None:
        result = PTZPresetsResult(
            presets=[], ptz_supported=False, ok=False, message="no camera"
        )
    else:
        result = await _enumerate_ptz_presets(db, camera)

    return await run_in_threadpool(
        _render_partial,
        request,
        db,
        user,
        "_partials/ptz_preset_picker.html",
        presets=result.presets,
        presets_ok=result.ok,
        ptz_supported=result.ptz_supported,
        selected_preset_id=None,
        ptz_pan=None,
        ptz_tilt=None,
        ptz_zoom=None,
        ptz_conflicts=ptz_conflicts,
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
    * an ``.alert info`` notice when a muxable combination is not H.264/MP4:
      such a render plays only in browsers that support its codec (each
      viewer's browser decides, #25) and the others download it.

    A combination that is both muxable and browser-streamable returns an empty
    element so HTMX clears any prior advisory. Always HTTP 200.
    """
    from html import escape

    from ...encode.browser_streamable import is_browser_streamable
    from ...encode.codec_string import playback_label

    encoder = request.query_params.get("render_encoder") or ""
    container = request.query_params.get("render_container") or ""

    parts: list[str] = []
    warning = render_settings.combination_warning(encoder, container)
    muxable = render_settings.is_supported_combination(encoder, container)
    if warning is not None:
        parts.append(f'<div class="alert warning">{escape(warning)}</div>')
    # Only advise for a combination that can actually be produced; an unmuxable
    # pair already shows the warning above and would never be encoded.
    if muxable and not is_browser_streamable(encoder, container):
        # The same "<codec> in <container>" name the can't-play note uses.
        label = escape(playback_label(encoder, container))
        parts.append(
            f'<div class="alert info">Plays only in browsers that support {label};'
            " others download it.</div>"
        )
    if not parts:
        return HTMLResponse("")
    return HTMLResponse(f'<div class="form-stack">{"".join(parts)}</div>')


@router.delete("/cameras/{camera_id}", response_class=HTMLResponse)
def delete_camera(
    request: Request, db: DbDep, user: OperatorUser, camera_id: int
) -> Response:
    """Delete a camera and return an empty fragment so HTMX removes the row."""
    try:
        result = camera_service.delete_camera(db, Actor.of(user), camera_id)
    except NotFoundError as exc:
        # Preserve the pre-service 404 response shape for a missing camera.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from exc
    db.commit()
    run_effects(result.effects, get_context())
    return HTMLResponse("")


@router.get("/cameras/{camera_id:int}/delete-confirm", response_class=HTMLResponse)
def camera_delete_confirm(
    request: Request, db: DbDep, user: OperatorUser, camera_id: int
) -> Response:
    """Full-page confirmation for the no-JavaScript camera delete (#123).

    With JavaScript, ``hx-confirm`` asks before ``DELETE /cameras/{id}``. Without
    it the row's Delete form submits here (a GET, so nothing is deleted) and the
    page asks first; its button posts to ``/cameras/{id}/delete``. A camera that
    is already gone redirects to the cameras page with the "no longer exists"
    banner, as the delete itself does.
    """
    camera = db.get(Camera, camera_id)
    if camera is None:
        return RedirectResponse(
            "/cameras?delete_error=not_found", status_code=status.HTTP_303_SEE_OTHER
        )
    return templates.TemplateResponse(
        request,
        "camera_delete_confirm.html",
        deps.base_context(request, db, user, camera=_camera_view(camera)),
    )


@router.post("/cameras/{camera_id:int}/delete")
def delete_camera_form(db: DbDep, user: OperatorUser, camera_id: int) -> Response:
    """Delete a camera from the plain (no-JavaScript) row form, then redirect.

    The JavaScript path sends ``DELETE /cameras/{id}`` from the same form and
    gets a fragment back. Without JavaScript the browser posts here (the CSRF
    token is the form field, checked by the middleware) and lands back on the
    cameras page with a banner, never on a raw error body: a camera still used
    by a project redirects with ``delete_error=in_use`` (and the project count
    when it is known), a camera already gone with ``delete_error=not_found``.
    """
    try:
        result = camera_service.delete_camera(db, Actor.of(user), camera_id)
    except NotFoundError:
        return RedirectResponse(
            "/cameras?delete_error=not_found", status_code=status.HTTP_303_SEE_OTHER
        )
    except ConflictError:
        in_use = db.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.camera_id == camera_id)
        )
        query = f"&n={int(in_use)}" if in_use else ""
        return RedirectResponse(
            f"/cameras?delete_error=in_use{query}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    db.commit()
    run_effects(result.effects, get_context())
    return RedirectResponse("/cameras?deleted=1", status_code=status.HTTP_303_SEE_OTHER)


# Shown when a multicast scan finds nothing: multicast stays on the server's own
# network segment, so on a segmented network an empty result is expected.
_MULTICAST_EMPTY_HINT = (
    '<div class="alert info">No cameras answered. Multicast discovery does not '
    "cross routed VLANs, so it only finds cameras on this server's own network "
    "segment. Scan a range instead: enter the cameras' subnet (for example "
    "192.168.1.0/24) and scan again.</div>"
)


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

    # Everything from here on is network I/O (the scan, then the enrichment), so
    # release the request's connection rather than hold it through the scan
    # (#66 C11). Enrichment carries no credentials at all (#67-S3, CAM-S4): a
    # scan reaches every host in a typed range, and no stored login belongs on
    # a stranger.
    await run_in_threadpool(_release_request_connection, db)
    if scan_range_input:
        found = await scan_range(scan_range_input)
    else:
        found = await discover_onvif()
        if not found:
            return HTMLResponse(_MULTICAST_EMPTY_HINT)

    supervisor = get_context().capture_supervisor
    if supervisor is not None:
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
            logger.warning("discovery URI enrichment timed out; returning basics")

    return HTMLResponse(_render_discovery_results(found))
