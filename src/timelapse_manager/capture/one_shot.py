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
* the same camera/credential loading and PTZ-positioning the interval loop uses.

Bookkeeping isolation is deliberate: a one-shot capture **must not** touch the
interval runner's cadence anchor (``CaptureState.last_capture_at``). To make that
impossible by construction, this function takes no :class:`CaptureState` -- it
keeps interval, exact-time and event capture cadence-independent.

The disk gate is the one universal gate that applies here (the schedule gate is
interval-only by design). When free space is below the watermark the capture is
*skipped* (returns ``None``), not failed -- so an anchor or event never fills a
full disk and a low-disk skip never burns a producer's retry. A genuine capture
failure raises a :class:`~timelapse_manager.cameras.base.CaptureError`, which the
caller records as a failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..cameras import build_adapter
from .frame_writer import WrittenFrame

if TYPE_CHECKING:
    from .supervisor import CaptureSupervisor, CaptureTarget

logger = logging.getLogger(__name__)


async def capture_one_now(
    supervisor: CaptureSupervisor,
    target: CaptureTarget,
    *,
    reason: str,
    trigger: dict[str, object] | None = None,
    dedup_key: str | None = None,
) -> WrittenFrame | None:
    """Capture a single frame for ``target`` now, off the interval cadence.

    Loads the camera snapshot and default credentials off the event loop, builds
    the adapter on the supervisor's shared HTTP client, applies the project's PTZ
    position, captures under the configured timeout, persists the frame via the
    shared writer (tagging it with ``capture_reason=reason``), and records a
    project-scoped audit event describing the reason and trigger detail.

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
    disk_ok = await asyncio.to_thread(
        supervisor._disk_monitor.is_capture_allowed, volume_path
    )
    if not disk_ok:
        logger.info(
            "one-shot capture skipped for low disk space project=%s reason=%s",
            target.project_id,
            reason,
        )
        await asyncio.to_thread(
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

    config = await asyncio.to_thread(supervisor._load_camera, target.camera_id)
    if config is None:
        raise RuntimeError(f"camera {target.camera_id} no longer exists")
    default_credentials = await asyncio.to_thread(supervisor._load_default_credentials)
    adapter = build_adapter(
        config,
        supervisor.http_client,
        ffmpeg_binary=supervisor.ffmpeg_binary,
        default_credentials=default_credentials,
        stream_id=target.stream_id,
    )
    try:
        await supervisor._apply_ptz(target, adapter)
        captured = await supervisor._capture_with_timeout(adapter)
    finally:
        await adapter.close()
    if captured is None:
        # A timeout is a transient failure, surfaced the same way the interval
        # loop surfaces it, so the caller records it as a failed attempt.
        from ..cameras.base import TimeoutCaptureError

        raise TimeoutCaptureError("one-shot capture timed out")

    written = await asyncio.to_thread(
        supervisor.frame_writer.write,
        target.project_id,
        captured,
        stream_id=target.stream_id,
        capture_reason=reason,
    )
    await asyncio.to_thread(
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
