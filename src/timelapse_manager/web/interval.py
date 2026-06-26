"""Capture-interval value/unit parsing and display decomposition.

The canonical storage for a capture interval is a whole number of seconds. The
web form, however, lets an operator enter a number plus a unit (e.g. "5
minutes") which is friendlier than typing raw seconds. This module owns the
single conversion table shared by the form handlers and their tests so the
factors never drift between call sites.

The ``months`` factor is a fixed 30-day approximation. It exists for coarse,
long-running campaigns where "3 months" is a convenient label, not an exact
calendar span. As a consequence, a stored value that is a clean multiple of
weeks but not of 30-day months decomposes back to weeks: e.g. 28 days
round-trips to "4 weeks", which is expected.
"""

from __future__ import annotations

# Seconds per unit. Order is irrelevant here; ``decompose_seconds`` defines the
# largest-first preference explicitly.
UNIT_FACTORS: dict[str, int] = {
    "seconds": 1,
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
    "weeks": 604800,
    # 30-day approximation -- see the module docstring.
    "months": 2592000,
}

# Largest unit first: ``decompose_seconds`` walks this to pick the coarsest unit
# that divides the stored seconds evenly.
_DECOMPOSE_ORDER: tuple[str, ...] = (
    "months",
    "weeks",
    "days",
    "hours",
    "minutes",
    "seconds",
)

# Default for a project whose stored interval is unset (the column is nullable).
# A blank field is not a useful prefill, so fall back to a sensible human value.
_DEFAULT_VALUE = 1
_DEFAULT_UNIT = "minutes"


def parse_interval_to_seconds(
    value_raw: str, unit_raw: str
) -> tuple[int | None, str | None]:
    """Convert a submitted value+unit into a whole number of seconds.

    Returns ``(seconds, None)`` on success, or ``(None, message)`` on any
    invalid input, mirroring the tuple convention used by the other form-field
    parsers. The value must be a positive integer (>= 1), the unit must be one
    of the known units, and the resulting seconds must be >= 1.
    """
    try:
        value = int(value_raw)
    except (TypeError, ValueError):
        return None, "Capture interval must be a whole number."
    if value < 1:
        return None, "Capture interval must be at least 1."
    factor = UNIT_FACTORS.get(unit_raw)
    if factor is None:
        return None, "Capture interval unit is not recognised."
    seconds = value * factor
    if seconds < 1:
        return None, "Capture interval must be at least 1 second."
    return seconds, None


def decompose_seconds(seconds: int | None) -> tuple[int, str]:
    """Decompose stored seconds into a ``(value, unit)`` pair for form prefill.

    Picks the largest unit that divides ``seconds`` evenly, checking months,
    weeks, days, hours, minutes, then seconds. ``None`` (no stored interval)
    falls back to a sensible default. See the module docstring for the
    30-day-month round-trip note.
    """
    if seconds is None or seconds < 1:
        return _DEFAULT_VALUE, _DEFAULT_UNIT
    for unit in _DECOMPOSE_ORDER:
        factor = UNIT_FACTORS[unit]
        if seconds % factor == 0:
            return seconds // factor, unit
    # Unreachable: seconds=1 always divides evenly, but be explicit.
    return seconds, "seconds"
