"""Anonymizing a problem report for a public issue (#21).

A report bound for a public GitHub issue must not carry a host name, an IP
address, a username, a camera or project name, a path or a secret. This module
replaces those with stable placeholders (``user-1``, ``camera-2``, ``host-1``,
``/path/data``) and then proves, by re-scanning, that none is left.

Two parts:

* :class:`Pseudonymizer` -- built once per report from the **entities** the
  install knows (:func:`collect_entities`: users, projects, cameras, camera
  hosts and URLs, the machine's host name and FQDN, the hosts in the reporting,
  LDAP, SMTP and webhook settings, LDAP DNs, every path in the settings and
  the projects) plus a **pattern safety net** (every absolute path outside a
  short allow-list, IPv4/IPv6, MAC and e-mail addresses, URL hosts and
  credentials, ``dc=`` runs, host names under a known domain). Entity matches
  are case-insensitive (casefolded), separator-tolerant for names, bounded by
  non-alphanumerics, and the same value always gets the same placeholder
  within one report so the log stays readable. Replacement is one compiled
  alternation, longest first, so a 2,000-line log is a single pass. The
  reporter's own title and description get kind-less ``name-N`` placeholders
  instead (:meth:`Pseudonymizer.user_text`).
* :class:`Verifier` -- the fail-closed check. After anonymizing, every zip
  member, the title and description and the GitHub URL are re-scanned (with
  an independently built matcher) for every entity value, every secret and the
  safety-net patterns, plus generic leftover checks that do not depend on
  the install's values. Any hit raises :class:`ReportAnonymizationError`; the
  caller then stores nothing. So does an entity source that cannot be read.

**Short and generic names (D16).** A user called ``admin`` or a camera called
``Pi`` cannot be replaced everywhere without wrecking the log ("admin" is also
a role, "pi" is inside many words' neighbours). Entities shorter than
:data:`MIN_ENTITY_LENGTH`, made only of digits, or on :data:`STOP_WORDS` are
*weak*: they are replaced only where they are a whole structured value (a
``username``/``name``/``hostname`` field). If one still appears in free text:

* with the GitHub hand-off enabled the verifier refuses the report and names
  the entity (:class:`GenericNameError`), so an administrator can rename it;
* in anonymize-only filing (GitHub off, not public) it is left in place.

Values under enumeration keys (``role``, ``level``, ``status``, ...) are app
vocabulary, not free text, so a user called ``admin`` does not trip on
``"role": "admin"``.

What this cannot catch is documented in ``docs/operations/problem-reports.md``:
free text a camera or library put in a log line that names nothing the install
knows about, and the screenshot (always left out of an anonymized report).
"""

from __future__ import annotations

import contextlib
import html
import ipaddress
import json
import logging
import re
import socket
import urllib.parse
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..services.errors import AppError
from .redaction import Scrubber, _variants

logger = logging.getLogger(__name__)

# Entities shorter than this are weak (structured fields only, D16).
MIN_ENTITY_LENGTH = 3

# Generic words: a name equal to one of these is weak (D16). The app's own
# vocabulary (roles, levels, scopes, states) is here so that, say, a camera
# called "Error" does not rewrite every log level.
STOP_WORDS: frozenset[str] = frozenset(
    {
        "admin",
        "administrator",
        "root",
        "user",
        "users",
        "guest",
        "operator",
        "viewer",
        "system",
        "test",
        "testing",
        "demo",
        "default",
        "camera",
        "cameras",
        "cam",
        "project",
        "projects",
        "timelapse",
        "frame",
        "frames",
        "render",
        "renders",
        "capture",
        "server",
        "host",
        "localhost",
        "local",
        "home",
        "main",
        "new",
        "none",
        "null",
        "unknown",
        "info",
        "debug",
        "warning",
        "error",
        "critical",
        "active",
        "paused",
        "stopped",
        "done",
        "failed",
        "pending",
        "running",
    }
)

# Values of these keys are app vocabulary, never free text: a weak name equal
# to one of them is not a leak (``"role": "admin"`` for a user ``admin``).
ENUM_KEYS: frozenset[str] = frozenset(
    {
        "role",
        "level",
        "scope",
        "area",
        "area_label",
        "source",
        "protocol",
        "status",
        "state",
        "kind",
        "type",
        "action",
        "decision",
        "origin",
        "lifecycle_state",
        "operational_status",
        "capture_status",
        "capture_reason",
        "delivery_state",
        "encoder_engine",
        "geolocation_source",
        "device_hostname_source",
        "tls_mode",
        "membership_mode",
        "smtp_security",
        "method",
    }
)

# Whole-value positions where a weak entity *is* replaced: structured fields
# that name a user, project, camera or host.
IDENTITY_KEYS: frozenset[str] = frozenset(
    {
        "username",
        "user",
        "name",
        "project_name",
        "camera_name",
        "hostname",
        "device_hostname",
        "host",
        "address",
        "reporter",
    }
)

# Location: never part of a public report, whatever the field holds.
_GEO_KEY = re.compile(r"latitude|longitude|\Alat\Z|\Alon\Z|\Alng\Z", re.IGNORECASE)

# Not private: loopback names and paths every install has.
_PUBLIC_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_GENERIC_PATHS = frozenset(
    {
        "/",
        "/tmp",
        "/var",
        "/var/lib",
        "/var/log",
        "/root",
        "/home",
        "/users",
        "/usr",
        "/opt",
        "/etc",
        "/srv",
        "/data",
        "/nonexistent",
    }
)
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "ac", "gov", "edu"})

ANONYMIZED_USERINFO = "«anonymized»@"

# The sentinel administrator (security/principal.py) has the same name on
# every install; it is not private and must not count as a D16 entity.
_SENTINEL_USER = (1, "system")

KIND_LABELS = {
    "user": "user name",
    "project": "project name",
    "camera": "camera name",
    "host": "host name",
}

_WHERE_TEXT = "your title or description"
_WHERE_SNAPSHOT = "the diagnostic snapshot"

