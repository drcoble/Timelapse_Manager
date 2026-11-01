#!/usr/bin/env bash
# Small helper for a Gitea package registry, used by the release
# workflow and by packaging/unpublish.sh.
#
#   registry.sh put    <package> <version> <file> [remote-name]   generic upload
#   registry.sh get    <package> <version> <remote-name> <dest>   generic download
#   registry.sh exists <type> <package> <version>                 0 yes, 1 no, 2 error
#   registry.sh delete <type> <package> <version>                 delete a version
#
# Environment:
#   GITEA_TOKEN         required; a token with package read/write (delete needs write)
#   TLM_REGISTRY_URL    required; base URL of the Gitea instance, for example
#                       https://gitea.example.com
#   TLM_REGISTRY_OWNER  required; the user or organisation that owns the packages
#
# The generic registry needs a token even for downloads. Versions are immutable:
# a second upload of the same file fails with HTTP 409 and is never retried here.
set -euo pipefail

die() { echo "registry.sh: $*" >&2; exit 2; }

[ -n "${TLM_REGISTRY_URL:-}" ] || die "TLM_REGISTRY_URL is not set"
[ -n "${TLM_REGISTRY_OWNER:-}" ] || die "TLM_REGISTRY_OWNER is not set"
BASE="${TLM_REGISTRY_URL%/}"
OWNER="${TLM_REGISTRY_OWNER}"

[ $# -ge 1 ] || die "usage: registry.sh put|get|exists|delete ..."
[ -n "${GITEA_TOKEN:-}" ] || die "GITEA_TOKEN is not set"

# Percent-encode a path segment (a deb or rpm version holds '~' or '+').
enc() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="~"))' "$1"; }

auth=(-H "Authorization: token ${GITEA_TOKEN}")
cmd="$1"
shift

case "$cmd" in
    put)
        [ $# -ge 3 ] || die "usage: registry.sh put <package> <version> <file> [remote-name]"
        pkg="$1"; ver="$2"; file="$3"; name="${4:-$(basename "$3")}"
        [ -f "$file" ] || die "no such file: $file"
        curl --fail --silent --show-error "${auth[@]}" --upload-file "$file" \
            "${BASE}/api/packages/${OWNER}/generic/$(enc "$pkg")/$(enc "$ver")/$(enc "$name")"
        ;;
    get)
        [ $# -eq 4 ] || die "usage: registry.sh get <package> <version> <remote-name> <dest>"
        curl --fail --silent --show-error --location "${auth[@]}" --output "$4" \
            "${BASE}/api/packages/${OWNER}/generic/$(enc "$1")/$(enc "$2")/$(enc "$3")"
        ;;
    exists)
        [ $# -eq 3 ] || die "usage: registry.sh exists <type> <package> <version>"
        code="$(curl --silent --output /dev/null --write-out '%{http_code}' "${auth[@]}" \
            "${BASE}/api/v1/packages/${OWNER}/$(enc "$1")/$(enc "$2")/$(enc "$3")")" || die "request failed"
        case "$code" in
            200) exit 0 ;;
            404) exit 1 ;;
            *) echo "registry.sh: unexpected HTTP ${code} probing $1/$2/$3" >&2; exit 2 ;;
        esac
        ;;
    delete)
        [ $# -eq 3 ] || die "usage: registry.sh delete <type> <package> <version>"
        code="$(curl --silent --output /dev/null --write-out '%{http_code}' -X DELETE "${auth[@]}" \
            "${BASE}/api/v1/packages/${OWNER}/$(enc "$1")/$(enc "$2")/$(enc "$3")")" || die "request failed"
        case "$code" in
            204|200) echo "deleted $1/$2/$3" ;;
            404) echo "not present: $1/$2/$3" ;;
            *) echo "registry.sh: unexpected HTTP ${code} deleting $1/$2/$3" >&2; exit 2 ;;
        esac
        ;;
    *) die "unknown command: $cmd" ;;
esac
