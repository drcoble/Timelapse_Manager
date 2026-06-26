"""Component version probing.

Reports the application's own version and the version of the bundled/installed
ffmpeg binary. The ffmpeg probe is defensive: it runs on unauthenticated
liveness paths, so every failure mode degrades to a sentinel string rather than
raising.
"""

from __future__ import annotations

import importlib
import subprocess

from . import __version__

# Returned for each build-info field when no generated build module is present
# (the normal case in a development checkout).
_BUILD_INFO_UNKNOWN = "unknown"

# Maximum time to wait on the ffmpeg version probe. The probe runs on an
# unauthenticated liveness route, so it must never hang the request.
_FFMPEG_PROBE_TIMEOUT_SECONDS = 5.0

# Returned when ffmpeg cannot be located or queried.
_FFMPEG_UNAVAILABLE = "unavailable"


def get_app_version() -> str:
    """Return the application version string."""
    return __version__


def get_build_info() -> dict[str, str]:
    """Return the build's short commit SHA and UTC build date.

    The values come from a small module the release script generates at build
    time (``_build_info``), so a packaged build carries the exact commit and
    date it was frozen from. That module is intentionally absent from the source
    tree and from a development checkout; when it cannot be imported -- the
    normal dev case -- both fields degrade to ``"unknown"`` rather than raising.

    The module is imported dynamically (not a static import) so it need not exist
    for the package to type-check or run from source.
    """
    try:
        module = importlib.import_module("._build_info", package=__package__)
    except ImportError:
        return {"sha": _BUILD_INFO_UNKNOWN, "date": _BUILD_INFO_UNKNOWN}
    return {
        "sha": str(getattr(module, "BUILD_SHA", _BUILD_INFO_UNKNOWN)),
        "date": str(getattr(module, "BUILD_DATE", _BUILD_INFO_UNKNOWN)),
    }


def probe_ffmpeg_version(binary: str = "ffmpeg") -> str:
    """Return the ffmpeg version string, or ``"unavailable"`` if it cannot run.

    Invokes ``<binary> -version`` and parses the first line of its output. Any
    failure mode -- binary absent, non-zero exit, timeout, or unreadable
    output -- resolves to ``"unavailable"`` rather than raising, so callers on
    request-handling paths cannot crash.

    :param binary: the ffmpeg executable to probe. Defaults to ``"ffmpeg"`` on
        ``PATH``; startup passes the resolved binary so the probe reports the
        exact build the application will encode with.
    """
    try:
        completed = subprocess.run(
            [binary, "-version"],
            capture_output=True,
            text=True,
            timeout=_FFMPEG_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return _FFMPEG_UNAVAILABLE

    if completed.returncode != 0:
        return _FFMPEG_UNAVAILABLE

    first_line = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    return first_line or _FFMPEG_UNAVAILABLE