GENERIC_REFUSAL = (
    "The report could not be anonymized safely, so nothing was saved or sent. "
    "Try again; if it keeps failing, tell an administrator."
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReportAnonymizationError(AppError):
    """Anonymizing failed or left private data behind; nothing is stored."""

    status = 422
    code = "report_not_anonymized"


@dataclass(frozen=True)
class GenericNameHit:
    """A weak entity found in free text of a report bound for GitHub."""

    kind: str
    value: str
    where: str


class GenericNameError(ReportAnonymizationError):
    """A short or generic name would reach a public issue (D16)."""

    code = "report_generic_name"

    def __init__(
        self,
        hits: Iterable[GenericNameHit],
        *,
        snapshot_optional: bool,
        reveal: bool = True,
    ) -> None:
        self.hits = tuple(hits)
        self.snapshot_optional = snapshot_optional
        super().__init__(
            generic_name_message(
                self.hits, snapshot_optional=snapshot_optional, reveal=reveal
            )
        )

    def for_viewer(self, *, reveal: bool) -> GenericNameError:
        """The same refusal worded for a viewer who may (not) see the names."""
        return GenericNameError(
            self.hits, snapshot_optional=self.snapshot_optional, reveal=reveal
        )


_FIXES = {
    "camera": "rename that camera",
    "project": "rename that project",
    "user": (
        "create an account with a more distinctive name and delete this one "
        "(user names cannot be changed in the app; a directory account is "
        "renamed in the directory)"
    ),
    "host": "give that host a more distinctive name",
}


def generic_name_message(
    hits: Iterable[GenericNameHit], *, snapshot_optional: bool, reveal: bool = True
) -> str:
    """The inline form error for a D16 refusal: what, where, and how to fix it.

    :param reveal: name the values. Only for an administrator (and the root
        CLI): a viewer or operator would otherwise learn other users' names
        or names their role cannot see, so they get the kinds only.
    """
    hits = list(hits)
    if not reveal:
        return _generic_name_message_for_non_admin(hits, snapshot_optional)
    unique: dict[tuple[str, str], set[str]] = {}
    for hit in hits:
        unique.setdefault((hit.kind, hit.value), set()).add(hit.where)
    named = [
        f"the {KIND_LABELS.get(kind, kind)} “{value}” "
        f"(in {' and '.join(sorted(where))})"
        for (kind, value), where in list(unique.items())[:5]
    ]
    fixes = sorted({_FIXES.get(kind, "rename it") for kind, _ in unique})
    in_text = any(_WHERE_TEXT in where for where in unique.values())
    in_snapshot = any(_WHERE_SNAPSHOT in where for where in unique.values())
    parts = [
        "This report can't be prepared for GitHub, so nothing was saved: "
        + "; ".join(named)
        + (" is" if len(named) == 1 else " are")
        + " too short or too common to anonymize reliably, and would appear "
        "in the public issue.",
        "To fix it, an administrator can " + "; or ".join(fixes) + ".",
    ]
    if in_text:
        parts.append("Leave the name out of your title and description.")
    if in_snapshot and snapshot_optional:
        parts.append("You can also send the report without the diagnostic snapshot.")
    parts.append(
        "An administrator can instead turn the GitHub hand-off off "
        "(reporting.github.enabled) to file reports privately."
    )
    return " ".join(parts)


def _generic_name_message_for_non_admin(
    hits: list[GenericNameHit], snapshot_optional: bool
) -> str:
    """The D16 refusal without any value.

    Kinds are named only for names found in the snapshot. For a name in the
    reporter's own text even the kind is withheld: otherwise typing guesses
    would tell a viewer which words are user names on this install.
    """
    in_text = any(hit.where == _WHERE_TEXT for hit in hits)
    kinds = sorted(
        {KIND_LABELS.get(hit.kind, "name") for hit in hits if hit.where != _WHERE_TEXT}
    )
    parts = ["This report can't be prepared for GitHub, so nothing was saved."]
    if kinds:
        listed = " and ".join(f"a {kind}" for kind in kinds)
        parts.append(
            f"{listed[0].upper()}{listed[1:]} on this server "
            f"{'is' if len(kinds) == 1 else 'are'} too short or too generic to "
            "anonymize safely. Ask an administrator to rename them, or to file "
            "the report for you."
        )
        if snapshot_optional:
            parts.append(
                "You can also send the report without the diagnostic snapshot."
            )
    if in_text:
        parts.append(
            "Your title or description contains a short or generic name this "
            "server knows; leave it out."
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    """One identifying value the report must not carry.

    :param kind: ``user``, ``project``, ``camera``, ``host``, ``domain``,
        ``ip``, ``email``, ``directory`` (an LDAP DN) or ``path``.
    :param value: the value as the install holds it.
    :param weak: short or generic (D16): replaced in structured fields only.
    :param refusable: a weak hit refuses a GitHub report. ``False`` for the
        host's OS account in a CLI report, which the operator cannot rename
        from the app (a stock Raspberry Pi user is ``pi``).
    :param placeholder: a fixed placeholder (paths); else one is numbered.
    """

    kind: str
    value: str
    weak: bool = False
    refusable: bool = True
    placeholder: str | None = None


def is_weak(value: str) -> bool:
    """Whether a name is too short or generic to replace in free text (D16)."""
    folded = value.strip().casefold()
    return len(folded) < MIN_ENTITY_LENGTH or folded in STOP_WORDS or folded.isdigit()


def _is_public_ip(value: str) -> bool:
    """Loopback and unspecified addresses identify nothing."""
    try:
        address = ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _registrable_domain(labels: list[str]) -> str | None:
    """``b.example.com`` -> ``example.com`` (``x.co.uk`` keeps three labels)."""
    if len(labels) < 3:
        return None
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:]) if len(labels) >= 4 else None
    return ".".join(labels[-2:])


def _host_entities(host: str | None) -> list[Entity]:
    """A host and what identifies it: the FQDN, its first label, its domain."""
    if not host:
        return []
    host = host.strip().strip("[]").rstrip(".")
    folded = host.casefold()
    if (
        not folded
        or folded in _PUBLIC_HOSTS
        or _is_public_ip(folded)
        or folded.endswith(".arpa")
    ):
        return []
    if _is_ip(folded):
        return [Entity("ip", host)]
    labels = folded.split(".")
    found: list[Entity] = []
    if not folded.isdigit():
        found.append(Entity("host", host, weak=is_weak(host)))
    # The first label alone identifies the machine too ("nas101" of
    # "nas101.example.com"). A short or generic one ("timelapse" of
    # "timelapse.example.com", "camera" of "camera.lan") identifies nothing
    # and would refuse every report on words the app itself logs, so it is
    # dropped: the full name and its domain still cover the host.
    if len(labels) > 1 and labels[0] and not is_weak(labels[0]):
        found.append(Entity("host", labels[0]))
    domain = _registrable_domain(labels)
    if domain:
        found.append(Entity("domain", domain))
    return found


