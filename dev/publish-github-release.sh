#!/usr/bin/env bash
# Promote a release built and published on the maintainer's Gitea instance to a
# GitHub Release and a ghcr.io image. Nothing is rebuilt: the files attached to
# the GitHub Release are byte-for-byte the files the build published, and the
# image is copied by digest.
#
# Usage: dev/publish-github-release.sh [--dry-run] [--assets-dir DIR] [--notes FILE]
#                                      [--no-package-contents] <tag>
#
#   <tag>            the release tag, for example v0.2.0
#   --dry-run        do everything except the GitHub and ghcr writes: download,
#                    checksum and scan the assets, then print the plan
#   --assets-dir DIR use the files already in DIR (SHA256SUMS included) instead
#                    of downloading them from the registry
#   --notes FILE     release notes (default: docs/releases/<version>.md)
#   --no-package-contents
#                    skip every .deb/.rpm introspection (file lists and
#                    contents); prints a warning per package
#
# Exit codes: 0 ok (or a clean dry run), 2 a usage error, a failed check or private
# details found, 3 an asset could not be read (a corrupt package, a missing tool).
#
# Steps:
#   1. Download every file of the version from the Gitea generic registry and run
#      `sha256sum --check SHA256SUMS`; any mismatch or missing file stops here.
#   2. Scan the extracted tarball, the .deb/.rpm file lists and contents, the CT
#      template's file list, the SBOMs and the release notes for private details
#      (the public-tree guard's deny-list). Any finding stops here.
#   3. Create the GitHub Release as a draft with the notes as its body, upload the
#      assets with SHA256SUMS last, then publish it (which starts the
#      verify-only workflow).
#   4. Copy the image, by digest, to ghcr.io/<owner>/<name>:<version> (and :latest
#      for a final version) and check that the digests match.
#
# Environment:
#   TLM_REGISTRY_URL      Gitea base URL                     (unless --assets-dir)
#   TLM_REGISTRY_OWNER    Gitea package owner                (unless --assets-dir)
#   GITEA_TOKEN or GITEA_TOKEN_FILE
#                         read access to the Gitea packages  (unless --assets-dir)
#   GITHUB_TOKEN_FILE     file holding a GitHub token with contents:write
#   GHCR_TOKEN_FILE       file holding a token with write:packages for ghcr.io
#   GITHUB_REPO           default drcoble/Timelapse_Manager
#   GHCR_IMAGE            default ghcr.io/drcoble/timelapse_manager
#   SOURCE_IMAGE          the built image in the Gitea registry, without a tag
#                         (default <registry host>/<owner>/timelapse-manager)
#   TLM_PUBLIC_GUARD_LIST the private deny-list (default
#                         .gitea/public-guard/deny.txt); required, the script
#                         refuses to run without it
#   PUBLISH_SCAN_ALLOW    optional file of allow rules for the payload scan, one
#                         'path regex<TAB>line regex' per line
# Tokens are read from files and never printed or placed on a command line.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
GUARD="${ROOT}/tests/unit/test_public_tree.py"
GUARD_LIST="${TLM_PUBLIC_GUARD_LIST:-${ROOT}/.gitea/public-guard/deny.txt}"

GITHUB_REPO="${GITHUB_REPO:-drcoble/Timelapse_Manager}"
GHCR_IMAGE="${GHCR_IMAGE:-ghcr.io/drcoble/timelapse_manager}"
PACKAGE_NAME="timelapse-manager"

die() { echo "publish-github-release.sh: $*" >&2; exit 2; }
step() { echo "==> $*"; }
plan() { echo "PLAN: $*"; }

dry_run=0 assets_dir="" notes="" pkg_contents=1 tag=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1; shift ;;
        --assets-dir) [ $# -ge 2 ] || die "--assets-dir needs a directory"; assets_dir="$2"; shift 2 ;;
        --notes) [ $# -ge 2 ] || die "--notes needs a file"; notes="$2"; shift 2 ;;
        --no-package-contents) pkg_contents=0; shift ;;
        -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) [ -z "${tag}" ] || die "one tag only"; tag="$1"; shift ;;
    esac
done
[ -n "${tag}" ] || die "usage: $0 [--dry-run] [--assets-dir DIR] [--notes FILE] <tag>"
[[ "${tag}" =~ ^v[0-9]+(\.[0-9]+)*((rc|\.dev|\.post)[0-9]+)*$ ]] \
    || die "the tag must look like v0.2.0 (got '${tag}')"
