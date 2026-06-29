"""Post-render actions: export, external-trigger webhook, and prune.

After a successful render, a project may run one or more *built-in* follow-up
actions. There are no arbitrary commands -- only a fixed, audited set:

* **export** -- copy the produced video to a configured directory (a NAS share,
  a publish folder), leaving the original render in place.
* **external_trigger** -- POST a small JSON notification to a configured URL
  (HTTP webhook), so an external system learns a render finished.
* **prune** -- delete old *non-archive* render outputs (and their job rows) for
  the project beyond a keep count. Archive renders and captured frames are never
  touched.

Three invariants make these safe:

* **Failure-isolated.** Every action runs in its own ``try``; a failure is
  logged and recorded as a project :class:`Event`, but it never fails the render
  (the video is already produced) and never stops the other actions. The event
  carries the hook a later notification-delivery phase reads.
* **Export disabled under Docker.** A containerised deployment has no access to
  an arbitrary host export directory, so the **export** action is skipped there
  (its destination would resolve inside the container and be lost on restart).
  The webhook and prune actions have no host-path dependency -- a webhook is a
  plain network call and prune only touches the project's own render root (a
  mounted volume) -- so they run normally in a container. Detection is behind an
  overridable seam.
* **Outbound URL chokepoint.** A webhook target passes through a single
  validation seam (:func:`validate_outbound_url`) so a later phase can enforce a
  deny-list without touching this call site. Redirects are not followed and a
  timeout is always set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db.models import RenderJob
from ..db.session import session_scope
from ..monitoring.events import EventType, log_event

logger = logging.getLogger(__name__)

# Files probed to decide we are running inside a container. Behind a function so
# tests can force the result without a real container or filesystem.
_DOCKERENV = Path("/.dockerenv")
_CGROUP = Path("/proc/1/cgroup")


def running_under_docker() -> bool:
    """Return whether the process appears to run inside a Docker container.

    Checks for the ``/.dockerenv`` marker or a container hint in the init
    process's cgroup. Best effort and never raises; a probe failure reads as
    "not in a container". This is the single seam tests monkeypatch to force the
    Docker-disabled path on or off.
    """
    try:
        if _DOCKERENV.exists():
            return True
        if _CGROUP.exists():
            text = _CGROUP.read_text(encoding="utf-8", errors="replace")
            return "docker" in text or "containerd" in text or "kubepods" in text
    except OSError:
        return False
    return False


def validate_outbound_url(url: str) -> str:
    """Return a validated outbound webhook URL (single SSRF-hardening seam).

    The webhook surface always uses the **full** deny-list with **no** private
    opt-in: a webhook may never target loopback, link-local/metadata, or any
    private/special-use address. The host is resolved and every resolved address
    validated before the request is made; the URL is returned unchanged so the
    caller still presents the original hostname (TLS/``Host`` header intact). The
    webhook callers also disable redirect-following, so a 30x cannot bounce the
    request to a denied host after this check.

    :raises SsrfError: when the URL targets a denied address or does not resolve.
    """
    from ..security.ssrf import assert_allowed_url

    return assert_allowed_url(url, allow_private=False)


async def run_post_actions(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    job_id: int,
    output_path: Path | None,
    action_specs: list[dict[str, Any]],
    kind: str = "manual",
) -> None:
    """Run every configured post-render action for a finished render.

    Each configured action is dispatched and its failure contained: a raised
    exception is logged and recorded as a project event, then the next action
    still runs. The render's own outcome is never affected. Only the *export*
    action no-ops under Docker (no host export path); webhook and prune run
    normally (see module docs).

    ``kind`` is the finished render's kind (``manual``/``scheduled``/``archive``).
    It gates two prune behaviours: the manually configured *prune* action is
    trigger-exempt for a ``manual`` render, and the automatic schedule-scoped
    auto-prune runs only for a ``scheduled`` or ``archive`` render. Auto-prune is
    independent of ``action_specs`` -- it runs even when no actions are
    configured -- so it is evaluated after the action loop and isolated in its own
    failure boundary, exactly like a configured action: a failure is logged and
    recorded as a project event but never fails the render.
    """
    if output_path is None:
        return

    # An export job produces a zip of frame files, not a render: it has no
    # post-render actions and -- critically -- must never reach the prune path
    # below. The configured prune and the schedule-scoped auto-prune both delete
    # *render outputs*; letting an export fall through here would let it trigger a
    # prune that deletes real rendered videos. Export is the second kind (after
    # ``manual``) that the prune path treats as a non-trigger, expressed here as a
    # hard early return so no prune logic can run for it. The reverse data-loss
    # direction -- a render's prune deleting an export's zip -- is guarded
    # separately in :func:`_prune` (an export row is excluded from prune
    # candidates).
    if kind == "export":
        return

    project_id = await asyncio.to_thread(_project_id_for_job, session_factory, job_id)
    if project_id is None:
        return

    for spec in action_specs:
        action_type = str(spec.get("type") or "").lower()
        try:
            await _dispatch_action(
                settings,
                session_factory,
                project_id=project_id,
                job_id=job_id,
                output_path=output_path,
                action_type=action_type,
                spec=spec,
                kind=kind,
            )
        except Exception as exc:  # noqa: BLE001 - actions must not fail the render
            logger.warning(
                "post-render action %r failed for job=%s: %s",
                action_type,
                job_id,
                exc,
            )
            await asyncio.to_thread(
                _record_event,
                session_factory,
                project_id=project_id,
                level="warning",
                message=(
                    f"post-render action {action_type!r} failed for render "
                    f"{job_id}: {exc}"
                ),
                metadata={"action": action_type, "render_id": job_id},
            )
            # Hook for a later notification-delivery phase: the event above is the
            # durable signal a notifier reads; nothing else is needed here.

    # Automatic schedule-scoped auto-prune. Independent of action_specs and run
    # in its own failure boundary so it can never fail an already-finished render
    # (the row is committed ``done`` before this is reached).
    try:
        await asyncio.to_thread(
            _auto_prune, settings, session_factory, project_id=project_id, kind=kind
        )
    except Exception as exc:  # noqa: BLE001 - auto-prune must not fail the render
        logger.warning("auto-prune failed for job=%s (kind=%s): %s", job_id, kind, exc)
        await asyncio.to_thread(
            _record_event,
            session_factory,
            project_id=project_id,
            level="warning",
            message=(f"auto-prune failed for render {job_id} (kind {kind!r}): {exc}"),
            metadata={"action": "auto_prune", "render_id": job_id},
        )


async def _dispatch_action(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    project_id: int,
    job_id: int,
    output_path: Path,
    action_type: str,
    spec: dict[str, Any],
    kind: str,
) -> None:
    """Route one action spec to its handler. Unknown types are ignored (logged)."""
    if action_type == "export":
        if running_under_docker():
            logger.info(
                "skipping export post-render action for job=%s: no host export "
                "path is available under Docker",
                job_id,
            )
            return
        await asyncio.to_thread(_export, output_path, spec)
    elif action_type == "external_trigger":
        await _webhook(settings, project_id=project_id, job_id=job_id, spec=spec)
    elif action_type == "prune":
        await asyncio.to_thread(
            _prune,
            settings,
            session_factory,
            project_id=project_id,
            spec=spec,
            kind=kind,
        )
    else:
        logger.info("ignoring unknown post-render action type %r", action_type)


def _export(output_path: Path, spec: dict[str, Any]) -> None:
    """Copy the render output to a configured directory. Synchronous.

    The destination directory is created if needed; the copy preserves the
    output's filename. Raises (to be caught and audited by the caller) when no
    destination is configured or the copy fails.
    """
    destination = spec.get("destination")
    if not destination:
        raise ValueError("export action requires a 'destination' directory")
    dest_dir = Path(str(destination))
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output_path, dest_dir / output_path.name)


async def _webhook(
    settings: Settings,
    *,
    project_id: int,
    job_id: int,
    spec: dict[str, Any],
) -> None:
    """POST a small JSON notification to the configured webhook URL.

    The URL passes through the outbound-validation seam; the request carries a
    timeout and does not follow redirects (so a 30x cannot bounce the call to an
    unintended host). A non-2xx response raises, to be caught and audited.
    """
    url = spec.get("url")
    if not url:
        raise ValueError("external_trigger action requires a 'url'")
    # validate_outbound_url resolves the host via blocking socket.getaddrinfo;
    # off-load it so a slow/stalled resolver cannot stall the event loop.
    target = await asyncio.to_thread(validate_outbound_url, str(url))
    payload = {
        "event": "render_completed",
        "project_id": project_id,
        "render_id": job_id,
    }
    timeout = settings.render.webhook_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.post(target, json=payload)
        response.raise_for_status()


def _prune(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    project_id: int,
    spec: dict[str, Any],
    kind: str,
) -> None:
    """Delete old non-archive render outputs and rows beyond a keep count.

    Keeps the newest ``keep`` *done, non-archive* renders for the project and
    removes the rest: each output file is unlinked (only when it resolves inside
    the project's render root -- a guard against a tampered path escaping it) and
    its job row deleted. Archive renders, export-zip jobs, and captured frames are
    never considered. Synchronous; call via a thread executor.

    Trigger-exempt for a ``manual`` render: a manual completion never runs this
    action (a one-off manual render must not sweep away earlier renders). It still
    runs for ``scheduled`` and ``archive`` triggers with the keep-count and
    archive-exclusion behaviour above.

    For a recurring series the automatic, schedule-scoped auto-prune
    (:func:`_auto_prune`) supersedes this manually configured action: it keeps
    only the latest render of the *same* kind, scoped per schedule, without a
    configured ``post_render_actions`` entry.
    """
    if kind == "manual":
        return

    keep: Any = spec.get("keep", spec.get("keep_count"))
    if keep is None:
        raise ValueError("prune action requires an integer 'keep' count")
    try:
        keep_count = max(0, int(keep))
    except (TypeError, ValueError) as exc:
        raise ValueError("prune action requires an integer 'keep' count") from exc

    from .spec import project_render_root  # local import avoids a cycle

    with session_scope(session_factory) as session:
        project = _project_for_prune(session, project_id)
        render_root = project_render_root(settings, project).resolve()
        candidates = (
            session.execute(
                select(RenderJob)
                .where(RenderJob.project_id == project_id)
                # Never a prune candidate: archive renders (kept by policy) and
                # export jobs (a zip of frames, not a render output -- pruning one
                # would delete a user's export artifact and its job row, breaking
                # its download). The configured-prune action retains rendered
                # videos only.
                .where(RenderJob.kind.not_in(("archive", "export")))
                .where(RenderJob.status == "done")
                .order_by(RenderJob.id.desc())
            )
            .scalars()
            .all()
        )
        for job in candidates[keep_count:]:
            _delete_render_output(job, render_root)
            session.delete(job)


def _auto_prune(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    project_id: int,
    kind: str,
) -> None:
    """Keep only the latest render of ``kind`` for the project; delete the rest.

    The automatic, schedule-scoped retention applied after a recurring render: a
    successful ``scheduled`` or ``archive`` render prunes only prior renders of
    that **same** kind for the **same** project, leaving the single newest one.
    Scope is the same kind only -- a ``scheduled`` completion never touches
    ``archive`` renders and vice versa -- and ``manual`` renders are never a
    trigger and are never deleted by this. It runs only when the relevant
    schedule has auto-prune enabled: the ``render_schedule`` for ``scheduled``,
    the ``archive_schedule`` for ``archive``.

    Idempotent and safe: only ``done`` rows are considered, ``manual`` rows are
    untouched, "latest" is the highest id (consistent with the configured prune
    action), and each output file is removed only when it resolves inside the
    project's render root. Synchronous; call via a thread executor.
    """
    if kind not in ("scheduled", "archive"):
        return

    from .settings import auto_prune_enabled  # local import: authored in parallel
    from .spec import project_render_root  # local import avoids a cycle

    with session_scope(session_factory) as session:
        project = _project_for_prune(session, project_id)
        schedule = (
            project.render_schedule if kind == "scheduled" else project.archive_schedule
        )
        if not auto_prune_enabled(schedule):
            return
        render_root = project_render_root(settings, project).resolve()
        candidates = (
            session.execute(
                select(RenderJob)
                .where(RenderJob.project_id == project_id)
                .where(RenderJob.kind == kind)
                .where(RenderJob.status == "done")
                .order_by(RenderJob.id.desc())
            )
            .scalars()
            .all()
        )
        # Keep only the newest (N=1); delete every older render of this kind.
        for job in candidates[1:]:
            _delete_render_output(job, render_root)
            session.delete(job)


def _delete_render_output(job: RenderJob, render_root: Path) -> None:
    """Unlink a render's output file when confined to the render root.

    Never touches frames or anything outside the render root: a stored path that
    does not resolve inside it is left on disk (only the row is removed by the
    caller), so a corrupted path can never trigger a delete elsewhere.
    """
    if not job.output_file_path:
        return
    resolved = Path(job.output_file_path).resolve()
    if not resolved.is_relative_to(render_root):
        logger.warning(
            "skipping prune of render %s: output %s is outside render root %s",
            job.id,
            resolved,
            render_root,
        )
        return
    with contextlib.suppress(FileNotFoundError):
        resolved.unlink()


def _project_for_prune(session: Session, project_id: int) -> Any:
    """Load the project row for prune, or raise if it vanished."""
    from ..db.models import Project

    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} no longer exists")
    return project


def _project_id_for_job(
    session_factory: sessionmaker[Session], job_id: int
) -> int | None:
    """Return the project id a render job belongs to, or ``None``. Synchronous."""
    with session_scope(session_factory) as session:
        job = session.get(RenderJob, job_id)
        return job.project_id if job is not None else None


def _record_event(
    session_factory: sessionmaker[Session],
    *,
    project_id: int,
    level: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a post-action-failure event so it is delivered to channels.

    Routed through :func:`log_event` with the ``postaction.failed`` type so the
    notification dispatcher recognises it and applies the configured routing
    rules (previously the event was written without a type and therefore never
    routed). Synchronous; opens its own short transaction.
    """
    with session_scope(session_factory) as session:
        log_event(
            session,
            scope="project",
            scope_id=project_id,
            level=level,
            type=EventType.POSTACTION_FAILED.value,
            message=message,
            metadata=metadata,
        )