def _url_host(value: Any) -> str | None:
    """The host of a URL-ish string (or a bare host), else ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "://" not in text:
        # A bare "host" or "host:port".
        text = "//" + text
    try:
        return urllib.parse.urlsplit(text).hostname
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# A field holding a filesystem path: replaced outright when anonymizing.
_PATH_KEY = re.compile(
    r"(?:_path|_dir|_root|_file|_sink|_folder|_destination)\Z"
    r"|\A(?:path|dir|directory|destination|file|folder|cwd|filename)\Z"
)

# Absolute paths that identify nothing, kept so tracebacks stay useful. Each
# is fixed by the OS or the package, never chosen by the operator:
# - /usr/{bin,sbin,lib,lib64,libexec,share}/, /bin/, /sbin/, /lib/, /lib64/:
#   distribution-owned binaries and libraries (ffmpeg, the Python stdlib).
#   /usr/local is left out: an operator installs there and names things.
# - /opt/timelapse-manager/: the packaged bundle, identical on every install.
# - /proc/self/ and /dev/null: kernel interfaces with fixed names.
# /etc/timelapse-manager is deliberately *not* here: certificate and key file
# names under it are the operator's choice. /tmp is not here either.
SYSTEM_PATH_PREFIXES: tuple[str, ...] = (
    "/usr/bin/",
    "/usr/sbin/",
    "/usr/lib/",
    "/usr/lib64/",
    "/usr/libexec/",
    "/usr/share/",
    "/bin/",
    "/sbin/",
    "/lib/",
    "/lib64/",
    "/opt/timelapse-manager/",
    "/proc/self/",
)
_SYSTEM_PATHS = frozenset({"/dev/null"})

# First segments of the app's own URL paths (``GET /settings/reports``): kept
# in free text and in request ``path`` fields. tests/unit/
# test_report_anonymize_review.py checks this against the app's routes.
APP_ROUTE_ROOTS: frozenset[str] = frozenset(
    {
        "about",
        "account",
        "alerts",
        "api",
        "audit",
        "cameras",
        "dashboard",
        "docs",
        "drawers",
        "events",
        "first-run",
        "frames",
        "healthz",
        "login",
        "logout",
        "notification-settings",
        "openapi.json",
        "partials",
        "projects",
        "redoc",
        "renders",
        "report",
        "settings",
        "static",
        "users",
    }
)

_PATH_PLACEHOLDER_ROOT = "/path/"

# Directories whose contents the app names itself; below them only names
# are replaced (see :meth:`Pseudonymizer._absolute_path`).
APP_MANAGED_ROLES = frozenset({"data", "frames", "storage", "export", "logs"})


def _path_role_of(placeholder: str) -> str:
    """``/path/storage-2`` -> ``storage``."""
    name = placeholder[len(_PATH_PLACEHOLDER_ROOT) :]
    return re.sub(r"-\d+\Z", "", name)


# Literal path segments of the app's routes after the first (``reports`` in
# ``/settings/reports``). A path in free text is the app's own only when it has
# exactly a route's shape: a known root, then only ids, report keys and these
# literals. ``/projects/AcmeCorp/out/x.mp4`` is a directory, not a route.
# tests/unit/test_report_anonymize_review.py checks this against the routes.
ROUTE_SEGMENTS: frozenset[str] = frozenset(
    {
        "add-form",
        "admin-read",
        "alerts",
        "archive",
        "archive-confirm",
        "audit",
        "batch",
        "bulk",
        "camera-credentials",
        "cameras",
        "cancel",
        "capture",
        "capture-status",
        "card",
        "clear",
        "clear-all",
        "clone",
        "combo-check",
        "confirm-cancel",
        "count",
        "create-issue",
        "debug",
        "delete",
        "delete-confirm",
        "detect-protocol",
        "disable",
        "discover",
        "download",
        "drawer",
        "edit",
        "edit-form",
        "enable",
        "exclude",
        "export",
        "fetch-certificate",
        "form",
        "frames",
        "geolocation",
        "image",
        "import",
        "include",
        "ldap",
        "list",
        "log-level",
        "materialize",
        "metadata",
        "metrics",
        "milestones",
        "new",
        "new-project",
        "new-user",
        "oauth2-redirect",
        "offset",
        "password",
        "pause",
        "permanent-delete",
        "pin",
        "preferences",
        "preflight",
        "projects",
        "ptz-presets",
        "query",
        "range",
        "render-activity",
        "renders",
        "report",
        "reports",
        "resend",
        "reset-password",
        "restore",
        "resume",
        "revoke-sessions",
        "ribbon",
        "runners",
        "settings",
        "since",
        "soft-delete",
        "solar-preview",
        "ssrf",
        "start",
        "status",
        "stop",
        "stream",
        "stream-profiles",
        "suggested-fps",
        "summary",
        "system",
        "table",
        "tasks",
        "test-connection",
        "theme",
        "threads",
        "thumbnail",
        "timezone",
        "tls",
        "unpin",
        "upload",
        "v1",
        "validate",
    }
)
_ROUTE_SEGMENT = re.compile(r"\d+|TLM-\d{8}-(?:\d{4}|CLI\d{6})")


def _is_route_shape(path: str) -> bool:
    """Whether ``path`` is exactly one of the app's URL paths.

    A query or fragment may follow, but not one holding a ``/``:
    ``/projects/3?x=/mnt/secret/dir`` is not the app's own.
    """
    route, _, query = path.partition("?")
    route, _, fragment = route.partition("#")
    if "/" in query or "/" in fragment or "/" in path.partition("#")[2]:
        return False
    segments = route.strip("/").split("/")
    if not segments or segments[0] not in APP_ROUTE_ROOTS:
        return False
    return all(
        segment in ROUTE_SEGMENTS or _ROUTE_SEGMENT.fullmatch(segment)
        for segment in segments[1:]
    )


def _is_allowed_path(path: str, *, app_routes: bool = True) -> bool:
    """A generic system path, or (in free text) exactly one of the app's routes.

    A ``.`` or ``..`` segment is never allowed: ``/opt/timelapse-manager/../..``
    must not ride on an allowed prefix.
    """
    if any(segment in (".", "..") for segment in path.split("/")):
        return False
    if path in _SYSTEM_PATHS or path.startswith(SYSTEM_PATH_PREFIXES):
        return True
    return app_routes and _is_route_shape(path)


def _path_role(key: str) -> str:
    """``cert_path`` -> ``cert``; ``destination`` -> ``export``."""
    if key == "destination":
        return "export"
    role = re.sub(r"(?:_path|_dir|_root|_file|_sink|_folder)\Z", "", key)
    return role.replace("_", "-") or "path"


def _path_values(value: Any, key: str | None = None) -> Iterator[tuple[str, str]]:
    """``(role, path)`` for every path-keyed string inside a JSON-like value."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _path_values(child, str(child_key))
    elif isinstance(value, list):
        for item in value:
            yield from _path_values(item, key)
    elif (
        isinstance(value, str)
        and value.strip()
        and key is not None
        and _PATH_KEY.search(key)
    ):
        yield _path_role(key), value.strip()


def _path_entities_from(candidates: Iterable[tuple[str, str]]) -> list[Entity]:
    """Path entities with readable fixed placeholders (``/path/cert``)."""
    found: list[Entity] = []
    used: dict[str, int] = {}
    seen: set[str] = set()
    for role, raw in candidates:
        text = raw.rstrip("/\\") or raw
        if (
            text in seen
            or len(text) < 5
            or text.casefold() in _GENERIC_PATHS
            or not text.startswith(("/", "~"))
        ):
            continue
        seen.add(text)
        used[role] = used.get(role, 0) + 1
        name = role if used[role] == 1 else f"{role}-{used[role]}"
        found.append(
            Entity("path", text, placeholder=f"{_PATH_PLACEHOLDER_ROOT}{name}")
        )
    return found


def _settings_paths(settings: Settings) -> Iterator[tuple[str, str]]:
    """The install's own directories and files from the settings."""
    from sqlalchemy.engine import make_url

    preferred = [
        ("data", settings.paths.data_dir),
        ("frames", settings.paths.frames_root),
    ]
    for role, path in preferred:
        if path is not None:
            yield role, str(path)
            yield role, str(Path(path).expanduser())
    sink = settings.logging.file_sink
    if sink is not None:
        yield "logs", str(Path(sink).parent)
    database = make_url(settings.database.url).database
    if database and database != ":memory:":
        yield "db", database
    with contextlib.suppress(RuntimeError, KeyError):
        yield "home", str(Path.home())
    # Every other path-valued setting: TLS cert/key, font, key and token
    # files, the log file, export destinations in config-file projects, ...
    yield from _path_values(settings.model_dump(mode="json"))


def _db_paths(session: Session) -> Iterator[tuple[str, str]]:
    """Project storage and export destinations, and the LDAP CA file."""
    from ..db.models import LdapSettings, Project

    for storage, actions in session.execute(
        select(Project.storage_path, Project.post_render_actions)
    ):
        if storage:
            yield "storage", storage
        yield from _path_values(actions, None)
    row = session.get(LdapSettings, 1)
    if row is not None and row.tls_ca_cert_path:
        yield "ldap-ca", row.tls_ca_cert_path


# ---------------------------------------------------------------------------
# Entity sources
# ---------------------------------------------------------------------------


def _collect(
    sink: list[Entity], fetch: Callable[[], Iterable[Entity]], label: str
) -> None:
    """Add what ``fetch`` returns. A failing source refuses the report.

    Fail closed: a source that cannot be read here may still have been read
    into the bundle (an unreadable ``user`` table does not stop the log from
    naming users), so its names would go out unreplaced.
    """
    try:
        sink.extend(fetch())
    except Exception as exc:
        logger.warning(
            "report anonymizer: %s could not be read (%s); refusing",
            label,
            type(exc).__name__,
        )
        raise ReportAnonymizationError(GENERIC_REFUSAL) from exc


def _users(session: Session) -> Iterator[Entity]:
    from ..db.models import User

    for user_id, username in session.execute(select(User.id, User.username)):
        if (user_id, username) == _SENTINEL_USER or not username:
            continue
        yield Entity("user", username, weak=is_weak(username))


def _projects(session: Session) -> Iterator[Entity]:
    from ..db.models import Project

    for name in session.execute(select(Project.name)).scalars():
        if name:
            yield Entity("project", name, weak=is_weak(name))


def _cameras(session: Session) -> Iterator[Entity]:
    from ..db.models import Camera

    rows = session.execute(
        select(
            Camera.name,
            Camera.address,
            Camera.device_hostname,
            Camera.snapshot_uri,
            Camera.stream_uri,
        )
    )
    for name, address, device_hostname, snapshot_uri, stream_uri in rows:
        if name:
            yield Entity("camera", name, weak=is_weak(name))
        for value in (address, device_hostname, snapshot_uri, stream_uri):
            yield from _host_entities(_url_host(value))


def _ldap(session: Session) -> Iterator[Entity]:
    from ..db.models import LdapSettings

    row = session.get(LdapSettings, 1)
    if row is None:
        return
    for url in row.server_urls or []:
        yield from _host_entities(_url_host(url))
    for name in (
        "bind_dn",
        "search_base",
        "group_search_base",
        "admin_group_dn",
        "operator_group_dn",
        "viewer_group_dn",
    ):
        value = getattr(row, name, None)
        if isinstance(value, str) and len(value.strip()) >= MIN_ENTITY_LENGTH:
            yield Entity("directory", value.strip())


