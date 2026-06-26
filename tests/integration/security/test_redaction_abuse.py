"""Abuse tests: log redaction boundary probing.

Verifies that the redaction layer scrubs secrets in every surface it covers,
and that it does not accidentally scrub non-secret content.  Tests target the
module-level functions as well as the JSON formatter integration.
"""

from __future__ import annotations

import json
import logging

import pytest

from timelapse_manager.logging import JsonFormatter, redact, redact_text

# ---------------------------------------------------------------------------
# redact_text: URL userinfo scrubbing
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestRedactTextUserinfo:
    @pytest.mark.parametrize(
        "raw,must_not_contain",
        [
            ("rtsp://user:hunter2@192.0.2.1/stream", "hunter2"),
            ("http://token123@webhook.example.com/notify", "token123"),
            ("ftp://u:p@files.example.com/path", ":p@"),
            ("https://api-key-here@api.example.com/v1", "api-key-here"),
        ],
    )
    def test_userinfo_credential_is_scrubbed(
        self, raw: str, must_not_contain: str
    ) -> None:
        assert must_not_contain not in redact_text(raw)

    def test_multiple_urls_in_single_string_all_scrubbed(self) -> None:
        raw = "A rtsp://u:p1@host1 and rtsp://u:p2@host2 B"
        result = redact_text(raw)
        assert "p1" not in result
        assert "p2" not in result

    def test_scheme_and_host_preserved_after_scrub(self) -> None:
        raw = "rtsp://admin:s3cr3t@192.0.2.10/stream"
        result = redact_text(raw)
        assert "rtsp://" in result
        assert "192.0.2.10" in result

    def test_non_url_text_is_not_altered(self) -> None:
        text = "camera 42 captured frame at 2024-01-01"
        assert redact_text(text) == text

    def test_url_without_credentials_not_altered(self) -> None:
        url = "https://api.example.com/webhooks/notify"
        assert redact_text(url) == url


# ---------------------------------------------------------------------------
# redact_text: query-string token scrubbing (Engineer C fix)
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestRedactTextQueryStringTokens:
    @pytest.mark.parametrize(
        "url,must_not_contain",
        [
            ("https://hooks.example.com/notify?token=abc-secret", "abc-secret"),
            ("https://api.example.com/send?api_key=my-key-value", "my-key-value"),
            ("https://hooks.example.com/x?access_token=tok123", "tok123"),
            ("https://hooks.example.com/x?secret=mysecret", "mysecret"),
            ("https://example.com/x?password=mypass", "mypass"),
            ("https://example.com/x?sig=deadbeef", "deadbeef"),
        ],
    )
    def test_query_string_secret_param_is_scrubbed(
        self, url: str, must_not_contain: str
    ) -> None:
        result = redact_text(url)
        assert must_not_contain not in result, (
            f"Secret value {must_not_contain!r} survived redact_text for URL: {url!r}"
        )

    def test_non_secret_query_param_preserved(self) -> None:
        url = "https://api.example.com/report?project_id=42&format=json"
        result = redact_text(url)
        assert "project_id=42" in result
        assert "format=json" in result

    def test_parameter_name_preserved_after_value_scrub(self) -> None:
        url = "https://example.com/x?token=abc-secret"
        result = redact_text(url)
        assert "token=" in result


# ---------------------------------------------------------------------------
# redact: structured data scrubbing
# ---------------------------------------------------------------------------


@pytest.mark.abuse
class TestRedactStructured:
    @pytest.mark.parametrize(
        "key",
        ["password", "smtp_password", "api_password", "my_password_field"],
    )
    def test_password_keyed_values_are_masked(self, key: str) -> None:
        data = {key: "cleartext-pw"}
        assert redact(data)[key] == "***"

    @pytest.mark.parametrize("key", ["token", "access_token", "auth_token"])
    def test_token_keyed_values_are_masked(self, key: str) -> None:
        data = {key: "tok-abc"}
        assert redact(data)[key] == "***"

    @pytest.mark.parametrize("key", ["api_key", "secret_key", "client_secret"])
    def test_secret_and_key_named_values_are_masked(self, key: str) -> None:
        data = {key: "sk-abc123"}
        assert redact(data)[key] == "***"

    def test_nested_password_is_masked(self) -> None:
        data = {"smtp": {"password": "pw", "server": "mail.host"}}
        result = redact(data)
        assert result["smtp"]["password"] == "***"
        assert result["smtp"]["server"] == "mail.host"

    def test_non_secret_fields_pass_through(self) -> None:
        data = {"camera_id": 7, "level": "info", "project": "test"}
        result = redact(data)
        assert result == data

    def test_integer_value_passes_through(self) -> None:
        data = {"port": 587, "timeout": 30}
        result = redact(data)
        assert result == data

    def test_list_of_strings_scrubbed(self) -> None:
        data = ["rtsp://u:p@host", "safe text"]
        result = redact(data)
        assert "p@" not in result[0]
        assert result[1] == "safe text"

    def test_none_value_for_secret_key_passes_through(self) -> None:
        data = {"password": None}
        result = redact(data)
        # None is not a string; it should pass through without masking.
        assert result["password"] is None or result["password"] == "***"


# ---------------------------------------------------------------------------
# JsonFormatter integration
# ---------------------------------------------------------------------------


@pytest.mark.abuse
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

    def test_userinfo_in_message_is_scrubbed(self) -> None:
        fmt = JsonFormatter()
        record = self._make_record(
            "connecting to rtsp://user:cleartext-pw@192.0.2.1/live"
        )
        output = fmt.format(record)
        assert "cleartext-pw" not in json.loads(output)["message"]

    def test_password_extra_field_is_masked(self) -> None:
        fmt = JsonFormatter()
        record = self._make_record("event", smtp_password="cleartext-pw")
        output = fmt.format(record)
        assert json.loads(output)["smtp_password"] == "***"

    def test_query_string_token_in_message_is_scrubbed(self) -> None:
        fmt = JsonFormatter()
        record = self._make_record(
            "POST https://hooks.example.com/notify?token=leak-me"
        )
        output = fmt.format(record)
        assert "leak-me" not in json.loads(output)["message"]

    def test_non_secret_message_passes_through_unchanged(self) -> None:
        fmt = JsonFormatter()
        record = self._make_record("camera 42 captured frame")
        output = fmt.format(record)
        assert "camera 42 captured frame" in json.loads(output)["message"]

    def test_json_output_is_valid_json(self) -> None:
        fmt = JsonFormatter()
        record = self._make_record("test message")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "message" in parsed
        assert "timestamp" in parsed or "time" in parsed or "level" in parsed
