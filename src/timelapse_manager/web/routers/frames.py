"""Frame routes: the frame browser plus per-project frame image, thumbnail,
detail, edit, soft-delete, and restore."""

from __future__ import annotations

import datetime
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from pydantic import BeforeValidator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from ...config import SessionSettings, Settings
from ...db.models import Frame, Project, RenderJob, User
from ...executors import run_in
from ...runtime import get_context
from ...security.sessions import reauthorize_after_body
from ...services import frames as frame_service
from ...services import renders as render_service
from ...services._base import Actor, Result, run_effects
from ...services.errors import ValidationError
from ...storage import paths
from ...storage.frames import ImportResult, distinct_project_ids
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ..idempotency import run_idempotent
from ..multipart import (
    BodyTooLargeError,
    InsufficientSpoolSpaceError,
    MultipartError,
    TooManyFilesError,
    UploadSpool,
    stream_multipart,
    upload_spool,
)
from ._jobs import card_response, delete_response
from ._shared import (
    _get_project_or_404,
    _settings,
)
from ._viewmodels import (
    _fmt_bytes,
    _fmt_dt,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_FRAMES_PER_PAGE = 60

# A scroll session appends at most this many batches (10 x 60 = 600 tiles) before
# the grid shows an end-cap with a "Jump further back" link instead of another
# sentinel. Every appended tile costs DOM nodes, observers and selection work, so
# an unbounded scroll slowly degrades the page (#70 F-P4).
_MAX_GRID_BATCHES = 10


@dataclass(frozen=True)
class _FrameView:
    """Display projection of a frame row for the browser grid."""

    id: int
    sequence_index: int
    capture_timestamp: str | None
    # Raw datetime for timezone-aware display via the localdt template filter.
    capture_timestamp_raw: datetime.datetime | None
    thumbnail_url: str | None
    lifecycle_state: str
    # When non-null, the frame is excluded from rendered output (but stays shown
    # in the browser); drives the tile/drawer "excluded" badge. Orthogonal to
    # lifecycle_state.
    excluded_at: datetime.datetime | None = None
    # Owning project — carried per-frame so a tile builds its own image/action
    # URLs (the cross-project "All Projects" grid mixes projects in one view).
    project_id: int = 0
    project_name: str | None = None
    # Detail-only fields, populated for the frame drawer; the grid tile leaves
    # them at their defaults (it never reads them).
    width: int | None = None
    height: int | None = None
    file_size_bytes: int | None = None
    file_size_display: str | None = None
    origin: str | None = None

    @property
    def capture_epoch(self) -> int | None:
        """Capture time as UTC epoch seconds, or ``None`` if untimed.

        The stored column is naive UTC, so it is read as UTC. This is the unit the
        frames-page scroll cursor maps against the ribbon's epoch-second bounds.
        """
        ts = self.capture_timestamp_raw
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.UTC)
        return int(ts.timestamp())


def _parse_at(at: str | None, viewer_tz: str | None) -> datetime.datetime | None:
    """Parse the ``?at=`` ISO-8601 jump anchor, or ``None`` if absent/invalid.

    Accepts the ``datetime-local`` form the jump form submits (no offset) as well
    as a full offset-aware ISO string. A naive value is wall-clock time in the
    viewer's zone ``viewer_tz`` (UTC when unset), the zone every timestamp on the
    page is shown in (#70 F-R5); an offset-aware value (a link shared between
    users) keeps its own offset. The result is aware UTC. A malformed value is
    ignored rather than erroring, so a hand-edited URL degrades to the plain
    newest-first first page.
    """
    if not at:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(at)
    except ValueError:
        return None
    return deps.viewer_local_to_utc(parsed, viewer_tz)


