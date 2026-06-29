#!/usr/bin/env bash
# Build native Linux installers (.deb and .rpm) from the frozen PyInstaller
# bundle produced by release.sh, using nfpm (one declarative spec -> both
# formats, no rpmbuild needed).
#
# Prerequisites:
#   * The frozen bundle dir exists: dist/timelapse-manager-<version>-linux-<arch>/
#     (run packaging/release.sh first).
#   * nfpm is on PATH (a single static binary: https://nfpm.goreleaser.com).
#
# Usage: packaging/build-linux-packages.sh [version] [arch]
#   version defaults to the package version; arch defaults to amd64.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-0.1.0}"
ARCH="${2:-amd64}"
export VERSION ARCH

BUNDLE_DIR="dist/timelapse-manager-${VERSION}-linux-${ARCH}"
if [ ! -x "${BUNDLE_DIR}/timelapse-manager" ]; then
    echo "error: frozen bundle not found at ${BUNDLE_DIR} (run release.sh first)" >&2
    exit 1
fi
if ! command -v nfpm >/dev/null 2>&1; then
    echo "error: nfpm not found on PATH (https://nfpm.goreleaser.com)" >&2
    exit 1
fi

# Render the tokenised systemd unit into a concrete one for the package, using
# the same fixed install layout install.sh uses.
mkdir -p packaging/build
sed \
    -e 's#@INSTALL_DIR@#/opt/timelapse-manager#g' \
    -e 's#@STATE_DIR@#/var/lib/timelapse-manager#g' \
    -e 's#@CONFIG_DIR@#/etc/timelapse-manager#g' \
    -e 's#@SERVICE_USER@#timelapse#g' \
    -e 's#@SERVICE_GROUP@#timelapse#g' \
    packaging/systemd/timelapse-manager.service \
    > packaging/build/timelapse-manager.service

# Render a concrete nfpm config (substitute VERSION/ARCH ourselves rather than
# relying on nfpm's env expansion, which does not cover the contents globs).
sed -e "s#\${VERSION}#${VERSION}#g" -e "s#\${ARCH}#${ARCH}#g" \
    packaging/nfpm.yaml > packaging/build/nfpm.yaml

mkdir -p dist
echo "Building .deb and .rpm for timelapse-manager ${VERSION} (${ARCH})..."
nfpm package --config packaging/build/nfpm.yaml --packager deb --target dist/
nfpm package --config packaging/build/nfpm.yaml --packager rpm --target dist/

echo "Done. Artifacts:"
ls -1 dist/*.deb dist/*.rpm 2>/dev/null || true