version="${tag#v}"
final=0
[[ "${version}" =~ ^[0-9]+(\.[0-9]+)*$ ]] && final=1
[ -n "${notes}" ] || notes="${ROOT}/docs/releases/${version}.md"
[ -f "${notes}" ] || die "release notes not found: ${notes}"
[ -f "${GUARD_LIST}" ] || die "the private deny-list is missing: ${GUARD_LIST} (set TLM_PUBLIC_GUARD_LIST)"
if [ -n "${assets_dir}" ]; then
    [ -d "${assets_dir}" ] || die "--assets-dir is not a directory: ${assets_dir}"
fi

# --- credentials (files only) -------------------------------------------------
read_token() { # <ENV NAME of the file variable>
    local var="$1" file="${!1:-}"
    [ -n "${file}" ] || die "${var} is not set"
    [ -f "${file}" ] || die "${var} does not point at a file"
    tr -d '\r\n' < "${file}"
}
if [ "${dry_run}" -eq 0 ]; then
    [ -n "${GITHUB_TOKEN_FILE:-}" ] || die "GITHUB_TOKEN_FILE is not set"
    [ -n "${GHCR_TOKEN_FILE:-}" ] || die "GHCR_TOKEN_FILE is not set"
    github_token="$(read_token GITHUB_TOKEN_FILE)"
    ghcr_token="$(read_token GHCR_TOKEN_FILE)"
fi
# The registry settings are needed to download, and (for the image copy) to
# authenticate against the source registry on a real run.
if [ -z "${assets_dir}" ] || [ "${dry_run}" -eq 0 ]; then
    [ -n "${TLM_REGISTRY_URL:-}" ] || [ -n "${assets_dir}" ] || die "TLM_REGISTRY_URL is not set"
    [ -n "${TLM_REGISTRY_OWNER:-}" ] || die "TLM_REGISTRY_OWNER is not set"
    if [ -z "${GITEA_TOKEN:-}" ]; then
        GITEA_TOKEN="$(read_token GITEA_TOKEN_FILE)"
    fi
    export GITEA_TOKEN TLM_REGISTRY_URL TLM_REGISTRY_OWNER
fi

# curl against the GitHub API with the token passed on stdin (not in argv).
gh_curl() {
    printf 'header = "Authorization: Bearer %s"\n' "${github_token}" \
        | curl --fail --silent --show-error --config - \
            -H "Accept: application/vnd.github+json" \
            -H "X-GitHub-Api-Version: 2022-11-28" "$@"
}

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
stage="${work}/assets"
mkdir -p "${stage}"

# --- 1. download and verify ---------------------------------------------------
if [ -n "${assets_dir}" ]; then
    step "using the assets in ${assets_dir}"
    cp -R "${assets_dir}/." "${stage}/"
else
    step "downloading SHA256SUMS for ${version}"
    "${ROOT}/packaging/ci/registry.sh" get "${PACKAGE_NAME}" "${version}" SHA256SUMS "${stage}/SHA256SUMS"
    while read -r _ name; do
        name="${name#\*}"
        [ -n "${name}" ] || continue
        step "downloading ${name}"
        "${ROOT}/packaging/ci/registry.sh" get "${PACKAGE_NAME}" "${version}" "${name}" "${stage}/${name}" </dev/null
    done < "${stage}/SHA256SUMS"
fi
[ -f "${stage}/SHA256SUMS" ] || die "SHA256SUMS is missing"

step "checking SHA256SUMS"
sha_check() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum --check "$@"; else shasum -a 256 --check "$@"; fi
}
(cd "${stage}" && sha_check SHA256SUMS) || die "checksum mismatch; nothing was published"

# Every file must be listed, and the expected set must be present.
for f in "${stage}"/*; do
    n="$(basename "${f}")"
    [ "${n}" = "SHA256SUMS" ] && continue
    awk -v n="${n}" '{ x = $2; sub(/^\*/, "", x); if (x == n) found = 1 } END { exit !found }' \
        "${stage}/SHA256SUMS" || die "${n} is not covered by SHA256SUMS"
done
need_glob() { compgen -G "${stage}/$1" >/dev/null || die "missing asset matching $1"; }
need_glob "timelapse-manager-${version}-linux-amd64.tar.gz"
need_glob "timelapse-manager_*_amd64.deb"
need_glob "timelapse-manager-*.x86_64.rpm"
need_glob "debian-13-timelapse-manager_${version}_amd64.tar.zst"
need_glob "debian-13-timelapse-manager_${version}_amd64.tar.zst.sha512"
need_glob "cyclonedx-bom.json"
need_glob "sbom-image.cdx.json"
need_glob "sbom-lxc.cdx.json"
need_glob "image-digest.txt"
need_glob "timelapse-manager-rpm-signing-key.asc"

