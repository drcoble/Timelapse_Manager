"""Directory (LDAP / Active Directory) authentication connector.

This module turns a configured :class:`~timelapse_manager.db.models.LdapSettings`
row into three composable, independently testable pieces:

* :func:`authenticate` -- the connector. Binds to the directory as the service
  account, finds the user by their username attribute, re-binds as that user with
  the submitted password to verify it, and resolves the user's group memberships.
  It returns a typed :class:`LdapAuthResult` and **never** raises a raw ``ldap3``
  exception to its caller: every failure maps to an :class:`LdapOutcome`.
* :func:`map_groups_to_role` -- a pure function mapping a set of matched group DNs
  to an application role using highest-privilege-wins precedence.
* :func:`provision_user` -- just-in-time provisioning that finds-or-creates the
  local ``User`` row for an authenticated directory user.

Server failover is delegated to ``ldap3``'s :class:`~ldap3.ServerPool` in
first-reachable order across all configured ``server_urls``: the pool tries each
server in turn and moves on when one is unreachable.

DN comparison
-------------
LDAP distinguished names compare case-insensitively (``CN=Admins,DC=example`` and
``cn=admins,dc=example`` denote the same entry), and directories vary in the
whitespace they emit around RDN separators. :func:`normalize_dn` collapses both so
group matching and the "is this the same user" check are reliable.
"""

from __future__ import annotations

import enum
import logging
import math
import ssl
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import ldap3
from ldap3.core.exceptions import (
    LDAPCommunicationError,
    LDAPException,
    LDAPResponseTimeoutError,
    LDAPServerPoolExhaustedError,
    LDAPSocketOpenError,
)
from ldap3.utils.conv import escape_filter_chars

from ..runtime import get_context

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..db.models import User
    from .ldap_settings_service import LdapSettingsView

logger = logging.getLogger(__name__)

# Seconds ldap3 sleeps between server-pool failover cycles before giving up. Its
# default is 10s, so an all-servers-unreachable pool stalls ~10s on top of the
# per-server connect attempts even when every server refuses instantly. This app
# is the only ldap3 consumer in the process, so lowering the (process-global)
# value is safe and keeps the all-down login bounded at roughly
# ``connect_timeout * num_servers`` plus this one cycle pause. There is no
# per-pool override for this timeout; ``set_config_parameter`` is the only lever.
_POOLING_LOOP_TIMEOUT_SECONDS = 1
ldap3.set_config_parameter("POOLING_LOOP_TIMEOUT", _POOLING_LOOP_TIMEOUT_SECONDS)

# ``auth.ldap_lock_attribute`` default, used when no application context is
# installed (a bare unit test).
_DEFAULT_LOCK_ATTRIBUTE = "nsAccountLock"

# Role precedence, lowest to highest. A user who matches multiple mapped groups
# is granted the most privileged of them (highest-privilege-wins).
_ROLE_PRECEDENCE: tuple[str, ...] = ("viewer", "operator", "admin")


class LdapOutcome(enum.Enum):
    """The disjoint outcomes of an authentication attempt.

    Authentication negatives (wrong password, unknown user) are *results*, not
    exceptions; the error channel is reserved for infrastructure faults so a
    caller can distinguish "deny this login" from "the directory is down".
    """

    AUTHENTICATED = "authenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    NO_SUCH_USER = "no_such_user"
    SERVER_UNREACHABLE = "server_unreachable"
    CONFIG_ERROR = "config_error"
    # The directory affirmatively reports the account as locked (``nsAccountLock``
    # or the configured ``auth.ldap_lock_attribute``). Only the password-less
    # re-evaluation reports it as such; a login for a locked account is a plain
    # ``INVALID_CREDENTIALS`` so it cannot be told from a wrong password.
    LOCKED = "locked"
    # Directory auth is switched off entirely. Distinct from CONFIG_ERROR so the
    # login flow can cleanly fall through to local authentication rather than
    # surfacing a misconfiguration error.
    DISABLED = "disabled"


@dataclass(frozen=True)
class LdapAuthResult:
    """The typed result of :func:`authenticate`.

    ``authenticated`` is true only for :attr:`LdapOutcome.AUTHENTICATED`. On any
    other outcome ``dn`` is empty, ``groups`` is empty, and ``detail`` carries a
    short, secret-free description for logging.
    """

    outcome: LdapOutcome
    dn: str = ""
    display_name: str = ""
    attributes: dict[str, list[str]] = field(default_factory=dict)
    groups: frozenset[str] = frozenset()
    detail: str = ""

    @property
    def authenticated(self) -> bool:
        """True iff the directory verified the user's credentials."""
        return self.outcome is LdapOutcome.AUTHENTICATED


