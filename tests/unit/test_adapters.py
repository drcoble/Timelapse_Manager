"""Unit tests for camera adapter construction helpers.

Covers:
- HTTP/JPEG snapshot URL usage and credential extraction
- VAPIX URL construction (no explicit URI, resolution, compression, explicit URI)
- VAPIX param parsing and geolocation extraction
- RTSP ffmpeg command-line building (flags, timeout unit, never shell string)
- RTSP stderr classifier (each failure category including the critical ordering
  case: connection-refused + echoed ?timeout= URL token must not become TIMEOUT)
- RTSP credential embedding (percent-encoding of special chars, skip if already present)
- Credentials never appear in log output

No network, subprocess, or filesystem access.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from timelapse_manager.cameras.base import (
    AuthCaptureError,
    OtherCaptureError,
    TimeoutCaptureError,
    UnreachableCaptureError,
    UnsupportedCodecCaptureError,
    ValidationFailure,
)
from timelapse_manager.cameras.http_jpeg import classify_http_status, credentials_from
from timelapse_manager.cameras.rtsp import (
    RtspAdapter,
    build_ffmpeg_command,
    classify_stderr,
)
from timelapse_manager.cameras.vapix import (
    PARAM_PATH,
    SCENE_METADATA_SCHEMA_VERSION,
    SNAPSHOT_PATH,
    VapixAdapter,
    build_snapshot_url,
    geolocation_from_params,
    geolocation_from_position_xml,
    parse_param_response,
    scene_fields_from_params,
)

# ---------------------------------------------------------------------------
# HTTP/JPEG helpers
# ---------------------------------------------------------------------------


class TestCredentialsFrom:
    def test_returns_tuple_when_username_and_password_present(self) -> None:
        cam = MagicMock()
        cam.credentials = {"username": "admin", "password": "secret"}
        assert credentials_from(cam) == ("admin", "secret")

    def test_returns_none_when_credentials_is_none(self) -> None:
        cam = MagicMock()
        cam.credentials = None
        assert credentials_from(cam) is None

    def test_returns_none_when_credentials_not_a_dict(self) -> None:
        cam = MagicMock()
        cam.credentials = "admin:secret"
        assert credentials_from(cam) is None

    def test_returns_none_when_username_missing(self) -> None:
        cam = MagicMock()
        cam.credentials = {"password": "secret"}
        assert credentials_from(cam) is None

    def test_empty_password_becomes_empty_string(self) -> None:
        cam = MagicMock()
        cam.credentials = {"username": "admin"}
        result = credentials_from(cam)
        assert result is not None
        assert result == ("admin", "")

    def test_none_password_becomes_empty_string(self) -> None:
        cam = MagicMock()
        cam.credentials = {"username": "admin", "password": None}
        result = credentials_from(cam)
        assert result is not None
        assert result[1] == ""


class TestClassifyHttpStatus:
    def test_200_is_success(self) -> None:
        assert classify_http_status(200) is None

    def test_204_is_success(self) -> None:
        assert classify_http_status(204) is None

    def test_401_is_auth_failure(self) -> None:
        assert classify_http_status(401) is ValidationFailure.AUTH

    def test_403_is_auth_failure(self) -> None:
        assert classify_http_status(403) is ValidationFailure.AUTH

    def test_404_is_other(self) -> None:
        assert classify_http_status(404) is ValidationFailure.OTHER

    def test_500_is_other(self) -> None:
        assert classify_http_status(500) is ValidationFailure.OTHER


# ---------------------------------------------------------------------------
# VAPIX URL construction
# ---------------------------------------------------------------------------


class TestBuildSnapshotUrl:
    def test_explicit_uri_returned_verbatim(self) -> None:
        explicit = "http://10.0.0.1/custom/snapshot"
        result = build_snapshot_url("10.0.0.2", explicit_snapshot_uri=explicit)
        assert result == explicit

    def test_defaults_to_axis_cgi_path(self) -> None:
        url = build_snapshot_url("10.0.0.1")
        assert SNAPSHOT_PATH in url
        assert url.startswith("http://10.0.0.1")

    def test_address_with_http_scheme_not_doubled(self) -> None:
        url = build_snapshot_url("http://10.0.0.1")
        assert url.startswith("http://10.0.0.1")
        assert "http://http://" not in url

    def test_resolution_added_as_query_param(self) -> None:
        url = build_snapshot_url("10.0.0.1", resolution="1920x1080")
        assert "resolution=1920x1080" in url

    def test_compression_added_as_query_param(self) -> None:
        url = build_snapshot_url("10.0.0.1", compression=50)
        assert "compression=50" in url

    def test_no_query_string_when_no_params(self) -> None:
        url = build_snapshot_url("10.0.0.1")
        assert "?" not in url

    def test_trailing_slash_stripped_from_address(self) -> None:
        url = build_snapshot_url("10.0.0.1/")
        assert "//" not in url.replace("http://", "")


class TestParseParamResponse:
    def test_parses_single_key_value(self) -> None:
        assert parse_param_response("root.key=value") == {"root.key": "value"}

    def test_parses_multiple_lines(self) -> None:
        text = "root.Geolocation.Latitude=51.5\nroot.Geolocation.Longitude=-0.1\n"
        result = parse_param_response(text)
        assert result["root.Geolocation.Latitude"] == "51.5"
        assert result["root.Geolocation.Longitude"] == "-0.1"

    def test_lines_without_equals_ignored(self) -> None:
        result = parse_param_response("no-equals-here\nkey=val")
        assert "no-equals-here" not in result
        assert result["key"] == "val"

    def test_empty_string_returns_empty_dict(self) -> None:
        assert parse_param_response("") == {}

    def test_value_with_equals_in_it_preserved(self) -> None:
        result = parse_param_response("key=a=b")
        # partition on first '=' only
        assert result["key"] == "a=b"


class TestGeolocationFromParams:
    def test_returns_geolocation_when_both_present(self) -> None:
        params = {
            "root.Geolocation.Latitude": "51.5074",
            "root.Geolocation.Longitude": "-0.1278",
        }
        geo = geolocation_from_params(params)
        assert geo is not None
        assert geo.latitude == pytest.approx(51.5074)
        assert geo.longitude == pytest.approx(-0.1278)
        assert geo.source == "camera"

    def test_returns_none_when_latitude_missing(self) -> None:
        params = {"root.Geolocation.Longitude": "-0.1278"}
        assert geolocation_from_params(params) is None

    def test_returns_none_when_longitude_missing(self) -> None:
        params = {"root.Geolocation.Latitude": "51.5"}
        assert geolocation_from_params(params) is None

    def test_returns_none_when_params_empty(self) -> None:
        assert geolocation_from_params({}) is None

    def test_returns_none_when_value_not_parseable_as_float(self) -> None:
        params = {
            "root.Geolocation.Latitude": "NOT_A_NUMBER",
            "root.Geolocation.Longitude": "-0.1278",
        }
        assert geolocation_from_params(params) is None

    def test_key_matching_is_case_insensitive_on_suffix(self) -> None:
        # Both 'Latitude' and 'latitude' suffixes should match
        params = {
            "root.Geolocation.latitude": "51.5",
            "root.Geolocation.longitude": "-0.1",
        }
        geo = geolocation_from_params(params)
        assert geo is not None


_POSITION_XML_OK = (
    '<PositionResponse SchemaVersion="1.0"><Success><GetSuccess>'
    "<Location><Lat>34.122748000</Lat><Lng>-083.936878000</Lng>"
    "<Heading>45.000000</Heading></Location>"
    "<ValidPosition>true</ValidPosition>"
    "</GetSuccess></Success></PositionResponse>"
)


class TestGeolocationFromPositionXml:
    """The modern Axis Geolocation API (geolocation/get.cgi) XML parser."""

    def test_parses_valid_position(self) -> None:
        geo = geolocation_from_position_xml(_POSITION_XML_OK)
        assert geo is not None
        # Leading-zero longitude must still parse.
        assert geo.latitude == pytest.approx(34.122748)
        assert geo.longitude == pytest.approx(-83.936878)
        assert geo.source == "camera"

    def test_parses_without_valid_position_flag(self) -> None:
        xml = (
            "<PositionResponse><Success><GetSuccess><Location>"
            "<Lat>10.0</Lat><Lng>20.0</Lng></Location>"
            "</GetSuccess></Success></PositionResponse>"
        )
        geo = geolocation_from_position_xml(xml)
        assert geo is not None
        assert geo.latitude == pytest.approx(10.0)

    def test_rejects_explicitly_invalid_position(self) -> None:
        xml = _POSITION_XML_OK.replace(
            "<ValidPosition>true</ValidPosition>",
            "<ValidPosition>false</ValidPosition>",
        )
        assert geolocation_from_position_xml(xml) is None

    def test_returns_none_when_no_location(self) -> None:
        xml = (
            "<PositionResponse><Error><ErrorCode>4</ErrorCode></Error>"
            "</PositionResponse>"
        )
        assert geolocation_from_position_xml(xml) is None

    def test_returns_none_on_unparseable_body(self) -> None:
        assert geolocation_from_position_xml("not xml at all") is None
        assert geolocation_from_position_xml("") is None

    def test_returns_none_when_coord_not_float(self) -> None:
        xml = (
            "<PositionResponse><Location><Lat>NaNeesh</Lat>"
            "<Lng>20.0</Lng></Location></PositionResponse>"
        )
        assert geolocation_from_position_xml(xml) is None


class TestVapixGetGeolocation:
    """get_geolocation prefers the API and falls back to the legacy params."""

    async def test_uses_api_response_when_available(self) -> None:
        client = MagicMock()
        adapter = VapixAdapter(client, address="10.0.0.9")
        with patch(
            "timelapse_manager.cameras.vapix.http_get_image",
            new=AsyncMock(return_value=_POSITION_XML_OK.encode()),
        ) as mock_get:
            geo = await adapter.get_geolocation()
        assert geo is not None
        assert geo.latitude == pytest.approx(34.122748)
        # The API endpoint was the URL queried.
        assert "geolocation/get.cgi" in mock_get.await_args.args[1]

    async def test_falls_back_to_param_group_when_api_empty(self) -> None:
        client = MagicMock()
        adapter = VapixAdapter(client, address="10.0.0.9")
        # API returns an error envelope (no location); legacy param group has it.
        legacy = b"root.Geolocation.Latitude=1.5\nroot.Geolocation.Longitude=2.5\n"
        bodies = [b"<PositionResponse><Error/></PositionResponse>", legacy]
        with patch(
            "timelapse_manager.cameras.vapix.http_get_image",
            new=AsyncMock(side_effect=bodies),
        ):
            geo = await adapter.get_geolocation()
        assert geo is not None
        assert geo.latitude == pytest.approx(1.5)
        assert geo.longitude == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# VAPIX scene metadata
# ---------------------------------------------------------------------------

# A 1x1 JPEG whose dimensions read back as 1x1 (SOF marker present), reused for
# the snapshot leg of a mocked capture.
_FAKE_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e41=>"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\x03\xff\xd9"
)

# A realistic Axis ``Image`` parameter-group response (the scene/image settings).
# The Appearance resolution/compression/rotation/overlays fields are present on
# every Axis generation; the finer brightness/exposure tuning fields appear only
# on some firmware.
_IMAGE_PARAM_RESPONSE = (
    "root.Image.I0.Appearance.Resolution=1920x1080\n"
    "root.Image.I0.Appearance.Compression=30\n"
    "root.Image.I0.Appearance.Rotation=180\n"
    "root.Image.I0.Appearance.Overlays=all\n"
    "root.Image.I0.Appearance.Brightness=55\n"
    "root.Image.I0.Appearance.Contrast=48\n"
    "root.Image.I0.Appearance.Saturation=60\n"
    "root.Image.I0.Appearance.Sharpness=42\n"
    "root.Image.I0.Appearance.ColorEnabled=yes\n"
    "root.Image.I0.Exposure.ExposureValue=70\n"
    "root.Image.I0.Exposure.ExposurePriority=balanced\n"
)


class TestSceneFieldsFromParams:
    def test_extracts_known_scene_fields(self) -> None:
        params = parse_param_response(_IMAGE_PARAM_RESPONSE)
        fields = scene_fields_from_params(params)
        # Appearance fields present across firmware generations.
        assert fields["appearance_resolution"] == "1920x1080"
        assert fields["compression"] == "30"
        assert fields["rotation"] == "180"
        assert fields["overlays"] == "all"
        # Finer tuning fields (present on this sample).
        assert fields["brightness"] == "55"
        assert fields["contrast"] == "48"
        assert fields["saturation"] == "60"
        assert fields["sharpness"] == "42"
        assert fields["color_enabled"] == "yes"
        assert fields["exposure_value"] == "70"
        assert fields["exposure_priority"] == "balanced"

    def test_absent_fields_are_omitted_not_invented(self) -> None:
        params = parse_param_response("root.Image.I0.Appearance.Brightness=55\n")
        fields = scene_fields_from_params(params)
        assert fields == {"brightness": "55"}

    def test_empty_group_yields_empty_dict(self) -> None:
        assert scene_fields_from_params({}) == {}


def _vapix_http_mock(
    snapshot_bytes: bytes,
    param_response: str | None,
) -> AsyncMock:
    """Return an AsyncMock standing in for ``http_get_image``.

    Routes the snapshot CGI path to ``snapshot_bytes`` and the parameter CGI path
    to ``param_response`` (UTF-8 encoded). When ``param_response`` is ``None`` the
    parameter leg raises a :class:`TimeoutCaptureError` -- the canonical real
    failure for a slow/unreachable parameter CGI. This is the path that actually
    occurs on a failed scene read: ``_read_params`` absorbs the ``CaptureError``
    into an empty result, which the collector treats as "no metadata".
    """

    async def _side_effect(client, url, credentials, timeout):  # noqa: ANN001
        if PARAM_PATH in url:
            if param_response is None:
                raise TimeoutCaptureError("simulated param read timeout")
            return param_response.encode("utf-8")
        return snapshot_bytes

    return AsyncMock(side_effect=_side_effect)


class TestVapixCaptureSceneMetadata:
    async def test_capture_attaches_versioned_scene_envelope(self) -> None:
        adapter = VapixAdapter(client=MagicMock(), address="192.0.2.10")
        mock = _vapix_http_mock(_FAKE_JPEG, _IMAGE_PARAM_RESPONSE)
        with patch("timelapse_manager.cameras.vapix.http_get_image", mock):
            frame = await adapter.capture()

        meta = frame.scene_metadata
        assert meta is not None
        # Versioned envelope, non-vacuous: specific keys and values asserted.
        assert meta["schema_version"] == SCENE_METADATA_SCHEMA_VERSION
        assert meta["source"] == "vapix"
        assert meta["captured_resolution"] == "1x1"
        assert meta["brightness"] == "55"
        assert meta["exposure_value"] == "70"

    async def test_capture_succeeds_with_none_metadata_on_param_failure(
        self,
    ) -> None:
        adapter = VapixAdapter(client=MagicMock(), address="192.0.2.10")
        # Snapshot succeeds; the parameter (scene) read fails.
        mock = _vapix_http_mock(_FAKE_JPEG, param_response=None)
        with patch("timelapse_manager.cameras.vapix.http_get_image", mock):
            frame = await adapter.capture()

        # The frame is still produced; metadata is simply absent.
        assert frame.image_bytes == _FAKE_JPEG
        assert frame.scene_metadata is None

    async def test_empty_scene_group_yields_no_metadata(self) -> None:
        adapter = VapixAdapter(client=MagicMock(), address="192.0.2.10")
        # An empty parameter read is indistinguishable from a failed one (the
        # param helper absorbs every reachability/parse failure into an empty
        # result), so it is treated as "no metadata" -- the frame still captures.
        mock = _vapix_http_mock(_FAKE_JPEG, param_response="")
        with patch("timelapse_manager.cameras.vapix.http_get_image", mock):
            frame = await adapter.capture()

        assert frame.image_bytes == _FAKE_JPEG
        assert frame.scene_metadata is None


class TestNonVapixCaptureHasNoSceneMetadata:
    async def test_http_jpeg_capture_leaves_scene_metadata_none(self) -> None:
        """A non-VAPIX adapter captures exactly as before: no metadata, no error,
        no extra parameter read."""
        from timelapse_manager.cameras.http_jpeg import HttpJpegAdapter

        adapter = HttpJpegAdapter(
            client=MagicMock(), snapshot_url="http://192.0.2.10/snap"
        )
        with patch(
            "timelapse_manager.cameras.http_jpeg.http_get_image",
            AsyncMock(return_value=_FAKE_JPEG),
        ):
            frame = await adapter.capture()

        assert frame.image_bytes == _FAKE_JPEG
        assert frame.scene_metadata is None


# ---------------------------------------------------------------------------
# RTSP ffmpeg command-line builder
# ---------------------------------------------------------------------------


class TestBuildFfmpegCommand:
    def test_returns_a_list_not_a_string(self) -> None:
        cmd = build_ffmpeg_command("rtsp://cam/stream")
        assert isinstance(cmd, list)
        assert all(isinstance(arg, str) for arg in cmd)

    def test_first_element_is_ffmpeg(self) -> None:
        cmd = build_ffmpeg_command("rtsp://cam/stream")
        assert cmd[0] == "ffmpeg"

    def test_uses_timeout_not_stimeout(self) -> None:
        cmd = build_ffmpeg_command("rtsp://cam/stream")
        assert "-timeout" in cmd
        assert "-stimeout" not in cmd

    def test_timeout_is_in_microseconds(self) -> None:
        cmd = build_ffmpeg_command("rtsp://cam/stream", timeout_seconds=15.0)
        idx = cmd.index("-timeout")
        value = int(cmd[idx + 1])
        assert value == 15_000_000

    def test_url_appears_as_separate_element_after_dash_i(self) -> None:
        url = "rtsp://cam/stream"
        cmd = build_ffmpeg_command(url)
        idx = cmd.index("-i")
        assert cmd[idx + 1] == url

    def test_frames_v_1_limits_to_single_frame(self) -> None:
        cmd = build_ffmpeg_command("rtsp://cam/stream")
        idx = cmd.index("-frames:v")
        assert cmd[idx + 1] == "1"

    def test_output_is_stdout(self) -> None:
        cmd = build_ffmpeg_command("rtsp://cam/stream")
        assert cmd[-1] == "-"

    def test_transport_tcp_by_default(self) -> None:
        cmd = build_ffmpeg_command("rtsp://cam/stream")
        assert "tcp" in cmd

    def test_transport_udp_can_be_set(self) -> None:
        cmd = build_ffmpeg_command("rtsp://cam/stream", transport="udp")
        assert "udp" in cmd


# ---------------------------------------------------------------------------
# RTSP stderr classifier
# ---------------------------------------------------------------------------


class TestClassifyStderr:
    def test_auth_failure_detected(self) -> None:
        assert classify_stderr("401 Unauthorized") is AuthCaptureError

    def test_authentication_keyword_detected(self) -> None:
        assert classify_stderr("Authentication failed") is AuthCaptureError

    def test_connection_refused_detected(self) -> None:
        assert classify_stderr("Connection refused") is UnreachableCaptureError

    def test_no_route_to_host_detected(self) -> None:
        assert classify_stderr("No route to host") is UnreachableCaptureError

    def test_name_or_service_not_known_detected(self) -> None:
        assert classify_stderr("Name or service not known") is UnreachableCaptureError

    def test_could_not_resolve_detected(self) -> None:
        result = classify_stderr("Could not resolve host: cam.local")
        assert result is UnreachableCaptureError

    def test_timed_out_phrase_detected(self) -> None:
        assert classify_stderr("Connection timed out") is TimeoutCaptureError

    def test_operation_timed_out_phrase_detected(self) -> None:
        assert classify_stderr("Operation timed out") is TimeoutCaptureError

    def test_decoder_not_found_detected(self) -> None:
        result = classify_stderr("Decoder not found for stream")
        assert result is UnsupportedCodecCaptureError

    def test_unsupported_keyword_detected(self) -> None:
        assert classify_stderr("unsupported codec") is UnsupportedCodecCaptureError

    def test_unknown_error_falls_back_to_other(self) -> None:
        result = classify_stderr("something completely unexpected happened")
        assert result is OtherCaptureError

    def test_empty_stderr_falls_back_to_other(self) -> None:
        assert classify_stderr("") is OtherCaptureError

    # Critical ordering test: this is the bug the ordering guards against.
    # ffmpeg echoes the input URL into many error lines; if the URL contains
    # a "?timeout=" query parameter, a naive "timeout" search would
    # misclassify a connection refusal as a timeout.
    def test_connection_refused_with_echoed_timeout_query_param_is_unreachable(
        self,
    ) -> None:
        stderr = (
            "rtsp://cam/stream?timeout=10000000: Connection refused\n"
            "Failed to open: rtsp://cam/stream?timeout=10000000"
        )
        result = classify_stderr(stderr)
        assert result is UnreachableCaptureError, (
            "connection-refused must win over the echoed ?timeout= URL token"
        )

    def test_404_not_found_classified_as_unreachable(self) -> None:
        assert classify_stderr("404 Not Found") is UnreachableCaptureError


# ---------------------------------------------------------------------------
# RTSP credential embedding
# ---------------------------------------------------------------------------


class TestApplyCredentials:
    def test_embeds_username_and_password_into_url(self) -> None:
        url = RtspAdapter._apply_credentials(
            "rtsp://cam.local/stream", ("admin", "password")
        )
        assert "admin:password@" in url

    def test_percent_encodes_special_chars_in_username(self) -> None:
        # '@' in username must be encoded so it doesn't break the URL structure
        url = RtspAdapter._apply_credentials(
            "rtsp://cam.local/stream", ("user@domain", "pass")
        )
        assert "user%40domain" in url
        assert url.count("@") == 1  # only the userinfo separator '@'

    def test_percent_encodes_at_sign_in_password(self) -> None:
        url = RtspAdapter._apply_credentials(
            "rtsp://cam.local/stream", ("admin", "p@ss")
        )
        assert "p%40ss" in url

    def test_percent_encodes_colon_in_password(self) -> None:
        url = RtspAdapter._apply_credentials(
            "rtsp://cam.local/stream", ("admin", "pass:word")
        )
        assert "pass%3Aword" in url

    def test_percent_encodes_slash_in_password(self) -> None:
        url = RtspAdapter._apply_credentials(
            "rtsp://cam.local/stream", ("admin", "pa/ss")
        )
        assert "pa%2Fss" in url

    def test_does_not_overwrite_existing_credentials_in_url(self) -> None:
        url_with_creds = "rtsp://root:existing@cam.local/stream"
        result = RtspAdapter._apply_credentials(url_with_creds, ("admin", "other"))
        assert result == url_with_creds

    def test_returns_url_unchanged_when_credentials_none(self) -> None:
        url = "rtsp://cam.local/stream"
        assert RtspAdapter._apply_credentials(url, None) == url

    def test_returns_url_unchanged_for_malformed_url(self) -> None:
        # No "://" separator — function should return url unchanged
        url = "not-a-url"
        result = RtspAdapter._apply_credentials(url, ("admin", "pass"))
        assert result == url


# ---------------------------------------------------------------------------
# Credentials do not appear in log output
# ---------------------------------------------------------------------------


class TestCredentialsNotLogged:
    def test_rtsp_password_not_in_adapter_log_output(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Building an RtspAdapter must not log the raw credentials."""
        with caplog.at_level(logging.DEBUG, logger="timelapse_manager"):
            RtspAdapter(
                stream_url="rtsp://cam.local/stream",
                credentials=("admin", "super_secret_password"),
            )
        log_text = caplog.text
        assert "super_secret_password" not in log_text