def _notifications(session: Session) -> Iterator[Entity]:
    from ..db.models import NotificationSettings

    row = session.get(NotificationSettings, 1)
    if row is None:
        return
    yield from _host_entities(row.smtp_server)
    if row.smtp_username:
        if "@" in row.smtp_username:
            yield Entity("email", row.smtp_username)
        else:
            yield Entity("user", row.smtp_username, weak=is_weak(row.smtp_username))
    for address in [row.smtp_from_address, *(row.smtp_recipients or [])]:
        if isinstance(address, str) and "@" in address:
            yield Entity("email", address.strip())
            yield from _host_entities(address.rsplit("@", 1)[1])
    for url in row.webhook_urls or []:
        yield from _host_entities(_url_host(url))


def _settings_entities(settings: Settings) -> Iterator[Entity]:
    reporting = settings.reporting
    for url in (
        reporting.public_base_url,
        reporting.webhook_url,
        reporting.gitea.base_url,
    ):
        yield from _host_entities(_url_host(url))
    for address in reporting.email_to:
        if "@" in address:
            yield Entity("email", address)
            yield from _host_entities(address.rsplit("@", 1)[1])
    yield from _host_entities(settings.server.bind_address)
    for kind, entries in (("camera", settings.cameras), ("project", settings.projects)):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                yield Entity(kind, name.strip(), weak=is_weak(name))
            for key in ("url", "address", "host", "snapshot_uri", "stream_uri"):
                yield from _host_entities(_url_host(entry.get(key)))


def _machine() -> Iterator[Entity]:
    yield from _host_entities(socket.gethostname())
    yield from _host_entities(socket.getfqdn())


def collect_entities(
    session: Session | None,
    settings: Settings,
    *,
    os_user: str | None = None,
) -> list[Entity]:
    """Every identifying value this install knows, for one report.

    :param session: an open session for the database-held entities, or
        ``None`` when the database is unavailable.
    :param os_user: the host account filing a CLI report (``SUDO_USER``).
        When weak it is replaced in structured fields but never refuses a
        report: the operator cannot rename it from the app.
    :raises ReportAnonymizationError: when any source cannot be read.
    """
    found: list[Entity] = []
    paths: list[tuple[str, str]] = []

    def add_paths(fetch: Callable[[], Iterable[tuple[str, str]]]) -> list[Entity]:
        paths.extend(fetch())
        return []

    if session is not None:
        _collect(found, lambda: list(_users(session)), "users")
        _collect(found, lambda: list(_projects(session)), "projects")
        _collect(found, lambda: list(_cameras(session)), "cameras")
        _collect(found, lambda: list(_ldap(session)), "LDAP settings")
        _collect(found, lambda: list(_notifications(session)), "notification settings")
        _collect(found, lambda: add_paths(lambda: _db_paths(session)), "project paths")
    _collect(found, lambda: list(_settings_entities(settings)), "settings")
    _collect(found, lambda: list(_machine()), "host name")
    _collect(found, lambda: add_paths(lambda: _settings_paths(settings)), "paths")
    found.extend(_path_entities_from(paths))
    if os_user and os_user.strip():
        weak = is_weak(os_user)
        found.append(Entity("user", os_user.strip(), weak=weak, refusable=not weak))
    return [entity for entity in found if entity.value.strip()]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

# Not preceded / followed by a letter or digit (``_`` counts as a separator,
# so ``Roof_2026`` still loses ``Roof``). ``\b`` would fail before ``/``.
_START = r"(?<![^\W_])"
_END = r"(?![^\W_])"

# What separates the words of a name: "North Side", "North_Side", "NorthSide",
# "North  Side", "North\nSide" and "north-side" are the same project.
_NAME_SEPARATOR = r"(?:[\s_\-+]|%20)*"
_NAME_KINDS = frozenset({"user", "project", "camera"})

# Unicode-aware: ``jo@bücher.de`` is an address too.
# Unicode-aware: ``jo@bücher.de`` is an address too. Bounded so a long run of
# word characters without ``@`` cannot make matching quadratic.
_EMAIL = (
    r"(?<![\w.%+-])[\w.%+-]{1,256}@"
    r"[^\W_][\w-]{0,62}(?:\.[\w-]{1,63}){0,8}\.[^\W\d_]{2,24}(?![\w-])"
)
# Everything between ``://`` and the last ``@`` of the token (up to 512
# characters) is credentials, so ``rtsp://bob:pa/ss@cam`` loses ``pa/ss`` too.
_URL = (
    r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]{1,15}://)"
    r"(?P<userinfo>[^\s\"'<>]{0,512}@)?"
    r"(?P<urlhost>\[[0-9A-Fa-f:.%]{1,64}\]|[A-Za-z0-9._%~-]{1,253})"
)
_HOMEDIR = (
    # Case-sensitive: the app's own ``/users/3/edit`` is not a home directory.
    r"(?<![\w.-])(?P<homeroot>(?-i:/(?:home|Users|var/home)/))"
    r"(?P<homename>[^/\s\"'<>:\\\x00\x01\x02]+)"
)
# Absolute paths. A path's *last* segment ends at whitespace or punctuation.
# A segment followed by another ``/`` may hold single spaces and ``& ( ) , = #
# ?`` between its words, so ``/mnt/nas media/Smith Family/2026/f.jpg`` is one
# path, while ``/settings/reports and /projects/3`` stays two (the word after
# a space must itself reach the ``/``). `` HTTP/`` ends a path, so
# ``GET /settings HTTP/1.1`` keeps its route. Not after a word character: URL
# paths (``host-1/x``) and ratios (``1/2``) are not paths; ``file:///x`` is.
# Separators and segment characters are disjoint, so matching stays linear.
_SEG = r"[^\s/\"'<>:;\[\]{}|\\`*&(),=#?\x00\x01\x02]"
_SEPC = r"[\x20&(),=#?]"
_PUNC = r"[&(),=#?]"
_WORDS = rf"(?:(?!\x20HTTP/){_SEPC}{{1,3}}{_SEG}+)*"
_MID_SEG = rf"{_PUNC}{{0,3}}{_SEG}+{_WORDS}{_PUNC}{{0,3}}/"
_PATH_TAIL = rf"(?:{_MID_SEG})*{_SEG}*"
_ABS_PATH = rf"(?<![\w.~\-\x00\x01\x02])/(?={_SEG}|{_PUNC})(?:{_MID_SEG})*{_SEG}*"
# What may follow one of the install's paths and still be part of it.
_KNOWN_PATH_REST = rf"(?:{_WORDS}{_PUNC}{{0,3}}/{_PATH_TAIL})?"
# A quoted path is a path whole, spaces and all: ``-i '/srv/My Videos/in.mp4'``.
_QUOTED_PATH = r"(?<!\w)(?P<q>['\"])(?P<qbody>/[^'\"\n\x00]{1,4096})(?P=q)"
# ``dc=example,dc=com`` (LDAP), with or without spaces after commas.
_DC_VALUE = r"[^\s,=+;\"'<>\\\x00\x01\x02]+"
_DC_RUN = rf"(?<![\w-])dc\s*=\s*{_DC_VALUE}(?:\s*,\s*dc\s*=\s*{_DC_VALUE})*"
# A host named after a MAC, as Axis cameras are (``axis-b8a44fb48e95``).
_MAC_HOST = r"(?<![\w.-])[A-Za-z][A-Za-z0-9]{0,62}-[0-9A-Fa-f]{12}(?![\w-])"
_MAC = (
    r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-])"
    r"|(?<![0-9A-Fa-f.])[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}(?![0-9A-Fa-f.])"
    r"|(?<![0-9A-Fa-f-])[0-9A-Fa-f]{12}(?![0-9A-Fa-f-])"
)
_IPV6 = (
    r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}"
    r"(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]{0,4})(?:%[\w.-]{1,32})?(?![\w:])"
)
# Only digits and dots bound an address: ``cam_10.0.0.5`` still loses it.
_IPV4 = r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9]|\.\d)"
_PRIVATE_TLD_HOST = (
    r"(?<![\w.-])(?:[A-Za-z0-9-]{1,63}\.){1,16}"
    r"(?:lan|local|localdomain|home|internal|intranet|corp|home\.arpa)(?![\w.-])"
)


