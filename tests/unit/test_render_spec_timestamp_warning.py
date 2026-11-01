"""A bad stored ``timestamp_format`` warns only when the overlay uses it (#140).

Rows saved before the format grammar existed may hold anything. The render falls
back to the default format; the WARNING that says so is logged only when the
timestamp overlay is enabled, and once per project per process.

Refs #140.
"""

from __future__ import annotations

import logging

import pytest

from timelapse_manager.db.models import RenderJob
from timelapse_manager.encode.overlay import DEFAULT_TIMESTAMP_FORMAT
from timelapse_manager.render import spec

_BAD = "%{eif:1:d}"


@pytest.fixture(autouse=True)
def _fresh_warning_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The once-per-project set is module state: start every test empty."""
    monkeypatch.setattr(spec, "_WARNED_BAD_TIMESTAMP_FORMAT", set())


def _job(project_id: int, *, enabled: bool, job_id: int = 1) -> RenderJob:
    return RenderJob(
        id=job_id,
        project_id=project_id,
        overlay_config={"timestamp_enabled": enabled, "timestamp_format": _BAD},
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


class TestBadTimestampFormatWarning:
    def test_disabled_overlay_falls_back_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=spec.logger.name):
            config = spec._overlay_config(_job(7, enabled=False))  # noqa: SLF001

        assert config.timestamp_format == DEFAULT_TIMESTAMP_FORMAT
        assert _warnings(caplog) == []

    def test_enabled_overlay_warns_once_per_project(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=spec.logger.name):
            first = spec._overlay_config(_job(7, enabled=True, job_id=1))  # noqa: SLF001
            second = spec._overlay_config(_job(7, enabled=True, job_id=2))  # noqa: SLF001

        assert first.timestamp_format == second.timestamp_format
        assert second.timestamp_format == DEFAULT_TIMESTAMP_FORMAT
        [record] = _warnings(caplog)
        assert "project 7" in record.getMessage()

    def test_a_silent_render_does_not_use_up_the_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=spec.logger.name):
            spec._overlay_config(_job(7, enabled=False))  # noqa: SLF001
            spec._overlay_config(_job(7, enabled=True))  # noqa: SLF001

        assert len(_warnings(caplog)) == 1

    def test_each_project_warns_for_itself(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=spec.logger.name):
            spec._overlay_config(_job(7, enabled=True))  # noqa: SLF001
            spec._overlay_config(_job(8, enabled=True))  # noqa: SLF001

        assert len(_warnings(caplog)) == 2

    def test_a_valid_format_never_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        job = RenderJob(
            id=1,
            project_id=7,
            overlay_config={"timestamp_enabled": True, "timestamp_format": "%H:%M"},
        )
        with caplog.at_level(logging.WARNING, logger=spec.logger.name):
            config = spec._overlay_config(job)  # noqa: SLF001

        assert config.timestamp_format == "%H:%M"
        assert _warnings(caplog) == []
