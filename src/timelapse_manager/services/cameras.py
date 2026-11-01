"""Camera write use-cases -- the single implementation of the domain rules.

Each public function performs one write against a caller-supplied session and
returns a :class:`~timelapse_manager.services._base.Result` pairing the produced
value with the post-commit effects the caller must run once it has committed.
Creating a camera drives no background component (no project uses it yet). An
edit or a delete wakes the capture supervisor's reconcile pass
(``CAPTURE_RECONCILE``), which compares each camera's configuration fingerprint
with its cached capture session and reloads a changed one, so the next capture
uses the edit at once instead of after the periodic reconcile (#69 CAM-P1).

The web and API layers are thin adapters over these functions: they parse their
own transport input into a :class:`CameraSpec`, call the service, commit, then
run the (empty) effects. Validation of the persisted shape lives here, once; a
rule violation raises a transport-neutral
:class:`~timelapse_manager.services.errors.AppError` that the web/API error
handler maps to the same status the endpoints returned before services existed.
Services never commit: they mutate, ``flush``, and return.

Camera *network* work -- SSRF host-resolution of the address, credential
encryption, reachability probes, protocol detection, geolocation/hostname
discovery -- is deliberately **not** here. The adapter performs those and hands
the already-validated, already-encrypted values in on the :class:`CameraSpec`,
so the service touches only the database.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..cameras.tls import (
    fingerprints_match,
    format_fingerprint,
    pem_fingerprint,
    pem_summary,
)
from ..capture.camera_config import has_own_credentials
from ..db.models import Camera, Event, Project
from ..db.models.event import OPEN_ALERT_INDEX_LEVELS
from ..security.credential_binding import split_userinfo, target_host
from ..security.crypto import encrypt_credentials
from ..security.principal import ensure_sentinel_admin
from ._base import Actor, Effect, Result
from .errors import ConflictError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)


@dataclass
class CameraSpec:
    """Normalised, already-parsed inputs for a camera write.

    One spec type serves both create and update. Every value is expected
    pre-normalised by the caller: the ``address`` has already passed through the
    SSRF host-resolution chokepoint, ``credentials`` is the final credential
    document to store (already encrypted, or ``None`` to clear), and the
    coordinates/hostname are already parsed. The service persists them verbatim.

    ``fields_set`` names the fields the caller actually supplied so
    :func:`update_camera` applies a partial edit -- touching only those fields --
    exactly as the previous handler did with the form's presence markers. A field
    absent from ``fields_set`` leaves its stored value untouched; a field present
    with value ``None`` clears the column. :func:`create_camera` reads the
    create-relevant fields directly and ignores ``fields_set``.
    """

    name: str | None = None
    address: str | None = None
    protocol: str | None = None
    credentials: dict[str, Any] | None = None
    credentials_inherit_default: bool = True
    # Opt-in to HTTP Basic over plain http:// (#69 CAM-S1, OD-3); off by default.
    allow_basic_over_http: bool = False
    snapshot_uri: str | None = None
    stream_uri: str | None = None
    default_resolution: str | None = None
    geolocation_latitude: float | None = None
    geolocation_longitude: float | None = None
    geolocation_source: str | None = None
    device_hostname: str | None = None
    device_hostname_source: str | None = None
    fields_set: frozenset[str] = field(default_factory=frozenset)


# Fields an update copies straight through once present in ``fields_set``. ``name``
# is handled separately (it carries the uniqueness rule); everything else is a
# plain passthrough whose shape was validated by the transport/adapter.
_PASSTHROUGH_FIELDS = (
    "address",
    "protocol",
    "credentials",
    "credentials_inherit_default",
    "allow_basic_over_http",
    "snapshot_uri",
    "stream_uri",
    "default_resolution",
    "geolocation_latitude",
    "geolocation_longitude",
    "geolocation_source",
    "device_hostname",
    "device_hostname_source",
)


# The camera fields that hold a URL (or a bare host) and so can carry a
# ``user:pass@`` part: the address and the two media URIs.
_URI_FIELDS = ("address", "snapshot_uri", "stream_uri")


def _has_control_character(text: str) -> bool:
    """Whether ``text`` holds a control character (including CR and LF)."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def normalize_camera_uris(
    spec: CameraSpec,
    *,
    editing: bool = False,
    stored_credentials: dict[str, Any] | None = None,
) -> CameraSpec:
    """Keep ``user:pass@`` out of the stored address and URIs (#69 CAM-S2, CAM-S7).

    A password in a URI is shown by the camera list, repeated in logs and
    errors, and put on the wire by clients that honour it. At the write
    boundary:

    * a camera with **no credentials of its own** has the userinfo moved into
      the encrypted credential document (and stops inheriting the default, as a
      camera with its own login does);
    * a camera that **has** its own raises a 422 telling the operator to remove
      the ``user:pass@`` from the URI -- merging two logins would silently pick
      one;
    * a username with a control character, CR or LF is a 422, whatever its
      source: it would be written into an authentication header.

    Only the fields the write sets are examined (an edit's ``fields_set``;
    every field on a create). Pure: it returns a new spec and touches no state.

    :param editing: the spec is a partial edit, so ``fields_set`` names what it
        sets; ``False`` for a create.
    :param stored_credentials: the camera's stored credential document, for an
        edit that does not set ``credentials``.
    :raises ValidationError: for either rejection above.
    """
    sets_credentials = not editing or "credentials" in spec.fields_set
    own = spec.credentials if sets_credentials else stored_credentials
    if sets_credentials and has_own_credentials(own):
        assert own is not None
        if _has_control_character(str(own.get("username", ""))):
            raise ValidationError(
                "the username must not contain control characters",
                field="username",
            )

    cleaned: dict[str, str | None] = {}
    moved: tuple[str, str] | None = None
    for name in _URI_FIELDS:
        if editing and name not in spec.fields_set:
            continue
        value = getattr(spec, name)
        if not value:
            continue
        clean, userinfo = split_userinfo(value)
        if userinfo is None:
            continue
        if has_own_credentials(own):
            raise ValidationError(
                "remove the user:pass@ from the URI; set the login in the "
                "credential fields",
                field=name,
            )
        if moved is not None and moved != userinfo:
            raise ValidationError(
                "the address and URIs carry different logins; remove the "
                "user:pass@ and set the login in the credential fields",
                field=name,
            )
        # What is left must be a URL httpx reads the same way: no remaining
        # login (the split above can read a backslash differently), no
        # whitespace or control characters.
        if target_host(clean, allow_userinfo=False) is None:
            raise ValidationError("the address or URI is not a valid URL", field=name)
        moved = userinfo
        cleaned[name] = clean
    if not cleaned:
        return spec

    updates: dict[str, Any] = dict(cleaned)
    fields = set(spec.fields_set)
    if moved is not None:
        username, password = moved
        if not username or _has_control_character(username):
            raise ValidationError(
                "the username in the URI is empty or holds a control character",
                field="username",
            )
        updates["credentials"] = encrypt_credentials(
            {"username": username, "password": password}
        )
        updates["credentials_inherit_default"] = False
        fields.update(("credentials", "credentials_inherit_default"))
    return replace(spec, fields_set=frozenset(fields), **updates)


