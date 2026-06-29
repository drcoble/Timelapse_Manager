"""Unit tests for auto-chapters logic in render/settings.py.

Covers:
- normalize_auto_chapters: valid values, invalid, None, "none"
- auto_chapters_choice: None schedule, missing key, unrecognised, valid
- normalize_render_settings: stores auto_chapters key in output
- output_settings_from_schedule: omits key for "none", includes for weekly/monthly
"""

from __future__ import annotations

from timelapse_manager.render.settings import (
    AUTO_CHAPTERS_KEY,
    DEFAULT_AUTO_CHAPTERS,
    auto_chapters_choice,
    normalize_auto_chapters,
    normalize_render_settings,
    output_settings_from_schedule,
)

# A minimal but complete normalize_render_settings call signature.
_BASE_KWARGS = {
    "enabled": True,
    "interval_seconds": 3600,
    "encoder": "libx264",
    "container": "mp4",
    "fps": 24,
    "resolution": "1920x1080",
}


# ---------------------------------------------------------------------------
# normalize_auto_chapters
# ---------------------------------------------------------------------------


class TestNormalizeAutoChapters:
    def test_weekly_is_returned_unchanged(self) -> None:
        assert normalize_auto_chapters("weekly") == "weekly"

    def test_monthly_is_returned_unchanged(self) -> None:
        assert normalize_auto_chapters("monthly") == "monthly"

    def test_none_string_collapses_to_none(self) -> None:
        assert normalize_auto_chapters("none") == DEFAULT_AUTO_CHAPTERS

    def test_python_None_collapses_to_none(self) -> None:
        assert normalize_auto_chapters(None) == DEFAULT_AUTO_CHAPTERS

    def test_unrecognised_string_collapses_to_none(self) -> None:
        assert normalize_auto_chapters("quarterly") == DEFAULT_AUTO_CHAPTERS

    def test_empty_string_collapses_to_none(self) -> None:
        assert normalize_auto_chapters("") == DEFAULT_AUTO_CHAPTERS

    def test_integer_collapses_to_none(self) -> None:
        assert normalize_auto_chapters(1) == DEFAULT_AUTO_CHAPTERS

    def test_dict_collapses_to_none(self) -> None:
        assert normalize_auto_chapters({}) == DEFAULT_AUTO_CHAPTERS

    def test_default_auto_chapters_constant_is_none_string(self) -> None:
        assert DEFAULT_AUTO_CHAPTERS == "none"


# ---------------------------------------------------------------------------
# auto_chapters_choice
# ---------------------------------------------------------------------------


class TestAutoChaptersChoice:
    def test_returns_none_for_none_schedule(self) -> None:
        assert auto_chapters_choice(None) == "none"

    def test_returns_none_for_empty_dict(self) -> None:
        assert auto_chapters_choice({}) == "none"

    def test_returns_none_when_key_absent(self) -> None:
        assert auto_chapters_choice({"fps": 24}) == "none"

    def test_returns_weekly_when_stored(self) -> None:
        assert auto_chapters_choice({AUTO_CHAPTERS_KEY: "weekly"}) == "weekly"

    def test_returns_monthly_when_stored(self) -> None:
        assert auto_chapters_choice({AUTO_CHAPTERS_KEY: "monthly"}) == "monthly"

    def test_returns_none_for_unrecognised_stored_value(self) -> None:
        assert auto_chapters_choice({AUTO_CHAPTERS_KEY: "daily"}) == "none"

    def test_returns_none_for_explicit_none_string(self) -> None:
        assert auto_chapters_choice({AUTO_CHAPTERS_KEY: "none"}) == "none"

    def test_non_dict_schedule_treated_as_no_schedule(self) -> None:
        # A non-dict value (e.g. from a corrupted row) must not raise.
        assert auto_chapters_choice("corrupted") == "none"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# normalize_render_settings stores auto_chapters
# ---------------------------------------------------------------------------


class TestNormalizeRenderSettingsAutoChapters:
    def test_default_auto_chapters_is_none(self) -> None:
        result = normalize_render_settings(**_BASE_KWARGS)
        assert result[AUTO_CHAPTERS_KEY] == "none"

    def test_weekly_is_stored(self) -> None:
        result = normalize_render_settings(**_BASE_KWARGS, auto_chapters="weekly")
        assert result[AUTO_CHAPTERS_KEY] == "weekly"

    def test_monthly_is_stored(self) -> None:
        result = normalize_render_settings(**_BASE_KWARGS, auto_chapters="monthly")
        assert result[AUTO_CHAPTERS_KEY] == "monthly"

    def test_unrecognised_value_normalised_to_none(self) -> None:
        result = normalize_render_settings(**_BASE_KWARGS, auto_chapters="quarterly")
        assert result[AUTO_CHAPTERS_KEY] == "none"


# ---------------------------------------------------------------------------
# output_settings_from_schedule auto_chapters key presence
# ---------------------------------------------------------------------------


class TestOutputSettingsFromScheduleAutoChapters:
    """output_settings_from_schedule only emits auto_chapters for real granularities."""

    def _schedule(self, auto_chapters: str) -> dict:
        """Build a full flat schedule via normalize_render_settings."""
        return normalize_render_settings(**_BASE_KWARGS, auto_chapters=auto_chapters)

    def test_none_choice_omits_auto_chapters_key(self) -> None:
        schedule = self._schedule("none")
        result = output_settings_from_schedule(schedule)
        assert result is not None
        assert AUTO_CHAPTERS_KEY not in result

    def test_weekly_includes_auto_chapters_key(self) -> None:
        schedule = self._schedule("weekly")
        result = output_settings_from_schedule(schedule)
        assert result is not None
        assert result[AUTO_CHAPTERS_KEY] == "weekly"

    def test_monthly_includes_auto_chapters_key(self) -> None:
        schedule = self._schedule("monthly")
        result = output_settings_from_schedule(schedule)
        assert result is not None
        assert result[AUTO_CHAPTERS_KEY] == "monthly"

    def test_none_schedule_returns_none(self) -> None:
        assert output_settings_from_schedule(None) is None

    def test_schedule_without_flat_keys_returns_none(self) -> None:
        # A schedule carrying only auto_chapters (no flat encode keys) is legacy.
        result = output_settings_from_schedule({AUTO_CHAPTERS_KEY: "weekly"})
        assert result is None

    def test_output_contains_fps_codec_container(self) -> None:
        schedule = self._schedule("none")
        result = output_settings_from_schedule(schedule)
        assert result is not None
        assert "fps" in result
        assert "codec" in result
        assert "container" in result

    def test_source_resolution_omits_width_height(self) -> None:
        schedule = normalize_render_settings(
            **{**_BASE_KWARGS, "resolution": "source"}, auto_chapters="none"
        )
        result = output_settings_from_schedule(schedule)
        assert result is not None
        assert "width" not in result
        assert "height" not in result

    def test_named_resolution_includes_width_height(self) -> None:
        result = output_settings_from_schedule(self._schedule("none"))
        assert result is not None
        assert result["width"] == 1920
        assert result["height"] == 1080
