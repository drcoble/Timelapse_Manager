"""Project write use-cases -- the single implementation of the domain rules.

Each public function performs one write against a caller-supplied session and
returns a :class:`~timelapse_manager.services._base.Result` pairing the produced
value with the post-commit effects the caller must run once it has committed. The
web and API layers are thin adapters over these functions: they parse their own
transport input into a :class:`ProjectSpec`, call the service, commit, then run
the returned effects.

Validation lives here, once. A rule violation raises a transport-neutral
:class:`~timelapse_manager.services.errors.AppError` (``NotFoundError`` for a
missing project/camera, ``ConflictError`` for a duplicate name, ``ValidationError``
for a semantically invalid input) that the web/API error handler maps to the same
status the endpoints returned before services existed. Services never commit and
never signal background components directly; they mutate, ``flush``, and return.

Camera *network* probes (event-topic enumeration, stream-profile/PTZ discovery)
are deliberately **not** here: the web adapter runs those and hands the already
resolved values in on the :class:`ProjectSpec`, so the service touches only the
database.
"""

from __future__ import annotations

import copy
import datetime
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..capture.schedule import parse_schedule
from ..db.models import Camera, Event, Project
from ..encode.overlay import (
    DEFAULT_TIMESTAMP_FORMAT,
    validate_text_content,
    validate_timestamp_format,
)
from ..security.principal import ensure_sentinel_admin
from ..storage.paths import StoragePathError, validate_storage_path
from ..storage.projects import delete_project_with_files
from ._base import Actor, Effect, Result
from .errors import ConflictError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..config.settings import Settings

logger = logging.getLogger(__name__)

# The key under which a render schedule carries the overlay each scheduled job
# copies (render/scheduler.py).
_OVERLAY_CONFIG_KEY = "overlay_config"


@dataclass
class ProjectSpec:
    """Normalised, already-parsed inputs for a project write.

    One spec type serves both create and update. ``fields_set`` names the fields
    the caller actually supplied so :func:`update_project` applies a partial edit
    -- touching only those fields -- exactly as the previous handlers did with
    Pydantic's ``exclude_unset`` and the web form's presence markers. A field
    absent from ``fields_set`` leaves its stored value untouched; a field present
    with value ``None`` clears the column (e.g. the web "solar / scheduled-times
    only" mode clears ``capture_interval_seconds``). :func:`create_project` reads
    the create-relevant fields directly and ignores ``fields_set``.

    Dates are expected pre-normalised to naive UTC by the caller (the API's
    Pydantic validator / the web form parser), matching the naive DB columns.
    """

    name: str | None = None
    camera_id: int | None = None
    capture_interval_seconds: int | None = None
    start_date: datetime.datetime | None = None
    end_date: datetime.datetime | None = None
    max_frame_count: int | None = None
    storage_path: str | None = None
    schedule: dict[str, Any] | None = None
    render_schedule: dict[str, Any] | None = None
    archive_schedule: dict[str, Any] | None = None
    post_render_actions: list[dict[str, Any]] | None = None
    stream_id: str | None = None
    stream_label: str | None = None
    exact_time_anchors: list[dict[str, Any]] | None = None
    event_triggers: list[dict[str, Any]] | None = None
    ptz_preset: str | None = None
    ptz_pan: float | None = None
    ptz_tilt: float | None = None
    ptz_zoom: float | None = None
    fields_set: frozenset[str] = frozenset()


# Fields an update copies straight through once present, with no domain check of
# their own (their shape was validated by the transport: the API's Pydantic model
# or the web form parser). ``storage_path`` is not one of them: it goes through
# :func:`_checked_storage_path` (#67 S5).
_PASSTHROUGH_FIELDS = (
    "capture_interval_seconds",
    "start_date",
    "end_date",
    "max_frame_count",
    "stream_id",
    "stream_label",
    "exact_time_anchors",
    "event_triggers",
    "ptz_preset",
    "ptz_pan",
    "ptz_tilt",
    "ptz_zoom",
)


def _get_project_or_404(session: Session, project_id: int) -> Project:
    """Return a project row or raise :class:`NotFoundError` (mapped to ``404``)."""
    project = session.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    return project


def _require_capture_camera(session: Session, camera_id: int) -> Camera:
    """Return a camera that exists (else ``404``) and carries a protocol (``422``)."""
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise NotFoundError(f"camera {camera_id} not found", field="camera_id")
    if camera.protocol is None:
        raise ValidationError(
            "the selected camera has no protocol configured", field="camera_id"
        )
    return camera


