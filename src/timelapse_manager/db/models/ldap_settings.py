"""LDAP settings: a single-row table holding directory integration config."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

_tls_mode_enum = Enum(
    "none",
    "ldaps",
    "starttls",
    name="ldap_tls_mode",
    native_enum=False,
)
_membership_mode_enum = Enum(
    "memberof",
    "group_search",
    name="ldap_membership_mode",
    native_enum=False,
)


class LdapSettings(TimestampMixin, Base):
    """Directory integration configuration.

    A single-row table: the primary key is constrained to ``1`` so there is at
    most one settings record. The ``bind_password`` column is encrypted at its
    persistence boundary with the at-rest helpers (``encrypt_secret`` /
    ``decrypt_secret`` in :mod:`timelapse_manager.security.crypto`, versioned
    ``enc:v1:`` prefix, legacy plaintext read transparently). The encrypt-on-write
    / decrypt-at-use / mask-on-read seam lives in
    :mod:`timelapse_manager.security.ldap_settings_service`; the bind password is
    never logged.

    Three configuration axes are deliberately kept orthogonal:

    * ``enabled`` -- the master switch. When false, the directory-auth path is
      off entirely and the connector refuses to run.
    * ``membership_mode`` -- *how* a user's groups are discovered: ``"memberof"``
      reads the user entry's ``memberOf`` attribute; ``"group_search"`` searches
      group entries whose member attribute references the user's DN.
    * ``nested_groups`` -- *whether* group evaluation is transitive (recursive,
      following nested/parent groups) or direct only. This is the recursion
      toggle; it is independent of ``membership_mode``.

    ``search_base`` / ``search_filter`` scope the *user* lookup (find the account
    by ``username_attribute``); the per-role ``*_group_dn`` values are the explicit
    group identities a matched membership is compared against. ``group_search_base``
    scopes the group lookup under ``membership_mode="group_search"`` (groups often
    live in a different subtree from users); when unset it falls back to the
    directory suffix of ``search_base``.
    """

    __tablename__ = "ldap_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ldap_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    server_urls: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    tls_mode: Mapped[str] = mapped_column(
        _tls_mode_enum, nullable=False, default="none"
    )
    # Optional path to a CA-certificate (PEM) trust anchor used to validate the
    # directory's LDAPS / StartTLS certificate against a private or internal CA,
    # without modifying the host OS trust store. Non-secret plain config (a
    # filesystem path, not a credential): it is never masked or encrypted. When
    # unset, validation falls back to the platform trust store (and OpenSSL's
    # ``SSL_CERT_FILE``). Validation always stays ``CERT_REQUIRED``; there is no
    # skip-verification option.
    tls_ca_cert_path: Mapped[str | None] = mapped_column(String, nullable=True)
    bind_dn: Mapped[str | None] = mapped_column(String, nullable=True)
    bind_password: Mapped[str | None] = mapped_column(String, nullable=True)
    search_base: Mapped[str | None] = mapped_column(String, nullable=True)
    search_filter: Mapped[str | None] = mapped_column(String, nullable=True)
    group_search_base: Mapped[str | None] = mapped_column(String, nullable=True)
    username_attribute: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name_attribute: Mapped[str | None] = mapped_column(String, nullable=True)
    membership_mode: Mapped[str] = mapped_column(
        _membership_mode_enum, nullable=False, default="memberof"
    )
    nested_groups: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    admin_group_dn: Mapped[str | None] = mapped_column(String, nullable=True)
    admin_group_filter: Mapped[str | None] = mapped_column(String, nullable=True)
    operator_group_dn: Mapped[str | None] = mapped_column(String, nullable=True)
    operator_group_filter: Mapped[str | None] = mapped_column(String, nullable=True)
    viewer_group_dn: Mapped[str | None] = mapped_column(String, nullable=True)
    viewer_group_filter: Mapped[str | None] = mapped_column(String, nullable=True)
