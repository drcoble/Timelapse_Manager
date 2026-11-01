"""Command-line interface.

A thin argparse front end over the application. Some subcommands run fully
offline (``version``, ``config show``, ``migrate``, ``user create``,
``setup-code``, ``report``, ``db backup``, ``db restore``,
``frames find-orphans``); the offline
database commands open the configured database directly. The rest --
``system info`` and the ``camera``/``project`` control commands -- talk to a
running service over the loopback control surface, so the CLI and web UI operate
on one shared state.

Loopback contract
-----------------
The service-touching commands call the local API on ``127.0.0.1:<http_port>``
over plain ``http://`` and authenticate with ``Authorization: Bearer <token>``,
where the token is read from ``settings.paths.token_file``. The service creates
that file at startup; the CLI only reads it (it never creates one, which run as
root would leave a file the service cannot read), and ``token rotate`` replaces
it. The service accepts the token from loopback by default. Redirects are not
followed and TLS verification is never disabled; full HTTPS plus an
HTTP-to-HTTPS redirect is a later phase, and the CLI must reach the service
exactly as it is served now. Because every mutation flows through the same API
the web UI uses, the running supervisor is notified and capture/render react
without a restart.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import (
    ConfigError,
    Settings,
    load_settings,
    load_settings_with_provenance,
)
from ..security.token import (
    TokenFileError,
    local_token_path,
    metrics_token_path,
    read_token_file,
    rotate_token_file,
)
from ..version import get_app_version

# Loopback is the only valid connect target for the local control surface;
# the server may bind 0.0.0.0, which is not itself a connectable address.
_LOOPBACK_HOST = "127.0.0.1"

# Bound the loopback call so a wedged service cannot hang the CLI.
_REQUEST_TIMEOUT_SECONDS = 10.0

# ``db restore`` only needs to know whether anything holds the HTTP port.
_HEALTH_PROBE_TIMEOUT_SECONDS = 3.0

_EXIT_OK = 0
_EXIT_ERROR = 1
# The token file is missing or unreadable: the service has not created it yet,
# or the CLI runs as an account that cannot read it.
_EXIT_NO_TOKEN = 2

# Config-file discovery for the CLI when neither ``--config`` nor ``TLM_CONFIG``
# names one: the packaged service's file first, then a per-user file.
_SYSTEM_CONFIG_PATH = Path("/etc/timelapse-manager/config.yml")
_USER_CONFIG_RELATIVE = Path("timelapse-manager") / "config.yml"
_CONFIG_PATH_ENV = "TLM_CONFIG"


def _user_config_path() -> Path:
    """Return the per-user config path (``$XDG_CONFIG_HOME`` or ``~/.config``)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
    return root / _USER_CONFIG_RELATIVE


# The config file ``main`` resolved for this run, for error messages that tell the
# operator which file (if any) decided the state directory.
_config_in_use: str | None = None


def _config_hint() -> str:
    """Name the config file in use, or say that none was (and how to supply one)."""
    if _config_in_use is not None:
        return f" (config file in use: {_config_in_use})"
    return (
        " (no config file was found; if the service reads one, pass --config or "
        f"set {_CONFIG_PATH_ENV}, e.g. {_SYSTEM_CONFIG_PATH})"
    )


def _resolve_cli_config_path(flag: str | None) -> str | None:
    """Pick the config file the CLI loads.

    Precedence, highest first: the ``--config`` flag, the ``TLM_CONFIG``
    environment variable, the system file ``/etc/timelapse-manager/config.yml``,
    then the per-user file ``$XDG_CONFIG_HOME/timelapse-manager/config.yml``
    (``~/.config`` when unset). A discovered file is used only when it exists and
    this account can read it, so a root-only system file does not stop an
    ordinary user falling through. ``None`` means no file layer (the loader
    consults ``TLM_CONFIG`` itself, and otherwise applies defaults).
    """
    if flag is not None or os.environ.get(_CONFIG_PATH_ENV):
        return flag
    for candidate in (_SYSTEM_CONFIG_PATH, _user_config_path()):
        if candidate.is_file() and os.access(candidate, os.R_OK):
            return str(candidate)
    return None


# Matches the ``user:password@`` credential portion of a connection URL.
_URL_CREDENTIALS = re.compile(r"://[^/@]+@")

# Environment fallback for ``user create --password``. Reading the password from
# the environment instead of the command line keeps it out of the process
# argument list (which is world-readable via ``ps`` on a shared host).
_USER_PASSWORD_ENV = "TLM_USER_PASSWORD"

# ``report`` reads the whole state directory, so it is root-only. Tests (and a
# developer checkout) set this to ``1`` to run it as an ordinary user.
_REPORT_ALLOW_NON_ROOT_ENV = "TLM_REPORT_ALLOW_NON_ROOT"

# A CLI bundle is written owner-only: it is meant to be copied off the host.
_REPORT_FILE_MODE = 0o600

# Account roles a seeded user may be given, least-privileged first.
_USER_ROLES = ("admin", "operator", "viewer")
_DEFAULT_USER_ROLE = "viewer"


