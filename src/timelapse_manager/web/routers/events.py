"""Event-log routes: the operational events view, the admin audit view, and
the live status partial."""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    Response,
)
from sqlalchemy import func, or_
from sqlalchemy.orm import Session as DbSession

from ...db.models import Event
from ...logging import redact_text
from ...monitoring import (
    EventType,
    get_events,
)
from ...monitoring.events import _levels_at_or_above
from .. import dependencies as deps
from ..dependencies import (
    AdminUser,
    CurrentUser,
    DbDep,
    templates,
)
from ._viewmodels import (
    _fmt_dt,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# Audit/security log batch size (keyset continuous scroll). The operational log
# uses its own larger batch size below; the two are deliberately independent so
# the operational stream can grow without changing the audit view's paging.
_EVENTS_PER_PAGE = 50


# Continuous-scroll batch size for the operational event log (keyset). Larger
# than the audit page so fewer scroll round-trips are needed on a busy log.
_OPERATIONAL_EVENTS_PER_PAGE = 75


_EVENT_LEVEL_FLOORS = ("info", "warning", "error", "critical")


_AUDIT_EVENT_TYPES = (
    EventType.AUDIT_CONTROL_ACTION.value,
    EventType.SECURITY_AUTH_EVENT.value,
)


@dataclass(frozen=True)
class _EventView:
    """Display projection of an event row for the log tables.

    The stored message and details are already redacted at write time; this view
    additionally scrubs any URL credentials defensively before display.
    """

    id: int
    level: str
    scope: str
    scope_id: int | None
    event_type: str
    message: str
    timestamp: str | None
    # Raw datetime for timezone-aware display via the localdt template filter.
    timestamp_raw: datetime.datetime | None


def _event_view(event: Event) -> _EventView:
    """Build an event view model, deriving the type from the JSON details."""
    details = event.event_metadata or {}
    event_type = ""
    if isinstance(details, dict):
        raw_type = details.get("type")
        event_type = str(raw_type) if raw_type is not None else ""
    return _EventView(
        id=event.id,
        level=event.level,
        scope=event.scope,
        scope_id=event.scope_id,
        event_type=event_type,
        message=redact_text(event.message or ""),
        timestamp=_fmt_dt(event.timestamp),
        timestamp_raw=event.timestamp,
    )


def _operational_events(
    db: DbSession,
    *,
    scope: str | None,
    level_floor: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Event], int]:
    """Return a page of *operational* events (audit/security excluded) and total.

    The operational log is visible to any authenticated role, so audit and
    security records must be filtered out here (they have their own admin-only
    view). The exclusion is NULL-tolerant: ``json_extract`` yields SQL ``NULL``
    for any row without a ``type`` key (capture/render/storage emits and the
    login/logout/settings audit-helper rows all lack one), and ``NULL NOT IN
    (...)`` is ``NULL`` -- which would silently drop every typeless operational
    row. The explicit ``IS NULL OR NOT IN`` keeps those rows visible while
    excluding the typed audit/security records.
    """
    type_expr = func.json_extract(Event.event_metadata, "$.type")
    query = db.query(Event).filter(
        or_(type_expr.is_(None), type_expr.notin_(_AUDIT_EVENT_TYPES))
    )
    if scope is not None:
        query = query.filter(Event.scope == scope)
    if level_floor is not None:
        query = query.filter(Event.level.in_(_levels_at_or_above(level_floor)))
    total = query.count()
    rows = (
        query.order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(max(1, limit))
        .offset(max(0, offset))
        .all()
    )
    return rows, total


def _parse_levels(level: str | None) -> list[str]:
    """Parse the multi-select ``level`` param into the canonical valid subset.

    The continuous-scroll log filters on *specific* levels (the chips), unlike
    the legacy single floor. ``debug`` is intentionally not offered.
    """
    if not level:
        return []
    requested = {p.strip() for p in level.split(",") if p.strip()}
    return [lv for lv in _EVENT_LEVEL_FLOORS if lv in requested]


def _operational_events_keyset(
    db: DbSession,
    *,
    before_id: int | None,
    levels: list[str],
    q: str | None,
    scope: str | None,
    scope_id: int | None = None,
    limit: int,
) -> list[Event]:
    """Operational events newest-first by ``id`` for keyset pagination.

    ``event.id`` is monotonic and unique, so it is a clean, stable cursor (no
    skip/duplicate as new events arrive at the head, which an offset cannot
    promise). ``before_id`` returns only strictly-older rows (``id < before``).
    ``scope_id`` narrows to a single scoped subject (e.g. one project's events,
    paired with ``scope='project'``). Audit/security records are excluded as in
    :func:`_operational_events`.
    """
    type_expr = func.json_extract(Event.event_metadata, "$.type")
    query = db.query(Event).filter(
        or_(type_expr.is_(None), type_expr.notin_(_AUDIT_EVENT_TYPES))
    )
    if scope:
        query = query.filter(Event.scope == scope)
    if scope_id is not None:
        query = query.filter(Event.scope_id == scope_id)
    if levels:
        query = query.filter(Event.level.in_(levels))
    if q:
        query = query.filter(Event.message.ilike(f"%{q}%"))
    if before_id is not None:
        query = query.filter(Event.id < before_id)
    return query.order_by(Event.id.desc()).limit(max(1, limit)).all()


