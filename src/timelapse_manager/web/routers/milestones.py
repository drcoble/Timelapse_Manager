"""Milestone routes: the per-project chapter-marker manager on the project page.

A milestone is a named position in a project's timeline -- a frame index *or* a
timestamp -- that a render turns into a chapter. These routes drive the inline,
HTMX-swapped manager on the project detail page: an add/edit form fragment, an
inline delete confirmation, and a refreshed table after every mutation.

Every route is gated to operators and admins (a viewer is 403). Each create,
edit, and delete is attributed to the acting user on the audit event it writes --
the real signed-in operator, never a sentinel -- and the milestone's own
``user_id`` records who placed it. A validation error re-renders the form
fragment at HTTP 200 (so HTMX swaps it back in) with an inline message, matching
the camera form's inline-error convention.
"""

from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ...db.models import Milestone, Project, User
from .. import dependencies as deps
from ..dependencies import (
    DbDep,
    FormDep,
    OperatorUser,
    templates,
)
from ._shared import (
    _audit,
    _get_project_or_404,
    _parse_optional_datetime,
)
from ._viewmodels import _project_view

logger = logging.getLogger(__name__)

router = APIRouter()

# An upper bound on a milestone label, so a pasted blob cannot bloat a row. A
# label is a short human marker (e.g. "Foundation poured"); 200 characters is
# generous for that while keeping the stored value sane.
_LABEL_MAX_LENGTH = 200


class _MilestoneView:
    """A milestone projected onto exactly the fields the templates read.

    The timestamp is kept as a (naive-UTC) ``datetime`` -- not a pre-formatted
    string -- so the template can run it through the ``localdt`` filter for
    display and ``.isoformat()`` for a ``datetime-local`` edit prefill. A
    pre-stringified value would break both.
    """

    __slots__ = ("id", "label", "position_frame_index", "position_timestamp")

    def __init__(self, row: Milestone) -> None:
        self.id = row.id
        self.label = row.label
        self.position_frame_index = row.position_frame_index
        self.position_timestamp = row.position_timestamp


def milestone_views(db: DbSession, project_id: int) -> list[_MilestoneView]:
    """Return a project's milestones as view objects, in creation order.

    Shared by the project detail page (which seeds the initial table) and the
    table fragment this router returns after each mutation, so both render the
    same shape from one place.
    """
    rows = (
        db.execute(
            select(Milestone)
            .where(Milestone.project_id == project_id)
            .order_by(Milestone.id)
        )
        .scalars()
        .all()
    )
    return [_MilestoneView(row) for row in rows]


def _table_response(
    request: Request, db: DbSession, user: User, project: Project
) -> Response:
    """Render the refreshed milestone table fragment after a mutation."""
    return templates.TemplateResponse(
        request,
        "_partials/milestone_table.html",
        deps.base_context(
            request,
            db,
            user,
            project=_project_view(db, project),
            milestones=milestone_views(db, project.id),
        ),
    )


def _form_response(
    request: Request,
    db: DbSession,
    user: User,
    project: Project,
    milestone: _MilestoneView | None,
    *,
    error: str | None = None,
) -> Response:
    """Render the milestone add/edit form fragment.

    ``milestone`` is ``None`` for the add form and a view object for the edit
    form. A validation ``error`` is surfaced as an inline flash message; the
    fragment is always returned at HTTP 200 so HTMX swaps it back into the slot
    (it does not swap a 4xx), matching the camera form's inline-error idiom.
    """
    flash = [{"type": "error", "message": error}] if error is not None else None
    return templates.TemplateResponse(
        request,
        "_partials/milestone_form.html",
        deps.base_context(
            request,
            db,
            user,
            project=_project_view(db, project),
            milestone=milestone,
            flash_messages=flash,
        ),
    )


def _get_milestone_or_404(
    db: DbSession, project_id: int, milestone_id: int
) -> Milestone:
    """Return a milestone that belongs to the project, or raise a 404."""
    milestone = db.get(Milestone, milestone_id)
    if milestone is None or milestone.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"milestone {milestone_id} not found in project {project_id}",
        )
    return milestone


def _parse_milestone_form(
    form: dict[str, str],
) -> tuple[str | None, int | None, datetime.datetime | None, str | None]:
    """Validate the milestone form, returning ``(label, frame_index, ts, error)``.

    On success ``error`` is ``None`` and exactly one of ``frame_index`` /
    ``ts`` (a naive-UTC ``datetime``) is set, per the chosen position type. On
    failure the first three values are unspecified and ``error`` carries a
    friendly inline message. The label is required, trimmed, and length-capped.
    A frame index of ``0`` is valid (it is the first frame), so it is range-
    checked as ``>= 0`` rather than reusing the positive-only parser.
    """
    label = (form.get("milestone_label") or "").strip()
    if not label:
        return None, None, None, "A milestone needs a label."
    if len(label) > _LABEL_MAX_LENGTH:
        return (
            None,
            None,
            None,
            f"A milestone label must be {_LABEL_MAX_LENGTH} characters or fewer.",
        )

    position_type = (form.get("milestone_position_type") or "").strip()
    if position_type == "frame":
        raw = (form.get("milestone_frame_index") or "").strip()
        if not raw:
            return None, None, None, "Enter a frame index for this milestone."
        try:
            frame_index = int(raw)
        except ValueError:
            return None, None, None, "Frame index must be a whole number."
        if frame_index < 0:
            return None, None, None, "Frame index must be zero or greater."
        return label, frame_index, None, None
    if position_type == "time":
        timestamp, err = _parse_optional_datetime(
            form.get("milestone_timestamp"), "Timestamp"
        )
        if err is not None:
            return None, None, None, err
        if timestamp is None:
            return None, None, None, "Enter a date and time for this milestone."
        return label, None, timestamp, None
    return None, None, None, "Choose whether to place this milestone by frame or time."


