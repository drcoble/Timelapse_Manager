"""Factory mapping a camera's configured protocol to a concrete adapter.

The capture engine calls :func:`build_adapter` with a camera record and the
shared HTTP client; it never imports a specific adapter class. Building an
adapter does no I/O, so a caller can construct one cheaply and decide later
whether to use it.
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import CameraAdapter
from .http_jpeg import HttpJpegAdapter, credentials_from
from .onvif import OnvifAdapter
from .rtsp import RtspAdapter
from .vapix import VapixAdapter


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


def build_adapter(
    camera: Any,
    http_client: httpx.AsyncClient,
    *,
    ffmpeg_binary: str = "ffmpeg",
    default_credentials: tuple[str, str] | None = None,
    stream_id: str | None = None,
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
    :raises ValueError: when the protocol is missing or unrecognised.
    """
    protocol = getattr(camera, "protocol", None)
    credentials = effective_credentials(camera, default_credentials)

    if protocol == "http":
        snapshot_url = _require(camera, "snapshot_uri", protocol)
        return HttpJpegAdapter(http_client, snapshot_url, credentials)

    if protocol == "rtsp":
        stream_url = _require(camera, "stream_uri", protocol)
        return RtspAdapter(stream_url, credentials, ffmpeg_binary=ffmpeg_binary)

    if protocol == "vapix":
        return VapixAdapter(
            http_client,
            address=_require(camera, "address", protocol),
            credentials=credentials,
            snapshot_uri=getattr(camera, "snapshot_uri", None),
            default_resolution=getattr(camera, "default_resolution", None),
            stream_id=stream_id,
        )

    if protocol == "onvif":
        return OnvifAdapter(
            http_client,
            address=_require(camera, "address", protocol),
            credentials=credentials,
            snapshot_uri=getattr(camera, "snapshot_uri", None),
            stream_uri=getattr(camera, "stream_uri", None),
            ffmpeg_binary=ffmpeg_binary,
            stream_id=stream_id,
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