def _redact_db_url(url: str) -> str:
    """Mask any embedded credentials in a database URL for safe display."""
    return _URL_CREDENTIALS.sub("://***@", url)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser and its subcommands."""
    parser = argparse.ArgumentParser(
        prog="timelapse",
        description="Control and inspect a Timelapse Manager instance.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            "Path to a configuration file (YAML or JSON). Without it the CLI "
            "uses $TLM_CONFIG, then /etc/timelapse-manager/config.yml, then "
            "~/.config/timelapse-manager/config.yml, whichever exists first."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version", help="Print the application version.")

    config_parser = subparsers.add_parser("config", help="Inspect configuration.")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_show = config_sub.add_parser(
        "show", help="Print the resolved, non-secret configuration."
    )
    config_show.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text."
    )

    system_parser = subparsers.add_parser("system", help="Query a running service.")
    system_sub = system_parser.add_subparsers(dest="system_command", required=True)
    system_info = system_sub.add_parser(
        "info", help="Fetch system info from the local service over loopback."
    )
    system_info.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text."
    )

    _add_camera_commands(subparsers)
    _add_project_commands(subparsers)
    _add_user_commands(subparsers)
    _add_token_commands(subparsers)
    _add_secrets_commands(subparsers)

    _add_report_command(subparsers)

    _add_db_commands(subparsers)
    _add_frames_commands(subparsers)

    subparsers.add_parser(
        "setup-code",
        help=(
            "Print the one-time code the first-run setup page asks for (creating "
            "it if needed). Fails once setup is complete."
        ),
    )

    subparsers.add_parser(
        "migrate",
        help=(
            "Apply database migrations to head (an existing database behind head "
            "is snapshotted first)."
        ),
    )

    subparsers.add_parser(
        "run",
        help="Run the service in the foreground (HTTPS + HTTP) until interrupted.",
    )

    return parser


def _add_camera_commands(subparsers: Any) -> None:
    """Register the ``camera`` subcommands (loopback API)."""
    camera_parser = subparsers.add_parser("camera", help="Manage cameras.")
    camera_sub = camera_parser.add_subparsers(dest="camera_command", required=True)
    add = camera_sub.add_parser("add", help="Add a camera.")
    add.add_argument("--name", required=True, help="Camera display name.")
    add.add_argument("--address", help="Host or IP address.")
    add.add_argument(
        "--protocol",
        help="Camera protocol (e.g. vapix, onvif, rtsp, http_jpeg).",
    )
    add.add_argument("--snapshot-uri", dest="snapshot_uri", help="Snapshot URL.")
    add.add_argument("--stream-uri", dest="stream_uri", help="Stream (RTSP) URL.")
    add.add_argument("--username", help="Camera auth username.")
    add.add_argument("--password", help="Camera auth password.")


def _add_project_commands(subparsers: Any) -> None:
    """Register the ``project`` subcommands (loopback API)."""
    project_parser = subparsers.add_parser("project", help="Manage projects.")
    project_sub = project_parser.add_subparsers(dest="project_command", required=True)

    create = project_sub.add_parser("create", help="Create a project.")
    create.add_argument("--name", required=True, help="Project name.")
    create.add_argument(
        "--camera-id", dest="camera_id", type=int, required=True, help="Camera id."
    )
    create.add_argument(
        "--interval",
        type=int,
        required=True,
        help="Capture interval in seconds (>= 1).",
    )

    start = project_sub.add_parser("start", help="Start (resume) a project's capture.")
    start.add_argument("project_id", type=int, help="Project id.")

    stop = project_sub.add_parser("stop", help="Stop (pause) a project's capture.")
    stop.add_argument("project_id", type=int, help="Project id.")

    render = project_sub.add_parser("render", help="Trigger a render now.")
    render.add_argument("project_id", type=int, help="Project id.")

    status = project_sub.add_parser("status", help="Show project status.")
    status.add_argument(
        "project_id", type=int, nargs="?", help="Project id (omit for all)."
    )
    status.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text."
    )


def _add_report_command(subparsers: Any) -> None:
    """Register ``report`` (offline, root-only problem-report bundle)."""
    report = subparsers.add_parser(
        "report",
        help=(
            "Build a redacted problem-report bundle from the state directory "
            "(root-only; works with the service stopped)."
        ),
    )
    report.add_argument("--title", required=True, help="One-line summary.")
    report.add_argument(
        "--description", default="", help="What happened and what you expected."
    )
    report.add_argument(
        "--area",
        default="other",
        help="capture, project, renders, cameras, login or other.",
    )
    report.add_argument("--project", type=int, default=None, help="Related project id.")
    report.add_argument(
        "--anonymize",
        action="store_true",
        help=(
            "Replace user, project and camera names, host names, addresses and "
            "paths with placeholders. Always on while reporting.github.enabled."
        ),
    )
    report.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="Where to write the zip (default: ./<report id>.zip).",
    )


def _add_db_commands(subparsers: Any) -> None:
    """Register the ``db`` subcommands (offline, against the configured DB)."""
    db_parser = subparsers.add_parser(
        "db", help="Back up or restore the database (WAL-safe copies)."
    )
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    backup = db_sub.add_parser(
        "backup",
        help=(
            "Write a consistent copy of the database to a new file. Safe while "
            "the service runs."
        ),
    )
    backup.add_argument(
        "path", help="Destination file (must not exist); written mode 0600."
    )
    restore = db_sub.add_parser(
        "restore",
        help=(
            "Replace the database with a backup. The service must be stopped; "
            "the current database is kept as <db>.replaced-<time>."
        ),
    )
    restore.add_argument(
        "path", help="Backup file to restore (from 'db backup' or a snapshot)."
    )


def _add_frames_commands(subparsers: Any) -> None:
    """Register the ``frames`` subcommands (offline, read-only)."""
    frames_parser = subparsers.add_parser("frames", help="Inspect stored frame files.")
    frames_sub = frames_parser.add_subparsers(dest="frames_command", required=True)
    orphans = frames_sub.add_parser(
        "find-orphans",
        help=(
            "List files in the frame directories that no frame refers to (left by "
            "a crash between writing a file and recording it). Read-only: nothing "
            "is deleted."
        ),
    )
    orphans.add_argument(
        "--project", type=int, default=None, help="Only this project's directory."
    )
    orphans.add_argument(
        "--min-age",
        dest="min_age",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Ignore files modified more recently than this (default 60), so a "
            "capture in progress is not reported."
        ),
    )


def _add_token_commands(subparsers: Any) -> None:
    """Register the ``token`` subcommands (offline, against the token files)."""
    token_parser = subparsers.add_parser("token", help="Manage the API tokens.")
    token_sub = token_parser.add_subparsers(dest="token_command", required=True)
    rotate = token_sub.add_parser(
        "rotate",
        help=(
            "Replace the local API token with a new one. The running service "
            "picks it up on its next request; nothing secret is printed."
        ),
    )
    rotate.add_argument(
        "--metrics",
        action="store_true",
        help="Rotate the read-only metrics token instead.",
    )


def _add_secrets_commands(subparsers: Any) -> None:
    """Register the ``secrets`` subcommands (offline, key store plus database)."""
    from . import secrets_admin

    secrets_admin.add_commands(subparsers)


def _add_user_commands(subparsers: Any) -> None:
    """Register the ``user`` subcommands (in-process against the configured DB).

    Unlike the camera/project commands, ``user create`` does not call the running
    service: a fresh deploy seeds accounts before the service is up, so this
    command opens the configured database directly, just as ``migrate`` does.
    """
    user_parser = subparsers.add_parser("user", help="Manage local user accounts.")
    user_sub = user_parser.add_subparsers(dest="user_command", required=True)

    create = user_sub.add_parser(
        "create",
        help="Create a local user account directly in the configured database.",
    )
    create.add_argument("--username", required=True, help="Account username.")
    create.add_argument(
        "--password",
        default=None,
        help=(
            "Account password. If omitted, it is read from the "
            f"{_USER_PASSWORD_ENV} environment variable to keep it out of the "
            "process argument list."
        ),
    )
    create.add_argument(
        "--role",
        choices=_USER_ROLES,
        default=_DEFAULT_USER_ROLE,
        help=f"Account role (default: {_DEFAULT_USER_ROLE}).",
    )


def _print_mapping(data: dict[str, Any]) -> None:
    """Print a flat or nested mapping as aligned ``key: value`` lines."""
    for key, value in data.items():
        if isinstance(value, dict):
            print(f"{key}:")
            for sub_key, sub_value in value.items():
                print(f"  {sub_key}: {sub_value}")
        else:
            print(f"{key}: {value}")


def _config_summary(settings: Settings) -> dict[str, Any]:
    """Build the non-secret configuration view for ``config show``."""
    return {
        "server": {
            "bind_address": settings.server.bind_address,
            "http_port": settings.server.http_port,
            "https_port": settings.server.https_port,
            "redirect_http_to_https": settings.server.redirect_http_to_https,
        },
        "database": {
            "url": _redact_db_url(settings.database.url),
            "timeout": settings.database.timeout,
        },
        "logging": {
            "level": settings.logging.level,
            "format": settings.logging.format,
            "file_sink": str(settings.logging.file_sink)
            if settings.logging.file_sink is not None
            else None,
        },
        "paths": {
            "data_dir": str(settings.paths.data_dir),
            "frames_root": str(settings.paths.frames_root),
            "token_file": str(settings.paths.token_file),
        },
    }


def _cmd_version() -> int:
    """Print the application version. Fully offline."""
    print(get_app_version())
    return _EXIT_OK


def _cmd_config_show(settings: Settings, as_json: bool) -> int:
    """Print the resolved configuration with secrets redacted."""
    summary = _config_summary(settings)
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        _print_mapping(summary)
    return _EXIT_OK


def _cmd_system_info(settings: Settings, as_json: bool) -> int:
    """Fetch ``/api/v1/system`` from the local service over loopback."""
    token = _read_cli_token(settings)
    url = f"http://{_LOOPBACK_HOST}:{settings.server.http_port}/api/v1/system"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        print(f"error: could not reach local service at {url}: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    if response.status_code != httpx.codes.OK:
        print(
            f"error: service returned HTTP {response.status_code}",
            file=sys.stderr,
        )
        return _EXIT_ERROR

    payload = response.json()
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        _print_mapping(payload)
    return _EXIT_OK


def _read_cli_token(settings: Settings) -> str:
    """Read the local API token for a loopback call, or exit with status 2.

    Never creates the file: only the service may, so it is owned by the service
    account. Prints the reason (never the token) when it cannot be read.
    """
    path = local_token_path(settings)
    try:
        return read_token_file(path)
    except FileNotFoundError:
        print(
            f"error: token file {path} not found; start the service once to create "
            f"it{_config_hint()}",
            file=sys.stderr,
        )
    except PermissionError:
        print(
            f"error: cannot read token file {path}: permission denied (run as "
            "the service account or root)",
            file=sys.stderr,
        )
    except (OSError, TokenFileError) as exc:
        print(f"error: cannot use token file {path}: {exc}", file=sys.stderr)
    raise SystemExit(_EXIT_NO_TOKEN)


def _cmd_token_rotate(settings: Settings, *, metrics: bool) -> int:
    """Replace the local API (or metrics) token file with a fresh token.

    The file must already exist (the service creates it); as root, the new file
    keeps the old one's owner and group. Prints the path, never the token.
    """
    path = metrics_token_path(settings) if metrics else local_token_path(settings)
    label = "metrics token" if metrics else "local API token"
    try:
        rotate_token_file(path)
    except FileNotFoundError:
        print(
            f"error: token file {path} not found; start the service once to create it",
            file=sys.stderr,
        )
        return _EXIT_NO_TOKEN
    except (OSError, TokenFileError) as exc:
        print(f"error: could not rotate {label} in {path}: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    print(
        f"Rotated the {label} in {path}. The running service uses it from its "
        "next request; update any client that holds the old one."
    )
    return _EXIT_OK


def _api_url(settings: Settings, path: str) -> str:
    """Build the loopback URL for a local-API path."""
    return f"http://{_LOOPBACK_HOST}:{settings.server.http_port}{path}"


def _api_call(
    settings: Settings,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response | None:
    """Call the local API over loopback with the bearer token.

    Returns the response, or ``None`` after printing a connection error (the
    caller maps ``None`` to a non-zero exit). The token is read from the
    configured token file; redirects are not followed and TLS is never bypassed,
    matching the ``system info`` contract.
    """
    token = _read_cli_token(settings)
    url = _api_url(settings, path)
    try:
        return httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        print(f"error: could not reach local service at {url}: {exc}", file=sys.stderr)
        return None


def _fail_from_response(response: httpx.Response) -> int:
    """Print a service error (with the API's ``detail`` when present) and fail."""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            detail = f": {body['detail']}"
    except ValueError:
        pass
    print(
        f"error: service returned HTTP {response.status_code}{detail}",
        file=sys.stderr,
    )
    return _EXIT_ERROR


def _cmd_camera_add(settings: Settings, args: argparse.Namespace) -> int:
    """Create a camera via the local API."""
    body: dict[str, Any] = {"name": args.name}
    for field, value in (
        ("address", args.address),
        ("protocol", args.protocol),
        ("snapshot_uri", args.snapshot_uri),
        ("stream_uri", args.stream_uri),
    ):
        if value:
            body[field] = value
    if args.username or args.password:
        credentials: dict[str, Any] = {}
        if args.username:
            credentials["username"] = args.username
        if args.password:
            credentials["password"] = args.password
        body["credentials"] = credentials

    response = _api_call(settings, "POST", "/api/v1/cameras", json_body=body)
    if response is None:
        return _EXIT_ERROR
    if response.status_code != httpx.codes.CREATED:
        return _fail_from_response(response)
    camera = response.json()
    print(f"camera {camera['id']} created: {camera['name']}")
    return _EXIT_OK


def _cmd_project_create(settings: Settings, args: argparse.Namespace) -> int:
    """Create a project via the local API."""
    body = {
        "name": args.name,
        "camera_id": args.camera_id,
        "capture_interval_seconds": args.interval,
    }
    response = _api_call(settings, "POST", "/api/v1/projects", json_body=body)
    if response is None:
        return _EXIT_ERROR
    if response.status_code != httpx.codes.CREATED:
        return _fail_from_response(response)
    project = response.json()
    print(f"project {project['id']} created: {project['name']}")
    return _EXIT_OK


def _cmd_project_start_stop(settings: Settings, project_id: int, *, start: bool) -> int:
    """Start (resume) or stop (pause) a project's capture via the local API."""
    past = "started" if start else "stopped"
    endpoint = "resume" if start else "pause"
    response = _api_call(settings, "POST", f"/api/v1/projects/{project_id}/{endpoint}")
    if response is None:
        return _EXIT_ERROR
    # Resuming a project that is already active answers 409; for a "start" that is
    # a benign no-op, so report it as success rather than an error.
    if start and response.status_code == httpx.codes.CONFLICT:
        print(f"project {project_id} is already active")
        return _EXIT_OK
    if response.status_code != httpx.codes.OK:
        return _fail_from_response(response)
    project = response.json()
    print(f"project {project['id']} {past} (lifecycle={project['lifecycle_state']})")
    return _EXIT_OK


def _cmd_project_render(settings: Settings, project_id: int) -> int:
    """Trigger an on-demand render for a project via the local API."""
    response = _api_call(
        settings, "POST", f"/api/v1/projects/{project_id}/renders", json_body={}
    )
    if response is None:
        return _EXIT_ERROR
    if response.status_code != httpx.codes.CREATED:
        return _fail_from_response(response)
    job = response.json()
    print(f"render job {job['id']} queued for project {project_id} ({job['status']})")
    return _EXIT_OK


def _print_project_status(project: dict[str, Any]) -> None:
    """Print a one-line status summary for a project."""
    uptime = project.get("uptime_seconds")
    uptime_str = f"{uptime}s" if uptime is not None else "-"
    print(
        f"[{project['id']}] {project['name']}: "
        f"{project['operational_status']} ({project['lifecycle_state']}), "
        f"frames={project['frame_count']}, "
        f"disk={project['disk_used_bytes']}B, uptime={uptime_str}"
    )


def _cmd_project_status(
    settings: Settings, project_id: int | None, as_json: bool
) -> int:
    """Show status for one project or all projects via the local API."""
    path = (
        f"/api/v1/projects/{project_id}"
        if project_id is not None
        else "/api/v1/projects"
    )
    response = _api_call(settings, "GET", path)
    if response is None:
        return _EXIT_ERROR
    if response.status_code != httpx.codes.OK:
        return _fail_from_response(response)
    payload = response.json()
    if as_json:
        print(json.dumps(payload, indent=2))
        return _EXIT_OK
    projects = payload if isinstance(payload, list) else [payload]
    if not projects:
        print("no projects")
        return _EXIT_OK
    for project in projects:
        _print_project_status(project)
    return _EXIT_OK


def _cmd_migrate(settings: Settings) -> int:
    """Apply database migrations to head against the configured database.

    Builds the Alembic configuration from ``alembic.ini`` and overrides its
    database URL with the resolved settings, so a custom ``--config`` that points
    at a different database is honored rather than silently ignored.

    The config file and migrations directory are resolved relative to the
    package/bundle rather than the working directory, so ``migrate`` works from
    any CWD and inside a frozen bundle (where ``alembic.ini``'s
    working-directory-relative ``script_location`` would otherwise break).
    """
    from ..db.migrate import MigrationAbortedError, apply_migrations

    try:
        apply_migrations(settings)
    except MigrationAbortedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    return _EXIT_OK


def _configured_database(settings: Settings) -> Path | None:
    """The configured SQLite file, or ``None`` after printing why there is none."""
    database = _sqlite_path(settings.database.url)
    if database is None:
        print(
            "error: 'db' commands support only a SQLite database file",
            file=sys.stderr,
        )
    return database


def _cmd_db_backup(settings: Settings, destination: str) -> int:
    """Write a WAL-consistent copy of the configured database to a new file.

    Uses SQLite's online backup API, so it is safe while the service writes. As
    root, the live database is read as its directory's owner (so no root-owned
    ``-wal``/``-shm`` file is left in the state directory) into memory, and the
    copy is written as the caller, so it can go anywhere root can write. The
    copy is created ``0600`` and never overwrites an existing file.
    """
    from ..db.backup import BackupError, backup_file_to_memory, backup_memory_to_file

    database = _configured_database(settings)
    if database is None:
        return _EXIT_ERROR
    if not database.is_file():
        print(f"error: database {database} does not exist", file=sys.stderr)
        return _EXIT_ERROR
    target = Path(destination)
    try:
        with _as_owner_of(database.parent):
            memory = backup_file_to_memory(database)
        try:
            backup_memory_to_file(memory, target)
        finally:
            memory.close()
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"error: backup failed: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    print(
        f"backed up {database} to {target} "
        f"({target.stat().st_size} bytes, integrity check ok)"
    )
    return _EXIT_OK


