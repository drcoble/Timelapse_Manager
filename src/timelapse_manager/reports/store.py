"""Report rows and files: keys, input validation, rate limit, retention.

Everything here is synchronous and takes an explicit session or settings, so
the web route, the API and the CLI share it without a running service.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Report
from ..services.errors import AppError, ValidationError

logger = logging.getLogger(__name__)

# The areas a report can be filed under, in the order the form lists them.
AREAS: dict[str, str] = {
    "capture": "Capture",
    "project": "Project page",
    "renders": "Renders",
    "cameras": "Cameras",
    "login": "Login / LDAP",
    "other": "Other",
}
DEFAULT_AREA = "other"

# The destinations a report can be delivered to. ``local`` is the stored zip.
DESTINATIONS = ("local", "email", "webhook", "gitea")

# ``TLM-YYYYMMDD-NNNN``. Anything reaching the filesystem or a URL is checked
# against this first, so a key can never carry a path separator.
REPORT_KEY_PATTERN = re.compile(r"\ATLM-\d{8}-\d{4}\Z")

# Longest title accepted, in characters.
TITLE_MAX_CHARS = 200

# How far back the user may say the problem happened, and how far ahead of the
# server clock a submitted time may sit (browser clock skew).
REPORTED_AT_MAX_AGE = timedelta(hours=24)
REPORTED_AT_FUTURE_SLACK = timedelta(minutes=5)

# The reports directory is created owner/group-only; zips inside it hold
# operational detail, not secrets, but are still not for other local users.
_DIR_MODE = 0o750
_FILE_MODE = 0o640

# Retries for the (rare) race where two reports claim the same daily sequence.
KEY_ALLOCATION_ATTEMPTS = 5


class ReportRateLimitedError(AppError):
    """The user has filed the hourly maximum of reports."""

    status = 429
    code = "rate_limited"


class ReportTooLargeError(AppError):
    """The upload or the text is over its size cap."""

    status = 413
    code = "too_large"


@dataclass(frozen=True)
class Attachment:
    """A validated image upload: its canonical extension, type and bytes."""

    extension: str
    content_type: str
    data: bytes

    @property
    def member_name(self) -> str:
        """The zip member name. Never the client's filename."""
        return f"attachment.{self.extension}"