def normalize_stored_camera_uris(session: Session) -> int:
    """Move ``user:pass@`` out of already-stored camera URIs; return how many moved.

    The startup counterpart of :func:`normalize_camera_uris` for rows written
    before it existed (an idempotent pass, not an Alembic step: it needs the
    runtime key provider to encrypt). For each camera whose address or URI
    carries userinfo:

    * with no credentials of its own, the login moves into the encrypted
      credential document and the URI is stripped;
    * with its own, the row is left alone -- stripping would lose the only copy
      of the URI's login -- and one warning event names the camera (not repeated
      on later starts). The HTTP client strips the userinfo before every
      request, so it is not sent either way.

    A row that fails is logged by camera id and skipped, so one bad row cannot
    block the rest or startup; it is retried at the next start. Nothing logged
    or evented carries a value from the URI. The caller commits.
    """
    moved = 0
    for camera in session.execute(select(Camera).order_by(Camera.id)).scalars():
        columns = {name: getattr(camera, name) for name in _URI_FIELDS}
        if not any(v and split_userinfo(v)[1] is not None for v in columns.values()):
            continue
        try:
            with session.begin_nested():
                if _normalize_stored_camera(session, camera, columns):
                    moved += 1
        except Exception:  # noqa: BLE001 -- one bad row must not block startup
            logger.warning(
                "could not normalise the URIs of camera %s; will retry at the next "
                "start",
                camera.id,
                exc_info=False,
            )
    return moved


