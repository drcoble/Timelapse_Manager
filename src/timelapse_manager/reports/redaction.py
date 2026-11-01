"""The final, value-based redaction pass over a report bundle.

Every JSON section is first built from allow-listed fields and run through
:func:`timelapse_manager.logging.redact` (the same pattern redaction the logs,
events and debug endpoints use). Pattern redaction cannot catch a password that
sits in plain text inside an error string, or a webhook whose secret is a path
segment. So, before anything is zipped, this module collects the *actual*
secret values the process can see and replaces every occurrence of each in
every member's bytes with :data:`REDACTED`.

Collected secrets: the LDAP bind password, the SMTP password, the webhook
signing secret and every notification webhook URL, every camera's stored
credentials and the default camera credentials, the at-rest encryption (Fernet)
key and key file, the local API token, the Gitea token, the database URL's
password, and the reporter's session cookie. A secret that cannot be read
(no key provider, a missing file) is simply not collected -- it then cannot
have been read into the bundle either.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings

logger = logging.getLogger(__name__)

REDACTED = "«redacted»"

# Values shorter than this are not scrubbed: replacing every "abc" in a bundle
# would mangle it while protecting nothing worth the name.
_MIN_SECRET_LENGTH = 4


def _read_secret_file(path: Path | None) -> str | None:
    """Return a file's stripped text if it exists and is readable. Never creates."""
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def _collect(sink: set[str], fetch: Callable[[], Any], label: str) -> None:
    """Add whatever ``fetch`` returns (a string or iterable of strings)."""
    try:
        value = fetch()
    except Exception as exc:  # noqa: BLE001 - an unreadable secret is not collected
        logger.debug("report redaction: %s unavailable (%s)", label, type(exc).__name__)
        return
    if value is None:
        return
    values = [value] if isinstance(value, str) else list(value)
    sink.update(item for item in values if isinstance(item, str) and item)


def _fernet_key() -> str | None:
    """The base64 Fernet key behind the installed key provider, if any."""
    from ..security.crypto import _get_provider

    cipher = _get_provider().get_cipher()
    signing = getattr(cipher, "_signing_key", None)
    encryption = getattr(cipher, "_encryption_key", None)
    if not isinstance(signing, bytes) or not isinstance(encryption, bytes):
        return None
    return base64.urlsafe_b64encode(signing + encryption).decode("ascii")


def _camera_secrets(session: Session) -> list[str]:
    """Every secret-looking value in every camera's stored credentials."""
    from ..db.models import Camera
    from ..security.crypto import _is_secret_field, decrypt_credentials

    found: list[str] = []
    for credentials in session.execute(select(Camera.credentials)).scalars():
        if not isinstance(credentials, dict):
            continue
        try:
            plain = decrypt_credentials(credentials) or credentials
        except Exception:  # noqa: BLE001 - fall back to the stored values
            plain = credentials
        for key, value in plain.items():
            if isinstance(value, str) and _is_secret_field(str(key)):
                found.append(value)
                # A stored-but-encrypted value is a secret in its own right too.
                stored = credentials.get(key)
                if isinstance(stored, str):
                    found.append(stored)
    return found


def _default_camera_password(session: Session) -> str | None:
    """The stored default camera password, whether or not the fallback is on."""
    from ..db.models import CameraDefaultCredentials
    from ..security.crypto import decrypt_secret

    row = session.execute(select(CameraDefaultCredentials)).scalars().first()
    if row is None or not row.password:
        return None
    return decrypt_secret(row.password)


def _database_password(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.unquote(parsed.password) if parsed.password else None


def collect_secrets(
    session: Session | None,
    settings: Settings,
    *,
    extra: Iterable[str] = (),
) -> set[str]:
    """Collect every secret value the bundle must not contain.

    :param session: an open session for the database-held secrets, or ``None``
        when the database is unavailable.
    :param extra: request-specific values, e.g. the reporter's session cookie.
    """
    from ..monitoring import settings_service as notifications
    from ..security import ldap_settings_service

    secrets: set[str] = {value for value in extra if value}
    if session is not None:
        _collect(
            secrets,
            lambda: ldap_settings_service.resolve_bind_password(session),
            "LDAP bind password",
        )
        _collect(
            secrets,
            lambda: notifications.resolve_smtp_password(session),
            "SMTP password",
        )
        _collect(
            secrets,
            lambda: notifications.resolve_webhook_signing_secret(session),
            "webhook signing secret",
        )
        _collect(
            secrets,
            lambda: notifications.resolve_webhook_urls(session),
            "webhook URLs",
        )
        _collect(secrets, lambda: _camera_secrets(session), "camera credentials")
        _collect(
            secrets,
            lambda: _default_camera_password(session),
            "default camera credentials",
        )
    _collect(secrets, _fernet_key, "encryption key")
    key_file = settings.secrets.key_file or settings.paths.data_dir / ".secret-key"
    _collect(secrets, lambda: _read_secret_file(key_file), "key file")
    _collect(
        secrets, lambda: _read_secret_file(settings.paths.token_file), "local token"
    )
    _collect(
        secrets,
        lambda: _read_secret_file(settings.reporting.gitea.token_file),
        "Gitea token",
    )
    _collect(
        secrets,
        lambda: _database_password(settings.database.url),
        "database password",
    )
    if settings.reporting.webhook_url:
        secrets.add(settings.reporting.webhook_url)
    return {value for value in secrets if len(value) >= _MIN_SECRET_LENGTH}


def _variants(secret: str) -> set[str]:
    """The forms a secret can take in a bundle: raw, JSON-escaped, URL-quoted."""
    return {
        secret,
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret),
    }


class Scrubber:
    """Replaces every collected secret, in any of its encodings, in a blob."""

    def __init__(self, secrets: Iterable[str]) -> None:
        needles: set[bytes] = set()
        for secret in secrets:
            for variant in _variants(secret):
                if len(variant) >= _MIN_SECRET_LENGTH:
                    needles.add(variant.encode("utf-8"))
        # Longest first, so a URL is replaced whole before a secret inside it.
        self._needles = sorted(needles, key=len, reverse=True)
        self._replacement = REDACTED.encode("utf-8")

    def scrub(self, data: bytes) -> bytes:
        """Return ``data`` with every secret replaced by :data:`REDACTED`."""
        for needle in self._needles:
            if needle in data:
                data = data.replace(needle, self._replacement)
        return data

    def contains(self, data: bytes) -> bool:
        """Whether any secret, in any of its encodings, is still in ``data``.

        The anonymized-report verifier (#21) uses this to fail closed.
        """
        return any(needle in data for needle in self._needles)

    def scrub_text(self, text: str) -> str:
        """String form of :meth:`scrub`."""
        return self.scrub(text.encode("utf-8")).decode("utf-8", errors="replace")