@dataclass(frozen=True)
class LdapDirectoryState:
    """The directory's current view of a user, without verifying a password.

    Returned by :func:`resolve_directory_state` -- the password-less companion to
    :func:`authenticate` used by periodic session re-evaluation. The session
    already proved the password at login; re-evaluation only needs to know whether
    the account still exists and which groups it is now in, so it skips the user
    re-bind entirely.

    ``outcome`` reuses the same :class:`LdapOutcome` vocabulary:

    * :attr:`LdapOutcome.AUTHENTICATED` -- the account was found; ``groups`` holds
      its current memberships (the name "authenticated" is read here as "the
      directory affirmatively resolved this account", not as a fresh credential
      check).
    * :attr:`LdapOutcome.NO_SUCH_USER` -- the account is gone (deprovisioned).
    * :attr:`LdapOutcome.LOCKED` -- the account exists but the directory marks it
      locked (disabled); access is withdrawn like a deprovisioned account.
    * :attr:`LdapOutcome.SERVER_UNREACHABLE` / :attr:`LdapOutcome.CONFIG_ERROR` /
      :attr:`LdapOutcome.DISABLED` -- transient or configuration faults the caller
      must treat as "cannot decide" (fail-safe: keep the session).
    """

    outcome: LdapOutcome
    groups: frozenset[str] = frozenset()
    display_name: str = ""
    detail: str = ""

    @property
    def found(self) -> bool:
        """True iff the directory affirmatively resolved the account."""
        return self.outcome is LdapOutcome.AUTHENTICATED


# ``LdapAuthResult.detail`` when no directory server could be reached *before*
# the user search, so nothing about the submitted name was learned (#67 S7).
# That covers a server that accepts the connection and then goes silent or drops
# it before the service-account bind completes (a receive timeout, #202): that
# fault happens before any client-supplied value is sent, so no request can
# provoke it and it says nothing about a name. A stall *after* the service bind
# (user search, group search) follows client input, so on its own it stays
# "directory error"; it earns this detail only when a client-independent canary
# search stalls as well (:func:`_canary_unanswered`, #216), and so never lets a
# crafted login open the breaker. A stall after the user was found (user re-bind)
# never does, so the answer cannot confirm the name.
DETAIL_NO_SERVER_REACHABLE = "no server reachable"
# ``LdapAuthResult.detail`` when the connection was lost *after* the user search
# found the name. Callers must treat it like any failed login: a distinct answer
# would confirm the name exists.
DETAIL_LOST_AFTER_USER_SEARCH = "server lost after user search"


@dataclass(frozen=True)
class _LocatedUser:
    """The user entry located by the service-account search."""

    dn: str
    attributes: dict[str, list[str]]


# Failures that, before the service bind completes, mean the directory is not
# answering: a receive timeout (ldap3 raises ``LDAPSocketReceiveError``, a
# ``TimeoutError``), a dropped connection, or no response at all.
_PRE_BIND_FAULTS = (LDAPCommunicationError, LDAPResponseTimeoutError)


def _directory_not_answering(exc: LDAPException, *, service_bound: bool) -> bool:
    """Return whether ``exc`` proves the directory is unresponsive (#202).

    True only while the service-account bind has not yet completed. Up to that
    point the conversation carries nothing a client chose (the bind DN and
    password are the administrator's), so every request meets the same fault and
    a crafted login cannot force it; after it, the search filter and the user's
    own bind follow client input and a slow answer there is not evidence.
    """
    return not service_bound and isinstance(exc, _PRE_BIND_FAULTS)


def _directory_unanswering_after_bind(
    exc: LDAPException,
    *,
    service_bound: bool,
    settings: LdapSettingsView,
    bind_password: str | None,
    connection_factory: _ConnectionFactory,
    start_tls: bool,
) -> bool:
    """Return whether a stall *after* the service bind proves a hung directory.

    A stall on the user search, the user re-bind or the group search follows a
    value the client chose (the username goes into the search filter), so on its
    own it is no evidence: a crafted name could make one search slow and, if that
    opened the breaker, defer every deprovisioned account's revocation (#201,
    #202). The fault is therefore *confirmed* with :func:`_canary_unanswered`, an
    operation built only from administrator configuration. If that stalls too, the
    directory is hung for everyone and no request is responsible; if it answers,
    the slow operation was specific to the request and the breaker stays shut.
    """
    if not service_bound or not isinstance(exc, _PRE_BIND_FAULTS):
        return False
    return _canary_unanswered(
        settings=settings,
        bind_password=bind_password,
        connection_factory=connection_factory,
        start_tls=start_tls,
    )


