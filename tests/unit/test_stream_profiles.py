"""Unit tests for the stream-identity seam.

Covers the camera-adapter surface that lets a project pick which of a camera's
named streams/profiles it captures from:

- the base adapter's single implicit "default" profile (rtsp/http inherit it);
- VAPIX stream-profile parsing helpers and ``list_stream_profiles`` over many /
  one / zero / unreachable cameras (no exception ever escapes);
- ONVIF media-profile parsing and ``list_stream_profiles`` happy path;
- ``build_adapter(..., stream_id=...)`` is honoured by capture: the outgoing
  VAPIX snapshot request reflects the chosen profile's resolution/compression
  (non-vacuous -- the request URL is captured and asserted on);
- a capture with ``stream_id=None`` behaves exactly as before (no param query,
  one snapshot GET) -- a regression guard.

No network, subprocess, or filesystem access: the HTTP layer is mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from timelapse_manager.cameras.base import (
    DEFAULT_STREAM_ID,
    DEFAULT_STREAM_LABEL,
    CapturedFrame,
    StreamProfile,
    StreamProfileResult,
)
from timelapse_manager.cameras.http_jpeg import HttpJpegAdapter
from timelapse_manager.cameras.onvif import OnvifAdapter, parse_profiles
from timelapse_manager.cameras.registry import build_adapter
from timelapse_manager.cameras.rtsp import RtspAdapter
from timelapse_manager.cameras.vapix import (
    SCENE_PARAM_GROUP,
    SNAPSHOT_PATH,
    STREAM_PROFILE_GROUP,
    VapixAdapter,
    parse_stream_profiles,
    snapshot_knobs_from_parameters,
    stream_profile_parameters,
)

_VAPIX = "timelapse_manager.cameras.vapix"


def _fake_frame() -> CapturedFrame:
    """A real CapturedFrame to stand in for ``frame_from_bytes`` in capture tests.

    ``capture()`` now reads the frame's dimensions and attaches scene metadata, so
    the snapshot result must be a genuine frame rather than a bare ``object()``.
    """
    return CapturedFrame(
        image_bytes=b"x",
        width=1,
        height=1,
        format="jpeg",
        captured_at=datetime.now(UTC),
    )


# A realistic two-profile StreamProfile param group, in the key=value shape Axis
# returns from param.cgi (the ``root.`` prefix is included to prove it is
# tolerated). Profile ids are their Names: "Quality" and "Bandwidth".
_TWO_PROFILE_PARAMS = {
    "root.StreamProfile.S0.Name": "Quality",
    "root.StreamProfile.S0.Description": "high quality",
    "root.StreamProfile.S0.Parameters": (
        "resolution=1920x1080&compression=10&videocodec=h264&fps=30"
    ),
    "root.StreamProfile.S1.Name": "Bandwidth",
    "root.StreamProfile.S1.Description": "low bandwidth",
    "root.StreamProfile.S1.Parameters": "resolution=640x480&compression=40",
}

_ONE_PROFILE_PARAMS = {
    "StreamProfile.S0.Name": "Only",
    "StreamProfile.S0.Parameters": "resolution=1280x720",
}


# ---------------------------------------------------------------------------
# Pure VAPIX parsing helpers
# ---------------------------------------------------------------------------


class TestParseStreamProfiles:
    def test_parses_multiple_profiles_in_index_order(self) -> None:
        profiles = parse_stream_profiles(_TWO_PROFILE_PARAMS)
        assert profiles == [
            StreamProfile(id="Quality", label="Quality"),
            StreamProfile(id="Bandwidth", label="Bandwidth"),
        ]

    def test_parses_single_profile(self) -> None:
        profiles = parse_stream_profiles(_ONE_PROFILE_PARAMS)
        assert profiles == [StreamProfile(id="Only", label="Only")]

    def test_empty_group_yields_no_profiles(self) -> None:
        assert parse_stream_profiles({}) == []

    def test_profile_without_name_is_skipped(self) -> None:
        params = {"root.StreamProfile.S0.Parameters": "resolution=800x600"}
        assert parse_stream_profiles(params) == []


class TestStreamProfileParameters:
    def test_returns_parameters_for_named_profile(self) -> None:
        result = stream_profile_parameters(_TWO_PROFILE_PARAMS, "Bandwidth")
        assert result == "resolution=640x480&compression=40"

    def test_returns_none_for_unknown_profile(self) -> None:
        assert stream_profile_parameters(_TWO_PROFILE_PARAMS, "Nope") is None


class TestSnapshotKnobsFromParameters:
    def test_extracts_resolution_and_compression_only(self) -> None:
        resolution, compression = snapshot_knobs_from_parameters(
            "resolution=1920x1080&compression=10&videocodec=h264&fps=30"
        )
        assert resolution == "1920x1080"
        assert compression == 10

    def test_missing_knobs_are_none(self) -> None:
        resolution, compression = snapshot_knobs_from_parameters("videocodec=h264")
        assert resolution is None
        assert compression is None

    def test_non_integer_compression_is_none(self) -> None:
        _, compression = snapshot_knobs_from_parameters("compression=high")
        assert compression is None


# ---------------------------------------------------------------------------
# Base default: single implicit profile (rtsp/http inherit it)
# ---------------------------------------------------------------------------


class TestBaseDefaultProfile:
    async def test_http_adapter_returns_single_implicit_profile(self) -> None:
        adapter = HttpJpegAdapter(MagicMock(), "http://192.0.2.10/snap.jpg")
        result = await adapter.list_stream_profiles()
        assert result.ok is True
        assert result.message is None
        assert result.profiles == [
            StreamProfile(id=DEFAULT_STREAM_ID, label=DEFAULT_STREAM_LABEL)
        ]

    async def test_rtsp_adapter_returns_single_implicit_profile(self) -> None:
        adapter = RtspAdapter("rtsp://192.0.2.10/stream")
        result = await adapter.list_stream_profiles()
        assert result.ok is True
        assert len(result.profiles) == 1
        assert result.profiles[0].id == DEFAULT_STREAM_ID


# ---------------------------------------------------------------------------
# VAPIX list_stream_profiles: many / one / zero / unreachable
# ---------------------------------------------------------------------------


def _vapix_with_params(params: dict[str, str]) -> VapixAdapter:
    """A VapixAdapter whose param read returns ``params`` (no real HTTP)."""
    adapter = VapixAdapter(MagicMock(), address="192.0.2.10")
    adapter._read_params = AsyncMock(return_value=params)  # type: ignore[method-assign]
    return adapter


class TestVapixListStreamProfiles:
    async def test_many_profiles(self) -> None:
        adapter = _vapix_with_params(_TWO_PROFILE_PARAMS)
        result = await adapter.list_stream_profiles()
        assert result.ok is True
        assert [p.id for p in result.profiles] == ["Quality", "Bandwidth"]

    async def test_exactly_one_profile(self) -> None:
        adapter = _vapix_with_params(_ONE_PROFILE_PARAMS)
        result = await adapter.list_stream_profiles()
        assert result.ok is True
        assert len(result.profiles) == 1

    async def test_zero_profiles_reports_not_ok_without_raising(self) -> None:
        # An empty group is how _read_params signals an unreachable/parse failure
        # (it catches CaptureError and returns {}). Reported as a clean ok=False.
        adapter = _vapix_with_params({})
        result = await adapter.list_stream_profiles()
        assert result.ok is False
        assert result.profiles == []
        assert result.message

    async def test_unreachable_camera_never_raises(self) -> None:
        # Even if the param read itself raised a CaptureError, list_stream_profiles
        # must return a clean result rather than letting it escape.
        from timelapse_manager.cameras.base import UnreachableCaptureError

        adapter = VapixAdapter(MagicMock(), address="192.0.2.10")
        adapter._read_params = AsyncMock(  # type: ignore[method-assign]
            side_effect=UnreachableCaptureError("camera down")
        )
        result = await adapter.list_stream_profiles()
        assert result.ok is False
        assert result.profiles == []
        assert "camera down" in (result.message or "")


# ---------------------------------------------------------------------------
# ONVIF: media-profile parsing + list_stream_profiles happy path
# ---------------------------------------------------------------------------

_GET_PROFILES_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="Profile_1"><tt:Name>MainStream</tt:Name></trt:Profiles>
      <trt:Profiles token="Profile_2"><tt:Name>SubStream</tt:Name></trt:Profiles>
    </trt:GetProfilesResponse>
  </s:Body>
</s:Envelope>"""


