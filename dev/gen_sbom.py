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

The vendored web assets (htmx and the Geist fonts) are likewise invisible to
the scanner. Their single provenance record is
``src/timelapse_manager/web/static/vendor.json`` (#67 S25, #70 F-S7): each
entry's file is re-hashed here and must match the recorded SHA-256 before it is
added as a component, so the SBOM never vouches for bytes that are not the ones
shipped. A missing, malformed or mismatched manifest is a hard failure.

``--augment FILE`` skips the environment scan and instead takes an SBOM that
another tool produced (``trivy image`` or ``trivy rootfs --format cyclonedx``
over the container image or the CT template rootfs) and adds the same FFmpeg and
vendored-asset components to it. Those scanners see the operating-system
packages and the Python distributions but not a static binary or vendored web
assets, so the image and template SBOMs would otherwise omit both. The file is
rewritten in place unless ``--output`` names another path.

Every SBOM is also made neutral before it is written, because the files are
published: ``file:`` references (local build paths) are dropped, ``--rename
OLD=NEW`` rewrites a string wherever it appears (raw and URL-encoded, so a purl
repository URL is covered), and ``--component-name`` sets the described
component's name (scanners record a temporary directory or a registry path
there).

Usage::

    python dev/gen_sbom.py [--venv .venv] [--output cyclonedx-bom.json] \
        [--pin ffmpeg-pin.json] [--vendor src/timelapse_manager/web/static/vendor.json]
    python dev/gen_sbom.py --augment trivy-sbom.json [--output sbom.cdx.json]

Exit codes: 0 on success (SBOM written and valid CycloneDX), non-zero on a
hard failure (cyclonedx invocation failed, output was not valid CycloneDX, or
the vendored-asset manifest is missing or does not match the files). A missing
FFmpeg pin is a warning, not a failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
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
_DEFAULT_VENDOR = (
    _REPO_ROOT / "src" / "timelapse_manager" / "web" / "static" / "vendor.json"
)
_VENDOR_REQUIRED = ("path", "name", "type", "license", "sha256")


def _eprint(message: str) -> None:
    """Write a message to stderr."""
    print(message, file=sys.stderr)


