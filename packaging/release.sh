#!/usr/bin/env bash
#
# Build a self-contained Timelapse Manager release bundle with PyInstaller and
# package it as a versioned tarball.
#
#   ./packaging/release.sh [--skip-smoke]
#
# What it produces (under ./dist):
#   * timelapse-manager-<version>-<os>-<arch>/   -- the one-dir frozen bundle
#       (the `timelapse-manager` executable + its runtime + bundled FFmpeg +
#        templates/static/migrations declared in timelapse-manager.spec)
#   * timelapse-manager-<version>-<os>-<arch>.tar.gz  -- the released artifact
#
# IMPORTANT -- platform: PyInstaller is NOT a cross-compiler. It freezes for the
# host OS/arch it runs on. The shipped V1 artifact is **linux/amd64**, so this
# script must run on a linux/amd64 host (the release CI runner). Running it on
# macOS produces a macOS bundle that is useful only for validating the .spec
# (that all data files / hidden imports are captured and that paths resolve when
# frozen) -- it is NOT the released Linux artifact.
#
# The pinned static FFmpeg in ffmpeg-pin.json is a linux/amd64 build; on a
# non-linux host the spec falls back to whatever FFmpeg the host provides for
# the bundle's ffmpeg/ directory so the freeze can still be exercised locally.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SPEC="${REPO_ROOT}/timelapse-manager.spec"
DIST_DIR="${REPO_ROOT}/dist"
BUILD_DIR="${REPO_ROOT}/build"

SKIP_SMOKE=0
for arg in "$@"; do
    case "${arg}" in
        --skip-smoke) SKIP_SMOKE=1 ;;
        *) printf 'error: unknown argument: %s\n' "${arg}" >&2; exit 2 ;;
    esac
done

err() { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

# Single cleanup handler for every temp dir and the smoke subprocess, installed
# once for EXIT, INT, and TERM. A bare EXIT trap does not reliably fire when bash
# is killed by SIGINT/SIGTERM (Ctrl-C, or the parent `make` killing this script),
# which is precisely the interrupt path that previously orphaned the frozen
# bundle and left it holding the smoke port. Each variable is set later as that
# resource comes into being; until then it is empty and skipped. Installing one
# consolidated trap also avoids the `trap ... EXIT` clobbering that would
# otherwise leak the staged-FFmpeg temp dir once the smoke section added its own.
_FFMPEG_STAGE=""
SMOKE_DIR=""
APP_PID=""
cleanup() {
    # Kill the whole process group of the smoke subprocess, not just APP_PID:
    # the PyInstaller bootloader re-execs the real app as a child, so killing the
    # bootloader alone can leave the child holding the port. `setsid` (below)
    # makes APP_PID a group leader, so the negative-PID signal reaches the child.
    if [ -n "${APP_PID}" ]; then
        kill -TERM -- "-${APP_PID}" 2>/dev/null || true
        # Brief grace for a clean shutdown, then force-kill any survivors.
        for _ in 1 2 3 4 5; do
            kill -0 -- "-${APP_PID}" 2>/dev/null || break
            sleep 1
        done
        kill -KILL -- "-${APP_PID}" 2>/dev/null || true
    fi
    [ -n "${SMOKE_DIR}" ] && rm -rf "${SMOKE_DIR}"
    [ -n "${_FFMPEG_STAGE}" ] && rm -rf "${_FFMPEG_STAGE}"
}
trap cleanup EXIT INT TERM

# Read a top-level string field from ffmpeg-pin.json without assuming `jq`.
_pin_field() {
    local field="$1"
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "import json,sys;print(json.load(open('${REPO_ROOT}/ffmpeg-pin.json'))['${field}'])"
    else
        uv run python -c "import json,sys;print(json.load(open('${REPO_ROOT}/ffmpeg-pin.json'))['${field}'])"
    fi
}

# Return the ordered list of candidate download URLs for the pinned FFmpeg.
# mirror_urls (if present) come first; url is always the final fallback.
# Each URL is printed on its own line.
_pin_download_urls() {
    local snippet='
import json, sys
pin = json.load(open(sys.argv[1]))
urls = pin.get("mirror_urls", [])
primary = pin["url"]
seen = list(dict.fromkeys(u for u in urls if u))
if primary not in seen:
    seen.append(primary)
for u in seen:
    print(u)
'
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "${snippet}" "${REPO_ROOT}/ffmpeg-pin.json"
    else
        uv run python -c "${snippet}" "${REPO_ROOT}/ffmpeg-pin.json"
    fi
}

# Download + SHA-256-verify the pinned static FFmpeg, extract ffmpeg/ffprobe to a
# scratch dir, and export TLM_BUNDLE_FFMPEG_DIR so the .spec embeds the PINNED
# binary rather than falling back to whatever ffmpeg is on the build host's PATH.
# The pin is a linux/amd64 build, so this only runs on linux/amd64; on any other
# host the .spec's PATH fallback is used (spec-validity build only).
_stage_pinned_ffmpeg() {
    if [ "${OS}" != "linux" ] || [ "${ARCH}" != "amd64" ]; then
        info "not linux/amd64: skipping pinned-FFmpeg staging (.spec uses PATH ffmpeg)"
        return 0
    fi
    [ -f "${REPO_ROOT}/ffmpeg-pin.json" ] || err "ffmpeg-pin.json missing; cannot stage the pinned FFmpeg"
    command -v curl >/dev/null 2>&1 || err "curl required to fetch the pinned FFmpeg"
    command -v sha256sum >/dev/null 2>&1 || err "sha256sum required to verify the pinned FFmpeg"

    local sha tarball stage
    sha="$(_pin_field sha256)"
    case "${sha}" in
        PLACEHOLDER*) err "ffmpeg-pin.json sha256 is a placeholder; fill it before releasing" ;;
    esac
    stage="$(mktemp -d)"
    tarball="${stage}/ffmpeg.tar.xz"

    # Try each candidate URL in order (owner mirror first, upstream fallback
    # last). The sha256 from ffmpeg-pin.json is the trust anchor regardless of
    # source: a mismatch means the tarball is corrupt or tampered, not that we
    # should accept it. We only fail hard when every URL is exhausted.
    local fetched=0
    while IFS= read -r candidate_url; do
        # Skip the owner-mirror placeholder so a pre-release build that hasn't
        # populated the mirror yet falls through to the upstream URL.
        case "${candidate_url}" in
            *REPLACE_ME*)
                info "skipping owner-mirror placeholder: ${candidate_url}"
                continue
                ;;
        esac
        info "fetching pinned FFmpeg from ${candidate_url}"

        # The Gitea generic package registry requires authentication (returns
        # HTTP 401 without a token).  When fetching from the Gitea host
        # (git.2cbn.com) add an Authorization header if GITEA_TOKEN is set.
        # Non-Gitea URLs (the upstream GitHub fallback) are fetched without
        # credentials.  The sha256 in ffmpeg-pin.json is the trust anchor
        # regardless of which source URL delivered the bytes.
        local _fetch_ok=0
        case "${candidate_url}" in
            *git.2cbn.com*)
                if [ -n "${GITEA_TOKEN:-}" ]; then
                    curl --fail --location --silent --show-error \
                        -H "Authorization: token ${GITEA_TOKEN}" \
                        --output "${tarball}" "${candidate_url}" \
                        && _fetch_ok=1 || true
                else
                    info "GITEA_TOKEN not set; cannot fetch from Gitea mirror; skipping"
                fi
                ;;
            *)
                curl --fail --location --silent --show-error \
                    --output "${tarball}" "${candidate_url}" \
                    && _fetch_ok=1 || true
                ;;
        esac

        if [ "${_fetch_ok}" -eq 1 ]; then
            if echo "${sha}  ${tarball}" | sha256sum --check - 2>/dev/null; then
                fetched=1
                break
            else
                info "SHA-256 mismatch from ${candidate_url}; trying next URL"
                rm -f "${tarball}"
            fi
        else
            info "download failed for ${candidate_url}; trying next URL"
        fi
    done < <(_pin_download_urls)
    [ "${fetched}" -eq 1 ] \
        || err "pinned FFmpeg could not be fetched from any mirror (sha256=${sha})"
    # Extract just the two binaries to a flat dir the .spec consumes.
    tar -C "${stage}" -xf "${tarball}"
    local ff fp
    ff="$(find "${stage}" -type f -name ffmpeg | head -1)"
    fp="$(find "${stage}" -type f -name ffprobe | head -1)"
    [ -n "${ff}" ] || err "pinned tarball did not contain an ffmpeg binary"
    mkdir -p "${stage}/bin"
    cp "${ff}" "${stage}/bin/ffmpeg"; chmod +x "${stage}/bin/ffmpeg"
    [ -n "${fp}" ] && { cp "${fp}" "${stage}/bin/ffprobe"; chmod +x "${stage}/bin/ffprobe"; }
    export TLM_BUNDLE_FFMPEG_DIR="${stage}/bin"
    _FFMPEG_STAGE="${stage}"
    info "staged pinned FFmpeg ($(_pin_field version)) at ${TLM_BUNDLE_FFMPEG_DIR}"
}

