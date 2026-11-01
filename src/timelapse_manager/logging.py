"""Logging configuration built on the standard library.

Provides a JSON or plain-text formatter selectable from settings, redaction of
secret-looking values in structured fields, an optional file sink alongside the
console, an in-memory ring buffer of recent records for problem reports, and a
one-line startup banner. No third-party logging dependency is used.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    from .config.settings import Settings

# The current request's correlation id, set by the web request-id middleware for
# the duration of a request and read back by :class:`RequestIdFilter` so every
# log record emitted while handling that request carries the id. ``None`` outside
# a request (startup, background loops), in which case no id is stamped.
#
# This lives in ``logging`` rather than the web package so ``configure_logging``
# can attach the filter without importing ``web`` -- importing the web package
# pulls in the router/middleware stack, which imports back into ``logging`` (via
# ``monitoring.events``), a cycle. The web middleware imports these names from
# here instead.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


class RequestIdFilter(logging.Filter):
    """Stamp the current request's correlation id onto each log record.

    Attached to every handler ``configure_logging`` installs. When a request is
    in flight its id is set on :data:`request_id_ctx`; this copies it onto the
    record as ``request_id`` (unless the record already carries one, e.g. an
    explicit ``extra=``) so the JSON formatter emits it as a field. Records
    outside any request are left untouched. Always returns ``True`` -- it
    enriches records, it never filters them out.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        rid = request_id_ctx.get()
        if rid is not None and not hasattr(record, "request_id"):
            record.request_id = rid
        return True


# Field names whose values are masked in structured log output. Matched
# case-insensitively as a substring, so "smtp_password" and "api_key" both hit.
_SECRET_NAME_PATTERN = re.compile(r"password|token|secret|key", re.IGNORECASE)
_REDACTED = "***"

# Credentials embedded in a URL's userinfo component, e.g.
# ``rtsp://user:pass@host`` or ``https://token@host``. The scheme and host are
# preserved; the credentials are replaced. Applied to both free-text messages
# and string values inside structured fields so an embedded camera/SMTP/webhook
# credential never reaches the log or, via log_event, the database.
# The userinfo runs to the *last* ``@`` before the path, so a password that holds
# an unescaped ``@`` (``rtsp://user:p@ss@host/``) is masked whole, not up to its
# first ``@`` with the rest left readable.
# This permissive class is the default and fails closed: it runs to the last ``@``
# before the next ``/`` or whitespace whatever characters (quotes, brackets,
# backslashes) the password holds, so a plain-text password is never left
# partly readable. Over-redacting is acceptable; leaking is not.
_URL_USERINFO_PATTERN = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s]+@")

# The same, bounded for text *known* to be a serialised JSON or repr structure
# (``redact_text(..., serialized=True)``): it stops at a double quote, whitespace
# and angle brackets so the match ends with the string it sits in instead of
# running on to an ``@`` in the next field (``{"u":"rtsp://user:pw@h","e":"a@b"}``,
# #224). A backslash escape (``\"``) is consumed as one unit, so a password with an
# escaped quote is masked whole. It must never be applied to plain text, where an
# unescaped quote in a password would end the match early and leak the rest.
_SERIALIZED_USERINFO_PATTERN = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?:\\.|[^/\s\"\\<>])+@"
)

# Secret-bearing query-string parameters, e.g. ``?token=abc&access_key=xyz``. A
# webhook URL can carry a credential in its query string that userinfo redaction
# does not cover, so the *value* of any parameter whose name looks like a secret
# (token/secret/key/password/sig/...) is masked while the parameter name and the
# rest of the URL are preserved for diagnosis. The value stops at the next
# query/fragment separator (``&``/``#``), whitespace, or a string/markup delimiter
# (quote, brace, bracket, angle bracket) so a URL embedded in JSON or a quoted log
# field has only its value masked, not the surrounding ``"}`` punctuation.
_QUERY_SECRET_PATTERN = re.compile(
    r"(?P<sep>[?&])(?P<key>[\w.\-]*"
    r"(?:token|secret|key|password|passwd|pwd|sig|signature|auth|credential)"
    r"[\w.\-]*)=(?P<value>[^&#\s\"'}\])>]+)",
    re.IGNORECASE,
)

