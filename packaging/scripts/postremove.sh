#!/bin/sh
# Post-remove: reload systemd after the unit is gone. Captured frames, the
# database, and config under /var/lib and /etc are intentionally preserved across
# removal so a reinstall keeps the operator's data; purge them manually if wanted.
set -e

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
fi

exit 0
