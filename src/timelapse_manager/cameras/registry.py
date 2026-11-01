"""Factory mapping a camera's configured protocol to a concrete adapter.

The capture engine calls :func:`build_adapter` with a camera record and the
shared HTTP client; it never imports a specific adapter class. Building an
adapter does no I/O, so a caller can construct one cheaply and decide later
whether to use it.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from .base import AdapterCache, CameraAdapter
from .clients import contained_client
from .http_auth import BasicPolicy, CameraAuthState
from .http_jpeg import HttpJpegAdapter, credentials_from
from .onvif import OnvifAdapter
from .rtsp import RtspAdapter
from .vapix import VapixAdapter


class _Unset:
    """The type of :data:`UNSET`."""

    def __repr__(self) -> str:
        return "UNSET"


#: ``build_adapter(credentials=UNSET)``: resolve the credentials from the camera
#: record. ``None`` is a real value there (a camera that uses no credentials).
UNSET: Final = _Unset()


def effective_credentials(
    camera: Any,
    default_credentials: tuple[str, str] | None,
) -> tuple[str, str] | None:
    """Resolve the credentials an adapter should use for ``camera``.

    A camera's own credentials always win. A camera with none of its own falls
    back to ``default_credentials`` only when it is configured to inherit the
    global default (the ``credentials_inherit_default`` flag). A camera with no
    own credentials and no inherit (or no default available) stays open -- this
    preserves a deliberately credential-free camera.

    Both inputs are already-resolved ``(username, password)`` tuples (the
    per-camera pair from :func:`credentials_from`, the default pair from the
    decrypt-at-use settings resolver), so this is a pure decision over plain
    values with no I/O and no database access.
    """
    own = credentials_from(camera)
    if own is not None:
        return own
    if getattr(camera, "credentials_inherit_default", False) and default_credentials:
        return default_credentials
    return None


def uses_default_login(
    camera: Any,
    resolved: tuple[str, str] | None,
    default_credentials: tuple[str, str] | None,
) -> bool:
    """Return whether ``resolved`` is the global default, not the camera's own.

    True when the camera has no credentials of its own (no username in its
    credential document) and ``resolved`` is the default the loader bound to it.
    Such a login is contained to the allowed private subnets at connect time
    (#268); a camera's own credentials are never affected.
    """
    if resolved is None or default_credentials is None:
        return False
    own = getattr(camera, "credentials", None)
    if isinstance(own, dict) and own.get("username"):
        return False
    return tuple(resolved) == tuple(default_credentials)


def auth_state_for(camera: Any) -> CameraAuthState:
    """Return a fresh auth negotiation state with ``camera``'s Basic policy.

    A saved camera (a ``Camera`` row or a capture config: anything with an
    ``allow_basic_over_http`` attribute) gets its own opt-in flag; anything
    else is an ad-hoc probe, which never sends Basic over plain HTTP (owner
    decision OD-3).
    """
    if hasattr(camera, "allow_basic_over_http"):
        policy = BasicPolicy.for_camera(bool(camera.allow_basic_over_http))
    else:
        policy = BasicPolicy.for_probe()
    return CameraAuthState(policy)


def build_adapter(
    camera: Any,
    http_client: httpx.AsyncClient,
    *,
    ffmpeg_binary: str = "ffmpeg",
    default_credentials: tuple[str, str] | None = None,
    stream_id: str | None = None,
    credentials: tuple[str, str] | None | _Unset = UNSET,
    uri_cache: AdapterCache | None = None,
    knob_cache: AdapterCache | None = None,
    auth_state: CameraAuthState | None = None,
) -> CameraAdapter:
    """Return the adapter for ``camera.protocol``.

    :param camera: a camera record exposing at least ``protocol``, ``address``,
        ``credentials``, ``snapshot_uri``, ``stream_uri`` and
        ``default_resolution`` (the ORM ``Camera`` model satisfies this).
    :param http_client: the shared async HTTP client adapters borrow.
    :param ffmpeg_binary: the ffmpeg executable the RTSP adapter invokes for a
        single-frame grab. Defaults to ``ffmpeg`` on ``PATH``; callers in a
        running process pass the binary resolved for the environment so capture
        uses the same ffmpeg as encode (bundled when frozen).
    :param default_credentials: the resolved global fallback ``(username,
        password)``, or ``None``. Used only when the camera has no credentials of
        its own and is configured to inherit the default; see
        :func:`effective_credentials`. Resolve this once at load time and pass it
        in so the validate path and the capture path behave identically.
    :param stream_id: which of the camera's named streams/profiles to capture
        from, or ``None`` for the camera default. Honoured by the multi-stream
        adapters (VAPIX, ONVIF); the single-stream rtsp/http adapters have one
        implicit stream, so it is a no-op for them.
    :param credentials: the ``(username, password)`` the adapter uses, already
        resolved and decrypted (the capture path's camera session does this
        once per camera), or ``None`` for none. When given,
        :func:`effective_credentials` is skipped and ``default_credentials`` is
        ignored; by default they are resolved from ``camera`` as before.
    :param uri_cache: a per-camera store the ONVIF adapter keeps its resolved
        profile token and media URIs in across captures; ignored by the others.
    :param knob_cache: a per-camera store the VAPIX adapter keeps resolved
        stream-profile snapshot knobs in; ignored by the others.
    :param auth_state: the camera session's HTTP auth negotiation state, so the
        HTTP-based adapters reuse the scheme and Digest nonce across captures
        (#69 CAM-P1). ``None`` builds a fresh one from the camera's Basic policy
        (:func:`auth_state_for`); ignored by the RTSP adapter.
    :raises ValueError: when the protocol is missing or unrecognised.
    """
    protocol = getattr(camera, "protocol", None)
    resolved = (
        effective_credentials(camera, default_credentials)
        if isinstance(credentials, _Unset)
        else credentials
    )
    state = auth_state if auth_state is not None else auth_state_for(camera)
    # The global default login never leaves the allowed private subnets, even
    # if DNS answers differently at connect time than at the binding check (#268).
    contain_default = uses_default_login(camera, resolved, default_credentials)
    if contain_default:
        http_client = contained_client(http_client)

    if protocol == "http":
        snapshot_url = _require(camera, "snapshot_uri", protocol)
        return HttpJpegAdapter(http_client, snapshot_url, resolved, auth_state=state)

    if protocol == "rtsp":
        stream_url = _require(camera, "stream_uri", protocol)
        return RtspAdapter(
            stream_url,
            resolved,
            ffmpeg_binary=ffmpeg_binary,
            contain_default=contain_default,
        )

    if protocol == "vapix":
        return VapixAdapter(
            http_client,
            address=_require(camera, "address", protocol),
            credentials=resolved,
            snapshot_uri=getattr(camera, "snapshot_uri", None),
            default_resolution=getattr(camera, "default_resolution", None),
            stream_id=stream_id,
            knob_cache=knob_cache,
            auth_state=state,
            contain_default=contain_default,
        )

    if protocol == "onvif":
        return OnvifAdapter(
            http_client,
            address=_require(camera, "address", protocol),
            credentials=resolved,
            snapshot_uri=getattr(camera, "snapshot_uri", None),
            stream_uri=getattr(camera, "stream_uri", None),
            ffmpeg_binary=ffmpeg_binary,
            stream_id=stream_id,
            uri_cache=uri_cache,
            auth_state=state,
            # A pinned camera never falls back to RTSP: the pin does not cover it.
            allow_rtsp_fallback=not getattr(camera, "tls_pinned_cert_pem", None),
            contain_default=contain_default,
        )

    if protocol is None:
        raise ValueError("camera has no protocol configured")
    raise ValueError(f"unsupported camera protocol: {protocol!r}")


def _require(camera: Any, field: str, protocol: str) -> str:
    """Return a required string field or raise a clear ValueError."""
    value = getattr(camera, field, None)
    if not value:
        raise ValueError(f"{protocol} camera requires '{field}' to be set")
    return str(value)