def _normalize_stored_camera(
    session: Session, camera: Camera, columns: dict[str, str | None]
) -> bool:
    """Normalise one stored camera; return whether its login was moved."""
    if has_own_credentials(camera.credentials):
        message = (
            f"camera {camera.id} has a user:pass@ in its address or URI and its "
            "own credentials: remove the user:pass@ from the URI"
        )
        already = session.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.scope == "camera",
                Event.scope_id == camera.id,
                Event.message == message,
            )
        )
        if not already:
            session.add(
                Event(
                    scope="camera",
                    scope_id=camera.id,
                    level="warning",
                    message=message,
                    timestamp=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
                )
            )
        return False
    login: tuple[str, str] | None = None
    cleaned: dict[str, str] = {}
    for name, value in columns.items():
        if not value:
            continue
        clean, userinfo = split_userinfo(value)
        if userinfo is None:
            continue
        if login is not None and login != userinfo:
            # Two different logins and nowhere to put both: leave the row alone.
            return False
        if target_host(clean, allow_userinfo=False) is None:
            return False  # not a URL both parsers read alike: leave it
        login = userinfo
        cleaned[name] = clean
    if login is None or not login[0] or _has_control_character(login[0]):
        return False
    camera.credentials = encrypt_credentials(
        {"username": login[0], "password": login[1]}
    )
    camera.credentials_inherit_default = False
    for name, clean in cleaned.items():
        setattr(camera, name, clean)
    session.flush()
    return True


def pin_target_url(
    protocol: str | None,
    address: str | None,
    snapshot_uri: str | None,
    stream_uri: str | None = None,
) -> str:
    """Return the ``https://`` URL whose certificate a camera would be pinned to.

    A pin only means something over TLS, and a pinned camera is refused plain
    HTTP at the client (see :class:`~timelapse_manager.cameras.tls.PinnedCameraClient`),
    so a camera that does not speak HTTPS cannot hold one. The URL is the
    address for VAPIX and ONVIF cameras and the snapshot URI for an HTTP
    camera. The scheme must be a literal lower-case ``https://``: the adapters
    read a scheme-less or differently-cased address as plain HTTP, and the
    check must not read it any other way.

    :raises ValidationError: when the camera cannot be pinned, with the reason.
    """
    if protocol == "rtsp":
        raise ValidationError(
            "certificate pinning applies to HTTP(S) cameras, not RTSP"
        )
    if protocol == "http":
        url = snapshot_uri
    elif protocol in ("vapix", "onvif"):
        url = address
    else:
        raise ValidationError("set the camera's protocol before pinning a certificate")
    if stream_uri:
        # An RTSP stream is not covered by a TLS pin, and an ONVIF camera would
        # fall back to it with the camera's credentials. A pinned camera does not
        # use one (the adapter also refuses the fallback), so none may be stored.
        raise ValidationError(
            "a pinned camera must not have a stream URI: RTSP is not covered by "
            "the certificate pin. Clear the stream URI (Advanced) or remove the "
            "certificate pin first",
            field="stream_uri",
        )
    if not url or not url.startswith("https://"):
        raise ValidationError(
            "a pinned camera must use an https:// address; use https:// (lower-case) "
            "or remove the certificate pin first"
        )
    if (
        protocol in ("vapix", "onvif")
        and snapshot_uri
        and snapshot_uri.lower().startswith("http://")
    ):
        raise ValidationError(
            "a pinned camera must not use an http:// snapshot URI; change it to "
            "https:// or clear it first",
            field="snapshot_uri",
        )
    return url


def pinned_fingerprint(camera: Camera) -> str | None:
    """Return the camera's pinned SHA-256 fingerprint (colon form), or ``None``."""
    pem = camera.tls_pinned_cert_pem
    if not pem:
        return None
    try:
        return format_fingerprint(pem_fingerprint(pem))
    except ValueError:
        return None


