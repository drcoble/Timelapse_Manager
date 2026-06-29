#!/usr/bin/env bash
#
# Install Timelapse Manager as a systemd service from an extracted bundle.
#
# Run this from inside an extracted release bundle directory (the one containing
# the `timelapse-manager` executable), as root:
#
#   sudo ./packaging/install.sh
#
# It is idempotent: re-running upgrades the installed files and the unit in
# place without recreating the service account or destroying existing state.
#
# What it does:
#   * creates a dedicated, unprivileged, no-login service user + group
#   * creates the install, state, and config directories with correct ownership
#   * copies the bundle to the install dir
#   * renders the systemd unit template with the resolved paths/user
#   * enables + starts the service
#
# No secrets are written. A runtime key file, if needed, is provided out of band
# (e.g. mounted/placed under the config dir) and never created here.

set -euo pipefail

# --- Configuration (override via environment before invoking) ---------------
SERVICE_NAME="timelapse-manager"
SERVICE_USER="${TLM_SERVICE_USER:-timelapse}"
SERVICE_GROUP="${TLM_SERVICE_GROUP:-timelapse}"
INSTALL_DIR="${TLM_INSTALL_DIR:-/opt/timelapse-manager}"
STATE_DIR="${TLM_STATE_DIR:-/var/lib/timelapse-manager}"
CONFIG_DIR="${TLM_CONFIG_DIR:-/etc/timelapse-manager}"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}.service"

# Resolve the bundle root (the directory this script lives under, one level up).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_TEMPLATE="${SCRIPT_DIR}/systemd/${SERVICE_NAME}.service"
EXECUTABLE="${BUNDLE_DIR}/${SERVICE_NAME}"

err() { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

# --- Preconditions ----------------------------------------------------------
[ "$(id -u)" -eq 0 ] || err "must run as root (try: sudo $0)"
command -v systemctl >/dev/null 2>&1 || err "systemctl not found; this installer targets systemd hosts"
[ -f "${UNIT_TEMPLATE}" ] || err "unit template not found at ${UNIT_TEMPLATE}"
[ -x "${EXECUTABLE}" ] || err "bundled executable not found or not executable at ${EXECUTABLE}"

# --- Service account (idempotent) ------------------------------------------
if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
    info "creating group ${SERVICE_GROUP}"
    groupadd --system "${SERVICE_GROUP}"
else
    info "group ${SERVICE_GROUP} already exists"
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    info "creating service user ${SERVICE_USER}"
    useradd --system \
        --gid "${SERVICE_GROUP}" \
        --home-dir "${STATE_DIR}" \
        --no-create-home \
        --shell /usr/sbin/nologin \
        "${SERVICE_USER}"
else
    info "service user ${SERVICE_USER} already exists"
fi

# --- Directories (idempotent) ----------------------------------------------
info "creating directories"
install -d -m 0755 "${INSTALL_DIR}"
# State holds the database, frames, generated cert, and local token; keep it
# private to the service account.
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${STATE_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${CONFIG_DIR}"

# --- Install the bundle -----------------------------------------------------
info "installing bundle to ${INSTALL_DIR}"
# Copy bundle contents (everything except this packaging tree) into the install
# dir. Use a trailing slash to copy contents, and preserve modes.
cp -a "${BUNDLE_DIR}/." "${INSTALL_DIR}/"
# The install dir is owned by root and read-only to the service; the service
# only needs to execute, not modify, its own program files.
chown -R root:root "${INSTALL_DIR}"
chmod 0755 "${INSTALL_DIR}/${SERVICE_NAME}"

# --- Render + install the unit ---------------------------------------------
info "rendering systemd unit to ${UNIT_DEST}"
tmp_unit="$(mktemp)"
trap 'rm -f "${tmp_unit}"' EXIT
sed \
    -e "s|@INSTALL_DIR@|${INSTALL_DIR}|g" \
    -e "s|@STATE_DIR@|${STATE_DIR}|g" \
    -e "s|@CONFIG_DIR@|${CONFIG_DIR}|g" \
    -e "s|@SERVICE_USER@|${SERVICE_USER}|g" \
    -e "s|@SERVICE_GROUP@|${SERVICE_GROUP}|g" \
    "${UNIT_TEMPLATE}" > "${tmp_unit}"
install -m 0644 "${tmp_unit}" "${UNIT_DEST}"

# --- Enable + start ---------------------------------------------------------
info "reloading systemd and enabling the service"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

info "done. Check status with: systemctl status ${SERVICE_NAME}"
info "logs: journalctl -u ${SERVICE_NAME} -f"
