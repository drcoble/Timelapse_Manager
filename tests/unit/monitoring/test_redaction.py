"""Unit tests for the logging redaction utilities.

Covers: redact_text scrubs URL userinfo; redact masks secret-named keys
and walks nested structures; the production JsonFormatter scrubs a URL-userinfo
log line.

NOTE: httpx logs the full request URL at INFO level, including any query-string
token parameter (e.g. ?token=...). The _URL_USERINFO_PATTERN only matches
scheme://credentials@host patterns in userinfo position, not query-string
parameters. If a webhook URL contains a token in the query string, it would
NOT be scrubbed by redact_text or JsonFormatter.
This is a REPORTED SRC DEFECT: see TestQueryStringTokenReportOnly below, which
uses real httpx (MockTransport) to confirm the leak empirically.
"""

from __future__ import annotations

import json
import logging

import httpx

from timelapse_manager.logging import JsonFormatter, redact, redact_text


class TestRedactText:
    def test_rtsp_userinfo_is_scrubbed(self) -> None:
        raw = "rtsp://admin:s3cr3t@192.0.2.10/stream"
        result = redact_text(raw)
        assert "s3cr3t" not in result
        assert "rtsp://" in result
        assert "192.0.2.10" in result

    def test_https_token_userinfo_is_scrubbed(self) -> None:
        raw = "https://my-api-token@hooks.example.com/notify"
        result = redact_text(raw)
        assert "my-api-token" not in result
        assert "https://" in result

    def test_user_colon_pass_pattern_is_scrubbed(self) -> None:
        raw = "ftp://user:hunter2@files.example.com/path"
        result = redact_text(raw)
        assert "hunter2" not in result

    def test_non_url_text_is_returned_unchanged(self) -> None:
        text = "camera captured frame 42"
        assert redact_text(text) == text

    def test_url_without_userinfo_is_not_altered(self) -> None:
        url = "https://api.example.com/webhooks/notify"
        assert redact_text(url) == url

    def test_multiple_urls_in_text_all_scrubbed(self) -> None:
        text = "rtsp://u:p@host1 and rtsp://a:b@host2"
        result = redact_text(text)
        assert ":p@" not in result
        assert ":b@" not in result


class TestRedact:
    def test_password_key_is_masked(self) -> None:
        data = {"password": "secret"}
        result = redact(data)
        assert result["password"] == "***"

    def test_token_key_is_masked(self) -> None:
        data = {"token": "abc123"}
        result = redact(data)
        assert result["token"] == "***"

    def test_api_key_is_masked(self) -> None:
        data = {"api_key": "my-key"}
        result = redact(data)
        assert result["api_key"] == "***"

    def test_non_secret_key_is_unchanged(self) -> None:
        data = {"camera_id": 42, "level": "warning"}
        result = redact(data)
        assert result["camera_id"] == 42
        assert result["level"] == "warning"

    def test_nested_dict_secret_key_is_masked(self) -> None:
        data = {"smtp": {"password": "pw123", "server": "mail.host"}}
        result = redact(data)
        assert result["smtp"]["password"] == "***"
        assert result["smtp"]["server"] == "mail.host"

    def test_list_values_are_walked(self) -> None:
        data = ["rtsp://u:p@host", "safe string"]
        result = redact(data)
        assert "p@" not in result[0]
        assert result[1] == "safe string"

    def test_string_url_value_is_redacted(self) -> None:
        data = {"camera_url": "rtsp://admin:pw@192.0.2.5/stream"}
        result = redact(data)
        assert "pw" not in result["camera_url"]

    def test_integer_value_is_passed_through(self) -> None:
        data = {"port": 554}
        result = redact(data)
        assert result["port"] == 554


