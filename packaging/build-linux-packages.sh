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
# Usage: packaging/build-linux-packages.sh <pep440-version> [arch]
#   version is required: the PEP 440 release version (for example 0.2.0.dev10),
#   which names the bundle dir. packaging/pkgversion.py maps it to the deb/rpm
#   version (0.2.0~dev10) so a pre-release sorts below its final. arch defaults
#   to amd64.
#
# Optional environment:
#   TLM_RPM_SIGNING_KEY_FILE   path to a PGP private key; when set, the .rpm is
#                              signed (the passphrase, if any, goes in
#                              NFPM_RPM_PASSPHRASE). Unset: the .rpm is unsigned.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:?usage: $0 <pep440-version> [arch]}"
ARCH="${2:-amd64}"

PKG_VERSION="$(python3 packaging/pkgversion.py "${VERSION}" --field version)"
PKG_PRERELEASE="$(python3 packaging/pkgversion.py "${VERSION}" --field prerelease)"
PKG_METADATA="$(python3 packaging/pkgversion.py "${VERSION}" --field metadata)"
PKG_FULL="$(python3 packaging/pkgversion.py "${VERSION}")"

BUNDLE_DIR="dist/timelapse-manager-${VERSION}-linux-${ARCH}"
if [ ! -x "${BUNDLE_DIR}/timelapse-manager" ]; then
    echo "error: frozen bundle not found at ${BUNDLE_DIR} (run release.sh first)" >&2
    exit 1
fi
if ! command -v nfpm >/dev/null 2>&1; then
    echo "error: nfpm not found on PATH (https://nfpm.goreleaser.com)" >&2
    exit 1
fi

RPM_KEY_FILE="${TLM_RPM_SIGNING_KEY_FILE:-}"
if [ -n "${RPM_KEY_FILE}" ] && [ ! -r "${RPM_KEY_FILE}" ]; then
    echo "error: TLM_RPM_SIGNING_KEY_FILE is set but not readable: ${RPM_KEY_FILE}" >&2
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
# A umask of 077 (common in CI) would otherwise leak into the packaged mode.
chmod 0644 packaging/build/timelapse-manager.service

# Stage the bundle without its packaging/ tree: release.sh copies it in for the
# tarball's install.sh, which a package install has no use for (nfpm cannot
# exclude paths). Hard links where possible keep this cheap.
BUNDLE_STAGE="packaging/build/bundle"
rm -rf "${BUNDLE_STAGE}"
cp -al "${BUNDLE_DIR}" "${BUNDLE_STAGE}" 2>/dev/null || cp -R "${BUNDLE_DIR}" "${BUNDLE_STAGE}"
rm -rf "${BUNDLE_STAGE}/packaging"

# Render a concrete nfpm config (substitute the tokens ourselves rather than
# relying on nfpm's env expansion, which does not cover the contents globs).
sed \
    -e "s#\${ARCH}#${ARCH}#g" \
    -e "s#\${PKG_VERSION}#${PKG_VERSION}#g" \
    -e "s#\${PKG_PRERELEASE}#${PKG_PRERELEASE}#g" \
    -e "s#\${PKG_METADATA}#${PKG_METADATA}#g" \
    -e "s#\${BUNDLE_STAGE}#./${BUNDLE_STAGE}#g" \
    -e "s#\${RPM_KEY_FILE}#${RPM_KEY_FILE}#g" \
    packaging/nfpm.yaml > packaging/build/nfpm.yaml

mkdir -p dist
echo "Building .deb and .rpm for timelapse-manager ${VERSION} as ${PKG_FULL}-1 (${ARCH})..."
nfpm package --config packaging/build/nfpm.yaml --packager deb --target dist/
nfpm package --config packaging/build/nfpm.yaml --packager rpm --target dist/

echo "Done. Artifacts:"
ls -1 dist/*.deb dist/*.rpm 2>/dev/null || true