def _canary_unanswered(
    *,
    settings: LdapSettingsView,
    bind_password: str | None,
    connection_factory: _ConnectionFactory,
    start_tls: bool,
) -> bool:
    """Return whether the directory fails to answer a client-independent search.

    Opens a *fresh* connection (a connection that timed out mid-operation is out
    of sync), binds as the service account and runs a base-scope, one-entry,
    no-attributes search of the configured search base. No part of it comes from
    a request, so a login cannot influence the result. Both phases are bounded by
    the same connect and receive timeouts as the conversation it confirms. Any
    answer at all, including an error result or a rejected bind, means the
    directory is alive and returns ``False``. Never raises; the connection is
    always unbound.
    """
    conn: ldap3.Connection | None = None
    try:
        pool = _build_server_pool(settings)
        conn = connection_factory(pool, settings.bind_dn or None, bind_password)
        if not _open_connection(conn, start_tls=start_tls):
            return False
        conn.search(
            search_base=settings.search_base,
            search_filter="(objectClass=*)",
            search_scope=ldap3.BASE,
            attributes=ldap3.NO_ATTRIBUTES,
            size_limit=1,
        )
        return False
    except (LDAPSocketOpenError, LDAPServerPoolExhaustedError):
        return True
    except LDAPException as exc:
        return isinstance(exc, _PRE_BIND_FAULTS)
    finally:
        _safe_unbind(conn)


def _configured_lock_attribute() -> str:
    """Return ``auth.ldap_lock_attribute`` (the default without a context)."""
    try:
        return get_context().settings.auth.ldap_lock_attribute.strip()
    except RuntimeError:
        return _DEFAULT_LOCK_ATTRIBUTE


def _is_locked(attributes: dict[str, list[str]], lock_attribute: str) -> bool:
    """Return whether the located entry carries ``lock_attribute`` = ``TRUE``.

    The attribute name is matched case-insensitively (servers differ in the case
    they return) and so is the value. An absent attribute, or an empty
    ``lock_attribute`` (check disabled), is "not locked".
    """
    if not lock_attribute:
        return False
    wanted = lock_attribute.casefold()
    for name, values in attributes.items():
        if name.casefold() == wanted:
            return any(v.strip().casefold() == "true" for v in values)
    return False


class _ConnectionFactory(Protocol):
    """Builds an ``ldap3.Connection`` for a server pool and (optional) credentials.

    Injected so tests can supply ``client_strategy=MOCK_SYNC`` connections without
    a live directory. ``user``/``password`` of ``None`` requests an anonymous
    connection (only used when no service ``bind_dn`` is configured).
    """

    def __call__(
        self,
        server: ldap3.ServerPool | ldap3.Server,
        user: str | None,
        password: str | None,
    ) -> ldap3.Connection: ...


def normalize_dn(dn: str) -> str:
    """Return a casefolded, whitespace-collapsed form of a DN for comparison.

    LDAP DNs are case-insensitive and directories differ in spacing around the
    ``,`` RDN separators. This canonicalises both so equality and set membership
    behave. It is a *comparison* key only -- never store or display the result.
    """
    parts = [part.strip() for part in dn.split(",")]
    return ",".join(p.casefold() for p in parts)


def _directory_suffix(base_dn: str) -> str:
    """Return the trailing domain-component RDNs of ``base_dn``.

    For ``ou=people,dc=example,dc=com`` this yields ``dc=example,dc=com`` -- the
    naming-context root -- so a group search rooted here reaches a sibling
    ``ou=groups`` subtree the user OU would not. When the DN has no ``dc=``
    components the original DN is returned unchanged (best effort).
    """
    parts = [part.strip() for part in base_dn.split(",") if part.strip()]
    dc_parts = [p for p in parts if p.split("=", 1)[0].strip().casefold() == "dc"]
    if dc_parts:
        return ",".join(dc_parts)
    return base_dn


