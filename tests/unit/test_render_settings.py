"""Pure-function tests for the enumerated render settings.

Covers the encoder/container compatibility rule (every pair), the backward-
tolerant prefill view, and the flat-schedule -> job ``output_settings``
translation (including the "source" resolution that omits explicit dimensions).
"""

from __future__ import annotations

import pytest

from timelapse_manager.render import settings


@pytest.mark.parametrize(
    ("encoder", "container", "expected"),
    [
        # MP4 carries H.264/H.265/AV1 but not VP9.
        ("libx264", "mp4", True),
        ("libx265", "mp4", True),
        ("libsvtav1", "mp4", True),
        ("libvpx-vp9", "mp4", False),
        # WebM carries only VP9 (not AV1).
        ("libvpx-vp9", "webm", True),
        ("libx264", "webm", False),
        ("libx265", "webm", False),
        ("libsvtav1", "webm", False),
        # MKV (Matroska) carries all of them, including AV1.
        ("libx264", "mkv", True),
        ("libx265", "mkv", True),
        ("libvpx-vp9", "mkv", True),
        ("libsvtav1", "mkv", True),
    ],
)
def test_is_supported_combination(encoder: str, container: str, expected: bool) -> None:
    assert settings.is_supported_combination(encoder, container) is expected


def test_av1_is_an_offered_encoder() -> None:
    encoders = dict(settings.ENCODER_OPTIONS)
    assert encoders["libsvtav1"] == "AV1 (libsvtav1)"


def test_unknown_encoder_or_container_is_unsupported() -> None:
    assert settings.is_supported_combination("nope", "mp4") is False
    assert settings.is_supported_combination("libx264", "avi") is False


def test_combination_warning_present_only_for_invalid() -> None:
    assert settings.combination_warning("libx264", "mp4") is None
    warning = settings.combination_warning("libvpx-vp9", "mp4")
    assert warning is not None
    assert "VP9" in warning


def test_view_falls_back_to_defaults_for_none() -> None:
    view = settings.render_settings_view(None)
    assert view == {
        "enabled": False,
        "interval_seconds": settings.DEFAULT_FREQUENCY_SECONDS,
        "encoder": settings.DEFAULT_ENCODER,
        "container": settings.DEFAULT_CONTAINER,
        "fps": settings.DEFAULT_FPS,
        "resolution": settings.DEFAULT_RESOLUTION,
        "auto_prune": settings.DEFAULT_AUTO_PRUNE,
        "autoprune": settings.DEFAULT_AUTO_PRUNE,
    }


def test_view_falls_back_for_old_shape() -> None:
    # An old {enabled, interval_seconds}-only schedule keeps those and defaults
    # the rest.
    view = settings.render_settings_view({"enabled": True, "interval_seconds": 3600})
    assert view["enabled"] is True
    assert view["interval_seconds"] == 3600
    assert view["encoder"] == settings.DEFAULT_ENCODER
    assert view["container"] == settings.DEFAULT_CONTAINER


def test_view_clamps_unknown_values_to_defaults() -> None:
    view = settings.render_settings_view(
        {
            "encoder": "bogus",
            "container": "avi",
            # Out of the accepted [MIN_FPS, MAX_FPS] range -> falls back.
            "fps": 1000,
            "resolution": "9000x9000",
            "interval_seconds": 7,
        }
    )
    assert view["encoder"] == settings.DEFAULT_ENCODER
    assert view["container"] == settings.DEFAULT_CONTAINER
    assert view["fps"] == settings.DEFAULT_FPS
    assert view["resolution"] == settings.DEFAULT_RESOLUTION
    assert view["interval_seconds"] == settings.DEFAULT_FREQUENCY_SECONDS


def test_output_settings_from_flat_schedule() -> None:
    out = settings.output_settings_from_schedule(
        {
            "enabled": True,
            "interval_seconds": 86400,
            "encoder": "libx265",
            "container": "mkv",
            "fps": 30,
            "resolution": "1280x720",
        }
    )
    assert out == {
        "fps": 30,
        "codec": "libx265",
        "container": "mkv",
        "width": 1280,
        "height": 720,
    }


def test_output_settings_source_omits_dimensions() -> None:
    out = settings.output_settings_from_schedule(
        {
            "enabled": True,
            "interval_seconds": 86400,
            "encoder": "libx264",
            "container": "mp4",
            "fps": 24,
            "resolution": "source",
        }
    )
    assert out is not None
    assert "width" not in out
    assert "height" not in out
    assert out["codec"] == "libx264"


def test_output_settings_none_for_empty_or_off_schedule() -> None:
    assert settings.output_settings_from_schedule(None) is None
    assert settings.output_settings_from_schedule({}) is None
    # Old-shape schedule with no encode choices and no nested dict -> None.
    assert (
        settings.output_settings_from_schedule({"enabled": True, "interval_seconds": 1})
        is None
    )


def test_stored_codec_preserves_browser_streamable_contract() -> None:
    # The form/stored codec is the encoder name (libx264); the browser-streamable
    # check must still recognise an H.264/MP4 render as streamable so the inline
    # stream affordance does not silently disappear.
    from timelapse_manager.encode.browser_streamable import is_browser_streamable

    out = settings.output_settings_from_schedule(
        {
            "enabled": True,
            "interval_seconds": 86400,
            "encoder": "libx264",
            "container": "mp4",
            "fps": 24,
            "resolution": "1920x1080",
        }
    )
    assert out is not None
    assert is_browser_streamable(out["codec"], out["container"]) is True


