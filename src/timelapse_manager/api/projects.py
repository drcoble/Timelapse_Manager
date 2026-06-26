"""Project management endpoints.

A project is a capture campaign bound to a single camera. Creating one through
this endpoint both persists the project and, when the capture engine is running,
notifies it so capture begins without a process restart -- mirroring the
server-rendered create form. Validation matches that form: a non-empty unique
name, an existing camera configured with a protocol, and a positive capture
interval.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..capture.schedule import parse_schedule
from ..db.models import Camera, Project
from ..db.session import get_session
from ..runtime import get_context
from ..security import require_operator_or_admin_principal
from ..storage.projects import delete_project_with_files

router = APIRouter(prefix="/projects", tags=["projects"])


def _to_naive_utc(value: datetime | None) -> datetime | None:
    """Normalise an optional datetime to naive UTC for the naive DB column.

    Aware inputs (a JSON client may send ``...Z`` / an offset) are converted to
    UTC and the offset dropped; naive inputs are assumed to already be UTC and
    pass through unchanged, matching how the supervisor interprets the stored
    value. Mirrors the frame layer's storage convention.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


class ProjectCreate(BaseModel):
    """Request body for creating a project.

    A ``schedule`` is optional: omit it (or send ``null``) and the project
    captures on a plain fixed interval (an always-open gate); supply one to gate
    capture to wall-clock windows, weekdays or a sun-relative window. The campaign
    bounds -- ``start_date``, ``end_date`` and ``max_frame_count`` -- are all
    optional; when both dates are given the end must be strictly after the start,
    and a frame cap must be positive. Datetimes are normalised to naive UTC for
    storage.
    """

    name: str = Field(min_length=1)
    camera_id: int
    capture_interval_seconds: int = Field(ge=1)
    start_date: datetime | None = None
    end_date: datetime | None = None
    max_frame_count: int | None = Field(default=None, gt=0)
    schedule: dict[str, Any] | None = None

    _normalise_dates = field_validator("start_date", "end_date")(
        staticmethod(_to_naive_utc)
    )

    @model_validator(mode="after")
    def _end_after_start(self) -> ProjectCreate:
        """Reject a campaign whose end is not strictly after its start."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date <= self.start_date
        ):
            raise ValueError("end_date must be after start_date")
        return self


class ProjectUpdate(BaseModel):
    """Request body for editing a project; every field is optional.

    Only the fields actually present in the request are applied (distinguished
    via ``model_dump(exclude_unset=True)``), so a PATCH that omits a field leaves
    its stored value untouched -- including the nullable ``storage_path``. The
    end-after-start rule is enforced only when *both* dates are present in the
    request body; a PATCH that sets just one date is not cross-checked against the
    stored value (the web form always submits both together).
    """

    name: str | None = Field(default=None, min_length=1)
    camera_id: int | None = None
    capture_interval_seconds: int | None = Field(default=None, ge=1)
    start_date: datetime | None = None
    end_date: datetime | None = None
    max_frame_count: int | None = Field(default=None, gt=0)
    storage_path: str | None = None
    schedule: dict[str, Any] | None = None
    render_schedule: dict[str, Any] | None = None
    archive_schedule: dict[str, Any] | None = None
    post_render_actions: list[dict[str, Any]] | None = None

    _normalise_dates = field_validator("start_date", "end_date")(
        staticmethod(_to_naive_utc)
    )

    @model_validator(mode="after")
    def _end_after_start(self) -> ProjectUpdate:
        """Reject end<=start when both dates are present in the request."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date <= self.start_date
        ):
            raise ValueError("end_date must be after start_date")
        return self


class ProjectClone(BaseModel):
    """Request body for cloning a project: just the new (unique) name."""

    name: str = Field(min_length=1)


class ProjectOut(BaseModel):
    """Project representation returned to clients."""

    id: int
    name: str
    camera_id: int
    capture_interval_seconds: int | None
    start_date: datetime | None
    end_date: datetime | None
    max_frame_count: int | None
    lifecycle_state: str
    operational_status: str
    frame_count: int
    disk_used_bytes: int
    # Forward-looking projection over the campaign duration. Both are ``None``
    # together for an open-ended campaign (no end date) or one with no usable
    # capture interval -- a clear "cannot project" rather than a fabricated zero.
    projected_total_bytes: int | None
    projected_frame_count_remaining: int | None
    uptime_seconds: int | None
    render_schedule: dict[str, Any] | None
    archive_schedule: dict[str, Any] | None
    post_render_actions: list[dict[str, Any]] | None