class TestOnvifProfiles:
    def test_parse_profiles_returns_token_and_name(self) -> None:
        profiles = parse_profiles(_GET_PROFILES_RESPONSE)
        assert profiles == [
            StreamProfile(id="Profile_1", label="MainStream"),
            StreamProfile(id="Profile_2", label="SubStream"),
        ]

    def test_parse_profiles_skips_tokenless_entries(self) -> None:
        xml = (
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
            "<s:Body><trt:GetProfilesResponse>"
            "<trt:Profiles/>"
            "</trt:GetProfilesResponse></s:Body></s:Envelope>"
        )
        assert parse_profiles(xml) == []

    async def test_list_stream_profiles_happy_path(self) -> None:
        adapter = OnvifAdapter(MagicMock(), address="192.0.2.20")
        adapter._soap_call = AsyncMock(  # type: ignore[method-assign]
            return_value=_GET_PROFILES_RESPONSE
        )
        result = await adapter.list_stream_profiles()
        assert result.ok is True
        assert [p.id for p in result.profiles] == ["Profile_1", "Profile_2"]

    async def test_list_stream_profiles_unreachable_is_clean(self) -> None:
        from timelapse_manager.cameras.base import UnreachableCaptureError

        adapter = OnvifAdapter(MagicMock(), address="192.0.2.20")
        adapter._soap_call = AsyncMock(  # type: ignore[method-assign]
            side_effect=UnreachableCaptureError("no route")
        )
        result = await adapter.list_stream_profiles()
        assert result.ok is False
        assert result.profiles == []


