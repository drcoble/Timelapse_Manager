"""``httpx`` transports that connect only to SSRF-validated addresses.

The SSRF guard (:mod:`timelapse_manager.security.ssrf`) validates the addresses
a host resolves to. A plain ``httpx`` client then resolves the name **again**
when it connects, so a DNS-rebinding host can answer with a public address for
the check and ``127.0.0.1`` or ``169.254.169.254`` for the connection.

:func:`pinned_async_transport` closes that gap at the socket. Its network
backend receives the request's host at TCP-connect time, obtains its validated
IPs (:func:`~timelapse_manager.security.ssrf.pinned_addresses_async`: the cached
result of the check, or a fresh resolve-and-check) and connects to those IPs
only, trying each in resolver order (so a dual-stack host whose first answer is
unreachable still connects over the other family). ``httpcore`` takes the TLS
SNI name and the ``Host`` header from the request URL, not from the connect
address, so certificate verification and virtual hosting still use the
original hostname.

Every connection the transport makes is pinned, including one ``httpx`` would
open for a redirect (callers also disable redirects). A denied or unresolvable
host fails as :class:`httpcore.ConnectError`, which ``httpx`` raises as
:class:`httpx.ConnectError`, so callers' existing transport-error handling
covers it. The client must not route through an environment proxy, or the
connection would go to the proxy instead: callers pass ``trust_env=False`` to
the client (the CA bundle settings in the environment are still honoured, by
the transport's own SSL context, or the one the caller passes).

A caller that needs its own trust (the camera clients: a configured CA bundle,
or a single pinned certificate) passes the context to
:func:`pinned_async_transport`; the transport verifies with exactly that and
never builds a default one beside it. The SNI name and the context's
host-name check (when it has one) still use the request's host, never the
connect address.
"""

from __future__ import annotations

import contextvars
import ipaddress
import logging
import ssl
import typing
from collections.abc import Callable, Iterable

import httpcore
import httpx

from .ssrf import (
    SsrfError,
    _in_any,
    _normalise,
    _parse_allowed_subnets,
    pinned_addresses_async,
)

logger = logging.getLogger(__name__)


def _default_inner_backend() -> httpcore.AsyncNetworkBackend:
    """The real socket backend (``anyio``, which serves asyncio)."""
    return httpcore.AnyIOBackend()


# The backend the pinned one delegates real sockets to. Tests replace it to
# record the connect address without opening a socket.
inner_backend_factory: Callable[[], httpcore.AsyncNetworkBackend] = (
    _default_inner_backend
)


#: True while a request that carries the **bound default login** is in flight on
#: this task (set by :class:`ContainedDefaultTransport`). Connections and sends
#: made under it are contained to the allowed private subnets (#268).
_default_contained: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "tlm_default_contained", default=False
)

#: The message for a refused contained connection or send. Names no host.
CONTAINED_MESSAGE = (
    "the saved default login is sent only to addresses inside the allowed "
    "private subnets"
)


def address_inside_subnets(
    address: str, allowed_private_subnets: Iterable[str]
) -> bool:
    """Return whether the literal IP ``address`` is inside any of the subnets.

    ``False`` for something that is not an IP literal and for an empty or
    wholly malformed subnet list (fail closed). An IPv4-mapped IPv6 address is
    compared as the IPv4 address it carries, as the SSRF guard does.
    """
    try:
        ip = _normalise(ipaddress.ip_address(address))
    except ValueError:
        return False
    return _in_any(ip, _parse_allowed_subnets(allowed_private_subnets))


def _live_subnets(
    source: Iterable[str] | Callable[[], Iterable[str]],
) -> tuple[str, ...]:
    """The current subnet policy from a tuple or a callable returning one."""
    return tuple(source() if callable(source) else source)


