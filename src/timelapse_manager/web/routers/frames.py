"""Frame routes: the frame browser plus per-project frame image, thumbnail,
detail, metadata, edit, soft-delete, and restore."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ...db.models import Frame, Project, RenderJob, User
from ...runtime import get_context
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ._shared import (
    _get_project_or_404,
    _settings,
)
from ._viewmodels import (
    _dt_input,
    _fmt_bytes,
    _fmt_dt,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_FRAMES_PER_PAGE = 60


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
    # Prefill value for the inline timestamp editor's datetime-local input.
    capture_timestamp_input: str = ""

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
    project_id: int | None = None,
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
    anchor = _parse_at(at)

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

    frame_times = frame_service.list_active_frame_times(db, project_id)
    if len(frame_times) < 2:
        return None, None
    now = datetime.datetime.now(datetime.UTC)
    aware_times = [_aware(t) for t in frame_times]
    span_start, span_end = _jump_span(project, aware_times, now)
    gaps = frame_service.find_capture_gaps(aware_times, span_start, span_end)
    if not gaps:
        return None, None
    # No explicit anchor: step relative to the newest captured frame.
    ref = _aware(anchor) if anchor is not None else aware_times[-1]
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


@router.get("/frames/batch", response_class=HTMLResponse)
def frames_batch(
    request: Request,
    db: DbDep,
    user: CurrentUser,
    project_id: int | None = None,
    before: int | None = None,
    show_deleted: int = 0,
) -> Response:
    """Return one continuous-scroll batch of frame tiles + a fresh sentinel.

    The sentinel (a real ``<a>`` that doubles as the no-JS pagination link)
    carries ``hx-trigger="revealed"`` and swaps itself out for the next batch, so
    scrolling appends older frames; the end-cap replaces it at the series start.

    With a ``project_id`` the batch is one project paged on ``sequence_index``;
    without one it is the cross-project "All Projects" batch paged on frame id.
    The presence of ``project_id`` is the sole mode discriminator.
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
        ),
    )