def pin_camera_certificate(
    session: Session,
    actor: Actor,
    camera_id: int,
    pem: str,
    *,
    expected_fingerprint: str,
) -> Result[Camera]:
    """Pin a camera to one certificate the operator has confirmed.

    ``pem`` must be exactly one valid certificate whose SHA-256 fingerprint
    equals ``expected_fingerprint`` (the one the operator saw and confirmed), so
    what is stored is what was approved. The camera must be one that can hold a
    pin (:func:`pin_target_url`). Audited with the fingerprint, never the PEM;
    the capture session reloads so the next connection is pinned. Flushes but
    does not commit.

    :raises NotFoundError: no such camera.
    :raises ValidationError: the certificate is malformed, outside its validity
        period, differs from the confirmed fingerprint, or the camera cannot be
        pinned.
    """
    camera = _get_camera_or_404(session, camera_id)
    pin_target_url(
        camera.protocol, camera.address, camera.snapshot_uri, camera.stream_uri
    )
    try:
        summary = pem_summary(pem)
    except ValueError:
        raise ValidationError("the certificate is not valid") from None
    actual = summary.fingerprint
    if not summary.valid_now:
        raise ValidationError(
            "the certificate is expired or not yet valid; the camera would "
            "refuse every connection. Renew its certificate first"
        )
    if not fingerprints_match(expected_fingerprint, actual):
        raise ValidationError(
            "the certificate is not the one that was confirmed; fetch it again",
            field="fingerprint",
        )
    camera.tls_pinned_cert_pem = pem.strip() + "\n"
    session.flush()
    _audit(
        session,
        actor,
        camera_id,
        f"camera {camera_id} pinned to certificate SHA-256 "
        f"{format_fingerprint(actual)}",
    )
    return Result(camera, (Effect.CAPTURE_RECONCILE,))


def unpin_camera_certificate(
    session: Session, actor: Actor, camera_id: int
) -> Result[Camera]:
    """Remove a camera's pin so it verifies against the CA bundle again.

    A no-op (no audit, no reload) for a camera that has none. Flushes but does
    not commit.

    :raises NotFoundError: no such camera.
    """
    camera = _get_camera_or_404(session, camera_id)
    if not camera.tls_pinned_cert_pem:
        return Result(camera, ())
    camera.tls_pinned_cert_pem = None
    session.flush()
    _audit(session, actor, camera_id, f"camera {camera_id} certificate pin removed")
    return Result(camera, (Effect.CAPTURE_RECONCILE,))


def _get_camera_or_404(session: Session, camera_id: int) -> Camera:
    """Return a camera row or raise :class:`NotFoundError` (mapped to ``404``)."""
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise NotFoundError(f"camera {camera_id} not found")
    return camera


def _require_unique_name(
    session: Session, name: str, *, exclude_id: int | None = None
) -> None:
    """Raise :class:`ConflictError` (``409``) if another camera uses ``name``.

    ``exclude_id`` skips the camera being edited so saving an unchanged name is
    not mistaken for a duplicate. A friendly pre-check; the UNIQUE constraint plus
    the caller's flush is the authoritative guard against a concurrent insert.
    """
    stmt = select(Camera.id).where(Camera.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Camera.id != exclude_id)
    if session.execute(stmt).scalar_one_or_none() is not None:
        raise ConflictError(f"a camera named {name!r} already exists", field="name")


def _audit(session: Session, actor: Actor, camera_id: int | None, message: str) -> None:
    """Append a camera-scoped audit event attributed to ``actor``.

    The actor is a foreign key to a user row, so the sentinel administrator is
    ensured to exist before the insert (matching every other audited write) so the
    foreign key never fails for a bearer-token caller.
    """
    ensure_sentinel_admin(session)
    session.add(
        Event(
            scope="camera",
            scope_id=camera_id,
            level="info",
            message=message,
            actor_user_id=actor.user_id,
            timestamp=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        )
    )


def create_camera(session: Session, actor: Actor, spec: CameraSpec) -> Result[Camera]:
    """Create a camera from already-normalised inputs.

    The name must be a non-empty string. The address, credentials and coordinates
    are persisted verbatim -- the adapter has already resolved the address through
    the SSRF chokepoint and encrypted the credential document. Inserts and flushes
    but does not commit; the caller commits.

    A duplicate name is left to surface as the database's ``IntegrityError`` at
    flush (an uncaught server error), preserving the pre-service behaviour: unlike
    the edit path, camera creation performed no friendly uniqueness pre-check.
    """
    name = (spec.name or "").strip()
    if not name:
        raise ValidationError("name is required", field="name")
    spec = normalize_camera_uris(spec)

    camera = Camera(
        name=name,
        address=spec.address,
        protocol=spec.protocol,
        credentials=spec.credentials,
        credentials_inherit_default=spec.credentials_inherit_default,
        allow_basic_over_http=spec.allow_basic_over_http,
        snapshot_uri=spec.snapshot_uri,
        stream_uri=spec.stream_uri,
        default_resolution=spec.default_resolution,
        geolocation_latitude=spec.geolocation_latitude,
        geolocation_longitude=spec.geolocation_longitude,
        geolocation_source=spec.geolocation_source,
        device_hostname=spec.device_hostname,
        device_hostname_source=spec.device_hostname_source,
    )
    session.add(camera)
    session.flush()

    _audit(session, actor, camera.id, f"camera {camera.id} created")
    return Result(camera, ())