class _ContainedStream(httpcore.AsyncNetworkStream):
    """A socket that refuses to carry the bound default login out of the subnets.

    Wraps every stream the pinned backend opens, remembering the validated IP it
    dialled. While a default-login request runs (:data:`_default_contained`),
    :meth:`write` re-checks that IP against the **live** policy before any byte
    leaves, so a kept-alive pooled connection to an address that is outside the
    subnets now (the administrator narrowed them, or the connection was opened
    by a request that did not carry the default) never gets the credentials
    (#268). Reads, TLS and closing are delegated untouched.
    """

    def __init__(
        self,
        inner: httpcore.AsyncNetworkStream,
        address: str,
        subnets: Iterable[str] | Callable[[], Iterable[str]],
        *,
        answer_inside: bool,
    ) -> None:
        self._inner = inner
        self._address = address
        self._subnets = subnets
        # Whether **every** address the host had when this connection was opened
        # was inside the subnets. A connection a plain request opened to a name
        # with a mixed answer is not, whichever address it happened to dial.
        self._answer_inside = answer_inside

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Delegate."""
        return await self._inner.read(max_bytes, timeout=timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        """Refuse the write when it would carry the default out of the subnets."""
        if _default_contained.get() and not (
            self._answer_inside
            and address_inside_subnets(self._address, _live_subnets(self._subnets))
        ):
            raise httpcore.ConnectError(CONTAINED_MESSAGE)
        await self._inner.write(buffer, timeout=timeout)

    async def aclose(self) -> None:
        """Delegate."""
        await self._inner.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Upgrade to TLS; the result stays guarded for the same address."""
        upgraded = await self._inner.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )
        return _ContainedStream(
            upgraded,
            self._address,
            self._subnets,
            answer_inside=self._answer_inside,
        )

    def get_extra_info(self, info: str) -> typing.Any:
        """Delegate."""
        return self._inner.get_extra_info(info)


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connects to a validated IP for each host, never to a re-resolved name."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        allowed_private_subnets: Iterable[str] | Callable[[], Iterable[str]] = (),
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        """Create the backend under one SSRF policy.

        :param allow_private: as for the SSRF guard; webhooks pass ``False``.
        :param allowed_private_subnets: as for the SSRF guard, or a callable
            returning them. A callable is read on **every** connect, so a policy
            an administrator edits at run time (the camera opt-in subnets) takes
            effect on the next connection without rebuilding the client.
        :param inner: the backend that opens the real socket.
        """
        self._allow_private = allow_private
        self._subnets_source = allowed_private_subnets
        self._inner = inner if inner is not None else inner_backend_factory()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Validate ``host``, then connect to a validated IP on ``port``.

        Tries each validated address in order and returns the first stream that
        connects; when none does, the last connect error is raised.
        """
        source = self._subnets_source
        try:
            subnets = tuple(source() if callable(source) else source)
            addresses = await pinned_addresses_async(
                host,
                allow_private=self._allow_private,
                allowed_private_subnets=subnets,
            )
        except SsrfError as exc:
            # The message names only the host, never a URL or credential.
            raise httpcore.ConnectError(
                f"outbound connection blocked by the SSRF guard: {exc}"
            ) from exc
        answer_inside = all(
            address_inside_subnets(address, subnets) for address in addresses
        )
        if _default_contained.get() and not answer_inside:
            # The same rule as credential binding: every address the host has
            # must be inside the subnets, decided on the answer dialled now.
            raise httpcore.ConnectError(CONTAINED_MESSAGE)
        last_error: Exception | None = None
        for address in addresses:
            try:
                stream = await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                return _ContainedStream(
                    stream, address, self._subnets_source, answer_inside=answer_inside
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
        if last_error is None:  # unreachable: a resolution is never empty
            raise httpcore.ConnectError("no validated address to connect to")
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Unix sockets are never an outbound target."""
        raise httpcore.ConnectError("unix sockets are not allowed for outbound calls")

    async def sleep(self, seconds: float) -> None:
        """Delegate the connection pool's retry sleep."""
        await self._inner.sleep(seconds)


class _PinnedAsyncTransport(httpx.AsyncHTTPTransport):
    """``AsyncHTTPTransport`` whose connection pool uses a pinned backend.

    ``httpx`` 0.28 exposes no ``network_backend`` argument, so the pool built by
    the base class is replaced with one built the same way plus the backend.
    The pinning tests fail if a future ``httpx`` stops using ``_pool``.
    """

    def __init__(
        self,
        backend: PinnedNetworkBackend,
        *,
        ssl_context: ssl.SSLContext | None = None,
        limits: httpx.Limits | None = None,
    ) -> None:
        """Create the transport.

        :param ssl_context: the TLS context connections verify with. ``None``
            builds the ``httpx`` default (the environment's CA settings
            honoured); a caller with its own trust passes it so a configured
            bundle or a certificate pin is not silently replaced.
        :param limits: the pool limits; ``max_connections=100`` and
            ``max_keepalive_connections=20`` when ``None``.
        """
        context = (
            ssl_context
            if ssl_context is not None
            else httpx.create_ssl_context(trust_env=True)
        )
        super().__init__(verify=context, trust_env=True)
        limits = limits or httpx.Limits(
            max_connections=100, max_keepalive_connections=20
        )
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=backend,
        )


def pinned_async_transport(
    *,
    allow_private: bool = False,
    allowed_private_subnets: Iterable[str] | Callable[[], Iterable[str]] = (),
    ssl_context: ssl.SSLContext | None = None,
    limits: httpx.Limits | None = None,
) -> httpx.AsyncHTTPTransport:
    """Return a transport that connects only to SSRF-validated addresses.

    Use it with ``httpx.AsyncClient(transport=..., trust_env=False,
    follow_redirects=False)``. ``ssl_context`` and ``limits`` are described on
    :class:`_PinnedAsyncTransport`; ``allowed_private_subnets`` on
    :class:`PinnedNetworkBackend`.
    """
    return _PinnedAsyncTransport(
        PinnedNetworkBackend(
            allow_private=allow_private,
            allowed_private_subnets=allowed_private_subnets,
        ),
        ssl_context=ssl_context,
        limits=limits,
    )


class ContainedDefaultTransport(httpx.AsyncBaseTransport):
    """A view of a pinned transport for requests that carry the bound default login.

    Shares the inner transport's pool and connections; the only difference is
    that every request it passes on runs with :data:`_default_contained` set, so
    the pinned backend refuses to connect to, or write to, a socket whose
    address is outside the live allowed private subnets (#268). It owns
    nothing: closing it leaves the inner transport to its own client.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` with the containment flag set for this task."""
        token = _default_contained.set(True)
        try:
            return await self._inner.handle_async_request(request)
        finally:
            _default_contained.reset(token)

    async def aclose(self) -> None:
        """No-op: the inner transport's own client closes it."""