def map_groups_to_role(
    matched_groups: frozenset[str],
    *,
    admin_group_dn: str | None,
    operator_group_dn: str | None,
    viewer_group_dn: str | None,
) -> str | None:
    """Map a user's group DNs to an application role (highest privilege wins).

    ``matched_groups`` is the set of group DNs the user belongs to. Each configured
    role group DN is compared case-insensitively (via :func:`normalize_dn`) against
    that set. If the user is in several mapped groups, the most privileged role is
    returned (``admin`` > ``operator`` > ``viewer``). A user matching **no**
    configured group returns ``None`` -- they get no role and must not be
    authorised or provisioned.
    """
    normalized_member = {normalize_dn(g) for g in matched_groups}
    role_to_dn = {
        "admin": admin_group_dn,
        "operator": operator_group_dn,
        "viewer": viewer_group_dn,
    }
    # Walk roles highest-privilege-first; the first configured group the user is a
    # member of wins.
    for role in reversed(_ROLE_PRECEDENCE):
        dn = role_to_dn.get(role)
        if dn and normalize_dn(dn) in normalized_member:
            return role
    return None


def _default_connection_factory(
    server: ldap3.ServerPool | ldap3.Server,
    user: str | None,
    password: str | None,
) -> ldap3.Connection:
    """Build a real synchronous connection (auto-binds on context entry/`bind`).

    A ``receive_timeout`` is set so a server that accepts the TCP connection but
    then stalls mid-operation cannot hang a login indefinitely: the per-server
    ``connect_timeout`` (set on the pooled :class:`~ldap3.Server` objects) bounds
    the connect phase, and this bounds every subsequent socket read. The value is
    derived from the pool's connect timeout so a single deployment knob governs
    both phases.
    """
    return ldap3.Connection(
        server,
        user=user,
        password=password,
        # Do not raise on operation result; we inspect ``conn.result`` ourselves so
        # an auth failure stays a result rather than becoming an exception.
        raise_exceptions=False,
        read_only=True,
        receive_timeout=_pool_receive_timeout(server),
    )


def _pool_receive_timeout(
    server: ldap3.ServerPool | ldap3.Server,
) -> int | None:
    """Derive a per-read timeout from a pool/server's configured connect timeout.

    The connector builds its pools via :func:`_build_server_pool`, which stamps
    each :class:`~ldap3.Server` with a ``connect_timeout``. Reuse that value for
    the post-connect read ceiling so both phases share one tunable. Returns
    ``None`` (ldap3's "wait forever" default) only when no server carries a
    connect timeout, which the connector never does in practice.

    The result is a whole number of seconds: ldap3 packs ``receive_timeout`` into
    a ``struct`` for ``SO_RCVTIMEO`` and a fractional value raises ``struct.error``
    on connect. The value is rounded up (and floored at one second) so the read
    ceiling is never shorter than the connect ceiling.
    """
    servers: list[ldap3.Server] = []
    if isinstance(server, ldap3.ServerPool):
        servers = list(server.servers)
    elif isinstance(server, ldap3.Server):
        servers = [server]
    timeouts = [s.connect_timeout for s in servers if s.connect_timeout is not None]
    if not timeouts:
        return None
    return max(1, math.ceil(float(max(timeouts))))


def _build_server_pool(view: LdapSettingsView) -> ldap3.ServerPool:
    """Build a first-reachable failover pool across all configured server URLs.

    TLS mode is applied uniformly to every server: ``ldaps`` wraps the socket in
    TLS, ``starttls`` upgrades a plain connection after connect, ``none`` is
    plaintext. Each URL becomes one :class:`~ldap3.Server` in the pool, tried in
    configuration order (:data:`ldap3.FIRST`) with the next tried on failure.

    Each server carries ``connect_timeout`` from the settings view so an
    unreachable server in the pool stalls for at most that many seconds before the
    next is tried. The all-servers-unreachable worst case is therefore bounded at
    roughly ``connect_timeout * len(server_urls)`` rather than the unbounded OS
    default connect wait.
    """
    tls = None
    if view.tls_mode in ("ldaps", "starttls"):
        # CERT_REQUIRED is the secure default and there is deliberately no
        # skip-verification option. When ``tls_ca_cert_path`` is set, that PEM file
        # is the trust anchor used to validate the directory certificate (a private
        # or internal CA, without touching the host OS trust store); the same Tls
        # object covers both ldaps and StartTLS. When unset (``None``), ldap3 falls
        # back to OpenSSL's default verify paths / ``SSL_CERT_FILE``. Note: ldap3
        # raises ``LDAPSSLConfigurationError`` (an ``LDAPException``) here if the
        # path is set but the file is missing/unreadable -- callers build the pool
        # inside their ``LDAPException`` guard so that surfaces as a typed outcome,
        # never an unhandled error.
        tls = ldap3.Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=(view.tls_ca_cert_path or None),
        )

    use_ssl = view.tls_mode == "ldaps"
    connect_timeout = view.connect_timeout_seconds
    servers = [
        ldap3.Server(
            url,
            use_ssl=use_ssl,
            tls=tls,
            get_info=ldap3.NONE,
            connect_timeout=connect_timeout,
        )
        for url in view.server_urls
    ]
    # ``active=1`` (one finite cycle), not ``active=True``: with the boolean form
    # ldap3 loops forever when every server is unreachable (its loop counter is
    # only decremented for the integer form), so the all-down case never returns.
    # One cycle tries each server once, then raises LDAPServerPoolExhaustedError
    # (an LDAPException), which the connector maps to SERVER_UNREACHABLE. ``exhaust``
    # must stay truthy with active checking (ldap3 rejects exhaust-without-active).
    return ldap3.ServerPool(servers, pool_strategy=ldap3.FIRST, active=1, exhaust=True)


