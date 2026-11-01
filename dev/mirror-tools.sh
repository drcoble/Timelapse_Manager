#!/usr/bin/env bash
# Seed the generic-registry mirror of the build tools the release
# workflow runs (nfpm, trivy). Owner-run, once per tool version.
#
# Usage: GITEA_TOKEN=<package:write token> TLM_REGISTRY_URL=<base url> \
#        TLM_REGISTRY_OWNER=<owner> dev/mirror-tools.sh [--dry-run] [tool ...]
#
# With no tool names it mirrors every tool in packaging/tool-pins.json. For each
# tool it downloads the upstream release asset, checks its sha256 against the
# pin AND against the upstream project's published checksums file, then uploads
# it to the generic registry (package ci-tools) at the pinned mirror_path under
# <TLM_REGISTRY_URL>/api/packages/<TLM_REGISTRY_OWNER>/generic/. A
# version that is already mirrored is left alone: the registry is immutable.
# --dry-run does everything except the upload.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PINS="${HERE}/../packaging/tool-pins.json"

dry_run=0
tools=()
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        -*) echo "error: unknown option: $arg" >&2; exit 2 ;;
        *) tools+=("$arg") ;;
    esac
done
if [ -z "${GITEA_TOKEN:-}" ] && [ "$dry_run" -eq 0 ]; then
    echo "error: GITEA_TOKEN (package:write) is not set" >&2
    exit 2
fi
for var in TLM_REGISTRY_URL TLM_REGISTRY_OWNER; do
    [ -n "${!var:-}" ] || { echo "error: ${var} is not set" >&2; exit 2; }
done
REGISTRY_BASE="${TLM_REGISTRY_URL%/}/api/packages/${TLM_REGISTRY_OWNER}/generic"

field() {
    python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]][sys.argv[3]])' "$PINS" "$1" "$2"
}
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

if [ "${#tools[@]}" -eq 0 ]; then
    # Read line by line: macOS ships bash 3.2, which has no mapfile.
    while IFS= read -r name; do
        tools+=("$name")
    done < <(python3 -c 'import json, sys; print("\n".join(k for k in json.load(open(sys.argv[1])) if not k.startswith("_")))' "$PINS")
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

for tool in "${tools[@]}"; do
    version="$(field "$tool" version)"
    file="$(field "$tool" file)"
    pin_sha="$(field "$tool" sha256)"
    upstream="$(field "$tool" upstream_url)"
    checksums="$(field "$tool" upstream_checksums_url)"
    mirror="${REGISTRY_BASE}/$(field "$tool" mirror_path)"

    echo "==> ${tool} ${version}"
    if [ "$dry_run" -eq 0 ]; then
        code="$(curl --silent --output /dev/null --write-out '%{http_code}' \
            -H "Authorization: token ${GITEA_TOKEN}" "$mirror")"
        if [ "$code" = "200" ]; then
            echo "    already mirrored at ${mirror}; leaving it (versions are immutable)"
            continue
        fi
    fi

    curl --fail --silent --show-error --location --output "${work}/${file}" "$upstream"
    actual="$(sha256_of "${work}/${file}")"
    [ "$actual" = "$pin_sha" ] || { echo "error: ${file} sha256 ${actual} does not match the pin ${pin_sha}" >&2; exit 1; }

    curl --fail --silent --show-error --location --output "${work}/checksums.txt" "$checksums"
    published="$(awk -v f="$file" '$2 == f || $2 == "*" f {print $1}' "${work}/checksums.txt" | head -n 1)"
    [ "$published" = "$pin_sha" ] || { echo "error: the upstream checksums file lists '${published}' for ${file}, not the pin ${pin_sha}" >&2; exit 1; }
    echo "    sha256 ${pin_sha} matches the pin and the upstream checksums file"

    if [ "$dry_run" -eq 1 ]; then
        echo "    dry run: would upload to ${mirror}"
        continue
    fi
    curl --fail --silent --show-error -H "Authorization: token ${GITEA_TOKEN}" \
        --upload-file "${work}/${file}" "$mirror"
    echo "    uploaded ${mirror}"
done