def test_output_settings_passes_through_nested_legacy_dict() -> None:
    nested = {"width": 640, "height": 480, "fps": 12, "codec": "h264"}
    out = settings.output_settings_from_schedule(
        {"enabled": True, "interval_seconds": 1, "output_settings": nested}
    )
    assert out == nested


# --- Frame rate: arbitrary positive integers, no preset clamp ---------------


@pytest.mark.parametrize("fps", [1, 17, 24, 48, 120, 240])
def test_view_carries_arbitrary_integer_fps(fps: int) -> None:
    # Any whole number in range survives the view unchanged -- the old preset
    # clamp (24/25/30/60 only) is gone.
    view = settings.render_settings_view({"fps": fps})
    assert view["fps"] == fps


@pytest.mark.parametrize("fps", [0, -1, 1000])
def test_view_falls_back_for_out_of_range_fps(fps: int) -> None:
    view = settings.render_settings_view({"fps": fps})
    assert view["fps"] == settings.DEFAULT_FPS


@pytest.mark.parametrize("fps", [1, 17, 48, 120, 240])
def test_normalize_accepts_arbitrary_integer_fps(fps: int) -> None:
    doc = _normalize(fps=fps)
    assert doc["fps"] == fps


@pytest.mark.parametrize("fps", [0, -1, 1000, 24.5, "30", None, True])
def test_normalize_rejects_invalid_fps(fps: object) -> None:
    # ``bool`` is an ``int`` subclass, so ``True`` must be rejected explicitly.
    with pytest.raises(ValueError):
        _normalize(fps=fps)


def test_output_settings_carries_non_preset_fps() -> None:
    out = settings.output_settings_from_schedule(
        {
            "enabled": True,
            "interval_seconds": 86400,
            "encoder": "libx264",
            "container": "mp4",
            "fps": 17,
            "resolution": "source",
        }
    )
    assert out is not None
    assert out["fps"] == 17


# --- Auto-prune: enabled by default -----------------------------------------


def test_auto_prune_enabled_defaults_true_when_key_missing() -> None:
    assert settings.auto_prune_enabled(None) is True
    assert settings.auto_prune_enabled({}) is True
    assert settings.auto_prune_enabled({"enabled": True}) is True


def test_auto_prune_enabled_honours_stored_bool() -> None:
    assert settings.auto_prune_enabled({"auto_prune": False}) is False
    assert settings.auto_prune_enabled({"auto_prune": True}) is True


def test_normalize_sets_auto_prune_enabled_by_default() -> None:
    doc = _normalize()
    assert doc["auto_prune"] is True
    assert settings.auto_prune_enabled(doc) is True


def test_normalize_can_disable_auto_prune() -> None:
    doc = _normalize(auto_prune=False)
    assert doc["auto_prune"] is False
    assert settings.auto_prune_enabled(doc) is False


def test_view_exposes_autoprune() -> None:
    assert settings.render_settings_view({"auto_prune": False})["autoprune"] is False
    assert settings.render_settings_view({"auto_prune": True})["autoprune"] is True
    # Missing key -> enabled by default.
    assert settings.render_settings_view({})["autoprune"] is True


@pytest.mark.parametrize("stored", [True, False])
def test_auto_prune_round_trips_through_view(stored: bool) -> None:
    # The save path persists the view's output dict as the schedule, then the
    # accessor reads it back: the choice must survive that round trip rather than
    # silently reverting to the default.
    view = settings.render_settings_view({"auto_prune": stored})
    assert settings.auto_prune_enabled(view) is stored


# --- Suggested frame rates ---------------------------------------------------


@pytest.mark.parametrize("interval", [1, 30, 600, 86400])
def test_suggested_fps_invariants(interval: int) -> None:
    suggestions = settings.suggested_fps(interval)
    assert suggestions, "expected at least one suggestion"
    assert all(isinstance(f, int) for f in suggestions)
    assert all(settings.MIN_FPS <= f <= settings.MAX_FPS for f in suggestions)
    # Ascending and de-duplicated.
    assert suggestions == sorted(set(suggestions))


def test_suggested_fps_concrete_cases() -> None:
    # A fast cadence leans toward higher playback rates...
    assert settings.suggested_fps(1) == [24, 30, 60]
    # ...and a slow (daily) cadence toward lower ones.
    assert settings.suggested_fps(86400) == [6, 12, 24]


def _normalize(**overrides: object) -> dict[str, object]:
    """Build a normalized schedule with sensible defaults, overriding as needed."""
    kwargs: dict[str, object] = {
        "enabled": True,
        "interval_seconds": settings.DEFAULT_FREQUENCY_SECONDS,
        "encoder": settings.DEFAULT_ENCODER,
        "container": settings.DEFAULT_CONTAINER,
        "fps": settings.DEFAULT_FPS,
        "resolution": settings.DEFAULT_RESOLUTION,
    }
    kwargs.update(overrides)
    return settings.normalize_render_settings(**kwargs)  # type: ignore[arg-type]