def _open_connection(conn: ldap3.Connection, *, start_tls: bool) -> bool:
    """Open and bind a connection, optionally negotiating StartTLS first.

    Returns the bind result. StartTLS is negotiated on the open socket before the
    bind so credentials are never sent in the clear under ``starttls`` mode.
    """
    if start_tls:
        conn.open()
        conn.start_tls()
    return bool(conn.bind())


def authenticate(
    *,
    settings: LdapSettingsView,
    username: str,
    password: str,
    bind_password: str | None,
    connection_factory: _ConnectionFactory = _default_connection_factory,
    lock_attribute: str | None = None,
) -> LdapAuthResult:
    """Authenticate ``username``/``password`` against the configured directory.

    Flow: bind as the service account (``bind_dn`` + decrypted ``bind_password``,
    or anonymously when no ``bind_dn`` is set) -> search for the user by
    ``username_attribute`` under ``search_base``/``search_filter`` -> re-bind as
    the found user DN with the submitted password -> resolve group memberships per
    ``membership_mode`` and ``nested_groups``.

    :param settings: the masked display view of the LDAP settings row. Its
        ``bind_password`` field is the mask sentinel, not the secret.
    :param bind_password: the *decrypted* service-bind password resolved out of
        band via
        :func:`timelapse_manager.security.ldap_settings_service.resolve_bind_password`,
        or ``None`` for an anonymous service bind. Never logged.
    :param lock_attribute: the account-lock attribute (default
        ``auth.ldap_lock_attribute``; empty disables). A locked account whose
        password verifies is refused as ``INVALID_CREDENTIALS``, the answer a
        wrong password gets, after the same user bind, so it cannot be told apart.
    :returns: a typed :class:`LdapAuthResult`; never raises a raw ``ldap3`` error.
    """
    if not settings.enabled:
        return LdapAuthResult(LdapOutcome.DISABLED, detail="directory auth disabled")
    if not settings.server_urls:
        return LdapAuthResult(LdapOutcome.CONFIG_ERROR, detail="no server configured")
    if not settings.search_base or not settings.username_attribute:
        return LdapAuthResult(
            LdapOutcome.CONFIG_ERROR, detail="user search not configured"
        )
    # An empty submitted password would, under LDAP simple bind, be treated as an
    # anonymous bind and could spuriously "succeed"; reject it as bad credentials.
    if not password:
        return LdapAuthResult(LdapOutcome.INVALID_CREDENTIALS, detail="empty password")

    start_tls = settings.tls_mode == "starttls"
    lock_attr = (
        _configured_lock_attribute() if lock_attribute is None else lock_attribute
    )
    located: _LocatedUser | None = None
    service_bound = False
    # Every connection opened below is unbound on every outcome (the ``finally``),
    # not only on success: a rejected bind or a mid-flight error used to leave the
    # socket to the garbage collector, so a burst of failed logins (or an outage)
    # leaked directory connections.
    service: ldap3.Connection | None = None
    user_conn: ldap3.Connection | None = None

    try:
        # Built inside the guard: a configured-but-missing CA path makes ldap3 raise
        # LDAPSSLConfigurationError (an LDAPException) here, which must surface as a
        # typed outcome rather than an unhandled error.
        pool = _build_server_pool(settings)
        service = connection_factory(pool, settings.bind_dn or None, bind_password)
        if not _open_connection(service, start_tls=start_tls):
            # The service account itself failed to bind: a misconfiguration, not an
            # end-user credential problem.
            return LdapAuthResult(
                LdapOutcome.CONFIG_ERROR, detail="service bind failed"
            )
        service_bound = True

        located = _find_user(service, settings, username, lock_attribute=lock_attr)
        if located is None:
            return LdapAuthResult(LdapOutcome.NO_SUCH_USER, detail="user not found")

        # Verify the password by re-binding as the located user DN.
        user_conn = connection_factory(pool, located.dn, password)
        if not _open_connection(user_conn, start_tls=start_tls):
            return LdapAuthResult(
                LdapOutcome.INVALID_CREDENTIALS, detail="user bind rejected"
            )
        _safe_unbind(user_conn)
        user_conn = None

        if _is_locked(located.attributes, lock_attr):
            return LdapAuthResult(
                LdapOutcome.INVALID_CREDENTIALS, detail="account locked"
            )

        groups = _resolve_groups(service, settings, located)
        display_name = _extract_display_name(settings, located, username)
        return LdapAuthResult(
            LdapOutcome.AUTHENTICATED,
            dn=located.dn,
            display_name=display_name,
            attributes=located.attributes,
            groups=frozenset(groups),
        )
    except (LDAPSocketOpenError, LDAPServerPoolExhaustedError):
        # Every configured server in the pool was unreachable: either a single
        # server refused the socket, or the failover pool exhausted every server.
        logger.warning("LDAP servers unreachable during authentication")
        detail = (
            DETAIL_NO_SERVER_REACHABLE
            if located is None
            else DETAIL_LOST_AFTER_USER_SEARCH
        )
        return LdapAuthResult(LdapOutcome.SERVER_UNREACHABLE, detail=detail)
    except LDAPException as exc:
        if _directory_not_answering(exc, service_bound=service_bound):
            # Silent or dropped before the service bind finished (#202): the same
            # fault for every request, so it counts as "no server reachable".
            logger.warning("LDAP directory not answering during authentication")
            return LdapAuthResult(
                LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_NO_SERVER_REACHABLE
            )
        # Any other protocol-level fault: surface as unreachable/infra rather than
        # leaking an ldap3 exception type. The message may embed a DN, so it is not
        # logged at info; the typed detail stays generic.
        if located is None and _directory_unanswering_after_bind(
            exc,
            service_bound=service_bound,
            settings=settings,
            bind_password=bind_password,
            connection_factory=connection_factory,
            start_tls=start_tls,
        ):
            # Bind-then-hang: the user search stalled and so did the canary, so
            # every request meets the same fault (not a crafted name). After the
            # user was found the detail stays generic, as for a lost server.
            logger.warning("LDAP directory hung after bind during authentication")
            return LdapAuthResult(
                LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_NO_SERVER_REACHABLE
            )
        logger.warning("LDAP protocol error during authentication")
        return LdapAuthResult(LdapOutcome.SERVER_UNREACHABLE, detail="directory error")
    finally:
        _safe_unbind(user_conn)
        _safe_unbind(service)