# ---------------------------------------------------------------------------
# StreamProfileResult shape
# ---------------------------------------------------------------------------


class TestStreamProfileResult:
    def test_defaults_to_ok_with_no_message(self) -> None:
        result = StreamProfileResult(profiles=[StreamProfile(id="a", label="A")])
        assert result.ok is True
        assert result.message is None


# ---------------------------------------------------------------------------
# build_adapter(..., stream_id=...) honoured by VAPIX capture (NON-vacuous)
# ---------------------------------------------------------------------------


def _camera(**overrides: object) -> MagicMock:
    """A minimal VAPIX camera record for build_adapter."""
    cam = MagicMock()
    cam.protocol = "vapix"
    cam.address = "192.0.2.10"
    cam.credentials = None
    cam.credentials_inherit_default = False
    cam.snapshot_uri = None
    cam.default_resolution = None
    return cam


class TestStreamIdHonouredByCapture:
    async def test_selected_profile_changes_the_snapshot_request(self) -> None:
        """A chosen stream_id resolves to its profile's resolution/compression,
        and the outgoing image.cgi request carries exactly those knobs."""
        cam = _camera()
        adapter = build_adapter(cam, MagicMock(), stream_id="Bandwidth")

        captured_urls: list[str] = []

        async def _record_get_image(
            client: object, url: str, creds: object, timeout: float
        ) -> bytes:
            captured_urls.append(url)
            return b"\xff\xd8\xff\xe0jpeg"

        # The StreamProfile read returns the profile group; the scene-metadata
        # read (a separate group) returns nothing so it never interferes with the
        # snapshot-knob assertions below.
        async def _record_read_params(
            group: str, *, timeout: float | None = None
        ) -> dict[str, str]:
            if group == STREAM_PROFILE_GROUP:
                return _TWO_PROFILE_PARAMS
            return {}

        get_image = AsyncMock(side_effect=_record_get_image)
        with (
            patch(f"{_VAPIX}.http_get_image", new=get_image),
            patch(f"{_VAPIX}.frame_from_bytes", return_value=_fake_frame()),
        ):
            adapter._read_params = _record_read_params  # type: ignore[method-assign]
            await adapter.capture()

        assert len(captured_urls) == 1
        url = captured_urls[0]
        assert SNAPSHOT_PATH in url
        # "Bandwidth" profile => resolution=640x480, compression=40.
        assert "resolution=640x480" in url
        assert "compression=40" in url
        # The other profile's knobs must NOT leak in.
        assert "1920x1080" not in url

    async def test_stale_stream_id_falls_back_to_default(self) -> None:
        """A stream_id that no longer matches any profile degrades to the camera
        default (no resolution/compression) rather than raising."""
        cam = _camera()
        adapter = build_adapter(cam, MagicMock(), stream_id="Deleted")
        captured_urls: list[str] = []

        async def _record_get_image(
            client: object, url: str, creds: object, timeout: float
        ) -> bytes:
            captured_urls.append(url)
            return b"\xff\xd8\xff\xe0jpeg"

        async def _read_params(
            group: str, *, timeout: float | None = None
        ) -> dict[str, str]:
            if group == STREAM_PROFILE_GROUP:
                return _TWO_PROFILE_PARAMS
            return {}

        get_image = AsyncMock(side_effect=_record_get_image)
        with (
            patch(f"{_VAPIX}.http_get_image", new=get_image),
            patch(f"{_VAPIX}.frame_from_bytes", return_value=_fake_frame()),
        ):
            adapter._read_params = _read_params  # type: ignore[method-assign]
            await adapter.capture()

        url = captured_urls[0]
        # Neither profile's knobs applied: a bare snapshot URL (camera default).
        assert "resolution=" not in url
        assert "compression=" not in url