[ -f "${SPEC}" ] || err "spec not found at ${SPEC}"

cd "${REPO_ROOT}"

# --- Resolve identifiers ----------------------------------------------------
# Version comes from the package itself (single source of truth), not a literal.
VERSION="$(uv run python -c 'import timelapse_manager as t; print(t.__version__)')" \
    || err "could not resolve the application version"

case "$(uname -s)" in
    Linux)  OS="linux" ;;
    Darwin) OS="macos" ;;
    *)      OS="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac
case "$(uname -m)" in
    x86_64|amd64) ARCH="amd64" ;;
    arm64|aarch64) ARCH="arm64" ;;
    *) ARCH="$(uname -m)" ;;
esac

BUNDLE_NAME="timelapse-manager-${VERSION}-${OS}-${ARCH}"
BUNDLE_DIR="${DIST_DIR}/${BUNDLE_NAME}"
TARBALL="${DIST_DIR}/${BUNDLE_NAME}.tar.gz"

if [ "${OS}" != "linux" ] || [ "${ARCH}" != "amd64" ]; then
    info "WARNING: building on ${OS}/${ARCH}, not linux/amd64."
    info "         This validates the .spec only; it is NOT the released artifact."
fi

# --- Generate build metadata ------------------------------------------------
# Write a small module carrying the short commit SHA and the UTC build date so a
# packaged build can report exactly what it was frozen from. version.py imports
# this defensively and falls back to "unknown" when it is absent (a dev checkout
# never has it). Robust if git is unavailable: a failed rev-parse yields
# "unknown" rather than aborting the release (set -e is on, so the `|| echo`
# guard is load-bearing).
info "generating build metadata module"
BUILD_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(date -u +%Y-%m-%d)"
cat > "${REPO_ROOT}/src/timelapse_manager/_build_info.py" <<EOF
"""Generated at build time by packaging/release.sh. Do not edit or commit."""

BUILD_SHA = "${BUILD_SHA}"
BUILD_DATE = "${BUILD_DATE}"
EOF
info "build metadata: sha=${BUILD_SHA} date=${BUILD_DATE}"

# --- Stage the pinned FFmpeg (linux/amd64 only) -----------------------------
# _FFMPEG_STAGE is declared and cleaned by the consolidated `cleanup` trap above;
# _stage_pinned_ffmpeg sets it when it stages a temp dir on linux/amd64.
_stage_pinned_ffmpeg

# --- Freeze -----------------------------------------------------------------
info "freezing ${BUNDLE_NAME} with PyInstaller"
rm -rf "${BUILD_DIR}/timelapse-manager" "${DIST_DIR}/timelapse-manager" "${BUNDLE_DIR}"
uv run pyinstaller --noconfirm --distpath "${DIST_DIR}" --workpath "${BUILD_DIR}" "${SPEC}"

# PyInstaller emits dist/timelapse-manager (the EXE name from the spec). Move it
# to the versioned, os/arch-qualified directory that we tar and release.
if [ -d "${DIST_DIR}/timelapse-manager" ]; then
    rm -rf "${BUNDLE_DIR}"
    mv "${DIST_DIR}/timelapse-manager" "${BUNDLE_DIR}"
