"""The camera configuration a capture builds its adapter from, loaded in one hop.

Every capture producer (the interval loop, the one-shot path behind exact-time
anchors and event triggers, and the event listener) needs the same thing before
it can talk to a camera: the camera's adapter-relevant fields and, when the
camera inherits them, the global default credentials. :func:`load_capture_config`
reads both in **one** session, so a capture costs one thread hop and one
database session instead of two (#66 C12), and returns a frozen snapshot of
plain values that is safe to hold on the event loop.

The global default is resolved **only** when the camera needs it: it inherits
the default and has no credentials of its own (#69 CAM-R8). A camera with its
own credentials never reads (or decrypts) the default row. The outcome is the
same as resolving the default unconditionally and letting
:func:`~timelapse_manager.cameras.registry.effective_credentials` discard it,
which a unit test pins for every combination.

The default is also **bound to the camera's hosts** (owner decision OD-1,
:func:`~timelapse_manager.security.credential_binding.bound_default_credentials`):
a camera whose address, snapshot or stream URI resolves outside
``ssrf.allowed_private_subnets`` gets no default, so editing an inheriting
camera onto a public host never sends the fleet login there.

A stored secret that cannot be decrypted with the current key raises
:class:`~timelapse_manager.security.crypto.CredentialDecryptError`, which names
the problem, instead of Fernet's empty ``InvalidToken`` (#69 CAM-R8).

:func:`config_version` fingerprints everything a capture is built from, so the
per-camera session cache (:mod:`.camera_sessions`) can tell a changed camera
from an unchanged one. It hashes the stored values, not ``updated_at``: SQLite's
``CURRENT_TIMESTAMP`` has one-second resolution, so two edits in the same second
would look identical.

Synchronous by design; call it through the capture executor seam
(``CaptureSupervisor._run_blocking``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ..db.models import Camera
from ..db.models.camera_default_credentials import CameraDefaultCredentials
from ..db.session import session_scope
from ..security.camera_defaults_service import resolve_default_credentials
from ..security.credential_binding import (
    bound_default_credentials,
    camera_credential_targets,
)
from ..security.crypto import (
    CredentialDecryptError,
    InvalidToken,
    decrypt_credentials_strict,
)

# The singleton default-credentials row (its primary key is constrained to 1).
_DEFAULTS_ROW_ID = 1


@dataclass(frozen=True)
class CaptureCameraConfig:
    """The subset of a camera record an adapter needs, as plain values.

    Exposes the attributes :func:`~timelapse_manager.cameras.build_adapter`
    reads, so it can be passed straight to it. Holding a detached snapshot keeps
    the ORM (and its session) out of the async capture path.

    :param updated_at: the camera row's ``updated_at``, so a later cache can tell
        a changed camera from an unchanged one.
    :param default_credentials: the resolved global fallback ``(username,
        password)`` when this camera needs it (see
        :func:`needs_default_credentials`), else ``None``. Pass it to
        ``build_adapter(default_credentials=...)``.
    :param config_version: the :func:`config_version` fingerprint of the rows
        this snapshot was read from, or ``None`` when unknown.
    :param allow_basic_over_http: the camera's opt-in to HTTP Basic over plain
        ``http://`` (owner decision OD-3; off by default). Read by
        :func:`~timelapse_manager.cameras.registry.auth_state_for`.
    :param tls_pinned_cert_pem: the PEM of the certificate this camera is pinned
        to (#69 CAM-S1), or ``None``. Public data. Selects the pinned client for
        the camera's captures and events.

    :param default_withheld: True when the camera needs the global default and
        one is saved, but binding held it back (a host outside the allowed
        private subnets, or a name that did not resolve just then). The session
        registry retries that decision on every use instead of keeping the
        verdict until the next edit or restart (#269).

    ``credentials`` and ``default_credentials`` are excluded from the ``repr`` so
    a logged config never carries a secret (the default password is already
    decrypted here).
    """

    protocol: str | None
    address: str | None
    credentials: dict[str, object] | None = field(repr=False)
    credentials_inherit_default: bool
    snapshot_uri: str | None
    stream_uri: str | None
    default_resolution: str | None
    updated_at: datetime | None = None
    default_credentials: tuple[str, str] | None = field(default=None, repr=False)
    config_version: str | None = None
    allow_basic_over_http: bool = False
    tls_pinned_cert_pem: str | None = None
    default_withheld: bool = False


def has_own_credentials(credentials: object) -> bool:
    """Return whether a stored credential document names a username.

    Mirrors :func:`~timelapse_manager.cameras.http_jpeg.credentials_from`, which
    treats a document without a usable username as "no credentials". Only
    password, secret and token fields are encrypted at rest, so the username can
    be checked without decrypting anything.
    """
    return isinstance(credentials, dict) and bool(credentials.get("username"))


def needs_default_credentials(camera: Any) -> bool:
    """Return whether ``camera`` would use the global default credentials.

    True only when the camera inherits the default and has no credentials of its
    own -- the one case in which
    :func:`~timelapse_manager.cameras.registry.effective_credentials` returns the
    default. Accepts any object with ``credentials`` and
    ``credentials_inherit_default`` attributes.
    """
    if not getattr(camera, "credentials_inherit_default", False):
        return False
    return not has_own_credentials(getattr(camera, "credentials", None))


def config_version(camera: Any, defaults: Any | None) -> str:
    """Fingerprint the stored values a capture on ``camera`` is built from.

    Covers the camera's adapter fields (address, protocol, URIs, resolution,
    the stored credential document including its ciphertext, the inherit flag,
    the Basic-over-HTTP opt-in) and, only when the camera needs it, the
    default-credentials row (enabled,
    username, password ciphertext). Fernet ciphertext changes on every write,
    so re-saving a password changes the fingerprint even when the password is
    the same: a spurious reload, never a missed one.

    :param camera: a ``Camera`` row (or any object with its attributes).
    :param defaults: the ``CameraDefaultCredentials`` row, or ``None``.
    :returns: a short hex digest; equal inputs give equal digests.
    """
    default_part: object = None
    if needs_default_credentials(camera):
        default_part = (
            None
            if defaults is None
            else [
                bool(getattr(defaults, "enabled", False)),
                getattr(defaults, "username", None),
                getattr(defaults, "password", None),
            ]
        )
    material = [
        getattr(camera, "protocol", None),
        getattr(camera, "address", None),
        getattr(camera, "credentials", None),
        bool(getattr(camera, "credentials_inherit_default", False)),
        getattr(camera, "snapshot_uri", None),
        getattr(camera, "stream_uri", None),
        getattr(camera, "default_resolution", None),
        default_part,
    ]
    # Appended only when set, so the fingerprint of every camera without the
    # opt-in (all of them on upgrade) is unchanged and no session reloads.
    if getattr(camera, "allow_basic_over_http", False):
        material.append({"allow_basic_over_http": True})
    # Likewise only when pinned: setting, changing or clearing the pin changes
    # the fingerprint, so the camera's session (and so its HTTP client) reloads.
    pin = getattr(camera, "tls_pinned_cert_pem", None)
    if pin:
        material.append({"tls_pinned_cert_pem": pin})
    encoded = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def load_defaults_row(session: Session) -> CameraDefaultCredentials | None:
    """Return the default-credentials row (still encrypted), or ``None``."""
    return session.get(CameraDefaultCredentials, _DEFAULTS_ROW_ID)


def resolve_capture_credentials(config: Any) -> tuple[str, str] | None:
    """Resolve the ``(username, password)`` a capture uses, decrypting strictly.

    The same decision as
    :func:`~timelapse_manager.cameras.registry.effective_credentials`: the
    camera's own credentials win; without them an inheriting camera gets
    ``config.default_credentials`` (already decrypted by the loader); otherwise
    ``None``. A missing password is an empty string, as the adapters expect.

    :raises CredentialDecryptError: when the camera's own secret cannot be
        decrypted with the current key.
    """
    own = getattr(config, "credentials", None)
    if has_own_credentials(own):
        decrypted = decrypt_credentials_strict(own) or {}
        username = decrypted.get("username")
        if username:
            return (str(username), str(decrypted.get("password") or ""))
    if getattr(config, "credentials_inherit_default", False):
        default = getattr(config, "default_credentials", None)
        if default:
            return (str(default[0]), str(default[1]))
    return None


def load_capture_config(
    session_factory: sessionmaker[Session], camera_id: int
) -> CaptureCameraConfig | None:
    """Read a camera and, if it needs them, the default credentials, in one session.

    Returns ``None`` when the camera no longer exists. Synchronous; run it off
    the event loop.

    :raises CredentialDecryptError: when the camera needs the default
        credentials and their stored password cannot be decrypted. A camera
        with its own credentials never reads the default, so a corrupted
        default cannot stop it.
    """
    with session_scope(session_factory) as session:
        camera = session.get(Camera, camera_id)
        if camera is None:
            return None
        needs_default = needs_default_credentials(camera)
        default_credentials: tuple[str, str] | None = None
        if needs_default:
            try:
                default_credentials = resolve_default_credentials(session)
            except InvalidToken:
                raise CredentialDecryptError from None
        defaults_row = load_defaults_row(session) if needs_default else None
        config = CaptureCameraConfig(
            protocol=camera.protocol,
            address=camera.address,
            credentials=camera.credentials,
            credentials_inherit_default=bool(camera.credentials_inherit_default),
            snapshot_uri=camera.snapshot_uri,
            stream_uri=camera.stream_uri,
            default_resolution=camera.default_resolution,
            updated_at=camera.updated_at,
            default_credentials=default_credentials,
            config_version=config_version(camera, defaults_row),
            allow_basic_over_http=bool(camera.allow_basic_over_http),
            tls_pinned_cert_pem=camera.tls_pinned_cert_pem or None,
        )
    # Bind the default to the camera's own hosts (OD-1) once the session, and
    # its connection, are released: the check resolves names. The fingerprint
    # above is deliberately that of the stored rows, which is what each
    # reconcile pass compares it with.
    if config.default_credentials is not None:
        bound = bound_default_credentials(
            camera_credential_targets(config), config.default_credentials
        )
        config = replace(
            config, default_credentials=bound, default_withheld=bound is None
        )
    return config