def resolve_directory_state(
    *,
    settings: LdapSettingsView,
    username: str,
    bind_password: str | None,
    connection_factory: _ConnectionFactory = _default_connection_factory,
    lock_attribute: str | None = None,
) -> LdapDirectoryState:
    """Re-read a user's directory state without a password (re-evaluation seam).

    The password-less companion to :func:`authenticate`. It binds as the service
    account, locates the user by ``username_attribute``, and resolves the current
    group memberships -- but performs **no** user re-bind, because re-evaluation
    runs on an already-authenticated session where no password is available.

    Used by periodic session re-evaluation: a long-lived "remember me" session can
    outlive a directory change, so before continuing to trust it the caller checks
    whether the account still exists and recomputes the role from current groups.

    :returns: a typed :class:`LdapDirectoryState`; never raises a raw ``ldap3``
        error. Infrastructure faults map to ``SERVER_UNREACHABLE`` so the caller
        can fail safe (keep the session) on a transient outage rather than locking
        a user out. An account whose ``lock_attribute`` (default
        ``auth.ldap_lock_attribute``) is ``TRUE`` returns
        :attr:`LdapOutcome.LOCKED`.
    """
    if not settings.enabled:
        return LdapDirectoryState(
            LdapOutcome.DISABLED, detail="directory auth disabled"
        )
    if not settings.server_urls:
        return LdapDirectoryState(
            LdapOutcome.CONFIG_ERROR, detail="no server configured"
        )
    if not settings.search_base or not settings.username_attribute:
        return LdapDirectoryState(
            LdapOutcome.CONFIG_ERROR, detail="user search not configured"
        )

    start_tls = settings.tls_mode == "starttls"
    lock_attr = (
        _configured_lock_attribute() if lock_attribute is None else lock_attribute
    )
    service: ldap3.Connection | None = None
    service_bound = False

    try:
        # Built inside the guard: a configured-but-missing CA path makes ldap3 raise
        # LDAPSSLConfigurationError (an LDAPException) here, which must surface as a
        # typed outcome rather than an unhandled error.
        pool = _build_server_pool(settings)
        service = connection_factory(pool, settings.bind_dn or None, bind_password)
        if not _open_connection(service, start_tls=start_tls):
            return LdapDirectoryState(
                LdapOutcome.CONFIG_ERROR, detail="service bind failed"
            )
        service_bound = True

        located = _find_user(service, settings, username, lock_attribute=lock_attr)
        if located is None:
            return LdapDirectoryState(LdapOutcome.NO_SUCH_USER, detail="user not found")
        if _is_locked(located.attributes, lock_attr):
            return LdapDirectoryState(LdapOutcome.LOCKED, detail="account locked")

        groups = _resolve_groups(service, settings, located)
        display_name = _extract_display_name(settings, located, username)
        return LdapDirectoryState(
            LdapOutcome.AUTHENTICATED,
            groups=frozenset(groups),
            display_name=display_name,
        )
    except (LDAPSocketOpenError, LDAPServerPoolExhaustedError):
        logger.warning("LDAP servers unreachable during re-evaluation")
        return LdapDirectoryState(
            LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_NO_SERVER_REACHABLE
        )
    except LDAPException as exc:
        if _directory_not_answering(exc, service_bound=service_bound):
            logger.warning("LDAP directory not answering during re-evaluation")
            return LdapDirectoryState(
                LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_NO_SERVER_REACHABLE
            )
        if _directory_unanswering_after_bind(
            exc,
            service_bound=service_bound,
            settings=settings,
            bind_password=bind_password,
            connection_factory=connection_factory,
            start_tls=start_tls,
        ):
            logger.warning("LDAP directory hung after bind during re-evaluation")
            return LdapDirectoryState(
                LdapOutcome.SERVER_UNREACHABLE, detail=DETAIL_NO_SERVER_REACHABLE
            )
        logger.warning("LDAP protocol error during re-evaluation")
        return LdapDirectoryState(
            LdapOutcome.SERVER_UNREACHABLE, detail="directory error"
        )
    finally:
        _safe_unbind(service)