def _runner_uptime_seconds(project_id: int) -> int | None:
    """Return the current capture runner's uptime in whole seconds, or ``None``.

    Consults the live supervisor state; ``None`` when the supervisor is absent,
    the project is not being captured, or the runner carries no start time.
    """
    supervisor = get_context().capture_supervisor
    state = supervisor.state_for_project(project_id) if supervisor is not None else None
    started_at = getattr(state, "started_at", None) if state is not None else None
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - started_at).total_seconds()))


def _to_out(project: Project, session: Session) -> ProjectOut:
    """Project a project row onto its public representation.

    ``disk_used_bytes`` and ``uptime_seconds`` are part of the status surface, so
    they are computed here from the project's active frames and the live capture
    runner respectively.
    """
    from ..storage import estimator
    from ..storage import frames as frame_service

    projected_total_bytes, projected_frames_remaining = estimator.estimate_for_project(
        session, project
    )
    return ProjectOut(
        id=project.id,
        name=project.name,
        camera_id=project.camera_id,
        capture_interval_seconds=project.capture_interval_seconds,
        start_date=project.start_date,
        end_date=project.end_date,
        max_frame_count=project.max_frame_count,
        lifecycle_state=project.lifecycle_state,
        operational_status=project.operational_status,
        frame_count=project.frame_count,
        disk_used_bytes=frame_service.sum_project_disk_usage(session, project.id),
        projected_total_bytes=projected_total_bytes,
        projected_frame_count_remaining=projected_frames_remaining,
        uptime_seconds=_runner_uptime_seconds(project.id),
        render_schedule=project.render_schedule,
        archive_schedule=project.archive_schedule,
        post_render_actions=project.post_render_actions,
    )


def _get_project_or_404(session: Session, project_id: int) -> Project:
    """Return a project row or raise a 404."""
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"project {project_id} not found",
        )
    return project


def _require_capture_camera(session: Session, camera_id: int) -> Camera:
    """Return a camera that exists (404) and carries a protocol (422)."""
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera {camera_id} not found",
        )
    if camera.protocol is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the selected camera has no protocol configured",
        )
    return camera


def _require_unique_name(
    session: Session, name: str, *, exclude_id: int | None = None
) -> None:
    """Raise a 409 if another project already uses ``name``.

    ``exclude_id`` skips the project being edited so saving an unchanged name is
    not mistaken for a duplicate. This is a friendly pre-check; the UNIQUE
    constraint plus the caller's flush is the authoritative guard against a
    concurrent insert.
    """
    stmt = select(Project.id).where(Project.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Project.id != exclude_id)
    if session.execute(stmt).scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a project named {name!r} already exists",
        )


def _validate_schedule(field: str, schedule: dict[str, Any] | None) -> None:
    """Reject a schedule that is enabled but has no positive interval.

    A ``None`` schedule (clearing it off), one with ``enabled`` falsy/absent, or
    one missing ``interval_seconds`` while disabled is allowed -- the scheduler
    reads all of those as "off". Only an *enabled* schedule must carry a positive
    numeric ``interval_seconds``. Extra keys (e.g. ``output_settings``) are left
    untouched.
    """
    if schedule is None:
        return
    if not schedule.get("enabled", False):
        return
    raw = schedule.get("interval_seconds")
    try:
        interval = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        interval = 0.0
    if interval <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"{field}: an enabled schedule requires a positive interval_seconds"
            ),
        )


def _validate_capture_schedule(schedule: dict[str, Any] | None) -> None:
    """Reject a malformed capture-gating schedule with a clear 422.

    Unlike the render/archive schedules, a capture schedule carries no
    ``interval_seconds`` -- it only *gates* when capture is allowed (wall-clock
    windows, weekdays, campaign bounds and an optional sun-relative window). A
    ``None`` schedule clears it off (an always-open gate) and is accepted. Any
    present schedule is validated by parsing it; a malformed field raises
    ``ValueError`` naming the offending key, which is surfaced verbatim as a 422.
    """
    if schedule is None:
        return
    try:
        parse_schedule(schedule)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"schedule: {exc}",
        ) from exc


