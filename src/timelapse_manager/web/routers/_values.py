"""Plain value objects an ``async`` route reads after its first threadpool hop.

An ORM row read on the event loop can refresh an expired attribute with a
synchronous ``SELECT`` (#66 L3). The route therefore copies the few columns it
needs into one of these frozen dataclasses *inside* a threadpool helper and reads
only the copy afterwards. None of these is an ORM object, so none can touch the
database from the loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ...db.models import Camera, User


@dataclass(frozen=True)
class UserView:
    """The columns of a signed-in ``User`` a route reads after its first hop.

    Build it with :meth:`of` in a threadpool helper (``run_in_threadpool``); the
    auth dependency's return type is unchanged.
    """

    id: int
    username: str
    role: str
    viewer_timezone: str | None

    @classmethod
    def of(cls, user: User) -> UserView:
        """Copy *user*'s columns. Synchronous: it may refresh expired attributes."""
        return cls(
            id=int(user.id),
            username=str(user.username),
            role=str(user.role),
            viewer_timezone=user.viewer_timezone,
        )


class Geolocated(Protocol):
    """What the solar-anchor check reads of a camera: its coordinates."""

    @property
    def geolocation_latitude(self) -> float | None:
        """Latitude in degrees, or ``None`` when the camera has no location."""

    @property
    def geolocation_longitude(self) -> float | None:
        """Longitude in degrees, or ``None`` when the camera has no location."""


@dataclass(frozen=True)
class CameraFacts:
    """A camera's protocol and coordinates, copied while the row is attached."""

    protocol: str | None
    geolocation_latitude: float | None
    geolocation_longitude: float | None

    @classmethod
    def of(cls, camera: Camera) -> CameraFacts:
        """Copy *camera*'s columns. Synchronous: it may refresh expired attributes."""
        return cls(
            protocol=camera.protocol,
            geolocation_latitude=camera.geolocation_latitude,
            geolocation_longitude=camera.geolocation_longitude,
        )


def load_camera_with_facts(
    db: Session, camera_id: int
) -> tuple[Camera | None, CameraFacts | None]:
    """Fetch a camera and copy its facts in one synchronous (threadpool) step.

    The ORM row is returned only to be handed on, unread, to code that expects
    one (the camera probes); the route reads the :class:`CameraFacts` copy.
    """
    from ...db.models import Camera

    camera = db.get(Camera, camera_id)
    if camera is None:
        return None, None
    return camera, CameraFacts.of(camera)
