"""User entity: a local or directory-backed account."""

from __future__ import annotations

from sqlalchemy import Boolean, Enum, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from ...usernames import username_key
from .base import Base, TimestampMixin

_auth_source_enum = Enum(
    "local",
    "ldap",
    name="user_auth_source",
    native_enum=False,
)
_role_enum = Enum(
    "admin",
    "operator",
    "viewer",
    name="user_role",
    native_enum=False,
)


class User(TimestampMixin, Base):
    """An account that can sign in to the application.

    Local users carry a password hash; directory (LDAP) users do not and have
    their role derived from group membership at login. The password column is
    present but is not yet populated with a securely derived hash; that is
    introduced in a later phase.
    """

    __tablename__ = "user"
    # AUTOINCREMENT (migration 031, #17): a deleted user's id is never reused,
    # so a new user does not inherit the old one's references.
    __table_args__ = (
        Index("ix_user_username_key", "username_key", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # Case-insensitive identity (#4): stripped, NFKC, casefolded ``username``,
    # unique, maintained by :meth:`_sync_username_key`. Look users up by this.
    username_key: Mapped[str] = mapped_column(String, nullable=False)
    auth_source: Mapped[str] = mapped_column(
        _auth_source_enum, nullable=False, default="local"
    )
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(_role_enum, nullable=False, default="viewer")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Per-user display preferences. theme_preference controls the colour scheme
    # ("light", "dark", or "system" — follow the OS); viewer_timezone is an IANA
    # timezone name used to localise displayed timestamps (None = show UTC).
    theme_preference: Mapped[str] = mapped_column(
        String, nullable=False, default="system"
    )
    viewer_timezone: Mapped[str | None] = mapped_column(String, nullable=True)

    @validates("username")
    def _sync_username_key(self, _key: str, value: str) -> str:
        """Keep ``username_key`` in step with every assignment to ``username``."""
        self.username_key = username_key(value)
        return value
