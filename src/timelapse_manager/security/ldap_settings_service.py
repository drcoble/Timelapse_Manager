"""Read and update the singleton LDAP directory-settings row.

This is the CRUD seam over the single ``ldap_settings`` row (its primary key is
constrained to ``1``). It enforces one correctness rule the web form alone cannot:
the directory **bind password** is masked when read for display and must not be
overwritten with the mask when an unchanged form is saved.

Secret handling and encryption at rest
--------------------------------------
The bind password is the one secret this row carries. It is encrypted on write
and never returned in plaintext from the display path:

* :func:`load_settings` returns the mask sentinel ``"***"`` for display when a
  password is stored (and an empty string when none is).
* :func:`update_settings` treats an empty / unchanged / sentinel-valued submitted
  password as "leave the stored secret alone"; only a genuinely new value
  overwrites it, and only that new value is encrypted (the keep-stored branch
  returns the already-encrypted ciphertext untouched, so it is never
  double-wrapped).
* :func:`resolve_bind_password` is the separate, explicit decrypt-at-use resolver
  the connector calls before binding. The bind password is never logged.

Encryption uses the versioned ``enc:v1:`` ciphertext prefix, so a legacy plaintext
value (written before encryption existed) is recognised on read and returned
unchanged, then silently re-encrypted the next time the field is written. No data
migration is required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..db.models import LdapSettings
from .crypto import decrypt_secret, encrypt_secret

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The sentinel a stored bind password is masked to for display, and the value the
# update path recognises as "unchanged -- keep the stored secret".
MASK_SENTINEL = "***"

# The singleton row's fixed primary key.
_ROW_ID = 1

# Default per-server TCP connect timeout (seconds) applied when a settings row
# does not specify one. Five seconds is long enough to tolerate a slow LAN
# directory yet short enough that a login against an unreachable pool fails fast.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

# Accepted enum values; an out-of-range submission falls back to the safe default.
_TLS_MODES = ("none", "ldaps", "starttls")
_MEMBERSHIP_MODES = ("memberof", "group_search")


@dataclass(frozen=True)
class LdapSettingsView:
    """A display-safe projection of the LDAP settings row.

    The bind password is never the real secret here: it is :data:`MASK_SENTINEL`
    when one is stored and an empty string when none is.
    """

    enabled: bool = False
    server_urls: list[str] = field(default_factory=list)
    tls_mode: str = "none"
    # Per-server TCP connect ceiling. Bounds how long a single unreachable server
    # in the failover pool may stall before the next is tried, so the all-servers-
    # down case returns in roughly this value times the number of servers rather
    # than hanging on the OS default connect timeout.
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    # Optional path to a CA-certificate (PEM) trust anchor for LDAPS / StartTLS
    # validation against a private CA. Non-secret plain config: surfaced verbatim
    # (never masked), unlike the bind password. Empty string when unset.
    tls_ca_cert_path: str | None = None
    bind_dn: str = ""
    bind_password: str = ""  # masked: MASK_SENTINEL if set, "" if not
    bind_password_set: bool = False
    search_base: str = ""
    search_filter: str = ""
    group_search_base: str = ""
    username_attribute: str = ""
    display_name_attribute: str = ""
    membership_mode: str = "memberof"
    nested_groups: bool = False
    admin_group_dn: str = ""
    admin_group_filter: str = ""
    operator_group_dn: str = ""
    operator_group_filter: str = ""
    viewer_group_dn: str = ""
    viewer_group_filter: str = ""


@dataclass(frozen=True)
class LdapSettingsUpdate:
    """The fields an admin may submit to update LDAP settings.

    ``bind_password`` follows the masked write-back rule: ``None``, an empty
    string, or :data:`MASK_SENTINEL` all mean "keep the stored secret". Only a
    different, non-empty value replaces it.
    """

    enabled: bool
    server_urls: list[str]
    tls_mode: str
    # Non-secret CA-cert trust-anchor path: an empty string normalises to ``None``
    # (no masked write-back rule applies -- it is not a secret).
    tls_ca_cert_path: str | None
    bind_dn: str | None
    bind_password: str | None
    search_base: str | None
    search_filter: str | None
    group_search_base: str | None
    username_attribute: str | None
    display_name_attribute: str | None
    membership_mode: str
    nested_groups: bool
    admin_group_dn: str | None
    admin_group_filter: str | None
    operator_group_dn: str | None
    operator_group_filter: str | None
    viewer_group_dn: str | None
    viewer_group_filter: str | None


def load_settings(session: Session) -> LdapSettingsView:
    """Return the current settings as a display-safe view (masking the password).

    A missing singleton row yields an all-default view, so the settings page
    renders cleanly on a fresh install with nothing configured yet.
    """
    row = session.get(LdapSettings, _ROW_ID)
    if row is None:
        return LdapSettingsView()
    has_password = bool(row.bind_password)
    return LdapSettingsView(
        enabled=bool(row.enabled),
        server_urls=_as_str_list(row.server_urls),
        tls_mode=row.tls_mode or "none",
        tls_ca_cert_path=row.tls_ca_cert_path or None,
        bind_dn=row.bind_dn or "",
        bind_password=MASK_SENTINEL if has_password else "",
        bind_password_set=has_password,
        search_base=row.search_base or "",
        search_filter=row.search_filter or "",
        group_search_base=row.group_search_base or "",
        username_attribute=row.username_attribute or "",
        display_name_attribute=row.display_name_attribute or "",
        membership_mode=row.membership_mode or "memberof",
        nested_groups=bool(row.nested_groups),
        admin_group_dn=row.admin_group_dn or "",
        admin_group_filter=row.admin_group_filter or "",
        operator_group_dn=row.operator_group_dn or "",
        operator_group_filter=row.operator_group_filter or "",
        viewer_group_dn=row.viewer_group_dn or "",
        viewer_group_filter=row.viewer_group_filter or "",
    )


def update_settings(session: Session, update: LdapSettingsUpdate) -> LdapSettingsView:
    """Apply an update to the singleton row (creating it on first write).

    Implements the masked write-back rule for the bind password: the stored
    secret is preserved unless the submitted value is genuinely new. Returns the
    refreshed, masked view. The caller's surrounding transaction commits.
    """
    row = session.get(LdapSettings, _ROW_ID)
    if row is None:
        row = LdapSettings(id=_ROW_ID)
        session.add(row)

    row.enabled = bool(update.enabled)
    row.server_urls = [u for u in update.server_urls if u]
    row.tls_mode = update.tls_mode if update.tls_mode in _TLS_MODES else "none"
    # Non-secret: stored verbatim with empty string normalised to NULL. No masking
    # or encryption (contrast the bind_password write-back rule below).
    row.tls_ca_cert_path = update.tls_ca_cert_path or None
    row.bind_dn = update.bind_dn or None
    row.bind_password = _resolve_password(update.bind_password, row.bind_password)
    row.search_base = update.search_base or None
    row.search_filter = update.search_filter or None
    row.group_search_base = update.group_search_base or None
    row.username_attribute = update.username_attribute or None
    row.display_name_attribute = update.display_name_attribute or None
    row.membership_mode = (
        update.membership_mode
        if update.membership_mode in _MEMBERSHIP_MODES
        else "memberof"
    )
    row.nested_groups = bool(update.nested_groups)
    row.admin_group_dn = update.admin_group_dn or None
    row.admin_group_filter = update.admin_group_filter or None
    row.operator_group_dn = update.operator_group_dn or None
    row.operator_group_filter = update.operator_group_filter or None
    row.viewer_group_dn = update.viewer_group_dn or None
    row.viewer_group_filter = update.viewer_group_filter or None

    session.flush()
    return load_settings(session)


def _resolve_password(submitted: str | None, stored: str | None) -> str | None:
    """Return the password to persist, honouring the masked write-back rule.

    A blank submission or the mask sentinel means "the admin did not type a new
    password" -- keep whatever is stored (already-encrypted ciphertext) untouched.
    Any other value is a genuine change and is encrypted before storage. Only the
    new-value branch encrypts, so an unchanged secret is never double-wrapped.
    """
    if submitted is None or submitted == "" or submitted == MASK_SENTINEL:
        return stored
    return encrypt_secret(submitted)


def resolve_bind_password(session: Session) -> str | None:
    """Return the decrypted stored bind password for the directory bind, or None.

    This is the decrypt-at-use seam the connector calls: the display path
    (:func:`load_settings`) only ever returns the mask sentinel, never plaintext.
    A legacy plaintext password passes through unchanged. Returns ``None`` when no
    row or no password is stored. The result is never logged.
    """
    row = session.get(LdapSettings, _ROW_ID)
    if row is None or not row.bind_password:
        return None
    return decrypt_secret(row.bind_password)


def _as_str_list(value: object) -> list[str]:
    """Coerce a stored JSON value into a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]
