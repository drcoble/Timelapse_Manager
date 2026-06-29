"""Unit tests for the scene-metadata display normalizer.

Covers the pure functions that turn a raw per-frame scene-metadata envelope into
display-ready, grouped rows so templates stay dumb:

- a full envelope yields the three groups (Capture, Appearance, Exposure) in
  order, with the right human labels and value formatting (``×`` resolution,
  ``°`` rotation, signed ``EV``, overlays "none");
- a sparse envelope (only the always-present keys) yields just the Capture
  group, with no empty Appearance/Exposure headers;
- a missing or empty envelope yields an empty list (a template null state);
- unexpected value types never raise and degrade to sensible strings.

No I/O: every function under test is pure.
"""

from __future__ import annotations

import pytest

from timelapse_manager.cameras.scene_metadata import (
    SceneGroup,
    SceneRow,
    normalize_scene_metadata,
    scene_schema_version,
)


def _full_envelope() -> dict[str, object]:
    """A maximal envelope exposing every optional key, as a rich camera would."""
    return {
        "schema_version": 1,
        "source": "vapix",
        "captured_resolution": "1920x1080",
        "appearance_resolution": "1280x720",
        "compression": "30",
        "rotation": "180",
        "overlays": "",
        "brightness": "50",
        "contrast": "55",
        "saturation": "60",
        "sharpness": "65",
        "color_enabled": "yes",
        "exposure_value": "1.3",
        "exposure_priority": "balanced",
    }


def _rows_as_pairs(group: SceneGroup) -> list[tuple[str, str]]:
    return [(row.label, row.value) for row in group.rows]


# --- full envelope ----------------------------------------------------------


def test_full_envelope_yields_three_groups_in_order() -> None:
    groups = normalize_scene_metadata(_full_envelope())
    assert [g.title for g in groups] == ["Capture", "Appearance", "Exposure"]


def test_capture_group_labels_and_resolution_formatting() -> None:
    groups = normalize_scene_metadata(_full_envelope())
    capture = next(g for g in groups if g.title == "Capture")
    assert _rows_as_pairs(capture) == [
        ("Resolution", "1920 × 1080"),
        ("Source", "vapix"),
    ]


def test_appearance_group_labels_and_formatting() -> None:
    groups = normalize_scene_metadata(_full_envelope())
    appearance = next(g for g in groups if g.title == "Appearance")
    assert _rows_as_pairs(appearance) == [
        ("Stream resolution", "1280 × 720"),
        ("Compression", "30"),
        ("Rotation", "180°"),
        ("Overlays", "none"),
    ]


def test_exposure_group_labels_order_and_signed_ev() -> None:
    groups = normalize_scene_metadata(_full_envelope())
    exposure = next(g for g in groups if g.title == "Exposure")
    assert _rows_as_pairs(exposure) == [
        ("Brightness", "50"),
        ("Contrast", "55"),
        ("Saturation", "60"),
        ("Sharpness", "65"),
        ("Color", "yes"),
        ("Exposure", "+1.3 EV"),
        ("Exposure priority", "balanced"),
    ]


def test_negative_exposure_value_keeps_its_sign() -> None:
    meta = _full_envelope()
    meta["exposure_value"] = "-0.5"
    groups = normalize_scene_metadata(meta)
    exposure = next(g for g in groups if g.title == "Exposure")
    assert ("Exposure", "-0.5 EV") in _rows_as_pairs(exposure)


def test_camera_supplied_positive_sign_is_not_doubled() -> None:
    meta = _full_envelope()
    meta["exposure_value"] = "+2"
    groups = normalize_scene_metadata(meta)
    exposure = next(g for g in groups if g.title == "Exposure")
    assert ("Exposure", "+2 EV") in _rows_as_pairs(exposure)


def test_non_numeric_exposure_value_passes_through() -> None:
    meta = _full_envelope()
    meta["exposure_value"] = "auto"
    groups = normalize_scene_metadata(meta)
    exposure = next(g for g in groups if g.title == "Exposure")
    assert ("Exposure", "auto") in _rows_as_pairs(exposure)


def test_non_numeric_rotation_passes_through_without_degree_sign() -> None:
    meta = _full_envelope()
    meta["rotation"] = "flip"
    groups = normalize_scene_metadata(meta)
    appearance = next(g for g in groups if g.title == "Appearance")
    assert ("Rotation", "flip") in _rows_as_pairs(appearance)


def test_overlays_list_is_joined_and_empty_list_is_none() -> None:
    meta = _full_envelope()
    meta["overlays"] = ["clock", "logo"]
    appearance = next(
        g for g in normalize_scene_metadata(meta) if g.title == "Appearance"
    )
    assert ("Overlays", "clock, logo") in _rows_as_pairs(appearance)

    meta["overlays"] = []
    appearance = next(
        g for g in normalize_scene_metadata(meta) if g.title == "Appearance"
    )
    assert ("Overlays", "none") in _rows_as_pairs(appearance)