def _is_mac_like(text: str) -> bool:
    """A bare or dotted hex run is a MAC only with both letters and digits.

    A 12-digit number (a byte count) is not; a 12-letter hex word is rare.
    The colon and dash forms are always MACs.
    """
    if ":" in text or "-" in text:
        return True
    hexdigits = text.replace(".", "")
    return any(c.isdigit() for c in hexdigits) and any(c.isalpha() for c in hexdigits)


def _norm(text: str) -> str:
    """One key for every form of a value (escaped, encoded, cased, spaced)."""
    value = html.unescape(text)
    value = urllib.parse.unquote(value)
    if "\\" in value:
        with contextlib.suppress(ValueError):
            decoded = json.loads(f'"{value}"')
            if isinstance(decoded, str):
                value = decoded
    return re.sub(r"[\s_\-+]+", "", value.casefold())


def _forms(value: str) -> set[str]:
    """The forms a value can take: raw, JSON/URL/HTML-escaped, casefolded."""
    forms: set[str] = set()
    for base in {value, value.casefold()}:
        forms |= _variants(base)
        forms.add(html.escape(base, quote=True))
        forms.add(html.escape(base, quote=False))
    return {form for form in forms if form}


_SEP_TOKEN = "\x00SEP"


def _sequence(entity: Entity, form: str) -> list[str]:
    """A form as trie units: lowercase characters, and separators for names.

    A name tolerates any separator (or none) between its words, so "North
    Side" also matches ``North_Side``, ``NorthSide`` and ``East%20Side``.
    """
    if entity.kind in _NAME_KINDS:
        words = [word for word in re.split(r"[\s_\-]+|%20|\+", form) if word]
        if len(words) > 1:
            units: list[str] = []
            for index, word in enumerate(words):
                if index:
                    units.append(_SEP_TOKEN)
                units.extend(word.lower())
            return units
    return list(form.lower())


def _trie_regex(sequences: Iterable[list[str]]) -> str:
    """One regex for many literals, as a trie: shared prefixes match once.

    Python's ``re`` tries the branches of a plain alternation one by one, so
    9,000 names made every position cost 9,000 attempts. As a trie, each
    character is examined once per depth. Longer matches win: at every node
    the continuation is tried before the "end here" choice.
    """
    root: dict[str, Any] = {}
    for sequence in sequences:
        if not sequence:
            continue
        node = root
        for unit in sequence:
            node = node.setdefault(unit, {})
        node[""] = {}
    return _emit(root)


def _unit_regex(unit: str) -> str:
    return _NAME_SEPARATOR if unit == _SEP_TOKEN else re.escape(unit)


def _emit(node: dict[str, Any]) -> str:
    branches: list[str] = []
    # The separator matches zero characters, so it goes last: a real
    # character is always tried first. Longest-match is then enforced by
    # :meth:`Pseudonymizer._longest_name_end`, not by branch order alone.
    units = sorted(key for key in node if key and key != _SEP_TOKEN)
    if _SEP_TOKEN in node:
        units.append(_SEP_TOKEN)
    for unit in units:
        # Collapse single-child chains into a run, to keep nesting shallow.
        run = [unit]
        child = node[unit]
        while len(child) == 1 and "" not in child:
            (next_unit,) = child
            run.append(next_unit)
            child = child[next_unit]
        prefix = "".join(_unit_regex(part) for part in run)
        rest = _emit(child) if any(key for key in child) else ""
        if rest and "" in child:
            branches.append(f"{prefix}(?:{rest})?")
        elif rest:
            branches.append(
                f"{prefix}{rest}" if "|" not in rest else f"{prefix}(?:{rest})"
            )
        else:
            branches.append(prefix)
    if not branches:
        return ""
    return branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"


def _prefixes_with_longer(norms: Iterable[str]) -> frozenset[str]:
    """The names that are a proper prefix of a longer name (sorted scan)."""
    ordered = sorted(set(norms))
    return frozenset(
        current
        for current, following in zip(ordered, ordered[1:], strict=False)
        if following.startswith(current)
    )


# A single word between a path and the end of its line.
_TRAILING_WORD = re.compile(r"\x20[^\s'\"<>/]+[ \t\r]*")


def _entity_regex(entities: Iterable[Entity]) -> str:
    return _trie_regex(
        _sequence(entity, form) for entity in entities for form in _forms(entity.value)
    )


def _literal_alternation(values: Iterable[str]) -> str:
    unique = sorted(set(values), key=lambda value: (-len(value), value))
    return "|".join(re.escape(value) for value in unique)


_PLACEHOLDER_PREFIX = {"directory": "dn"}
_NEUTRAL_PREFIX = "name"


def _rank(entity: Entity) -> int:
    return (0 if entity.weak else 2) + (1 if entity.refusable else 0)


def _is_snake_key(key: str) -> bool:
    """An app field name (``storage_path``), never a user-chosen name."""
    return re.fullmatch(r"[a-z][a-z0-9_]*", key) is not None