def _validate_post_render_actions(actions: Any) -> None:
    """Reject a post-render action list that is not a well-formed list of types.

    A ``None`` value (clearing the actions off) is allowed. Otherwise the value
    must be a ``list`` and each element must be a ``dict`` carrying a non-empty
    string ``type``. Per-action keys are deliberately *not* validated here: the
    dispatcher logs and ignores unknown types and each handler validates its own
    keys when the action actually runs.
    """
    if actions is None:
        return
    if not isinstance(actions, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="post_render_actions must be a list of action objects",
        )
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(f"post_render_actions[{index}] must be an object with a type"),
            )
        action_type = action.get("type")
        if not isinstance(action_type, str) or not action_type.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"post_render_actions[{index}] requires a non-empty string type"
                ),
            )


def _notify_reconcile() -> None:
    """Wake the capture supervisor, if one is running, to converge to the DB."""
    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        supervisor.notify_reconcile()


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
def create_project(
    payload: ProjectCreate,
    session: Annotated[Session, Depends(get_session)],
) -> ProjectOut:
    """Create a project and start capturing it live.

    The name must be unique (a duplicate is a ``409``), the camera must exist
    (``404``) and carry a protocol (``422``). On success the project is committed
    and the running capture supervisor is notified so capture begins without a
    restart.
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name is required",
        )

    camera = session.get(Camera, payload.camera_id)
    if camera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera {payload.camera_id} not found",
        )
    if camera.protocol is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the selected camera has no protocol configured",
        )

    # Validate any capture-gating schedule before touching the session, so a
    # malformed schedule is a clean 422 and never autoflushes a half-built row.
    _validate_capture_schedule(payload.schedule)

    existing = session.execute(
        select(Project.id).where(Project.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a project named {name!r} already exists",
        )

    project = Project(
        camera_id=payload.camera_id,
        name=name,
        capture_interval_seconds=payload.capture_interval_seconds,
        start_date=payload.start_date,
        end_date=payload.end_date,
        max_frame_count=payload.max_frame_count,
        schedule=payload.schedule,
        lifecycle_state="active",
    )
    session.add(project)
    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the uniqueness race against a concurrent create.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a project named {name!r} already exists",
        ) from exc

    out = _to_out(project, session)
    # Commit before notifying: the supervisor reconciles in a fresh session on
    # another thread and would not see an uncommitted row.
    session.commit()
    supervisor = get_context().capture_supervisor
    if supervisor is not None:
        supervisor.notify_reconcile()
    return out


@router.get("", response_model=list[ProjectOut])
def list_projects(
    session: Annotated[Session, Depends(get_session)],
) -> list[ProjectOut]:
    """List every project with its current status, ordered by id.

    One disk-usage aggregate per project (the same accepted N+1 the web status
    view uses); the project counts this UI targets keep that cheap.
    """
    projects = session.execute(select(Project).order_by(Project.id)).scalars().all()
    return [_to_out(project, session) for project in projects]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> ProjectOut:
    """Return a single project's full status."""
    project = _get_project_or_404(session, project_id)
    return _to_out(project, session)


