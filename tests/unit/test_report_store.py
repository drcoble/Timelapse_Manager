"""Unit tests for problem-report storage rules (#8).

Upload sniffing, text caps, the reported-time window, key allocation, the
delivery roll-up, the value scrubber, and retention pruning (by age and by
count, removing the zip with the row).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.reports_support import PNG_BYTES
from timelapse_manager.config.settings import ReportingSettings
from timelapse_manager.db.models import Event, Report
from timelapse_manager.db.session import session_scope
from timelapse_manager.reports.redaction import REDACTED, Scrubber
from timelapse_manager.reports.store import (
    ReportTooLargeError,
    check_reported_at,
    next_report_key,
    prune_reports,
    roll_up,
    sniff_image,
    utcnow,
    validate_attachment,
    validate_text,
    write_zip,
)
from timelapse_manager.runtime import get_context
from timelapse_manager.services.errors import ValidationError

_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
_WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
_GIF = b"GIF89a" + b"\x00" * 8


@pytest.mark.parametrize(
    ("data", "content_type", "extension"),
    [
        (PNG_BYTES, "image/png", "png"),
        (_JPEG, "image/jpeg", "jpg"),
        (_WEBP, "image/webp", "webp"),
        (_GIF, "image/gif", "gif"),
    ],
)
def test_accepts_each_image_type(
    data: bytes, content_type: str, extension: str
) -> None:
    attachment = validate_attachment(content_type, data, max_bytes=1024)
    assert attachment.extension == extension
    assert attachment.member_name == f"attachment.{extension}"


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        (b"MZ\x90\x00", "image/png"),  # an executable declared as PNG
        (PNG_BYTES, "image/jpeg"),  # type and bytes disagree
        (PNG_BYTES, "application/octet-stream"),
        (b"RIFF\x00\x00\x00\x00WAVE", "image/webp"),
    ],
)
def test_rejects_non_images(data: bytes, content_type: str) -> None:
    with pytest.raises(ValidationError):
        validate_attachment(content_type, data, max_bytes=1024)


def test_rejects_oversize_upload() -> None:
    with pytest.raises(ReportTooLargeError):
        validate_attachment("image/png", PNG_BYTES + b"\x00" * 100, max_bytes=50)


def test_sniff_ignores_short_input() -> None:
    assert sniff_image(b"RIFF") is None
    assert sniff_image(b"") is None


def test_text_caps_are_utf8_bytes() -> None:
    validate_text("t", "é" * 10, max_text_bytes=20)
    with pytest.raises(ReportTooLargeError):
        validate_text("t", "é" * 11, max_text_bytes=20)
    with pytest.raises(ValidationError):
        validate_text("   ", "", max_text_bytes=20)
    with pytest.raises(ValidationError):
        validate_text("x" * 201, "", max_text_bytes=20)


def test_reported_at_window() -> None:
    now = utcnow()
    assert check_reported_at(now - timedelta(hours=23), now) == now - timedelta(
        hours=23
    )
    # A little browser clock skew is clamped to now, not refused.
    assert check_reported_at(now + timedelta(minutes=2), now) == now
    with pytest.raises(ValidationError):
        check_reported_at(now - timedelta(hours=25), now)
    with pytest.raises(ValidationError):
        check_reported_at(now + timedelta(hours=1), now)


def test_roll_up() -> None:
    assert roll_up({"local": {"status": "ok"}}) == "ok"
    assert roll_up({"local": {"status": "ok"}, "gitea": {"status": "pending"}}) == (
        "pending"
    )
    assert roll_up({"local": {"status": "ok"}, "email": {"status": "failed"}}) == (
        "failed"
    )


def test_scrubber_replaces_every_encoding() -> None:
    secret = 'pa ss"word/1'
    scrubber = Scrubber([secret, "abc"])  # too short to scrub
    blob = (
        b'raw pa ss"word/1 json "pa ss\\"word/1" '
        b"url pa%20ss%22word%2F1 plus pa+ss%22word%2F1 abc"
    )
    out = scrubber.scrub(blob)
    assert b"pa ss" not in out
    assert b"pa%20ss" not in out
    assert b"pa+ss" not in out
    assert out.count(REDACTED.encode()) == 4
    assert out.endswith(b"abc")


def test_write_zip_rejects_bad_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_zip(get_settings_stub(tmp_path), "../escape", b"x")


def get_settings_stub(tmp_path: Path) -> object:
    from timelapse_manager.config.settings import PathsSettings, Settings

    return Settings(paths=PathsSettings(data_dir=tmp_path))


# ---------------------------------------------------------------------------
# Keys and retention (against a migrated database)
# ---------------------------------------------------------------------------


def _add(session: object, key: str, age: timedelta, state: str = "ok") -> None:
    session.add(  # type: ignore[attr-defined]
        Report(
            report_key=key,
            area="other",
            title=key,
            description="",
            reported_at=utcnow() - age,
            created_at=utcnow() - age,
            delivery_state=state,
            zip_path=f"{key}.zip",
        )
    )


def test_next_key_counts_per_day(web_client: TestClient) -> None:
    now = utcnow()
    prefix = f"TLM-{now:%Y%m%d}-"
    with session_scope(get_context().session_factory) as session:
        assert next_report_key(session, now) == f"{prefix}0001"
        _add(session, f"{prefix}0009", timedelta(0))
        _add(session, "TLM-19990101-0042", timedelta(0))
        session.flush()
        assert next_report_key(session, now) == f"{prefix}0010"


def _audit_event(
    session: object, message: str, metadata: dict[str, Any] | None
) -> None:
    session.add(  # type: ignore[attr-defined]
        Event(scope="system", level="info", message=message, event_metadata=metadata)
    )


def test_next_key_skips_keys_the_audit_log_records(web_client: TestClient) -> None:
    """Deleted and pruned keys of the day are never issued again."""
    now = utcnow()
    prefix = f"TLM-{now:%Y%m%d}-"
    audit = "audit.control_action"
    with session_scope(get_context().session_factory) as session:
        _add(session, f"{prefix}0001", timedelta(0))
        _audit_event(
            session,
            f"problem report {prefix}0003 deleted",
            {"type": audit, "action": "report.delete", "report_id": f"{prefix}0003"},
        )
        session.flush()
        assert next_report_key(session, now) == f"{prefix}0004"
        _audit_event(
            session,
            f"retention pruned 1 problem report(s): {prefix}0006",
            {"type": audit, "action": "report.prune", "report_ids": [f"{prefix}0006"]},
        )
        session.flush()
        assert next_report_key(session, now) == f"{prefix}0007"


def test_next_key_ignores_unstructured_or_malformed_values(
    web_client: TestClient,
) -> None:
    """Only app-written audit metadata counts; user text and junk do not."""
    now = utcnow()
    prefix = f"TLM-{now:%Y%m%d}-"
    audit = "audit.control_action"
    with session_scope(get_context().session_factory) as session:
        _add(session, f"{prefix}0002", timedelta(0))
        # A username carrying a key: the message matches, but the metadata
        # names the real key.
        _audit_event(
            session,
            f"user 'x {prefix}9000' filed problem report {prefix}0002",
            {"type": audit, "action": "report.filed", "report_id": f"{prefix}0002"},
        )
        # Free text with no structured metadata.
        _audit_event(session, f"note: {prefix}8000 looked odd", None)
        # A foreign action or type naming a key.
        _audit_event(
            session,
            f"{prefix}7000",
            {"type": audit, "action": "camera.update", "report_id": f"{prefix}7000"},
        )
        _audit_event(
            session,
            f"{prefix}6000",
            {"type": "other", "action": "report.delete", "report_id": f"{prefix}6000"},
        )
        # Malformed keys (too long, wrong day, not a string).
        _audit_event(
            session,
            f"{prefix}99999999999999999999",
            {
                "type": audit,
                "action": "report.prune",
                "report_ids": [f"{prefix}99999999999999999999", "TLM-19990101-5000", 7],
            },
        )
        session.flush()
        assert next_report_key(session, now) == f"{prefix}0003"


def test_concurrent_allocations_never_collide(web_client: TestClient) -> None:
    """Parallel submissions each get a distinct key without retrying."""
    import threading

    factory = get_context().session_factory
    now = utcnow()
    keys: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def allocate() -> None:
        try:
            barrier.wait()
            with session_scope(factory) as session:
                key = next_report_key(session, now)
                _add(session, key, timedelta(0))
                session.flush()
                keys.append(key)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=allocate) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    assert len(keys) == 6
    assert len(set(keys)) == 6


def test_prune_by_age_and_count(web_client: TestClient) -> None:
    settings = get_context().settings.model_copy(
        update={
            "reporting": ReportingSettings(retention_days=90, retention_max_reports=3)
        }
    )
    keys = [f"TLM-20261001-{n:04d}" for n in range(1, 7)]
    ages = [timedelta(days=100), *(timedelta(hours=h) for h in (5, 4, 3, 2, 1))]
    for key in keys:
        write_zip(settings, key, b"zip")
    with session_scope(get_context().session_factory) as session:
        for key, age in zip(keys, ages, strict=True):
            _add(session, key, age)
    with session_scope(get_context().session_factory) as session:
        pruned = prune_reports(session, settings, utcnow())
    # The 100-day-old one by age, then the two oldest beyond the count of 3.
    assert sorted(pruned) == keys[:3]
    with session_scope(get_context().session_factory) as session:
        left = sorted(session.execute(select(Report.report_key)).scalars())
    assert left == keys[3:]
    reports_dir = settings.paths.data_dir / "reports"
    assert sorted(path.stem for path in reports_dir.glob("*.zip")) == keys[3:]
    with session_scope(get_context().session_factory) as session:
        audit = [
            event
            for event in session.execute(select(Event)).scalars()
            if (event.event_metadata or {}).get("action") == "report.prune"
        ]
    [event] = audit
    assert event.scope == "system"
    assert event.message == (
        "retention pruned 3 problem report(s): " + ", ".join(keys[:3])
    )


def test_count_limit_keeps_undelivered_reports(web_client: TestClient) -> None:
    """Over the count cap, pending/failed reports stay until they age out."""
    settings = get_context().settings.model_copy(
        update={
            "reporting": ReportingSettings(retention_days=90, retention_max_reports=1)
        }
    )
    with session_scope(get_context().session_factory) as session:
        _add(session, "TLM-20261001-0001", timedelta(days=95), state="failed")
        _add(session, "TLM-20261001-0002", timedelta(hours=4), state="failed")
        _add(session, "TLM-20261001-0003", timedelta(hours=3), state="pending")
        _add(session, "TLM-20261001-0004", timedelta(hours=2), state="ok")
        _add(session, "TLM-20261001-0005", timedelta(hours=1), state="ok")
    with session_scope(get_context().session_factory) as session:
        pruned = prune_reports(session, settings, utcnow())
    # Aged out regardless of state; then only the delivered one past the cap.
    assert sorted(pruned) == ["TLM-20261001-0001", "TLM-20261001-0004"]


def test_scheduler_housekeeping_prunes(web_client: TestClient) -> None:
    """Retention runs from the render scheduler's periodic housekeeping."""

    from timelapse_manager.render.scheduler import RenderScheduler

    context = get_context()
    with session_scope(context.session_factory) as session:
        _add(session, "TLM-20261001-0001", timedelta(days=400))
    scheduler = RenderScheduler(
        context.settings,
        context.session_factory,
        context.render_queue,  # type: ignore[arg-type]
    )
    _run_private_loop(scheduler.run_housekeeping(force=True))
    with session_scope(context.session_factory) as session:
        assert session.execute(select(Report)).first() is None


@pytest.mark.parametrize("repo", ["../x", "owner/..", "owner", "a/b/c", "a b/c"])
def test_gitea_repo_must_be_owner_slash_name(repo: str) -> None:
    from pydantic import ValidationError as PydanticValidationError

    from timelapse_manager.config.settings import ReportingGiteaSettings

    with pytest.raises(PydanticValidationError):
        ReportingGiteaSettings(repo=repo)


def _run_private_loop(coro: Any) -> Any:
    """Run ``coro`` on a fresh loop without touching the thread's default loop.

    ``asyncio.run`` clears the default loop afterwards, which breaks later
    tests in the same worker that call ``asyncio.get_event_loop()``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_server_zip_write_never_follows_a_symlink(tmp_path: Path) -> None:
    settings = get_settings_stub(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep")
    (reports / "TLM-20261023-0001.zip").symlink_to(sentinel)
    with pytest.raises(ValueError):
        write_zip(settings, "TLM-20261023-0001", b"zip")  # type: ignore[arg-type]
    assert sentinel.read_text() == "keep"
    written = write_zip(settings, "TLM-20261023-0002", b"zip")  # type: ignore[arg-type]
    assert written.read_bytes() == b"zip"
    assert written.stat().st_mode & 0o777 == 0o640