def test_resolution_accepts_unicode_separator() -> None:
    meta = _full_envelope()
    meta["captured_resolution"] = "1920×1080"
    capture = next(g for g in normalize_scene_metadata(meta) if g.title == "Capture")
    assert ("Resolution", "1920 × 1080") in _rows_as_pairs(capture)


def test_schema_version_is_never_a_display_row() -> None:
    groups = normalize_scene_metadata(_full_envelope())
    all_labels = [row.label for group in groups for row in group.rows]
    assert "schema_version" not in all_labels
    assert "Schema version" not in all_labels


# --- sparse envelope --------------------------------------------------------


def test_sparse_envelope_yields_only_capture_group() -> None:
    meta: dict[str, object] = {
        "schema_version": 1,
        "source": "vapix",
        "captured_resolution": "640x480",
    }
    groups = normalize_scene_metadata(meta)
    assert [g.title for g in groups] == ["Capture"]
    assert _rows_as_pairs(groups[0]) == [
        ("Resolution", "640 × 480"),
        ("Source", "vapix"),
    ]


def test_partial_appearance_skips_missing_rows() -> None:
    meta: dict[str, object] = {
        "schema_version": 1,
        "source": "vapix",
        "captured_resolution": "640x480",
        "compression": "20",
    }
    groups = normalize_scene_metadata(meta)
    assert [g.title for g in groups] == ["Capture", "Appearance"]
    appearance = next(g for g in groups if g.title == "Appearance")
    assert _rows_as_pairs(appearance) == [("Compression", "20")]


def test_present_key_with_none_value_is_skipped() -> None:
    meta: dict[str, object] = {
        "schema_version": 1,
        "source": "vapix",
        "captured_resolution": "640x480",
        "compression": None,
        "rotation": None,
    }
    groups = normalize_scene_metadata(meta)
    # Appearance had only None values -> no group emitted.
    assert [g.title for g in groups] == ["Capture"]


# --- null states ------------------------------------------------------------


def test_none_envelope_yields_empty_list() -> None:
    assert normalize_scene_metadata(None) == []


def test_empty_envelope_yields_empty_list() -> None:
    assert normalize_scene_metadata({}) == []


# --- defensive typing -------------------------------------------------------


def test_unexpected_value_types_do_not_raise() -> None:
    meta: dict[str, object] = {
        "schema_version": 1,
        "source": 7,  # int instead of str
        "captured_resolution": 1080,  # int that is not a WxH pair
        "compression": [1, 2, 3],  # list
        "rotation": 90,  # numeric int -> degree sign
        "overlays": None,  # explicit None -> skipped, not "none"
        "brightness": 12.5,  # float
        "exposure_value": 2,  # numeric int -> signed EV
    }
    groups = normalize_scene_metadata(meta)

    capture = next(g for g in groups if g.title == "Capture")
    assert ("Source", "7") in _rows_as_pairs(capture)
    # Not a WxH pair -> coerced as-is rather than mangled.
    assert ("Resolution", "1080") in _rows_as_pairs(capture)

    appearance = next(g for g in groups if g.title == "Appearance")
    appearance_pairs = _rows_as_pairs(appearance)
    assert ("Compression", "[1, 2, 3]") in appearance_pairs
    assert ("Rotation", "90°") in appearance_pairs
    # overlays was an explicit None -> skipped entirely.
    assert all(label != "Overlays" for label, _ in appearance_pairs)

    exposure = next(g for g in groups if g.title == "Exposure")
    exposure_pairs = _rows_as_pairs(exposure)
    assert ("Brightness", "12.5") in exposure_pairs
    assert ("Exposure", "+2 EV") in exposure_pairs


# --- schema version reader --------------------------------------------------


def test_scene_schema_version_reads_int() -> None:
    assert scene_schema_version({"schema_version": 1}) == 1


def test_scene_schema_version_none_for_missing_or_empty() -> None:
    assert scene_schema_version(None) is None
    assert scene_schema_version({}) is None
    assert scene_schema_version({"source": "vapix"}) is None


def test_scene_schema_version_none_for_non_int() -> None:
    assert scene_schema_version({"schema_version": "1"}) is None
    assert scene_schema_version({"schema_version": True}) is None


# --- dataclass surface ------------------------------------------------------


def test_row_and_group_are_frozen_dataclasses() -> None:
    row = SceneRow(label="Resolution", value="1920 × 1080")
    group = SceneGroup(title="Capture", rows=[row])
    assert group.rows[0] is row

    with pytest.raises(AttributeError):
        row.label = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        group.title = "x"  # type: ignore[misc]
