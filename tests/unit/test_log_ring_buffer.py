"""Tests for the in-memory log ring buffer and recent-request registry (#8)."""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

import pytest

import timelapse_manager.logging as tlm_logging
from timelapse_manager.config.settings import LoggingSettings, PathsSettings, Settings
from timelapse_manager.logging import (
    _RecentRequests,
    configure_logging,
    get_ring_buffer,
    request_id_ctx,
)


@pytest.fixture()
def restore_root() -> Generator[None, None, None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def _settings(tmp_path: Path, records: int) -> Settings:
    return Settings(
        logging=LoggingSettings(
            level="INFO", format="text", ring_buffer_records=records
        ),
        paths=PathsSettings(data_dir=tmp_path),
    )


def test_keeps_the_last_n_records_redacted(tmp_path: Path, restore_root: None) -> None:
    configure_logging(_settings(tmp_path, 5))
    log = logging.getLogger("timelapse_manager.tests.ring")
    for index in range(10):
        log.info("line %d", index)
    log.warning("camera rtsp://admin:hunter22@10.0.0.5/ failed")
    buffer = get_ring_buffer()
    assert buffer is not None
    texts = [line.text for line in buffer.lines()]
    assert len(texts) == 5
    assert texts[-1].endswith("camera rtsp://***@10.0.0.5/ failed")
    assert "hunter22" not in "\n".join(texts)


def test_survives_reconfiguration(tmp_path: Path, restore_root: None) -> None:
    configure_logging(_settings(tmp_path, 50))
    logging.getLogger("timelapse_manager.tests.ring").info("before")
    first = get_ring_buffer()
    configure_logging(_settings(tmp_path, 50))
    assert get_ring_buffer() is first
    assert any("before" in line.text for line in first.lines())  # type: ignore[union-attr]
    assert first in logging.getLogger().handlers


def test_captures_request_id(tmp_path: Path, restore_root: None) -> None:
    configure_logging(_settings(tmp_path, 10))
    token = request_id_ctx.set("req-abc")
    try:
        logging.getLogger("timelapse_manager.tests.ring").info("in a request")
    finally:
        request_id_ctx.reset(token)
    buffer = get_ring_buffer()
    assert buffer is not None
    [line] = [line for line in buffer.lines() if "in a request" in line.text]
    assert line.request_id == "req-abc"
    assert "[req-abc]" in line.text


def test_zero_disables(tmp_path: Path, restore_root: None) -> None:
    configure_logging(_settings(tmp_path, 0))
    assert get_ring_buffer() is None
    configure_logging(_settings(tmp_path, 10))  # leave it installed for others
    assert tlm_logging.get_ring_buffer() is not None


def test_recent_requests_are_bounded_per_user() -> None:
    registry = _RecentRequests()
    for index in range(30):
        registry.record(1, f"r{index}", "GET", "/")
    registry.record(1, "r29", "GET", "/")  # the same request twice counts once
    entries = registry.for_user(1)
    assert len(entries) == 20
    assert entries[-1].request_id == "r29"
    assert entries[-2].request_id == "r28"
    assert registry.for_user(2) == []
