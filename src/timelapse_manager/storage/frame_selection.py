"""Range-descriptor resolution: turning a "select all in this time range" gesture
into a concrete set of frame ids without enumerating thousands of them on the
client.

A :class:`RangeDescriptor` is the single object the browser builds when the user
drag-selects a span on the timeline (or escalates to "select all in range" /
"select all in project"). It is resolved server-side to a frame-id set that the
bulk endpoints act on, so a selection of tens of thousands of frames travels as
one small descriptor plus an explicit list of any tiles the user individually
deselected afterwards.

This module is a leaf: it imports only the models, the session, and the
timestamp normaliser from the sibling lifecycle module. It imports nothing from
the web layer, so both the router (which parses a request into a descriptor) and
storage callers can depend on it without an import cycle.

Two genuinely different scopes, with two different WHERE clauses:

* ``in_range`` -- frames whose ``capture_timestamp`` falls in the given range.
  This scope adds ``capture_timestamp IS NOT NULL``: a frame with no capture time
  has no place on the time axis, so a time-range selection never includes it.
* ``in_project`` -- the whole campaign, **including** frames with a null capture
  timestamp. There is no time predicate at all.

Both scopes hide soft-deleted frames unless ``filters.include_deleted`` is set,
mirroring the grid's Show-Deleted toggle. There is deliberately no
"include excluded" filter: render-excluded frames stay visible in the browser, so
a select-all naturally includes them. After the scope query resolves, the
``deselected_ids`` (tiles the user un-checked inside a select-all) are subtracted.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Frame
from .frames import _to_naive_utc

# A deselection list is a correction inside a select-all, not a primary
# selection; a list larger than this signals client misuse and is rejected.
_MAX_DESELECTED_IDS = 10_000

_VALID_SCOPES = ("in_range", "in_project")


class DescriptorError(ValueError):
    """Raised when a request body cannot be parsed into a valid descriptor."""


@dataclass(frozen=True)
class TimeRange:
    """An inclusive capture-time window; either bound may be open (``None``).

    ``time_from`` / ``time_to`` are stored as naive UTC datetimes (matching the
    naive-UTC ``capture_timestamp`` column). The wire shape names them ``from`` /
    ``to``; ``from`` is a Python keyword, so the field is renamed here and mapped
    at parse time.
    """

    time_from: datetime.datetime | None = None
    time_to: datetime.datetime | None = None


@dataclass(frozen=True)
class DescriptorFilters:
    """The descriptor's only filter: whether soft-deleted frames are in scope."""

    include_deleted: bool = False


@dataclass(frozen=True)
class RangeDescriptor:
    """A resolvable description of a frame selection.

    ``scope`` is ``in_range`` (frames in ``time_range``) or ``in_project`` (the
    whole campaign, null-timestamp frames included). ``time_range`` is required
    for ``in_range`` and ignored for ``in_project``. ``deselected_ids`` are the
    ids subtracted after the scope query resolves -- the frames the user
    un-checked inside a select-all.
    """

    scope: str
    project_id: int
    time_range: TimeRange | None = None
    filters: DescriptorFilters = field(default_factory=DescriptorFilters)
    deselected_ids: list[int] = field(default_factory=list)


def _base_predicate(descriptor: RangeDescriptor):  # type: ignore[no-untyped-def]
    """Build the shared WHERE-applied SELECT skeleton for a descriptor.

    Returns a ``select`` over ``Frame`` with project scoping, the active-only
    filter (unless ``include_deleted``), and -- for ``in_range`` only -- the
    ``capture_timestamp IS NOT NULL`` plus the open-or-closed range bounds. The
    ``in_project`` scope adds no time predicate, so its result includes
    null-timestamp frames. The ``deselected_ids`` subtraction is applied by the
    callers, since ``count`` and ``resolve`` express it differently (a SQL
    ``NOT IN`` versus a Python set difference).
    """
    stmt = select(Frame.id).where(Frame.project_id == descriptor.project_id)
    if not descriptor.filters.include_deleted:
        stmt = stmt.where(Frame.lifecycle_state == "active")
    if descriptor.scope == "in_range":
        stmt = stmt.where(Frame.capture_timestamp.is_not(None))
        time_range = descriptor.time_range or TimeRange()
        if time_range.time_from is not None:
            stmt = stmt.where(
                Frame.capture_timestamp >= _to_naive_utc(time_range.time_from)
            )
        if time_range.time_to is not None:
            stmt = stmt.where(
                Frame.capture_timestamp <= _to_naive_utc(time_range.time_to)
            )
    return stmt


def resolve(session: Session, descriptor: RangeDescriptor) -> set[int]:
    """Resolve a descriptor to the concrete set of frame ids it selects.

    Runs the scope query (see :func:`_base_predicate`) and subtracts
    ``deselected_ids`` as a Python set difference, so an id in the deselection
    list that is not actually in range is simply a no-op.
    """
    ids = set(session.execute(_base_predicate(descriptor)).scalars().all())
    return ids - set(descriptor.deselected_ids)


