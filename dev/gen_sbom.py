#!/usr/bin/env python3
"""Generate a CycloneDX SBOM for the installed environment.

This wraps ``cyclonedx-py environment`` to produce a CycloneDX JSON SBOM over
the project's virtual environment, then augments it with a component entry for
the bundled FFmpeg binary. FFmpeg ships as a native binary rather than a Python
package, so the dependency scanner cannot see it; releases bundle a pinned
static build, and a complete SBOM must account for it.

The FFmpeg pin is read at runtime from ``ffmpeg-pin.json`` at the repository
root (schema: ``version``, ``url``, ``sha256``, ``license``, ``binaries``).
When the pin file is absent the SBOM is still produced from the environment,
the FFmpeg component is skipped, and a clear warning is emitted -- so this is
runnable before the pin exists, just with an incomplete bundle picture.

Usage::

    python dev/gen_sbom.py [--venv .venv] [--output cyclonedx-bom.json] \
        [--pin ffmpeg-pin.json]

Exit codes: 0 on success (SBOM written and valid CycloneDX), non-zero on a
hard failure (cyclonedx invocation failed, or output was not valid CycloneDX).
A missing pin is a warning, not a failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Repository root is two levels up from this file (dev/gen_sbom.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_VENV = Path(
    os.environ.get("VIRTUAL_ENV")
    or os.environ.get("UV_PROJECT_ENVIRONMENT")
    or (_REPO_ROOT / ".venv")
)
_DEFAULT_OUTPUT = _REPO_ROOT / "cyclonedx-bom.json"
_DEFAULT_PIN = _REPO_ROOT / "ffmpeg-pin.json"


def _eprint(message: str) -> None:
    """Write a message to stderr."""
    print(message, file=sys.stderr)


def _run_cyclonedx(venv: Path, output: Path) -> None:
    """Invoke ``cyclonedx-py environment`` to write a CycloneDX JSON SBOM.

    Runs through ``uv run`` so the tool resolves from the project's locked
    development dependencies rather than relying on a globally installed copy.
    Raises ``SystemExit`` on a non-zero exit.
    """
    cmd = [
        "uv",
        "run",
        "cyclonedx-py",
        "environment",
        str(venv),
        "--of",
        "JSON",
        "-o",
        str(output),
    ]
    _eprint(f"[sbom] running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=_REPO_ROOT, check=False)
    if result.returncode != 0:
        _eprint(f"[sbom] ERROR: cyclonedx-py exited {result.returncode}")
        raise SystemExit(result.returncode)


def _load_pin(pin_path: Path) -> dict[str, Any] | None:
    """Load and minimally validate the FFmpeg pin file.

    Returns the parsed pin, or ``None`` when the file is absent. A present but
    malformed pin is a hard error: a release that ships FFmpeg must not record
    an unverifiable provenance.
    """
    if not pin_path.exists():
        _eprint(
            f"[sbom] WARNING: FFmpeg pin not found at {pin_path}; "
            "the SBOM will omit the bundled FFmpeg component. This is expected "
            "before the pin is created, but a release SBOM must include it."
        )
        return None
    try:
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _eprint(f"[sbom] ERROR: FFmpeg pin at {pin_path} is unreadable: {exc}")
        raise SystemExit(1) from exc
    version = pin.get("version")
    if not version:
        _eprint(f"[sbom] ERROR: FFmpeg pin at {pin_path} has no 'version'.")
        raise SystemExit(1)
    return pin


def _ffmpeg_component(pin: dict[str, Any]) -> dict[str, Any]:
    """Build a CycloneDX component entry for the bundled FFmpeg.

    The name deliberately contains ``ffmpeg`` and the pinned version so the
    component is discoverable by simple name/version matching. License and all
    download URLs (with the recorded SHA-256) are carried as provenance.

    Mirror URLs from ``mirror_urls`` are emitted as additional
    ``externalReferences`` entries alongside the primary ``url``. The single
    ``sha256`` is the trust anchor for all of them.
    """
    version = str(pin["version"])
    license_id = pin.get("license")
    primary_url = pin.get("url")
    sha256 = pin.get("sha256")

    # Build the ordered list of candidate URLs the same way the fetch consumers
    # do: mirror_urls first (owner mirror → durable), primary url as fallback.
    mirror_urls: list[str] = pin.get("mirror_urls") or []
    all_urls: list[str] = []
    seen: set[str] = set()
    for u in mirror_urls:
        u_str = str(u)
        if u_str and u_str not in seen:
            all_urls.append(u_str)
            seen.add(u_str)
    if primary_url and str(primary_url) not in seen:
        all_urls.append(str(primary_url))

    component: dict[str, Any] = {
        "type": "application",
        "bom-ref": f"ffmpeg@{version}",
        "name": "ffmpeg",
        "version": version,
        "description": (
            "Bundled static FFmpeg/ffprobe build invoked as a subprocess for "
            "video encoding."
        ),
        "purl": f"pkg:generic/ffmpeg@{version}",
    }
    if license_id:
        component["licenses"] = [{"license": {"name": str(license_id)}}]
    # All candidate URLs are recorded as distribution references so the SBOM
    # captures the full provenance chain (owner mirror + upstream). The sha256
    # field on the component is the single trust anchor that covers them all.
    if all_urls:
        component["externalReferences"] = [
            {"type": "distribution", "url": u} for u in all_urls
        ]
    elif primary_url:
        component["externalReferences"] = [
            {"type": "distribution", "url": str(primary_url)}
        ]
    if sha256:
        component["hashes"] = [{"alg": "SHA-256", "content": str(sha256)}]
    return component


def _inject_ffmpeg(sbom: dict[str, Any], component: dict[str, Any]) -> None:
    """Append (or replace) the FFmpeg component in the SBOM in place."""
    components = sbom.setdefault("components", [])
    # Replace any prior ffmpeg entry so repeated runs stay idempotent.
    components[:] = [
        c for c in components if c.get("bom-ref") != component["bom-ref"]
    ]
    components.append(component)


def _validate_cyclonedx(sbom: dict[str, Any]) -> None:
    """Assert the document self-identifies as CycloneDX. Raises on mismatch."""
    fmt = sbom.get("bomFormat")
    if fmt != "CycloneDX":
        _eprint(f"[sbom] ERROR: bomFormat is {fmt!r}, expected 'CycloneDX'.")
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    """Generate the SBOM and inject the bundled FFmpeg component."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--venv",
        type=Path,
        default=_DEFAULT_VENV,
        help=(
            "Path to the virtual environment to scan. "
            "Defaults to $VIRTUAL_ENV, then $UV_PROJECT_ENVIRONMENT, "
            "then <repo>/.venv."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="Where to write the SBOM (default: ./cyclonedx-bom.json).",
    )
    parser.add_argument(
        "--pin",
        type=Path,
        default=_DEFAULT_PIN,
        help="Path to the FFmpeg pin file (default: ./ffmpeg-pin.json).",
    )
    args = parser.parse_args(argv)

    _run_cyclonedx(args.venv, args.output)

    sbom = json.loads(args.output.read_text(encoding="utf-8"))
    _validate_cyclonedx(sbom)

    pin = _load_pin(args.pin)
    if pin is not None:
        component = _ffmpeg_component(pin)
        _inject_ffmpeg(sbom, component)
        _eprint(
            f"[sbom] injected FFmpeg component ffmpeg@{component['version']}."
        )

    args.output.write_text(
        json.dumps(sbom, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )

    count = len(sbom.get("components", []))
    _eprint(
        f"[sbom] wrote {args.output} ({sbom.get('specVersion')}, "
        f"{count} components)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
