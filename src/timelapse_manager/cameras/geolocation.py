"""Resolve a camera's geolocation, honouring a manual operator override.

Precedence is defined in one place here so it cannot drift:

1. If the camera record carries a *manual* override
   (``geolocation_source == "manual"`` with both coordinates set), that wins
   unconditionally -- an operator has deliberately pinned the location.
2. Otherwise, ask the adapter for a device-reported location.
3. If neither is available, return None.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import CameraAdapter, GeoLocation

logger = logging.getLogger(__name__)


def manual_override(camera: Any) -> GeoLocation | None:
    """Return a manual :class:`GeoLocation` from a camera record, else None.

    Requires ``geolocation_source == "manual"`` and both coordinates present.
    """
    source = getattr(camera, "geolocation_source", None)
    if source != "manual":
        return None
    latitude = getattr(camera, "geolocation_latitude", None)
    longitude = getattr(camera, "geolocation_longitude", None)
    if latitude is None or longitude is None:
        return None
    return GeoLocation(
        latitude=float(latitude), longitude=float(longitude), source="manual"
    )


async def get_camera_geolocation(
    adapter: CameraAdapter, camera: Any | None = None
) -> GeoLocation | None:
    """Resolve a camera's location, manual override taking precedence.

    :param adapter: the adapter used to query a device-reported location.
    :param camera: the camera record carrying any manual override; when omitted,
        only the device-reported location is considered.
    """
    if camera is not None:
        override = manual_override(camera)
        if override is not None:
            return override
    try:
        return await adapter.get_geolocation()
    except Exception as exc:
        # Geolocation is strictly best-effort metadata; never let a lookup
        # failure (network, parsing, unsupported op) propagate to the caller.
        logger.debug("device geolocation lookup failed: %s", exc)
        return None