@router.get("/projects/{project_id}/milestones/new", response_class=HTMLResponse)
def new_milestone_form(
    request: Request, db: DbDep, user: OperatorUser, project_id: int
) -> Response:
    """Render the empty add-milestone form fragment."""
    project = _get_project_or_404(db, project_id)
    return _form_response(request, db, user, project, milestone=None)


@router.post("/projects/{project_id}/milestones", response_class=HTMLResponse)
def create_milestone(
    request: Request, db: DbDep, user: OperatorUser, project_id: int, form: FormDep
) -> Response:
    """Create a milestone, attributed to the acting user, and refresh the table.

    A validation failure re-renders the add form with an inline message at HTTP
    200 so HTMX swaps it back. On success the milestone records the placing user
    and the audit event is attributed to that same real user (no sentinel), then
    the refreshed table fragment is returned.
    """
    project = _get_project_or_404(db, project_id)
    label, frame_index, timestamp, error = _parse_milestone_form(form)
    if error is not None:
        return _form_response(request, db, user, project, milestone=None, error=error)

    milestone = Milestone(
        project_id=project_id,
        user_id=user.id,
        label=label,
        position_frame_index=frame_index,
        position_timestamp=timestamp,
    )
    db.add(milestone)
    db.flush()
    _audit(
        db,
        scope="project",
        scope_id=project_id,
        actor_user_id=user.id,
        message=f"milestone {milestone.id} created",
    )
    db.flush()
    return _table_response(request, db, user, project)


@router.get(
    "/projects/{project_id}/milestones/{milestone_id}/edit",
    response_class=HTMLResponse,
)
def edit_milestone_form(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    milestone_id: int,
) -> Response:
    """Render the prefilled edit-milestone form fragment."""
    project = _get_project_or_404(db, project_id)
    milestone = _get_milestone_or_404(db, project_id, milestone_id)
    return _form_response(
        request, db, user, project, milestone=_MilestoneView(milestone)
    )


@router.post(
    "/projects/{project_id}/milestones/{milestone_id}/edit",
    response_class=HTMLResponse,
)
def edit_milestone(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    milestone_id: int,
    form: FormDep,
) -> Response:
    """Apply an edit to a milestone and return the refreshed table.

    A validation failure re-renders the edit form with an inline message at HTTP
    200. On success the label and position are replaced (the position type fully
    determines which of frame index / timestamp is set, the other cleared), the
    audit event is attributed to the acting user, and the refreshed table
    fragment is returned.
    """
    project = _get_project_or_404(db, project_id)
    milestone = _get_milestone_or_404(db, project_id, milestone_id)
    label, frame_index, timestamp, error = _parse_milestone_form(form)
    if error is not None:
        return _form_response(
            request, db, user, project, milestone=_MilestoneView(milestone), error=error
        )

    milestone.label = label
    milestone.position_frame_index = frame_index
    milestone.position_timestamp = timestamp
    _audit(
        db,
        scope="project",
        scope_id=project_id,
        actor_user_id=user.id,
        message=f"milestone {milestone_id} updated",
    )
    db.flush()
    return _table_response(request, db, user, project)


@router.get(
    "/projects/{project_id}/milestones/{milestone_id}/delete-confirm",
    response_class=HTMLResponse,
)
def delete_milestone_confirm(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    milestone_id: int,
) -> Response:
    """Inline confirmation row for deleting a milestone."""
    _get_project_or_404(db, project_id)
    milestone = _get_milestone_or_404(db, project_id, milestone_id)
    label = milestone.label or "this milestone"
    return templates.TemplateResponse(
        request,
        "_partials/inline_confirm.html",
        deps.base_context(
            request,
            db,
            user,
            confirm_action=(f"/projects/{project_id}/milestones/{milestone_id}/delete"),
            confirm_message=f"Delete {label}? This cannot be undone.",
            confirm_label="Yes, delete",
            confirm_cancel=f"/projects/{project_id}/confirm-cancel",
            confirm_danger=True,
        ),
    )


@router.post(
    "/projects/{project_id}/milestones/{milestone_id}/delete",
    response_class=HTMLResponse,
)
def delete_milestone(
    request: Request,
    db: DbDep,
    user: OperatorUser,
    project_id: int,
    milestone_id: int,
) -> Response:
    """Delete a milestone and return the refreshed table."""
    project = _get_project_or_404(db, project_id)
    milestone = _get_milestone_or_404(db, project_id, milestone_id)
    db.delete(milestone)
    _audit(
        db,
        scope="project",
        scope_id=project_id,
        actor_user_id=user.id,
        message=f"milestone {milestone_id} deleted",
    )
    db.flush()
    return _table_response(request, db, user, project)
