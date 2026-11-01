#!/usr/bin/env bash
# Remove a published Timelapse Manager release from the Gitea registries so the
# same tag can be built and published again. OWNER-RUN ONLY: the release
# workflow never calls this, and a published version is immutable to it.
#
# Usage: GITEA_TOKEN=<token with package:write> TLM_REGISTRY_URL=<base url> \
#        TLM_REGISTRY_OWNER=<owner> packaging/unpublish.sh <pep440-version> [--yes] [--latest]
#
# Without --yes it only prints what it would delete. It removes, in this order:
#   * the container image tag            <registry>/<owner>/timelapse-manager:<version>
#   * the RPM registry package version   (version mapped by packaging/pkgversion.py, release 1)
#   * the Debian registry package version
#   * the generic registry version       timelapse-manager/<version> (tarball, packages,
#                                        CT template, SBOMs, SHA256SUMS)
# The container `latest` tag is a moving pointer and is left alone unless you
# pass --latest; re-point it by publishing the next final release. Deleting a
# registry version cannot be undone.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGISTRY="${HERE}/ci/registry.sh"

version="" yes=0 latest=0
for arg in "$@"; do
    case "$arg" in
        --yes) yes=1 ;;
        --latest) latest=1 ;;
        -*) echo "error: unknown option: $arg" >&2; exit 2 ;;
        *) [ -z "$version" ] || { echo "error: one version only" >&2; exit 2; }; version="$arg" ;;
    esac
done
[ -n "$version" ] || { echo "usage: $0 <pep440-version> [--yes] [--latest]" >&2; exit 2; }
[ -n "${GITEA_TOKEN:-}" ] || { echo "error: GITEA_TOKEN is not set" >&2; exit 2; }
[ -n "${TLM_REGISTRY_URL:-}" ] || { echo "error: TLM_REGISTRY_URL is not set" >&2; exit 2; }
[ -n "${TLM_REGISTRY_OWNER:-}" ] || { echo "error: TLM_REGISTRY_OWNER is not set" >&2; exit 2; }

full="$(python3 "${HERE}/pkgversion.py" "$version")"
pkg_version="${full}-1"

targets=(
    "container timelapse-manager ${version}"
    "rpm timelapse-manager ${pkg_version}"
    "debian timelapse-manager ${pkg_version}"
    "generic timelapse-manager ${version}"
)
[ "$latest" -eq 0 ] || targets=("container timelapse-manager latest" "${targets[@]}")

echo "Registry: ${TLM_REGISTRY_URL}  owner: ${TLM_REGISTRY_OWNER}"
for target in "${targets[@]}"; do
    rc=0
    # shellcheck disable=SC2086  # the three words are type, package and version
    "$REGISTRY" exists $target || rc=$?
    case "$rc" in
        0) state="present" ;;
        1) state="absent" ;;
        *) echo "error: could not probe ${target}" >&2; exit 2 ;;
    esac
    echo "  ${target}: ${state}"
done

if [ "$yes" -ne 1 ]; then
    echo "Dry run. Re-run with --yes to delete the versions marked present."
    exit 0
fi
for target in "${targets[@]}"; do
    # shellcheck disable=SC2086
    "$REGISTRY" delete $target
done
echo "Done. The tag can be published again."
