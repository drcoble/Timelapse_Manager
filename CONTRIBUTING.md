# Contributing to Timelapse Manager

This guide covers everything you need to set up a working development environment,
run the quality gates, and contribute changes.

---

## Prerequisites

| Tool | Required | Notes |
|---|---|---|
| **Python ≥ 3.11** | Yes | 3.12 is the primary target |
| **uv** | Yes | Manages the virtualenv and all dependencies |
| **ffmpeg** | Recommended | Required by some tests and the encoder path |
| **mediamtx** | Optional | Only needed to run the RTSP mock camera |
| **Docker** | Optional | Only needed for the container workflow |

Install **uv** from [https://docs.astral.sh/uv/](https://docs.astral.sh/uv/).

---

## Quick start

```bash
# 1. Clone and enter the repository
git clone https://github.com/your-org/timelapse-manager.git
cd timelapse-manager

# 2. Install all dependencies (including dev tools) from the committed lockfile
make bootstrap

# 3. Apply database migrations (creates all core tables)
make migrate

# 4. Generate the self-signed TLS certificate for local HTTPS
make dev-cert

# 5. Start the app
make run
# Serving on https://localhost:8443

# 6. Verify it is healthy
curl -k https://localhost:8443/healthz
# {"app_version":"0.1.0","ffmpeg_version":"7.1","ffmpeg_path":"ffmpeg","db_status":"ok","alembic_revision":"002_create_core_schema"}
```

`make bootstrap` runs `uv sync`, which creates a virtualenv and installs
everything declared in `pyproject.toml` — including the dev tools — from the
committed `uv.lock`. No separate `pip install` step is needed.

`make migrate` applies all Alembic migrations to head. After a fresh clone (or
after pulling changes that add migrations), run it before starting the service.
The current migration creates all core tables: `camera`, `user`, `project`,
`frame`, `render_job`, `milestone`, `session`, `ldap_settings`,
`notification_settings`, and `event`.

`make dev-cert` writes `.dev-cert.pem` and `.dev-cert-key.pem` in the repo root
(both git-ignored). The certificate is valid for `localhost` and `127.0.0.1`.
The `make run` target generates it automatically if it is missing, so running
`make dev-cert` separately is optional. Use `curl -k` (or configure your browser
to trust the cert) because it is self-signed.

When `ffmpeg` is not installed, the `/healthz` response will include
`"ffmpeg_version": "unavailable"` — that is expected.

`make bootstrap` also installs the `timelapse` and `timelapse-daemon` console
scripts into the virtualenv. After bootstrap you can run them directly:

```bash
timelapse version           # print the application version
timelapse config show       # show the resolved configuration
timelapse migrate           # alternative to `make migrate`
timelapse-daemon            # run the service (plain HTTP, port 8080)
```

`make run` starts the service differently — it calls uvicorn directly with the
dev TLS cert, serving HTTPS on port 8443. Use `make run` for local browser
testing; use `timelapse-daemon` when you want the plain-HTTP service path (e.g.
behind a reverse proxy, or in the Docker container workflow).

---

## Common commands

| Target | What it does |
|---|---|
| `make help` | Print a summary of all targets |
| `make bootstrap` | Create the virtualenv and install deps from `uv.lock` |
| `make dev-cert` | Generate the self-signed dev TLS cert (skips if already present) |
| `make run` | Run the app locally over HTTPS at `https://localhost:8443` |
| `make test` | Run the test suite with pytest |
| `make lint` | Lint `src/` and `tests/` with ruff |
| `make typecheck` | Type-check `src/` with mypy |
| `make fmt` | Format `src/` and `tests/` with ruff |
| `make migrate` | Apply pending database migrations |
| `make mock-cameras` | Start the mock HTTP-snapshot and RTSP camera servers |
| `make check` | Run lint + typecheck + tests (the full pre-push gate) |

---

## Project layout

```
src/timelapse_manager/   # Application package
    app.py               # FastAPI application factory
    api/                 # REST API route handlers
    cameras/             # Camera adapter layer
    capture/             # Still-capture scheduling and execution
    cli/                 # Command-line interface
    config/              # Settings and configuration loading
    db/                  # Database session management (engine.py)
    encode/              # FFmpeg-based timelapse encoder
    security/            # TLS / authentication helpers
    service/             # Background service coordination
    storage/             # Frame and asset storage
    web/                 # Jinja2 templates and HTMX routes

alembic/                 # Database migration environment
    versions/            # Migration scripts (one file per revision)

tests/
    unit/                # Fast, isolated unit tests
    integration/         # Tests that hit the real database and app

dev/
    gen_dev_cert.py      # Self-signed cert generator
    mock_cameras/        # Mock camera servers for adapter development

docker/
    Dockerfile           # Multi-stage production image
    docker-compose.yml   # App + mock cameras
    compose.dev.yml      # Dev override (live source mount)
```

When building or changing the web interface, follow the
[UI / UX Style Guide](./docs/ui-style-guide.md) — design tokens, components,
overlay decisions, accessibility, and RBAC conventions for the Jinja2 + HTMX
layer.

---

## Running the test suite and quality gates

```bash
make test        # pytest only
make lint        # ruff check
make typecheck   # mypy
make check       # all three (what CI runs)
```

CI runs `make check` on Python 3.11 and 3.12 on every push and pull request.
Your branch must pass all three gates before merging.

CI also runs `ruff format --check` to verify formatting. Run `make fmt` locally
before pushing to avoid a format-only failure.

---

## Mock cameras

`make mock-cameras` starts three processes that together simulate a pair of IP
cameras, so camera adapters can be developed and tested without physical hardware:

| Endpoint | Protocol | URL |
|---|---|---|
| Generic JPEG snapshot | HTTP | `http://localhost:8555/snapshot.jpg` |
| VAPIX-shaped snapshot (Axis-style) | HTTP | `http://localhost:8555/axis-cgi/jpg/image.cgi` |
| Synthetic test-pattern stream | RTSP | `rtsp://localhost:8554/testsrc` |

The HTTP snapshot stub is pure Python standard library and starts without
additional dependencies. The RTSP stream requires both **ffmpeg** and
**mediamtx** on your `PATH` — if either is missing the launcher will exit with
a clear error.

Press `Ctrl-C` to stop all three processes. If any child exits unexpectedly,
the supervisor tears the rest down automatically.

All adapter and capture tests in the standard suite (`make test`) run against
the bundled HTTP-JPEG mock with no physical camera hardware required.

---

## Live camera tests

An optional test suite exercises the real adapter and capture pipeline against
physical hardware. These tests are marked `@pytest.mark.live` and are in
`tests/integration/test_live_cameras.py`.

**They are skipped automatically when the required environment variables are
not set.** `make test` and CI do not set these variables, so the live tests
never run in CI and will simply be skipped in a standard local `make test` run.

To run the live tests, set the environment variables for your camera and pass
`-m live`:

```bash
TLM_TEST_AXIS_HOST=CAMERA_HOST \
TLM_TEST_AXIS_USER=CAMERA_USER \
TLM_TEST_AXIS_PASS=CAMERA_PASS \
uv run pytest -m live
```

| Variable | Required | Description |
|---|---|---|
| `TLM_TEST_AXIS_HOST` | Yes | IP or hostname of the Axis/VAPIX camera |
| `TLM_TEST_AXIS_USER` | Yes | Camera username |
| `TLM_TEST_AXIS_PASS` | Yes | Camera password |
| `TLM_TEST_RTSP_URL` | No | Full RTSP URL; if unset, derived from `TLM_TEST_AXIS_HOST` |
| `TLM_TEST_DISCOVERY_RANGE` | No | CIDR or dash-range for the `scan_range` test (e.g. `192.168.1.0/24`) |

**Never hard-code real IP addresses or credentials** in the test files — always
use the environment variable helpers (`_require_env`) already in the test module.

---

## Database and migrations

Timelapse Manager uses **SQLite in WAL mode** for local state. Apply all pending
migrations before starting the app after pulling changes:

```bash
make migrate
# runs: uv run alembic upgrade head
```

A fresh `make migrate` creates the full core schema in one step: `camera`,
`user`, `project`, `frame`, `render_job`, `milestone`, `session`,
`ldap_settings`, `notification_settings`, and `event`, along with their
foreign keys, unique constraints, check constraints, and indexes.

To create a new migration after changing a SQLAlchemy model:

```bash
uv run alembic revision --autogenerate -m "describe_the_change"
```

Review the generated file in `alembic/versions/` — autogenerate is a starting
point and sometimes needs manual adjustment. Run `make migrate` to apply it, then
`make test` to confirm nothing regressed.

---

## Containers

The Docker Compose stack brings up the application and the mock cameras together:

```bash
docker compose -f docker/docker-compose.yml up
```

The application is served over plain HTTP inside the container and mapped to port
8080 on the host (`http://localhost:8080`). The mock-camera ports (8554 for RTSP,
8555 for HTTP snapshot) are also exposed.

For development with live source mounting, layer the dev override:

```bash
docker compose -f docker/docker-compose.yml -f docker/compose.dev.yml up
```

The target platform is `linux/amd64`.

---

## Before you commit or open a PR

**Run the full gate:**

```bash
make check
```

This runs lint, typecheck, and tests in sequence — the same jobs CI runs.

**Install pre-commit hooks** (one-time setup) to catch issues before each commit:

```bash
uv run pre-commit install
```

The hooks defined in `.pre-commit-config.yaml` run ruff (lint + format) and mypy
on every `git commit`. They mirror the CI gates so failures surface locally
before they reach the remote.