class Pseudonymizer:
    """Replaces identifying values with stable placeholders, for one report.

    Two namespaces:

    * the **snapshot** (:meth:`text`, :meth:`member`): kinded placeholders
      (``user-1``, ``camera-2``, ``host-1``, ``/path/data``), stable within
      the report so the log stays readable;
    * the **reporter's own words** (:meth:`user_text`, for the title and
      description): every replaced value becomes a kind-less ``name-N`` with
      its own numbering. A reporter who types a guess sees only that it was
      replaced, not whether it is a user, a camera or a host, and cannot line
      ``name-1`` up with ``user-3`` in the zip.
    """

    def __init__(self, entities: Iterable[Entity]) -> None:
        self.entities: tuple[Entity, ...] = tuple(entities)
        self._by_norm: dict[str, Entity] = {}
        for entity in self.entities:
            for form in _forms(entity.value):
                key = _norm(form)
                existing = self._by_norm.get(key)
                # A strong reading of a value wins over a weak one, and a
                # refusable weak one over a non-refusable one.
                if existing is None or _rank(entity) > _rank(existing):
                    self._by_norm[key] = entity
        self._placeholders: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self._neutral: dict[tuple[str, str], str] = {}
        self.issued: set[str] = set()
        self._paths = [e for e in self.entities if e.kind == "path" and not e.weak]
        self._path_placeholders: set[str] = set()
        # Placeholder -> normalized source value, for the verifier's
        # independent "name cut short" check.
        self.sources: dict[str, str] = {}
        names = [e for e in self.entities if not e.weak and e.kind != "path"]
        self.name_norms: frozenset[str] = frozenset(
            _norm(form) for entity in names for form in _forms(entity.value)
        )
        self.max_raw = max(
            (len(form) for entity in names for form in _forms(entity.value)),
            default=0,
        )
        self.has_longer: frozenset[str] = _prefixes_with_longer(self.name_norms)
        self._pattern = self._compile()

    # -- placeholders ------------------------------------------------------

    def _assign(self, kind: str, value: str, fixed: str | None = None) -> str:
        key = (kind, _norm(value))
        placeholder = self._placeholders.get(key)
        if placeholder is None:
            if fixed is not None:
                placeholder = fixed
            elif kind == "fspath":
                self._counters[kind] = self._counters.get(kind, 0) + 1
                placeholder = f"{_PATH_PLACEHOLDER_ROOT}{self._counters[kind]}"
            else:
                self._counters[kind] = self._counters.get(kind, 0) + 1
                prefix = _PLACEHOLDER_PREFIX.get(kind, kind)
                placeholder = f"{prefix}-{self._counters[kind]}"
            self._placeholders[key] = placeholder
            self.issued.add(placeholder)
            self.sources[placeholder] = _norm(value)
            if placeholder.startswith(_PATH_PLACEHOLDER_ROOT):
                self._path_placeholders.add(placeholder)
        return placeholder

    def _entity_placeholder(self, entity: Entity) -> str:
        return self._assign(entity.kind, entity.value, entity.placeholder)

    def _entity_for(self, text: str) -> Entity | None:
        return self._by_norm.get(_norm(text))

    def _lookup(self, kind: str, raw: str) -> str:
        """The placeholder for ``raw``: its entity's if known, else a new one."""
        if raw in self.issued:
            return raw
        entity = self._entity_for(raw)
        if entity is not None:
            return self._entity_placeholder(entity)
        return self._assign(kind, raw)

    def _neutral_name(self, raw: str) -> str:
        entity = self._entity_for(raw)
        key = (entity.kind, entity.value) if entity else ("~", _norm(raw))
        name = self._neutral.get(key)
        if name is None:
            name = f"{_NEUTRAL_PREFIX}-{len(self._neutral) + 1}"
            self._neutral[key] = name
            self.issued.add(name)
            self.sources[name] = _norm(entity.value if entity else raw)
        return name

    def reporter_name(self, username: str | None) -> str | None:
        """The reporter's snapshot placeholder, for the stored row (#21)."""
        if not username:
            return username
        return self.whole_value(username) or self._assign("user", username)

    # -- matching ----------------------------------------------------------

    def _compile(self) -> re.Pattern[str]:
        strong = [entity for entity in self.entities if not entity.weak]
        paths = [entity for entity in strong if entity.kind == "path"]
        literals = [entity for entity in strong if entity.kind != "path"]
        domains = [entity.value for entity in strong if entity.kind == "domain"]
        parts = [
            rf"(?P<email>{_EMAIL})",
            rf"(?P<url>{_URL})",
            rf"(?P<qpath>{_QUOTED_PATH})",
        ]
        if paths:
            # The install's own paths first, longest first, with whatever
            # follows them (R1): ``/mnt/nas media/x/f.jpg`` is one match.
            parts.append(
                rf"(?P<path>(?P<known>{_entity_regex(paths)}){_END}"
                rf"(?P<rest>{_KNOWN_PATH_REST}))"
            )
        parts.append(rf"(?P<abspath>{_ABS_PATH})")
        parts.append(rf"(?P<dc>{_DC_RUN})")
        if domains:
            parts.append(
                rf"(?P<sub>(?<![\w.-])(?:[A-Za-z0-9-]{{1,63}}\.){{1,16}}"
                rf"(?:{_literal_alternation(domains)})(?![\w-]))"
            )
        if literals:
            parts.append(rf"(?P<ent>{_START}{_entity_regex(literals)}{_END})")
        parts += [
            rf"(?P<machost>{_MAC_HOST})",
            rf"(?P<privhost>{_PRIVATE_TLD_HOST})",
            rf"(?P<mac>{_MAC})",
            rf"(?P<ipv6>{_IPV6})",
            rf"(?P<ipv4>{_IPV4})",
        ]
        return re.compile("|".join(parts), re.IGNORECASE)

    def _host(self, host: str) -> str:
        """A URL host's replacement (loopback and placeholders are kept)."""
        bare = host.strip("[]")
        if (
            bare.casefold() in _PUBLIC_HOSTS
            or _is_public_ip(bare)
            or host in self.issued
        ):
            return host
        if _is_ip(bare):
            return self._lookup("ip", bare)
        return self._lookup("host", host)

    def _keeps(self, match: re.Match[str]) -> bool:
        """Whether a pattern match is left as it is (not private after all)."""
        group = match.lastgroup
        text = match.group(0)
        if group in ("ipv4", "ipv6"):
            address = text.split("%", 1)[0]
            return not _is_ip(address) or _is_public_ip(address)
        if group == "mac":
            return not _is_mac_like(text)
        if group == "abspath":
            # Already a placeholder (text anonymized twice), or generic.
            return self._known_path_prefix(text) is not None or _is_allowed_path(text)
        if group == "qpath":
            body = match.group("qbody")
            return self._known_path_prefix(body) is not None or _is_allowed_path(body)
        if group == "url":
            host = match.group("urlhost").strip("[]")
            return not match.group("userinfo") and (
                host.casefold() in _PUBLIC_HOSTS or _is_public_ip(host)
            )
        return False

    def _known_path_prefix(self, text: str) -> str | None:
        """The issued path placeholder ``text`` starts with, if any (O(1))."""
        if not text.startswith(_PATH_PLACEHOLDER_ROOT):
            return None
        head = text[len(_PATH_PLACEHOLDER_ROOT) :].split("/", 1)[0]
        candidate = _PATH_PLACEHOLDER_ROOT + head
        return candidate if candidate in self._path_placeholders else None

    def _replace(self, match: re.Match[str]) -> str:
        group = match.lastgroup
        text = match.group(0)
        if self._keeps(match):
            if group in ("abspath", "qpath"):
                # Kept path: still replace names along it.
                return self._names_only(text)
            return text
        if group == "email":
            return self._lookup("email", text)
        if group == "url":
            userinfo = ANONYMIZED_USERINFO if match.group("userinfo") else ""
            return match.group("scheme") + userinfo + self._host(match.group("urlhost"))
        if group == "qpath":
            quote = match.group("q")
            return quote + self._absolute_path(match.group("qbody")) + quote
        if group == "path":
            return self._known_path(match.group("known"), match.group("rest") or "")
        if group == "abspath":
            return self._absolute_path(text)
        if group == "dc":
            values = re.findall(rf"dc\s*=\s*({_DC_VALUE})", text, re.IGNORECASE)
            return "dc=" + self._lookup("domain", ".".join(values))
        if group in ("sub", "privhost", "ent", "machost"):
            return self._lookup("host", text)
        if group == "mac":
            return self._lookup("mac", text)
        return self._lookup("ip", text)

    def _replace_neutral(self, match: re.Match[str]) -> str:
        """The reporter's own words: any replacement is a kind-less ``name-N``."""
        if self._keeps(match):
            return match.group(0)
        return self._neutral_name(match.group(0))

    def _names_only(self, path: str) -> str:
        """Replace names inside each segment of a path, keeping its shape."""
        return "/".join(self._scan(part, neutral=False) for part in path.split("/"))

    def _known_path(self, known: str, rest: str) -> str:
        """One of the install's paths, plus what followed it.

        Below the data, frames, storage, export and log directories the app
        names things itself (``frames/2026/0001.jpg``), and only names are
        replaced. Below any other known path -- a home directory, a
        certificate directory -- every segment may be the operator's, so the
        whole path is replaced.
        """
        entity = self._entity_for(known)
        if entity is None:
            return self._lookup("fspath", known + rest)
        placeholder = self._entity_placeholder(entity)
        if not rest:
            return placeholder
        if rest.startswith("/") and _path_role_of(placeholder) in APP_MANAGED_ROLES:
            return placeholder + self._names_only(rest)
        return self._lookup("fspath", known + rest)

    def _absolute_path(self, text: str) -> str:
        """An absolute path that is not generic: known prefix, else replaced."""
        if self._known_path_prefix(text) is not None:
            return self._names_only(text)
        known = self._longest_known_path(text)
        if known is not None:
            entity, rest = known
            return self._known_path(entity.value, rest)
        return self._lookup("fspath", text)

    def _longest_known_path(self, text: str) -> tuple[Entity, str] | None:
        best: tuple[Entity, str] | None = None
        for entity in self._paths:
            value = entity.value
            matches = text == value or text.startswith(value + "/")
            if matches and (best is None or len(value) > len(best[0].value)):
                best = (entity, text[len(value) :])
        return best

    def text(self, value: str) -> str:
        """Anonymize snapshot free text: strong entities and the safety net."""
        return self._scan(value, neutral=False)

    def user_text(self, value: str) -> str:
        """Anonymize the reporter's title or description (neutral names)."""
        return self._scan(value, neutral=True)

    def _scan(self, value: str, *, neutral: bool) -> str:
        """``re.sub`` with two widenings a callback cannot make.

        * **Leftmost-longest names.** Python's alternation takes the first
          branch that matches, so "Back Yard" could win over "Backyard Smith
          Cam" at the same place. Every name match is widened to the longest
          known name starting there (:meth:`_longest_name_end`).
        * **Path tails.** An unknown path whose directories hold spaces runs
          to the end of the line (or closing quote), and a single word left
          between a path and the end of the line is taken with it: the
          *last* segment of an unquoted path cannot be told from prose, so
          the safe reading is the longer one.
        """
        replace = self._replace_neutral if neutral else self._replace
        out: list[str] = []
        pos = 0
        while True:
            match = self._pattern.search(value, pos)
            if match is None:
                break
            start, end = match.span()
            replacement: str | None = None
            group = match.lastgroup
            if group == "ent":
                longest = self._longest_name_end(value, start, end)
                if longest != end:
                    end = longest
                    raw = value[start:end]
                    replacement = (
                        self._neutral_name(raw)
                        if neutral
                        else self._lookup("host", raw)
                    )
            elif group in ("abspath", "path") and not self._keeps(match):
                # Spaces inside a known path are its own; look at the rest.
                known = match.group("known") if group == "path" else None
                tail = self._path_tail_end(
                    value, start + (len(known) if known else 0), end
                )
                if tail != end:
                    end = tail
                    raw = value[start:end]
                    replacement = (
                        self._neutral_name(raw)
                        if neutral
                        else self._lookup("fspath", raw)
                    )
            if replacement is None:
                replacement = replace(match)
            out.append(value[pos:start])
            out.append(replacement)
            if end == start:  # pragma: no cover - no alternative matches empty
                out.append(value[start : start + 1])
                end = start + 1
            pos = end
        out.append(value[pos:])
        return "".join(out)

    def _longest_name_end(self, value: str, start: int, end: int) -> int:
        """The end of the longest known name starting at ``start``."""
        if _norm(value[start:end]) not in self.has_longer:
            return end
        best = end
        limit = min(len(value), start + self.max_raw)
        for stop in range(end + 1, limit + 1):
            if stop < len(value) and value[stop].isalnum():
                continue
            if not _norm(value[stop - 1]):
                continue  # ends on a separator
            if _norm(value[start:stop]) in self.name_norms:
                best = stop
        return best

    @staticmethod
    def _path_tail_end(value: str, start: int, end: int) -> int:
        """Where an unknown path really ends (see :meth:`_scan`)."""
        line_end = len(value)
        for stop_char in ("\n", "'", '"', "<", ">"):
            found = value.find(stop_char, end)
            if found != -1:
                line_end = min(line_end, found)
        rest = value[end:line_end]
        if " " in value[start:end]:
            return end + len(rest.rstrip())
        if _TRAILING_WORD.fullmatch(rest):
            return end + len(rest.rstrip())
        return end

    def whole_value(self, value: str) -> str | None:
        """The placeholder if ``value`` is exactly an entity (weak ones too)."""
        stripped = value.strip()
        if stripped in self.issued:
            return stripped
        entity = self._entity_for(stripped)
        if entity is None:
            return None
        return self._entity_placeholder(entity)

    # -- structured --------------------------------------------------------

    def _key(self, key: str) -> str:
        entity = self._entity_for(key.strip())
        if entity is None or (entity.weak and _is_snake_key(key)):
            return key
        return self._entity_placeholder(entity)

    def _path_field(self, key: str, value: str) -> str:
        """A path-valued field: kept only when its anonymized form is generic."""
        anonymized = self.text(value)
        if "/" not in anonymized and "\\" not in anonymized:
            return anonymized
        if anonymized.startswith("/") and (
            self._known_path_prefix(anonymized) is not None
            or _is_allowed_path(anonymized, app_routes=key == "path")
        ):
            return anonymized
        return self._lookup("fspath", value)

    def _walk(self, value: Any, key: str | None) -> Any:
        if key is not None and _GEO_KEY.search(key):
            return None
        if isinstance(value, dict):
            # Keys are the app's own field names; one that is wholly an
            # entity (a dict keyed by camera name) is replaced.
            return {
                (self._key(k) if isinstance(k, str) else k): self._walk(v, str(k))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._walk(item, key) for item in value]
        if isinstance(value, str):
            if key in IDENTITY_KEYS:
                whole = self.whole_value(value)
                if whole is not None:
                    return whole
            if key is not None and _PATH_KEY.search(key):
                return self._path_field(key, value)
            return self.text(value)
        return value

    def structured(self, value: Any) -> Any:
        """Anonymize a JSON-like value, replacing weak names in identity fields."""
        return self._walk(value, None)

    def member(self, name: str, data: bytes) -> bytes:
        """Anonymize one (already secret-scrubbed) zip member.

        JSON members are parsed and walked, so a weak name is replaced where
        it is a whole ``username``/``name``/``hostname`` value and a path field
        is replaced outright; text members get the free-text pass.
        Undecodable bytes raise: the caller refuses.
        """
        if name.endswith(".json"):
            parsed = json.loads(data.decode("utf-8"))
            return json.dumps(
                self.structured(parsed), indent=2, default=str, ensure_ascii=False
            ).encode("utf-8")
        return self.text(data.decode("utf-8")).encode("utf-8")

    def verifier(self, scrubber: Scrubber, *, refuse_weak: bool) -> Verifier:
        """A :class:`Verifier` over this report's entities and secrets."""
        return Verifier(self, scrubber, refuse_weak=refuse_weak)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

