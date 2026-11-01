#!/usr/bin/env bash
# Fetch a pinned build tool (nfpm, trivy) from the generic registry
# mirror of a Gitea instance, verify its sha256 against packaging/tool-pins.json, and install the
# binary into a directory.
#
# Usage: packaging/ci/fetch-tool.sh <tool> <dest-dir>
#
# Environment (all required): GITEA_TOKEN (the registry needs a token even to
# read), TLM_REGISTRY_URL (base URL of the Gitea instance) and
# TLM_REGISTRY_OWNER (the user or organisation that owns the packages).
#
# There is deliberately no fallback to the upstream URL: the mirror is the
# immutable copy, the sha256 pin is the trust anchor, and a missing mirror is an
# owner action (dev/mirror-tools.sh), not something to paper over in CI.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PINS="${TOOL_PINS_FILE:-${HERE}/../tool-pins.json}"

tool="${1:?usage: $0 <tool> <dest-dir>}"
dest="${2:?usage: $0 <tool> <dest-dir>}"
[ -n "${GITEA_TOKEN:-}" ] || { echo "error: GITEA_TOKEN is not set" >&2; exit 2; }
[ -n "${TLM_REGISTRY_URL:-}" ] || { echo "error: TLM_REGISTRY_URL is not set" >&2; exit 2; }
[ -n "${TLM_REGISTRY_OWNER:-}" ] || { echo "error: TLM_REGISTRY_OWNER is not set" >&2; exit 2; }

field() {
    python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]][sys.argv[3]])' "$PINS" "$tool" "$1"
}
version="$(field version)"
sha="$(field sha256)"
url="${TLM_REGISTRY_URL%/}/api/packages/${TLM_REGISTRY_OWNER}/generic/$(field mirror_path)"
binary="$(field binary)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> fetching ${tool} ${version} from ${url}"
if ! curl --fail --silent --show-error --location \
    -H "Authorization: token ${GITEA_TOKEN}" --output "${work}/tool.tar.gz" "$url"; then
    echo "error: the ${tool} ${version} mirror is missing or unreadable." >&2
    echo "       Seed it once with: GITEA_TOKEN=... TLM_REGISTRY_URL=... TLM_REGISTRY_OWNER=... dev/mirror-tools.sh ${tool}" >&2
    exit 1
fi

actual="$(sha256sum "${work}/tool.tar.gz" | cut -d' ' -f1)"
if [ "$actual" != "$sha" ]; then
    echo "error: ${tool} sha256 mismatch (pinned ${sha}, got ${actual})" >&2
    exit 1
fi

mkdir -p "$dest" "${work}/x"
tar -C "${work}/x" -xzf "${work}/tool.tar.gz"
found="$(find "${work}/x" -type f -name "$binary" | head -n 1)"
[ -n "$found" ] || { echo "error: no ${binary} binary in the ${tool} tarball" >&2; exit 1; }
install -m 0755 "$found" "${dest}/${binary}"
echo "==> installed ${dest}/${binary} (sha256 ${sha} verified)"
