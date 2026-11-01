"""Secrets key record: a single-row table naming the key that encrypted the data.

At startup the application compares the encryption key it loaded with the key
recorded here (#67 S8), so a missing, restored or rotated key is reported
clearly instead of leaving stored credentials silently undecryptable.
"""

from __future__ import annotations

import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SecretsMeta(Base):
    """Which key provider holds the at-rest key, and the key's fingerprint.

    A single-row table: the primary key is constrained to ``1``. The fingerprint
    is a keyed hash of the key (see :func:`timelapse_manager.security.keystore
    .key_fingerprint`); it cannot be reversed to the key. No secret is stored.
    """

    __tablename__ = "secrets_meta"
    __table_args__ = (CheckConstraint("id = 1", name="ck_secrets_meta_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    rotated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