_MASK = "\x00"
# Markers for masked path placeholders: app-managed directories (contents may
# follow) and any other path (nothing may follow).
_PATH_MARK = "\x01"
_PATH_MARK_OTHER = "\x02"
# Every placeholder the pseudonymizer issues has one of these shapes; a match
# is blanked only if it was actually issued (a set lookup, not a regex over
# thousands of placeholders).
_NAME_PLACEHOLDER = re.compile(r"(?<![^\W_])[a-z]+-\d+(?![^\W_])")
_PLACEHOLDER_SHAPE = re.compile(
    r"(?<![^\W_])[a-z]+-\d+(?![^\W_])|/path/[a-z0-9-]+(?:/[^\s\"'<>]*)?"
)


class Verifier:
    """Re-scans anonymized output; any private value left refuses the report.

    Checks, each on the output with issued placeholders blanked (``camera-1``
    must not match a camera called ``camera``):

    1. **secrets** on the raw bytes (any encoding the scrubber knows);
    2. **strong entities**, from the install's values;
    3. **generic leftovers**, independent of what the install knows: any
       e-mail, MAC (colon, bare or dotted), IPv4/IPv6 address, home directory,
       private-TLD host, URL host or URL credentials, ``dc=`` run or absolute
       path that is not a placeholder or on the short allow-lists above;
    4. **weak entities** (D16) on free text and non-field-name keys, only when
       ``refuse_weak``: collected and raised as :class:`_PendingGenericNames`
       so :func:`verify_all` can name them all at once.
    """

    def __init__(
        self, pseudonymizer: Pseudonymizer, scrubber: Scrubber, *, refuse_weak: bool
    ) -> None:
        self._pseudonymizer = pseudonymizer
        self._scrubber = scrubber
        self._refuse_weak = refuse_weak
        strong = [entity for entity in pseudonymizer.entities if not entity.weak]
        strong_norms = {
            _norm(form) for entity in strong for form in _forms(entity.value)
        }
        self._strong_norms = strong_norms
        weak = [
            entity
            for entity in pseudonymizer.entities
            if entity.weak
            and entity.refusable
            and _norm(entity.value) not in strong_norms
        ]
        self._weak = {
            _norm(form): entity for entity in weak for form in _forms(entity.value)
        }
        strong_regex = _entity_regex(strong)
        self._strong = (
            re.compile(rf"{_START}{strong_regex}{_END}", re.IGNORECASE)
            if strong_regex
            else None
        )
        weak_regex = _entity_regex(weak)
        self._weak_pattern = (
            re.compile(rf"{_START}{weak_regex}{_END}", re.IGNORECASE)
            if weak_regex
            else None
        )
        self._leftovers = re.compile(
            "|".join(
                [
                    rf"(?P<email>{_EMAIL})",
                    r"(?P<urlcreds>\b[A-Za-z][A-Za-z0-9+.-]{0,15}://"
                    r"(?P<ui>[^\s\"'<>]{0,512})@)",
                    r"(?P<urlhost>\b[A-Za-z][A-Za-z0-9+.-]{0,15}://"
                    r"(?P<h>\[[^\]\s]{0,64}\]|[A-Za-z0-9._%~-]{1,253}))",
                    rf"(?P<homedir>{_HOMEDIR})",
                    # A path tail after a path placeholder (R1): the
                    # replacer swallowed only part of a spaced path.
                    rf"(?P<pathtail>[{_PATH_MARK}{_PATH_MARK_OTHER}]"
                    rf"(?:(?:(?!\x20HTTP/){_SEPC}{{1,3}}{_SEG}+)+{_PUNC}{{0,3}}"
                    rf"|{_PUNC}{{1,3}})/)",
                    # Below a path that is not app-managed nothing may follow.
                    rf"(?P<pathsub>{_PATH_MARK_OTHER}/)",
                    # A word left between a path and the end of its line
                    # (the replacer always takes it with the path).
                    rf"(?P<patheol>[{_PATH_MARK}{_PATH_MARK_OTHER}](?:/|{_SEG}){{0,512}}"
                    r"\x20[^\s'\"<>/]+[ \t\r]*$)",
                    rf"(?P<abspath>{_ABS_PATH})",
                    rf"(?P<dc>(?<![\w-])dc\s*=\s*{_DC_VALUE})",
                    rf"(?P<machost>{_MAC_HOST})",
                    rf"(?P<mac>{_MAC})",
                    rf"(?P<ipv6>{_IPV6})",
                    rf"(?P<ipv4>{_IPV4})",
                    rf"(?P<privhost>{_PRIVATE_TLD_HOST})",
                ]
            ),
            re.IGNORECASE | re.MULTILINE,
        )

    def _mask(self, text: str) -> str:
        """Blank out issued placeholders, by shape, checked against the set.

        Path placeholders get a marker of their own (app-managed vs other),
        so the path checks can tell what may follow them.
        """
        issued = self._pseudonymizer.issued
        if not issued:
            return text

        def blank(match: re.Match[str]) -> str:
            value = match.group(0)
            if value not in issued:
                # A longer path-ish run: blank only the placeholder part.
                head = self._pseudonymizer._known_path_prefix(value)
                if head is None:
                    return value
                return self._path_mark(head) + value[len(head) :]
            if value.startswith(_PATH_PLACEHOLDER_ROOT):
                return self._path_mark(value)
            return _MASK

        return _PLACEHOLDER_SHAPE.sub(blank, text)

    @staticmethod
    def _path_mark(placeholder: str) -> str:
        managed = _path_role_of(placeholder) in APP_MANAGED_ROLES
        return _PATH_MARK if managed else _PATH_MARK_OTHER

    def _leftover(self, text: str) -> str | None:
        """The kind of the first private pattern still present, else ``None``."""
        for match in self._leftovers.finditer(text):
            group = match.lastgroup or "pattern"
            value = match.group(0)
            if group in ("ipv4", "ipv6"):
                address = value.split("%", 1)[0]
                if not _is_ip(address) or _is_public_ip(address):
                    continue
            elif group == "mac" and not _is_mac_like(value):
                continue
            elif group == "urlcreds":
                if match.group("ui") in ("«anonymized»", "***"):
                    continue
            elif group == "urlhost":
                host = match.group("h").strip("[]")
                if host.casefold() in _PUBLIC_HOSTS or _is_public_ip(host):
                    continue
            elif group == "abspath" and _is_allowed_path(value):
                continue
            return group
        return None

    def _fail(self, where: str, what: str) -> None:
        # Never log the value itself: the log can end up in the next report.
        logger.warning("anonymized report refused: %s found in %s", what, where)
        raise ReportAnonymizationError(GENERIC_REFUSAL)

    def _weak_hits(self, where: str, values: Iterable[str]) -> list[GenericNameHit]:
        if not self._refuse_weak or self._weak_pattern is None:
            return []
        # report.json holds the reporter's own title and description (its
        # username field is replaced whole), so its hits are their text.
        own = ("title", "description", "report.json")
        scope = _WHERE_TEXT if where in own else _WHERE_SNAPSHOT
        hits: list[GenericNameHit] = []
        for value in values:
            for match in self._weak_pattern.finditer(self._mask(value)):
                found = match.group(0)
                entity = self._weak.get(_norm(found))
                hits.append(
                    GenericNameHit(
                        entity.kind if entity else "name",
                        entity.value if entity else found,
                        scope,
                    )
                )
        return hits

    def _cut_short(self, value: str) -> bool:
        """Whether a placeholder plus what follows it spells a longer name.

        Independent of the replacer's matcher: it reads the placeholder's
        source value and the raw text after it, so a shorter name winning
        over a longer one ("Back Yard" in "Backyard Smith Cam") is caught.
        """
        pseudo = self._pseudonymizer
        if not pseudo.has_longer:
            return False
        for match in _NAME_PLACEHOLDER.finditer(value):
            source = pseudo.sources.get(match.group(0))
            if source is None or source not in pseudo.has_longer:
                continue
            window = value[match.end() : match.end() + pseudo.max_raw]
            for stop in range(1, len(window) + 1):
                if stop < len(window) and window[stop].isalnum():
                    continue
                if source + _norm(window[:stop]) in pseudo.name_norms and _norm(
                    window[:stop]
                ):
                    return True
        return False

    def _check_strings(
        self,
        where: str,
        strings: Iterable[str],
        free_text: Iterable[str],
        keys: Iterable[str] = (),
    ) -> None:
        for value in strings:
            if self._cut_short(value):
                self._fail(where, "a known name cut short")
            masked = self._mask(value)
            if self._strong is not None and self._strong.search(masked):
                self._fail(where, "a known name")
            leftover = self._leftover(masked)
            if leftover is not None:
                self._fail(where, f"an unreplaced {leftover}")
        hits = self._weak_hits(where, free_text)
        for key in keys:
            if key.strip() in self._pseudonymizer.issued:
                continue
            if _norm(key) in self._strong_norms:
                self._fail(where, "a known name as a key")
            if not _is_snake_key(key):
                hits += self._weak_hits(where, [key])
        if hits:
            raise _PendingGenericNames(hits)

    def check_text(self, where: str, text: str) -> None:
        """Refuse if ``text`` still holds a secret, an entity or a private pattern.

        :param where: ``title``, ``description``, a member name or ``url``; it
            goes to the log line and decides the D16 message's wording.
        """
        if self._scrubber.contains(text.encode("utf-8")):
            self._fail(where, "a secret")
        self._check_strings(where, [text], [text])

    def check_member(self, name: str, data: bytes) -> None:
        """Check one finished zip member.

        A JSON member is checked leaf by leaf: every string value for strong
        names and patterns, every key for being an entity, and the
        non-enumeration values for weak names.
        """
        if self._scrubber.contains(data):
            self._fail(name, "a secret")
        text = data.decode("utf-8")
        if not name.endswith(".json"):
            self._check_strings(name, [text], [text])
            return
        keys, strings, free = _json_strings(json.loads(text))
        self._check_strings(name, strings, free, keys)

    def check_url(self, url: str) -> None:
        """Check the GitHub URL: encoded as a whole, and its decoded query."""
        if self._scrubber.contains(url.encode("utf-8")):
            self._fail("url", "a secret")
        masked = self._mask(url)
        if self._strong is not None and self._strong.search(masked):
            self._fail("url", "a known name")
        query = urllib.parse.urlsplit(url).query
        for name, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
            self.check_text("description" if name == "body" else name, value)


