"""Command-line interface.

A thin argparse front end over the application. Some subcommands run fully
offline (``version``, ``config show``, ``migrate``, ``user create``); the
offline database commands open the configured database directly. The rest --
``system info`` and the ``camera``/``project`` control commands -- talk to a
running service over the loopback control surface, so the CLI and web UI operate
on one shared state.

Loopback contract
-----------------
The service-touching commands call the local API on ``127.0.0.1:<http_port>``
over plain ``http://`` and authenticate with ``Authorization: Bearer <token>``,
where the token is read from ``settings.paths.token_file``. Redirects are not
followed and TLS verification is never disabled; full HTTPS plus an
HTTP-to-HTTPS redirect is a later phase, and the CLI must reach the service
exactly as it is served now. Because every mutation flows through the same API
the web UI uses, the running supervisor is notified and capture/render react
without a restart.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

import httpx

from ..config import (
    ConfigError,
    Settings,
    load_settings,
    load_settings_with_provenance,
)
from ..security.token import ensure_local_token
from ..version import get_app_version

# Loopback is the only valid connect target for the local control surface;
# the server may bind 0.0.0.0, which is not itself a connectable address.
_LOOPBACK_HOST = "127.0.0.1"

# Bound the loopback call so a wedged service cannot hang the CLI.
_REQUEST_TIMEOUT_SECONDS = 10.0

_EXIT_OK = 0
_EXIT_ERROR = 1

# Matches the ``user:password@`` credential portion of a connection URL.
_URL_CREDENTIALS = re.compile(r"://[^/@]+@")

# Environment fallback for ``user create --password``. Reading the password from
# the environment instead of the command line keeps it out of the process
# argument list (which is world-readable via ``ps`` on a shared host).
_USER_PASSWORD_ENV = "TLM_USER_PASSWORD"

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
        help="Path to a configuration file (YAML or JSON).",
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

    subparsers.add_parser("migrate", help="Apply database migrations to head.")

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
    token = ensure_local_token(settings)
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
    token = ensure_local_token(settings)
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
    from ..db.migrate import apply_migrations

    apply_migrations(settings)
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
                select(User.id).where(User.username == args.username)
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

    try:
        settings = load_settings(args.config)
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
    if args.command == "migrate":
        raise SystemExit(_cmd_migrate(settings))
    if args.command == "run":
        raise SystemExit(_cmd_run(args.config))

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


def _dispatch_user(settings: Settings, args: argparse.Namespace) -> int:
    """Route a ``user`` subcommand to its handler."""
    if args.user_command == "create":
        return _cmd_user_create(settings, args)
    return _EXIT_ERROR
