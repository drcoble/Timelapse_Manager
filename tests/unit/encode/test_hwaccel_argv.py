"""Unit tests for the hardware-acceleration argv assembly in FfmpegEncoder.

These tests call ``_build_argv`` directly, bypassing ``render()``, so no
subprocess is ever spawned and no real ffmpeg binary is needed.  The
``available_hw_encoders`` constructor parameter is the injection seam that
lets us exercise the hardware path on a machine without a GPU.

Naming note: this file covers argv *shape* -- flag presence, order, and
per-API differences -- not the actual encode outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from timelapse_manager.encode.encoder import (
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderSpec,
)
from timelapse_manager.encode.ffmpeg_impl import FfmpegEncoder

# ---------------------------------------------------------------------------
# Helpers: minimal fixtures
# ---------------------------------------------------------------------------


def _frame_refs(tmp_path: Path, count: int = 3) -> list[FrameRef]:
    """Return FrameRef objects pointing at real (but empty) tmp files.

    _build_argv only writes a concat list -- it never reads the frame data --
    so placeholder files are sufficient.
    """
    base_ts = datetime(2024, 6, 1, 12, tzinfo=UTC)
    refs = []
    for i in range(count):
        p = tmp_path / f"frame_{i:03d}.jpg"
        p.write_bytes(b"")  # placeholder -- _build_argv never decodes this
        refs.append(
            FrameRef(
                sequence_index=i,
                capture_timestamp=base_ts + timedelta(minutes=i),
                absolute_path=p,
                width=64,
                height=48,
            )
        )
    return refs


def _minimal_output(
    codec: str = "h264",
    container: str = "mp4",
    crf: int | None = 23,
    fps: float = 24.0,
) -> OutputSettings:
    return OutputSettings(
        fps=fps,
        width=64,
        height=48,
        codec=codec,
        container=container,
        crf=crf,
    )


def _build_spec(
    tmp_path: Path,
    output_path: Path,
    *,
    codec: str = "h264",
    container: str = "mp4",
    crf: int | None = 23,
    fps: float = 24.0,
) -> RenderSpec:
    frames = FrameSequence(project_id=1, frames=_frame_refs(tmp_path))
    return RenderSpec(
        project_id=1,
        frames=frames,
        output_settings=_minimal_output(
            codec=codec, container=container, crf=crf, fps=fps
        ),
        overlay=OverlayConfig(),  # all overlays off -> no font lookup
        chapters=[],
        deflicker=False,
        output_path=output_path,
        project_render_root=output_path.parent,
    )


def _call_build_argv(
    spec: RenderSpec,
    tmp_path: Path,
    encoder: FfmpegEncoder,
) -> list[str]:
    """Invoke _build_argv with a real tmp work_dir (needed for the concat list)."""
    output = spec.output_path.resolve()
    root = spec.project_render_root.resolve()
    return encoder._build_argv(spec, output, root, tmp_path)


# ---------------------------------------------------------------------------
# Software path -- byte-for-byte shape contract
# ---------------------------------------------------------------------------


class TestSoftwareArgvShape:
    def test_software_disabled_has_no_hwaccel_flags(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out)
        enc = FfmpegEncoder(hwaccel_enabled=False)
        argv = _call_build_argv(spec, tmp_path, enc)

        assert "-hwaccel" not in argv
        assert "-init_hw_device" not in argv
        assert "cuda" not in argv
        assert "-vaapi_device" not in argv
        assert "-global_quality" not in argv
        assert "-cq" not in argv

    def test_software_uses_libx264(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        enc = FfmpegEncoder(hwaccel_enabled=False)
        argv = _call_build_argv(spec, tmp_path, enc)

        cv_idx = argv.index("-c:v")
        assert argv[cv_idx + 1] == "libx264"

    def test_software_emits_pix_fmt_yuv420p(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out)
        enc = FfmpegEncoder(hwaccel_enabled=False)
        argv = _call_build_argv(spec, tmp_path, enc)

        pf_idx = argv.index("-pix_fmt")
        assert argv[pf_idx + 1] == "yuv420p"

    def test_software_emits_crf_when_set(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, crf=28)
        enc = FfmpegEncoder(hwaccel_enabled=False)
        argv = _call_build_argv(spec, tmp_path, enc)

        crf_idx = argv.index("-crf")
        assert argv[crf_idx + 1] == "28"

    def test_software_no_quality_flag_when_crf_none(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, crf=None)
        enc = FfmpegEncoder(hwaccel_enabled=False)
        argv = _call_build_argv(spec, tmp_path, enc)

        assert "-crf" not in argv
        assert "-b:v" not in argv

    def test_software_unchanged_when_hwaccel_requested_but_unavailable(
        self, tmp_path: Path
    ) -> None:
        """Forcing fallback via empty available set must produce identical
        software argv."""
        out1 = tmp_path / "out1.mp4"
        out2 = tmp_path / "out2.mp4"
        spec1 = _build_spec(tmp_path, out1)
        spec2 = _build_spec(tmp_path, out2, codec="h264")

        enc_sw = FfmpegEncoder(hwaccel_enabled=False)
        enc_forced_fallback = FfmpegEncoder(
            hwaccel_enabled=True,
            hwaccel_api="nvenc",
            available_hw_encoders=frozenset(),  # nothing available -> fallback
        )

        argv_sw = _call_build_argv(spec1, tmp_path, enc_sw)
        argv_fallback = _call_build_argv(spec2, tmp_path, enc_forced_fallback)

        # Strip the output path (last element) and concat list path (which
        # differ because the specs have different output_path values) before
        # comparing: we want to confirm the codec/filter/quality shape is the
        # same, not the incidental file paths.
        def _strip_paths(argv: list[str]) -> list[str]:
            return [
                tok
                for tok in argv
                if not tok.endswith(".mp4") and not tok.endswith(".txt")
            ]

        assert _strip_paths(argv_sw) == _strip_paths(argv_fallback)


# ---------------------------------------------------------------------------
# NVENC hardware path
# ---------------------------------------------------------------------------


class TestNvencArgvShape:
    def _nvenc_enc(self, codec: str = "h264") -> FfmpegEncoder:
        return FfmpegEncoder(
            hwaccel_enabled=True,
            hwaccel_api="nvenc",
            available_hw_encoders=frozenset({"h264_nvenc", "hevc_nvenc"}),
        )

    def test_nvenc_emits_hwaccel_cuda(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._nvenc_enc())

        assert "-hwaccel" in argv
        hwa_idx = argv.index("-hwaccel")
        assert argv[hwa_idx + 1] == "cuda"

    def test_nvenc_uses_h264_nvenc_encoder(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._nvenc_enc())

        cv_idx = argv.index("-c:v")
        assert argv[cv_idx + 1] == "h264_nvenc"

    def test_nvenc_uses_cq_quality_flag(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264", crf=20)
        argv = _call_build_argv(spec, tmp_path, self._nvenc_enc())

        assert "-cq" in argv
        cq_idx = argv.index("-cq")
        assert argv[cq_idx + 1] == "20"
        assert "-crf" not in argv

    def test_nvenc_emits_pix_fmt_yuv420p(self, tmp_path: Path) -> None:
        # NVENC reads system-memory frames, so pix_fmt stays yuv420p.
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._nvenc_enc())

        pf_idx = argv.index("-pix_fmt")
        assert argv[pf_idx + 1] == "yuv420p"

    def test_nvenc_hwaccel_before_input_flags(self, tmp_path: Path) -> None:
        # -hwaccel cuda must appear before -loglevel/-y/-f concat (the input block).
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._nvenc_enc())

        hwa_idx = argv.index("-hwaccel")
        lg_idx = argv.index("-loglevel")
        assert hwa_idx < lg_idx


# ---------------------------------------------------------------------------
# QSV hardware path
# ---------------------------------------------------------------------------


class TestQsvArgvShape:
    def _qsv_enc(self) -> FfmpegEncoder:
        return FfmpegEncoder(
            hwaccel_enabled=True,
            hwaccel_api="qsv",
            available_hw_encoders=frozenset({"h264_qsv", "hevc_qsv", "av1_qsv"}),
        )

    def test_qsv_emits_init_hw_device(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._qsv_enc())

        assert "-init_hw_device" in argv
        iw_idx = argv.index("-init_hw_device")
        assert argv[iw_idx + 1].startswith("qsv")

    def test_qsv_uses_h264_qsv_encoder(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._qsv_enc())

        cv_idx = argv.index("-c:v")
        assert argv[cv_idx + 1] == "h264_qsv"

    def test_qsv_uses_global_quality_flag(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264", crf=22)
        argv = _call_build_argv(spec, tmp_path, self._qsv_enc())

        assert "-global_quality" in argv
        gq_idx = argv.index("-global_quality")
        assert argv[gq_idx + 1] == "22"
        assert "-crf" not in argv
        assert "-cq" not in argv

    def test_qsv_av1_uses_av1_qsv(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="av1")
        argv = _call_build_argv(spec, tmp_path, self._qsv_enc())

        cv_idx = argv.index("-c:v")
        assert argv[cv_idx + 1] == "av1_qsv"

    def test_qsv_filtergraph_ends_in_hwupload(self, tmp_path: Path) -> None:
        # QSV (hwupload API) must have hwupload in the filtergraph so frames
        # are on the device surface before the encoder reads them.
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._qsv_enc())

        vf_idx = argv.index("-vf")
        filtergraph = argv[vf_idx + 1]
        assert "hwupload" in filtergraph
        assert "format=nv12" in filtergraph


# ---------------------------------------------------------------------------
# VAAPI hardware path
# ---------------------------------------------------------------------------


class TestVaapiArgvShape:
    def _vaapi_enc(self, device: str | None = None) -> FfmpegEncoder:
        return FfmpegEncoder(
            hwaccel_enabled=True,
            hwaccel_api="vaapi",
            hwaccel_device=device,
            available_hw_encoders=frozenset({"h264_vaapi", "hevc_vaapi", "av1_vaapi"}),
        )

    def test_vaapi_does_not_emit_hwaccel_decode(self, tmp_path: Path) -> None:
        # VA-API deliberately keeps frames in system memory so the software
        # filtergraph can process them; the trailing format=nv12,hwupload uploads
        # to the device afterwards. Emitting -hwaccel/-hwaccel_output_format here
        # would deliver GPU surfaces the software filters cannot touch.
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._vaapi_enc())

        assert "-hwaccel" not in argv

    def test_vaapi_does_not_emit_hwaccel_output_format(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._vaapi_enc())

        assert "-hwaccel_output_format" not in argv

    def test_vaapi_includes_vaapi_device_when_set(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._vaapi_enc("/dev/dri/renderD128"))

        assert "-vaapi_device" in argv
        vd_idx = argv.index("-vaapi_device")
        assert argv[vd_idx + 1] == "/dev/dri/renderD128"

    def test_vaapi_no_vaapi_device_when_unset(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._vaapi_enc(None))

        assert "-vaapi_device" not in argv

    def test_vaapi_uses_h264_vaapi_encoder(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._vaapi_enc())

        cv_idx = argv.index("-c:v")
        assert argv[cv_idx + 1] == "h264_vaapi"

    def test_vaapi_uses_qp_quality_flag(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264", crf=25)
        argv = _call_build_argv(spec, tmp_path, self._vaapi_enc())

        assert "-qp" in argv
        qp_idx = argv.index("-qp")
        assert argv[qp_idx + 1] == "25"
        assert "-crf" not in argv

    def test_vaapi_filtergraph_ends_in_hwupload(self, tmp_path: Path) -> None:
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._vaapi_enc())

        vf_idx = argv.index("-vf")
        filtergraph = argv[vf_idx + 1]
        assert "hwupload" in filtergraph
        assert "format=nv12" in filtergraph

    def test_vaapi_no_pix_fmt_after_encoder(self, tmp_path: Path) -> None:
        # VA-API/QSV take pixel format from the uploaded surface, so no
        # trailing -pix_fmt should appear after -c:v on the VAAPI path.
        out = tmp_path / "out.mp4"
        spec = _build_spec(tmp_path, out, codec="h264")
        argv = _call_build_argv(spec, tmp_path, self._vaapi_enc())

        cv_idx = argv.index("-c:v")
        # -pix_fmt must not appear AFTER the -c:v position (that's the encoder
        # side).  It may appear in the filtergraph string, not as a bare flag.
        post_cv = argv[cv_idx:]
        assert "-pix_fmt" not in post_cv