def _json_strings(value: Any) -> tuple[list[str], list[str], list[str]]:
    """A JSON value's keys, all string leaves, and the free-text leaves."""
    keys: list[str] = []
    strings: list[str] = []
    free: list[str] = []

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, dict):
            for child_key, child in node.items():
                keys.append(str(child_key))
                walk(child, str(child_key))
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str):
            strings.append(node)
            if key not in ENUM_KEYS:
                free.append(node)

    walk(value, None)
    return keys, strings, free


class _PendingGenericNames(Exception):
    """Internal: weak names found; collected across checks by :func:`verify_all`."""

    def __init__(self, hits: list[GenericNameHit]) -> None:
        super().__init__("generic names")
        self.hits = hits


def verify_all(
    checks: Iterable[Callable[[], None]], *, snapshot_optional: bool
) -> None:
    """Run every check; a secret/strong leak refuses at once, weak names together.

    Collecting the weak-name hits across the title, description and every
    member lets the form name all of them in one message.

    :raises ReportAnonymizationError: on any leak or failure.
    :raises GenericNameError: when only short or generic names remain (D16).
    """
    hits: list[GenericNameHit] = []
    for check in checks:
        try:
            check()
        except _PendingGenericNames as pending:
            hits.extend(pending.hits)
    if hits:
        raise GenericNameError(hits, snapshot_optional=snapshot_optional)
