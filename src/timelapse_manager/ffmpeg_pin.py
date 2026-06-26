"""FFmpeg binary pin and runtime resolution.

Two related concerns live here:

* **The pin** (:func:`load_ffmpeg_pin`) reads ``ffmpeg-pin.json`` -- the single
  source of truth for which static ffmpeg build a packaged release ships. The
  packaging pipeline reads it to download and verify the binary; this reader
  exposes the same data to the application (for example, to surface the pinned
  version or to attribute its license).

* **The resolver** (:func:`resolve_ffmpeg_binary`) decides, at runtime, *which*
  ffmpeg executable to invoke. A frozen release must use the binary laid into
  the bundle and fail loudly if it is missing; a container deployment points at
  a fixed path via configuration; a development checkout falls back to ``ffmpeg``
  on ``PATH``.

The pin file is located relative to the bundle/package (never the current
working directory), so resolution is correct regardless of where a frozen or
installed application is launched from.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import bundle_root, is_frozen

if TYPE_CHECKING:
    from .config import Settings

# Name of the pin file at the repository / bundle root.
_PIN_FILENAME = "ffmpeg-pin.json"

# Directory, relative to the bundle root, the packaging spec lays the bundled
# ffmpeg/ffprobe binaries into. The resolver and the spec must agree on this.
_BUNDLED_FFMPEG_DIR = "ffmpeg"


class FfmpegPinError(RuntimeError):
    """Raised when the ffmpeg pin file is missing, unreadable, or malformed."""


class FfmpegResolutionError(RuntimeError):
    """Raised when the ffmpeg binary cannot be resolved for the environment.

    This is intentionally fatal: a frozen release with no bundled ffmpeg cannot
    render, and silently falling back to ``PATH`` would mask a broken package.
    """


@dataclass(frozen=True)
class FfmpegPin:
    """Parsed contents of ``ffmpeg-pin.json``.

    :param version: the pinned ffmpeg version string.
    :param url: primary/fallback download URL of the static ``linux/amd64``
        tarball. Always a string for backward compatibility; consumers that
        want an ordered list of all mirror candidates should call
        :meth:`download_urls` instead.
    :param sha256: hex SHA-256 of the tarball. This is the **trust anchor**:
        regardless of which URL a tarball is fetched from, its SHA-256 must
        match this value before any binary is used.
    :param license: the build's license identifier (for attribution).
    :param binaries: mapping of logical name (``ffmpeg``/``ffprobe``) to the
        binary's path inside the tarball.
    :param mirror_urls: optional ordered list of candidate download URLs.
        When present, the list is tried in order by packaging consumers and
        ``url`` is used as the final fallback. When absent, ``[url]`` is used.
        The first entry is reserved for the owner-maintained durable mirror
        (replace the ``REPLACE_ME`` placeholder before a release).
    """

    version: str
    url: str
    sha256: str
    license: str
    binaries: dict[str, str]
    mirror_urls: tuple[str, ...] = ()

    def download_urls(self) -> list[str]:
        """Return an ordered list of candidate download URLs for this pin.

        Consumers should try each URL in order, verify the downloaded bytes
        against :attr:`sha256` regardless of source, and fail only when no
        URL yields a matching tarball. The ``sha256`` value is the sole trust
        anchor: the source URL does not affect what is safe to ship.

        :returns: non-empty list; the owner-mirror placeholder is first when
            present, ``url`` is always the final entry.
        """
        if self.mirror_urls:
            # Deduplicate while preserving order; url is the authoritative
            # upstream fallback and must always appear in the list.
            seen: list[str] = []
            for u in self.mirror_urls:
                if u not in seen:
                    seen.append(u)
            if self.url not in seen:
                seen.append(self.url)
            return seen
        return [self.url]


def _pin_path() -> Path:
    """Return the path to ``ffmpeg-pin.json``.

    When frozen, the packaging spec places the pin at the bundle root. In a
    source checkout the file lives at the repository root, one level above the
    ``src`` package directory; both locations are probed so the reader works in
    development and in a freeze without depending on the working directory.
    """
    bundled = bundle_root() / _PIN_FILENAME
    if bundled.is_file():
        return bundled
    # Source checkout: repo root is two parents up from this file
    # (src/timelapse_manager/ffmpeg_pin.py -> repo root).
    repo_root = Path(__file__).resolve().parent.parent.parent / _PIN_FILENAME
    return repo_root


def load_ffmpeg_pin() -> FfmpegPin:
    """Read and validate ``ffmpeg-pin.json`` into a :class:`FfmpegPin`.

    :raises FfmpegPinError: if the file is missing, is not valid JSON, or is
        missing a required field. The message names the problem so a packaging
        failure is diagnosable.
    """
    path = _pin_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FfmpegPinError(f"Cannot read ffmpeg pin '{path}': {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FfmpegPinError(f"Cannot parse ffmpeg pin '{path}': {exc}") from exc

    if not isinstance(data, dict):
        raise FfmpegPinError(f"ffmpeg pin '{path}' must be a JSON object")

    try:
        binaries = data["binaries"]
        # mirror_urls is optional; fall back to an empty tuple so the field
        # is always present but callers distinguish "explicit list" from "none".
        raw_mirrors = data.get("mirror_urls", [])
        if not isinstance(raw_mirrors, list):
            raise FfmpegPinError(
                f"ffmpeg pin '{path}': 'mirror_urls' must be a JSON array"
            )
        pin = FfmpegPin(
            version=str(data["version"]),
            url=str(data["url"]),
            sha256=str(data["sha256"]),
            license=str(data["license"]),
            binaries={str(k): str(v) for k, v in binaries.items()},
            mirror_urls=tuple(str(u) for u in raw_mirrors),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise FfmpegPinError(
            f"ffmpeg pin '{path}' is missing a required field: {exc}"
        ) from exc
    return pin


def _bundled_binary_path(name: str) -> Path:
    """Return the path a bundled ffmpeg-family binary is laid into.

    The packaging spec copies the binaries into ``<bundle>/ffmpeg/``; Windows
    binaries carry a ``.exe`` suffix, so it is appended there.
    """
    filename = f"{name}.exe" if sys.platform.startswith("win") else name
    return bundle_root() / _BUNDLED_FFMPEG_DIR / filename


def resolve_ffmpeg_binary(settings: Settings, name: str = "ffmpeg") -> str:
    """Resolve which ffmpeg-family executable to invoke for ``name``.

    Precedence:

    1. **Explicit knob** (``settings.render.ffmpeg_binary``): used verbatim when
       set. This is an operator's deliberate choice -- for example a container
       image that copies the pinned static binary to a fixed path and points the
       application at it -- so it wins even in a frozen process. (Only the
       ``ffmpeg`` binary is configured; ``ffprobe`` is resolved beside it.)
    2. **Frozen** (and no knob): the binary laid into the bundle at
       ``<bundle>/ffmpeg/<name>``. If it is absent the call **raises**
       :class:`FfmpegResolutionError` rather than silently falling back to
       ``PATH`` -- a frozen release ships its own ffmpeg, and its absence is a
       packaging defect that must surface, not be hidden.
    3. **Development / unfrozen** (and no knob): the bare name (``"ffmpeg"`` /
       ``"ffprobe"``), found on ``PATH`` as today.

    :param settings: resolved application settings.
    :param name: ``"ffmpeg"`` or ``"ffprobe"``.
    :returns: a path or bare command suitable for ``subprocess``/``asyncio``
        ``exec`` (never run through a shell).
    :raises FfmpegResolutionError: when frozen and the bundled binary is absent.
    """
    knob = settings.render.ffmpeg_binary
    if knob:
        # An explicit ffmpeg path is configured. ffprobe is expected to sit
        # beside it under the same name, so derive its sibling rather than
        # ignoring the operator's choice and reaching for PATH.
        if name == "ffmpeg":
            return knob
        return str(Path(knob).with_name(_sibling_name(name)))

    if is_frozen():
        candidate = _bundled_binary_path(name)
        if not candidate.is_file():
            raise FfmpegResolutionError(
                f"Bundled '{name}' not found at '{candidate}'. A packaged "
                "release must ship its own ffmpeg; this indicates a broken "
                "bundle. Reinstall the application, or set "
                "'render.ffmpeg_binary' (env 'TLM_RENDER__FFMPEG_BINARY') to a "
                "valid ffmpeg path."
            )
        return str(candidate)

    # Development / unfrozen with no knob: rely on PATH, today's behaviour.
    return name


def _sibling_name(name: str) -> str:
    """Return ``name`` with a platform-appropriate executable suffix.

    Used to derive ``ffprobe`` beside a configured ``ffmpeg`` path while
    preserving a ``.exe`` suffix on Windows.
    """
    if sys.platform.startswith("win"):
        return f"{name}{os.extsep}exe"
    return name
