"""The camera HTTP clients: one pool each for capture, events and probes.

Before #69 CAM-P2 every camera request shared one :class:`httpx.AsyncClient`, so
a burst of operator probes (Detect, discovery enrichment) or a set of event
long-polls could fill the pool and make scheduled captures wait for a
connection. :class:`CameraHttpClients` gives each purpose its own pool:

* ``capture`` -- scheduled, anchor and manual captures (the snapshot fetch and
  the metadata read after it);
* ``events`` -- event listeners (VAPIX SOAP, ONVIF PullPoint long-polls);
* ``probe`` -- operator-driven reads: Detect, query, discovery enrichment,
  validation, topic/stream/preset enumeration and geolocation reads.

All three verify TLS through one context from :mod:`.tls`, which keeps
honouring ``SSL_CERT_FILE`` when no camera bundle is configured.

A camera pinned to one certificate (#69 CAM-S1; ``camera.tls_pinned_cert_pem``)
gets its own :class:`~.tls.PinnedCameraClient` per purpose instead, built lazily
by :meth:`CameraHttpClients.for_purpose` with the same limits and timeouts and
cached by the pin's SHA-256, so the camera keeps one pool however often its
configuration reloads. The pin is that camera's only trust anchor: it takes
precedence over the shared context, and an unpinned camera is untouched. A pinned
client is https-only, so a plain ``http://`` URI an ONVIF device reports for a
pinned camera is refused (fail closed, by design: the pin cannot cover it). The
supervisor's reconcile pass evicts the client of a pin no camera has any more
(:meth:`CameraHttpClients.retain_pinned`), so a re-pin or an unpin takes effect
without a restart; Detect and Query run for an existing camera use its pin too.

Every one of these clients, pinned or not, connects only to an SSRF-validated
address (#69 #222; :mod:`timelapse_manager.security.pinned_http`). The adapters
check a camera's host before each request, and the transport dials the
addresses that check validated rather than resolving the name a second time, so
a DNS answer that changes between the check (credential binding, the ONVIF host
check) and the connection cannot send a camera's credentials elsewhere. The name
is still resolved afresh on each new connection once the short-lived validated
answer expires, so a camera whose DNS record legitimately moves is followed. TLS
server name and certificate host-name verification stay on the camera's name,
never on the dialled IP.

A wait for a free connection that runs out (:class:`httpx.PoolTimeout`) is a
server-side condition, not a camera failure. The adapters report it as a
*local* :class:`~.base.OtherCaptureError` (see :func:`pool_exhausted_error`),
which camera health does not count (#69 CAM-R6). For that report to win over
the capture budget, the pool wait is bounded by half the capture timeout (see
:func:`request_timeout`).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

import httpx

from ..security.pinned_http import ContainedDefaultTransport, pinned_async_transport
from .base import OtherCaptureError
from .host_resolution import camera_allowed_subnets
from .tls import (
    PinnedCameraClient,
    camera_ssl_context,
    pem_fingerprint,
    pinned_ssl_context,
)

if TYPE_CHECKING:
    from ..config.settings import CaptureSettings

logger = logging.getLogger(__name__)

__all__ = [
    "CameraHttpClients",
    "ClientPurpose",
    "PinUnusableError",
    "allow_uncontained_double",
    "contained_client",
    "pool_exhausted_error",
    "request_timeout",
]

#: What a camera request is for; selects the client (and pool) it uses.
ClientPurpose = Literal["capture", "events", "probe"]

# An evicted pinned client is closed this many multiples of the request timeout
# after it leaves the cache, so a request that already holds it finishes first.
_RETIRE_GRACE_TIMEOUTS = 2.0

# A pool wait may use at most this fraction of the capture timeout. The capture
# budget equals the timeout, so a wait bounded below it surfaces as a local
# pool error instead of an (apparently camera-caused) capture timeout.
_POOL_WAIT_FRACTION = 0.5


class PinUnusableError(OtherCaptureError, ValueError):
    """A camera's stored pin is not a usable certificate (local; never unpinned).

    Also a :class:`ValueError`, so the web and API sites that already turn a bad
    camera configuration into a message (they catch it around ``build_adapter``)
    handle it instead of failing with a 500.
    """


class CameraHttpClients:
    """Three bounded async HTTP clients for camera traffic, one per purpose.

    Built from :class:`~timelapse_manager.config.settings.CaptureSettings`
    alone. Construction opens no connection: httpx connects lazily on the first
    request. It does load the TLS trust store, as a default
    :class:`httpx.AsyncClient` does.

    Redirects stay off (httpx's default), so a ``30x`` cannot bounce a camera
    request to a host the SSRF guard would deny. Body-size ceilings are applied
    per read by the adapters, not here.
    """

    def __init__(self, capture: CaptureSettings) -> None:
        """Create the three clients; performs no network I/O.

        :param capture: the ``capture`` settings section (pool limits, the
            request timeout and the optional camera CA bundle).
        :raises OSError: when ``capture.tls_ca_bundle`` cannot be read.
        :raises ssl.SSLError: when the bundle holds no usable certificate.
        """
        context = camera_ssl_context(capture.tls_ca_bundle)
        timeout = httpx.Timeout(
            capture.timeout_seconds,
            pool=capture.timeout_seconds * _POOL_WAIT_FRACTION,
        )
        keepalive = capture.http_keepalive_connections
        expiry = capture.http_keepalive_expiry_seconds
        self._timeout = timeout
        self._limits: dict[str, httpx.Limits] = {
            "capture": httpx.Limits(
                max_connections=capture.http_max_connections,
                max_keepalive_connections=keepalive,
                keepalive_expiry=expiry,
            ),
            "events": httpx.Limits(
                max_connections=capture.event_http_max_connections,
                max_keepalive_connections=min(
                    keepalive, capture.event_http_max_connections
                ),
                keepalive_expiry=expiry,
            ),
            "probe": httpx.Limits(
                max_connections=capture.probe_http_max_connections,
                max_keepalive_connections=min(
                    keepalive, capture.probe_http_max_connections
                ),
                keepalive_expiry=expiry,
            ),
        }
        self._capture = _build_client(self._limits["capture"], timeout, context)
        self._events = _build_client(self._limits["events"], timeout, context)
        self._probe = _build_client(self._limits["probe"], timeout, context)
        # One client per (purpose, pinned certificate), built on first use.
        self._pinned: dict[tuple[str, str], PinnedCameraClient] = {}
        # Evicted pinned clients waiting out their grace period before closing.
        self._retiring: set[asyncio.Task[None]] = set()
        read_timeout = timeout.read if timeout.read is not None else 60.0
        self.retire_grace_seconds = read_timeout * _RETIRE_GRACE_TIMEOUTS

    @property
    def capture(self) -> httpx.AsyncClient:
        """The client for captures (snapshot plus the metadata read after it)."""
        return self._capture

    @property
    def events(self) -> httpx.AsyncClient:
        """The client for event listeners (SOAP calls and long-polls)."""
        return self._events

    @property
    def probe(self) -> httpx.AsyncClient:
        """The client for operator probes, queries and discovery enrichment."""
        return self._probe

    def for_purpose(
        self, purpose: ClientPurpose, pinned_cert_pem: str | None = None
    ) -> httpx.AsyncClient:
        """Return the client for ``purpose``, pinned when a certificate is given.

        :param pinned_cert_pem: the camera's pinned certificate
            (``camera.tls_pinned_cert_pem``), or ``None``/empty for the shared
            context. With a pin the camera trusts that certificate alone (see
            :mod:`.tls`); the shared clients are never used for it, so a pinned
            camera cannot fall back to the CA bundle.
        :raises ValueError: for an unknown purpose.
        :raises OtherCaptureError: when the stored pin is not a usable
            certificate (the camera is refused, never silently unpinned).
        """
        if pinned_cert_pem:
            return self._pinned_client(purpose, pinned_cert_pem)
        if purpose == "capture":
            return self._capture
        if purpose == "events":
            return self._events
        if purpose == "probe":
            return self._probe
        raise ValueError(f"unknown camera client purpose: {purpose!r}")

    def _pinned_client(self, purpose: str, pem: str) -> PinnedCameraClient:
        """Return (building on first use) the pinned client for ``purpose``."""
        if purpose not in self._limits:
            raise ValueError(f"unknown camera client purpose: {purpose!r}")
        try:
            key = (purpose, pem_fingerprint(pem))
            client = self._pinned.get(key)
            if client is None:
                built = _build_client(
                    self._limits[purpose],
                    self._timeout,
                    pinned_ssl_context(pem),
                    pin=pem,
                )
                assert isinstance(built, PinnedCameraClient)
                client = self._pinned[key] = built
        except (ValueError, ssl.SSLError) as exc:
            raise PinUnusableError(
                "the camera's pinned certificate is unusable; re-pin it in the "
                "camera form",
                local=True,
            ) from exc
        return client

    def retain_pinned(self, pinned_cert_pems: Iterable[str]) -> int:
        """Evict every pinned client whose certificate no camera has any more (#233).

        ``pinned_cert_pems`` is the set of pins currently stored on cameras.
        A client built for a pin that was replaced (re-pin) or removed (unpin)
        would otherwise stay cached until shutdown. An evicted client is closed
        after :attr:`retire_grace_seconds`, so a request that already holds it
        completes; the next request for a camera with that pin builds a new one.
        Unparsable pins are skipped (they never had a client). Call from the
        event loop.

        :returns: how many clients were evicted.
        """
        keep: set[str] = set()
        for pem in pinned_cert_pems:
            try:
                keep.add(pem_fingerprint(pem))
            except ValueError:
                continue
        stale = [key for key in self._pinned if key[1] not in keep]
        for key in stale:
            self._retire(self._pinned.pop(key))
        return len(stale)

    def _retire(self, client: PinnedCameraClient) -> None:
        """Close ``client`` after the grace period, off the caller's path."""

        async def close_later() -> None:
            try:
                await asyncio.sleep(self.retire_grace_seconds)
            finally:
                # Also on cancellation (shutdown): the client is always closed.
                try:
                    await client.aclose()
                except Exception as exc:  # an evicted client must never raise
                    logger.warning("closing an evicted pinned client failed: %r", exc)

        task = asyncio.get_running_loop().create_task(close_later())
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def aclose(self) -> None:
        """Close all clients (shared and pinned), even if closing one fails.

        :raises Exception: the first close failure, after every client has had
            its close attempted.
        """
        first_error: Exception | None = None
        for task in list(self._retiring):
            task.cancel()
        await asyncio.gather(*self._retiring, return_exceptions=True)
        for name, client in (
            ("capture", self._capture),
            ("events", self._events),
            ("probe", self._probe),
            *((f"pinned {key[0]}", pinned) for key, pinned in self._pinned.items()),
        ):
            try:
                await client.aclose()
            except Exception as exc:  # close the others regardless
                logger.warning(
                    "closing the %s camera HTTP client failed: %r", name, exc
                )
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def _build_client(
    limits: httpx.Limits,
    timeout: httpx.Timeout,
    context: ssl.SSLContext,
    *,
    pin: str | None = None,
) -> httpx.AsyncClient:
    """Build one camera client: the single place camera clients are constructed.

    Every client gets a transport that dials only SSRF-validated addresses under
    the camera policy (the administrator's private-subnet opt-in, read at each
    connect), built with the TLS ``context`` and the pool ``limits``, so the
    address pinning composes with -- and never replaces -- the CA bundle or the
    certificate pin. Anything else that must apply to every camera connection
    belongs here, so a pinned camera gets it too.

    :param context: the TLS context the client verifies with -- the shared one,
        or (with ``pin``) the pin context.
    :param pin: the camera's pinned certificate; builds a
        :class:`~.tls.PinnedCameraClient` (https only, fixed mismatch error)
        instead of a plain client.
    """
    transport = pinned_async_transport(
        allow_private=True,
        allowed_private_subnets=camera_allowed_subnets,
        ssl_context=context,
        limits=limits,
    )
    client = _wrap_transport(transport, limits, timeout, context, pin=pin)
    # The default-login twin shares the transport (pool and connections) and
    # adds the connect- and send-time containment (#268).
    twin = _wrap_transport(
        ContainedDefaultTransport(transport), limits, timeout, context, pin=pin
    )
    setattr(client, _CONTAINED_ATTR, twin)
    return client


def _wrap_transport(
    transport: httpx.AsyncBaseTransport,
    limits: httpx.Limits,
    timeout: httpx.Timeout,
    context: ssl.SSLContext,
    *,
    pin: str | None,
) -> httpx.AsyncClient:
    """Wrap ``transport`` in the client type the camera needs (pinned or plain)."""
    if pin is not None:
        return PinnedCameraClient(
            pin, limits=limits, timeout=timeout, transport=transport, trust_env=False
        )
    return httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        verify=context,
        transport=transport,
        trust_env=False,
    )


# The attribute a camera client keeps its default-login twin under.
_CONTAINED_ATTR = "_tlm_contained_twin"


def contained_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Return the view of ``client`` to use when a request carries the default login.

    A camera that inherits the **global default** login (owner decision OD-1)
    must never have it sent to an address outside ``ssrf.allowed_private_subnets``.
    Credential binding decides that when a session loads and again at each use,
    but the cached resolution it reads lives 60 s and a pooled connection is not
    re-checked. The returned client closes that gap at the socket (#268): it
    shares ``client``'s pool, connects only when every address the host has is
    inside the live subnets, and refuses to write to a kept-alive connection
    whose address is not. Cameras with their own credentials keep using
    ``client`` itself.

    Fails closed: a client not built by :class:`CameraHttpClients` has no twin,
    so there is nothing that would contain the login, and this raises instead of
    handing back the plain client. A test that uses a double opts in with
    :func:`allow_uncontained_double`.

    :raises TypeError: ``client`` has no containing twin.
    """
    twin = getattr(client, _CONTAINED_ATTR, None)
    if twin is client or isinstance(twin, httpx.AsyncClient):
        return twin
    raise TypeError(
        "this camera client has no default-login containment; refusing to use "
        "it for the saved default login"
    )


def allow_uncontained_double(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Give a **test double** a twin that is itself, so the default may be used.

    For tests only: production clients are built by :class:`CameraHttpClients`
    and always have a real twin.
    """
    setattr(client, _CONTAINED_ATTR, client)
    return client


def request_timeout(client: httpx.AsyncClient, timeout: float) -> httpx.Timeout:
    """Return a per-request timeout that keeps ``client``'s pool-wait bound.

    A bare float passed as a request ``timeout`` sets every phase, including
    the wait for a pooled connection, to that value. Through this helper the
    connect, read and write phases still get ``timeout``, but the pool wait
    stays at the client's (shorter) pool bound, so an exhausted pool is reported
    before an enclosing capture budget of ``timeout`` expires.
    """
    client_timeout = getattr(client, "timeout", None)
    pool = client_timeout.pool if isinstance(client_timeout, httpx.Timeout) else None
    if pool is None or pool > timeout:
        pool = timeout
    return httpx.Timeout(timeout, pool=pool)


def pool_exhausted_error(url: str) -> OtherCaptureError:
    """Return the local capture error for a request that found no free connection.

    ``local=True`` keeps it out of camera health: the camera was never
    contacted. Callers catch :class:`httpx.PoolTimeout` *before*
    :class:`httpx.TimeoutException` (its base class) and raise this ``from`` it,
    so the cause stays attached.
    """
    return OtherCaptureError(
        f"local connection pool exhausted requesting {url}", local=True
    )
