"""Session entity: an authenticated login session for a user."""

from __future__ import annotations

import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Session(Base):
    """A user's login session.

    Carries its own ``created_at`` / lifecycle timestamps rather than the shared
    mixin because the meaningful lifecycle fields here are expiry and
    revocation, not a generic "updated" marker. ``updated_at`` is still kept for
    consistency with the rest of the schema.
    """

    __tablename__ = "session"
    __table_args__ = (
        Index("ix_session_user_id", "user_id"),
        Index("ix_session_token_hash", "token_hash", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 hash (hex) of the raw session token. The raw token lives only in
    # the client cookie; storing just its hash means a database leak does not
    # disclose usable session credentials. Nullable at the column level so the
    # additive migration can run against existing rows; every row this code
    # writes populates it. Unique so a token resolves to at most one session.
    token_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # Per-session secret backing the synchronizer CSRF token. Nullable so the
    # migration is additive and so non-web sessions need not carry one.
    csrf_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    # Last time the session was used to authenticate a request; the idle-timeout
    # clock is measured from here. Distinct from ``last_revalidated_at``, which
    # is reserved for a heavier periodic re-check; idle expiry keys on this.
    last_active: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_revalidated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