# Standard LogRecord attributes; anything else on a record is treated as a
# structured extra and included (after redaction) in JSON output.
_RESERVED_RECORD_KEYS = frozenset(
    logging.makeLogRecord({}).__dict__.keys() | {"message", "asctime", "taskName"}
)


def _is_secret_key(name: str) -> bool:
    """Return True if a field name looks like it holds a secret."""
    return _SECRET_NAME_PATTERN.search(name) is not None


def redact_text(text: str, *, serialized: bool = False) -> str:
    """Mask credentials embedded in any URL within a free-text string.

    Two classes of credential are scrubbed while the rest of the URL is kept for
    diagnosis:

    * **Userinfo** -- ``rtsp://user:pass@host`` becomes ``rtsp://***@host``.
    * **Secret query parameters** -- ``https://h/x?token=abc`` becomes
      ``https://h/x?token=***``; the value of any parameter whose name looks like
      a secret (``token``/``secret``/``key``/``password``/``sig``/...) is masked.

    ``serialized=True`` is for text the caller *knows* is a serialised JSON or repr
    structure: the userinfo match then stops at the string's own quote (#224). The
    default treats the text as plain and masks through the last ``@``.

    Non-URL text is returned unchanged. Used for log message bodies and by the
    event logger before a message is persisted.
    """
    pattern = _SERIALIZED_USERINFO_PATTERN if serialized else _URL_USERINFO_PATTERN
    text = pattern.sub(r"\g<scheme>***@", text)
    return _QUERY_SECRET_PATTERN.sub(r"\g<sep>\g<key>=" + _REDACTED, text)


