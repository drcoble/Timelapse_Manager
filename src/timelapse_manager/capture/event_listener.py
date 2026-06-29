"""The event-source factory that turns a project target into a live event stream.

The capture supervisor owns the listener *lifecycle* (launch, supervise, back
off, tear down) but stays agnostic about how a camera delivers events: it depends
only on a factory it can call per project to obtain an async iterator of opaque
event objects. This module supplies the real factory.

For a given project the factory loads the bound camera's configuration, builds
the matching adapter, and returns that adapter's event source -- ONVIF PullPoint
for an ONVIF camera, and for a VAPIX camera the WebSocket fast-path with a
transparent fallback to ONVIF PullPoint whenever the WebSocket path fails before
delivering a single event (an absent endpoint, a handshake/subscribe failure, or
a connection that drops before the first frame). Every transport yields the same
canonical
:class:`~timelapse_manager.cameras.base.CameraEvent`, so the matching layer the
supervisor drives is identical regardless of protocol.

The factory call itself is synchronous and does no I/O: it returns an async
iterator whose first iteration performs the subscribe/upgrade. That matches what
the supervisor expects (it calls the factory synchronously, and re-calls it on
every backoff retry to obtain a fresh subscription), and it lets the
VAPIX-WebSocket-to-ONVIF fallback be decided at subscribe time rather than at
construction time.

Outbound event traffic goes to the camera through the camera-allowlisted HTTP
client and the camera SSRF policy (every event call is guarded fail-closed inside
the adapters), never the outbound webhook path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from ..cameras import build_adapter
from ..cameras.base import CameraEvent

if TYPE_CHECKING:
    from .supervisor import CaptureTarget

logger = logging.getLogger(__name__)


class CameraConfigLoader(Protocol):
    """Loads the camera record an adapter is built from, by camera id.

    Returns an object exposing the attributes :func:`build_adapter` reads
    (``protocol``, ``address``, ``credentials``, ``snapshot_uri``,
    ``stream_uri``, ``default_resolution``), or ``None`` when the camera no
    longer exists. The supervisor already has such a loader
    (``_load_camera``); the factory borrows it so the listener and the capture
    path resolve a camera identically.
    """

    def __call__(self, camera_id: int) -> Any | None:
        """Return the camera config for ``camera_id``, or ``None``."""
        ...


class EventListenerFactory:
    """Builds a per-project event source for the capture supervisor.

    Construction captures the shared dependencies the supervisor holds: the
    camera-allowlisted HTTP client, the resolved ffmpeg binary, a camera-config
    loader, and a callable returning the resolved global default credentials.
    Calling the instance with a project target returns the right event source, or
    ``None`` when the project's camera cannot be resolved or its protocol exposes
    no events -- in which case the supervisor parks that listener idle.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        camera_loader: CameraConfigLoader,
        *,
        ffmpeg_binary: str = "ffmpeg",
        default_credentials_loader: Callable[[], tuple[str, str] | None] | None = None,
    ) -> None:
        self._http_client = http_client
        self._camera_loader = camera_loader
        self._ffmpeg_binary = ffmpeg_binary
        self._default_credentials_loader = default_credentials_loader

    def __call__(self, target: CaptureTarget) -> AsyncIterator[CameraEvent] | None:
        """Return an event source for ``target``.

        Synchronous and strictly I/O-free: it does no camera lookup and only
        constructs the async iterator. Resolving the camera config (a synchronous
        DB read) happens off the event loop on the iterator's *first* iteration,
        so calling the factory -- which the supervisor does on launch and on every
        backoff retry -- never blocks the loop.

        Always returns an iterator (never ``None``): when the project has no
        consumable source (camera deleted, or a protocol with no event mechanism)
        the iterator parks until cancelled, reproducing the supervisor's
        idle-park semantics without a cleanly-ending source that would trigger an
        immediate busy respawn.
        """
        return self._events(target)

    async def _events(self, target: CaptureTarget) -> AsyncIterator[CameraEvent]:
        """Yield events for one subscription attempt, with VAPIX->ONVIF fallback.

        Resolves the camera off-thread first (the loader does a synchronous DB
        read), then opens the right source. ONVIF PullPoint is the proven primary
        transport for both protocols; for a VAPIX camera the WebSocket is an
        optional fast-path tried first. If that fast-path fails for *any* reason
        before delivering a single event -- a handshake/upgrade error, a failed
        subscribe, an absent endpoint (``404``), or a connection that drops
        before the first frame -- the iterator transparently falls back to ONVIF
        PullPoint against the same camera, so a broken WebSocket path never loops
        forever without ever trying PullPoint.

        Once events have started flowing over the WebSocket, a later failure
        propagates instead of falling back, so the supervisor's backoff +
        re-subscribe re-establishes the same (working) WebSocket. ``CancelledError``
        is never caught here, so teardown propagates cleanly. When the project has
        no consumable source the iterator parks (waits forever, cancellable)
        rather than ending, so the supervisor does not immediately respawn it.
        """
        config = await asyncio.to_thread(self._camera_loader, target.camera_id)
        protocol = getattr(config, "protocol", None) if config is not None else None
        if config is None or protocol not in ("onvif", "vapix"):
            # No camera, or a protocol with no event mechanism: park idle until
            # the listener task is cancelled (teardown / reconcile). This mirrors
            # how the supervisor parks a project with no event source.
            if config is None:
                logger.debug(
                    "no camera config for event listener project=%s",
                    target.project_id,
                )
            await asyncio.Event().wait()
            return
            yield  # pragma: no cover - unreachable; makes this an async generator
        if protocol == "vapix":
            yielded_any = False
            try:
                logger.info(
                    "subscribing to vapix websocket events project=%s",
                    target.project_id,
                )
                async for event in self._vapix_ws_events(config):
                    if not yielded_any:
                        logger.info(
                            "vapix websocket events subscribed project=%s",
                            target.project_id,
                        )
                    yielded_any = True
                    yield event
            except Exception as exc:  # noqa: BLE001 - any pre-event WS failure -> fall back
                if yielded_any:
                    # The WebSocket was working and then dropped: let the
                    # supervisor back off and re-subscribe over the same fast-path
                    # rather than silently downgrading a healthy camera.
                    raise
                logger.warning(
                    "vapix websocket events failed before any event project=%s; "
                    "falling back to onvif pullpoint: %s",
                    target.project_id,
                    exc,
                )
            else:
                if yielded_any:
                    # WebSocket delivered events and then ended cleanly: report the
                    # clean end to the supervisor (it resets backoff and re-opens
                    # the WebSocket) instead of downgrading to PullPoint.
                    return
                logger.warning(
                    "vapix websocket produced no events project=%s; "
                    "falling back to onvif pullpoint",
                    target.project_id,
                )
        # ONVIF PullPoint: the primary transport, and the fallback for a VAPIX
        # camera whose websocket fast-path failed before delivering any event. A
        # VAPIX camera reaches this via the onvif adapter pointed at the same
        # address, which works whenever the camera's ONVIF user is provisioned.
        logger.info(
            "subscribing to onvif pullpoint events project=%s",
            target.project_id,
        )
        async for event in self._onvif_pullpoint_events(config):
            yield event

    async def _vapix_ws_events(self, config: Any) -> AsyncIterator[CameraEvent]:
        """Open and drain the VAPIX WebSocket source for a camera config."""
        adapter = build_adapter(
            config,
            self._http_client,
            ffmpeg_binary=self._ffmpeg_binary,
            default_credentials=self._default_credentials(),
        )
        try:
            async for event in adapter.open_event_source():
                yield event
        finally:
            await adapter.close()

    async def _onvif_pullpoint_events(self, config: Any) -> AsyncIterator[CameraEvent]:
        """Open and drain the ONVIF PullPoint source for a camera config.

        Builds an ONVIF-flavoured view of the camera (a VAPIX camera reuses its
        address/credentials over the ONVIF adapter for the fallback) so the same
        config object drives the PullPoint loop.
        """
        adapter = build_adapter(
            _as_onvif(config),
            self._http_client,
            ffmpeg_binary=self._ffmpeg_binary,
            default_credentials=self._default_credentials(),
        )
        try:
            async for event in adapter.open_event_source():
                yield event
        finally:
            await adapter.close()

    def _default_credentials(self) -> tuple[str, str] | None:
        if self._default_credentials_loader is None:
            return None
        return self._default_credentials_loader()


class _OnvifView:
    """A read-only camera view that forces the ONVIF protocol.

    Used for the VAPIX-camera fallback: the same address/credentials drive the
    ONVIF adapter without mutating the underlying config object. All attribute
    reads delegate to the wrapped config except ``protocol``, which is forced to
    ``"onvif"``.
    """

    __slots__ = ("_config",)

    def __init__(self, config: Any) -> None:
        self._config = config

    @property
    def protocol(self) -> str:
        return "onvif"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._config, name)


def _as_onvif(config: Any) -> Any:
    """Return a view of ``config`` whose protocol is ONVIF (no mutation)."""
    if getattr(config, "protocol", None) == "onvif":
        return config
    return _OnvifView(config)
