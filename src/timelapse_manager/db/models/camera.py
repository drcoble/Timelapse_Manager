"""Camera entity: a network IP camera the application can capture from."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Enum, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

# Stored without a native SQLite enum type; rendered as a CHECK constraint so the
# allowed values are enforced at the database level and remain portable.
_protocol_enum = Enum(
    "onvif",
    "rtsp",
    "http",
    "vapix",
    name="camera_protocol",
    native_enum=False,
)
_geolocation_source_enum = Enum(
    "camera",
    "manual",
    name="camera_geolocation_source",
    native_enum=False,
)
_device_hostname_source_enum = Enum(
    "camera",
    "manual",
    name="camera_device_hostname_source",
    native_enum=False,
)


class Camera(TimestampMixin, Base):
    """A configured camera and how to reach it.

    Credentials are stored as a JSON document. At-rest encryption helpers for
    this document exist (``encrypt_credentials`` / ``decrypt_credentials`` in
    :mod:`timelapse_manager.security.crypto`): they encrypt the secret-bearing
    fields with a versioned ``enc:v1:`` prefix and read legacy plaintext
    transparently. The camera write/read paths should apply those helpers at
    their persistence boundary; until they do, these credentials remain plaintext
    at rest.
    """

    __tablename__ = "camera"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    protocol: Mapped[str | None] = mapped_column(_protocol_enum, nullable=True)
    credentials: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Opt-in fallback to the global default credentials when this camera carries
    # no credentials of its own. New cameras created through the application
    # default to inheriting (ORM default ``True``); rows that predate this column
    # default off at the database level so an upgrade never changes how an
    # existing camera connects.
    credentials_inherit_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    snapshot_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    stream_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    default_resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    geolocation_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    geolocation_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    geolocation_source: Mapped[str | None] = mapped_column(
        _geolocation_source_enum, nullable=True
    )
    # The camera's network hostname. ``device_hostname`` is the most recent value
    # known (device-reported or operator-set); ``device_hostname_source`` records
    # which, mirroring the geolocation pair above. Both are nullable: a camera
    # whose hostname has never been resolved or set carries neither.
    device_hostname: Mapped[str | None] = mapped_column(String, nullable=True)
    device_hostname_source: Mapped[str | None] = mapped_column(
        _device_hostname_source_enum, nullable=True
    )
