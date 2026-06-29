#!/usr/bin/env python3
"""Resolve, mirror, and pin the latest stable static FFmpeg build.

This script is stdlib-only (no third-party dependencies) so it runs with a
bare ``python3`` on any CI host.

Modes
-----
--check
    Resolve the latest stable build and compare to the current pin.  No
    downloads, no registry uploads, no file writes.  Exit codes:
      0  – pin is up to date (version and sha256 match).
     10  – an update is available; run --apply to fetch and mirror it.
    Any other exit code signals a failure that must be investigated.

--apply
    Full run: resolve → download → verify sha256 → upload to the Gitea
    generic package registry (or confirm the existing mirror matches) →
    rewrite ``ffmpeg-pin.json``.  Idempotent: if the resolved version and
    sha256 already match the pin, exits 0 without touching anything.

Environment variables
---------------------
GITEA_BASE_URL   Base URL of the Gitea instance (e.g. https://git.2cbn.com).
GITEA_OWNER      Gitea user/org namespace that owns the package registry
                 (e.g. drew.coble).
GITEA_TOKEN      Gitea personal-access token with package:write scope.
                 Required for --apply.  --check does not need it.
GITHUB_TOKEN     Optional GitHub personal-access token.  Raises the API rate
                 limit from 60 to 5000 req/h, useful when running frequently.

Immutable, conflict-free mirror versions
----------------------------------------
The registry version segment embeds the first 8 hex of the sha256
(``ffmpeg-static/n8.1-f6f07ebf/…``), so every distinct build is a unique,
immutable object.  A rebuild of the same release under the same filename but
with different bytes uploads to a *new* path rather than colliding with the
old one, so the refresh never needs manual recovery.  A PUT that does 409 now
means the *identical* (version + sha256) build is already mirrored, which the
sha256 re-check confirms and treats as success.  The pin's human-readable
``version`` field stays the clean release label (``n8.1``); only the storage
path carries the sha8.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_RELEASE_URL = (
    "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
)

# Matches assets that are:
#   ffmpeg-n<MAJOR.MINOR>-latest-linux64-gpl-<MAJOR.MINOR>.tar.xz
# The version number appears twice; both occurrences must be identical.
# Excludes: -shared, -lgpl, master, win64, macOS.
_ASSET_RE = re.compile(
    r"^ffmpeg-n(\d+(?:\.\d+)+)-latest-linux64-gpl-(\d+(?:\.\d+)+)\.tar\.xz$"
)

LICENSE = "GPL-3.0"

# Exit code signalling "update available" (distinct from 1=error, 0=up-to-date)
EXIT_UPDATE_AVAILABLE = 10

# Path to ffmpeg-pin.json relative to this script's parent (i.e. repo root).
_PIN_PATH = Path(__file__).resolve().parent.parent / "ffmpeg-pin.json"


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _info(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

# Default User-Agent sent with all requests.  Some reverse-proxy WAFs block
# the default "Python-urllib/3.x" string; use a neutral value instead.
_DEFAULT_UA = "ffmpeg-pin-refresh/1.0"


def _http_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    """Perform a GET and return the body bytes; raise on non-2xx.

    For small responses (API calls, metadata).  Use :func:`_http_download_to`
    for large files.
    """
    merged = {"User-Agent": _DEFAULT_UA}
    merged.update(headers or {})
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GET {url} → HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc


def _http_download_to(
    url: str,
    dest: Path,
    headers: dict[str, str] | None = None,
) -> None:
    """Stream a GET response to *dest*, reporting progress.  Raise on non-2xx."""
    merged = {"User-Agent": _DEFAULT_UA}
    merged.update(headers or {})
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            chunk_size = 1 << 20  # 1 MiB
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print(
                        f"\r    {downloaded // (1 << 20)} MiB / "
                        f"{total // (1 << 20)} MiB ({pct}%)",
                        end="",
                        flush=True,
                    )
            if total:
                print()  # newline after progress
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GET {url} → HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc


def _http_put_file(
    url: str,
    src: Path,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Stream a file as a PUT body; return (status_code, body_bytes).

    Streams from disk rather than loading into memory, which is critical for
    large uploads (e.g. 120 MiB FFmpeg tarballs) where buffering the entire
    body would cause connection timeouts on the server side.
    """
    file_size = src.stat().st_size
    all_headers: dict[str, str] = {"User-Agent": _DEFAULT_UA}
    all_headers.update(headers or {})
    all_headers.setdefault("Content-Type", "application/octet-stream")
    all_headers["Content-Length"] = str(file_size)

    with src.open("rb") as fh:
        req = urllib.request.Request(url, data=fh, method="PUT", headers=all_headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


def _http_put(
    url: str,
    data: bytes,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Perform a PUT with an in-memory body; return (status_code, body_bytes).

    Use :func:`_http_put_file` for large uploads.
    """
    merged: dict[str, str] = {"User-Agent": _DEFAULT_UA}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=data, method="PUT", headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _github_headers(token: str | None) -> dict[str, str]:
    h: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gitea_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"token {token}"}


# ---------------------------------------------------------------------------
# SHA-256 helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Resolution logic
# ---------------------------------------------------------------------------


def _resolve_latest_stable(github_token: str | None) -> dict[str, Any]:
    """Query the GitHub release and return the best stable asset metadata.

    Returns a dict with keys: name, version, url, digest_sha256.
    Raises RuntimeError on any failure.
    """
    _info("Querying GitHub BtbN/FFmpeg-Builds release 'latest' ...")
    raw = _http_get(GITHUB_RELEASE_URL, headers=_github_headers(github_token))
    release = json.loads(raw)
    assets = release.get("assets", [])
    if not assets:
        raise RuntimeError(
            "GitHub release 'latest' returned no assets; "
            "API may be rate-limited or the release structure changed."
        )

    candidates: list[dict[str, Any]] = []
    for asset in assets:
        name = asset.get("name", "")
        m = _ASSET_RE.match(name)
        if not m:
            continue
        ver1, ver2 = m.group(1), m.group(2)
        if ver1 != ver2:
            # Both version occurrences must match; skip if they diverge.
            _warn(f"Skipping {name}: version mismatch ({ver1} vs {ver2})")
            continue
        # Parse version as a tuple of ints for reliable numeric comparison.
        ver_tuple = tuple(int(x) for x in ver1.split("."))
        digest = asset.get("digest", "")
        digest_sha256: str | None = None
        if digest.startswith("sha256:"):
            digest_sha256 = digest[len("sha256:") :]
        candidates.append(
            {
                "name": name,
                "version": f"n{ver1}",  # canonical form: "n8.1"
                "ver_tuple": ver_tuple,
                "url": asset["browser_download_url"],
                "digest_sha256": digest_sha256,
            }
        )

    if not candidates:
        raise RuntimeError(
            "No stable static linux64-gpl asset found in the 'latest' release.\n"
            f"Available assets: {[a.get('name') for a in assets]}"
        )

    # Pick the asset with the numerically highest version (e.g. n8.1 > n7.1).
    best = max(candidates, key=lambda c: c["ver_tuple"])
    _info(
        f"Selected stable build: {best['name']} "
        f"(version={best['version']}, github_digest={best['digest_sha256'] or 'none'})"
    )
    return best


# ---------------------------------------------------------------------------
# Pin file
# ---------------------------------------------------------------------------


def _load_pin() -> dict[str, Any]:
    """Return the parsed ffmpeg-pin.json, or an empty dict if absent/invalid."""
    if not _PIN_PATH.exists():
        _warn(f"ffmpeg-pin.json not found at {_PIN_PATH}; treating as empty.")
        return {}
    try:
        return json.loads(_PIN_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        _warn(f"Could not parse {_PIN_PATH}: {exc}; treating as empty.")
        return {}


def _write_pin(pin: dict[str, Any]) -> None:
    _PIN_PATH.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    _info(f"Wrote {_PIN_PATH}")


# ---------------------------------------------------------------------------
# Tarball inspection
# ---------------------------------------------------------------------------


def _peek_topdir(tarball_path: Path) -> str:
    """Return the top-level directory name from the tarball."""
    with tarfile.open(tarball_path, mode="r:xz") as tf:
        names = tf.getnames()
        if not names:
            raise RuntimeError("Tarball appears empty.")
        return names[0].split("/")[0]


# ---------------------------------------------------------------------------
# Mirror upload
# ---------------------------------------------------------------------------


def _mirror_url(gitea_base: str, owner: str, version: str, filename: str) -> str:
    # Note: owner may contain a dot (drew.coble) — Gitea accepts it here.
    return (
        f"{gitea_base}/api/packages/{owner}/generic/ffmpeg-static/{version}/{filename}"
    )


def _upload_to_gitea(
    gitea_base: str,
    owner: str,
    token: str,
    version: str,
    filename: str,
    tarball_path: Path,
    expected_sha256: str,
) -> str:
    """Stream the tarball file to the Gitea generic package registry.

    Returns the mirror URL on success.  HTTP 409 is treated as success IF the
    existing object's sha256 matches expected_sha256; otherwise fails.

    Uses :func:`_http_put_file` to stream from disk rather than loading the
    entire tarball into memory, which is required for ~120 MiB payloads.
    """
    url = _mirror_url(gitea_base, owner, version, filename)
    _info(f"Uploading to Gitea package registry: {url}")
    headers = {"Authorization": f"token {token}"}
    status, body = _http_put_file(url, tarball_path, headers=headers)
    if status in (200, 201):
        _info(f"Mirror upload succeeded (HTTP {status}).")
        return url

    if status == 409:
        _info(
            "HTTP 409 (package version exists); verifying sha256 of existing mirror ..."
        )
        try:
            existing_bytes = _http_get(url, headers={"Authorization": f"token {token}"})
        except RuntimeError as exc:
            _fail(
                f"409 from mirror PUT and could not re-download to verify: {exc}\n"
                "Delete the existing package version from Gitea and re-run --apply."
            )
        existing_sha = _sha256_bytes(existing_bytes)
        if existing_sha == expected_sha256:
            _info("Existing mirror sha256 matches — treating 409 as success.")
            return url
        # With the sha8 embedded in the version path, different bytes map to a
        # different path, so a 409 with a sha mismatch should be unreachable. If
        # it ever happens, the registry holds a corrupt object at this exact path.
        _fail(
            f"409 from mirror PUT and sha256 MISMATCH on existing object.\n"
            f"  Expected: {expected_sha256}\n"
            f"  Got:      {existing_sha}\n"
            "Unexpected (the version path embeds the sha8). The mirrored object at\n"
            "this path is corrupt; delete this 'ffmpeg-static' version in the Gitea\n"
            "registry, then re-run --apply."
        )

    _fail(
        f"Mirror PUT returned HTTP {status}.\n"
        f"Body: {body[:500].decode(errors='replace')}"
    )
    # unreachable; _fail exits
    return ""


# ---------------------------------------------------------------------------
# Main modes
# ---------------------------------------------------------------------------


def mode_check(github_token: str | None) -> None:
    """--check: compare latest stable to current pin; no side effects."""
    try:
        best = _resolve_latest_stable(github_token)
    except RuntimeError as exc:
        _fail(f"Could not resolve latest stable FFmpeg: {exc}")

    pin = _load_pin()
    pin_version = pin.get("version", "")
    pin_sha256 = pin.get("sha256", "")

    resolved_version = best["version"]
    resolved_sha256 = best["digest_sha256"]

    _info(f"Current pin: version={pin_version} sha256={pin_sha256[:16]}...")
    _info(f"Latest stable: version={resolved_version} sha256={resolved_sha256 or '?'}")

    if resolved_sha256 is None:
        _warn(
            "GitHub did not provide a digest for this asset; "
            "comparing version only.  Run --apply to get the definitive sha256."
        )
        if pin_version == resolved_version:
            _info("Pin version matches latest stable. Assuming up to date.")
            sys.exit(0)
        else:
            _info(f"Update available: {pin_version} → {resolved_version}")
            sys.exit(EXIT_UPDATE_AVAILABLE)

    if pin_version == resolved_version and pin_sha256 == resolved_sha256:
        _info("Pin is up to date.")
        sys.exit(0)

    reasons = []
    if pin_version != resolved_version:
        reasons.append(f"version {pin_version} → {resolved_version}")
    if pin_sha256 != resolved_sha256:
        reasons.append(
            f"sha256 changed ({pin_sha256[:16]}... → {resolved_sha256[:16]}...)"
        )
    _info(f"Update available: {', '.join(reasons)}")
    sys.exit(EXIT_UPDATE_AVAILABLE)


def mode_apply(
    github_token: str | None,
    gitea_base: str,
    gitea_owner: str,
    gitea_token: str,
) -> None:
    """--apply: resolve, download, verify, mirror, rewrite pin."""
    try:
        best = _resolve_latest_stable(github_token)
    except RuntimeError as exc:
        _fail(f"Could not resolve latest stable FFmpeg: {exc}")

    pin = _load_pin()
    resolved_version = best["version"]
    resolved_sha256_from_github = best["digest_sha256"]

    # Idempotency check: if both version and sha256 match the current pin,
    # and a real mirror URL is already present, skip everything.
    if (
        pin.get("version") == resolved_version
        and pin.get("sha256") == resolved_sha256_from_github
        and resolved_sha256_from_github is not None
        and any(gitea_base in u for u in pin.get("mirror_urls", []))
    ):
        _info(
            f"Pin is already up to date (version={resolved_version}) "
            "with a valid mirror URL.  Nothing to do."
        )
        sys.exit(0)

    filename = best["name"]
    upstream_url = best["url"]

    # Stream the download to a temp file so we can compute sha256, peek the
    # topdir, and upload without loading the entire ~120 MiB into memory.
    _info(f"Downloading {upstream_url} ...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tarball_path = Path(tmpdir) / filename
        try:
            _http_download_to(upstream_url, tarball_path)
        except RuntimeError as exc:
            _fail(f"Download failed: {exc}")

        computed_sha256 = _sha256_file(tarball_path)
        size_bytes = tarball_path.stat().st_size
        _info(f"Downloaded {size_bytes:,} bytes; sha256={computed_sha256}")

        # Defense-in-depth: if GitHub provided a digest, verify it matches.
        if resolved_sha256_from_github is not None:
            if computed_sha256 != resolved_sha256_from_github:
                _fail(
                    f"sha256 MISMATCH after download!\n"
                    f"  GitHub digest:   {resolved_sha256_from_github}\n"
                    f"  Computed sha256: {computed_sha256}\n"
                    "This may indicate a corrupt download or a tampered asset. "
                    "Retry --apply; if the problem persists, investigate the asset."
                )
            _info("sha256 verified against GitHub digest.")

        # Peek the tarball to learn the top-level directory name.
        _info("Inspecting tarball structure ...")
        try:
            topdir = _peek_topdir(tarball_path)
        except Exception as exc:
            _fail(f"Could not read tarball: {exc}")
        _info(f"Tarball top-level directory: {topdir}")

        # Mirror upload — stream from the temp file, not from memory.
        #
        # The registry version segment embeds the sha8 (``n8.1-f6f07ebf``) so every
        # distinct build is a unique, immutable object. A rebuild of the same
        # release under the same filename but with new bytes therefore uploads to a
        # *new* path instead of colliding with the old one — eliminating the
        # manual-recovery 409 case. The pin's human-readable ``version`` stays the
        # clean release label (``n8.1``); only the storage path carries the sha8.
        registry_version = f"{resolved_version}-{computed_sha256[:8]}"
        mirror_url = _upload_to_gitea(
            gitea_base=gitea_base,
            owner=gitea_owner,
            token=gitea_token,
            version=registry_version,
            filename=filename,
            tarball_path=tarball_path,
            expected_sha256=computed_sha256,
        )

    # Rewrite ffmpeg-pin.json.
    new_pin: dict[str, Any] = {
        "version": resolved_version,
        "url": upstream_url,
        "sha256": computed_sha256,
        "license": LICENSE,
        "binaries": {
            "ffmpeg": f"{topdir}/bin/ffmpeg",
            "ffprobe": f"{topdir}/bin/ffprobe",
        },
        "mirror_urls": [
            mirror_url,
            upstream_url,
        ],
    }
    _write_pin(new_pin)
    _info(
        f"ffmpeg-pin.json updated: version={resolved_version} "
        f"sha256={computed_sha256} mirror={mirror_url}"
    )
    _info(
        "Note: the Gitea package registry requires auth (HTTP 401 without token).\n"
        "      release.sh reads GITEA_TOKEN and adds the Authorization header when\n"
        "      fetching from the mirror URL."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve and mirror the latest stable static FFmpeg build."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check",
        action="store_true",
        help=(
            "Check whether an update is available (exit 0=up-to-date, "
            f"exit {EXIT_UPDATE_AVAILABLE}=update available). "
            "No side effects."
        ),
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help=("Download, verify, upload to mirror, and rewrite ffmpeg-pin.json."),
    )
    args = parser.parse_args()

    github_token = os.environ.get("GITHUB_TOKEN")

    if args.check:
        mode_check(github_token=github_token)
        return

    # --apply: require Gitea env vars.
    gitea_base = os.environ.get("GITEA_BASE_URL", "").rstrip("/")
    gitea_owner = os.environ.get("GITEA_OWNER", "")
    gitea_token = os.environ.get("GITEA_TOKEN", "")

    missing = [
        name
        for name, val in [
            ("GITEA_BASE_URL", gitea_base),
            ("GITEA_OWNER", gitea_owner),
            ("GITEA_TOKEN", gitea_token),
        ]
        if not val
    ]
    if missing:
        _fail(
            f"--apply requires environment variables: {', '.join(missing)}\n"
            "Set them before running --apply."
        )

    mode_apply(
        github_token=github_token,
        gitea_base=gitea_base,
        gitea_owner=gitea_owner,
        gitea_token=gitea_token,
    )


if __name__ == "__main__":
    main()