def update_camera(
    session: Session, actor: Actor, camera_id: int, spec: CameraSpec
) -> Result[Camera]:
    """Apply a partial edit to a camera from already-normalised inputs.

    Only the fields named in ``spec.fields_set`` are changed. A new ``name`` must
    be non-empty and stay unique (``409``). Every other field is copied straight
    through -- the adapter resolved the address, chose the credential document and
    parsed the coordinates before building the spec. Validates the name before
    mutating, flushes, but does not commit. A missing camera is a ``404``.
    """
    camera = _get_camera_or_404(session, camera_id)
    spec = normalize_camera_uris(
        spec, editing=True, stored_credentials=camera.credentials
    )

    updates: dict[str, Any] = {}
    if "name" in spec.fields_set:
        name = (spec.name or "").strip()
        if not name:
            raise ValidationError("name is required", field="name")
        _require_unique_name(session, name, exclude_id=camera_id)
        updates["name"] = name
    for name_ in _PASSTHROUGH_FIELDS:
        if name_ in spec.fields_set:
            updates[name_] = getattr(spec, name_)

    if camera.tls_pinned_cert_pem:
        # An edit must not turn a pinned camera into one that cannot be pinned
        # (plain HTTP, RTSP): that would leave a pin that silently never applies.
        pin_target_url(
            updates.get("protocol", camera.protocol),
            updates.get("address", camera.address),
            updates.get("snapshot_uri", camera.snapshot_uri),
            updates.get("stream_uri", camera.stream_uri),
        )

    for key, value in updates.items():
        setattr(camera, key, value)
    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the uniqueness race against a concurrent rename/create.
        session.rollback()
        raise ConflictError(
            "a camera with that name already exists", field="name"
        ) from exc

    _audit(session, actor, camera_id, f"camera {camera_id} updated")
    # Reload the capture session the edit made stale (#69 CAM-P1).
    return Result(camera, (Effect.CAPTURE_RECONCILE,))


def delete_camera(session: Session, actor: Actor, camera_id: int) -> Result[None]:
    """Delete a camera by id.

    A missing camera is a ``404``. A camera that any project still references
    (in any lifecycle state) is a ``409``: ``project.camera_id`` is a ``RESTRICT``
    foreign key, so the delete could never commit, and without this check the
    integrity error surfaced as an unexplained ``500`` (#70 F-R1). The count
    gives the user an actionable message; the flush guard covers a project
    created between the count and the delete. Deletes and audits but does not
    commit; the caller commits.

    Any active camera-scoped ``camera_offline`` alert for the camera is
    auto-cleared in the same transaction (#132): with the camera gone no
    ``camera_recovered`` can ever resolve it.
    """
    camera = _get_camera_or_404(session, camera_id)
    in_use = session.scalar(
        select(func.count()).select_from(Project).where(Project.camera_id == camera_id)
    )
    if in_use:
        raise ConflictError(_camera_in_use_detail(in_use))
    session.delete(camera)
    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the race against a concurrent project create on this camera.
        session.rollback()
        raise ConflictError(_camera_in_use_detail(None)) from exc
    _clear_camera_offline_alerts(session, camera_id)
    _audit(session, actor, camera_id, f"camera {camera_id} deleted")
    # Drop the deleted camera's capture session (#69 CAM-P1).
    return Result(None, (Effect.CAPTURE_RECONCILE,))


def _clear_camera_offline_alerts(session: Session, camera_id: int) -> int:
    """Auto-clear the camera's active ``camera_offline`` alerts; return the count.

    Matches the alert the capture supervisor raises: camera-scoped, at an
    alerting level, not yet cleared, with ``reason`` ``camera_offline`` in its
    metadata. Recorded as an automatic clear (no user), like a recovery.
    """
    reason = func.json_extract(Event.event_metadata, "$.reason")
    result = session.execute(
        update(Event)
        .where(
            Event.scope == "camera",
            Event.scope_id == camera_id,
            Event.alert_cleared_at.is_(None),
            Event.level.in_(OPEN_ALERT_INDEX_LEVELS),
            reason == "camera_offline",
        )
        .values(
            alert_cleared_at=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
            alert_cleared_by=None,
            alert_clear_reason="auto",
        )
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _camera_in_use_detail(count: int | None) -> str:
    """The human ``detail`` for a refused delete of a camera still in use."""
    used_by = "a project" if count is None else f"{count} project(s)"
    return f"Camera is used by {used_by}; move or delete them first."
