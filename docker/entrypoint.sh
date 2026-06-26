#!/bin/sh
# Container entrypoint: bring the database schema to head, then hand off to the
# main process (the CMD) as PID 1.
#
# A packaged deployment has no separate "migrate first" step, so the container
# must initialize its own schema on startup. `timelapse migrate` resolves the
# bundled migrations and the configured database URL from the environment
# (TLM_DATABASE__URL / TLM_PATHS__*), is idempotent on an already-migrated
# database, and runs as the unprivileged `app` user (so the DB it may create is
# owned correctly). `exec "$@"` then replaces this shell with the CMD, keeping
# uvicorn as PID 1 for correct signal handling under the container runtime.
set -e

timelapse migrate

exec "$@"
