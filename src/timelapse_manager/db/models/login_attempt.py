"""Login-attempt log: the durable backing store for brute-force throttling.

Every failed (and every successful) login is recorded as a row so the throttle
decision survives a process restart and cannot be reset by clearing in-memory
state. The throttle counts the failures in a sliding window, per source IP and
per *submitted* username string.

Deliberately there is **no foreign key on ``username``** (nor on ``ip``): the
non-enumerating throttle design counts the submitted username string whether or
not such an account exists, so the row must be writable for an unknown user. A
foreign key would both reject those rows and leak account existence. The table
therefore carries no relationships and is pruned purely by the sliding window.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class LoginAttempt(Base):
    """One recorded login attempt, used only by the brute-force throttle.

    A failure inserts a row with ``succeeded`` false; a success inserts one with
    ``succeeded`` true. The throttle counts the failures recorded *after* the most
    recent success within the configured window, so a genuine login clears the
    earlier typos without deleting the audit trail. Rows older than the window are
    pruned on the write path.
    """

    __tablename__ = "login_attempt"
    __table_args__ = (
        Index("ix_login_attempt_ip_attempted_at", "ip", "attempted_at"),
        Index("ix_login_attempt_username_attempted_at", "username", "attempted_at"),
        # ``attempted_at`` leads its own index so the sliding-window prune -- a
        # ``DELETE WHERE attempted_at < cutoff`` on every login write -- is an
        # index range scan rather than a full-table scan as the table grows
        # under a burst of attempts.
        Index("ix_login_attempt_attempted_at", "attempted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The throttling key for the source: the effective client IP resolved with
    # reverse-proxy trust in mind (never the raw proxy peer).
    ip: Mapped[str] = mapped_column(String, nullable=False)
    # The username string exactly as submitted -- never resolved against the
    # account table, so recording it reveals nothing about whether it exists.
    username: Mapped[str] = mapped_column(String, nullable=False)
    # When the attempt was made (naive UTC, matching the other datetime columns).
    attempted_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    # Whether the attempt authenticated. Failures drive the throttle; a success
    # row marks the point past which earlier failures no longer count.
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