def _find_user(
    conn: ldap3.Connection,
    settings: LdapSettingsView,
    username: str,
    *,
    lock_attribute: str = "",
) -> _LocatedUser | None:
    """Search for the user entry by username attribute; return its DN + attributes.

    The search filter combines the configured ``search_filter`` (an object-class
    or scope restriction) with an equality match on ``username_attribute``. The
    submitted username is escaped so it cannot inject LDAP filter syntax.

    ``lock_attribute`` is requested by name next to ``*``: the lock flag is an
    operational attribute that a bare ``*`` does not return (#67 S10).
    """
    safe_username = escape_filter_chars(username)
    user_clause = f"({settings.username_attribute}={safe_username})"
    base_filter = settings.search_filter or "(objectClass=*)"
    combined = f"(&{base_filter}{user_clause})"

    ok = conn.search(
        search_base=settings.search_base,
        search_filter=combined,
        search_scope=ldap3.SUBTREE,
        attributes=[ldap3.ALL_ATTRIBUTES, lock_attribute]
        if lock_attribute
        else ldap3.ALL_ATTRIBUTES,
    )
    if not ok or not conn.entries:
        return None
    entry = conn.entries[0]
    return _LocatedUser(
        dn=str(entry.entry_dn),
        attributes=_attributes_as_str_lists(entry),
    )


def _resolve_groups(
    conn: ldap3.Connection,
    settings: LdapSettingsView,
    located: _LocatedUser,
) -> set[str]:
    """Resolve the user's group DNs per the configured membership mode.

    ``memberof`` reads the user entry's ``memberOf`` values directly. ``group_search``
    searches group entries whose ``member`` attribute references the user's DN. When
    ``nested_groups`` is set under group-search, parent groups are followed
    transitively (a bounded walk) so indirect memberships are included.
    """
    if settings.membership_mode == "memberof":
        # The user entry already lists its groups; nested expansion (if any) is the
        # directory's responsibility for the memberOf attribute.
        attrs = located.attributes
        return set(attrs.get("memberOf", []) or attrs.get("memberof", []))

    # group_search: find groups whose member attribute names this user. Groups
    # commonly live outside the user subtree, so search the configured
    # group_search_base, falling back to the directory suffix of the user base
    # (e.g. "dc=example,dc=com") rather than the user OU itself -- searching only
    # under the user OU would miss a sibling "ou=groups" tree.
    base = settings.group_search_base or _directory_suffix(settings.search_base)
    found: set[str] = set()
    _collect_member_groups(conn, base, located.dn, found, settings.nested_groups)
    return found