def _run_cyclonedx(venv: Path, output: Path) -> None:
    """Invoke ``cyclonedx-py environment`` to write a CycloneDX JSON SBOM.

    Runs the tool with this interpreter (``python -m cyclonedx_py``), the
    project environment that ``make sbom`` enters with ``uv run``, so it
    resolves from the locked development dependencies rather than a globally
    installed copy. It deliberately does not shell out to ``uv run`` itself: a
    nested ``uv run`` re-syncs the project environment -- and recreates it when
    ``UV_PYTHON`` names another interpreter -- so the SBOM tests running under
    ``pytest -n auto`` rewrote the ``.venv`` every other worker was importing
    from (#34). Raises ``SystemExit`` on a non-zero exit.
    """
    cmd = [
        sys.executable,
        "-m",
        "cyclonedx_py",
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
    components[:] = [c for c in components if c.get("bom-ref") != component["bom-ref"]]
    components.append(component)


def _sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_vendor_assets(manifest_path: Path) -> list[dict[str, Any]]:
    """Load ``vendor.json`` and verify every listed file against its SHA-256.

    Paths are relative to the manifest's directory and must stay inside it.
    Any problem (missing or unreadable manifest, a missing field, a path that
    escapes, a missing file, a hash mismatch) is a hard error: a release SBOM
    must not record a vendored file it cannot vouch for.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _eprint(f"[sbom] ERROR: vendor manifest {manifest_path} is unreadable: {exc}")
        raise SystemExit(1) from exc
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    if not isinstance(assets, list) or not assets:
        _eprint(f"[sbom] ERROR: vendor manifest {manifest_path} lists no assets.")
        raise SystemExit(1)
    root = manifest_path.parent.resolve()
    for entry in assets:
        missing = [key for key in _VENDOR_REQUIRED if not entry.get(key)]
        if missing:
            _eprint(f"[sbom] ERROR: vendor entry {entry!r} lacks {missing}.")
            raise SystemExit(1)
        target = (root / str(entry["path"])).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            _eprint(f"[sbom] ERROR: vendored file {entry['path']!r} not found.")
            raise SystemExit(1)
        actual = _sha256_file(target)
        if actual != str(entry["sha256"]).lower():
            _eprint(
                f"[sbom] ERROR: {entry['path']} sha256 {actual} does not match "
                f"vendor.json ({entry['sha256']})."
            )
            raise SystemExit(1)
    return assets


def _vendor_component(entry: dict[str, Any]) -> dict[str, Any]:
    """Build a CycloneDX component for one verified ``vendor.json`` entry.

    The version is omitted when the manifest records it as ``unknown``; the
    origin note then goes in the description, so the gap stays visible.
    """
    name = str(entry["name"])
    version = str(entry.get("version") or "")
    known_version = version and version != "unknown"
    component: dict[str, Any] = {
        "type": str(entry["type"]),
        "bom-ref": f"vendored:{entry['path']}",
        "name": name,
        "description": (
            f"Vendored web asset web/static/{entry['path']}"
            + (f"; {entry['origin']}" if entry.get("origin") else "")
            + "."
        ),
        "licenses": [{"license": {"id": str(entry["license"])}}],
        "hashes": [{"alg": "SHA-256", "content": str(entry["sha256"]).lower()}],
    }
    if known_version:
        component["version"] = version
    if entry.get("supplier"):
        component["supplier"] = {"name": str(entry["supplier"])}
    if entry.get("purl"):
        component["purl"] = str(entry["purl"])
    if entry.get("upstream"):
        component["externalReferences"] = [
            {"type": "distribution", "url": str(entry["upstream"])}
        ]
    return component


def _inject_vendor(sbom: dict[str, Any], components: list[dict[str, Any]]) -> None:
    """Append (or replace) the vendored-asset components in the SBOM in place."""
    refs = {component["bom-ref"] for component in components}
    existing = sbom.setdefault("components", [])
    existing[:] = [c for c in existing if c.get("bom-ref") not in refs]
    existing.extend(components)


def _rewrite(node: Any, pairs: list[tuple[str, str]]) -> Any:
    """Return *node* with every string rewritten and ``file:`` references dropped."""
    if isinstance(node, str):
        for old, new in pairs:
            node = node.replace(old, new)
        return node
    if isinstance(node, list):
        kept = [
            item
            for item in node
            if not (
                isinstance(item, dict)
                and item.get("type") in {"distribution", "vcs", "website", "other"}
                and str(item.get("url", "")).startswith("file:")
            )
        ]
        return [_rewrite(item, pairs) for item in kept]
    if isinstance(node, dict):
        return {key: _rewrite(value, pairs) for key, value in node.items()}
    return node


def _neutralise(
    sbom: dict[str, Any], renames: list[str], component_name: str | None
) -> dict[str, Any]:
    """Make the SBOM safe to publish (see the module docstring)."""
    pairs: list[tuple[str, str]] = []
    for spec in renames:
        old, sep, new = spec.partition("=")
        if not sep or not old:
            _eprint(f"[sbom] ERROR: --rename expects OLD=NEW, got {spec!r}.")
            raise SystemExit(2)
        pairs.append((old, new))
        quoted = urllib.parse.quote(old, safe="")
        if quoted != old:
            pairs.append((quoted, urllib.parse.quote(new, safe="")))
    # Longest first so a path is rewritten before any prefix of it.
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    result: dict[str, Any] = _rewrite(sbom, pairs)
    if component_name:
        metadata = result.setdefault("metadata", {})
        component = metadata.get("component")
        if isinstance(component, dict):
            component["name"] = component_name
    return result


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
        default=None,
        help=(
            "Where to write the SBOM (default: ./cyclonedx-bom.json, or the "
            "--augment file itself)."
        ),
    )
    parser.add_argument(
        "--augment",
        type=Path,
        default=None,
        help=(
            "Add the FFmpeg and vendored-asset components to this existing "
            "CycloneDX SBOM instead of scanning an environment."
        ),
    )
    parser.add_argument(
        "--pin",
        type=Path,
        default=_DEFAULT_PIN,
        help="Path to the FFmpeg pin file (default: ./ffmpeg-pin.json).",
    )
    parser.add_argument(
        "--vendor",
        type=Path,
        default=_DEFAULT_VENDOR,
        help=(
            "Path to the vendored web-asset manifest "
            "(default: src/timelapse_manager/web/static/vendor.json)."
        ),
    )
    parser.add_argument(
        "--rename",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Rewrite OLD to NEW in every string of the SBOM (repeatable).",
    )
    parser.add_argument(
        "--component-name",
        default=None,
        help="Set the name of the component the SBOM describes.",
    )
    args = parser.parse_args(argv)
    output: Path = args.output or args.augment or _DEFAULT_OUTPUT

    # Verify the vendored files before the (slow) scan, so a mismatch fails fast.
    vendor = [_vendor_component(entry) for entry in _load_vendor_assets(args.vendor)]

    if args.augment is not None:
        try:
            sbom = json.loads(args.augment.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _eprint(f"[sbom] ERROR: {args.augment} is unreadable: {exc}")
            raise SystemExit(1) from exc
        if not isinstance(sbom, dict):
            _eprint(f"[sbom] ERROR: {args.augment} is not a CycloneDX document.")
            raise SystemExit(1)
    else:
        _run_cyclonedx(args.venv, output)
        sbom = json.loads(output.read_text(encoding="utf-8"))
    _validate_cyclonedx(sbom)

    pin = _load_pin(args.pin)
    if pin is not None:
        component = _ffmpeg_component(pin)
        _inject_ffmpeg(sbom, component)
        _eprint(f"[sbom] injected FFmpeg component ffmpeg@{component['version']}.")
    _inject_vendor(sbom, vendor)
    _eprint(f"[sbom] injected {len(vendor)} vendored web-asset components.")
    sbom = _neutralise(sbom, args.rename, args.component_name)

    output.write_text(
        json.dumps(sbom, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )

    count = len(sbom.get("components", []))
    _eprint(f"[sbom] wrote {output} ({sbom.get('specVersion')}, {count} components).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
