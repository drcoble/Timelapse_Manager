#!/usr/bin/env bash
# Build the Timelapse Manager Proxmox CT template (Debian 13, .tar.zst) from a
# release .deb, using Docker only: no root and no privileged mode.
#
# Usage:
#   packaging/lxc/build-ct-template.sh --deb PATH --version VERSION \
#       [--out-dir DIR] [--pve-template FILE --pve-sha512 HEX] [--base-image REF]
#
#   --deb           the timelapse-manager .deb to install (required)
#   --version       PEP 440 version used in the output name (required)
#   --out-dir       where the .tar.zst and .sha512 land (default: dist)
#   --pve-template  Option A (preferred): the official Proxmox
#                   debian-13-standard_*_amd64.tar.zst, mirrored and pinned.
#                   Verified against --pve-sha512 (from Proxmox's aplinfo index)
#                   before it is imported.
#   --pve-sha512    expected sha512 of --pve-template (required with it)
#   --base-image    Option B override; default is the digest-pinned
#                   debian:trixie below, with the CT packages installed on top.
#
# Output: <out-dir>/debian-13-timelapse-manager_<version>_amd64.tar.zst and a
# matching .sha512 (the checksum PVE's download-url form expects).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# debian:trixie index digest, resolved 2026-10-29. Refresh deliberately.
DEFAULT_BASE="debian:trixie@sha256:2dc419c6d7fe3dad0a2f9fd469c34cc90b0f9ba642d8fde837fe3d7d34a0e7f1"

deb="" version="" out_dir="dist" pve_template="" pve_sha512="" base_image=""
while [ $# -gt 0 ]; do
    case "$1" in
        --deb) deb="${2:?--deb needs a value}"; shift 2 ;;
        --version) version="${2:?--version needs a value}"; shift 2 ;;
        --out-dir) out_dir="${2:?--out-dir needs a value}"; shift 2 ;;
        --pve-template) pve_template="${2:?--pve-template needs a value}"; shift 2 ;;
        --pve-sha512) pve_sha512="${2:?--pve-sha512 needs a value}"; shift 2 ;;
        --base-image) base_image="${2:?--base-image needs a value}"; shift 2 ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ -n "$deb" ] && [ -f "$deb" ] || { echo "error: --deb must name an existing file" >&2; exit 2; }
[ -n "$version" ] || { echo "error: --version is required" >&2; exit 2; }
case "$version" in
    *[!0-9A-Za-z.+_-]*) echo "error: unsafe --version: $version" >&2; exit 2 ;;
esac
if [ -n "$pve_template" ] && { [ -z "$pve_sha512" ] || [ ! -f "$pve_template" ]; }; then
    echo "error: --pve-template needs an existing file and --pve-sha512" >&2
    exit 2
fi
if [ -n "$pve_template" ] && [ -n "$base_image" ]; then
    echo "error: --pve-template and --base-image are mutually exclusive" >&2
    exit 2
fi
for tool in docker zstd python3 sha512sum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool not found on PATH" >&2; exit 1; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$out_dir"

install_base_packages=0
if [ -n "$pve_template" ]; then
    # Option A: verify the pinned sha512, then import as a local base image.
    actual="$(sha512sum "$pve_template" | cut -d' ' -f1)"
    if [ "$actual" != "$pve_sha512" ]; then
        echo "error: PVE template sha512 mismatch (expected $pve_sha512, got $actual)" >&2
        exit 1
    fi
    base_image="pve-debian13:${pve_sha512:0:12}"
    zstd -dc "$pve_template" | docker import - "$base_image" >/dev/null
    echo "==> base: imported PVE template as $base_image"
else
    base_image="${base_image:-$DEFAULT_BASE}"
    install_base_packages=1
    case "$base_image" in
        *@sha256:*) ;;
        *) echo "error: --base-image must be digest-pinned (@sha256:...)" >&2; exit 2 ;;
    esac
    echo "==> base: $base_image (CT packages installed on top)"
fi

cp "$HERE/Dockerfile" "$HERE/timelapse-manager.env" "$work/"
cp "$deb" "$work/timelapse-manager.deb"

rootfs="$work/rootfs.tar"
docker buildx build --platform linux/amd64 \
    --build-arg "BASE_IMAGE=$base_image" \
    --build-arg "INSTALL_BASE_PACKAGES=$install_base_packages" \
    --output "type=tar,dest=$rootfs" \
    "$work"

# buildx's tar output has no container init layer, so .dockerenv should never
# appear; strip it defensively if a different exporter ever adds it. GNU tar only.
# List to a variable first: with pipefail, grep -q exiting early SIGPIPEs tar.
rootfs_list="$(tar -tf "$rootfs")"
if grep -qxE '(\./)?\.dockerenv' <<<"$rootfs_list"; then
    tar --delete -f "$rootfs" ./.dockerenv .dockerenv 2>/dev/null || true
fi

python3 "$HERE/check_rootfs.py" "$rootfs"

name="debian-13-timelapse-manager_${version}_amd64.tar.zst"
zstd -19 -T0 -q -f "$rootfs" -o "$out_dir/$name"

# Re-check what actually ships: the compressed artifact, streamed.
zstd -dc "$out_dir/$name" | python3 "$HERE/check_rootfs.py" -

(cd "$out_dir" && sha512sum "$name" > "$name.sha512")
echo "==> built $out_dir/$name"
