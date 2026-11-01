#!/bin/sh
# Pre-remove: stop and disable the service before its files are removed.
#
# Only on a real removal. An upgrade also runs the old package's pre-remove
# script, and stopping and disabling the service there left it down (and, on
# rpm, disabled) after every upgrade.
#   deb prerm: "remove" on removal; "upgrade", "failed-upgrade" and
#              "deconfigure" otherwise.
#   rpm %preun: 0 on removal; 1 or more when another version stays installed.
# No argument (a manual run, or a format that passes none) counts as removal.
set -e

case "${1:-}" in
    remove | purge | 0 | "") ;;
    *) exit 0 ;;
esac

if command -v systemctl >/dev/null 2>&1; then
    systemctl stop timelapse-manager.service || true
    systemctl disable timelapse-manager.service || true
fi

exit 0
