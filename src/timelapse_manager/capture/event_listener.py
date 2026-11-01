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
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeVar

import httpx

from ..cameras import build_adapter
from ..cameras.base import CameraEvent, CameraMissingError, OtherCaptureError
from ..cameras.onvif import OnvifAdapter
from ..cameras.registry import UNSET, effective_credentials
from ..cameras.vapix import VapixAdapter
from ..logging import redact_text
from ..security.credential_binding import (
    bound_default_credentials,
    camera_credential_targets,
)
from ..security.crypto import CredentialDecryptError, InvalidToken
from .camera_config import CaptureCameraConfig, needs_default_credentials
from .executor import run_blocking

if TYPE_CHECKING:
    from concurrent.futures import Executor

    from ..cameras.registry import _Unset
    from .camera_sessions import CameraSession, CameraSessionRegistry
    from .supervisor import CaptureTarget

_P = ParamSpec("_P")
_T = TypeVar("_T")

logger = logging.getLogger(__name__)

#: The transports a listener reports to its supervisor (see ``ListenerState``).
TRANSPORT_VAPIX_WEBSOCKET = "vapix-websocket"
TRANSPORT_ONVIF_PULLPOINT = "onvif-pullpoint"


class ListenerReporter(Protocol):
    """Where a factory reports what a subscription is doing.

    Implemented by the capture supervisor, which keeps the report in the
    listener's state (and raises or resolves the camera's alert). Both calls
    are awaited from inside the listener's own task, so they run on the loop
    and end with it.
    """

    async def listener_subscribed(self, target: CaptureTarget, transport: str) -> None:
        """The camera accepted a subscription on ``transport``."""
        ...

    async def listener_downgraded(
        self,
        target: CaptureTarget,
        *,
        from_transport: str,
        to_transport: str,
        reason: str,
    ) -> None:
        """The listener fell back from ``from_transport`` to ``to_transport``."""
        ...


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
    camera HTTP client for event traffic (``supervisor.client_for("events")``,
    its own connection pool, so long-polls never take a capture's connection;
    #69 CAM-P2), the resolved ffmpeg binary, a camera-config
    loader, a callable returning the resolved global default credentials, the
    executor blocking reads run on and, in the running app, the supervisor's
    camera-session registry. Calling the instance with a project
    target returns the right event source, or ``None`` when the project's camera
    cannot be resolved or its protocol exposes no events -- in which case the
    supervisor parks that listener idle.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        camera_loader: CameraConfigLoader,
        *,
        ffmpeg_binary: str = "ffmpeg",
        default_credentials_loader: Callable[[], tuple[str, str] | None] | None = None,
        executor: Executor | None = None,
        camera_sessions: CameraSessionRegistry | None = None,
        pinned_client_for: Callable[[str], httpx.AsyncClient] | None = None,
    ) -> None:
        """Capture the shared dependencies; performs no I/O.

        :param http_client: the client event sources borrow; pass the
            supervisor's ``client_for("events")``.
        :param pinned_client_for: maps a pinned camera's certificate (PEM) to the
            events client that trusts that certificate alone; pass
            ``lambda pem: supervisor.client_for("events", pem)``. A camera with a
            pin (#69 CAM-S1) never uses ``http_client``. ``None`` leaves every
            camera on ``http_client`` (a factory built without a supervisor).
        :param executor: where the subscription's blocking reads run; ``None``
            (the default) is the event loop's default executor. Pass the same
            executor as the supervisor's so capture work shares one pool.
        :param camera_sessions: the supervisor's camera-session registry. When
            given, a subscription takes the camera's configuration and its
            once-decrypted credentials from the registry (the captures' cache),
            ``camera_loader`` and ``default_credentials_loader`` are unused, and
            a credential that cannot be decrypted fails the subscription with
            the named *local* error (#153). ``None`` keeps the per-subscription
            load.
        """
        self._http_client = http_client
        self._camera_loader = camera_loader
        self._ffmpeg_binary = ffmpeg_binary
        self._default_credentials_loader = default_credentials_loader
        self._executor = executor
        self._camera_sessions = camera_sessions
        self._pinned_client_for = pinned_client_for
        self._reporter: ListenerReporter | None = None

    def set_reporter(self, reporter: ListenerReporter | None) -> None:
        """Set where subscribe and downgrade reports go (the supervisor does this)."""
        self._reporter = reporter

    def _subscribed_hook(
        self, target: CaptureTarget | None, transport: str
    ) -> Callable[[], Awaitable[None]] | None:
        """Return the adapter's ``on_subscribed`` for ``target``, or ``None``.

        A report that fails must never break the subscription it reports on, so
        the hook swallows everything but cancellation.
        """
        reporter = self._reporter
        if reporter is None or target is None:
            return None

        async def hook() -> None:
            try:
                await reporter.listener_subscribed(target, transport)
            except Exception:  # noqa: BLE001 - reporting must not break the subscription
                logger.exception(
                    "failed to report event subscription project=%s",
                    target.project_id,
                )

        return hook

    async def _report_downgrade(self, target: CaptureTarget, reason: str) -> None:
        """Tell the reporter the WebSocket failed and PullPoint takes over."""
        if self._reporter is None:
            return
        try:
            await self._reporter.listener_downgraded(
                target,
                from_transport=TRANSPORT_VAPIX_WEBSOCKET,
                to_transport=TRANSPORT_ONVIF_PULLPOINT,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - reporting must not break the fallback
            logger.exception(
                "failed to report event transport downgrade project=%s",
                target.project_id,
            )

    def _client_for(self, config: Any) -> httpx.AsyncClient:
        """Return the events client for ``config``: its pinned one, if it has a pin."""
        pin = getattr(config, "tls_pinned_cert_pem", None)
        if pin and self._pinned_client_for is not None:
            return self._pinned_client_for(str(pin))
        return self._http_client

    async def _run_blocking(
        self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> _T:
        """Run a synchronous callable off the loop, on the factory's executor."""
        return await run_blocking(self._executor, fn, *args, **kwargs)

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

        Resolves the camera, and the default credentials when the camera
        inherits them, in one off-loop hop (both are synchronous DB reads), then
        opens the right source. ONVIF PullPoint is the proven primary
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

        With a camera-session registry the camera is resolved through it, before
        any transport is tried: a credential that cannot be decrypted raises the
        registry's named local error to the supervisor (which backs off), and is
        never mistaken for a WebSocket failure that falls back to PullPoint.
        """
        credentials: tuple[str, str] | None | _Unset = UNSET
        session: CameraSession | None = None
        if self._camera_sessions is not None:
            try:
                session = await self._camera_sessions.get(target.camera_id)
            except CameraMissingError:
                config, default_credentials = None, None
            else:
                config = session.config
                default_credentials = session.config.default_credentials
                credentials = session.credentials
        else:
            # Credentials are decrypted in the same off-loop hop (#162), so
            # building the adapter below never decrypts on the loop and a stored
            # secret that cannot be read fails with the named local error.
            config, default_credentials, credentials = await self._run_blocking(
                self._load_resolved_subscription, target.camera_id
            )
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
                async for event in self._vapix_ws_events(
                    config,
                    default_credentials,
                    credentials,
                    target=target,
                    session=session,
                ):
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
                    redact_text(str(exc)),
                )
                await self._report_downgrade(target, str(exc) or type(exc).__name__)
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
                await self._report_downgrade(target, "the stream produced no events")
        # ONVIF PullPoint: the primary transport, and the fallback for a VAPIX
        # camera whose websocket fast-path failed before delivering any event. A
        # VAPIX camera reaches this via the onvif adapter pointed at the same
        # address, which works whenever the camera's ONVIF user is provisioned.
        logger.info(
            "subscribing to onvif pullpoint events project=%s",
            target.project_id,
        )
        async for event in self._onvif_pullpoint_events(
            config, default_credentials, credentials, target=target, session=session
        ):
            yield event

    def _load_subscription(
        self, camera_id: int
    ) -> tuple[Any | None, tuple[str, str] | None]:
        """Resolve the camera config and its default credentials, synchronously.

        Runs in a worker thread (see :meth:`_events`), so both database reads
        stay off the event loop and happen once per subscription attempt; the
        VAPIX-to-ONVIF fallback reuses the same resolved tuple. The global
        default is read only when the camera needs it (it inherits the default
        and has no credentials of its own). A merged
        :class:`~timelapse_manager.capture.camera_config.CaptureCameraConfig`
        already carries the resolved default, so no second read is made for it.
        Returns plain values only.
        """
        config = self._camera_loader(camera_id)
        if config is None or not needs_default_credentials(config):
            return config, None
        if isinstance(config, CaptureCameraConfig):
            return config, config.default_credentials
        if self._default_credentials_loader is None:
            return config, None
        # A config that does not carry its own (already bound) default: bind
        # the loader's to the camera's hosts here (#67-S3, OD-1).
        return config, bound_default_credentials(
            camera_credential_targets(config), self._default_credentials_loader()
        )

    def _load_resolved_subscription(
        self, camera_id: int
    ) -> tuple[Any | None, tuple[str, str] | None, tuple[str, str] | None | _Unset]:
        """:meth:`_load_subscription` plus the camera's decrypted credentials.

        For a factory built without a session registry. Runs in a worker thread,
        so the decrypt is off the loop; a stored secret the current key cannot
        read raises the registry's named *local* error, never a bare
        ``InvalidToken`` (#162). ``UNSET`` stands in for a camera that does not
        exist (the caller parks before using it).
        """
        try:
            config, default_credentials = self._load_subscription(camera_id)
            if config is None:
                return None, None, UNSET
            return (
                config,
                default_credentials,
                effective_credentials(config, default_credentials),
            )
        except (CredentialDecryptError, InvalidToken) as exc:
            raise OtherCaptureError(str(CredentialDecryptError()), local=True) from exc

    def _session_caches(self, session: CameraSession | None) -> dict[str, Any]:
        """Return the ``build_adapter`` keywords that share a session's caches.

        The listener's adapter shares the session's resolved-value caches and
        HTTP auth state with the camera's captures: one Digest negotiation, and
        the ONVIF clock offset measured once (#69 CAM-P1, CAM-R9).
        """
        if session is None:
            return {}
        return {
            "uri_cache": session.uri_cache,
            "knob_cache": session.knob_cache,
            "auth_state": session.auth_state,
        }

    async def _vapix_ws_events(
        self,
        config: Any,
        default_credentials: tuple[str, str] | None,
        credentials: tuple[str, str] | None | _Unset = UNSET,
        *,
        target: CaptureTarget | None = None,
        session: CameraSession | None = None,
    ) -> AsyncIterator[CameraEvent]:
        """Open and drain the VAPIX WebSocket source for a camera config."""
        adapter = build_adapter(
            config,
            self._client_for(config),
            ffmpeg_binary=self._ffmpeg_binary,
            default_credentials=default_credentials,
            credentials=credentials,
            **self._session_caches(session),
        )
        if isinstance(adapter, VapixAdapter):
            adapter.on_subscribed = self._subscribed_hook(
                target, TRANSPORT_VAPIX_WEBSOCKET
            )
        try:
            async for event in adapter.open_event_source():
                yield event
        finally:
            await adapter.close()

    async def _onvif_pullpoint_events(
        self,
        config: Any,
        default_credentials: tuple[str, str] | None,
        credentials: tuple[str, str] | None | _Unset = UNSET,
        *,
        target: CaptureTarget | None = None,
        session: CameraSession | None = None,
    ) -> AsyncIterator[CameraEvent]:
        """Open and drain the ONVIF PullPoint source for a camera config.

        Builds an ONVIF-flavoured view of the camera (a VAPIX camera reuses its
        address/credentials over the ONVIF adapter for the fallback) so the same
        config object drives the PullPoint loop.
        """
        adapter = build_adapter(
            _as_onvif(config),
            self._client_for(config),
            ffmpeg_binary=self._ffmpeg_binary,
            default_credentials=default_credentials,
            credentials=credentials,
            **self._session_caches(session),
        )
        if isinstance(adapter, OnvifAdapter):
            adapter.on_subscribed = self._subscribed_hook(
                target, TRANSPORT_ONVIF_PULLPOINT
            )
        try:
            async for event in adapter.open_event_source():
                yield event
        finally:
            await adapter.close()


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
