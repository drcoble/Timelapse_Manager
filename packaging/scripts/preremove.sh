#!/bin/sh
# Pre-remove: stop and disable the service before its files are removed.
set -e

if command -v systemctl >/dev/null 2>&1; then
    systemctl stop timelapse-manager.service || true
    systemctl disable timelapse-manager.service || true
fi

exit 0