# Magic-byte signatures for the accepted image types: (extension, MIME type).
_IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def sniff_image(data: bytes) -> str | None:
    """Return the image extension for ``data``'s magic bytes, or ``None``."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return None


def validate_attachment(
    content_type: str, data: bytes, *, max_bytes: int
) -> Attachment:
    """Accept an image upload only if its declared type and its bytes agree.

    The declared ``Content-Type`` must be one of PNG/JPEG/WebP/GIF *and* the
    magic bytes must match that same type, so a renamed executable or an SVG
    (which can carry script) is refused.

    :raises ReportTooLargeError: when the upload exceeds ``max_bytes``.
    :raises ValidationError: when it is not one of the accepted images.
    """
    if len(data) > max_bytes:
        raise ReportTooLargeError(
            f"The upload is larger than {max_bytes // (1024 * 1024)} MB.",
            field="attachment",
        )
    declared = content_type.split(";", 1)[0].strip().lower()
    sniffed = sniff_image(data)
    if sniffed is None or _IMAGE_TYPES[sniffed] != declared:
        raise ValidationError(
            "Only PNG, JPEG, WebP or GIF images can be attached.",
            field="attachment",
        )
    return Attachment(extension=sniffed, content_type=declared, data=data)


def validate_text(title: str, description: str, *, max_text_bytes: int) -> None:
    """Check the title and free text against their caps.

    :raises ValidationError: when the title is empty or too long.
    :raises ReportTooLargeError: when the description is over the byte cap.
    """
    if not title.strip():
        raise ValidationError("Give the report a title.", field="title")
    if len(title) > TITLE_MAX_CHARS:
        raise ValidationError(
            f"The title is longer than {TITLE_MAX_CHARS} characters.", field="title"
        )
    if len(description.encode("utf-8")) > max_text_bytes:
        raise ReportTooLargeError(
            f"The description is longer than {max_text_bytes:,} bytes.",
            field="description",
        )


def normalise_area(area: str | None) -> str:
    """Return ``area`` when it is a known area, else the default."""
    return area if area in AREAS else DEFAULT_AREA


def check_reported_at(reported_at: datetime, now: datetime) -> datetime:
    """Validate "when it happened" (naive UTC) against the last-24-hours window.

    :raises ValidationError: when it is in the future or more than 24 h ago.
    """
    if reported_at > now + REPORTED_AT_FUTURE_SLACK:
        raise ValidationError("The time cannot be in the future.", field="when")
    if reported_at < now - REPORTED_AT_MAX_AGE:
        raise ValidationError(
            "The time must be within the last 24 hours.", field="when"
        )
    return min(reported_at, now)


def utcnow() -> datetime:
    """The current time as naive UTC, matching the database columns."""
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Rate limit and keys
# ---------------------------------------------------------------------------


def check_rate_limit(
    session: Session, user_id: int | None, *, per_hour: int, now: datetime
) -> None:
    """Refuse a report when the user made ``per_hour`` attempts in the last hour.

    Counted from the ``report`` table plus the ``report.refused`` audit
    events (#21): a refused anonymized report leaves no row, but it still
    cost a full snapshot build, so it counts. Both are in the database, so
    the limit survives a restart.

    :raises ReportRateLimitedError: when the limit is reached.
    """
    if user_id is None:
        return
    since = now - timedelta(hours=1)
    count = session.execute(
        select(func.count(Report.id))
        .where(Report.user_id == user_id)
        .where(Report.created_at >= since)
    ).scalar_one()
    count += _refusals_since(session, user_id, since)
    if count >= per_hour:
        raise ReportRateLimitedError(
            f"You can file {per_hour} reports an hour. Try again later."
        )


# The audit action a refused anonymized report leaves behind (#21).
REFUSED_ACTION = "report.refused"


def _refusals_since(session: Session, user_id: int, since: datetime) -> int:
    """The user's refused report attempts since ``since`` (audit events)."""
    from ..db.models import Event
    from ..monitoring.events import EventType

    rows = session.execute(
        select(Event.event_metadata)
        .where(Event.actor_user_id == user_id)
        .where(Event.scope == "system")
        .where(Event.timestamp >= since)
    ).scalars()
    return sum(
        1
        for metadata in rows
        if isinstance(metadata, dict)
        and metadata.get("type") == EventType.AUDIT_CONTROL_ACTION.value
        and metadata.get("action") == REFUSED_ACTION
    )


def next_report_key(session: Session, now: datetime) -> str:
    """Return the next never-issued ``TLM-YYYYMMDD-NNNN`` for ``now``'s UTC date.

    The sequence is one more than the highest number of that day ever issued:
    the keys still in the table and the keys the audit log records as filed,
    deleted or pruned (so deleting the day's newest report never frees its key
    for the next one). Only the structured ``report_id`` / ``report_ids``
    metadata the application writes on those audit events is read, never free
    message text, and every candidate must be a well-formed key of that day.

    On SQLite the database write lock is taken first, so the read and the
    caller's insert happen under one lock and concurrent submissions queue
    instead of computing the same key. The unique constraint (and the caller's
    retry, see :data:`KEY_ALLOCATION_ATTEMPTS`) still backs this up.
    """
    prefix = f"TLM-{now:%Y%m%d}-"
    _lock_for_key_allocation(session)
    keys = list(
        session.execute(
            select(Report.report_key).where(Report.report_key.like(f"{prefix}%"))
        ).scalars()
    )
    keys += _keys_in_audit_log(session, prefix)
    numbers = [
        int(key.rsplit("-", 1)[1])
        for key in keys
        if key.startswith(prefix) and REPORT_KEY_PATTERN.match(key)
    ]
    return f"{prefix}{max(numbers, default=0) + 1:04d}"


# Audit actions whose metadata names report keys the application issued.
_KEY_AUDIT_ACTIONS = frozenset(
    {"report.filed", "report.delete", "report.prune", "report.refused"}
)


def _keys_in_audit_log(session: Session, prefix: str) -> list[str]:
    """Report keys of one day recorded in the metadata of report audit events."""
    from ..db.models import Event
    from ..monitoring.events import EventType

    rows = session.execute(
        select(Event.event_metadata).where(
            Event.scope == "system", Event.message.like(f"%{prefix}%")
        )
    ).scalars()
    keys: list[str] = []
    for metadata in rows:
        if not isinstance(metadata, dict):
            continue
        if metadata.get("type") != EventType.AUDIT_CONTROL_ACTION.value:
            continue
        if metadata.get("action") not in _KEY_AUDIT_ACTIONS:
            continue
        single = metadata.get("report_id")
        if isinstance(single, str):
            keys.append(single)
        many = metadata.get("report_ids")
        if isinstance(many, list):
            keys += [key for key in many if isinstance(key, str)]
    return keys


def _lock_for_key_allocation(session: Session) -> None:
    """Take SQLite's write lock now, before the day's keys are read.

    A no-op ``UPDATE`` opens the write transaction (the driver issues ``BEGIN``
    before any write), so a second submission's allocation waits on the busy
    timeout until the first has inserted its row and committed. Other
    databases rely on the unique constraint and the caller's retry.
    """
    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.execute(text("UPDATE report SET id = id WHERE 0"))


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def reports_dir(settings: Settings) -> Path:
    """The directory report zips are stored in: ``<data_dir>/reports``."""
    return settings.paths.data_dir / "reports"


def zip_path_for(settings: Settings, report: Report) -> Path | None:
    """The absolute path of ``report``'s zip, or ``None`` when it has none.

    The stored name is re-derived from the validated key rather than trusted,
    so a tampered row cannot point the download outside the reports directory.
    """
    if not report.zip_path or not REPORT_KEY_PATTERN.match(report.report_key):
        return None
    return reports_dir(settings) / f"{report.report_key}.zip"


class UnsafeTargetError(ValueError):
    """The destination is a symlink or not a regular file; it is not replaced."""


def atomic_write(target: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``target`` without ever following a planted link.

    The bytes go to a fresh ``mkstemp`` file (``O_EXCL``, random name) in the
    target's directory, are fsynced, then ``os.replace``-d into place. Nothing
    is opened by the final name, so a symlink there cannot redirect the write
    into another file; an existing symlink or non-regular file at the target
    is refused outright rather than silently replaced.

    :raises UnsafeTargetError: when ``target`` is a symlink or not a file.
    """
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise UnsafeTargetError(f"refusing to replace {target}: not a regular file")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".partial", dir=target.parent
    )
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def write_zip(settings: Settings, report_key: str, data: bytes) -> Path:
    """Write a report zip atomically and return its path."""
    if not REPORT_KEY_PATTERN.match(report_key):
        raise ValueError(f"invalid report key {report_key!r}")
    directory = reports_dir(settings)
    directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    target = directory / f"{report_key}.zip"
    atomic_write(target, data, _FILE_MODE)
    return target


# ---------------------------------------------------------------------------
# Delivery bookkeeping
# ---------------------------------------------------------------------------


def roll_up(deliveries: dict[str, dict[str, object]]) -> str:
    """The ``delivery_state`` for a deliveries map.

    ``failed`` when any destination failed, else ``pending`` while any is in
    flight, else ``ok``. Skipped (unconfigured) destinations do not count.
    """
    statuses = {entry.get("status") for entry in deliveries.values()}
    if "failed" in statuses:
        return "failed"
    if "pending" in statuses:
        return "pending"
    return "ok"


def set_delivery(
    report: Report, destination: str, status: str, **detail: object
) -> None:
    """Record one destination's outcome on ``report`` and refresh the roll-up."""
    deliveries = dict(report.deliveries or {})
    entry: dict[str, object] = {"status": status, "at": utcnow().isoformat()}
    entry.update({key: value for key, value in detail.items() if value is not None})
    deliveries[destination] = entry
    # Reassign (not mutate) so SQLAlchemy sees the JSON column change.
    report.deliveries = deliveries
    report.delivery_state = roll_up(deliveries)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def prune_reports(session: Session, settings: Settings, now: datetime) -> list[str]:
    """Apply retention: the age limit, then the count limit. Periodic only.

    Runs from the scheduler's housekeeping pass, never on the submit path, so
    filing a report can never evict anyone else's. Two rules:

    * **Age.** Every report older than ``retention_days`` goes.
    * **Count.** Beyond the newest ``retention_max_reports``, only reports
      whose delivery finished (``delivery_state == "ok"``) go; one still
      pending or failed is kept for an admin to resend until it ages out.

    Removes both the row and its zip, and writes one system audit event naming
    the pruned keys. Returns the pruned keys. A zip that is already gone is not
    an error; one that cannot be removed is logged and the row still goes, so
    retention never stalls on a stray file.
    """
    from ..monitoring.events import EventType, log_event

    reporting = settings.reporting
    cutoff = now - timedelta(days=reporting.retention_days)
    expired = list(
        session.execute(
            select(Report).where(Report.created_at < cutoff).order_by(Report.id)
        ).scalars()
    )
    survivors = list(
        session.execute(
            select(Report)
            .where(Report.created_at >= cutoff)
            .order_by(Report.created_at.desc(), Report.id.desc())
        ).scalars()
    )
    expired += [
        report
        for report in survivors[reporting.retention_max_reports :]
        if report.delivery_state == "ok"
    ]
    pruned: list[str] = []
    for report in expired:
        delete_report_files(settings, report)
        pruned.append(report.report_key)
        session.delete(report)
    if pruned:
        pruned.sort()
        logger.info("pruned %d problem report(s)", len(pruned))
        log_event(
            session,
            scope="system",
            scope_id=None,
            level="info",
            message=(
                f"retention pruned {len(pruned)} problem report(s): "
                + ", ".join(pruned)
            ),
            type=EventType.AUDIT_CONTROL_ACTION.value,
            metadata={"action": "report.prune", "report_ids": pruned},
        )
    return pruned


def delete_report_files(settings: Settings, report: Report) -> None:
    """Remove a report's zip, if any. Never raises."""
    path = zip_path_for(settings, report)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "could not remove report zip %s: %s", report.report_key, type(exc).__name__
        )