def _blank_to_none(value: object) -> object:
    """Read an empty (or all-blank) query value as absent.

    The project picker's "All Projects" option has ``value=""``, so choosing it
    requests ``?project_id=``. ``int | None`` rejects the empty string with a 422;
    it means "no project", the same as leaving the parameter out (#226).
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


#: A ``project_id`` query parameter where the empty string means "all projects".
OptionalProjectId = Annotated[int | None, BeforeValidator(_blank_to_none)]

# Recognised values for the ``?jump=`` campaign-scale navigation control. Each
# is a thin wrapper over the existing keyset/window machinery (see
# ``_resolve_jump``): ``start``/``newest`` land on a series end, the two gap jumps
# step to a capture lapse relative to ``?at=``.
_JUMP_VALUES = frozenset({"start", "newest", "prev_gap", "next_gap"})


@router.get("/frames", response_class=HTMLResponse)
def frames_page(
    request: Request,
    db: DbDep,
    user: CurrentUser,
    project_id: OptionalProjectId = None,
    before: int | None = None,
    at: str | None = None,
    jump: str | None = None,
    show_deleted: int = 0,
) -> Response:
    """Render a continuous-scroll frame browser.

    With a ``project_id`` this is one project's grid, keyset-paged on
    ``sequence_index``. Without one (the bare ``/frames`` nav link) it is the
    cross-project "All Projects" grid, keyset-paged on the globally monotonic
    frame id, with a project picker to narrow to a single project.

    A single-project request may carry ``?at=<iso8601>`` to jump the grid to a
    bidirectional window centered on that capture time (older and newer frames
    around the anchor), or ``?jump=`` for a campaign-scale jump
    (``start``/``newest``/``prev_gap``/``next_gap``). Both are single-project only
    -- the global grid pages on insertion id with nullable timestamps, so it has
    no time axis; a ``jump`` or ``at`` on the global grid is ignored. An HTMX
    request (the jump form/buttons) gets just the batch fragment to swap into the
    grid; a plain GET (no-JS) gets the full page resolved the same way.

    Jump resolution may add two display-only context keys the grid renders:
    ``preceding_gap`` (a capture lapse the grid landed before, for an in-grid gap
    band) and ``nearest_frame_note`` (the real capture an off-frame ``?at=``
    resolved to). Both are absent (``None``) unless the jump produced them.
    """
    anchor = _parse_at(at, user.viewer_timezone)

    if project_id is not None and (anchor is not None or (jump in _JUMP_VALUES)):
        project = _get_project_or_404(db, project_id)
        frames, next_before, extra = _resolve_jump(
            db,
            project_id,
            project=project,
            jump=jump,
            anchor=anchor,
            include_deleted=bool(show_deleted),
        )
        # The jump form/buttons target #frame-grid with an innerHTML swap, so an
        # HTMX request gets only the tiles+sentinel fragment; a full page swapped
        # into the grid would nest the page inside itself.
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "_partials/frames_batch.html",
                deps.base_context(
                    request,
                    db,
                    user,
                    project={"id": project_id},
                    all_projects=False,
                    frames=frames,
                    next_before=next_before,
                    show_deleted=bool(show_deleted),
                    **extra,
                ),
            )
        return _frames_full_page(
            request,
            db,
            user,
            project=project,
            frames=frames,
            next_before=next_before,
            show_deleted=bool(show_deleted),
            **extra,
        )

    if project_id is None:
        # "All Projects" -- a cross-project grid, keyset-paged on the frame id
        # (globally monotonic; per-project sequence_index can't span projects).
        frames, next_before = _global_frame_batch(
            db, before=before, include_deleted=bool(show_deleted)
        )
        return _frames_full_page(
            request,
            db,
            user,
            project=None,
            frames=frames,
            next_before=next_before,
            show_deleted=bool(show_deleted),
            all_projects=True,
        )

    project = _get_project_or_404(db, project_id)
    frames, next_before = _frame_batch(
        db, project_id, before=before, include_deleted=bool(show_deleted)
    )
    return _frames_full_page(
        request,
        db,
        user,
        project=project,
        frames=frames,
        next_before=next_before,
        show_deleted=bool(show_deleted),
    )


def _frames_full_page(
    request: Request,
    db: DbSession,
    user: User,
    *,
    project: Any,
    frames: list[_FrameView],
    next_before: int | None,
    show_deleted: bool,
    all_projects: bool = False,
    preceding_gap: Any = None,
    nearest_frame_note: Any = None,
) -> Response:
    """Render the full frames page, resolving the project picker once.

    Shared by the All-Projects, single-project, and jump/windowed (``?at=`` /
    ``?jump=``) page paths so the picker query and template wiring stay in one
    place. ``preceding_gap`` and ``nearest_frame_note`` are the optional jump
    annotations; they default to ``None`` (the non-jump paths) and are passed
    explicitly so they are not silently dropped from the template context.
    """
    picker_rows = db.execute(
        select(Project.id, Project.name).order_by(Project.name)
    ).all()
    picker_projects = [{"id": pid, "name": name} for pid, name in picker_rows]
    return templates.TemplateResponse(
        request,
        "frames.html",
        deps.base_context(
            request,
            db,
            user,
            project=project,
            all_projects=all_projects,
            projects=picker_projects,
            frames=frames,
            next_before=next_before,
            show_deleted=show_deleted,
            preceding_gap=preceding_gap,
            nearest_frame_note=nearest_frame_note,
        ),
    )


def _frame_window(
    db: DbSession,
    project_id: int,
    *,
    anchor: datetime.datetime,
    include_deleted: bool,
    side_limit: int = 30,
) -> tuple[list[_FrameView], int | None]:
    """Fetch a centered window of frames around ``anchor`` + the next cursor.

    Thin view-layer wrapper over the storage window resolver: returns
    ``(views, next_before)`` where ``next_before`` is the ``sequence_index`` to
    pass as ``before`` for the following (older) batch, so the existing sentinel
    continues the downward scroll from the oldest frame in the window.
    """
    from ...storage import frames as frame_service

    rows, next_before = frame_service.list_frames_window(
        db,
        project_id,
        anchor,
        side_limit=side_limit,
        include_deleted=include_deleted,
    )
    return [_to_frame_view(f) for f in rows], next_before


def _aware(value: datetime.datetime) -> datetime.datetime:
    """Coerce a stored (naive UTC) datetime to an aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _jump_span(
    project: Any,
    frame_times: list[datetime.datetime],
    now: datetime.datetime,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Resolve the [start, end] span gap detection runs over for a project.

    Identical to the ribbon SVG's span: the campaign's ``start_date`` (or the
    first frame, or an hour ago when neither exists) to its ``end_date`` (or
    now), with a degenerate span widened to an hour. Matching the ribbon's span
    is what makes the Next/Prev-gap buttons land on exactly the lapses the ribbon
    draws as markers.
    """
    start_raw = project.start_date or (frame_times[0] if frame_times else None)
    start = (
        _aware(start_raw)
        if start_raw is not None
        else now - datetime.timedelta(hours=1)
    )
    end = _aware(project.end_date) if project.end_date is not None else now
    if end <= start:
        end = start + datetime.timedelta(hours=1)
    return start, end


def _gap_band(gap: Any) -> dict[str, Any]:
    """Shape a storage ``CaptureGap`` into the grid's gap-band display context.

    Carries the bounding capture times as raw (aware UTC) datetimes -- not
    pre-formatted strings -- so the template renders them through the same
    timezone-aware filter every other timestamp uses. ``frame_count`` is always
    ``0`` (a gap is by definition the absence of frames between its bounds);
    it is surfaced explicitly so the band can read "0 frames" without the
    template inferring it.
    """
    return {
        "start": _aware(gap.before),
        "end": _aware(gap.after),
        "duration_seconds": int(gap.duration.total_seconds()),
        "frame_count": 0,
    }


def _resolve_jump(
    db: DbSession,
    project_id: int,
    *,
    project: Any,
    jump: str | None,
    anchor: datetime.datetime | None,
    include_deleted: bool,
) -> tuple[list[_FrameView], int | None, dict[str, Any]]:
    """Resolve a ``?jump=`` / ``?at=`` request to a batch + the display extras.

    Returns ``(frames, next_before, extra)`` where ``extra`` always carries both
    ``preceding_gap`` and ``nearest_frame_note`` keys (``None`` when not
    applicable), so the response context shape is uniform across every jump.

    * ``start`` -- the oldest batch, anchored at the first frame so the end-cap
      renders (``next_before`` is ``None``).
    * ``newest`` -- the default newest-first first batch (the bare grid).
    * ``next_gap`` / ``prev_gap`` -- detect the capture gaps over the ribbon's
      span, pick the nearest one after/before the ``?at=`` anchor, and window on
      the last frame before that gap; the gap rides back as ``preceding_gap``.
      With no anchor, or no gap in that direction, this degrades to the newest
      batch with no gap band rather than erroring.
    * no jump (plain ``?at=``) -- the existing centered window.

    The nearest-frame note is surfaced for any timestamp-anchored window (the
    plain ``?at=`` path and gap jumps) whenever the anchor lands between frames.
    """
    from ...storage import frames as frame_service

    extra: dict[str, Any] = {"preceding_gap": None, "nearest_frame_note": None}

    if jump == "start":
        oldest = frame_service.oldest_active_seq(
            db, project_id, include_deleted=include_deleted
        )
        if oldest is None:
            return [], None, extra
        # The oldest *page*, newest-first: up to a page of frames at-or-after the
        # series-minimum sequence (so a 200-frame project shows its oldest ~60,
        # not a single tile). Nothing is older than the minimum, so next_before is
        # always None and the series-start end-cap renders. Sparse sequences (from
        # soft-deletes) are handled too -- this counts off existing rows, not a
        # fixed sequence span.
        rows = frame_service.list_frames_at_or_after_seq(
            db,
            project_id,
            oldest,
            limit=_FRAMES_PER_PAGE,
            include_deleted=include_deleted,
        )
        return [_to_frame_view(f) for f in rows], None, extra

    if jump == "newest":
        frames, next_before = _frame_batch(
            db, project_id, before=None, include_deleted=include_deleted
        )
        return frames, next_before, extra

    if jump in ("next_gap", "prev_gap"):
        # Gap detection is pinned to active-only frames so the buttons land on
        # exactly the lapses the ribbon draws (the ribbon detects over active-only
        # frames unconditionally). The resolved window/note below still honour
        # show_deleted for what is displayed.
        effective_anchor, gap = _resolve_gap_anchor(
            db,
            project_id,
            project=project,
            anchor=anchor,
            direction="next" if jump == "next_gap" else "prev",
        )
        if effective_anchor is None:
            # No anchor or no gap in that direction: stay on the newest batch.
            frames, next_before = _frame_batch(
                db, project_id, before=None, include_deleted=include_deleted
            )
            return frames, next_before, extra
        if gap is not None:
            extra["preceding_gap"] = _gap_band(gap)
        frames, next_before = _frame_window(
            db, project_id, anchor=effective_anchor, include_deleted=include_deleted
        )
        extra["nearest_frame_note"] = _nearest_note(
            db, project_id, effective_anchor, include_deleted=include_deleted
        )
        return frames, next_before, extra

    # Plain ?at= window (no jump button): the existing centered window, with a
    # nearest-frame note when the anchor lands off an exact frame.
    assert anchor is not None  # caller only routes here with an anchor
    frames, next_before = _frame_window(
        db, project_id, anchor=anchor, include_deleted=include_deleted
    )
    extra["nearest_frame_note"] = _nearest_note(
        db, project_id, anchor, include_deleted=include_deleted
    )
    return frames, next_before, extra


def _resolve_gap_anchor(
    db: DbSession,
    project_id: int,
    *,
    project: Any,
    anchor: datetime.datetime | None,
    direction: str,
) -> tuple[datetime.datetime | None, Any]:
    """Find the gap adjacent to ``anchor`` and the timestamp to window on.

    Returns ``(effective_anchor, gap)`` -- the last-frame-before-the-gap
    timestamp to centre the window on, and the gap itself for the band. Returns
    ``(None, None)`` when there is no anchor or no gap in ``direction`` (the
    caller then leaves the grid on the newest batch). The anchor defaults to the
    grid's newest captured frame when ``?at=`` is absent, so a gap step from the
    default view still has a reference point. Gaps are computed over active-only
    frames (matching the ribbon's markers), regardless of any ``show_deleted``
    view state.
    """
    from ...storage import frames as frame_service

    # The pair walk runs in SQL (#66 C7): only the bounds and the gaps come
    # back, not every timestamp of the project.
    bounds = frame_service.active_frame_time_bounds(db, project_id)
    if bounds is None or bounds.count < 2:
        return None, None
    now = datetime.datetime.now(datetime.UTC)
    span_start, span_end = _jump_span(project, [_aware(bounds.first)], now)
    gaps = [
        frame_service.CaptureGap(before=_aware(g.before), after=_aware(g.after))
        for g in frame_service.list_capture_gaps(db, project_id, span_start, span_end)
    ]
    if not gaps:
        return None, None
    # No explicit anchor: step relative to the newest captured frame.
    ref = _aware(anchor) if anchor is not None else _aware(bounds.last)
    gap = frame_service.nearest_gap(gaps, ref, direction=direction)
    if gap is None:
        return None, None
    return gap.before, gap


def _nearest_note(
    db: DbSession,
    project_id: int,
    anchor: datetime.datetime,
    *,
    include_deleted: bool,
) -> datetime.datetime | None:
    """Return the off-frame nearest-capture note for a timestamp anchor, or None.

    The grid centres on the first frame at-or-after the anchor. When the anchor
    is not an exact frame time, that centre frame's timestamp is surfaced (as a
    raw aware datetime) so the template can show "Nearest frame: <time>". An exact
    hit, or an anchor past the last frame, yields ``None`` -- no note.
    """
    from ...storage import frames as frame_service

    center, exact = frame_service.resolve_window_center(
        db, project_id, anchor, include_deleted=include_deleted
    )
    if center is None or exact:
        return None
    return _aware(center)


def _frame_batch(
    db: DbSession,
    project_id: int,
    *,
    before: int | None,
    include_deleted: bool,
    limit: int = _FRAMES_PER_PAGE,
) -> tuple[list[_FrameView], int | None]:
    """Fetch one keyset batch of frames (newest-first) + the next cursor.

    Returns ``(views, next_before)``; ``next_before`` is the sequence_index to
    pass as ``before`` for the following (older) batch, or ``None`` at the start
    of the series (no more frames).
    """
    from ...storage import frames as frame_service

    rows = frame_service.list_frames_keyset(
        db,
        project_id,
        before_seq=before,
        limit=limit + 1,
        include_deleted=include_deleted,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    views = [_to_frame_view(f) for f in rows]
    next_before = rows[-1].sequence_index if (rows and has_more) else None
    return views, next_before


def _to_frame_view(frame: Frame, *, project_name: str | None = None) -> _FrameView:
    """Project a frame row into its grid display view.

    Image and per-tile action URLs are built from the frame's OWN
    ``project_id`` so a tile is correct whether it appears in a single-project
    grid or the mixed cross-project "All Projects" grid.
    """
    return _FrameView(
        id=frame.id,
        sequence_index=frame.sequence_index,
        capture_timestamp=_fmt_dt(frame.capture_timestamp),
        capture_timestamp_raw=frame.capture_timestamp,
        thumbnail_url=f"/projects/{frame.project_id}/frames/{frame.id}/thumbnail",
        lifecycle_state=frame.lifecycle_state,
        excluded_at=frame.excluded_at,
        project_id=frame.project_id,
        project_name=project_name,
    )


def _global_frame_batch(
    db: DbSession,
    *,
    before: int | None,
    include_deleted: bool,
    limit: int = _FRAMES_PER_PAGE,
) -> tuple[list[_FrameView], int | None]:
    """Fetch one keyset batch of frames across ALL projects (newest-first).

    Pages on the frame primary key ``id`` (globally monotonic, never null);
    ``next_before`` is the id to pass as ``before`` for the next/older batch, or
    ``None`` at the start of the series. Each view carries its project's name so
    the mixed grid can label which project a frame belongs to.
    """
    from ...storage import frames as frame_service

    rows = frame_service.list_all_frames_keyset(
        db,
        before_id=before,
        limit=limit + 1,
        include_deleted=include_deleted,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    # Resolve project names in one query for the projects present in this batch.
    name_by_id: dict[int, str] = {}
    pids = {f.project_id for f in rows}
    if pids:
        name_by_id = dict(
            db.execute(select(Project.id, Project.name).where(Project.id.in_(pids)))
            .tuples()
            .all()
        )
    views = [_to_frame_view(f, project_name=name_by_id.get(f.project_id)) for f in rows]
    next_before = rows[-1].id if (rows and has_more) else None
    return views, next_before


def _batch_cap_href(
    frames: list[_FrameView],
    *,
    batch_no: int,
    project_id: int | None,
    next_before: int | None,
    show_deleted: bool,
) -> str | None:
    """The cap's jump link when this batch ends the scroll session, else ``None``.

    ``None`` means "render the normal sentinel" (or, at the series start, the
    ordinary end-cap).
    """
    if next_before is None or batch_no < _MAX_GRID_BATCHES:
        return None
    return _cap_jump_href(
        frames,
        project_id=project_id,
        next_before=next_before,
        show_deleted=show_deleted,
    )


def _cap_jump_href(
    frames: list[_FrameView],
    *,
    project_id: int | None,
    next_before: int,
    show_deleted: bool,
) -> str:
    """Link for the batch-cap end-cap: continue from the oldest loaded frame.

    A single-project grid has a time axis, so the link is
    ``?at=<oldest loaded capture time, ISO-8601 with offset>`` (the offset keeps
    it unambiguous for any viewer zone; ``_parse_at`` honours it). The
    cross-project grid has no time axis, and a loaded frame without a capture
    time has nothing to anchor on, so both continue from the keyset cursor
    (``before=``) instead.
    """
    tail = "&show_deleted=1" if show_deleted else ""
    scope = "" if project_id is None else f"project_id={project_id}&"
    oldest = frames[-1].capture_timestamp_raw if frames else None
    if project_id is not None and oldest is not None:
        anchor = _aware(oldest).isoformat()
        return f"/frames?{scope}at={quote(anchor, safe='')}{tail}"
    return f"/frames?{scope}before={next_before}{tail}"


@router.get("/frames/batch", response_class=HTMLResponse)
def frames_batch(
    request: Request,
    db: DbDep,
    user: CurrentUser,
    project_id: OptionalProjectId = None,
    before: int | None = None,
    show_deleted: int = 0,
    batch_no: Annotated[int, Query(ge=1, le=10_000)] = 1,
) -> Response:
    """Return one continuous-scroll batch of frame tiles + a fresh sentinel.

    The sentinel (a real ``<a>`` that doubles as the no-JS pagination link)
    carries ``hx-trigger="revealed"`` and swaps itself out for the next batch, so
    scrolling appends older frames; the end-cap replaces it at the series start.

    With a ``project_id`` the batch is one project paged on ``sequence_index``;
    without one it is the cross-project "All Projects" batch paged on frame id.
    The presence of ``project_id`` is the sole mode discriminator.

    ``batch_no`` is this batch's 1-based position in the scroll session (the
    sentinel that fetched it carries it). From ``_MAX_GRID_BATCHES`` on, a batch
    that still has older frames ends in a cap with a jump link instead of a
    sentinel, which keeps a long scroll from growing the DOM without bound.
    """
    if project_id is None:
        frames, next_before = _global_frame_batch(
            db, before=before, include_deleted=bool(show_deleted)
        )
        return templates.TemplateResponse(
            request,
            "_partials/frames_batch.html",
            deps.base_context(
                request,
                db,
                user,
                project=None,
                all_projects=True,
                frames=frames,
                next_before=next_before,
                show_deleted=bool(show_deleted),
                batch_no=batch_no,
                cap_href=_batch_cap_href(
                    frames,
                    batch_no=batch_no,
                    project_id=None,
                    next_before=next_before,
                    show_deleted=bool(show_deleted),
                ),
            ),
        )

    _get_project_or_404(db, project_id)
    frames, next_before = _frame_batch(
        db, project_id, before=before, include_deleted=bool(show_deleted)
    )
    return templates.TemplateResponse(
        request,
        "_partials/frames_batch.html",
        deps.base_context(
            request,
            db,
            user,
            project={"id": project_id},
            all_projects=False,
            frames=frames,
            next_before=next_before,
            show_deleted=bool(show_deleted),
            batch_no=batch_no,
            cap_href=_batch_cap_href(
                frames,
                batch_no=batch_no,
                project_id=project_id,
                next_before=next_before,
                show_deleted=bool(show_deleted),
            ),
        ),
    )


@router.get("/frames/since")
def frames_since(
    db: DbDep,
    user: CurrentUser,
    after: int,
    project_id: OptionalProjectId = None,
    show_deleted: int = 0,
) -> dict[str, int]:
    """Return how many frames were added after the client's newest frame id.

    The browser polls this with ``after=<data-newest-id>`` (the grid's newest
    frame id) to drive the "N new frames" pill. The cursor is the globally
    monotonic primary key ``id`` -- the same key the grid exposes as its newest
    item -- so the count is ``Frame.id > after``. The single-vs-global scope is
    discriminated by ``project_id`` exactly as the grid is: present scopes the
    count to one project, absent counts across all projects. A cheap COUNT only;
    no rows are returned.
    """
    from ...storage import frames as frame_service

    count = frame_service.count_frames_newer_than(
        db,
        after_id=after,
        project_id=project_id,
        include_deleted=bool(show_deleted),
    )
    return {"count": count}


# Frame image bytes are JPEG by capture, but uploads may be PNG, so the served
# content type is derived from the on-disk file extension rather than assumed.
_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _resolve_frame_real_path(db: DbSession, project_id: int, frame_id: int) -> str:
    """Resolve a frame's on-disk real path, or raise 404.

    The frame must belong to ``project_id`` (anti-IDOR) and its stored path must
    resolve inside the project's own frame directories -- a corrupt or hostile
    path (traversal, absolute escape, or symlink) is treated as not found. The
    containment rule is shared with the export and render paths
    (:func:`~timelapse_manager.storage.paths.resolve_contained_frame_path`,
    #67 S16). The path is derived solely from the stored ``file_path``; no client
    string reaches the filesystem. Shared by the full-image and thumbnail routes.
    """
    import os.path

    from ...storage import paths

    frame = db.get(Frame, frame_id)
    if frame is None or frame.project_id != project_id or frame.file_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    project = _get_project_or_404(db, project_id)
    try:
        real_path = os.fspath(
            paths.resolve_contained_frame_path(_settings(), project, frame.file_path)
        )
    except paths.PathContainmentError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from None
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return real_path


# Frame bytes never change once written, so they are cacheable; kept private
# because the routes are gated behind authentication.
_FRAME_CACHE_CONTROL = "private, max-age=86400"
# A full-size frame served in place of a thumbnail (no generation slot free, or
# generation failed) must not be cached under the thumbnail URL: the browser
# asks again later and then gets the real thumbnail.
_THUMBNAIL_FALLBACK_CACHE_CONTROL = "private, no-cache"


def _frame_file_response(real_path: str, cache_control: str) -> FileResponse:
    """Serve a contained, already-resolved image file with ``cache_control``."""
    import os.path

    media_type = _IMAGE_MEDIA_TYPES.get(
        os.path.splitext(real_path)[1].lower(), "application/octet-stream"
    )
    return FileResponse(
        real_path, media_type=media_type, headers={"Cache-Control": cache_control}
    )


@router.get("/projects/{project_id}/frames/{frame_id}/image")
def frame_image(
    db: DbDep, user: CurrentUser, project_id: int, frame_id: int
) -> Response:
    """Serve a frame's image bytes from local disk to any authenticated user.

    Viewing a frame is a read, so any logged-in role may fetch it. The frame is
    looked up by id and required to belong to ``project_id`` -- a mismatch is a
    404, so a frame id cannot be probed across project scopes. Only local files
    are served, so there is no outbound-request (SSRF) surface.
    """
    real_path = _resolve_frame_real_path(db, project_id, frame_id)
    return _frame_file_response(real_path, _FRAME_CACHE_CONTROL)


# Frame ids whose thumbnail was refused as oversized, so the warning is logged
# once per frame per process. Capped so a flood of bad frames cannot grow it
# without bound; past the cap the refusals go unlogged.
_REFUSED_THUMBNAIL_FRAMES: set[int] = set()
_REFUSED_THUMBNAIL_FRAMES_CAP = 10_000
_REFUSED_THUMBNAIL_LOCK = threading.Lock()


def _log_refused_thumbnail_once(project_id: int, frame_id: int) -> None:
    """Log a refused (oversized or unreadable) frame once per frame id."""
    with _REFUSED_THUMBNAIL_LOCK:
        if (
            frame_id in _REFUSED_THUMBNAIL_FRAMES
            or len(_REFUSED_THUMBNAIL_FRAMES) >= _REFUSED_THUMBNAIL_FRAMES_CAP
        ):
            return
        _REFUSED_THUMBNAIL_FRAMES.add(frame_id)
    logger.warning(
        "no thumbnail for frame %s (project %s): its image header is oversized "
        "or unreadable",
        frame_id,
        project_id,
    )


def _thumbnail_is_fresh(thumb_path: Path, source_path: str) -> bool:
    """Return whether a usable cached thumbnail at least as new as the frame exists.

    ``lstat`` is used so a symlink planted in the cache is never served: only a
    regular, non-empty file counts.
    """
    import os
    import stat

    try:
        info = os.lstat(thumb_path)
        source_mtime = os.path.getmtime(source_path)
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_size > 0
        and info.st_mtime >= source_mtime
    )


@router.get("/projects/{project_id}/frames/{frame_id}/thumbnail")
def frame_thumbnail(
    db: DbDep,
    user: CurrentUser,
    project_id: int,
    frame_id: int,
    size: int = paths.DEFAULT_THUMBNAIL_WIDTH,
) -> Response:
    """Serve a cached, downscaled JPEG of a frame (any authenticated user).

    ``size`` is the width in px: 320 (the default, the frame grid) or 640 (the
    dashboard card and project header, #70 F-P2); anything else is a 422. The
    source frame is resolved through the same anti-IDOR + containment guard as
    the full image, and the cache file through the thumbnail-root containment
    check. Local files only -- no SSRF surface.

    A missing or stale thumbnail is generated with the bundled ffmpeg (#67 P3):

    * at most ``render.thumbnail_concurrency`` generations run at once; a
      request that waits longer than two seconds for a slot gets the full
      image, marked ``no-cache`` so the browser retries the thumbnail later;
    * the file is written to a temporary name and renamed into place, so a
      partial thumbnail is never served or cached;
    * a frame whose header declares an oversized image is a 404 (logged once
      per frame); any other generation failure serves the full image, uncached.

    The database session is committed, and so released, once the frame is
    resolved and before any waiting or generation (#182).
    """
    import subprocess

    from ...encode import thumbnail

    if size not in paths.THUMBNAIL_WIDTHS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="size must be one of: "
            + ", ".join(str(width) for width in sorted(paths.THUMBNAIL_WIDTHS)),
        )

    real_path = _resolve_frame_real_path(db, project_id, frame_id)
    # All the database work is done: end the transaction before the generation
    # slot wait (up to 2 s) and the ffmpeg run (up to 20 s), so the pooled
    # connection and its read snapshot are released instead of pinning a WAL
    # checkpoint (#182). Committing, not rolling back, keeps what the auth
    # dependency wrote (the session's last-seen touch), as the dependency's own
    # exit would.
    db.commit()
    settings = _settings()
    try:
        cache_dir = paths.contained_thumbnail_dir(settings, project_id)
    except paths.PathContainmentError:
        logger.warning(
            "thumbnail cache of project %s is outside the thumbnail root; "
            "serving the full frame",
            project_id,
        )
        return _frame_file_response(real_path, _THUMBNAIL_FALLBACK_CACHE_CONTROL)
    thumb_path = cache_dir / paths.thumbnail_file_name(frame_id, size)

    if not _thumbnail_is_fresh(thumb_path, real_path):
        with thumbnail.generation_slot(
            settings.render.thumbnail_concurrency
        ) as acquired:
            if not acquired:
                return _frame_file_response(
                    real_path, _THUMBNAIL_FALLBACK_CACHE_CONTROL
                )
            # Another request may have built it while this one waited.
            if not _thumbnail_is_fresh(thumb_path, real_path):
                try:
                    thumbnail.generate_thumbnail_atomic(
                        get_context().ffmpeg_path,
                        real_path,
                        thumb_path,
                        width=size,
                    )
                except ValueError:
                    # ImageTooLargeError: the header declares a dimension bomb.
                    _log_refused_thumbnail_once(project_id, frame_id)
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail="not found"
                    ) from None
                except (subprocess.SubprocessError, OSError):
                    logger.debug(
                        "thumbnail generation failed for frame %s",
                        frame_id,
                        exc_info=True,
                    )
                    return _frame_file_response(
                        real_path, _THUMBNAIL_FALLBACK_CACHE_CONTROL
                    )

    return _frame_file_response(str(thumb_path), _FRAME_CACHE_CONTROL)


def _frame_metadata_context(
    db: DbSession, project_id: int, frame_id: int
) -> dict[str, Any]:
    """Build the scene-metadata view context for a frame, or raise 404.

    The frame must belong to ``project_id`` (anti-IDOR). The raw scene-metadata
    envelope is run through the display normalizer here so the templates stay
    dumb. An absent or empty envelope yields ``scene_groups=[]`` and
    ``schema_version=None`` -- the template's null state, not an error. Shared by
    the drawer fragment and the no-JS full-page fallback.
    """
    from ...cameras.scene_metadata import normalize_scene_metadata, scene_schema_version

    frame = db.get(Frame, frame_id)
    if frame is None or frame.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    frame_view = _FrameView(
        id=frame.id,
        sequence_index=frame.sequence_index,
        capture_timestamp=_fmt_dt(frame.capture_timestamp),
        capture_timestamp_raw=frame.capture_timestamp,
        thumbnail_url=f"/projects/{project_id}/frames/{frame.id}/thumbnail",
        lifecycle_state=frame.lifecycle_state,
        project_id=frame.project_id,
    )
    return {
        "frame": frame_view,
        "scene_groups": normalize_scene_metadata(frame.scene_metadata),
        "schema_version": scene_schema_version(frame.scene_metadata),
    }


@router.get("/projects/{project_id}/frames/{frame_id}", response_class=HTMLResponse)
def frame_detail(
    request: Request, db: DbDep, user: CurrentUser, project_id: int, frame_id: int
) -> Response:
    """Render a frame's scene metadata as a full page (no-JS fallback).

    The frame tile's link is HTMX-enhanced to open the drawer, but its plain
    ``href`` points here so the feature still works without JavaScript. Wrapped in
    the app's base layout with a link back to the frame browser; a frame outside
    ``project_id`` is a 404.
    """
    return templates.TemplateResponse(
        request,
        "frame_detail.html",
        deps.base_context(
            request,
            db,
            user,
            project_id=project_id,
            **_frame_metadata_context(db, project_id, frame_id),
        ),
    )


def _frame_drawer_view(frame: Frame) -> _FrameView:
    """Project a frame row into the richer view the drawer renders.

    Carries the detail-only fields (dimensions, file size, origin) and the
    timestamp prefill the inline editor needs, in addition to the grid fields.
    """
    return _FrameView(
        id=frame.id,
        sequence_index=frame.sequence_index,
        capture_timestamp=_fmt_dt(frame.capture_timestamp),
        capture_timestamp_raw=frame.capture_timestamp,
        thumbnail_url=f"/projects/{frame.project_id}/frames/{frame.id}/thumbnail",
        lifecycle_state=frame.lifecycle_state,
        excluded_at=frame.excluded_at,
        project_id=frame.project_id,
        width=frame.width,
        height=frame.height,
        file_size_bytes=frame.file_size_bytes,
        file_size_display=(
            _fmt_bytes(frame.file_size_bytes)
            if frame.file_size_bytes is not None
            else None
        ),
        origin=frame.origin,
    )


def _neighbor_frame_ids(
    db: DbSession, project_id: int, sequence_index: int
) -> tuple[int | None, int | None]:
    """Resolve the (newer, older) neighbour frame ids within a project.

    Neighbours are ordered by ``sequence_index`` across the project's full
    series (lifecycle state is not filtered, so navigation is stable regardless
    of which frames are soft-deleted). Each side is ``None`` at the campaign end.
    The grid renders newest-first, so the newer neighbour is the next-higher
    sequence index and the older neighbour the next-lower one.
    """
    newer = db.execute(
        select(Frame.id)
        .where(Frame.project_id == project_id, Frame.sequence_index > sequence_index)
        .order_by(Frame.sequence_index.asc())
        .limit(1)
    ).scalar_one_or_none()
    older = db.execute(
        select(Frame.id)
        .where(Frame.project_id == project_id, Frame.sequence_index < sequence_index)
        .order_by(Frame.sequence_index.desc())
        .limit(1)
    ).scalar_one_or_none()
    return newer, older


def _frame_drawer_context(
    db: DbSession, project_id: int, frame_id: int
) -> dict[str, Any]:
    """Build the frame-drawer view context, or raise 404.

    The frame must belong to ``project_id`` (anti-IDOR). Bundles the rich frame
    view, its newer/older neighbour ids, and the normalized scene-metadata
    groups. Shared by the drawer GET route and the post-mutation re-render so the
    drawer body reflects the new state without a second round-trip.
    """
    from ...cameras.scene_metadata import normalize_scene_metadata, scene_schema_version

    frame = db.get(Frame, frame_id)
    if frame is None or frame.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    newer_id, older_id = _neighbor_frame_ids(db, project_id, frame.sequence_index)
    return {
        "project_id": project_id,
        "frame": _frame_drawer_view(frame),
        "newer_id": newer_id,
        "older_id": older_id,
        "scene_groups": normalize_scene_metadata(frame.scene_metadata),
        "schema_version": scene_schema_version(frame.scene_metadata),
    }


@router.get(
    "/projects/{project_id}/frames/{frame_id}/drawer",
    response_class=HTMLResponse,
)
def frame_drawer(
    request: Request, db: DbDep, user: CurrentUser, project_id: int, frame_id: int
) -> Response:
    """Serve a frame's detail as a drawer fragment, or the full page (no-JS).

    Viewing a frame is a read, so any logged-in role may open it. An HTMX request
    gets the drawer-body fragment to swap into the persistent drawer; a direct
    (no-JS) request gets the standalone full-page detail, so the feature still
    works without JavaScript. The frame must belong to ``project_id`` (a mismatch
    is a 404). The trailing ``/drawer`` literal is a distinct sibling of the
    ``/image`` and ``/thumbnail`` suffixes; a path parameter never
    matches across a ``/``, so this cannot collide with the bare detail route.
    """
    context = _frame_drawer_context(db, project_id, frame_id)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "_partials/drawer_frame_detail.html",
            deps.base_context(request, db, user, **context),
        )
    # No-JS fallback: the existing full-page scene-metadata detail.
    return templates.TemplateResponse(
        request,
        "frame_detail.html",
        deps.base_context(
            request, db, user, **_frame_metadata_context(db, project_id, frame_id)
        ),
    )


# --- bulk lifecycle endpoint ------------------------------------------------
#
# The four uniform lifecycle mutations share one polymorphic endpoint. Each runs
# synchronously and returns a summary partial swapped into the selection bar.
#
# Sync ceiling: above this many ids the synchronous path (one row UPDATE plus one
# audit-event INSERT per frame, in a single request/transaction) would hold a
# write transaction too long and risk the request timeout, so the request is
# rejected with a clear "too large" summary instead. The seam is named here; a
# later background-job path can plug in above the ceiling without changing the
# request/response contract or the client.
_BULK_SYNC_MAX = 500

# OOB-tile ceiling: at or below this many affected frames the response re-renders
# each changed tile out-of-band so on-screen tiles update in place. Above it the
# response instead flags the client to reload the current grid window, so a large
# operation never emits a wall of re-rendered tiles. This is a separate, smaller
# number than the sync ceiling: hundreds of synchronous flag flips are fine, but
# hundreds of OOB tile swaps are not.
_BULK_OOB_MAX = 60

# operation -> (storage helper, inverse operation). The inverse drives Undo: it
# is the operation that returns the succeeded id-set to its prior state.
_BULK_OPS: dict[str, tuple[str, str]] = {
    "delete": ("soft_delete_many", "restore"),
    "restore": ("restore_many", "delete"),
    "exclude": ("exclude_many", "include"),
    "include": ("include_many", "exclude"),
}


def _parse_frame_ids(raw: str) -> list[int]:
    """Parse the comma-joined ``frame_ids`` form field into a list of ints.

    The client serialises the selection as a single comma-joined field (the form
    parser keeps only the last value of a repeated field, so repeated fields
    cannot be used). Blank entries are skipped; a non-integer entry makes the
    whole field invalid. Order and duplicates are preserved as sent -- the
    storage helper is id-keyed and idempotent, so a duplicate is harmless.
    """
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def _build_bulk_summary(operation: str, result: Any) -> dict[str, Any]:
    """Shape a storage :class:`BulkResult` into the bulk-summary response context.

    The summary is the response contract: succeeded/failed counts, the failed
    ids (kept selected client-side for Retry), the affected ids (drive the tile
    updates and the selection clear), and a materialised inverse operation for
    Undo. The undo op is computed over the *succeeded* id-set captured here, at
    apply time -- it is never re-derived later, so an Undo always replays exactly
    the frames this operation changed.
    """
    _helper, inverse_op = _BULK_OPS[operation]
    affected = list(result.succeeded)
    return {
        "operation": operation,
        "succeeded": len(result.succeeded),
        "failed": len(result.failed),
        "failed_ids": list(result.failed),
        "affected_ids": affected,
        "undo": {"operation": inverse_op, "frame_ids": affected},
        # Past the OOB ceiling the client reloads the window instead of taking a
        # wall of out-of-band tiles.
        "reload_window": len(affected) > _BULK_OOB_MAX,
    }


def _bulk_error_summary(operation: str, message: str) -> dict[str, Any]:
    """Shape an error-state bulk response (nothing applied).

    Used for an over-ceiling selection or a malformed request. Returned as a 200
    so the client swaps the partial into the selection bar and shows ``message``;
    a non-2xx status would not be swapped by HTMX without extra handling.
    """
    return {
        "operation": operation,
        "succeeded": 0,
        "failed": 0,
        "failed_ids": [],
        "affected_ids": [],
        "undo": None,
        "reload_window": False,
        "error": message,
    }


def _bulk_result_response(
    request: Request,
    db: DbSession,
    user: User,
    summary: dict[str, Any],
) -> Response:
    """Render the bulk-result partial, with OOB tiles for the affected frames.

    The result partial swaps into ``#frames-action-bar``. When the response is
    not flagged for a window reload, each affected frame's tile is re-rendered
    out-of-band so the on-screen grid updates in place; HTMX silently drops an
    OOB swap whose target id is absent from the DOM, so the server can emit tiles
    for every affected id without tracking what is currently on screen.
    """
    db.flush()
    oob_frames: list[_FrameView] = []
    if not summary.get("reload_window") and not summary.get("error"):
        rows = (
            db.execute(select(Frame).where(Frame.id.in_(summary["affected_ids"])))
            .scalars()
            .all()
        )
        oob_frames = [_to_frame_view(f) for f in rows]
    return templates.TemplateResponse(
        request,
        "_partials/frames_bulk_result.html",
        deps.base_context(
            request,
            db,
            user,
            summary=summary,
            oob_frames=oob_frames,
        ),
    )


def _parse_descriptor_field(raw: str) -> Any:
    """Decode the ``descriptor`` form field (a JSON string) into a dataclass.

    The router owns the JSON decode so the storage leaf never sees a raw string
    or a request; the leaf then validates the decoded mapping into a
    :class:`RangeDescriptor`. A decode error or a validation error both surface as
    a :class:`DescriptorError`, which the callers turn into a clear summary.
    """
    from ...storage.frame_selection import DescriptorError, parse_descriptor

    try:
        body = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise DescriptorError("descriptor is not valid JSON") from exc
    return parse_descriptor(body)


@router.post("/frames/bulk")
def frames_bulk(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    form: FormDep,
) -> Response:
    """Apply one lifecycle mutation to a set of frames.

    Body (``application/x-www-form-urlencoded``):

    * ``operation`` -- one of ``delete``, ``restore``, ``exclude``, ``include``.
    * exactly one of:
      * ``frame_ids`` -- the selected ids, comma-joined (e.g. ``11,12,13``); or
      * ``descriptor`` -- a JSON range descriptor resolved server-side to an
        id-set (the "select all in range / in project" path). Both present, or
        neither, is rejected.

    Operator-gated and CSRF-protected like every other frame mutation (the
    selection bar's buttons drive this through HTMX, so the per-session token
    rides the request header automatically). The response is the bulk-result
    partial swapped into the selection bar: succeeded/failed counts, an Undo that
    replays the materialised inverse operation, and either out-of-band tile
    updates or a window-reload flag.

    Unknown ids are skipped, not fatal -- they come back in ``failed_ids`` so the
    client can keep them selected for a one-click Retry. A set larger than the
    synchronous ceiling -- whether listed explicitly or resolved from a descriptor
    -- is rejected with an error-state summary (status 200) telling the client to
    narrow it rather than risk a request timeout. The descriptor path is where a
    later background-mutation substrate plugs in above the ceiling without
    changing this request/response contract.
    """
    operation = form.get("operation", "")
    if operation not in _BULK_OPS:
        return _bulk_result_response(
            request,
            db,
            user,
            _bulk_error_summary(operation or "delete", "Unknown bulk operation."),
        )

    raw_descriptor = form.get("descriptor", "")
    raw_frame_ids = form.get("frame_ids", "")
    if raw_descriptor and raw_frame_ids:
        return _bulk_result_response(
            request,
            db,
            user,
            _bulk_error_summary(
                operation, "Provide either an explicit selection or a range, not both."
            ),
        )

    if raw_descriptor:
        frame_ids = _resolve_bulk_descriptor(db, raw_descriptor)
        if isinstance(frame_ids, str):
            return _bulk_result_response(
                request, db, user, _bulk_error_summary(operation, frame_ids)
            )
    else:
        try:
            frame_ids = _parse_frame_ids(raw_frame_ids)
        except ValueError:
            return _bulk_result_response(
                request,
                db,
                user,
                _bulk_error_summary(operation, "Invalid frame selection."),
            )

    if not frame_ids:
        return _bulk_result_response(
            request,
            db,
            user,
            _bulk_error_summary(operation, "No frames selected."),
        )

    if len(frame_ids) > _BULK_SYNC_MAX:
        return _bulk_result_response(
            request,
            db,
            user,
            _bulk_error_summary(
                operation,
                f"Too many frames ({len(frame_ids)}). Select at most "
                f"{_BULK_SYNC_MAX} at once, or use a range selection.",
            ),
        )

    from ...storage import frames as frame_service

    helper_name, _inverse = _BULK_OPS[operation]
    helper = getattr(frame_service, helper_name)
    result = helper(db, frame_ids, user.id)
    summary = _build_bulk_summary(operation, result)
    return _bulk_result_response(request, db, user, summary)


def _resolve_bulk_descriptor(db: DbSession, raw_descriptor: str) -> list[int] | str:
    """Resolve a descriptor form field to a sorted id-list, or an error message.

    The project the descriptor names must exist (404 otherwise). On a malformed
    descriptor the error message string is returned so the caller renders it as an
    error-state summary; on success a deterministic, sorted id-list is returned.
    """
    from ...storage import frame_selection

    try:
        descriptor = _parse_descriptor_field(raw_descriptor)
    except frame_selection.DescriptorError as exc:
        return str(exc)
    _get_project_or_404(db, descriptor.project_id)
    return frame_selection.materialize(db, descriptor)


@router.post("/frames/range/count")
def frames_range_count(
    db: DbDep,
    user: OperatorUser,
    form: FormDep,
) -> Response:
    """Return the resolved size of a range descriptor as ``{"count": N}``.

    Drives the honest "≈N" estimate the escalation banner and the selection bar
    show before any mutation. The estimate equals the size of the id-set a bulk
    operation over the same descriptor would act on, deselected ids subtracted.
    Operator-gated and CSRF-protected (it exists only to feed mutations); a
    malformed descriptor is a 400.
    """
    from ...storage import frame_selection

    descriptor = _descriptor_from_form(db, form)
    return JSONResponse({"count": frame_selection.count(db, descriptor)})


@router.post("/frames/range/materialize")
def frames_range_materialize(
    db: DbDep,
    user: OperatorUser,
    form: FormDep,
) -> Response:
    """Resolve a range descriptor to a concrete id-list as ``{"frame_ids": [...]}``.

    The explicit id-list an id-only operation (a later bulk timestamp offset)
    needs: it pins the exact frames at resolution time so a replay acts on the
    same set even if new frames have since arrived. Operator-gated, CSRF-protected,
    and a malformed descriptor is a 400. A range larger than ``frames.max_range_ids``
    is a 413.
    """
    from ...storage import frame_selection

    descriptor = _descriptor_from_form(db, form)
    # Refuse before resolving, so a whole-project selection never builds a
    # multi-megabyte id list (#67 P9). The count route gives the UI the number.
    limit = get_context().settings.frames.max_range_ids
    if frame_selection.count(db, descriptor) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Range too large; narrow the selection",
        )
    return JSONResponse({"frame_ids": frame_selection.materialize(db, descriptor)})


def _descriptor_from_form(db: DbSession, form: dict[str, str]) -> Any:
    """Parse-and-validate the ``descriptor`` field for the range query routes.

    Unlike the bulk path (which swaps an error summary into the action bar), the
    pure query routes return JSON, so a bad descriptor is a real 400 and a missing
    project is a 404. Returns the validated :class:`RangeDescriptor`.
    """
    from ...storage import frame_selection

    raw = form.get("descriptor", "")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="descriptor is required"
        )
    try:
        descriptor = _parse_descriptor_field(raw)
    except frame_selection.DescriptorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    _get_project_or_404(db, descriptor.project_id)
    return descriptor


def _build_offset_summary(seconds: int, result: Any) -> dict[str, Any]:
    """Shape a storage :class:`OffsetResult` into the offset-summary response.

    The summary is the response contract for ``POST /frames/offset``. It reports
    how many frames were shifted, how many were skipped because they carry no
    capture timestamp (off the time axis), and how many ids could not be found.

    Undo is an **inverse offset**, not a restore: it replays ``-seconds`` over
    exactly the ids that were shifted -- captured here at apply time, so a later
    Undo acts on the same frames even if new ones have since arrived. Only shifted
    ids are reversible; skipped-null and failed ids are never in the undo set.
    """
    shifted = list(result.shifted)
    return {
        "operation": "offset",
        "seconds": seconds,
        "shifted": len(result.shifted),
        "skipped_null": len(result.skipped_null),
        "failed": len(result.failed),
        "shifted_ids": shifted,
        "skipped_null_ids": list(result.skipped_null),
        "failed_ids": list(result.failed),
        # The inverse offset over the exact shifted id-set; seconds negated so the
        # replay returns those frames to their original capture times.
        "undo": {
            "operation": "offset",
            "frame_ids": shifted,
            "seconds": -seconds,
        },
    }


def _offset_error_summary(message: str) -> dict[str, Any]:
    """Shape an error-state offset response (nothing applied)."""
    return {
        "operation": "offset",
        "seconds": 0,
        "shifted": 0,
        "skipped_null": 0,
        "failed": 0,
        "shifted_ids": [],
        "skipped_null_ids": [],
        "failed_ids": [],
        "undo": None,
        "error": message,
    }


@router.post("/frames/offset")
def frames_offset(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    form: FormDep,
) -> Response:
    """Shift the capture timestamp of an explicit frame set by a signed offset.

    Body (``application/x-www-form-urlencoded``):

    * ``frame_ids`` -- the selected ids, comma-joined (e.g. ``11,12,13``), exactly
      as ``POST /frames/bulk`` takes them;
    * ``seconds`` -- a signed integer offset; negative shifts the frames earlier.

    **Ids only -- a range descriptor is deliberately not accepted here.** Offset
    mutates the very axis a time-range selection is defined by, so resolving a
    descriptor at apply time would be self-referential (the frames it selects
    change as their timestamps move). The client must pin a range to a concrete
    id-set first via ``POST /frames/range/materialize`` and then send those ids,
    so this route only ever sees an explicit, frozen selection.

    Operator-gated and CSRF-protected like every other frame mutation. The
    response is JSON -- not the action-bar HTML partial the lifecycle bulk endpoint
    returns -- because the offset summary carries fields that partial cannot
    express (a signed ``seconds``, a separate skipped-null count, and an Undo that
    must replay ``-seconds``); the offset panel is a custom client handler that
    consumes this JSON to render its result bar and wire its inverse-offset Undo.

    Frames with a null capture timestamp are reported separately as skipped (they
    have no time to move); unknown ids are reported as failed. A selection larger
    than the synchronous ceiling is rejected, like the bulk endpoint, to avoid
    holding a write transaction too long. Undo is the inverse offset over the
    shifted id-set, never a restore.

    An ``Idempotency-Key`` header makes a repeat of the same request (a double
    click, a retry after a dropped connection) replay the first response instead
    of shifting the frames again (see :mod:`..idempotency`).
    """
    return run_idempotent(
        request,
        user_id=user.id,
        route="frames.offset",
        form=form,
        handler=lambda: _offset_frames(db, user.id, form),
    )


def _offset_frames(db: DbSession, user_id: int, form: dict[str, str]) -> Response:
    """Validate and apply one offset request; the body of :func:`frames_offset`.

    Commits before returning a success, so a stored idempotent replay never
    reports a shift that a later commit failure rolled back.
    """
    raw_seconds = form.get("seconds", "")
    try:
        seconds = int(raw_seconds)
    except (TypeError, ValueError):
        return JSONResponse(
            _offset_error_summary("seconds must be a signed integer."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        frame_ids = _parse_frame_ids(form.get("frame_ids", ""))
    except ValueError:
        return JSONResponse(
            _offset_error_summary("Invalid frame selection."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not frame_ids:
        return JSONResponse(
            _offset_error_summary("No frames selected."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if len(frame_ids) > _BULK_SYNC_MAX:
        return JSONResponse(
            _offset_error_summary(
                f"Too many frames ({len(frame_ids)}). Offset at most "
                f"{_BULK_SYNC_MAX} at once, or narrow the selection."
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    from ...storage import frames as frame_service

    result = frame_service.offset_timestamps_many(db, frame_ids, seconds, user_id)
    db.commit()
    return JSONResponse(_build_offset_summary(seconds, result))


# --- async export endpoints -------------------------------------------------
#
# Export bundles a selection of frame images into a downloadable zip. Unlike the
# four synchronous lifecycle mutations, it is asynchronous: the POST enqueues a
# ``kind="export"`` RenderJob onto the shared render queue and returns a job
# handle the client polls. The work runs on the bounded render worker (the same
# atomic-claim, bounded-concurrency, orphan-reclaim substrate a render uses), so
# a large export never blocks the request or starves capture.


def _export_job_status(job: RenderJob) -> dict[str, Any]:
    """Shape an export job's row into the poll response the client renders.

    ``status`` reuses the render-job lifecycle (``pending``/``encoding``/
    ``done``/``failed``); the client maps ``pending``/``encoding`` to "Preparing…"
    and ``done`` to "Ready · Download". ``progress`` is coarse and derived from
    ``status`` -- the worker records only terminal status, not incremental
    progress, so there is no finer signal to surface than 0/50/100. ``frame_count``
    is the size of the requested selection (the ids the job carries), reported so
    the bar can show "Preparing N frames…" before the zip is built.
    """
    from ...render.export import export_frame_ids

    progress = {"pending": 0, "encoding": 50, "done": 100, "failed": 0}.get(
        job.status, 0
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "progress": progress,
        "frame_count": len(export_frame_ids(job)),
        "ready": job.status == "done" and bool(job.output_file_path),
    }


def _export_ids_from_form(db: DbSession, form: dict[str, str]) -> list[int] | str:
    """Resolve the export selection from the form, or an error message string.

    Accepts the same two mutually-exclusive inputs as ``POST /frames/bulk``:
    explicit ``frame_ids`` (comma-joined) or a JSON ``descriptor`` resolved
    server-side. Both present, or neither, is an error. A malformed descriptor or
    a missing project surfaces as the returned message. The result is the ordered
    id-list the export job will bundle; an empty selection is reported by the
    caller.
    """
    raw_descriptor = form.get("descriptor", "")
    raw_frame_ids = form.get("frame_ids", "")
    if raw_descriptor and raw_frame_ids:
        return "Provide either an explicit selection or a range, not both."
    if raw_descriptor:
        resolved = _resolve_bulk_descriptor(db, raw_descriptor)
        return resolved  # list[int] on success, str (message) on error
    try:
        return _parse_frame_ids(raw_frame_ids)
    except ValueError:
        return "Invalid frame selection."


@router.post("/frames/export")
def frames_export(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    form: FormDep,
) -> Response:
    """Enqueue an async export of a frame selection; return its job handle.

    Body (``application/x-www-form-urlencoded``):

    * exactly one of:
      * ``frame_ids`` -- the selected ids, comma-joined (e.g. ``11,12,13``); or
      * ``descriptor`` -- a JSON range descriptor resolved server-side to an
        id-set (the "select all in range / in project" path). Both present, or
        neither, is rejected.

    The selection is pinned to a concrete id-set at enqueue time and stored on the
    job, so the produced zip reflects exactly the frames selected when the request
    was made even if new frames arrive while it builds. A ``kind="export"``
    RenderJob is created ``pending`` and the render worker is notified; the
    response is the job handle ``{"job_id": N, "status": "pending"}`` the client
    polls via ``GET /frames/export/{job_id}``.

    Operator-gated and CSRF-protected like every other frame mutation. A selection
    larger than the synchronous bulk ceiling is still accepted here -- export is
    asynchronous by design, so a large bundle is the expected case, not a rejected
    one. The single cross-project constraint is that every id must belong to one
    project (the descriptor is single-project; an explicit id-list is validated to
    a single project below) so the zip has one render root.

    An ``Idempotency-Key`` header makes a repeat of the same request replay the
    first job handle instead of enqueueing a second export (see
    :mod:`..idempotency`).
    """
    return run_idempotent(
        request,
        user_id=user.id,
        route="frames.export",
        form=form,
        handler=lambda: _enqueue_export(db, user, form),
    )


def _enqueue_export(db: DbSession, user: User, form: dict[str, str]) -> Response:
    """Validate and enqueue one export request; the body of :func:`frames_export`.

    Commits before returning the job handle (and before waking the worker), so a
    stored idempotent replay always names a job that exists.
    """
    resolved = _export_ids_from_form(db, form)
    if isinstance(resolved, str):
        return JSONResponse(
            {"error": resolved}, status_code=status.HTTP_400_BAD_REQUEST
        )
    frame_ids = resolved
    if not frame_ids:
        return JSONResponse(
            {"error": "No frames selected."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # An explicit id list obeys the same ceiling as a materialised range (#96).
    # A descriptor is resolved server-side and is not capped: a whole-project
    # export is the expected large case.
    limit = get_context().settings.frames.max_range_ids
    if not form.get("descriptor") and len(frame_ids) > limit:
        return JSONResponse(
            {"error": "Selection too large; narrow the selection."},
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    project_id = _single_project_for_ids(db, frame_ids)
    if project_id is None:
        return JSONResponse(
            {"error": "Selection must belong to a single project."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # The service inserts the pending export and audits who asked for it (the
    # export card's "started by"); commit before waking the worker.
    result = render_service.enqueue_export(
        db, Actor.of(user), project_id, frame_ids=frame_ids
    )
    job_id = result.value.id
    db.commit()
    run_effects(result.effects, get_context())

    return JSONResponse(
        {"job_id": job_id, "status": "pending"},
        status_code=status.HTTP_202_ACCEPTED,
    )


def _single_project_for_ids(db: DbSession, frame_ids: list[int]) -> int | None:
    """Return the one project all ``frame_ids`` belong to, or ``None``.

    An export produces one zip under one project's render root, so the selection
    must be single-project. Returns the shared project id when every listed frame
    exists and shares it; returns ``None`` when the ids span projects or none of
    them resolve to a real frame (an export of nothing has no project).
    """
    rows = distinct_project_ids(db, frame_ids)
    if len(rows) != 1:
        return None
    return rows[0]


@router.get("/frames/export/{job_id}")
def frames_export_status(
    db: DbDep,
    user: OperatorUser,
    job_id: int,
) -> Response:
    """Return an export job's status as JSON for the client's progress poll.

    ``{"job_id", "status", "progress", "frame_count", "ready"}`` -- the client
    polls this every couple of seconds while ``status`` is ``pending`` or
    ``encoding`` ("Preparing…") and switches to "Ready · Download" once ``ready``
    is true. A non-export job id, or an unknown one, is a 404 so this endpoint
    never reports on a render through the export surface. Operator-gated to match
    the POST that created the job.
    """
    job = db.get(RenderJob, job_id)
    if job is None or job.kind != "export":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="export not found"
        )
    return JSONResponse(_export_job_status(job))


@router.get("/frames/export/{job_id}/download")
def frames_export_download(
    db: DbDep,
    user: OperatorUser,
    job_id: int,
) -> Response:
    """Serve a finished export's zip, or 404 if it is not ready or missing.

    The job must be a ``done`` export with a produced output path that resolves
    inside its project's render root -- the same anti-escape containment guard the
    render download uses, so a tampered path can never read a file elsewhere on
    disk. A still-running, failed, or unknown export is a 404. The zip is served
    as an attachment with a stable filename. Operator-gated like the rest of the
    export surface.
    """
    import os.path

    from ...render import project_render_root

    job = db.get(RenderJob, job_id)
    if job is None or job.kind != "export":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="export not found"
        )
    if job.status != "done" or not job.output_file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="export is not ready"
        )
    project = _get_project_or_404(db, job.project_id)
    root = project_render_root(_settings(), project).resolve()
    resolved = Path(os.path.realpath(job.output_file_path))
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="export is unavailable"
        )
    return FileResponse(
        str(resolved),
        media_type="application/zip",
        filename=f"export-{job_id}.zip",
    )


@router.get("/frames/export/{job_id}/card", response_class=HTMLResponse)
def frames_export_card(
    request: Request, db: DbDep, user: CurrentUser, job_id: int
) -> Response:
    """The export card: status, who started it, frame window, zip, failure.

    Opened from the project Frames tab's Exports list in the same dialog as the
    render card. Every role may open it; Download and Delete appear only for
    operators and admins, matching the operator-gated export download route.
    """
    job = db.get(RenderJob, job_id)
    if job is None or job.kind != "export":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="export not found"
        )
    return card_response(request, db, user, job, template="_partials/export_card.html")


@router.post("/frames/export/{job_id}/delete")
async def frames_export_delete(
    request: Request, db: DbDep, user: OperatorUser, job_id: int, form: FormDep
) -> Response:
    """Delete a frame export's zip and row (cancelling it first if running).

    Same rules as deleting a render: the zip is removed only inside the project's
    render root, a missing zip is not an error, and the delete is audited.
    """
    return await delete_response(
        request, db, user, job_id, kind="export", next_url=form.get("next")
    )


@router.post("/projects/{project_id}/frames/{frame_id}/soft-delete")
def frame_soft_delete(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    frame_id: int,
    all_projects: int = 0,
    drawer: int = 0,
) -> Response:
    """Soft-delete a frame and re-render its tile (or the drawer body)."""
    frame = frame_service.soft_delete_frame(
        db, Actor.of(user), project_id, frame_id
    ).value
    if drawer:
        return _frame_drawer_mutation_response(request, db, user, project_id, frame)
    return _frame_tile_response(
        request, db, user, project_id, frame, all_projects=bool(all_projects)
    )


@router.post("/projects/{project_id}/frames/{frame_id}/restore")
def frame_restore(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    frame_id: int,
    all_projects: int = 0,
    drawer: int = 0,
) -> Response:
    """Restore a soft-deleted frame and re-render its tile (or the drawer body)."""
    frame = frame_service.restore_frame(db, Actor.of(user), project_id, frame_id).value
    if drawer:
        return _frame_drawer_mutation_response(request, db, user, project_id, frame)
    return _frame_tile_response(
        request, db, user, project_id, frame, all_projects=bool(all_projects)
    )


@router.post("/projects/{project_id}/frames/{frame_id}/exclude")
def frame_exclude(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    frame_id: int,
    all_projects: int = 0,
    drawer: int = 0,
) -> Response:
    """Exclude a frame from renders and re-render its tile (or the drawer body).

    The frame stays visible in the browser; only the encoder skips it. Mirrors
    the soft-delete/restore response shape: a ``?drawer=1`` request gets the
    drawer body plus an out-of-band tile update, otherwise just the tile.
    """
    frame = frame_service.exclude_frame(db, Actor.of(user), project_id, frame_id).value
    if drawer:
        return _frame_drawer_mutation_response(request, db, user, project_id, frame)
    return _frame_tile_response(
        request, db, user, project_id, frame, all_projects=bool(all_projects)
    )


@router.post("/projects/{project_id}/frames/{frame_id}/include")
def frame_include(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    frame_id: int,
    all_projects: int = 0,
    drawer: int = 0,
) -> Response:
    """Return an excluded frame to renders and re-render its tile (or drawer)."""
    frame = frame_service.include_frame(db, Actor.of(user), project_id, frame_id).value
    if drawer:
        return _frame_drawer_mutation_response(request, db, user, project_id, frame)
    return _frame_tile_response(
        request, db, user, project_id, frame, all_projects=bool(all_projects)
    )


@router.patch("/projects/{project_id}/frames/{frame_id}")
def frame_edit(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    frame_id: int,
    form: FormDep,
    drawer: int = 0,
) -> Response:
    """Correct a frame's capture timestamp and re-render its tile (or drawer row).

    A naive ``capture_timestamp`` (the ``datetime-local`` input) is read in the
    viewer's zone, as the jump boxes and milestone times are (#124); an
    offset-aware value keeps its own offset. Storage normalises to naive UTC.
    """
    raw = form.get("capture_timestamp", "")
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="capture_timestamp must be ISO-8601",
        ) from exc
    capture_timestamp = deps.viewer_local_to_utc(parsed, user.viewer_timezone)
    frame = frame_service.edit_frame_timestamp(
        db, Actor.of(user), project_id, frame_id, capture_timestamp
    ).value
    if drawer:
        db.flush()
        return templates.TemplateResponse(
            request,
            "_partials/frame_timestamp_row.html",
            deps.base_context(
                request,
                db,
                user,
                project_id=project_id,
                frame=_frame_drawer_view(frame),
            ),
        )
    return _frame_tile_response(request, db, user, project_id, frame)


def _frame_drawer_mutation_response(
    request: Request,
    db: DbSession,
    user: User,
    project_id: int,
    frame: Frame,
) -> Response:
    """Re-render the drawer body for a frame after a lifecycle mutation.

    The response swaps the same frame's drawer body into the open drawer (so its
    action footer reflects the new active/deleted state) and carries an
    out-of-band re-render of the underlying grid tile, so the frame's tile in the
    page behind the drawer updates in the same round-trip.
    """
    db.flush()
    context = _frame_drawer_context(db, project_id, frame.id)
    context["oob_tile"] = True
    return templates.TemplateResponse(
        request,
        "_partials/drawer_frame_detail.html",
        deps.base_context(request, db, user, **context),
    )


def _frame_tile_response(
    request: Request,
    db: DbSession,
    user: User,
    project_id: int,
    frame: Any,
    *,
    all_projects: bool = False,
) -> Response:
    """Render the single-frame tile fragment after a lifecycle mutation.

    When the tile lives in the cross-project "All Projects" grid
    (``all_projects``), the project name is resolved so the swapped-in tile keeps
    its project label; a single-project grid leaves it unset (no label).
    """
    db.flush()
    project_name: str | None = None
    if all_projects:
        project_name = db.execute(
            select(Project.name).where(Project.id == frame.project_id)
        ).scalar_one_or_none()
    view = _FrameView(
        id=frame.id,
        sequence_index=frame.sequence_index,
        capture_timestamp=_fmt_dt(frame.capture_timestamp),
        capture_timestamp_raw=frame.capture_timestamp,
        # Full-size image route (CSS scales the tile); thumbnails are deferred.
        thumbnail_url=f"/projects/{project_id}/frames/{frame.id}/thumbnail",
        lifecycle_state=frame.lifecycle_state,
        excluded_at=frame.excluded_at,
        project_id=frame.project_id,
        project_name=project_name,
    )
    return templates.TemplateResponse(
        request,
        "_partials/frame_tile.html",
        deps.base_context(request, db, user, frame=view, project={"id": project_id}),
    )


# --- batch frame import (multipart upload) ----------------------------------
#
# Importing a batch of existing image files is a multipart/form-data upload. The
# rest of the web layer parses application/x-www-form-urlencoded by hand (the
# optional multipart dependency is intentionally absent); the import body is read
# as a stream by :func:`..multipart.stream_multipart`, which writes each file to a
# spool directory as it arrives, so the batch is never held in memory. The CSRF
# token rides in the X-CSRF-Token header (added to every HTMX request by csrf.js
# from the csrf-token meta tag); a no-JS submit carries it as the *first*
# ``csrf_token`` part, which the CSRF middleware checks before any file byte is
# read (the form template pins that order).

# Upper bound on the whole multipart request body, independent of the per-file
# count cap the storage importer enforces. An absurd Content-Length is rejected up
# front; the streaming reader enforces the caps on the bytes actually received.
_MAX_IMPORT_REQUEST_BYTES = 512 * 1024 * 1024


def _import_result_response(
    request: Request,
    db: DbSession,
    user: User,
    *,
    result: Any = None,
    error: str | None = None,
) -> Response:
    """Render the import-result fragment (HTTP 200 so HTMX always swaps it).

    A success carries the storage ``ImportResult``; an error (over-size batch or
    empty selection) carries a message instead. Both return 200 so the inline
    fragment swaps into ``#frames-import-result`` -- the repo's inline-error
    convention, since HTMX does not swap a non-2xx response without extra wiring.

    Synchronous and session-reading (the CSRF token comes from the session row),
    so :func:`frames_import` awaits it through the threadpool (#49).
    """
    return templates.TemplateResponse(
        request,
        "_partials/frames_import_result.html",
        deps.base_context(request, db, user, result=result, error=error),
    )


@router.post(
    "/projects/{project_id}/frames/import",
    response_class=HTMLResponse,
)
async def frames_import(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
) -> Response:
    """Import a batch of image files as uploaded frames for the project.

    Body: ``multipart/form-data`` with one or more ``files`` parts (the field
    name is ``files``, repeated per file) and the per-session CSRF token: the
    ``X-CSRF-Token`` header (HTMX adds it automatically) or, for a no-JS submit,
    the leading ``csrf_token`` part. The CSRF middleware verifies it before any
    file byte is read. The files are streamed to a spool directory as they
    arrive (never buffered) and imported on the jobs executor; the caller's
    access is re-checked live once the body is in.

    Operator/admin-gated like every other frame mutation. Each file's bytes are
    validated and its Exif capture time read by the storage importer; a file with
    no readable time falls back to now (naive UTC) and is flagged inferred. A time
    without a UTC offset of its own is read in the optional ``exif_timezone`` field
    (an IANA name; an unknown one is an inline error), else in the project's
    schedule timezone. The whole project is then re-sequenced chronologically.
    The size and file-count caps are enforced while the body streams in (an
    over-size batch returns an inline error); an absurd body is rejected up
    front by ``Content-Length``.

    The response is the ``#frames-import-result`` fragment -- "{n} imported" plus
    any skipped files with reasons -- returned at HTTP 200 (success or error
    alike) so HTMX swaps it into the result container.
    """
    # The request's Session is only ever used off the event loop (#49): the
    # project lookup, the transaction release and every result render (which
    # reads the session row for the CSRF token) run in the threadpool, while the
    # body read stays on the loop and the import itself runs in a worker thread.
    await run_in_threadpool(_get_project_or_404, db, project_id)

    content_length = request.headers.get("content-length")
    declared = 0
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > _MAX_IMPORT_REQUEST_BYTES:
            return await run_in_threadpool(
                _import_result_response,
                request,
                db,
                user,
                error="The upload is too large. Import fewer files at once.",
            )

    context = get_context()
    settings = context.settings
    # Release this request's transaction before the (possibly slow) body read and
    # before the importer runs. The importer writes from a worker thread on its
    # own connection, and SQLite (even in WAL) allows only one writer at a time;
    # this request's session would hold the writer slot until it commits. Nothing
    # is pending, and the session re-opens a fresh transaction when the result
    # fragment renders.
    await run_in_threadpool(db.commit)
    try:
        async with upload_spool(
            settings.paths.data_dir,
            expected_bytes=max(declared, 0),
            low_watermark_bytes=settings.storage.low_watermark_bytes,
        ) as spool:
            form = await stream_multipart(
                request,
                spool_dir=spool.path,
                file_field="files",
                text_fields=frozenset({"csrf_token", "exif_timezone"}),
                max_file_bytes=settings.capture.max_image_bytes,
                max_total_bytes=min(
                    _MAX_IMPORT_REQUEST_BYTES, settings.server.max_request_body_bytes
                ),
                max_files=frame_service.MAX_IMPORT_BATCH,
                low_watermark_bytes=settings.storage.low_watermark_bytes,
            )
            files = [(part.filename, part.path) for part in form.files if part.path]
            if not files:
                return await run_in_threadpool(
                    _import_result_response,
                    request,
                    db,
                    user,
                    error="No files were selected to import.",
                )
            # The dependency authorised before the body; a revoke, disable or
            # demotion can land while it streams in (#207), so re-check live
            # now. The actor comes from the live row, inside the hop.
            actor = await run_in_threadpool(
                _reauthorize_actor, request, db, settings.session
            )
            fallback = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
            # The worker owns the spool from here and removes it, so a client
            # that disconnects mid-import neither loses the import nor leaves
            # files behind.
            result = await run_in(
                context.jobs_executor,
                _import_from_spool,
                context.session_factory,
                settings,
                actor,
                project_id,
                spool,
                files,
                fallback,
                form.fields.get("exif_timezone"),
            )
    except BodyTooLargeError:
        error = "The upload is too large. Import fewer files at once."
    except ValidationError as exc:
        error = str(exc)
    except (TooManyFilesError, frame_service.ImportBatchTooLargeError):
        error = (
            f"import batch exceeds the maximum of {frame_service.MAX_IMPORT_BATCH} "
            "files; import fewer files at once"
        )
    except MultipartError:
        error = "The upload could not be read. Reload the page and try again."
    except InsufficientSpoolSpaceError:
        error = "There is not enough free disk space to accept this upload."
    else:
        return await run_in_threadpool(
            _import_result_response, request, db, user, result=result.value
        )
    return await run_in_threadpool(
        _import_result_response, request, db, user, error=error
    )


def _reauthorize_actor(
    request: Request, db: DbSession, session_settings: SessionSettings
) -> Actor:
    """Re-check the caller's access after the body; return the live actor.

    Synchronous, for a threadpool hop (the session read stays off the loop).
    The role comes from the live user row, never from the dependency's earlier
    answer, so a demoted operator cannot finish an upload begun as one.

    :raises HTTPException: ``401`` / ``403`` when access was lost meanwhile.
    """
    user = reauthorize_after_body(
        request,
        db,
        settings=session_settings,
        allowed_roles=(deps.OPERATOR_ROLE, deps.ADMIN_ROLE),
    )
    if user is None:  # a cookie-authenticated route always has a session
        raise HTTPException(status_code=401, detail="Authentication required.")
    return Actor.of(user)


def _import_from_spool(
    session_factory: sessionmaker[DbSession],
    settings: Settings,
    actor: Actor,
    project_id: int,
    spool: UploadSpool,
    files: list[tuple[str, Path]],
    fallback: datetime.datetime,
    exif_timezone: str | None = None,
) -> Result[ImportResult]:
    """Import the spooled files; always removes the spool directory.

    Synchronous, for the jobs executor.
    """
    spool.claim_or_raise()
    try:
        return frame_service.import_frames(
            session_factory,
            settings,
            actor,
            project_id,
            files,
            fallback,
            exif_timezone,
        )
    finally:
        spool.finish()
