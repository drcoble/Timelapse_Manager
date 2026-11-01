"""Milestone write use-cases -- the single implementation of the domain rules.

A milestone is a named position in a project's timeline (a frame index *or* a
timestamp) that a render turns into a chapter. Each public function performs one
write against a caller-supplied session and returns a
:class:`~timelapse_manager.services._base.Result` pairing the produced value with
the post-commit effects the caller must run. Milestone writes drive no background
component, so every result carries no effects; the web and API layers are thin
adapters that parse their transport input into a :class:`MilestoneSpec`, call the
service, and commit.

Validation lives here, once. A missing project or milestone raises
:class:`~timelapse_manager.services.errors.NotFoundError` (``404``); a milestone
left with neither a frame index nor a timestamp raises
:class:`~timelapse_manager.services.errors.ValidationError` (``422``). Timestamps
are expected pre-normalised to naive UTC by the caller, matching the naive DB
column. Services never commit; they mutate, ``flush``, and return.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..db.models import Event, Milestone, Project
from ..security.principal import ensure_sentinel_admin
from ._base import Actor, Result
from .errors import NotFoundError, ValidationError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass
class MilestoneSpec:
    """Normalised, already-parsed inputs for a milestone write.

    One spec type serves both create and update. ``fields_set`` names the fields
    the caller actually supplied so :func:`update_milestone` applies a partial
    edit -- touching only those fields -- exactly as the previous API handler did
    with Pydantic's ``model_fields_set``. The web adapter always supplies all
    three (choosing one position field and clearing the other), so its edit fully
    replaces the position; the API adapter supplies only the patched fields.

    A ``position_timestamp`` is expected pre-normalised to naive UTC by the caller
    (the API's ``_naive_utc`` / the web form parser), matching the naive DB column.
    """

    label: str | None = None
    position_frame_index: int | None = None
    position_timestamp: datetime.datetime | None = None
    fields_set: frozenset[str] = frozenset()


def _get_project_or_404(session: Session, project_id: int) -> Project:
    """Return a project row or raise :class:`NotFoundError` (mapped to ``404``)."""
    project = session.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    return project


def _get_milestone_or_404(
    session: Session, project_id: int, milestone_id: int
) -> Milestone:
    """Return a milestone that belongs to the project, or raise a ``404``."""
    milestone = session.get(Milestone, milestone_id)
    if milestone is None or milestone.project_id != project_id:
        raise NotFoundError(
            f"milestone {milestone_id} not found in project {project_id}"
        )
    return milestone


def _require_position(
    frame_index: int | None, timestamp: datetime.datetime | None
) -> None:
    """Raise :class:`ValidationError` (``422``) if a milestone has no position."""
    if frame_index is None and timestamp is None:
        raise ValidationError(
            "a milestone needs a position_frame_index or position_timestamp"
        )


def _audit(session: Session, actor: Actor, project_id: int, message: str) -> None:
    """Append a project-scoped audit event attributed to ``actor``.

    The actor is a foreign key to a user row, so the sentinel administrator is
    ensured to exist before the insert (matching every other audited write) so the
    foreign key never fails for a bearer-token caller.
    """
    ensure_sentinel_admin(session)
    session.add(
        Event(
            scope="project",
            scope_id=project_id,
            level="info",
            message=message,
            actor_user_id=actor.user_id,
            timestamp=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        )
    )


def create_milestone(
    session: Session, actor: Actor, project_id: int, spec: MilestoneSpec
) -> Result[Milestone]:
    """Place a milestone in a project's timeline, attributed to ``actor``.

    The project must exist (``404``) and the milestone must carry at least one of
    a frame index or a timestamp (``422``). The placing user is recorded on the
    milestone's ``user_id``; the sentinel administrator is ensured first so that
    foreign key holds for a bearer-token caller. Validates, inserts, and flushes
    -- but does not commit.
    """
    _get_project_or_404(session, project_id)
    _require_position(spec.position_frame_index, spec.position_timestamp)
    # The milestone references the placing user; materialise the sentinel admin so
    # the foreign key holds until real accounts are seeded.
    ensure_sentinel_admin(session)
    milestone = Milestone(
        project_id=project_id,
        user_id=actor.user_id,
        label=spec.label,
        position_frame_index=spec.position_frame_index,
        position_timestamp=spec.position_timestamp,
    )
    session.add(milestone)
    session.flush()
    _audit(session, actor, project_id, f"milestone {milestone.id} created")
    return Result(milestone, ())


def update_milestone(
    session: Session,
    actor: Actor,
    project_id: int,
    milestone_id: int,
    spec: MilestoneSpec,
) -> Result[Milestone]:
    """Apply a partial edit to a milestone, attributed to ``actor``.

    Only the fields named in ``spec.fields_set`` change. A position field is
    applied only when the caller actually supplied it, so omitting both leaves the
    existing position untouched; supplying one replaces it. The create-time
    invariant is preserved: an update may not leave the milestone with neither a
    frame index nor a timestamp (``422``). A missing milestone is a ``404``.
    Validates, mutates, and flushes -- but does not commit.
    """
    milestone = _get_milestone_or_404(session, project_id, milestone_id)

    if "label" in spec.fields_set and spec.label is not None:
        milestone.label = spec.label
    new_frame = (
        spec.position_frame_index
        if "position_frame_index" in spec.fields_set
        else milestone.position_frame_index
    )
    new_timestamp = (
        spec.position_timestamp
        if "position_timestamp" in spec.fields_set
        else milestone.position_timestamp
    )
    _require_position(new_frame, new_timestamp)
    milestone.position_frame_index = new_frame
    milestone.position_timestamp = new_timestamp

    _audit(session, actor, project_id, f"milestone {milestone_id} updated")
    session.flush()
    return Result(milestone, ())


def delete_milestone(
    session: Session, actor: Actor, project_id: int, milestone_id: int
) -> Result[None]:
    """Delete a milestone from a project, attributed to ``actor``.

    A missing milestone (or one belonging to another project) is a ``404``.
    Deletes the row, audits, and flushes -- but does not commit.
    """
    milestone = _get_milestone_or_404(session, project_id, milestone_id)
    session.delete(milestone)
    _audit(session, actor, project_id, f"milestone {milestone_id} deleted")
    session.flush()
    return Result(None, ())