class TestStreamIdNoneRegression:
    async def test_default_stream_skips_profile_resolution_and_takes_one_snapshot(
        self,
    ) -> None:
        """With stream_id=None the snapshot is taken from the camera default: the
        StreamProfile-resolution path is skipped (no stream knobs on the URL) and
        exactly one snapshot GET is made. The only param read is the best-effort
        scene-metadata read, which targets the scene group -- never the
        StreamProfile group."""
        cam = _camera()
        adapter = build_adapter(cam, MagicMock(), stream_id=None)

        captured_urls: list[str] = []

        async def _record_get_image(
            client: object, url: str, creds: object, timeout: float
        ) -> bytes:
            captured_urls.append(url)
            return b"\xff\xd8\xff\xe0jpeg"

        read_params = AsyncMock(return_value={})
        get_image = AsyncMock(side_effect=_record_get_image)
        with (
            patch(f"{_VAPIX}.http_get_image", new=get_image),
            patch(f"{_VAPIX}.frame_from_bytes", return_value=_fake_frame()),
        ):
            adapter._read_params = read_params  # type: ignore[method-assign]
            await adapter.capture()

        # The only param read is the scene-metadata read; the StreamProfile group
        # (resolution selection) is never touched on the default path.
        read_params.assert_called_once()
        scene_group = read_params.call_args.args[0]
        assert scene_group == SCENE_PARAM_GROUP
        assert scene_group != STREAM_PROFILE_GROUP
        # Exactly one snapshot GET, with no stream-profile knobs on the URL.
        assert len(captured_urls) == 1
        assert "resolution=" not in captured_urls[0]
        assert "compression=" not in captured_urls[0]


@pytest.mark.parametrize("protocol", ["rtsp", "http"])
def test_build_adapter_accepts_stream_id_for_single_stream(protocol: str) -> None:
    """build_adapter accepts stream_id for single-stream protocols (no-op)."""
    cam = MagicMock()
    cam.protocol = protocol
    cam.address = "192.0.2.10"
    cam.credentials = None
    cam.credentials_inherit_default = False
    cam.snapshot_uri = "http://192.0.2.10/snap.jpg"
    cam.stream_uri = "rtsp://192.0.2.10/stream"
    # Must not raise even though these adapters ignore the stream.
    build_adapter(cam, MagicMock(), stream_id="anything")