def redact(value: Any) -> Any:
    """Recursively mask secrets in an arbitrary structured value.

    Values stored under a secret-looking key (``password``/``token``/...) are
    fully masked; string values anywhere have any embedded URL credentials
    scrubbed via :func:`redact_text`. Dicts and lists/tuples are walked
    recursively; other scalars pass through unchanged.
    """
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_secret_key(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


# Backwards-compatible private alias for existing internal call sites.
_redact = redact


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects.

    Standard fields (timestamp, level, logger, message) are always present;
    structured extras passed via ``logging``'s ``extra=`` are appended with
    secret-looking fields redacted.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS:
                continue
            payload[key] = _REDACTED if _is_secret_key(key) else redact(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# In-memory ring buffer (#8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BufferedLogLine:
    """One log record as the ring buffer keeps it.

    Only the already-formatted, redacted text is kept -- never the
    ``LogRecord`` itself, which can pin exception frames (and their locals) in
    memory for as long as the record sits in the buffer.
    """

    created: float
    request_id: str | None
    text: str


class RingBufferHandler(logging.Handler):
    """Keep the last ``capacity`` log records in memory for problem reports.

    A problem report includes the app's own log around the reported time. The
    systemd sandbox may not let the service read the journal, so this handler
    keeps a bounded copy in process. Each record is rendered as one plain-text
    line with the message (and any traceback) run through :func:`redact_text`,
    so the buffer never holds a URL credential even when the configured
    console format is the non-redacting text format.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._lines: deque[BufferedLogLine] = deque(maxlen=max(1, capacity))
        self._lines_lock = threading.Lock()
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )

    def resize(self, capacity: int) -> None:
        """Change the capacity, keeping the newest records that still fit."""
        with self._lines_lock:
            self._lines = deque(self._lines, maxlen=max(1, capacity))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self._render(record)
            rid = getattr(record, "request_id", None)
            line = BufferedLogLine(
                created=record.created,
                request_id=rid if isinstance(rid, str) else None,
                text=text,
            )
            with self._lines_lock:
                self._lines.append(line)
        except Exception:  # noqa: BLE001 - a log handler must never raise
            self.handleError(record)

    def _render(self, record: logging.LogRecord) -> str:
        """One redacted line: time, level, logger, request id, message."""
        when = datetime.fromtimestamp(record.created, UTC).isoformat(
            timespec="milliseconds"
        )
        rid = getattr(record, "request_id", None)
        rid_part = f" [{rid}]" if isinstance(rid, str) else ""
        text = (
            f"{when} {record.levelname} {record.name}{rid_part} "
            f"{redact_text(record.getMessage())}"
        )
        if record.exc_info:
            formatter = self.formatter or logging.Formatter()
            text += "\n" + redact_text(formatter.formatException(record.exc_info))
        return text

    def lines(self) -> list[BufferedLogLine]:
        """A copy of the buffered lines, oldest first."""
        with self._lines_lock:
            return list(self._lines)


# The process-wide ring buffer. ``configure_logging`` runs more than once per
# process (the factory path and the lifespan both call it, and each test does),
# and it removes every root handler each time, so the buffer is a singleton that
# is re-attached (and resized) on every call rather than rebuilt and emptied.
_ring_buffer: RingBufferHandler | None = None


def get_ring_buffer() -> RingBufferHandler | None:
    """Return the installed ring buffer, or ``None`` when it is disabled."""
    return _ring_buffer


def _install_ring_buffer(capacity: int, request_id_filter: logging.Filter) -> None:
    """Attach the ring buffer to the root logger, creating it on first use."""
    global _ring_buffer
    if capacity <= 0:
        _ring_buffer = None
        return
    if _ring_buffer is None:
        _ring_buffer = RingBufferHandler(capacity)
    else:
        _ring_buffer.resize(capacity)
        _ring_buffer.filters.clear()
    _ring_buffer.addFilter(request_id_filter)
    logging.getLogger().addHandler(_ring_buffer)


# ---------------------------------------------------------------------------
# Recent requests per user (#8)
# ---------------------------------------------------------------------------

# How many recent requests are remembered per user, and for how many users.
_RECENT_REQUESTS_PER_USER = 20
_RECENT_REQUESTS_MAX_USERS = 500


@dataclass(frozen=True)
class RecentRequest:
    """One authenticated request: its correlation id, method, path and time."""

    request_id: str
    method: str
    path: str
    at: float


class _RecentRequests:
    """A bounded per-user memory of recent request ids.

    A problem report names the reporter's last few requests so a maintainer can
    find their lines in the log. Log records carry a ``request_id`` but not the
    user, so the authentication dependency records the pairing here. Only the
    method and path are kept -- never a query string, header or cookie.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_user: dict[int, deque[RecentRequest]] = {}

    def record(self, user_id: int, request_id: str, method: str, path: str) -> None:
        with self._lock:
            entries = self._by_user.pop(user_id, None)
            if entries is None:
                entries = deque(maxlen=_RECENT_REQUESTS_PER_USER)
            if not entries or entries[-1].request_id != request_id:
                entries.append(RecentRequest(request_id, method, path, time.time()))
            # Re-insert so dict order is least-recently-active first.
            self._by_user[user_id] = entries
            while len(self._by_user) > _RECENT_REQUESTS_MAX_USERS:
                self._by_user.pop(next(iter(self._by_user)))

    def for_user(self, user_id: int) -> list[RecentRequest]:
        with self._lock:
            return list(self._by_user.get(user_id, ()))


recent_requests = _RecentRequests()


def _build_formatter(log_format: str) -> logging.Formatter:
    """Return the formatter selected by the configured format."""
    if log_format == "json":
        return JsonFormatter()
    return logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _redact_db_url(url: str) -> str:
    """Mask any embedded credentials in a database URL for safe logging."""
    return re.sub(r"://[^/@]+@", "://***@", url)


_PACKAGE_LOGGER = "timelapse_manager"

# Uvicorn's loggers. ``uvicorn.error`` carries the traceback of every unhandled
# request exception, so it must never be left disabled.
_SERVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _reenable_package_loggers(level: int) -> None:
    """Undo a prior ``disable_existing_loggers`` for the application loggers.

    Clears ``disabled`` on the ``timelapse_manager`` logger and every existing
    descendant (a ``disable_existing_loggers=True`` dictConfig sets the flag on
    each logger individually, and it is not inherited), and pins the package
    logger's level and propagation so its records reach the root handler. Loggers
    created later inherit the cleared state, so this is sufficient for the modules
    imported after configuration too.
    """
    package = logging.getLogger(_PACKAGE_LOGGER)
    package.disabled = False
    package.setLevel(level)
    package.propagate = True
    # Re-fetch each existing descendant by name (a placeholder in loggerDict is
    # not a Logger and has no ``disabled`` flag, so getLogger materialises the
    # real logger and we clear the flag there).
    prefix = _PACKAGE_LOGGER + "."
    names = [
        name
        for name in list(logging.getLogger().manager.loggerDict)
        if name.startswith(prefix)
    ]
    for name in names:
        logging.getLogger(name).disabled = False


def configure_logging(settings: Settings, config_path: str | None = None) -> None:
    """Configure root logging from settings and emit a startup banner.

    Installs a console handler (and an optional file handler when
    ``logging.file_sink`` is set) using the configured level and formatter,
    replacing any handlers from a previous call so repeated invocation is safe.

    :param settings: resolved application settings.
    :param config_path: the config file path in use, if any, included in the
        banner for operator visibility.
    """
    level = getattr(logging, settings.logging.level, logging.INFO)
    formatter = _build_formatter(settings.logging.format)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # One filter instance, shared across handlers, stamps the in-flight request's
    # correlation id onto every record so a JSON log line is traceable to its
    # request. It only enriches records, so sharing one instance is safe.
    request_id_filter = RequestIdFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(request_id_filter)
    root.addHandler(console)

    # Re-enable the application loggers explicitly. When the process is launched
    # via ``uvicorn --factory`` (the container path), uvicorn imports this package
    # -- creating every module-level ``getLogger("timelapse_manager.*")`` -- and
    # then runs its default ``logging.config.dictConfig`` whose
    # ``disable_existing_loggers`` defaults to True, which sets ``disabled=True``
    # on *each* already-created logger individually (parent and children alike).
    # Reconfiguring only the root logger (above) does not clear those per-logger
    # flags, and ``disabled`` is checked per-logger rather than inherited, so
    # without this our records would be dropped before reaching root's handler.
    # Clear the flag on the package logger and every existing descendant, and pin
    # the package level/propagation so ``timelapse_manager.*`` emits at the
    # configured level on every launch path.
    _reenable_package_loggers(level)
    # The server's own loggers get the same treatment: the in-process migration
    # (Alembic's ``fileConfig``) used to disable ``uvicorn.error`` the same way,
    # which dropped every "Exception in ASGI application" traceback (#37).
    for name in _SERVER_LOGGERS:
        logging.getLogger(name).disabled = False

    # The problem-report ring buffer (#8) sits alongside the console handler so
    # it sees exactly what the operator's log sees.
    _install_ring_buffer(settings.logging.ring_buffer_records, request_id_filter)

    if settings.logging.file_sink is not None:
        sink = settings.logging.file_sink
        sink.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(sink, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(request_id_filter)
        root.addHandler(file_handler)

    # httpx/httpcore emit the full request URL at INFO on every call. A URL can
    # carry a secret in its query string (e.g. a webhook token) that userinfo
    # redaction does not cover, so cap these libraries at WARNING -- request URLs
    # never reach the logs. Our own code still redacts URL userinfo via
    # redact_text for anything it logs directly.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    safe_db_url = _redact_db_url(settings.database.url)
    banner_fields = {
        "app_version": __version__,
        "config_path": config_path,
        "bind_address": settings.server.bind_address,
        "http_port": settings.server.http_port,
        "https_port": settings.server.https_port,
        "database_url": safe_db_url,
    }
    # Fold the fields into the message text so the banner is complete under the
    # plain-text formatter too (which does not render ``extra=`` fields), while
    # still passing them as structured extras for the JSON formatter. The db URL
    # is redacted before either path sees it.
    summary = (
        f"Timelapse Manager starting "
        f"version={__version__} "
        f"config_path={config_path} "
        f"bind={settings.server.bind_address} "
        f"http_port={settings.server.http_port} "
        f"https_port={settings.server.https_port} "
        f"database_url={safe_db_url}"
    )
    logging.getLogger(__name__).info(summary, extra=banner_fields)
