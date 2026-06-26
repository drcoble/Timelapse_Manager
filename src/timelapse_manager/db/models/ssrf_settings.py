"""SSRF settings: a single-row table holding the admin-managed subnet allow-list."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, CheckConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class SsrfSettings(TimestampMixin, Base):
    """Admin-managed SSRF opt-in allow-list.

    A single-row table: the primary key is constrained to ``1`` so there is at
    most one settings record. The one column, ``allowed_private_subnets``, is a
    JSON list of CIDR strings an administrator has opted into from the web UI.

    The stored list is *additive* to any subnets supplied by configuration or the
    environment: at startup and on every save the two are merged into the running
    SSRF policy (see
    :mod:`timelapse_manager.security.ssrf_settings_service`). It only ever widens
    the camera/scan surface; it never relaxes the always-blocked ranges
    (loopback/link-local/cloud-metadata) and never applies to outbound webhooks.
    No secrets are stored here, so the column is neither encrypted nor masked.
    """

    __tablename__ = "ssrf_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ssrf_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    allowed_private_subnets: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True
    )
