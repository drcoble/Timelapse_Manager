"""Logging configuration built on the standard library.

Provides a JSON or plain-text formatter selectable from settings, redaction of
secret-looking values in structured fields, an optional file sink alongside the
console, and a one-line startup banner. No third-party logging dependency is
used.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    from .config.settings import Settings

# Field names whose values are masked in structured log output. Matched
# case-insensitively as a substring, so "smtp_password" and "api_key" both hit.
_SECRET_NAME_PATTERN = re.compile(r"password|token|secret|key", re.IGNORECASE)
_REDACTED = "***"

# Credentials embedded in a URL's userinfo component, e.g.
# ``rtsp://user:pass@host`` or ``https://token@host``. The scheme and host are
# preserved; the credentials are replaced. Applied to both free-text messages
# and string values inside structured fields so an embedded camera/SMTP/webhook
# credential never reaches the log or, via log_event, the database.
_URL_USERINFO_PATTERN = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s@]+@")

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


def redact_text(text: str) -> str:
    """Mask credentials embedded in any URL within a free-text string.

    Two classes of credential are scrubbed while the rest of the URL is kept for
    diagnosis:

    * **Userinfo** -- ``rtsp://user:pass@host`` becomes ``rtsp://***@host``.
    * **Secret query parameters** -- ``https://h/x?token=abc`` becomes
      ``https://h/x?token=***``; the value of any parameter whose name looks like
      a secret (``token``/``secret``/``key``/``password``/``sig``/...) is masked.

    Non-URL text is returned unchanged. Used for log message bodies and by the
    event logger before a message is persisted.
    """
    text = _URL_USERINFO_PATTERN.sub(r"\g<scheme>***@", text)
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

    console = logging.StreamHandler()
    console.setFormatter(formatter)
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

    if settings.logging.file_sink is not None:
        sink = settings.logging.file_sink
        sink.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(sink, encoding="utf-8")
        file_handler.setFormatter(formatter)
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