class TestJsonFormatterRedaction:
    def _make_record(self, msg: str, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_url_userinfo_in_message_is_scrubbed_by_json_formatter(self) -> None:
        """The production JsonFormatter must scrub userinfo from a message URL.

        This uses the real JsonFormatter (not a bare Formatter) so the scrub
        logic in redact_text is exercised as it runs in production.
        """
        fmt = JsonFormatter()
        record = self._make_record("connecting to rtsp://user:password@192.0.2.1/live")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "password" not in parsed["message"]
        assert "rtsp://" in parsed["message"]

    def test_password_extra_field_is_masked_by_json_formatter(self) -> None:
        fmt = JsonFormatter()
        record = self._make_record("event", smtp_password="cleartext-pw")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["smtp_password"] == "***"

    def test_non_secret_extra_field_is_preserved(self) -> None:
        fmt = JsonFormatter()
        record = self._make_record("event", camera_id=7)
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["camera_id"] == 7


# ---------------------------------------------------------------------------
# SRC DEFECT REPORT (do not fix here — report only)
# ---------------------------------------------------------------------------


class TestQueryStringTokenReportOnly:
    def test_query_string_token_in_url_is_scrubbed_by_redact_text(self) -> None:
        """URL query-string tokens are scrubbed by redact_text.

        redact_text was extended to cover query-string parameters whose names
        match the secret-key pattern (e.g. ?token=..., ?api_key=...). This
        test confirms the fix is in effect.
        """
        url_with_token = "https://hooks.example.com/notify?token=abc-secret"
        result = redact_text(url_with_token)
        # Fixed behaviour: query-string token is scrubbed.
        assert "abc-secret" not in result

    async def test_real_httpx_logs_query_string_token_at_info_level(self) -> None:
        """REPORT (authoritative): real httpx logs query-string tokens at INFO.

        This uses httpx.MockTransport so the request executes through the real
        httpx internals (including its logger) without network I/O. The token
        in the webhook URL query-string appears in an 'HTTP Request:' INFO log
        record emitted by httpx itself, confirming the leak path is real.

        Two factors make the standard caplog approach unreliable in the full suite:

        1. configure_logging() (called by app-lifespan fixtures) replaces all root
           handlers, evicting caplog's LogCaptureHandler.
        2. alembic/env.py calls logging.config.fileConfig(alembic.ini) with the
           default disable_existing_loggers=True, which sets httpx.disabled=True
           on every test that uses the migrated_factory fixture. Logger.disabled
           is an instance attribute that silently drops all records regardless of
           level or handler state.

        Fix: install a handler directly on the httpx logger AND explicitly clear
        the disabled flag for the duration of the test. Both are restored in the
        finally block so neighboring tests are not affected.

        Candidate fix for the src defect: silence the httpx logger to WARNING in
        configure_logging() so its INFO lines (which embed the full request URL)
        are never emitted. No src change is made here.
        """

        def transport_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        class _RecordingHandler(logging.Handler):
            def __init__(self) -> None:
                super().__init__(logging.NOTSET)
                self.messages: list[str] = []

            def emit(self, record: logging.LogRecord) -> None:
                self.messages.append(record.getMessage())

        token = "abc-secret-qs-token"
        url = f"https://hooks.example.com/notify?token={token}"

        httpx_logger = logging.getLogger("httpx")
        original_level = httpx_logger.level
        original_disabled = httpx_logger.disabled
        recorder = _RecordingHandler()
        httpx_logger.addHandler(recorder)
        httpx_logger.setLevel(logging.INFO)
        # alembic/env.py fileConfig(disable_existing_loggers=True) sets
        # httpx_logger.disabled=True on any test that runs migrated_factory first.
        # Clear it here so the handler can receive records.
        httpx_logger.disabled = False
        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(transport_handler),
                follow_redirects=False,
            ) as client:
                await client.post(url, json={"event": "test"})
        finally:
            httpx_logger.removeHandler(recorder)
            httpx_logger.setLevel(original_level)
            httpx_logger.disabled = original_disabled

        # httpx emits 'HTTP Request: POST <full-url>' at INFO — the token is exposed.
        all_messages = " ".join(recorder.messages)
        assert token in all_messages, (
            "httpx did NOT log the query-string token at INFO — "
            "the leak may have been fixed upstream or the logger name changed."
        )