@router.patch(
    "/{project_id}",
    response_model=ProjectOut,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    session: Annotated[Session, Depends(get_session)],
) -> ProjectOut:
    """Apply a partial edit to a project and reconcile its live capture.

    Only the fields present in the request are changed. A new ``camera_id`` must
    reference an existing camera (``404``) that carries a protocol (``422``); a
    new ``name`` must stay unique (``409``). On success the project is committed
    and the running supervisor is notified so an interval/camera/storage change
    takes effect without a restart.
    """
    project = _get_project_or_404(session, project_id)
    fields = payload.model_dump(exclude_unset=True)

    if "name" in fields:
        name = fields["name"].strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="name is required",
            )
        _require_unique_name(session, name, exclude_id=project_id)
        fields["name"] = name
    if "camera_id" in fields:
        _require_capture_camera(session, fields["camera_id"])
    if "schedule" in fields:
        _validate_capture_schedule(fields["schedule"])
    if "render_schedule" in fields:
        _validate_schedule("render_schedule", fields["render_schedule"])
    if "archive_schedule" in fields:
        _validate_schedule("archive_schedule", fields["archive_schedule"])
    if "post_render_actions" in fields:
        _validate_post_render_actions(fields["post_render_actions"])

    for key, value in fields.items():
        setattr(project, key, value)
    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the uniqueness race against a concurrent rename/create.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a project with that name already exists",
        ) from exc

    out = _to_out(project, session)
    session.commit()
    _notify_reconcile()
    return out


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
def delete_project(
    project_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """Delete a project, its cascaded child rows, and its on-disk files.

    The ``ON DELETE CASCADE`` foreign keys drop the child rows; the shared
    cleanup helper unlinks the frame and render files those rows pointed at. A
    missing project is a ``404``. The supervisor is notified so it stops the
    project's capture loop promptly.
    """
    project = _get_project_or_404(session, project_id)
    delete_project_with_files(session, get_context().settings, project)
    session.commit()
    _notify_reconcile()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{project_id}/clone",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
def clone_project(
    project_id: int,
    payload: ProjectClone,
    session: Annotated[Session, Depends(get_session)],
) -> ProjectOut:
    """Create a new active project copying a source's capture configuration.

    Frames and renders are *not* copied -- only the capture/render/archive
    configuration. The new name must be unique (``409``); the source must exist
    (``404``). On success the clone is committed and the supervisor notified so it
    begins capturing without a restart.
    """
    source = _get_project_or_404(session, project_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name is required",
        )
    _require_unique_name(session, name)

    clone = _clone_from(source, name)
    session.add(clone)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a project named {name!r} already exists",
        ) from exc

    out = _to_out(clone, session)
    session.commit()
    _notify_reconcile()
    return out


@router.post(
    "/{project_id}/pause",
    response_model=ProjectOut,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
def pause_project(
    project_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> ProjectOut:
    """Pause a project's capture, stopping just that project's loop.

    Sets the project to ``paused`` and wakes the supervisor, whose reconcile loop
    drops the now-non-qualifying project and cancels its capture task -- no
    process restart. Idempotent: pausing an already-paused project simply leaves
    it paused. A missing project is a ``404``.
    """
    project = _get_project_or_404(session, project_id)
    project.lifecycle_state = "paused"
    out = _to_out(project, session)
    session.commit()
    _notify_reconcile()
    return out


@router.post(
    "/{project_id}/resume",
    response_model=ProjectOut,
    dependencies=[Depends(require_operator_or_admin_principal)],
)
def resume_project(
    project_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> ProjectOut:
    """Resume a paused project's capture, relaunching just that project's loop.

    Resume is only valid from the ``paused`` state: resuming a project that is not
    paused (active or archived) is a ``409`` rather than a silent no-op, so a
    client cannot mistake "already running" or "archived" for a successful resume
    -- reactivating an archived project is a deliberate, separate action. On a
    paused project this sets it ``active`` and wakes the supervisor so its capture
    task is relaunched without a restart. A missing project is a ``404``.
    """
    project = _get_project_or_404(session, project_id)
    if project.lifecycle_state != "paused":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"project {project_id} is not paused",
        )
    project.lifecycle_state = "active"
    out = _to_out(project, session)
    session.commit()
    _notify_reconcile()
    return out


def _clone_from(source: Project, name: str) -> Project:
    """Build a new active project copying ``source``'s capture configuration.

    Copies the capture/render/archive configuration, the campaign bounds (start
    date, end date, frame cap) and the storage path but nothing
    project-instance-specific: the clone starts ``active`` with a zero frame
    count and no frames or renders of its own. (Copying a past end date means the
    clone archives itself on the next reconcile, which is the intended literal
    copy of the source's configuration.)
    """
    return Project(
        camera_id=source.camera_id,
        name=name,
        capture_interval_seconds=source.capture_interval_seconds,
        start_date=source.start_date,
        end_date=source.end_date,
        max_frame_count=source.max_frame_count,
        schedule=source.schedule,
        render_schedule=source.render_schedule,
        archive_schedule=source.archive_schedule,
        post_render_actions=source.post_render_actions,
        storage_path=source.storage_path,
        lifecycle_state="active",
        frame_count=0,
    )