def count(session: Session, descriptor: RangeDescriptor) -> int:
    """Return the size of the resolved set without materialising the id list.

    The count drives the honest "≈N" estimate the escalation banner and the
    selection bar show before any mutation runs. It applies the same predicate as
    :func:`resolve`, subtracting only the deselected ids that actually fall in
    range -- expressed here as a SQL ``NOT IN`` so no large id list is built just
    to count. ``count`` and ``len(resolve(...))`` are therefore always equal.
    """
    stmt = _base_predicate(descriptor)
    if descriptor.deselected_ids:
        # Guarded: NOT IN over an empty list is a degenerate predicate (and warns
        # in SQLAlchemy), so only add it when there is something to subtract.
        stmt = stmt.where(Frame.id.not_in(descriptor.deselected_ids))
    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    return int(total or 0)


def materialize(session: Session, descriptor: RangeDescriptor) -> list[int]:
    """Resolve a descriptor to a sorted, concrete list of frame ids.

    The list form an explicit-id operation (e.g. a bulk timestamp offset) needs:
    it pins the exact frames at resolution time so a later replay acts on the same
    set even if new frames have since arrived. Sorted for a deterministic order.
    """
    return sorted(resolve(session, descriptor))


def _parse_iso_datetime(value: object, *, field_name: str) -> datetime.datetime | None:
    """Parse an optional ISO-8601 bound, or ``None`` for an absent/null one.

    A non-string, non-null value or an unparseable string is a hard error: the
    bound came from the client and a malformed one must not silently widen the
    range. The trailing ``Z`` (UTC) the client sends is accepted natively.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DescriptorError(f"{field_name} must be an ISO-8601 datetime string")
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise DescriptorError(f"{field_name} is not a valid ISO-8601 datetime") from exc


def _parse_deselected_ids(value: object) -> list[int]:
    """Parse and bound-check the ``deselected_ids`` list from a request body."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise DescriptorError("deselected_ids must be a list of integers")
    if len(value) > _MAX_DESELECTED_IDS:
        raise DescriptorError(
            f"deselected_ids exceeds the maximum of {_MAX_DESELECTED_IDS}"
        )
    out: list[int] = []
    for item in value:
        # JSON booleans are ints in Python; reject them explicitly so a stray
        # ``true`` is not silently read as the id ``1``.
        if isinstance(item, bool) or not isinstance(item, int):
            raise DescriptorError("deselected_ids must contain only integers")
        out.append(item)
    return out


def parse_descriptor(body: dict[str, object]) -> RangeDescriptor:
    """Build a :class:`RangeDescriptor` from a decoded request body, validating it.

    ``body`` is the already-decoded mapping (the router does the JSON decode and
    catches a decode error before calling here, so the leaf never sees a request
    or a raw string). Validation is bounded and the messages are explicit:

    * ``scope`` must be one of the known scopes;
    * ``project_id`` must be a positive integer;
    * ``in_range`` requires a ``time_range``, and ``from`` must not be after
      ``to`` (either bound may be null = open-ended);
    * ``deselected_ids`` is capped.

    Raises :class:`DescriptorError` on any violation.
    """
    if not isinstance(body, dict):
        raise DescriptorError("descriptor must be an object")

    scope = body.get("scope")
    if scope not in _VALID_SCOPES:
        raise DescriptorError(f"scope must be one of {', '.join(_VALID_SCOPES)}")

    raw_project_id = body.get("project_id")
    if isinstance(raw_project_id, bool) or not isinstance(raw_project_id, int):
        raise DescriptorError("project_id must be an integer")
    project_id = raw_project_id
    if project_id <= 0:
        raise DescriptorError("project_id must be a positive integer")

    time_range: TimeRange | None = None
    if scope == "in_range":
        raw_range = body.get("time_range")
        if raw_range is None:
            raise DescriptorError("scope 'in_range' requires a time_range")
        if not isinstance(raw_range, dict):
            raise DescriptorError("time_range must be an object")
        time_from = _parse_iso_datetime(
            raw_range.get("from"), field_name="time_range.from"
        )
        time_to = _parse_iso_datetime(raw_range.get("to"), field_name="time_range.to")
        if time_from is not None and time_to is not None and time_from > time_to:
            raise DescriptorError("time_range.from must not be after time_range.to")
        time_range = TimeRange(time_from=time_from, time_to=time_to)

    raw_filters = body.get("filters") or {}
    if not isinstance(raw_filters, dict):
        raise DescriptorError("filters must be an object")
    filters = DescriptorFilters(
        include_deleted=bool(raw_filters.get("include_deleted"))
    )

    deselected_ids = _parse_deselected_ids(body.get("deselected_ids"))

    return RangeDescriptor(
        scope=scope,
        project_id=project_id,
        time_range=time_range,
        filters=filters,
        deselected_ids=deselected_ids,
    )
