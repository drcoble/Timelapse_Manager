"""E2E app server whose render worker runs a slow fake encoder (#62).

Subprocess entry point for ``test_render_cancel_off_loop.py``; takes the same
arguments as ``_serve.py``, swaps the render queue's encoder for
:class:`SlowEncoder`, and runs ``_serve.main`` with the render worker started.
The Mac and CI test hosts need no ffmpeg for it: the fake behaves like
``FfmpegEncoder`` under cancellation (the partial output is removed before the
cancellation propagates) and otherwise encodes forever.

When a render starts, the encoder writes the partial output's path to
``<data dir>/render-started``, so the test knows the job is genuinely running
and which file must be gone after the cancel.

Do NOT import this module in tests -- it is a subprocess entry point only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

STARTED_MARKER = "render-started"


def _build_slow_encoder_class(marker: Path) -> type[Any]:
    """Return the fake encoder class (built after ``timelapse_manager`` imports)."""
    from timelapse_manager.encode.encoder import (
        Encoder,
        OutputSettings,
        RenderResult,
        RenderSpec,
    )

    class SlowEncoder(Encoder):
        """Writes a partial output, then encodes until cancelled."""

        async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
            return None

        async def render(self, spec: RenderSpec) -> RenderResult:
            spec.output_path.parent.mkdir(parents=True, exist_ok=True)
            spec.output_path.write_bytes(b"partial")
            marker.write_text(str(spec.output_path))
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # What FfmpegEncoder does: kill the child, remove the partial.
                spec.output_path.unlink(missing_ok=True)
                raise
            raise AssertionError("unreachable")

    return SlowEncoder


def main(argv: list[str]) -> None:
    import _serve  # type: ignore[import-not-found]  # sibling script, on sys.path

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", required=True)
    known, _ = parser.parse_known_args(argv)
    marker = Path(known.data_dir) / STARTED_MARKER

    from timelapse_manager.render import queue as queue_module

    slow_encoder = _build_slow_encoder_class(marker)

    def build_slow_encoder(*args: Any, **kwargs: Any) -> Any:
        return slow_encoder()

    queue_module.build_encoder = build_slow_encoder  # type: ignore[assignment]
    _serve.main([*argv, "--render-autostart"])


if __name__ == "__main__":
    main(sys.argv[1:])
