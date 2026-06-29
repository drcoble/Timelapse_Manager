"""Notification settings: a single-row table for outbound alert configuration."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, CheckConstraint, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

_smtp_security_enum = Enum(
    "none",
    "tls",
    "starttls",
    name="notification_smtp_security",
    native_enum=False,
)


class NotificationSettings(TimestampMixin, Base):
    """Email/webhook notification configuration and routing rules.

    A single-row table: the primary key is constrained to ``1``. The SMTP
    password column is present but not yet protected at rest.
    """

    __tablename__ = "notification_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_notification_settings_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    enabled_channels: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    smtp_server: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_security: Mapped[str] = mapped_column(
        _smtp_security_enum, nullable=False, default="none"
    )
    smtp_username: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_password: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_from_address: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_recipients: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    webhook_urls: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    routing_rules: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
