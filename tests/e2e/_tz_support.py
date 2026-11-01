"""Pin the signed-in user's display time zone in a browser test (#70 F-R5, #110).

Since WP-70-H a naive ``datetime-local`` value is read in the viewer's stored
zone, and the browser shim fills an empty zone from the browser's own zone in
the background. A browser test that types a wall-clock time must therefore set
the zone itself, or its result would depend on the host's zone (this Mac is
America/New_York; CI is UTC) and on whether the shim's POST had landed yet.

``set_viewer_zone`` stores the zone the way the Preferences page does
(``source=manual``), which wins over any shim POST in either order, then
reloads so the page renders in it. ``local_input`` gives the ``datetime-local``
string for a fixed UTC instant in that zone, computed in Python, so tests stay
independent of today's date and of the browser's zone.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime

_POST_ZONE_JS = """async (tz) => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    const r = await fetch('/account/timezone', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded',
                  'X-CSRF-Token': meta ? meta.content : ''},
        body: 'source=manual&timezone=' + encodeURIComponent(tz)
    });
    return r.status;
}"""


def set_viewer_zone(page: object, tz: str) -> None:
    """Store ``tz`` (``""`` clears it) as the signed-in user's zone; reload.

    The page must already be an authenticated app page (it reads the CSRF meta).
    """
    status = page.evaluate(_POST_ZONE_JS, tz)  # type: ignore[attr-defined]
    assert status == 204, f"setting the viewer zone to {tz!r} returned {status}"
    page.reload(wait_until="domcontentloaded")  # type: ignore[attr-defined]
    if tz:
        page.wait_for_function(  # type: ignore[attr-defined]
            "(tz) => window.tlmViewerTz && window.tlmViewerTz() === tz", arg=tz
        )


def local_input(instant: datetime, tz: str) -> str:
    """``instant`` (aware) as a ``datetime-local`` value in zone ``tz``."""
    return instant.astimezone(zoneinfo.ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def utc(*args: int) -> datetime:
    """A UTC instant, e.g. ``utc(2026, 2, 1, 0, 20)``."""
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]