def _service_answers(settings: Settings) -> bool:
    """Whether something answers ``/healthz`` on the loopback HTTP port.

    Only a refused connection counts as "not running"; a timeout or any HTTP
    answer (including a redirect or an error status) means the port is held.
    """
    try:
        httpx.get(
            _api_url(settings, "/healthz"),
            timeout=_HEALTH_PROBE_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.ConnectError:
        return False
    except httpx.HTTPError:
        return True
    return True


def _cmd_db_restore(settings: Settings, source: str) -> int:
    """Replace the configured database with a backup, with the service stopped.

    Refuses while the service answers on loopback. The backup is read into
    memory, must pass ``PRAGMA integrity_check`` and carry an Alembic revision.
    The live database (with its ``-wal``) is kept as ``<db>.replaced-<time>``;
    the restored file is written as the state directory's owner. Migrations to
    head run at the next service start, behind a fresh snapshot.
    """
    from ..db.backup import (
        BackupError,
        backup_file_to_memory,
        integrity_problems,
        restore_into_place,
        revision_of,
    )

    database = _configured_database(settings)
    if database is None:
        return _EXIT_ERROR
    if _service_answers(settings):
        print(
            "error: the service is answering on "
            f"{_api_url(settings, '/healthz')}; stop it first "
            "(systemctl stop timelapse-manager)",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    backup = Path(source)
    if not backup.is_file():
        print(f"error: backup {backup} does not exist", file=sys.stderr)
        return _EXIT_ERROR
    if not database.parent.is_dir():
        print(f"error: directory {database.parent} does not exist", file=sys.stderr)
        return _EXIT_ERROR
    try:
        with _as_owner_of(backup.parent):
            memory = backup_file_to_memory(backup)
        try:
            problems = integrity_problems(memory)
            if problems:
                print(
                    f"error: {backup} failed its integrity check: {problems[:5]}",
                    file=sys.stderr,
                )
                return _EXIT_ERROR
            revision = revision_of(memory)
            if revision is None:
                print(
                    f"error: {backup} has no schema revision; it is not a "
                    "Timelapse Manager database",
                    file=sys.stderr,
                )
                return _EXIT_ERROR
            with _as_owner_of(database.parent):
                replaced = restore_into_place(memory, database)
        finally:
            memory.close()
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"error: restore failed: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    kept = f"; the previous database is kept as {replaced}" if replaced else ""
    print(f"restored {backup} (revision {revision}) to {database}{kept}")
    return _EXIT_OK


def _cmd_user_create(settings: Settings, args: argparse.Namespace) -> int:
    """Create a local user account directly in the configured database.

    Runs in-process against the already-migrated database (the deploy runs
    ``migrate`` first), opening it with the same engine/session bootstrap the
    service uses so the SQLite pragmas match. Used to seed fixed accounts on a
    fresh, headless install without the interactive first-run web flow.

    The password comes from ``--password`` or, when that is omitted, the
    ``TLM_USER_PASSWORD`` environment variable, so it need not appear in the
    process argument list. If the username already exists the account is left
    untouched and the command fails, so re-running a deploy never overwrites or
    re-hashes an existing credential. The plaintext password is never logged.
    """
    # Imported lazily so the offline CLI commands (e.g. ``version``) do not drag
    # in the ORM and security layer, matching ``migrate``/``run``.
    from sqlalchemy import select

    from ..db.engine import create_db_engine
    from ..db.models import User
    from ..db.session import create_session_factory, session_scope
    from ..security.login import create_local_user
    from ..security.principal import ensure_sentinel_admin
    from ..usernames import username_key

    password = args.password or os.environ.get(_USER_PASSWORD_ENV)
    if not password:
        print(
            "error: a password is required; pass --password or set "
            f"{_USER_PASSWORD_ENV}",
            file=sys.stderr,
        )
        return _EXIT_ERROR

    engine = create_db_engine(settings.database.url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as db:
            # Reserve the sentinel's fixed id=1 and satisfy the audit foreign key
            # before inserting; on a freshly migrated database the sentinel row
            # may not exist yet.
            ensure_sentinel_admin(db)

            existing = db.execute(
                select(User.id).where(User.username_key == username_key(args.username))
            ).first()
            if existing is not None:
                print(
                    f"error: user '{args.username}' already exists; "
                    "not modifying the existing account",
                    file=sys.stderr,
                )
                return _EXIT_ERROR

            create_local_user(
                db,
                args.username,
                password,
                args.role,
                settings=settings.auth,
            )
    finally:
        engine.dispose()

    print(f"created user '{args.username}' with role {args.role}")
    return _EXIT_OK


def _cmd_frames_find_orphans(settings: Settings, args: argparse.Namespace) -> int:
    """Print the frame files that have no ``Frame`` row, one per line.

    Read-only and offline: it reads the configured database and lists directory
    entries, and never writes either. Each line is ``<kind> project=<id>
    size=<bytes> <path>``; ``kind`` is ``frame`` (a finished image whose row was
    never committed) or ``temp`` (a leftover temporary file). The exit status is
    0 whether or not orphans exist; a missing database is an error.
    """
    from ..db.engine import create_db_engine
    from ..db.session import create_session_factory, session_scope
    from ..storage.orphans import DEFAULT_MIN_AGE_SECONDS, find_orphan_files

    database = _sqlite_path(settings.database.url)
    if database is not None and not database.is_file():
        print(f"error: database {database} does not exist", file=sys.stderr)
        return _EXIT_ERROR
    min_age = DEFAULT_MIN_AGE_SECONDS if args.min_age is None else args.min_age
    if min_age < 0:
        print("error: --min-age must not be negative", file=sys.stderr)
        return _EXIT_ERROR

    engine = create_db_engine(settings.database.url)
    try:
        with session_scope(create_session_factory(engine)) as session:
            orphans = find_orphan_files(
                session, settings, project_id=args.project, min_age_seconds=min_age
            )
    finally:
        engine.dispose()

    for item in orphans:
        print(
            f"{item.kind} project={item.project_id} size={item.size_bytes} {item.path}"
        )
    total = sum(item.size_bytes for item in orphans)
    print(f"{len(orphans)} orphan file(s), {total} bytes; nothing was deleted")
    return _EXIT_OK


def _cmd_setup_code(settings: Settings) -> int:
    """Print the first-run setup code, creating it if setup is still needed.

    Runs in-process against the configured database and data directory, so it
    works before anyone has opened the setup page and whether or not the service
    is running. Run as root (``sudo``), it switches to the data directory's
    owner first, so a code file it creates -- and any SQLite side file -- is
    owned by the service account, and no symbolic link in that directory is
    followed as root. Once an administrator exists there is nothing to print:
    any leftover code file is removed and the command fails.
    """
    # Imported lazily, like ``user create``: the offline commands should not
    # drag in the ORM and security layer.
    from sqlalchemy.exc import SQLAlchemyError

    from ..db.engine import create_db_engine
    from ..db.session import create_session_factory, session_scope
    from ..security.login import (
        discard_setup_code,
        ensure_setup_code,
        first_run_needed,
        read_setup_code,
    )

    data_dir = settings.paths.data_dir
    if not data_dir.is_dir():
        print(f"error: data directory {data_dir} does not exist", file=sys.stderr)
        return _EXIT_ERROR

    with _as_owner_of(data_dir):
        engine = create_db_engine(settings.database.url)
        try:
            with session_scope(create_session_factory(engine)) as db:
                needed = first_run_needed(db)
        except SQLAlchemyError as exc:
            print(
                f"error: could not read the database (run 'migrate' first?): {exc}",
                file=sys.stderr,
            )
            return _EXIT_ERROR
        finally:
            engine.dispose()

        if not needed:
            discard_setup_code(data_dir)
            print(
                "error: first-run setup is already complete; no setup code is needed",
                file=sys.stderr,
            )
            return _EXIT_ERROR
        try:
            ensure_setup_code(data_dir)
        except OSError as exc:
            print(f"error: could not create the setup code: {exc}", file=sys.stderr)
            return _EXIT_ERROR
        code = read_setup_code(data_dir)

    if code is None:
        print("error: the setup code file is unreadable", file=sys.stderr)
        return _EXIT_ERROR
    print(code)
    return _EXIT_OK


@contextlib.contextmanager
def _as_owner_of(path: Path) -> Iterator[None]:
    """Run the block with the effective uid/gid of ``path``'s owner.

    ``report`` runs as root but reads the service's state directory. Opening
    the SQLite database can create ``-wal``/``-shm`` files, and a root-owned
    file left in the service's state directory has broken a service here
    before. Switching the effective ids to the directory's owner for the
    duration makes anything created there owned correctly. A no-op when not
    root or when the owner is root.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        yield
        return
    info = path.stat()
    if info.st_uid == 0:
        yield
        return
    os.setegid(info.st_gid)
    os.seteuid(info.st_uid)
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(0)


def _install_existing_key_provider(settings: Settings) -> None:
    """Use the recorded at-rest key only if it already exists (never create one).

    Without a key the stored secrets cannot be decrypted -- which also means
    they cannot be read into the bundle. The provider is the one recorded in
    ``secrets_meta`` (the key file when nothing is recorded yet).
    """
    from ..security.crypto import set_key_provider
    from . import secrets_admin

    provider = secrets_admin.existing_key_provider(settings)
    if provider is not None:
        set_key_provider(provider)


def _cmd_secrets(settings: Settings, args: argparse.Namespace) -> int:
    """Run a ``secrets`` subcommand as the owner of the state directory.

    Run as root, a key file or SQLite sidecar created here would be root-owned
    and unreadable to the service user, which would then refuse to start; so
    the work happens with the data directory owner's effective ids.
    """
    from . import secrets_admin

    running = args.secrets_command != "status" and _service_answers(settings)
    with _as_owner_of(settings.paths.data_dir):
        return secrets_admin.run(settings, args, service_running=running)


def _cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    """Build a problem-report bundle offline, straight from the state directory.

    Covers the case where the web UI itself is broken. The bundle has the same
    sections as a web report, redacted the same way; the live sections (capture
    runners, task dump, in-memory log) say they are unavailable, and the log
    comes from ``logging.file_sink`` when one is configured.

    Read-only on the state: it registers no report row and writes nothing
    under the state directory, so it never leaves a root-owned file there. The
    zip goes to ``--out`` (default ``./<report id>.zip``), mode ``0600``.

    ``--anonymize`` (forced while ``reporting.github.enabled``) anonymizes the
    bundle exactly as the web form does (#21). A bundle that cannot be proved
    clean is refused and nothing is written. The ``sudo`` user's own name is
    replaced but, when short or generic (``pi``), never refuses the bundle.
    """
    if (
        hasattr(os, "geteuid")
        and os.geteuid() != 0
        and os.environ.get(_REPORT_ALLOW_NON_ROOT_ENV) != "1"
    ):
        print(
            "error: 'report' reads the service's state directory and must run "
            "as root (sudo)",
            file=sys.stderr,
        )
        return _EXIT_ERROR

    from ..db.engine import create_db_engine
    from ..db.session import create_session_factory, session_scope
    from ..ffmpeg_pin import resolve_ffmpeg_binary
    from ..reports.anonymize import ReportAnonymizationError
    from ..reports.service import build_report_zip, should_anonymize
    from ..reports.snapshot import ReportMeta, RequestContext, SnapshotSources
    from ..reports.store import normalise_area
    from ..version import probe_ffmpeg_version

    now = datetime.now(UTC).replace(tzinfo=None)
    key = f"TLM-{now:%Y%m%d}-CLI{now:%H%M%S}"
    data_dir = settings.paths.data_dir
    if not data_dir.is_dir():
        print(f"error: state directory {data_dir} does not exist", file=sys.stderr)
        return _EXIT_ERROR
    # Opening a missing SQLite file would create an empty (root-owned)
    # database; a wrong path almost always means the service's environment
    # (TLM_CONFIG / TLM_DATABASE__URL) was not passed through sudo.
    database = _sqlite_path(settings.database.url)
    if database is not None and not database.is_file():
        print(
            f"error: database {database} does not exist; pass the service's "
            "environment (see docs/operations/problem-reports.md)",
            file=sys.stderr,
        )
        return _EXIT_ERROR

    try:
        ffmpeg_version = probe_ffmpeg_version(resolve_ffmpeg_binary(settings))
    except Exception:  # noqa: BLE001 - a missing ffmpeg must not block a report
        ffmpeg_version = "unavailable"

    meta = ReportMeta(
        report_key=key,
        title=args.title,
        description=args.description,
        area=normalise_area(args.area),
        reported_at=now,
        created_at=now,
        username=os.environ.get("SUDO_USER") or None,
        role="host",
        project_id=args.project,
        camera_id=None,
        include_snapshot=True,
        source="cli",
        anonymize=should_anonymize(settings, bool(args.anonymize)),
    )
    sources = SnapshotSources(
        ffmpeg_version=ffmpeg_version,
        offline_reason="built offline by the CLI; the service process was not read",
        extra_app_fields={"process": "cli (pid and uptime describe the CLI)"},
    )
    with _as_owner_of(data_dir):
        _install_existing_key_provider(settings)
        engine = create_db_engine(settings.database.url)
        try:
            factory = create_session_factory(engine)
            with session_scope(factory) as session:
                if args.project is not None:
                    from ..db.models import Project

                    project = session.get(Project, args.project)
                    if project is not None:
                        meta = _with_camera(meta, project.camera_id)
                try:
                    data = build_report_zip(
                        session,
                        settings,
                        meta,
                        RequestContext(user_agent="timelapse-manager CLI"),
                        sources,
                        None,
                        os_user=meta.username,
                    )
                except ReportAnonymizationError as exc:
                    print(f"error: {exc.detail}", file=sys.stderr)
                    return _EXIT_ERROR
        finally:
            engine.dispose()

    from ..reports.store import UnsafeTargetError, atomic_write

    out = Path(args.out) if args.out else Path.cwd() / f"{key}.zip"
    # Runs as root, possibly in a shared directory: never open the output by
    # name (a planted symlink would redirect the write); see atomic_write.
    try:
        atomic_write(out, data, _REPORT_FILE_MODE)
    except (UnsafeTargetError, OSError) as exc:
        print(f"error: could not write {out}: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    note = ", anonymized" if meta.anonymize else ""
    print(f"report {key} written to {out} ({len(data)} bytes{note})")
    return _EXIT_OK


def _sqlite_path(url: str) -> Path | None:
    """The file behind a SQLite URL, or ``None`` for another backend/memory."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    if not parsed.database or parsed.database == ":memory:":
        return None
    return Path(parsed.database)


def _with_camera(meta: Any, camera_id: int | None) -> Any:
    """Return ``meta`` with the related camera filled in."""
    from dataclasses import replace

    return replace(meta, camera_id=camera_id)


def _cmd_run(config_path: str | None) -> int:
    """Run the service in the foreground until interrupted.

    Routes through the same serve coroutine the daemon entry point uses, so the
    frozen ``timelapse-manager run`` command (invoked by a service manager's
    ``ExecStart``) and ``timelapse-daemon`` share one serve implementation rather
    than duplicating it. Blocks until a signal triggers graceful shutdown.

    Settings are re-resolved here *with provenance* (rather than reusing the
    already-loaded settings) so the long-running service reports which values the
    environment controls -- the frozen ``run`` command is the production serve
    path for packaged installs.
    """
    from ..service import serve

    settings, env_overrides = load_settings_with_provenance(config_path)
    serve(settings, env_overrides)
    return _EXIT_OK


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. Exits with a status code reflecting success or failure."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # ``version`` is fully offline and must not touch settings or the filesystem.
    if args.command == "version":
        raise SystemExit(_cmd_version())

    global _config_in_use
    config_path = _resolve_cli_config_path(args.config)
    _config_in_use = config_path or os.environ.get(_CONFIG_PATH_ENV) or None
    try:
        settings = load_settings(config_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(_EXIT_ERROR) from exc

    if args.command == "config":
        raise SystemExit(_cmd_config_show(settings, args.json))
    if args.command == "system":
        raise SystemExit(_cmd_system_info(settings, args.json))
    if args.command == "camera":
        raise SystemExit(_dispatch_camera(settings, args))
    if args.command == "project":
        raise SystemExit(_dispatch_project(settings, args))
    if args.command == "user":
        raise SystemExit(_dispatch_user(settings, args))
    if args.command == "token":
        raise SystemExit(_cmd_token_rotate(settings, metrics=args.metrics))
    if args.command == "report":
        raise SystemExit(_cmd_report(settings, args))
    if args.command == "db":
        raise SystemExit(_dispatch_db(settings, args))
    if args.command == "frames":
        raise SystemExit(_dispatch_frames(settings, args))
    if args.command == "secrets":
        raise SystemExit(_cmd_secrets(settings, args))
    if args.command == "setup-code":
        raise SystemExit(_cmd_setup_code(settings))
    if args.command == "migrate":
        raise SystemExit(_cmd_migrate(settings))
    if args.command == "run":
        raise SystemExit(_cmd_run(config_path))

    parser.error(f"unknown command: {args.command}")


def _dispatch_camera(settings: Settings, args: argparse.Namespace) -> int:
    """Route a ``camera`` subcommand to its handler."""
    if args.camera_command == "add":
        return _cmd_camera_add(settings, args)
    return _EXIT_ERROR


def _dispatch_project(settings: Settings, args: argparse.Namespace) -> int:
    """Route a ``project`` subcommand to its handler."""
    command = args.project_command
    if command == "create":
        return _cmd_project_create(settings, args)
    if command == "start":
        return _cmd_project_start_stop(settings, args.project_id, start=True)
    if command == "stop":
        return _cmd_project_start_stop(settings, args.project_id, start=False)
    if command == "render":
        return _cmd_project_render(settings, args.project_id)
    if command == "status":
        return _cmd_project_status(settings, args.project_id, args.json)
    return _EXIT_ERROR


def _dispatch_db(settings: Settings, args: argparse.Namespace) -> int:
    """Route a ``db`` subcommand to its handler."""
    if args.db_command == "backup":
        return _cmd_db_backup(settings, args.path)
    if args.db_command == "restore":
        return _cmd_db_restore(settings, args.path)
    return _EXIT_ERROR


def _dispatch_frames(settings: Settings, args: argparse.Namespace) -> int:
    """Route a ``frames`` subcommand to its handler."""
    if args.frames_command == "find-orphans":
        return _cmd_frames_find_orphans(settings, args)
    return _EXIT_ERROR


def _dispatch_user(settings: Settings, args: argparse.Namespace) -> int:
    """Route a ``user`` subcommand to its handler."""
    if args.user_command == "create":
        return _cmd_user_create(settings, args)
    return _EXIT_ERROR
