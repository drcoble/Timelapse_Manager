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
# Configuration is root-owned and group-readable: the service reads it and never
# writes there. Entries an earlier install left owned by the service account are
# fixed too (-execdir/-h: never follow a link the service account planted).
install -d -o root -g "${SERVICE_GROUP}" -m 0750 "${CONFIG_DIR}"
find "${CONFIG_DIR}" \( ! -user root -o ! -group "${SERVICE_GROUP}" \) \
    -execdir chown -h root:"${SERVICE_GROUP}" {} +

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

# --- Health report ----------------------------------------------------------
# --- BEGIN health helpers (identical in packaging/install.sh and scripts/postinstall.sh)
# A test keeps the two copies byte-identical; edit both together (#266, #273).

# Last KEY=VALUE assignment of KEY in an env file, surrounding quotes removed.
# Prints nothing when the file or key is absent.
env_file_value() {
    [ -r "$1" ] || return 0
    sed -n "s/^[[:space:]]*$2=//p" "$1" | tail -n 1 \
        | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# Value of KEY under the top-level "server:" block of a YAML file (block style
# only; a flow-style mapping reads as absent). Prints nothing when absent.
yaml_server_value() {
    [ -r "$1" ] || return 0
    awk -v key="$2" -v q="'" '
        /^[^[:space:]#]/ { in_server = ($0 ~ /^server:[[:space:]]*(#.*)?$/); next }
        in_server && $0 ~ ("^[[:space:]]+" key ":") {
            v = $0
            sub("^[[:space:]]+" key ":[[:space:]]*", "", v)
            sub(/[[:space:]]+#.*$/, "", v)
            sub(/[[:space:]]+$/, "", v)
            sub(/^"/, "", v); sub(/"$/, "", v)
            sub("^" q, "", v); sub(q "$", "", v)
            print v
            exit
        }
    ' "$1"
}

# Loopback /healthz URL for the configured HTTP listener. Reads the installed
# config the way the service does: the env file (TLM_SERVER__*) overrides
# config.yml, which overrides the defaults (0.0.0.0, 8080). A wildcard bind is
# probed on its loopback (0.0.0.0 -> 127.0.0.1, :: -> ::1); a specific address
# is probed as is; anything unparseable falls back to 127.0.0.1.
health_url() {
    health_env="${CONFIG_DIR}/timelapse-manager.env"
    health_yaml="${CONFIG_DIR}/config.yml"
    health_port=$(env_file_value "$health_env" TLM_SERVER__HTTP_PORT)
    [ -n "$health_port" ] || health_port=$(yaml_server_value "$health_yaml" http_port)
    case $health_port in
        '' | *[!0-9]*) health_port=8080 ;;
    esac
    { [ "$health_port" -ge 1 ] && [ "$health_port" -le 65535 ]; } || health_port=8080
    health_bind=$(env_file_value "$health_env" TLM_SERVER__BIND_ADDRESS)
    [ -n "$health_bind" ] || health_bind=$(yaml_server_value "$health_yaml" bind_address)
    case $health_bind in
        '' | 0.0.0.0) health_host=127.0.0.1 ;;
        :: | 0:0:0:0:0:0:0:0) health_host='[::1]' ;;
        *[!0-9A-Za-z.:-]*) health_host=127.0.0.1 ;;
        *:*) health_host="[$health_bind]" ;;
        *) health_host=$health_bind ;;
    esac
    printf 'http://%s:%s/healthz\n' "$health_host" "$health_port"
}

# Whether anything answers HTTP at $1. Any response counts, a 503 from
# readiness or a 400 from the Host allowlist included (curl exits 0 unless it
# got no response at all); only a refused or timed-out connection does not.
health_answers() {
    curl -s -o /dev/null --noproxy '*' --connect-timeout 1 --max-time 2 "$1" \
        >/dev/null 2>&1
}

# Wait for unit $1 to come up, then set "state" to its last systemd state.
# Returns 0 as soon as /healthz answers (needs curl) or, failing that, if the
# unit is "active" at the end of the window. The unit is Type=simple, so systemd
# calls it "active" the moment the process forks, well before the application's
# startup checks: "active" alone never ends the wait early, only an answer does.
# "failed" ends it early too (a key refusal, exit 78, is not restarted). Any
# other crash shows as "activating" while RestartSec runs. Without curl this
# degrades to the systemd state check alone, the old behaviour.
# TLM_UPGRADE_HEALTH_POLLS x TLM_UPGRADE_HEALTH_INTERVAL bound the wait (15 s).
wait_for_service_start() {
    wait_polls=${TLM_UPGRADE_HEALTH_POLLS:-15}
    wait_interval=${TLM_UPGRADE_HEALTH_INTERVAL:-1}
    wait_url=""
    if command -v curl >/dev/null 2>&1; then
        wait_url=$(health_url)
    fi
    state=""
    while [ "$wait_polls" -gt 0 ]; do
        state=$(systemctl is-active "$1" 2>/dev/null || true)
        [ "$state" = failed ] && break
        if [ -n "$wait_url" ] && health_answers "$wait_url"; then
            return 0
        fi
        wait_polls=$((wait_polls - 1))
        [ "$wait_polls" -gt 0 ] && sleep "$wait_interval"
    done
    [ "$state" = active ] && return 0
    return 1
}
# --- END health helpers

# After the start, say so if the service is not up. Never fails the install: the
# files are in place whatever the service does.
report_service_health() {
    wait_for_service_start "${SERVICE_NAME}" && return 0
    main_status="$(systemctl show -p ExecMainStatus "${SERVICE_NAME}" 2>/dev/null \
        | sed -n 's/^ExecMainStatus=//p' || true)"
    printf 'warning: %s is not running after the install (state: %s, last exit status: %s); see: journalctl -u %s\n' \
        "${SERVICE_NAME}" "${state:-unknown}" "${main_status:-unknown}" "${SERVICE_NAME}" >&2
    if [ "${main_status}" = 78 ]; then
        printf 'warning: exit 78 means the encryption key does not match the stored secrets; run: sudo -u %s env TLM_PATHS__DATA_DIR=%s TLM_DATABASE__URL=sqlite:///%s/timelapse.db %s/%s secrets status\n' \
            "${SERVICE_USER}" "${STATE_DIR}" "${STATE_DIR}" "${INSTALL_DIR}" "${SERVICE_NAME}" >&2
    fi
}

# --- Enable + start ---------------------------------------------------------
info "reloading systemd and enabling the service"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
info "waiting for the service to answer"
report_service_health

info "done. Check status with: systemctl status ${SERVICE_NAME}"
info "logs: journalctl -u ${SERVICE_NAME} -f"