def _require_unique_name(
    session: Session, name: str, *, exclude_id: int | None = None
) -> None:
    """Raise :class:`ConflictError` (``409``) if another project uses ``name``.

    ``exclude_id`` skips the project being edited so saving an unchanged name is
    not mistaken for a duplicate. A friendly pre-check; the UNIQUE constraint plus
    the caller's flush is the authoritative guard against a concurrent insert.
    """
    stmt = select(Project.id).where(Project.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Project.id != exclude_id)
    if session.execute(stmt).scalar_one_or_none() is not None:
        raise ConflictError(f"a project named {name!r} already exists", field="name")


def _validate_schedule(field_name: str, schedule: dict[str, Any] | None) -> None:
    """Reject a render/archive schedule that is enabled but lacks a positive interval.

    A ``None`` schedule, one with ``enabled`` falsy/absent, or one missing
    ``interval_seconds`` while disabled is allowed (the scheduler reads all of
    those as "off"). Only an *enabled* schedule must carry a positive numeric
    ``interval_seconds``. Extra keys (e.g. ``output_settings``) are left untouched.
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
        raise ValidationError(
            f"{field_name}: an enabled schedule requires a positive interval_seconds",
            field=field_name,
        )


def _validate_render_overlay(schedule: dict[str, Any] | None) -> None:
    """Reject a render schedule whose overlay has a bad timestamp format or caption.

    The scheduler copies ``render_schedule["overlay_config"]`` into every
    scheduled job, and the format ends up inside an ffmpeg filtergraph, so it
    must pass :func:`~timelapse_manager.encode.overlay.validate_timestamp_format`
    (#67 S4); the caption, one ffmpeg argument, is capped by
    :func:`~timelapse_manager.encode.overlay.validate_text_content` (#139).
    Checked whether or not the schedule is enabled: a disabled
    schedule is stored too and can be switched on later. A schedule without an
    overlay, or an overlay without a format, is fine; a non-object overlay is
    rejected, since nothing could use it.
    """
    if schedule is None or _OVERLAY_CONFIG_KEY not in schedule:
        return
    overlay = schedule[_OVERLAY_CONFIG_KEY]
    if overlay is None:
        return
    if not isinstance(overlay, dict):
        raise ValidationError(
            "render_schedule: overlay_config must be an object",
            field="render_schedule",
        )
    try:
        if "timestamp_format" in overlay:
            validate_timestamp_format(overlay["timestamp_format"])
        if overlay.get("text_content") is not None:
            validate_text_content(overlay["text_content"])
    except ValueError as exc:
        raise ValidationError(
            f"render_schedule: overlay_config: {exc}", field="render_schedule"
        ) from exc


def _sanitised_render_schedule(
    schedule: dict[str, Any] | None, *, source_id: int
) -> dict[str, Any] | None:
    """Return a copy of ``schedule`` that is safe to store on a clone.

    The source row may predate the timestamp-format check (#67 S4). Rather than
    refuse the clone, an invalid format in the copied overlay is replaced by the
    default, with a WARNING naming the source project; the source row is not
    changed. The copy is deep, so the clone never shares the source's JSON.
    """
    if not isinstance(schedule, dict):
        return copy.deepcopy(schedule)
    copied = copy.deepcopy(schedule)
    overlay = copied.get(_OVERLAY_CONFIG_KEY)
    if not isinstance(overlay, dict) or "timestamp_format" not in overlay:
        return copied
    stored = overlay["timestamp_format"]
    try:
        validate_timestamp_format(stored)
    except ValueError as exc:
        logger.warning(
            "clone of project %s: stored render_schedule timestamp_format %r is "
            "invalid (%s); the clone gets the default %r",
            source_id,
            stored,
            exc,
            DEFAULT_TIMESTAMP_FORMAT,
        )
        overlay["timestamp_format"] = DEFAULT_TIMESTAMP_FORMAT
    return copied


def _validate_capture_schedule(schedule: dict[str, Any] | None) -> None:
    """Reject a malformed capture-gating schedule with a clear ``422``.

    A capture schedule carries no ``interval_seconds`` -- it only *gates* when
    capture is allowed. A ``None`` schedule clears it off (an always-open gate).
    Any present schedule is validated by parsing it; a malformed field raises
    ``ValueError`` naming the offending key, surfaced verbatim.
    """
    if schedule is None:
        return
    try:
        parse_schedule(schedule)
    except ValueError as exc:
        raise ValidationError(f"schedule: {exc}", field="schedule") from exc


def _validate_post_render_actions(actions: Any) -> None:
    """Reject a post-render action list that is not a well-formed list of types.

    A ``None`` value (clearing the actions off) is allowed. Otherwise the value
    must be a ``list`` and each element a ``dict`` carrying a non-empty string
    ``type``. Per-action keys are deliberately not validated here.
    """
    if actions is None:
        return
    if not isinstance(actions, list):
        raise ValidationError(
            "post_render_actions must be a list of action objects",
            field="post_render_actions",
        )
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValidationError(
                f"post_render_actions[{index}] must be an object with a type",
                field="post_render_actions",
            )
        action_type = action.get("type")
        if not isinstance(action_type, str) or not action_type.strip():
            raise ValidationError(
                f"post_render_actions[{index}] requires a non-empty string type",
                field="post_render_actions",
            )


def _checked_storage_path(
    session: Session,
    raw: str | None,
    settings: Settings,
    actor: Actor,
    *,
    project_id: int,
) -> str | None:
    """Return the value to store for a proposed ``storage_path``, or raise ``422``.

    Applies :func:`~timelapse_manager.storage.paths.validate_storage_path` with
    the actor's role: an administrator may use any permitted directory, anyone
    else only the configured ``storage.allowed_roots``. The path also may not
    collide with the default layout or with a path another project already uses
    (#171). The resolved path comes back, so the stored value is canonical.
    """
    others = session.scalars(
        select(Project.storage_path).where(
            Project.storage_path.is_not(None), Project.id != project_id
        )
    ).all()
    try:
        return validate_storage_path(
            raw,
            settings,
            is_admin=actor.role == "admin",
            project_id=project_id,
            other_storage_paths=[path for path in others if path],
        )
    except StoragePathError as exc:
        raise ValidationError(str(exc), field="storage_path") from exc


def _storage_path_unchanged(raw: str | None, stored: str | None) -> bool:
    """Return whether a submitted ``storage_path`` repeats the stored value.

    The web settings form always posts the field back, prefilled with the stored
    value. Re-validating an unchanged value would refuse every edit of a project
    whose path predates the policy (the startup scan only warns about those), and
    would rewrite a stored value to its resolved form behind the user's back. So
    an unchanged value is left alone; only a change is validated.
    """
    submitted = (raw or "").strip() or None
    return submitted == (stored or None)


def _clone_from(source: Project, name: str) -> Project:
    """Build a new active project copying ``source``'s capture configuration.

    Copies the capture/render/archive configuration and the campaign bounds but
    nothing project-instance-specific: the clone starts ``active`` with a zero
    frame count and no frames or renders of its own. The render schedule is
    copied through :func:`_sanitised_render_schedule`. ``storage_path`` is *not*
    copied: the clone uses the default layout, its own directory under the frames
    root, so it can never write into (or delete from) the source's directory
    (#171).
    """
    return Project(
        camera_id=source.camera_id,
        name=name,
        capture_interval_seconds=source.capture_interval_seconds,
        start_date=source.start_date,
        end_date=source.end_date,
        max_frame_count=source.max_frame_count,
        schedule=source.schedule,
        render_schedule=_sanitised_render_schedule(
            source.render_schedule, source_id=source.id
        ),
        archive_schedule=source.archive_schedule,
        post_render_actions=source.post_render_actions,
        storage_path=None,
        lifecycle_state="active",
        frame_count=0,
    )


def _audit(
    session: Session, actor: Actor, project_id: int | None, message: str
) -> None:
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


def create_project(
    session: Session, actor: Actor, spec: ProjectSpec
) -> Result[Project]:
    """Create an active project and ask the caller to reconcile capture.

    The name must be a non-empty unique string (a duplicate is a ``409``), the
    camera must exist (``404``) and carry a protocol (``422``), and any capture
    schedule must parse (``422``). Validates, inserts, and flushes -- but does not
    commit; the caller commits and runs :data:`Effect.CAPTURE_RECONCILE`.
    """
    name = (spec.name or "").strip()
    if not name:
        raise ValidationError("name is required", field="name")
    camera_id = spec.camera_id
    assert camera_id is not None  # a create always supplies a camera id
    _require_capture_camera(session, camera_id)
    _validate_capture_schedule(spec.schedule)
    _require_unique_name(session, name)

    project = Project(
        camera_id=camera_id,
        name=name,
        capture_interval_seconds=spec.capture_interval_seconds,
        start_date=spec.start_date,
        end_date=spec.end_date,
        max_frame_count=spec.max_frame_count,
        schedule=spec.schedule,
        lifecycle_state="active",
    )
    session.add(project)
    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the uniqueness race against a concurrent create.
        session.rollback()
        raise ConflictError(
            f"a project named {name!r} already exists", field="name"
        ) from exc

    _audit(session, actor, project.id, f"project {project.id} created")
    return Result(project, (Effect.CAPTURE_RECONCILE,))


def update_project(
    session: Session,
    actor: Actor,
    project_id: int,
    spec: ProjectSpec,
    *,
    settings: Settings,
) -> Result[Project]:
    """Apply a partial edit to a project and ask the caller to reconcile capture.

    Only the fields named in ``spec.fields_set`` are changed. A new ``camera_id``
    must reference an existing camera (``404``) that carries a protocol (``422``);
    a new ``name`` must be non-empty and stay unique (``409``); a present schedule
    or post-render action list must be well-formed (``422``); a changed
    ``storage_path`` must pass the storage policy for the actor's role (``422``,
    #67 S5) and is stored resolved. Validates every supplied field before
    mutating, flushes, but does not commit.
    """
    project = _get_project_or_404(session, project_id)

    updates: dict[str, Any] = {}
    if "name" in spec.fields_set:
        name = (spec.name or "").strip()
        if not name:
            raise ValidationError("name is required", field="name")
        _require_unique_name(session, name, exclude_id=project_id)
        updates["name"] = name
    if "camera_id" in spec.fields_set:
        camera_id = spec.camera_id
        assert camera_id is not None  # present camera_id is always concrete
        _require_capture_camera(session, camera_id)
        updates["camera_id"] = camera_id
    if "schedule" in spec.fields_set:
        _validate_capture_schedule(spec.schedule)
        updates["schedule"] = spec.schedule
    if "render_schedule" in spec.fields_set:
        _validate_render_overlay(spec.render_schedule)
        _validate_schedule("render_schedule", spec.render_schedule)
        updates["render_schedule"] = spec.render_schedule
    if "archive_schedule" in spec.fields_set:
        _validate_schedule("archive_schedule", spec.archive_schedule)
        updates["archive_schedule"] = spec.archive_schedule
    if "post_render_actions" in spec.fields_set:
        _validate_post_render_actions(spec.post_render_actions)
        updates["post_render_actions"] = spec.post_render_actions
    if "storage_path" in spec.fields_set and not _storage_path_unchanged(
        spec.storage_path, project.storage_path
    ):
        updates["storage_path"] = _checked_storage_path(
            session, spec.storage_path, settings, actor, project_id=project_id
        )
    for name_ in _PASSTHROUGH_FIELDS:
        if name_ in spec.fields_set:
            updates[name_] = getattr(spec, name_)

    for key, value in updates.items():
        setattr(project, key, value)
    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the uniqueness race against a concurrent rename/create.
        session.rollback()
        raise ConflictError(
            "a project with that name already exists", field="name"
        ) from exc

    _audit(session, actor, project_id, f"project {project_id} updated")
    return Result(project, (Effect.CAPTURE_RECONCILE,))


def delete_project(
    session: Session, actor: Actor, project_id: int, *, settings: Settings
) -> Result[None]:
    """Delete a project, its cascaded child rows, and its on-disk files.

    The ``ON DELETE CASCADE`` foreign keys drop the child rows; the shared cleanup
    helper unlinks the frame and render files those rows pointed at. A missing
    project is a ``404``. Does not commit; the caller commits and reconciles.
    """
    project = _get_project_or_404(session, project_id)
    delete_project_with_files(session, settings, project)
    _audit(session, actor, project_id, f"project {project_id} deleted")
    return Result(None, (Effect.CAPTURE_RECONCILE,))


def clone_project(
    session: Session,
    actor: Actor,
    source_id: int,
    *,
    name: str,
    settings: Settings,
) -> Result[Project]:
    """Create a new active project copying a source's capture configuration.

    Frames and renders are not copied -- only the capture/render/archive
    configuration. The new name must be non-empty and unique (``409``); the source
    must exist (``404``). The source's ``storage_path`` is not copied: the clone
    gets the default layout (#171). ``settings`` is kept for the callers' sake.
    Does not commit; the caller commits and reconciles.
    """
    source = _get_project_or_404(session, source_id)
    clean = name.strip()
    if not clean:
        raise ValidationError("name is required", field="name")
    _require_unique_name(session, clean)
    clone = _clone_from(source, clean)
    session.add(clone)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(
            f"a project named {clean!r} already exists", field="name"
        ) from exc

    _audit(session, actor, clone.id, f"project {clone.id} cloned from {source_id}")
    return Result(clone, (Effect.CAPTURE_RECONCILE,))


def pause_project(session: Session, actor: Actor, project_id: int) -> Result[Project]:
    """Pause a project's capture (idempotent) and ask the caller to reconcile.

    Sets the project to ``paused``; pausing an already-paused project simply
    leaves it paused. A missing project is a ``404``. Does not commit.
    """
    project = _get_project_or_404(session, project_id)
    project.lifecycle_state = "paused"
    session.flush()
    _audit(session, actor, project_id, f"project {project_id} paused")
    return Result(project, (Effect.CAPTURE_RECONCILE,))


def resume_project(session: Session, actor: Actor, project_id: int) -> Result[Project]:
    """Resume a paused project's capture and ask the caller to reconcile.

    Resume is only valid from the ``paused`` state: resuming a project that is not
    paused (active or archived) is a ``409`` rather than a silent no-op. On a
    paused project this sets it ``active``. A missing project is a ``404``. Does
    not commit.
    """
    project = _get_project_or_404(session, project_id)
    if project.lifecycle_state != "paused":
        raise ConflictError(f"project {project_id} is not paused")
    project.lifecycle_state = "active"
    session.flush()
    _audit(session, actor, project_id, f"project {project_id} resumed")
    return Result(project, (Effect.CAPTURE_RECONCILE,))


def set_project_lifecycle(
    session: Session,
    actor: Actor,
    project_id: int,
    *,
    state: str,
    action: str,
) -> Result[Project]:
    """Set a project's lifecycle state unconditionally and reconcile capture.

    The web's lifecycle controls (start/pause/resume/stop/archive/reactivate) all
    reduce to setting ``lifecycle_state`` and recording the requested ``action``;
    the supervisor's reconcile loop converges the running capture tasks. Unlike
    :func:`resume_project`, this applies the transition with no state guard,
    preserving the web controls' unconditional behaviour. A missing project is a
    ``404``. Does not commit.
    """
    project = _get_project_or_404(session, project_id)
    project.lifecycle_state = state
    session.flush()
    _audit(session, actor, project_id, f"project {action} requested")
    return Result(project, (Effect.CAPTURE_RECONCILE,))


def _warn_on_shared_storage_paths(rows: Sequence[Any]) -> int:
    """Log a WARNING per ``storage_path`` that more than one project uses (#171).

    Before #171 a clone copied its source's path, so the two wrote into one
    directory and deleting one project could remove the other's frames. Rows are
    never changed (moving frame files is the operator's call); the warning names
    the projects. Returns the number of projects warned about.
    """
    by_path: dict[str, list[tuple[int, str]]] = {}
    for project_id, project_name, stored in rows:
        try:
            real = os.path.realpath(stored)
        except (OSError, ValueError):
            continue  # an unresolvable legacy value; the policy scan reports it
        by_path.setdefault(real, []).append((project_id, project_name))
    warned = 0
    for real, owners in by_path.items():
        if len(owners) < 2:
            continue
        warned += len(owners)
        logger.warning(
            "storage_path policy: projects %s share the storage path %r, so they "
            "write into one directory and deleting one removes the other's "
            "frames. Give each its own directory (a clone no longer copies its "
            "source's path) (#171).",
            ", ".join(f"{pid} ({name!r})" for pid, name in owners),
            real,
        )
    return warned


def warn_on_storage_path_policy(session: Session, settings: Settings) -> int:
    """Log a WARNING for each project whose stored ``storage_path`` breaks policy.

    Run once at startup (#67 S5). Rows are **never** changed: a project with a
    path set before the policy existed keeps capturing, rendering and serving
    frames from it; the warning tells the operator to move it. The check uses the
    administrator rules (structure plus the protected directories), because
    ``storage.allowed_roots`` only limits who may *set* a path. Returns the number
    of projects warned about.
    """
    rows = session.execute(
        select(Project.id, Project.name, Project.storage_path)
        .where(Project.storage_path.is_not(None))
        .order_by(Project.id)
    ).all()
    warned = _warn_on_shared_storage_paths(rows)
    for project_id, project_name, stored in rows:
        try:
            validate_storage_path(
                stored, settings, is_admin=True, project_id=project_id
            )
        except StoragePathError as exc:
            warned += 1
            logger.warning(
                "storage_path policy: project %s (%r) has storage_path %r, which "
                "the storage policy no longer accepts: %s. Capture, renders and "
                "frame serving keep using it unchanged; move the project to an "
                "allowed directory and update its storage path (#67 S5).",
                project_id,
                project_name,
                stored,
                exc,
            )
    return warned
