#!/usr/bin/env bash
#
# Produce integrity material for a set of release artifacts:
#   * SHA256SUMS         -- a SHA-256 manifest over every artifact
#   * SHA256SUMS.asc     -- a detached GPG signature over that manifest
#
# Signing the manifest (rather than each file) covers every listed artifact
# transitively: verify the signature once, then verify each file against the
# manifest.
#
#   ./dev/sign_release.sh [--key <gpg-key-id>] [--out <dir>] <artifact> [<artifact>...]
#
# If --key is omitted, GPG's default signing key is used. In release CI the key
# is provided from a protected secret and imported into the keyring before this
# runs; the private key never lives in the repository or in an image layer.
#
# Verification (publish these alongside the release, with the public key):
#
#   gpg --verify SHA256SUMS.asc SHA256SUMS      # 1. trust the manifest
#   sha256sum --check SHA256SUMS                # 2. trust each artifact
#
# On macOS, `sha256sum` may be absent; this script falls back to `shasum -a 256`
# (and `gsha256sum` if present) so it can be exercised locally.

set -euo pipefail

err() { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

KEY=""
OUT_DIR=""
ARTIFACTS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --key) KEY="${2:-}"; shift 2 ;;
        --out) OUT_DIR="${2:-}"; shift 2 ;;
        -*) err "unknown option: $1" ;;
        *) ARTIFACTS+=("$1"); shift ;;
    esac
done

[ "${#ARTIFACTS[@]}" -ge 1 ] || err "no artifacts given"
command -v gpg >/dev/null 2>&1 || err "gpg not found"

# Pick a SHA-256 tool that emits the `<hash>  <name>` format `--check` expects.
if command -v sha256sum >/dev/null 2>&1; then
    SHA_CMD=(sha256sum)
elif command -v gsha256sum >/dev/null 2>&1; then
    SHA_CMD=(gsha256sum)
elif command -v shasum >/dev/null 2>&1; then
    SHA_CMD=(shasum -a 256)
else
    err "no sha256 tool found (need sha256sum, gsha256sum, or shasum)"
fi

# All artifacts must share a directory so the manifest uses bare filenames and
# `sha256sum --check` resolves them relative to the manifest.
FIRST_DIR="$(cd "$(dirname "${ARTIFACTS[0]}")" && pwd)"
[ -z "${OUT_DIR}" ] && OUT_DIR="${FIRST_DIR}"
MANIFEST="${OUT_DIR}/SHA256SUMS"
SIGNATURE="${MANIFEST}.asc"

: > "${MANIFEST}"
for artifact in "${ARTIFACTS[@]}"; do
    [ -f "${artifact}" ] || err "artifact not found: ${artifact}"
    dir="$(cd "$(dirname "${artifact}")" && pwd)"
    [ "${dir}" = "${FIRST_DIR}" ] \
        || err "all artifacts must be in one directory (${FIRST_DIR}); got ${dir}"
    base="$(basename "${artifact}")"
    ( cd "${dir}" && "${SHA_CMD[@]}" "${base}" ) >> "${MANIFEST}"
done
info "wrote manifest ${MANIFEST} (${#ARTIFACTS[@]} artifacts)"

# --- Detached signature -----------------------------------------------------
sign_args=(--armor --detach-sign --output "${SIGNATURE}")
[ -n "${KEY}" ] && sign_args+=(--local-user "${KEY}")
info "signing manifest"
gpg --yes "${sign_args[@]}" "${MANIFEST}"
info "wrote signature ${SIGNATURE}"

# --- Self-verify (fail loudly if the material does not verify) --------------
info "verifying signature"
gpg --verify "${SIGNATURE}" "${MANIFEST}" \
    || err "signature verification failed"
info "verifying artifacts against manifest"
( cd "${OUT_DIR}" && "${SHA_CMD[@]}" --check "$(basename "${MANIFEST}")" ) \
    || err "checksum verification failed"

info "release integrity material verified"
printf '\nVerify with:\n  gpg --verify %s %s\n  (cd %s && %s --check %s)\n' \
    "$(basename "${SIGNATURE}")" "$(basename "${MANIFEST}")" \
    "${OUT_DIR}" "${SHA_CMD[*]}" "$(basename "${MANIFEST}")"
