"""User entity: a local or directory-backed account."""

from __future__ import annotations

from sqlalchemy import Boolean, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
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
