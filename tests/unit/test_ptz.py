"""Unit tests for the pan/tilt/zoom (PTZ) seam.

Covers the camera-adapter surface that lets a project position a PTZ camera
before it captures:

- the base adapter's safe defaults (no PTZ): an empty, supported=False preset
  result; an all-None ``move_to`` no-op; and ``PTZUnsupportedError`` on any real
  positioning request (rtsp/http/onvif inherit this);
- VAPIX preset parsing (header line dropped, names with spaces preserved,
  numeric ordering, empty names skipped) and the ``PTZEnabled`` boolean
  interpretation (a non-empty ``"no"`` is not "supported");
- VAPIX ``list_ptz_presets`` over presets / enabled-but-no-presets / unreachable
  (no exception ever escapes);
- VAPIX ``move_to`` URL construction for the preset and raw-move branches, the
  bounded settle wait on success, and fail-closed ``PTZError`` on an Axis
  ``Error:`` body.

No network, subprocess, or filesystem access: the HTTP layer is mocked, and the
settle wait is neutralised so the suite never actually sleeps.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from timelapse_manager.cameras.base import (
    PTZError,
    PTZPreset,
    PTZPresetsResult,
    PTZUnsupportedError,
)
from timelapse_manager.cameras.http_jpeg import HttpJpegAdapter
from timelapse_manager.cameras.vapix import (
    PTZ_PARAM_GROUP,
    PTZ_PATH,
    VapixAdapter,
    build_ptz_goto_url,
    build_ptz_move_url,
    is_ptz_error_body,
    parse_ptz_presets,
    ptz_enabled_from_params,
)

_VAPIX = "timelapse_manager.cameras.vapix"

# A realistic presetposall body: the header line (no "="), a default "Home"
# preset, and two named positions -- one with a space, one lower-cased -- proving
# names round-trip verbatim and the header is dropped.
_PRESETPOSALL_BODY = (
    "Preset Positions for camera 1\r\n"
    "presetposno1=Home\r\n"
    "presetposno2=Position 1\r\n"
    "presetposno3=position 2\r\n"
)


# ---------------------------------------------------------------------------
# Pure VAPIX parsing helpers
# ---------------------------------------------------------------------------


class TestParsePtzPresets:
    def test_parses_presets_dropping_header_and_keeping_spaces(self) -> None:
        presets = parse_ptz_presets(_PRESETPOSALL_BODY)
        assert presets == [
            PTZPreset(id="Home", label="Home"),
            PTZPreset(id="Position 1", label="Position 1"),
            PTZPreset(id="position 2", label="position 2"),
        ]

    def test_orders_by_numeric_index_not_text(self) -> None:
        # Index 10 must sort after 2, not lexically before it.
        body = "presetposno2=Two\npresetposno10=Ten\n"
        assert [p.id for p in parse_ptz_presets(body)] == ["Two", "Ten"]

    def test_empty_body_yields_no_presets(self) -> None:
        assert parse_ptz_presets("Preset Positions for camera 1\n") == []

    def test_preset_with_empty_name_is_skipped(self) -> None:
        assert parse_ptz_presets("presetposno1=\n") == []


class TestPtzEnabledFromParams:
    def test_truthy_value_is_enabled(self) -> None:
        assert ptz_enabled_from_params({"root.PTZ.PTZEnabled": "yes"}) is True

    def test_non_empty_negative_value_is_not_enabled(self) -> None:
        # The key is present and non-empty, yet "no" must read as not supported --
        # a plain presence check would wrongly report support here.
        assert ptz_enabled_from_params({"root.PTZ.PTZEnabled": "no"}) is False

    def test_absent_flag_is_not_enabled(self) -> None:
        assert ptz_enabled_from_params({}) is False


class TestIsPtzErrorBody:
    def test_detects_error_prefix(self) -> None:
        assert is_ptz_error_body("Error: invalid preset name") is True

    def test_detects_hash_error_prefix(self) -> None:
        assert is_ptz_error_body("# Error: out of range") is True

    def test_success_body_is_not_an_error(self) -> None:
        assert is_ptz_error_body("") is False
        assert is_ptz_error_body("OK") is False


class TestBuildPtzUrls:
    def test_goto_url_encodes_preset_name_with_spaces(self) -> None:
        url = build_ptz_goto_url("192.0.2.10", "Position 1")
        assert PTZ_PATH in url
        assert "camera=1" in url
        # The space in the name must be percent-encoded, not left raw.
        assert "gotoserverpresetname=Position+1" in url or (
            "gotoserverpresetname=Position%201" in url
        )

    def test_move_url_includes_only_provided_axes(self) -> None:
        url = build_ptz_move_url("192.0.2.10", pan=12.5, zoom=900)
        assert "pan=12.5" in url
        assert "zoom=900" in url
        # tilt was None -- it must not appear at all.
        assert "tilt=" not in url

    def test_move_url_passes_negative_values_verbatim(self) -> None:
        # Ranges are camera units, not 0..1: a negative tilt must survive intact.
        url = build_ptz_move_url("192.0.2.10", tilt=-45.0)
        assert "tilt=-45.0" in url


# ---------------------------------------------------------------------------
# Base default: no PTZ (rtsp/http/onvif inherit it)
# ---------------------------------------------------------------------------


class TestBaseDefaultNoPtz:
    async def test_list_ptz_presets_reports_unsupported_but_ok(self) -> None:
        adapter = HttpJpegAdapter(MagicMock(), "http://192.0.2.10/snap.jpg")
        result = await adapter.list_ptz_presets()
        assert result == PTZPresetsResult(
            presets=[], ptz_supported=False, ok=True, message=None
        )

    async def test_move_to_with_no_args_is_a_noop(self) -> None:
        adapter = HttpJpegAdapter(MagicMock(), "http://192.0.2.10/snap.jpg")
        # Must not raise: there is nothing to position to.
        await adapter.move_to()

    async def test_move_to_with_a_preset_raises_unsupported(self) -> None:
        adapter = HttpJpegAdapter(MagicMock(), "http://192.0.2.10/snap.jpg")
        with pytest.raises(PTZUnsupportedError):
            await adapter.move_to(preset_id="Home")

    async def test_move_to_with_raw_axis_raises_unsupported(self) -> None:
        adapter = HttpJpegAdapter(MagicMock(), "http://192.0.2.10/snap.jpg")
        with pytest.raises(PTZUnsupportedError):
            await adapter.move_to(pan=10.0)


# ---------------------------------------------------------------------------
# VAPIX list_ptz_presets: presets / enabled-no-presets / unreachable
# ---------------------------------------------------------------------------


def _vapix() -> VapixAdapter:
    """A VapixAdapter with a stand-in client (no real HTTP is ever issued)."""
    return VapixAdapter(MagicMock(), address="192.0.2.10")


class TestVapixListPtzPresets:
    async def test_presets_present_is_supported(self) -> None:
        adapter = _vapix()
        adapter._read_params = AsyncMock(return_value={})  # type: ignore[method-assign]
        get_image = AsyncMock(return_value=_PRESETPOSALL_BODY.encode())
        with patch(f"{_VAPIX}.http_get_image", new=get_image):
            result = await adapter.list_ptz_presets()
        assert result.ok is True
        assert result.ptz_supported is True
        assert [p.id for p in result.presets] == ["Home", "Position 1", "position 2"]

    async def test_no_presets_but_enabled_flag_is_supported(self) -> None:
        # A PTZ camera with no presets defined still supports raw moves; the
        # PTZEnabled flag carries the support signal in that case.
        adapter = _vapix()
        adapter._read_params = AsyncMock(  # type: ignore[method-assign]
            return_value={"root.PTZ.PTZEnabled": "yes"}
        )
        get_image = AsyncMock(return_value=b"Preset Positions for camera 1\n")
        with patch(f"{_VAPIX}.http_get_image", new=get_image):
            result = await adapter.list_ptz_presets()
        assert result.ok is True
        assert result.presets == []
        assert result.ptz_supported is True

    async def test_no_presets_and_not_enabled_is_unsupported(self) -> None:
        adapter = _vapix()
        adapter._read_params = AsyncMock(  # type: ignore[method-assign]
            return_value={"root.PTZ.PTZEnabled": "no"}
        )
        get_image = AsyncMock(return_value=b"Preset Positions for camera 1\n")
        with patch(f"{_VAPIX}.http_get_image", new=get_image):
            result = await adapter.list_ptz_presets()
        assert result.ok is True
        assert result.ptz_supported is False

    async def test_unreachable_camera_never_raises(self) -> None:
        from timelapse_manager.cameras.base import UnreachableCaptureError

        adapter = _vapix()
        get_image = AsyncMock(side_effect=UnreachableCaptureError("camera down"))
        with patch(f"{_VAPIX}.http_get_image", new=get_image):
            result = await adapter.list_ptz_presets()
        assert result.ok is False
        assert result.ptz_supported is False
        assert result.presets == []
        assert "camera down" in (result.message or "")

    async def test_reads_the_correct_ptz_param_group(self) -> None:
        adapter = _vapix()
        read_params = AsyncMock(return_value={})
        adapter._read_params = read_params  # type: ignore[method-assign]
        get_image = AsyncMock(return_value=_PRESETPOSALL_BODY.encode())
        with patch(f"{_VAPIX}.http_get_image", new=get_image):
            await adapter.list_ptz_presets()
        read_params.assert_awaited_once_with(PTZ_PARAM_GROUP)


# ---------------------------------------------------------------------------
# VAPIX move_to: URL construction (preset vs raw), settle, fail-closed
# ---------------------------------------------------------------------------


class TestVapixMoveTo:
    async def test_no_args_is_noop_and_issues_no_request(self) -> None:
        adapter = _vapix()
        get_image = AsyncMock()
        with patch(f"{_VAPIX}.http_get_image", new=get_image):
            await adapter.move_to()
        get_image.assert_not_called()

    async def test_preset_branch_builds_goto_url_and_settles(self) -> None:
        adapter = _vapix()
        captured: list[str] = []

        async def _record(client, url, creds, timeout):  # type: ignore[no-untyped-def]
            captured.append(url)
            return b"OK"

        get_image = AsyncMock(side_effect=_record)
        sleep = AsyncMock()
        with (
            patch(f"{_VAPIX}.http_get_image", new=get_image),
            patch(f"{_VAPIX}.asyncio.sleep", new=sleep),
        ):
            await adapter.move_to(preset_id="Position 1")
        assert len(captured) == 1
        assert PTZ_PATH in captured[0]
        assert "gotoserverpresetname=Position" in captured[0]
        # A clean success must wait the bounded settle delay exactly once.
        sleep.assert_awaited_once()

    async def test_raw_branch_builds_move_url(self) -> None:
        adapter = _vapix()
        captured: list[str] = []

        async def _record(client, url, creds, timeout):  # type: ignore[no-untyped-def]
            captured.append(url)
            return b""

        get_image = AsyncMock(side_effect=_record)
        sleep = AsyncMock()
        with (
            patch(f"{_VAPIX}.http_get_image", new=get_image),
            patch(f"{_VAPIX}.asyncio.sleep", new=sleep),
        ):
            await adapter.move_to(pan=30.0, tilt=-10.0, zoom=500)
        url = captured[0]
        assert "pan=30.0" in url
        assert "tilt=-10.0" in url
        assert "zoom=500" in url
        assert "gotoserverpresetname" not in url

    async def test_error_body_fails_closed_without_settling(self) -> None:
        adapter = _vapix()
        get_image = AsyncMock(return_value=b"Error: preset name does not exist")
        sleep = AsyncMock()
        with (
            patch(f"{_VAPIX}.http_get_image", new=get_image),
            patch(f"{_VAPIX}.asyncio.sleep", new=sleep),
            pytest.raises(PTZError),
        ):
            await adapter.move_to(preset_id="Ghost")
        # Fail-closed: a rejected move must never reach the settle wait.
        sleep.assert_not_awaited()

    async def test_transport_failure_is_reraised_as_ptz_error(self) -> None:
        from timelapse_manager.cameras.base import UnreachableCaptureError

        adapter = _vapix()
        get_image = AsyncMock(side_effect=UnreachableCaptureError("no route"))
        with (
            patch(f"{_VAPIX}.http_get_image", new=get_image),
            pytest.raises(PTZError) as exc_info,
        ):
            await adapter.move_to(preset_id="Home")
        assert "no route" in str(exc_info.value)
