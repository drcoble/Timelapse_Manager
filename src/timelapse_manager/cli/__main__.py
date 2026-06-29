"""Executable entry point for the CLI package.

Lets the CLI be invoked as ``python -m timelapse_manager.cli`` and serves as the
single script the packaged (frozen) ``timelapse-manager`` executable runs, so
the frozen binary exposes every subcommand -- in particular ``run`` (foreground
serve) and ``migrate`` -- through one entry point.
"""

from __future__ import annotations

# Absolute (not relative) import: this module is the script PyInstaller freezes
# as the executable's entry point, where it runs as top-level ``__main__`` with
# no package parent -- a relative import would fail there. The absolute form
# works identically under ``python -m timelapse_manager.cli``.
from timelapse_manager.cli import main

if __name__ == "__main__":
    main()