fi
[ -x "${BUNDLE_DIR}/timelapse-manager" ] \
    || err "frozen executable missing at ${BUNDLE_DIR}/timelapse-manager"

# Lay the service installer + systemd unit into the bundle so a user who
# extracts the tarball can run ./packaging/install.sh directly (the install
# script and the release CI's systemd test both expect this layout).
info "bundling packaging/ (installer + systemd unit)"
cp -R "${REPO_ROOT}/packaging" "${BUNDLE_DIR}/packaging"

# --- Package ----------------------------------------------------------------
info "packaging ${TARBALL}"
tar -C "${DIST_DIR}" -czf "${TARBALL}" "${BUNDLE_NAME}"
info "wrote ${TARBALL}"

# --- Local smoke test -------------------------------------------------------
# Extract the tarball to a scratch dir, start the frozen exe, and confirm the
# health route answers -- proving the bundle runs without the source tree. This
# is a best-effort local check; the authoritative clean-host smoke (no Python /
# system FFmpeg on PATH) runs in the release CI on linux/amd64.
if [ "${SKIP_SMOKE}" -eq 1 ]; then
    info "smoke test skipped (--skip-smoke)"
    exit 0
fi

# Ask the kernel for two free loopback ports so the smoke never collides with a
# fixed port a previous (or unrelated) process is holding -- the failure mode
# that used to wedge a release for ~16 minutes. Both sockets are held open until
# both ports are read, so the kernel cannot hand the same port out twice (a
# collision would make the HTTP and HTTPS listeners fight over one port). python3
# falls back to `uv run`, mirroring _pin_field, so this works on the clean
# release host either way. Prints the two ports space-separated on one line.
_free_ports() {
    local snippet='import socket
socks=[]
for _ in range(2):
    s=socket.socket(); s.bind(("127.0.0.1",0)); socks.append(s)
print(" ".join(str(s.getsockname()[1]) for s in socks))
for s in socks: s.close()'
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "${snippet}"
    else
        uv run python -c "${snippet}"
    fi
}

info "smoke testing the packaged bundle"
SMOKE_DIR="$(mktemp -d)"
tar -C "${SMOKE_DIR}" -xzf "${TARBALL}"
EXE="${SMOKE_DIR}/${BUNDLE_NAME}/timelapse-manager"

read -r HTTP_PORT HTTPS_PORT < <(_free_ports) \
    || err "could not allocate free smoke ports"
[ -n "${HTTP_PORT}" ] && [ -n "${HTTPS_PORT}" ] \
    || err "could not allocate free smoke ports"

# Isolated, relocatable state so the smoke run never touches developer state.
export TLM_PATHS__DATA_DIR="${SMOKE_DIR}/state"
export TLM_DATABASE__URL="sqlite:///${SMOKE_DIR}/state/timelapse.db"
export TLM_SERVER__HTTP_PORT="${HTTP_PORT}"
export TLM_SERVER__HTTPS_PORT="${HTTPS_PORT}"
mkdir -p "${SMOKE_DIR}/state"

# `setsid` runs the bundle in its own process group so `cleanup` can signal the
# whole group (the PyInstaller bootloader plus the real app child) on any exit.
setsid "${EXE}" run &
APP_PID=$!

ok=0
for _ in $(seq 1 30); do
    if curl --fail --silent --insecure "https://127.0.0.1:${HTTPS_PORT}/healthz" >/dev/null 2>&1 \
        || curl --fail --silent "http://127.0.0.1:${HTTP_PORT}/healthz" >/dev/null 2>&1; then
        ok=1
        break
    fi
    # Stop early if the bundle died, rather than waiting out the full budget.
    kill -0 "${APP_PID}" 2>/dev/null || break
    sleep 1
done

if [ "${ok}" -eq 1 ]; then
    info "smoke test PASSED: /healthz answered from the frozen bundle"
else
    err "smoke test FAILED: /healthz did not answer within 30s"
fi
