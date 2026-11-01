#!/bin/sh
# Post-install: create the service account, migrate the database, and enable the
# service. Idempotent so an upgrade re-runs it safely. Mirrors install.sh.
#
# Arguments: deb postinst gets "configure <old-version>" (and the abort-* forms);
# rpm %post gets 1 (install) or 2+ (upgrade). The steps are the same for all.
set -e

# A fixed PATH: GNU find refuses -execdir when PATH holds a relative entry, and
# a root maintainer script should not depend on the caller's PATH anyway.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

SERVICE_USER=timelapse
SERVICE_GROUP=timelapse
STATE_DIR=/var/lib/timelapse-manager
# Configuration is root-owned, group-readable by the service: the service never
# writes to it, so a compromised service account cannot rewrite its own config.
CONFIG_OWNER=root
CONFIG_DIR=/etc/timelapse-manager
EXE=/opt/timelapse-manager/timelapse-manager
UNIT=timelapse-manager.service
# Present only while systemd is the running init (sd_booted(3)).
SYSTEMD_RUN_DIR=/run/systemd/system

# Dedicated, unprivileged, no-login service account.
if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_GROUP"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    nologin_shell=/usr/sbin/nologin
    [ -x "$nologin_shell" ] || nologin_shell=/sbin/nologin
    useradd --system --gid "$SERVICE_GROUP" --home-dir "$STATE_DIR" \
        --no-create-home --shell "$nologin_shell" "$SERVICE_USER"
fi

# The packaged dirs exist; make sure ownership is right (the bundle is root-owned
# and read-only; only the state/config dirs are service-writable). Only entries
# with the wrong owner or group are touched, so an upgrade does not walk-and-
# rewrite every captured frame. -execdir runs chown from inside each entry's
# directory and -h changes a symlink itself, so a link the service account
# planted in its own tree cannot hand it ownership of a file elsewhere.
find "$STATE_DIR" \
    \( ! -user "$SERVICE_USER" -o ! -group "$SERVICE_GROUP" \) \
    -execdir chown -h "$SERVICE_USER":"$SERVICE_GROUP" {} +
# The config directory (and anything an earlier version left owned by the service
# account in it) is root:<service group>, so the service can read but not write.
find "$CONFIG_DIR" \
    \( ! -user "$CONFIG_OWNER" -o ! -group "$SERVICE_GROUP" \) \
    -execdir chown -h "$CONFIG_OWNER":"$SERVICE_GROUP" {} +
chmod 0750 "$STATE_DIR" "$CONFIG_DIR"

# Apply database migrations to head before first start. Run as the service user
# so the SQLite file (and the pre-migration snapshot beside it) is created with
# the right ownership.
migrate_failed=0
if [ -x "$EXE" ]; then
    if ! su -s /bin/sh -c "TLM_PATHS__DATA_DIR=$STATE_DIR \
        TLM_DATABASE__URL=sqlite:///$STATE_DIR/timelapse.db \
        $EXE migrate" "$SERVICE_USER"; then
        migrate_failed=1
    fi
fi

# Exit status of a startup refused because the encryption key does not match the
# stored secrets (#67 S8). The unit's RestartPreventExitStatus=78 stops systemd
# retrying it, so the service stays "failed" until an operator acts (#243).
KEY_REFUSAL_STATUS=78

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

# After a restart, report why the service is not up, if it is not (#243, #273).
# Never fails the package script: the files are in place whatever the service does.
report_service_health() {
    wait_for_service_start "$UNIT" && return 0
    main_status=$(systemctl show -p ExecMainStatus "$UNIT" 2>/dev/null \
        | sed -n 's/^ExecMainStatus=//p' || true)
    if [ "$state" = failed ] && [ "$main_status" = "$KEY_REFUSAL_STATUS" ]; then
        cat >&2 <<MSG
************************************************************************
timelapse-manager: STARTUP REFUSED (exit $KEY_REFUSAL_STATUS): the encryption key
does not match the secrets stored in the database. systemd will not retry.
Check the state, then either restore the original key file or reset:
  sudo -u $SERVICE_USER env TLM_PATHS__DATA_DIR=$STATE_DIR \\
      TLM_DATABASE__URL=sqlite:///$STATE_DIR/timelapse.db $EXE secrets status
  (restore the original key file into the state directory, or)
  sudo -u $SERVICE_USER env TLM_PATHS__DATA_DIR=$STATE_DIR \\
      TLM_DATABASE__URL=sqlite:///$STATE_DIR/timelapse.db \\
      $EXE secrets reset --confirm
Then: systemctl start $UNIT
WARNING: 'secrets reset' also clears the LDAP bind password, so make sure a
local break-glass admin exists before using it.
************************************************************************
MSG
    else
        echo "timelapse-manager: the service is not running after the upgrade" \
            "(state: ${state:-unknown}, last exit status: ${main_status:-unknown});" \
            "see: journalctl -u $UNIT" >&2
    fi
}

if command -v systemctl >/dev/null 2>&1; then
    if [ -d "$SYSTEMD_RUN_DIR" ]; then
        systemctl daemon-reload || true
        systemctl enable "$UNIT" || true
        # A failed migration must not be followed by a restart: the new code
        # would start against a schema it does not expect (and retry the
        # migration in a loop). Leave the service as it is for the operator.
        if [ "$migrate_failed" -eq 0 ]; then
            systemctl restart "$UNIT" || true
            report_service_health
        fi
    else
        # systemd is not the running init (an image or rootfs build, a chroot):
        # there is no manager to reload, restart or ask. Enabling only writes the
        # wants symlink, so it works offline; the service starts at first boot.
        systemctl enable "$UNIT" || true
    fi
fi

if [ "$migrate_failed" -ne 0 ]; then
    cat >&2 <<MSG
************************************************************************
timelapse-manager: DATABASE MIGRATION FAILED. The service was NOT restarted.
  Database:  $STATE_DIR/timelapse.db
  Snapshot:  $STATE_DIR/timelapse.db.pre-* (taken before the migration, if
             the database was behind)
Read the error above, fix the cause, then re-run the migration:
  sudo -u $SERVICE_USER env TLM_PATHS__DATA_DIR=$STATE_DIR \\
      TLM_DATABASE__URL=sqlite:///$STATE_DIR/timelapse.db $EXE migrate
To roll back instead, stop the service and restore the snapshot:
  sudo env TLM_PATHS__DATA_DIR=$STATE_DIR \\
      TLM_DATABASE__URL=sqlite:///$STATE_DIR/timelapse.db \\
      $EXE db restore $STATE_DIR/timelapse.db.pre-<revision>-<time>
************************************************************************
MSG
    exit 1
fi

exit 0
