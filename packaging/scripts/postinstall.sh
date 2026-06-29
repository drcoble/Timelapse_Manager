#!/bin/sh
# Post-install: create the service account, migrate the database, and enable the
# service. Idempotent so an upgrade re-runs it safely. Mirrors install.sh.
set -e

SERVICE_USER=timelapse
STATE_DIR=/var/lib/timelapse-manager
EXE=/opt/timelapse-manager/timelapse-manager

# Dedicated, unprivileged, no-login service account.
if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_USER"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    nologin_shell=/usr/sbin/nologin
    [ -x "$nologin_shell" ] || nologin_shell=/sbin/nologin
    useradd --system --gid "$SERVICE_USER" --home-dir "$STATE_DIR" \
        --no-create-home --shell "$nologin_shell" "$SERVICE_USER"
fi

# The packaged dirs exist; make sure ownership is right (the bundle is root-owned
# and read-only; only the state/config dirs are service-writable).
chown -R "$SERVICE_USER":"$SERVICE_USER" "$STATE_DIR" /etc/timelapse-manager
chmod 0750 "$STATE_DIR" /etc/timelapse-manager

# Apply database migrations to head before first start. Run as the service user
# so the SQLite file is created with the right ownership.
if [ -x "$EXE" ]; then
    su -s /bin/sh -c "TLM_PATHS__DATA_DIR=$STATE_DIR \
        TLM_DATABASE__URL=sqlite:///$STATE_DIR/timelapse.db \
        $EXE migrate" "$SERVICE_USER" || true
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
    systemctl enable timelapse-manager.service || true
    systemctl restart timelapse-manager.service || true
fi

exit 0
