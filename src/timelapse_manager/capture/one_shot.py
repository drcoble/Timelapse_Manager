"""The unified one-shot capture path.

Several producers want to capture a *single* frame outside the interval cadence:
an exact-time anchor firing once a day, or a camera event triggering a frame.
Rather than each re-implementing adapter construction, positioning, timeout
handling, frame persistence and audit logging, they all funnel through one
function, :func:`capture_one_now`, which is a thin orchestration over machinery
that already lives on the capture supervisor.

By living on the supervisor's shared surface it inherits, by construction:

* the shared :class:`httpx.AsyncClient` (for HTTP-based adapters, the
  camera-allowlisted client every adapter GET is guarded against);
* the shared :class:`~timelapse_manager.capture.frame_writer.FrameWriter`, which
  already holds the per-project write lock and the unique-sequence backstop, so
  no new locking is invented and writes are serialised against interval capture;
* the resolved ffmpeg binary;
* the same per-camera session (the cached camera config and credentials, one
  merged hop on a miss), capture slot, PTZ positioning and post-capture
  scene-metadata step the interval loop uses;
* the supervisor's executor seam (``supervisor._run_blocking``) for every
  blocking hop.

Bookkeeping isolation is deliberate: a one-shot capture **must not** touch the
interval runner's cadence anchor (``CaptureState.last_capture_at``). To make that
impossible by construction, this function takes no :class:`CaptureState` -- it
keeps interval, exact-time and event capture cadence-independent.

The disk gate is the one universal gate that applies here (the schedule gate is
interval-only by design). When free space is below the watermark the capture is
*skipped* (returns ``None``), not failed -- so an anchor or event never fills a
full disk and a low-disk skip never burns a producer's retry. A genuine capture
failure raises a :class:`~timelapse_manager.cameras.base.CaptureError`, which the
caller records as a failure. By default the failure also writes one project
event (#69); a caller that writes its own, richer failure event (the exact-time
anchors) turns that off.

Camera health (#69 CAM-R6) is fed by the *callers*, not here: the exact-time
anchors report each attempt (including their own attempt timeout, which this
function never sees) and the event listener reports each triggered capture.
Reporting here as well would count an anchor attempt twice. A deleted camera
raises :class:`~timelapse_manager.cameras.base.CameraMissingError`, a local
failure that health ignores; so does a stored credential that cannot be
decrypted, as a named local ``OtherCaptureError`` (#69 CAM-R8).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..cameras import build_adapter
from ..logging import redact_text
from .frame_writer import WrittenFrame

if TYPE_CHECKING:
    from collections.abc import Callable

    from .supervisor import CaptureSupervisor, CaptureTarget

logger = logging.getLogger(__name__)


async def capture_one_now(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    *,
    reason: str,
    trigger: dict[str, object] | None = None,
    dedup_key: str | None = None,
    on_failure_event: bool = True,
    on_written: Callable[[WrittenFrame], None] | None = None,
    slot_wait_seconds: float | None = None,
) -> WrittenFrame | None:
    """Capture a single frame for ``target`` now, off the interval cadence.

    Loads the camera snapshot and (when it inherits them) the default
    credentials in one off-loop hop, builds the adapter on the supervisor's
    shared HTTP client, applies the project's PTZ position, captures under the
    configured timeout, collects scene metadata under its own total timeout
    (outside the capture budget, so a slow read never discards the frame),
    persists the frame via the shared writer (tagging it with
    ``capture_reason=reason``), and records a project-scoped audit event
    describing the reason and trigger detail.

    :param supervisor: the live supervisor; its shared client, writer, ffmpeg
        binary, disk monitor and camera/credential loaders are reused.
    :param target: the project capture configuration snapshot (project id,
        camera, stream, PTZ, storage path). Producers that do not have one from
        the interval loop build their own; this function never consults the
        supervisor's interval ``_runners`` map.
    :param reason: why the frame is being captured, recorded verbatim on the
        frame's ``capture_reason`` and in the audit event (e.g. ``"anchor:clock"``,
        ``"anchor:solar_noon"``, ``"event:<topic>"``).
    :param trigger: optional provenance detail merged into the audit event's
        metadata alongside the reason (e.g. ``{"anchor_id": ..., "local_date":
        ...}`` for an anchor, or the matched event fields).
    :param dedup_key: an opaque key carried for the caller's own logging and
        provenance. Idempotency is **not** enforced here -- it belongs to the
        caller (the exact-time fire-log's unique constraint, the event
        listener's cooldown). Recorded in the audit metadata when present.
    :param on_failure_event: when true (the default), a failed attempt writes one
        warning project event (the error text redacted) before the error is
        re-raised. The exact-time anchors pass ``False`` and write their own
        event, which carries the attempt count and the retry decision.
    :param on_written: an optional synchronous callback run in the *same*
        blocking hop as the frame write, right after it. A caller that must
        record the outcome durably (the anchor fire-log) uses it so the record
        cannot be lost to a cancellation landing between the write and a
        separate hop: once the write starts, the thread finishes both.
    :param slot_wait_seconds: the longest to wait for the camera's capture
        slot (see :meth:`CaptureSupervisor.capture_slot`); ``None`` is the
        scheduled-capture wait. The API manual capture passes ``0``: it takes a
        slot only when one is free and never queues ahead of an anchor.
    :returns: the :class:`WrittenFrame` on success, or ``None`` when the capture
        was skipped because free disk space is below the low watermark (a skip,
        not a failure).
    :raises CaptureError: (a subclass) when the camera could not be reached,
        authenticated, positioned, or captured -- so the caller can record the
        attempt as a failure. A skip (low disk) returns ``None`` instead.
    """
    # Universal disk gate: skip (do not fail) when free space is low, so a
    # one-shot capture never fills a full disk and the skip never burns a retry.
    volume_path = supervisor._volume_path(target)
    disk_ok = await supervisor._run_blocking(
        supervisor._disk_monitor.is_capture_allowed, volume_path
    )
    if not disk_ok:
        logger.info(
            "one-shot capture skipped for low disk space project=%s reason=%s",
            target.project_id,
            reason,
        )
        await supervisor._run_blocking(
            supervisor._write_event,
            scope_id=target.project_id,
            level="warning",
            message=(
                f"one-shot capture skipped for project {target.project_name!r}: "
                f"free disk space below the low watermark on {volume_path}"
            ),
            metadata=_event_metadata(reason, trigger, dedup_key, skipped="low_disk"),
        )
        return None

    try:
        written = await _capture_and_write(
            supervisor, target, reason, on_written, slot_wait_seconds
        )
    except Exception as exc:
        if on_failure_event:
            await _write_failure_event(
                supervisor, target, exc, _event_metadata(reason, trigger, dedup_key)
            )
        raise
    await supervisor._run_blocking(
        supervisor._write_event,
        scope_id=target.project_id,
        level="info",
        message=(
            f"captured a frame for project {target.project_name!r} (reason: {reason})"
        ),
        metadata=_event_metadata(reason, trigger, dedup_key),
    )
    logger.info(
        "one-shot capture wrote frame project=%s reason=%s seq=%s",
        target.project_id,
        reason,
        written.sequence_index,
    )
    return written


def _event_metadata(
    reason: str,
    trigger: dict[str, object] | None,
    dedup_key: str | None,
    *,
    skipped: str | None = None,
) -> dict[str, object]:
    """Build the audit-event metadata for a one-shot capture or skip."""
    metadata: dict[str, object] = {"reason": reason}
    if trigger:
        metadata.update(trigger)
    if dedup_key is not None:
        metadata["dedup_key"] = dedup_key
    if skipped is not None:
        metadata["skipped"] = skipped
    return metadata


async def _capture_and_write(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    reason: str,
    on_written: Callable[[WrittenFrame], None] | None,
    slot_wait_seconds: float | None = None,
) -> WrittenFrame:
    """Load the camera, capture one frame and persist it (no audit event).

    Raises on any failure; :func:`capture_one_now` events it. ``on_written``
    runs in the write's own blocking hop (see :func:`capture_one_now`).

    The camera config and credentials come from the camera's cached session,
    and the camera work runs while holding one of its capture slots (taken
    outside the capture budget, released before the write), exactly as the
    interval loop does (#69 CAM-P1, CAM-R4).
    """
    session = await supervisor.camera_sessions.get(target.camera_id)
    adapter = build_adapter(
        session.config,
        supervisor.capture_client_for(session.config),
        ffmpeg_binary=supervisor.ffmpeg_binary,
        default_credentials=session.config.default_credentials,
        stream_id=target.stream_id,
        credentials=session.credentials,
        uri_cache=session.uri_cache,
        knob_cache=session.knob_cache,
        auth_state=session.auth_state,
    )
    try:
        async with supervisor.capture_slot(target, wait_seconds=slot_wait_seconds):
            await supervisor._apply_ptz(target, adapter)
            captured = await supervisor._capture_with_timeout(adapter)
            if captured is not None:
                await supervisor.collect_scene_metadata(adapter, captured)
    finally:
        await adapter.close()
    if captured is None:
        # A timeout is a transient failure, surfaced the same way the interval
        # loop surfaces it, so the caller records it as a failed attempt.
        from ..cameras.base import TimeoutCaptureError

        raise TimeoutCaptureError("one-shot capture timed out")
    frame = captured

    def write_frame() -> WrittenFrame:
        written = supervisor.frame_writer.write(
            target.project_id,
            frame,
            stream_id=target.stream_id,
            capture_reason=reason,
        )
        if on_written is not None:
            on_written(written)
        return written

    written = await supervisor._run_blocking(write_frame)
    await supervisor._report_capture_degradations(target, frame)
    return written


async def _write_failure_event(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    exc: Exception,
    metadata: dict[str, object],
) -> None:
    """Record one warning event for a failed one-shot attempt (#69).

    Best effort: the capture error is what the caller must see, so a failure to
    write the event is logged, not raised in its place.
    """
    error = redact_text(str(exc) or type(exc).__name__)[:200]
    try:
        await supervisor._run_blocking(
            supervisor._write_event,
            scope_id=target.project_id,
            level="warning",
            message=(
                f"one-shot capture failed for project {target.project_name!r}: {error}"
            ),
            metadata={**metadata, "failed": type(exc).__name__, "error": error},
        )
    except asyncio.CancelledError:
        raise
    except Exception as event_exc:  # noqa: BLE001 - never mask the capture error
        logger.warning(
            "could not record one-shot failure event project=%s: %s",
            target.project_id,
            type(event_exc).__name__,
        )
