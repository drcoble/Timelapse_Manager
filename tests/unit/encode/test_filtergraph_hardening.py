"""Hostile overlay, timestamp and font values through the built filtergraph (#67 S4).

Each case builds the real argv with ``FfmpegEncoder._build_argv`` (no spawn),
takes the ``-vf`` / ``-filter_complex`` value, and reads it back through a model
of ffmpeg's three parsers (:mod:`tests.unit.encode.ffmpeg_parse_model`):

* the graph must still contain exactly the filters the builder intended;
* every ``drawtext`` must carry exactly its eight options;
* a caption must come back as literal text, with no ``%{...}`` expansion;
* a timestamp format must come back as the single ``pts:gmtime`` call's
  strftime argument.

The model is a transcription of the FFmpeg 7.1 C code, so this is
self-consistent rather than proof: a real ffmpeg parse of these graphs is a CI
item.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.encode.ffmpeg_parse_model import (
    drawtext_options,
    expand_drawtext,
    parse_graph,
)
from timelapse_manager.encode import hwaccel
from timelapse_manager.encode.encoder import (
    EncoderCapabilityError,
    EncoderError,
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderSpec,
)
from timelapse_manager.encode.ffmpeg_impl import FfmpegEncoder

_DRAWTEXT_KEYS = [
    "fontfile",
    "text",
    "x",
    "y",
    "fontcolor",
    "fontsize",
    "box",
    "boxcolor",
]

# Captions chosen to break each parser level: quotes for the graph level, colons
# and backslashes for the option level, ``%`` and ``\`` for drawtext expansion,
# the graph separators, control characters, non-ASCII, a leading dash and a long
# value.
HOSTILE_CAPTIONS = [
    "'",
    "',null",
    "x',movie=/etc/passwd",
    "x',drawtext=textfile=/etc/passwd",
    "[a];movie=x",
    "[in]",
    "a,b;c[d]e",
    "%{eif:1:d}",
    "%{localtime}",
    "\\%{localtime}",
    "\\\\%{localtime}",
    "%",
    "100%",
    "%%",
    "\\",
    "a\\",
    "\\\\",
    "C:\\temp\\x",
    "a:b:c",
    "x:textfile=/etc/passwd",
    "x\\:textfile=/etc/passwd",
    "x\\':textfile=/etc/passwd",
    "line1\nline2\r\nline3\rline4",
    "tab\there",
    "nul\x00byte\x07bell\x1besc\x7fdel",
    "é ünïcödé 日本語 🎥 ’",
    "-leading dash",
    "--help",
    "}{",
    "$(id) `id` ;id",
    "x" * 5000,
    "a'b'c''d",
    "'\\''",
]

_CAPTION_IDS = [f"caption{i}" for i in range(len(HOSTILE_CAPTIONS))]

VALID_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%H:%M",
    "%d/%m/%Y %I:%M %p",
    "%a %d %b %Y",
    "%A %B %d",
    "%j.%y",
    "100%% %Z %z",
    "-%Y_%m",
    "::%S::",
    "a" * 64,
]


def _font(tmp_path: Path) -> Path:
    """A font file whose path contains a colon (an option separator)."""
    font_dir = tmp_path / "fonts:dir"
    font_dir.mkdir()
    font = font_dir / "font.ttf"
    font.write_bytes(b"")
    return font


def _spec(tmp_path: Path, overlay: OverlayConfig) -> RenderSpec:
    base = datetime(2024, 6, 1, 12, tzinfo=UTC)
    frames = []
    for i in range(2):
        path = tmp_path / f"frame_{i}.jpg"
        path.write_bytes(b"")
        frames.append(
            FrameRef(
                sequence_index=i,
                capture_timestamp=base + timedelta(minutes=i),
                absolute_path=path,
                width=64,
                height=48,
            )
        )
    render_root = tmp_path / "renders"
    render_root.mkdir(exist_ok=True)
    return RenderSpec(
        project_id=1,
        frames=FrameSequence(project_id=1, frames=frames),
        output_settings=OutputSettings(
            fps=24.0, width=64, height=48, codec="h264", container="mp4", crf=23
        ),
        overlay=overlay,
        chapters=[],
        deflicker=False,
        output_path=render_root / "out.mp4",
        project_render_root=render_root,
    )


def _graph(tmp_path: Path, overlay: OverlayConfig) -> tuple[str, Path]:
    font = _font(tmp_path)
    encoder = FfmpegEncoder(font_path=str(font))
    spec = _spec(tmp_path, overlay)
    work = tmp_path / "work"
    work.mkdir()
    argv = encoder._build_argv(
        spec,
        spec.output_path.resolve(),
        spec.project_render_root.resolve(),
        work,
    )
    flag = "-filter_complex" if "-filter_complex" in argv else "-vf"
    return argv[argv.index(flag) + 1], font


def _expected_caption(caption: str) -> str:
    """What drawtext should display for ``caption``.

    Line breaks and tabs become spaces, other controls are dropped, ``'`` is the
    typographic quote, and the option parser trims unescaped outer spaces.
    """
    text = caption.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = text.replace("\t", " ")
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return text.replace("'", "\u2019").strip(" ")


class TestHostileCaptions:
    @pytest.mark.parametrize("caption", HOSTILE_CAPTIONS, ids=_CAPTION_IDS)
    def test_caption_reads_back_as_literal_text(
        self, tmp_path: Path, caption: str
    ) -> None:
        graph, font = _graph(
            tmp_path, OverlayConfig(text_enabled=True, text_content=caption)
        )

        names = [f.name for f in parse_graph(graph)]
        assert names == ["setpts", "drawtext", "setpts", "fps", "scale", "format"]
        (options,) = drawtext_options(graph)
        assert list(options) == _DRAWTEXT_KEYS
        assert options["fontfile"] == str(font)
        literal, calls = expand_drawtext(options["text"])
        assert calls == []
        assert literal == _expected_caption(caption)

    @pytest.mark.parametrize("caption", HOSTILE_CAPTIONS, ids=_CAPTION_IDS)
    def test_caption_with_timestamp_and_image_keeps_graph_shape(
        self, tmp_path: Path, caption: str
    ) -> None:
        overlay = OverlayConfig(
            timestamp_enabled=True,
            text_enabled=True,
            text_content=caption,
            image_enabled=True,
            image_path=str(tmp_path / "renders" / "logo.png"),
        )
        (tmp_path / "renders").mkdir()
        (tmp_path / "renders" / "logo.png").write_bytes(b"")

        graph, _ = _graph(tmp_path, overlay)

        parsed = parse_graph(graph)
        assert [f.name for f in parsed] == [
            "setpts",
            "drawtext",
            "drawtext",
            "setpts",
            "fps",
            "scale",
            "format",
            "overlay",
        ]
        assert parsed[0].inputs == ["0:v"]
        assert parsed[-2].outputs == ["v"]
        assert parsed[-1].inputs == ["v", "1:v"]
        assert parsed[-1].outputs == ["out"]
        stamp, text = drawtext_options(graph)
        assert expand_drawtext(text["text"]) == (_expected_caption(caption), [])
        _, calls = expand_drawtext(stamp["text"])
        assert calls == [["pts", "gmtime", "0", "%Y-%m-%d %H:%M:%S"]]


class TestTimestampFormats:
    @pytest.mark.parametrize("fmt", VALID_FORMATS)
    def test_format_reaches_strftime_unchanged(self, tmp_path: Path, fmt: str) -> None:
        graph, _ = _graph(
            tmp_path, OverlayConfig(timestamp_enabled=True, timestamp_format=fmt)
        )

        (options,) = drawtext_options(graph)
        assert list(options) == _DRAWTEXT_KEYS
        literal, calls = expand_drawtext(options["text"])
        assert literal == "\x00"  # the whole text is the one function call
        assert calls == [["pts", "gmtime", "0", fmt]]

    @pytest.mark.parametrize(
        "fmt",
        [
            "'",
            "',null",
            "x',movie=/etc/passwd",
            "[a];movie=x",
            "%{eif:1:d}",
            "\\",
            "%H\n",
            "%Y" * 33,
        ],
    )
    def test_invalid_format_fails_before_a_graph_exists(
        self, tmp_path: Path, fmt: str
    ) -> None:
        # The write paths reject these; a value that slipped past them still
        # never becomes part of a filtergraph.
        with pytest.raises(EncoderError, match="invalid timestamp overlay format"):
            _graph(
                tmp_path, OverlayConfig(timestamp_enabled=True, timestamp_format=fmt)
            )


class TestFinalGraphCheck:
    """``_build_filtergraph`` parses its own output and refuses a mismatch."""

    @staticmethod
    def _build(
        tmp_path: Path,
        pre: list[str],
        image: list[str] | None = None,
        choice: hwaccel.EncoderChoice | None = None,
    ) -> str:
        spec = _spec(tmp_path, OverlayConfig())
        graph, _ = FfmpegEncoder()._build_filtergraph(
            spec,
            1_700_000_000,
            0,
            pre,
            image or [],
            choice or hwaccel.EncoderChoice("libx264", None),
        )
        return graph

    def test_injected_disallowed_filter_is_a_mismatch(self, tmp_path: Path) -> None:
        with pytest.raises(EncoderError, match="filtergraph mismatch"):
            self._build(tmp_path, ["drawtext=text='x',movie=/etc/passwd"])

    def test_injected_allowlisted_filter_is_a_mismatch(self, tmp_path: Path) -> None:
        # A second drawtext passes a name allowlist and a set comparison; only
        # the ordered list comparison catches it.
        with pytest.raises(EncoderError, match="filtergraph mismatch"):
            self._build(tmp_path, ["drawtext=text='a',drawtext=textfile=/x"])

    def test_injected_chain_separator_is_a_mismatch(self, tmp_path: Path) -> None:
        with pytest.raises(EncoderError, match="filtergraph mismatch"):
            self._build(tmp_path, ["drawtext=text='a';[x]movie=/etc/passwd"])

    def test_mismatch_is_caught_on_the_filter_complex_branch(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(EncoderError, match="filtergraph mismatch"):
            self._build(tmp_path, [], image=["overlay=10:10,movie=/etc/passwd"])

    def test_mismatch_is_caught_on_the_hardware_branch(self, tmp_path: Path) -> None:
        choice = hwaccel.EncoderChoice("h264_vaapi", "vaapi")
        with pytest.raises(EncoderError, match="filtergraph mismatch"):
            self._build(
                tmp_path,
                ["drawtext=text='a',drawtext=text=b"],
                image=["overlay=10:10"],
                choice=choice,
            )

    def test_extra_drawtext_option_is_a_mismatch(self, tmp_path: Path) -> None:
        drawtext = (
            "drawtext=fontfile='/f.ttf':text='a':textfile=/etc/passwd"
            ":x=10:y=10:fontcolor=white:fontsize=24:box=1:boxcolor=black@0.4"
        )
        with pytest.raises(EncoderError, match="drawtext options"):
            self._build(tmp_path, [drawtext])

    def test_unparseable_graph_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(EncoderError, match="unparseable filtergraph"):
            self._build(tmp_path, ["drawtext=text='a'[unterminated"])

    def test_renamed_filter_is_refused_by_the_allowlist(self, tmp_path: Path) -> None:
        # The parsed names are what reach the allowlist, not the builder's list.
        with pytest.raises((EncoderError, EncoderCapabilityError)):
            self._build(tmp_path, ["'movie'=x"])

    def test_software_graph_is_unchanged(self, tmp_path: Path) -> None:
        graph = self._build(tmp_path, [])
        assert graph == (
            "setpts=PTS+1700000000/TB,setpts=N/24.0/TB,fps=24.0,scale=64:48,"
            "format=yuv420p"
        )
