"""Periodic re-evaluation of directory-backed (LDAP) sessions.

A local session is trusted for its whole lifetime: nothing about a local account
changes outside this application. A directory-backed session is different -- the
account it represents can be disabled, deleted, or moved between groups in the
directory at any time, and a long-lived "remember me" session would otherwise
keep honouring stale privileges until its absolute cap.

This module closes that gap. On a request whose session is directory-backed and
whose last re-check is older than the configured interval, it re-reads the user's
current directory state (without a password -- see
:func:`timelapse_manager.security.ldap_directory.resolve_directory_state`) and
reconciles the stored account with it:

* **Account gone or maps to no role** -> revoke the session (force re-login).
* **Role changed** -> update the stored role, keep the session.
* **Unchanged** -> keep the session.

Fail-safe on outage
-------------------
If the directory is unreachable or misconfigured at re-check time, the session is
**kept and the timestamp is not advanced**: a transient outage must never lock
out an otherwise-valid session, and not advancing the timestamp means the next
request retries promptly once the directory recovers. Only a *definitive*
negative answer from the directory (the account is gone, or it now maps to no
mapped group) revokes a session.

The interval gate, the directory call, and the clock are all injectable so the
behaviour is unit-testable without a live directory or real elapsed time.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session as DbSession

from ..config import SessionSettings
from ..db.models import Session as SessionRow
from ..db.models import User
from .ldap_directory import (
    LdapDirectoryState,
    LdapOutcome,
    map_groups_to_role,
    resolve_directory_state,
)

logger = logging.getLogger(__name__)

_LDAP_AUTH_SOURCE = "ldap"

NowFn = Callable[[], datetime.datetime]

# A function that, given the username, returns the directory's current view of the
# account. Injected so tests can supply a deterministic resolver and the real
# wiring can bind in the settings load + bind-password decrypt. Returning
# ``None`` means "no directory configured / cannot resolve settings" and is
# treated as a fail-safe keep, exactly like an outage.
DirectoryResolver = Callable[[str], LdapDirectoryState | None]


def _utcnow() -> datetime.datetime:
    """Return the current UTC time as a naive datetime (matches stored columns)."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _default_directory_resolver(db: DbSession) -> DirectoryResolver:
    """Build the production resolver: load settings, decrypt bind pw, re-search.

    Bound lazily against the request's DB session. Returns a resolver that, for a
    username, loads the current LDAP settings view and the decrypted service-bind
    password and asks the connector for the account's present state. If LDAP is
    not enabled the connector itself returns the ``DISABLED`` outcome, which the
    caller treats as a fail-safe keep.
    """
    # Imported here (not at module load) so the session layer's import graph does
    # not pull in the settings-service/crypto chain unless re-eval actually runs.
    from .ldap_settings_service import load_settings, resolve_bind_password

    def resolve(username: str) -> LdapDirectoryState | None:
        view = load_settings(db)
        bind_password = resolve_bind_password(db)
        return resolve_directory_state(
            settings=view,
            username=username,
            bind_password=bind_password,
        )

    return resolve


def revalidate_ldap_session(
    db: DbSession,
    row: SessionRow,
    user: User,
    *,
    settings: SessionSettings,
    resolver: DirectoryResolver | None = None,
    now: NowFn = _utcnow,
) -> bool:
    """Re-evaluate a directory-backed session against the directory if due.

    Returns ``True`` if the session remains valid (kept, possibly with an updated
    role) and ``False`` if it was revoked and must no longer be honoured.

    A no-op that returns ``True`` for:

    * a local session (``auth_source != "ldap"``) -- never re-evaluated;
    * a directory session whose last re-check is within the configured interval.

    When a re-check is due, the injected ``resolver`` is asked for the account's
    current directory state and the outcome is applied:

    * account gone (``NO_SUCH_USER``) or present-but-no-mapped-group -> revoke,
      return ``False``;
    * role changed -> update ``user.role``, advance the timestamp, return ``True``;
    * unchanged -> advance the timestamp, return ``True``;
    * directory unreachable / misconfigured / disabled, or no resolver available
      -> **fail safe**: keep the session, do **not** advance the timestamp,
      return ``True``.
    """
    if user.auth_source != _LDAP_AUTH_SOURCE:
        return True

    current = now()
    if not _revalidation_due(row, settings, current):
        return True

    if resolver is None:
        resolver = _default_directory_resolver(db)
    state = resolver(user.username)

    # No resolver answer at all (settings unresolvable) -> fail safe, keep.
    if state is None:
        return True

    if state.outcome is LdapOutcome.NO_SUCH_USER:
        # Definitive: the account has been deprovisioned. Revoke now so a
        # "remember me" cookie cannot outlive removal.
        _revoke(row, current)
        logger.info("Revoked LDAP session: account no longer in directory")
        return False

    if state.outcome is not LdapOutcome.AUTHENTICATED:
        # SERVER_UNREACHABLE / CONFIG_ERROR / DISABLED: cannot decide. Fail safe --
        # keep the session and leave last_revalidated_at unchanged so the next
        # request retries promptly once the directory recovers.
        logger.warning(
            "LDAP re-evaluation could not reach the directory; keeping session"
        )
        return True

    # The account is present: recompute the role from current group membership.
    # The role group DNs live in the settings row, not in the directory state, so
    # they are read directly here.
    from .ldap_settings_service import load_settings

    view = load_settings(db)
    new_role = map_groups_to_role(
        state.groups,
        admin_group_dn=view.admin_group_dn or None,
        operator_group_dn=view.operator_group_dn or None,
        viewer_group_dn=view.viewer_group_dn or None,
    )
    if new_role is None:
        # Present but in no mapped group: access is no longer granted. Definitive
        # negative -> revoke.
        _revoke(row, current)
        logger.info("Revoked LDAP session: user maps to no role in directory")
        return False

    if new_role != user.role:
        user.role = new_role
        logger.info("Updated LDAP session role from directory group membership")

    row.last_revalidated_at = current
    db.flush()
    return True


def _revalidation_due(
    row: SessionRow, settings: SessionSettings, current: datetime.datetime
) -> bool:
    """Return whether this session's directory re-check interval has elapsed.

    A session whose ``last_revalidated_at`` is unset is due immediately -- but the
    login path seeds it at session creation, so in practice a freshly minted LDAP
    session is not re-checked until a full interval has passed.
    """
    last = row.last_revalidated_at
    if last is None:
        return True
    interval = datetime.timedelta(seconds=settings.ldap_revalidation_interval_seconds)
    return current - last >= interval


def _revoke(row: SessionRow, when: datetime.datetime) -> None:
    """Mark a session row revoked (by flag, preserving the row for audit)."""
    row.revoked = True
    row.revoked_at = when


__all__ = [
    "DirectoryResolver",
    "revalidate_ldap_session",
]