@router.get("/frames/since")
def frames_since(
    db: DbDep,
    user: CurrentUser,
    after: int,
    project_id: int | None = None,
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
    resolve inside the project's own frame directory -- a corrupt or hostile path
    (traversal, absolute escape, or symlink) is treated as not found. The path is
    derived solely from the stored ``file_path``; no client string reaches the
    filesystem. Shared by the full-image and thumbnail routes.
    """
    import os.path

    from ...storage import paths

    frame = db.get(Frame, frame_id)
    if frame is None or frame.project_id != project_id or frame.file_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    project = _get_project_or_404(db, project_id)
    settings = _settings()
    resolved = paths.resolve_absolute(settings, project_id, frame.file_path)

    # Containment boundary is the project's own frame directory, which is the
    # frames root for default layout and the explicit storage_path otherwise --
    # so custom-storage frames (legitimately outside the frames root) still pass.
    boundary = os.path.realpath(paths.frame_dir(settings, project))
    real_path = os.path.realpath(resolved)
    if not (real_path == boundary or real_path.startswith(boundary + os.sep)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return real_path


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
    import os.path

    real_path = _resolve_frame_real_path(db, project_id, frame_id)
    media_type = _IMAGE_MEDIA_TYPES.get(
        os.path.splitext(real_path)[1].lower(), "application/octet-stream"
    )
    # Frame bytes never change once written, so they are cacheable; kept private
    # because the route is gated behind authentication.
    return FileResponse(
        real_path,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


# Width (px) of generated thumbnails; height is auto to preserve aspect ratio.
_THUMBNAIL_WIDTH = 320


@router.get("/projects/{project_id}/frames/{frame_id}/thumbnail")
def frame_thumbnail(
    db: DbDep, user: CurrentUser, project_id: int, frame_id: int
) -> Response:
    """Serve a small, cached thumbnail of a frame (any authenticated user).

    The source frame is resolved through the same anti-IDOR + containment guard
    as the full image. A downscaled JPEG is generated once with the bundled
    ffmpeg into a per-project cache under the data directory and reused on
    subsequent requests; if generation fails the full image is served as a
    graceful fallback. Local files only -- no SSRF surface.
    """
    import os.path
    import subprocess

    from ...encode.thumbnail import generate_thumbnail
    from ...storage import paths

    real_path = _resolve_frame_real_path(db, project_id, frame_id)

    settings = _settings()
    cache_dir = paths.thumbnail_cache_dir(settings, project_id)
    thumb_path = cache_dir / f"{frame_id}.jpg"

    # Regenerate when missing or older than the source frame. Generation is an
    # ffmpeg subprocess isolated in the encode layer; on any failure fall back to
    # the full-size image so the UI still shows something.
    needs_build = (
        not thumb_path.is_file()
        or thumb_path.stat().st_mtime < os.path.getmtime(real_path)
    )
    if needs_build:
        try:
            generate_thumbnail(
                get_context().ffmpeg_path,
                real_path,
                thumb_path,
                width=_THUMBNAIL_WIDTH,
            )
        except (subprocess.SubprocessError, OSError):
            return frame_image(db, user, project_id, frame_id)

    return FileResponse(
        str(thumb_path),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


def _frame_metadata_context(
    db: DbSession, project_id: int, frame_id: int
) -> dict[str, Any]:
    """Build the scene-metadata view context for a frame, or raise 404.

    The frame must belong to ``project_id`` (anti-IDOR). The raw scene-metadata
    envelope is run through the display normalizer here so the templates stay
    dumb. An absent or empty envelope yields ``scene_groups=[]`` and
    ``schema_version=None`` -- the template's null state, not an error. Shared by
    the HTMX panel endpoint and the no-JS full-page fallback.
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


@router.get(
    "/projects/{project_id}/frames/{frame_id}/metadata",
    response_class=HTMLResponse,
)
def frame_metadata(
    request: Request, db: DbDep, user: CurrentUser, project_id: int, frame_id: int
) -> Response:
    """Return a frame's scene-metadata panel fragment (any authenticated user).

    Viewing metadata is a read, so any logged-in role may fetch it. The frame is
    required to belong to ``project_id`` (a mismatch is a 404). A frame with no
    recorded scene metadata renders the partial's null state -- that is normal,
    not an error.
    """
    return templates.TemplateResponse(
        request,
        "_partials/frame_metadata.html",
        deps.base_context(
            request, db, user, **_frame_metadata_context(db, project_id, frame_id)
        ),
    )


@router.get("/projects/{project_id}/frames/{frame_id}", response_class=HTMLResponse)
def frame_detail(
    request: Request, db: DbDep, user: CurrentUser, project_id: int, frame_id: int
) -> Response:
    """Render a frame's scene metadata as a full page (no-JS fallback).

    The frame tile's Info link is HTMX-enhanced to swap the metadata panel in
    place, but its plain ``href`` points here so the feature still works without
    JavaScript. Same context and 404 contract as the HTMX panel endpoint, wrapped
    in the app's base layout with a link back to the frame browser.
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
        capture_timestamp_input=_dt_input(frame.capture_timestamp),
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
    ``/image``, ``/thumbnail``, and ``/metadata`` suffixes; a path parameter never
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
    and a malformed descriptor is a 400.
    """
    from ...storage import frame_selection

    descriptor = _descriptor_from_form(db, form)
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

    result = frame_service.offset_timestamps_many(db, frame_ids, seconds, user.id)
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

    project_id = _single_project_for_ids(db, frame_ids)
    if project_id is None:
        return JSONResponse(
            {"error": "Selection must belong to a single project."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    job = RenderJob(
        project_id=project_id,
        kind="export",
        status="pending",
        output_settings={"frame_ids": frame_ids},
    )
    db.add(job)
    db.flush()
    job_id = job.id
    db.commit()

    queue = get_context().render_queue
    if queue is not None:
        queue.notify()

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
    rows = (
        db.execute(select(Frame.project_id).where(Frame.id.in_(frame_ids)).distinct())
        .scalars()
        .all()
    )
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
    from ...storage import frames as frame_service

    _assert_frame_in_project(db, project_id, frame_id)
    frame = frame_service.soft_delete(db, frame_id, user.id)
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
    from ...storage import frames as frame_service

    _assert_frame_in_project(db, project_id, frame_id)
    frame = frame_service.restore(db, frame_id, user.id)
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
    from ...storage import frames as frame_service

    _assert_frame_in_project(db, project_id, frame_id)
    frame = frame_service.exclude(db, frame_id, user.id)
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
    from ...storage import frames as frame_service

    _assert_frame_in_project(db, project_id, frame_id)
    frame = frame_service.include(db, frame_id, user.id)
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
    """Correct a frame's capture timestamp and re-render its tile (or drawer row)."""
    from ...storage import frames as frame_service

    raw = form.get("capture_timestamp", "")
    try:
        capture_timestamp = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="capture_timestamp must be ISO-8601",
        ) from exc
    _assert_frame_in_project(db, project_id, frame_id)
    frame = frame_service.edit_capture_timestamp(
        db, frame_id, capture_timestamp, user.id
    )
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


def _assert_frame_in_project(db: DbSession, project_id: int, frame_id: int) -> None:
    """Raise 404 unless ``frame_id`` belongs to ``project_id``."""
    frame = db.get(Frame, frame_id)
    if frame is None or frame.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


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
# rest of the web layer only ever parses application/x-www-form-urlencoded by
# hand (the optional multipart dependency is intentionally absent), so this is
# the single place that decodes a multipart body, kept minimal: it pulls only the
# named file parts and ignores everything else. The CSRF token rides in the
# X-CSRF-Token header (injected on every HTMX request by hx-headers on <body>),
# so the middleware verifies it without needing to read the multipart body.

# Upper bound on the whole multipart request body, independent of the per-file
# count cap the storage importer enforces. The body is read fully into memory to
# parse it, so an absurd Content-Length is rejected up front rather than buffered.
_MAX_IMPORT_REQUEST_BYTES = 512 * 1024 * 1024


def _parse_multipart_files(
    body: bytes, content_type: str, field: str
) -> list[tuple[str, bytes]]:
    """Extract ``(filename, bytes)`` for every ``field`` part of a multipart body.

    A deliberately small multipart/form-data reader for the one upload route the
    web layer has -- the project does not depend on a full multipart parser. Only
    parts whose ``Content-Disposition`` name equals ``field`` are returned; any
    other field (e.g. a redundant ``csrf_token``) is ignored. A part with no
    filename, or with empty bytes, is skipped so an empty file input does not
    become a zero-byte "file". Returns an empty list when the boundary is missing
    or no matching part is present.
    """
    boundary = _multipart_boundary(content_type)
    if boundary is None:
        return []
    delimiter = b"--" + boundary
    files: list[tuple[str, bytes]] = []
    # Split on the delimiter; the first chunk is the preamble and the last is the
    # closing "--\r\n" epilogue, both discarded by the header/blank-line check.
    for segment in body.split(delimiter):
        if not segment or segment in (b"--\r\n", b"--", b"\r\n"):
            continue
        segment = segment[2:] if segment.startswith(b"\r\n") else segment
        header_blob, sep, content = segment.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = _parse_part_headers(header_blob)
        disposition = headers.get("content-disposition", "")
        name = _disposition_param(disposition, "name")
        if name != field:
            continue
        filename = _disposition_param(disposition, "filename")
        if not filename:
            continue
        # Trim the trailing CRLF the multipart framing appends after the content.
        if content.endswith(b"\r\n"):
            content = content[:-2]
        if not content:
            continue
        files.append((filename, content))
    return files


def _multipart_boundary(content_type: str) -> bytes | None:
    """Return the boundary token from a multipart Content-Type, or ``None``."""
    if not content_type.startswith("multipart/form-data"):
        return None
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "boundary":
            return value.strip().strip('"').encode("latin-1")
    return None


def _parse_part_headers(header_blob: bytes) -> dict[str, str]:
    """Parse one multipart part's header block into a lowercased-key dict."""
    headers: dict[str, str] = {}
    for line in header_blob.split(b"\r\n"):
        if not line:
            continue
        key, _, value = line.partition(b":")
        headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return headers


def _disposition_param(disposition: str, param: str) -> str | None:
    """Return a ``Content-Disposition`` parameter value (e.g. ``name``), or None."""
    for piece in disposition.split(";"):
        key, _, value = piece.strip().partition("=")
        if key.strip().lower() == param:
            return value.strip().strip('"')
    return None


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
    name is ``files``, repeated per file) and the per-session CSRF token, which
    rides in the ``X-CSRF-Token`` header (HTMX adds it automatically) -- so the
    CSRF middleware verifies the request without reading the multipart body.

    Operator/admin-gated like every other frame mutation. Each file's bytes are
    validated and its Exif capture time read by the storage importer; a file with
    no readable time falls back to now (naive UTC) and is flagged inferred. The
    whole project is then re-sequenced chronologically. The per-file count cap is
    enforced by the importer (an over-size batch returns an inline error); an
    absurd request body is rejected up front by ``Content-Length``.

    The response is the ``#frames-import-result`` fragment -- "{n} imported" plus
    any skipped files with reasons -- returned at HTTP 200 (success or error
    alike) so HTMX swaps it into the result container.
    """
    from ...storage import frames as frame_service

    _get_project_or_404(db, project_id)

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_IMPORT_REQUEST_BYTES:
                return _import_result_response(
                    request,
                    db,
                    user,
                    error="The upload is too large. Import fewer files at once.",
                )
        except ValueError:
            pass

    body = await request.body()
    files = _parse_multipart_files(
        body, request.headers.get("content-type", ""), "files"
    )
    if not files:
        return _import_result_response(
            request, db, user, error="No files were selected to import."
        )

    context = get_context()
    fallback = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    # Release this request's transaction before the importer runs. The importer
    # writes from a worker thread on its own connection, and SQLite (even in WAL)
    # allows only one writer at a time; this request's session holds the writer
    # slot until it commits, so without this the threaded import would wait out
    # the busy timeout and fail. Committing here is safe -- nothing else is
    # pending -- and the session re-opens a fresh transaction when the result
    # fragment renders.
    db.commit()
    try:
        result = await asyncio.to_thread(
            frame_service.import_frames,
            context.session_factory,
            context.settings,
            project_id,
            files,
            fallback,
            user.id,
        )
    except frame_service.ImportBatchTooLargeError as exc:
        return _import_result_response(request, db, user, error=str(exc))

    return _import_result_response(request, db, user, result=result)
