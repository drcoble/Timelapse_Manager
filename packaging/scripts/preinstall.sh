#!/bin/sh
# Pre-install: create the service account before the package's files (and the
# state/config directories owned by it) are laid down, so rpm no longer warns
# "user timelapse does not exist - using root". postinstall.sh repeats the same
# idempotent block, which also covers an upgrade from a package without this
# script.
#
# Arguments: deb preinst gets "install" or "upgrade <old-version>"; rpm %pre gets
# 1 (install) or 2+ (upgrade). The step is the same for all.
set -e

# A fixed PATH, as in postinstall.sh: a root maintainer script should not depend
# on the caller's PATH.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

SERVICE_USER=timelapse
SERVICE_GROUP=timelapse
STATE_DIR=/var/lib/timelapse-manager

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

exit 0