# --- 2. scan for private details ---------------------------------------------
allow_args=(--list "${GUARD_LIST}")
[ -z "${PUBLISH_SCAN_ALLOW:-}" ] || allow_args+=(--allow "${PUBLISH_SCAN_ALLOW}")
scan_failed=0
guard() { python3 -I "${GUARD}" ${allow_args[@]+"${allow_args[@]}"} "$@" || scan_failed=1; }

step "scanning the release notes and the text assets"
mkdir -p "${work}/text"
cp "${notes}" "${work}/text/release-notes.md"
cp "${stage}"/*.json "${stage}/image-digest.txt" "${stage}"/*.asc "${work}/text/"
guard --scan "${work}/text"

# A reader tool failing (a corrupt asset, a missing tool) is its own error, exit
# code 3, never a finding. Findings are exit code 2 ("private details found").
read_into() { # <asset> <outfile> <command...>: the command's stdout goes to <outfile>
    local asset="$1" out="$2" err
    shift 2
    err="$("$@" 2>&1 >"${out}")" || {
        echo "publish-github-release.sh: could not read ${asset}: ${err:-$1 failed}" >&2
        exit 3
    }
}
names_guard() { # <listing file>
    python3 -I "${GUARD}" --list "${GUARD_LIST}" --names < "$1" || scan_failed=1
}

step "scanning the tarball"
mkdir -p "${work}/tarball"
read_into "the tarball" /dev/null \
    tar -xzf "${stage}/timelapse-manager-${version}-linux-amd64.tar.gz" -C "${work}/tarball"
guard --scan "${work}/tarball" --binary

step "scanning the CT template file list"
ct="debian-13-timelapse-manager_${version}_amd64.tar.zst"
read_into "${ct}" "${work}/ct.list" tar --zstd -tf "${stage}/${ct}"
names_guard "${work}/ct.list"

extract_deb() { # <deb> <dest>
    mkdir -p "$2/ar"
    (cd "$2/ar" && ar x "$1") || return 1
    for data in "$2"/ar/data.tar*; do
        [ -e "${data}" ] && tar -xf "${data}" -C "$2" && return 0
    done
    echo "no data archive in the package" >&2
    return 1
}
unpack_rpm() { # <rpm> <dest>
    if command -v rpm2cpio >/dev/null 2>&1 && command -v cpio >/dev/null 2>&1; then
        (cd "$2" && bash -c 'set -o pipefail; rpm2cpio "$1" | cpio -idm --quiet' _ "$1")
    elif command -v bsdtar >/dev/null 2>&1; then
        bsdtar -xf "$1" -C "$2"
    else
        echo "needs rpm2cpio and cpio, or bsdtar (or pass --no-package-contents)" >&2
        return 1
    fi
}

# --no-package-contents skips every package-introspection call, file lists too.
for deb in "${stage}"/timelapse-manager_*_amd64.deb; do
    name="$(basename "${deb}")"
    step "scanning ${name}"
    if [ "${pkg_contents}" -eq 1 ]; then
        if command -v dpkg-deb >/dev/null 2>&1; then
            read_into "${name}" "${work}/deb.list" dpkg-deb -c "${deb}"
            awk '{print $NF}' "${work}/deb.list" > "${work}/deb.names"
            names_guard "${work}/deb.names"
        fi
        d="${work}/deb-${name}"
        read_into "${name}" /dev/null extract_deb "${deb}" "${d}"
        guard --scan "${d}" --binary
    else
        echo "WARNING: contents of ${name} not scanned (--no-package-contents)" >&2
    fi
done
for rpm in "${stage}"/timelapse-manager-*.x86_64.rpm; do
    name="$(basename "${rpm}")"
    step "scanning ${name}"
    if [ "${pkg_contents}" -eq 1 ]; then
        if command -v rpm >/dev/null 2>&1; then
            read_into "${name}" "${work}/rpm.names" rpm -qlp "${rpm}"
            names_guard "${work}/rpm.names"
        fi
        d="${work}/rpm-${name}"
        mkdir -p "${d}"
        read_into "${name}" /dev/null unpack_rpm "${rpm}" "${d}"
        guard --scan "${d}" --binary
    else
        echo "WARNING: contents of ${name} not scanned (--no-package-contents)" >&2
    fi
done
[ "${scan_failed}" -eq 0 ] || die "private details found in the assets; nothing was published"
step "scans clean"

# --- 3/4. the plan, then the writes -------------------------------------------
# image-digest.txt is published as built, so it holds only the bare digest.
digest="$(tr -d '\r\n' < "${stage}/image-digest.txt")"
[[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "image-digest.txt must contain only a sha256:<digest> line"
# The source image in the Gitea registry (container packages share the host).
src_image="${SOURCE_IMAGE:-}"
if [ -z "${src_image}" ] && [ -n "${TLM_REGISTRY_URL:-}" ]; then
    src_image="${TLM_REGISTRY_URL#*://}"
    src_image="${src_image%%/*}/${TLM_REGISTRY_OWNER}/${PACKAGE_NAME}"
fi
[ -n "${src_image}" ] || src_image="<registry>/<owner>/${PACKAGE_NAME}"
digest_ref="${src_image}@${digest}"
tags=("${GHCR_IMAGE}:${version}")
[ "${final}" -eq 1 ] && tags+=("${GHCR_IMAGE}:latest")

assets=()
for f in "${stage}"/*; do
    n="$(basename "${f}")"
    [ "${n}" = "SHA256SUMS" ] || assets+=("${n}")
done
assets+=(SHA256SUMS)

prerelease=true
[ "${final}" -eq 1 ] && prerelease=false

plan "repository: ${GITHUB_REPO}, tag ${tag} (prerelease: ${prerelease})"
plan "create a draft release with ${notes#"${ROOT}"/} as its body"
for n in "${assets[@]}"; do plan "upload ${n}"; done
plan "publish the release"
for t in "${tags[@]}"; do plan "copy ${digest_ref} -> ${t}"; done
plan "verify that every ghcr tag resolves to ${digest}"

if [ "${dry_run}" -eq 1 ]; then
    step "dry run: nothing was written to GitHub or ghcr"
    exit 0
fi

# GitHub Release: refuse to touch an existing one.
if gh_curl -o /dev/null "https://api.github.com/repos/${GITHUB_REPO}/releases/tags/${tag}" 2>/dev/null; then
    die "a release for ${tag} already exists on ${GITHUB_REPO}; remove it by hand first"
fi
step "creating the draft release ${tag}"
body="$(python3 - "${tag}" "${prerelease}" "${notes}" <<'PY'
import json
import sys

tag, prerelease, notes = sys.argv[1:4]
with open(notes, encoding="utf-8") as handle:
    text = handle.read()
print(json.dumps({"tag_name": tag, "name": tag, "body": text, "draft": True,
                  "prerelease": prerelease == "true"}))
PY
)"
created="$(printf '%s' "${body}" | gh_curl -X POST --data @- "https://api.github.com/repos/${GITHUB_REPO}/releases")"
release_id="$(printf '%s' "${created}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
[ -n "${release_id}" ] || die "could not read the new release id"

for n in "${assets[@]}"; do
    step "uploading ${n}"
    gh_curl -X POST -H "Content-Type: application/octet-stream" --data-binary "@${stage}/${n}" \
        "https://uploads.github.com/repos/${GITHUB_REPO}/releases/${release_id}/assets?name=${n}" >/dev/null
done

step "publishing the release"
gh_curl -X PATCH --data '{"draft": false}' \
    "https://api.github.com/repos/${GITHUB_REPO}/releases/${release_id}" >/dev/null

# Image copy by digest.
step "copying the image by digest"
export DOCKER_CONFIG="${work}/docker"
mkdir -p "${DOCKER_CONFIG}"
src_host="${src_image%%/*}"
if command -v crane >/dev/null 2>&1; then
    printf '%s' "${GITEA_TOKEN}" \
        | crane auth login "${src_host}" -u "${TLM_REGISTRY_OWNER}" --password-stdin
    printf '%s' "${ghcr_token}" | crane auth login "${GHCR_IMAGE%%/*}" -u "${GITHUB_REPO%%/*}" --password-stdin
    for t in "${tags[@]}"; do crane copy "${digest_ref}" "${t}"; done
    for t in "${tags[@]}"; do
        [ "$(crane digest "${t}")" = "${digest}" ] || die "${t} does not resolve to ${digest}"
    done
elif command -v docker >/dev/null 2>&1; then
    printf '%s' "${GITEA_TOKEN}" \
        | docker login "${src_host}" -u "${TLM_REGISTRY_OWNER}" --password-stdin
    printf '%s' "${ghcr_token}" | docker login "${GHCR_IMAGE%%/*}" -u "${GITHUB_REPO%%/*}" --password-stdin
    tag_args=()
    for t in "${tags[@]}"; do tag_args+=(-t "${t}"); done
    docker buildx imagetools create "${tag_args[@]}" "${digest_ref}"
    for t in "${tags[@]}"; do
        got="$(docker buildx imagetools inspect "${t}" --format '{{json .Manifest.Digest}}' | tr -d '"')"
        [ "${got}" = "${digest}" ] || die "${t} does not resolve to ${digest}"
    done
else
    die "neither crane nor docker is installed; the release is published but the image was not copied"
fi
step "published ${tag}; make the ghcr package public and link it to the repository if this is its first release"
