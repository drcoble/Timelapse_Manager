"""Default camera credentials: a single-row table holding a fallback login.

A global username/password used when connecting to a camera that carries no
credentials of its own and is configured to inherit the default. Like the LDAP
settings row, this is a singleton (its primary key is constrained to ``1``).
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class CameraDefaultCredentials(TimestampMixin, Base):
    """A global fallback username/password for credential-free cameras.

    A single-row table: the primary key is constrained to ``1`` so there is at
    most one record. The ``password`` column is encrypted at its persistence
    boundary with the at-rest helpers (``encrypt_secret`` / ``decrypt_secret`` in
    :mod:`timelapse_manager.security.crypto`, versioned ``enc:v1:`` prefix, legacy
    plaintext read transparently). The encrypt-on-write / decrypt-at-use /
    mask-on-read seam lives in
    :mod:`timelapse_manager.security.camera_defaults_service`; the password is
    never logged.

    ``enabled`` is the master switch: when false the fallback is off entirely and
    a credential-free camera stays open regardless of its inherit flag. The
    fallback is also opt-in per camera (the ``camera.credentials_inherit_default``
    flag), so enabling this alone never changes how an existing camera connects.
    """

    __tablename__ = "camera_default_credentials"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_camera_default_credentials_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    password: Mapped[str | None] = mapped_column(String, nullable=True)