def _events_batch(
    db: DbSession,
    *,
    before_id: int | None,
    levels: list[str],
    q: str | None,
    scope: str | None,
    limit: int = _OPERATIONAL_EVENTS_PER_PAGE,
) -> tuple[list[_EventView], int | None]:
    """Fetch one keyset batch of operational events + the next cursor."""
    rows = _operational_events_keyset(
        db, before_id=before_id, levels=levels, q=q, scope=scope, limit=limit + 1
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    events = [_event_view(e) for e in rows]
    next_before = rows[-1].id if (rows and has_more) else None
    return events, next_before


def _audit_events_keyset(
    db: DbSession,
    *,
    before_id: int | None,
    q: str | None,
    limit: int,
) -> list[Event]:
    """Audit/security events newest-first by ``id`` for keyset pagination.

    The mirror image of :func:`_operational_events_keyset`: where the operational
    query *excludes* audit/security records, this *includes* only them. The
    ``type`` is read from the JSON details (there is no ``type`` column) and must
    be one of :data:`_AUDIT_EVENT_TYPES` -- so no operational (non-audit) row can
    ever leak into this result. ``event.id`` is a monotonic, unique cursor, so
    ``before_id`` returns only strictly-older rows (``id < before``) with no
    skip/duplicate as new records arrive at the head.

    This helper is intentionally authorization-free; the admin gate lives at the
    route (mirroring the operational helpers), so every caller must be
    admin-gated.
    """
    type_expr = func.json_extract(Event.event_metadata, "$.type")
    query = db.query(Event).filter(type_expr.in_(_AUDIT_EVENT_TYPES))
    if q:
        query = query.filter(Event.message.ilike(f"%{q}%"))
    if before_id is not None:
        query = query.filter(Event.id < before_id)
    return query.order_by(Event.id.desc()).limit(max(1, limit)).all()


def _audit_batch(
    db: DbSession,
    *,
    before_id: int | None,
    q: str | None,
    limit: int = _EVENTS_PER_PAGE,
) -> tuple[list[_EventView], int | None]:
    """Fetch one keyset batch of audit/security events + the next cursor."""
    rows = _audit_events_keyset(db, before_id=before_id, q=q, limit=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    events = [_event_view(e) for e in rows]
    next_before = rows[-1].id if (rows and has_more) else None
    return events, next_before


def _audit_filter_qs(q: str | None) -> str:
    """URL-encoded querystring of the active audit filters, for the sentinel."""
    return urlencode({"q": q}) if q else ""


def _parse_at(at: str | None) -> datetime.datetime | None:
    """Parse the ``?at=`` ISO-8601 jump anchor, or ``None`` if absent/invalid.

    Accepts the ``datetime-local`` form the jump form submits (no offset) as well
    as a full offset-aware ISO string. A malformed value is ignored rather than
    erroring, so a hand-edited URL degrades to the plain newest-first first page.
    """
    if not at:
        return None
    try:
        return datetime.datetime.fromisoformat(at)
    except ValueError:
        return None


def _to_naive_utc(value: datetime.datetime) -> datetime.datetime:
    """Return ``value`` as naive UTC, matching the naive ``Event.timestamp`` column.

    Aware values are converted to UTC and made naive; naive values are assumed to
    already be UTC and returned unchanged, mirroring the storage-layer convention.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.UTC).replace(tzinfo=None)


def _before_id_at_or_before(db: DbSession, anchor: datetime.datetime) -> int | None:
    """Resolve a timestamp anchor to the keyset ``before_id`` for the jump.

    Returns ``anchor_id + 1`` where ``anchor_id`` is the id of the newest event
    whose ``timestamp`` is at-or-before ``anchor`` -- the ``+1`` makes the strict
    ``id < before_id`` keyset *include* the anchor event itself, so the jump lands
    on the event at-or-before the requested instant and pages older from there.

    The resolution is deliberately filter-agnostic: it finds the newest event in
    the whole operational log at-or-before the instant, ignoring the active
    level/scope/search filters. Because the id and timestamp are co-monotonic
    (events are append-only), applying the filters to the *boundary* would skip
    matching rows that fall between a filtered boundary and the true time
    boundary; the filters are applied only to the page that follows.

    Returns ``None`` when no event is at-or-before the anchor (the anchor precedes
    the entire log), so the caller renders an empty window rather than clamping to
    the newest page -- the opposite of a forward (at-or-after) resolver.
    """
    type_expr = func.json_extract(Event.event_metadata, "$.type")
    anchor_id = (
        db.query(Event.id)
        .filter(or_(type_expr.is_(None), type_expr.notin_(_AUDIT_EVENT_TYPES)))
        .filter(Event.timestamp <= _to_naive_utc(anchor))
        .order_by(Event.id.desc())
        .limit(1)
        .scalar()
    )
    return None if anchor_id is None else anchor_id + 1


def _events_filter_qs(levels: list[str], q: str | None, scope: str | None) -> str:
    """URL-encoded querystring of the active filters, for the sentinel link."""
    params: dict[str, str] = {}
    if levels:
        params["level"] = ",".join(levels)
    if q:
        params["q"] = q
    if scope:
        params["scope"] = scope
    return urlencode(params)


@router.get("/events/batch", response_class=HTMLResponse)
def events_batch(
    request: Request,
    db: DbDep,
    user: CurrentUser,
    before: int | None = None,
    level: str | None = None,
    q: str | None = None,
    scope: str | None = None,
) -> Response:
    """Return one continuous-scroll batch of event rows + a fresh sentinel.

    The sentinel is a ``<tr>`` (table-valid) carrying ``hx-trigger="revealed"``
    that swaps itself out for the next (older) batch; the end-cap replaces it at
    the start of the log. All active filters ride the sentinel's cursor URL.
    """
    levels = _parse_levels(level)
    qval = (q or "").strip() or None
    scopeval = (scope or "").strip() or None
    events, next_before = _events_batch(
        db, before_id=before, levels=levels, q=qval, scope=scopeval
    )
    return templates.TemplateResponse(
        request,
        "_partials/events_batch.html",
        deps.base_context(
            request,
            db,
            user,
            events=events,
            next_before=next_before,
            filter_qs=_events_filter_qs(levels, qval, scopeval),
        ),
    )


@router.get("/events", response_class=HTMLResponse)
def events_page(
    request: Request,
    db: DbDep,
    user: CurrentUser,
    before: int | None = None,
    at: str | None = None,
    level: str | None = None,
    q: str | None = None,
    scope: str | None = None,
) -> Response:
    """Render the operational event log (continuous scroll); any role may view.

    Audit and security events are excluded -- they appear only in the admin-only
    audit view -- so a viewer never sees control-action or authentication records
    here. Content is redacted at write time. The sentinel appends older events
    (keyset by id); ``?before=`` is the no-JS pagination fallback.

    A request may carry ``?at=<iso8601>`` to jump the log to the events at-or-
    before that instant: the anchor is resolved to a starting ``before`` cursor
    and the page continues older from there. An HTMX request (the date-jump form)
    gets just the events-batch fragment to swap into the list; a plain GET (no-JS)
    gets the full page windowed at ``at``. The active filters ride the jump and
    its continuation; ``at`` itself is single-shot (not carried by the sentinel).
    """
    levels = _parse_levels(level)
    qval = (q or "").strip() or None
    scopeval = (scope or "").strip() or None

    events: list[_EventView]
    next_before: int | None
    anchor = _parse_at(at)
    if anchor is not None:
        before_at = _before_id_at_or_before(db, anchor)
        if before_at is None:
            # The anchor precedes the whole log -> an empty window (not the newest
            # page); the backward resolver clamps to "nothing older exists here".
            events, next_before = [], None
        else:
            events, next_before = _events_batch(
                db, before_id=before_at, levels=levels, q=qval, scope=scopeval
            )
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "_partials/events_batch.html",
                deps.base_context(
                    request,
                    db,
                    user,
                    events=events,
                    next_before=next_before,
                    filter_qs=_events_filter_qs(levels, qval, scopeval),
                ),
            )
    else:
        events, next_before = _events_batch(
            db, before_id=before, levels=levels, q=qval, scope=scopeval
        )

    return templates.TemplateResponse(
        request,
        "events.html",
        deps.base_context(
            request,
            db,
            user,
            events=events,
            next_before=next_before,
            filter_qs=_events_filter_qs(levels, qval, scopeval),
            selected_levels=levels,
            search_q=qval or "",
            scope=scopeval or "",
            level_options=_EVENT_LEVEL_FLOORS,
            audit_view=False,
        ),
    )


def _count_operational_events_newer_than(
    db: DbSession,
    *,
    after_id: int,
    levels: list[str],
    q: str | None,
    scope: str | None,
) -> int:
    """Count operational events with ``id > after_id`` matching the active filters.

    The cursor is the monotonic primary key ``id`` (the value the list exposes as
    its newest item), so this is ``Event.id > after_id``. The same audit/security
    exclusion and level/scope/search filters as the visible list are applied, so
    the count reflects only events the user would actually see appended. One cheap
    indexed aggregate; returns ``0`` when nothing newer matches.
    """
    type_expr = func.json_extract(Event.event_metadata, "$.type")
    query = (
        db.query(func.count(Event.id))
        .filter(or_(type_expr.is_(None), type_expr.notin_(_AUDIT_EVENT_TYPES)))
        .filter(Event.id > after_id)
    )
    if scope:
        query = query.filter(Event.scope == scope)
    if levels:
        query = query.filter(Event.level.in_(levels))
    if q:
        query = query.filter(Event.message.ilike(f"%{q}%"))
    return int(query.scalar() or 0)


@router.get("/events/since")
def events_since(
    db: DbDep,
    user: CurrentUser,
    after: int,
    level: str | None = None,
    q: str | None = None,
    scope: str | None = None,
) -> dict[str, int]:
    """Return how many matching operational events were added after the list head.

    The browser polls this with ``after=<newest id on the list>`` to drive the
    "N new events" pill. The count honours the active level/search/scope filters
    (and the audit/security exclusion), so it reflects only events that would
    actually appear in the current view. A cheap COUNT only; no rows are returned.
    """
    levels = _parse_levels(level)
    qval = (q or "").strip() or None
    scopeval = (scope or "").strip() or None
    count = _count_operational_events_newer_than(
        db, after_id=after, levels=levels, q=qval, scope=scopeval
    )
    return {"count": count}


@router.get("/audit/batch", response_class=HTMLResponse)
def audit_batch(
    request: Request,
    db: DbDep,
    user: AdminUser,
    before: int | None = None,
    q: str | None = None,
) -> Response:
    """Return one continuous-scroll batch of audit/security rows + a sentinel.

    Admin-only: the ``AdminUser`` dependency 403s any non-admin (viewer or
    operator) before this runs. The batch contains *only* audit/security records
    (the keyset helper filters by type), so no operational event can leak in. The
    sentinel mirrors the operational ``/events/batch`` shape: a ``<tr>`` carrying
    ``hx-trigger="revealed"`` that swaps itself for the next (older) batch, with
    the active search filter riding its cursor URL.
    """
    qval = (q or "").strip() or None
    events, next_before = _audit_batch(db, before_id=before, q=qval)
    return templates.TemplateResponse(
        request,
        "_partials/audit_batch.html",
        deps.base_context(
            request,
            db,
            user,
            events=events,
            next_before=next_before,
            filter_qs=_audit_filter_qs(qval),
        ),
    )


@router.get("/events/audit", response_class=HTMLResponse)
def audit_events_page(
    request: Request,
    db: DbDep,
    user: AdminUser,
    before: int | None = None,
    q: str | None = None,
) -> Response:
    """Render the audit/security event log (continuous scroll); admin-only.

    Admin-only: the ``AdminUser`` dependency 403s any non-admin (viewer or
    operator) before this renders. The page shows only audit/security records;
    no operational event can appear here. The sentinel appends older records
    (keyset by id); ``?before=`` is the no-JS pagination fallback and ``?q=``
    filters by message text.
    """
    qval = (q or "").strip() or None
    events, next_before = _audit_batch(db, before_id=before, q=qval)
    return templates.TemplateResponse(
        request,
        "audit.html",
        deps.base_context(
            request,
            db,
            user,
            events=events,
            next_before=next_before,
            filter_qs=_audit_filter_qs(qval),
            search_q=qval or "",
            audit_view=True,
        ),
    )


@router.get("/partials/status", response_class=HTMLResponse)
def partial_status(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Return the status banner fragment for the lazy HTMX load.

    Failure-isolated: any error building the banner degrades to a benign empty
    fragment rather than failing the page. This is why the banner is a lazy
    partial and not a synchronous query in the base template -- a monitoring
    query hiccup must never 500 every authenticated page.
    """
    try:
        recent, total = get_events(db, level_floor="error", limit=1, offset=0)
        error_count = total
        latest = _event_view(recent[0]) if recent else None
    except Exception:  # noqa: BLE001 - the banner must never fail the page
        logger.exception("status banner query failed")
        error_count = 0
        latest = None
    return templates.TemplateResponse(
        request,
        "_partials/status_banner.html",
        deps.base_context(
            request, db, user, error_count=error_count, latest_error=latest
        ),
    )
