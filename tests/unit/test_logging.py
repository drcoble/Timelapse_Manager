"""Tests for logging configuration: level, format, redaction, and banner."""

from __future__ import annotations

import contextlib
import json
import logging

from timelapse_manager.config.settings import (
    LoggingSettings,
    Settings,
)
from timelapse_manager.logging import JsonFormatter, configure_logging

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(level: str = "DEBUG", fmt: str = "json") -> Settings:
    from pathlib import Path

    from timelapse_manager.config.settings import (
        DatabaseSettings,
        PathsSettings,
    )

    return Settings(
        logging=LoggingSettings(level=level, format=fmt),  # type: ignore[arg-type]
        database=DatabaseSettings(url="sqlite:///./test.db"),
        paths=PathsSettings(data_dir=Path("/tmp/tlm-test")),
    )


def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    **extra: object,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


# ---------------------------------------------------------------------------
# configure_logging: level and format
# ---------------------------------------------------------------------------


class TestConfigureLoggingLevel:
    def test_root_logger_level_is_set_to_debug(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        root = logging.getLogger()
        original_level = root.level
        original_handlers = root.handlers[:]
        try:
            configure_logging(_make_settings(level="DEBUG"))
            assert root.level == logging.DEBUG
        finally:
            root.setLevel(original_level)
            root.handlers[:] = original_handlers

    def test_root_logger_level_is_set_to_warning(self) -> None:
        root = logging.getLogger()
        original_level = root.level
        original_handlers = root.handlers[:]
        try:
            configure_logging(_make_settings(level="WARNING"))
            assert root.level == logging.WARNING
        finally:
            root.setLevel(original_level)
            root.handlers[:] = original_handlers

    def test_repeated_calls_do_not_duplicate_handlers(self) -> None:
        root = logging.getLogger()
        original_level = root.level
        original_handlers = root.handlers[:]
        try:
            configure_logging(_make_settings(level="INFO"))
            count_after_first = len(root.handlers)
            configure_logging(_make_settings(level="INFO"))
            count_after_second = len(root.handlers)
            assert count_after_second == count_after_first
        finally:
            root.setLevel(original_level)
            root.handlers[:] = original_handlers


class TestPackageLoggerSurvivesDisableExistingLoggers:
    """Application logs must emit even after a ``disable_existing_loggers`` dictConfig.

    Under ``uvicorn --factory`` (the container launch path) uvicorn imports the
    package -- creating ``timelapse_manager.*`` loggers -- and then runs a default
    ``logging.config.dictConfig`` whose ``disable_existing_loggers`` defaults to
    True, which sets ``disabled=True`` on those loggers. ``configure_logging``
    must clear that flag so our records are not silently dropped at the logger.
    This was the live-test symptom: a startup banner but not one listener WARNING.
    """

    def test_app_logger_emits_after_dictconfig_disables_existing(self) -> None:
        import logging.config

        # Ensure the application logger exists, mirroring how uvicorn's import of
        # the package would have created it before its dictConfig runs.
        app_logger = logging.getLogger("timelapse_manager.capture.supervisor")
        root = logging.getLogger()
        original_level = root.level
        original_handlers = root.handlers[:]
        try:
            # Mimic uvicorn's default dictConfig: configure only root and disable
            # every pre-existing logger (the documented default).
            logging.config.dictConfig(
                {
                    "version": 1,
                    "disable_existing_loggers": True,
                    "handlers": {
                        "default": {"class": "logging.NullHandler"},
                    },
                    "root": {"handlers": ["default"], "level": "INFO"},
                }
            )
            assert app_logger.disabled is True  # precondition: the bug's state

            configure_logging(_make_settings(level="DEBUG", fmt="text"))

            # After our fix the app logger is re-enabled and a WARNING is emitted.
            assert app_logger.disabled is False
            with self._capture(app_logger) as records:
                app_logger.warning("event listener failed; retrying")
            assert any("event listener failed" in r.getMessage() for r in records)
        finally:
            root.setLevel(original_level)
            root.handlers[:] = original_handlers

    @staticmethod
    @contextlib.contextmanager
    def _capture(target: logging.Logger):  # type: ignore[no-untyped-def]
        """Attach a capturing handler to the logger chain for the block."""
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        logging.getLogger().addHandler(handler)
        try:
            yield captured
        finally:
            logging.getLogger().removeHandler(handler)


# ---------------------------------------------------------------------------
# JsonFormatter: output shape and redaction
# ---------------------------------------------------------------------------


class TestJsonFormatter:
    def test_output_is_valid_json(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("test message")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_standard_fields_present(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("test message")
        parsed = json.loads(fmt.format(record))
        assert "timestamp" in parsed
        assert "level" in parsed
        assert "logger" in parsed
        assert "message" in parsed

    def test_message_field_matches(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("hello world")
        parsed = json.loads(fmt.format(record))
        assert parsed["message"] == "hello world"

    def test_level_field_matches(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("msg", level=logging.WARNING)
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "WARNING"

    def test_extra_field_is_included(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("msg", request_id="abc-123")
        parsed = json.loads(fmt.format(record))
        assert parsed.get("request_id") == "abc-123"

    def test_password_extra_field_is_redacted(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("msg", password="s3cr3t")
        parsed = json.loads(fmt.format(record))
        assert parsed["password"] == "***"
        assert "s3cr3t" not in json.dumps(parsed)

    def test_token_extra_field_is_redacted(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("msg", token="abcdef1234567890")
        parsed = json.loads(fmt.format(record))
        assert parsed["token"] == "***"

    def test_secret_extra_field_is_redacted(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("msg", api_secret="very-secret")
        parsed = json.loads(fmt.format(record))
        assert parsed["api_secret"] == "***"

    def test_api_key_extra_field_is_redacted(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("msg", api_key="my-key-value")
        parsed = json.loads(fmt.format(record))
        assert parsed["api_key"] == "***"

    def test_nested_dict_secret_is_redacted(self) -> None:
        fmt = JsonFormatter()
        record = _make_record(
            "msg", credentials={"username": "alice", "password": "pw"}
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["credentials"]["password"] == "***"
        assert parsed["credentials"]["username"] == "alice"

    def test_non_secret_extra_field_is_not_redacted(self) -> None:
        fmt = JsonFormatter()
        record = _make_record("msg", camera_id=42)
        parsed = json.loads(fmt.format(record))
        assert parsed["camera_id"] == 42


# ---------------------------------------------------------------------------
# Text formatter: redaction is not expected (secrets are in extra only)
# ---------------------------------------------------------------------------


class TestTextFormatter:
    def test_text_format_produces_non_json_output(self) -> None:
        root = logging.getLogger()
        original_level = root.level
        original_handlers = root.handlers[:]
        try:
            configure_logging(_make_settings(level="DEBUG", fmt="text"))
            # Verify a console handler was added with a non-JSON formatter
            assert any(
                not isinstance(h.formatter, JsonFormatter)
                for h in root.handlers
                if h.formatter is not None
            )
        finally:
            root.setLevel(original_level)
            root.handlers[:] = original_handlers


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------


class TestStartupBanner:
    def test_startup_banner_is_emitted(self) -> None:
        """configure_logging must emit a startup banner log record.

        configure_logging() installs a fresh root handler and emits the banner
        via logging.getLogger('timelapse_manager.logging').info(...). Rather
        than fighting root-logger level state left by earlier tests, we use
        unittest.mock.patch to intercept the logger.info call directly.
        """
        from unittest.mock import MagicMock, patch

        mock_logger = MagicMock()

        root = logging.getLogger()
        original_level = root.level
        original_handlers = root.handlers[:]
        try:
            with patch("timelapse_manager.logging.logging") as mock_logging_mod:
                # Make getLogger('timelapse_manager.logging') return our mock.
                mock_logging_mod.getLogger.return_value = mock_logger
                # But getLogger() with no args must return a real root logger
                # so configure_logging can set level and handlers on it.
                mock_logging_mod.getLogger.side_effect = lambda name=None: (
                    mock_logger
                    if name == "timelapse_manager.logging"
                    else logging.getLogger(name)
                )
                # Forward other attributes used by configure_logging.
                mock_logging_mod.INFO = logging.INFO
                mock_logging_mod.DEBUG = logging.DEBUG
                mock_logging_mod.WARNING = logging.WARNING
                mock_logging_mod.ERROR = logging.ERROR
                mock_logging_mod.Formatter = logging.Formatter
                mock_logging_mod.StreamHandler = logging.StreamHandler
                mock_logging_mod.FileHandler = logging.FileHandler
                configure_logging(_make_settings(level="INFO"))
        finally:
            root.setLevel(original_level)
            root.handlers[:] = original_handlers

        # The banner must have been emitted via logger.info().
        assert mock_logger.info.called, "Expected configure_logging to call logger.info"
        call_args = mock_logger.info.call_args
        assert "Timelapse Manager starting" in call_args[0][0]
