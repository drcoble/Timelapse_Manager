#!/bin/sh
# Post-remove: reload systemd after the unit is gone, and on an upgrade restart
# the service so it runs the new files. Captured frames, the database, and
# config under /var/lib and /etc are intentionally preserved across removal so a
# reinstall keeps the operator's data; purge them manually if wanted.
#
#   deb postrm: "upgrade" on an upgrade; "remove", "purge" and others otherwise.
#   rpm %postun: 0 on removal; 1 or more on an upgrade.
# try-restart only restarts a service that is running, so a stopped service
# stays stopped.
set -e

restart=no
case "${1:-}" in
    upgrade) restart=yes ;;
    "" | *[!0-9]*) ;;
    *) [ "$1" -ge 1 ] && restart=yes ;;
esac

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
    if [ "$restart" = yes ]; then
        systemctl try-restart timelapse-manager.service || true
    fi
fi

exit 0