def _collect_member_groups(
    conn: ldap3.Connection,
    base: str,
    member_dn: str,
    found: set[str],
    nested: bool,
    *,
    _depth: int = 0,
) -> None:
    """Add every group whose ``member`` is ``member_dn``; recurse when nested.

    The recursion depth is bounded to guard against directory cycles. Already-seen
    groups are skipped so a membership cycle terminates.
    """
    if _depth > 16:
        return
    safe_member = escape_filter_chars(member_dn)
    ok = conn.search(
        search_base=base,
        search_filter=f"(member={safe_member})",
        search_scope=ldap3.SUBTREE,
        attributes=["cn"],
    )
    if not ok:
        return
    for group in conn.entries:
        group_dn = group.entry_dn
        if group_dn in found:
            continue
        found.add(group_dn)
        if nested:
            _collect_member_groups(
                conn, base, group_dn, found, nested, _depth=_depth + 1
            )


def _extract_display_name(
    settings: LdapSettingsView, located: _LocatedUser, fallback: str
) -> str:
    """Return the user's display name from the configured attribute, or a fallback."""
    attr = settings.display_name_attribute
    if attr:
        values = located.attributes.get(attr) or located.attributes.get(attr.lower())
        if values:
            return values[0]
    return fallback


def _attributes_as_str_lists(entry: object) -> dict[str, list[str]]:
    """Coerce an ldap3 entry's attributes into a ``{name: [str, ...]}`` mapping."""
    result: dict[str, list[str]] = {}
    raw = getattr(entry, "entry_attributes_as_dict", {})
    for key, values in raw.items():
        result[key] = [str(v) for v in values]
    return result


def _safe_unbind(conn: ldap3.Connection | None) -> None:
    """Unbind a connection (``None`` is a no-op), swallowing any teardown error.

    Called from ``finally`` blocks, so it must never raise: an error escaping here
    would replace the typed result the connector is about to return. That covers a
    socket-level ``OSError`` on a connection the peer already dropped, not only
    ``LDAPException``. Nothing about the failure is logged beyond its type, since
    the message may embed a DN.
    """
    if conn is None:
        return
    try:
        conn.unbind()
    except Exception as exc:
        logger.debug("LDAP unbind failed (%s)", type(exc).__name__)


def provision_user(
    session: Session,
    *,
    username: str,
    role: str,
    display_name: str,
) -> User:
    """Find-or-create the local ``User`` row for an authenticated directory user.

    On first login the row is created with ``auth_source="ldap"``, the mapped
    role, and no password hash (directory users never carry one). On later logins
    the role is refreshed from the current group mapping. The display name is not
    persisted on the ``User`` model (which has no display-name column); it is
    accepted so callers can surface it without a second lookup.

    :raises LdapProvisioningError: if a *local* account already owns the username.
        A directory login must never silently take over a local account, so the
        collision is refused rather than converting the row to ``auth_source="ldap"``.
    """
    from ..db.models import User
    from ..usernames import username_key

    # One row per person (#4): match case-insensitively, so ``Alice.smith`` and
    # ``alice.smith`` resolve to the same account -- and so a directory name that
    # differs from a local account only by case is still refused below.
    existing = (
        session.query(User)
        .filter(User.username_key == username_key(username))
        .one_or_none()
    )
    if existing is not None:
        if existing.auth_source != "ldap":
            raise LdapProvisioningError(
                f"username '{username}' is already a local account"
            )
        # Refresh the directory-derived fields on each login, including the
        # stored spelling (callers pass the directory's canonical value).
        if existing.username != username:
            existing.username = username
        existing.role = role
        existing.auth_source = "ldap"
        existing.password_hash = None
        session.flush()
        return existing

    user = User(
        username=username,
        auth_source="ldap",
        password_hash=None,
        role=role,
        enabled=True,
    )
    session.add(user)
    session.flush()
    return user


class LdapProvisioningError(Exception):
    """Raised when JIT provisioning cannot safely create or update a user row."""


# Re-exported so callers can use the factory type in their own signatures.
ConnectionFactory = _ConnectionFactory

__all__ = [
    "ConnectionFactory",
    "LdapAuthResult",
    "LdapDirectoryState",
    "LdapOutcome",
    "LdapProvisioningError",
    "authenticate",
    "map_groups_to_role",
    "normalize_dn",
    "provision_user",
    "resolve_directory_state",
]
