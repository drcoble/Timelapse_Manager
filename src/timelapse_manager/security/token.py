"""Local bearer-token authentication.

The local API is bound to the loopback control surface and protected by a single
high-entropy bearer token persisted to a file readable only by the owner. The
service (and the in-process application) ensures the token exists at startup; the
CLI reads the same file to authenticate its loopback calls.

The token-checking dependency reads the expected token from the running
process's settings (via the application context) at request time, so it keeps a
stable import name without binding to a particular ``Settings`` instance.
"""

from __future__ import annotations

import contextlib
import secrets

from fastapi import HTTPException, Request, status

from ..config import Settings
from ..runtime import get_context

# Bytes of entropy for a freshly generated token (token_hex yields 2 hex chars
# per byte, so 32 bytes -> a 64-character hex string).
_TOKEN_BYTES = 32

# Owner read/write only; the token grants full local API access.
_TOKEN_FILE_MODE = 0o600

_BEARER_PREFIX = "Bearer "


def ensure_local_token(settings: Settings) -> str:
    """Return the local API token, creating and persisting it if absent.

    Reads ``settings.paths.token_file``. If the file exists, its stripped
    contents are returned. Otherwise a new high-entropy token is generated, its
    parent directory is created, and it is written with owner-only permissions
    before being returned. Idempotent across restarts.
    """
    token_file = settings.paths.token_file
    assert token_file is not None  # populated by PathsSettings validator
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()

    token = secrets.token_hex(_TOKEN_BYTES)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions atomically where the platform honors
    # the mode on open; chmod afterwards covers platforms that apply umask.
    token_file.write_text(token, encoding="utf-8")
    # Some filesystems (notably on Windows) do not support POSIX modes; the
    # loopback binding remains the primary protection there.
    with contextlib.suppress(OSError):
        token_file.chmod(_TOKEN_FILE_MODE)
    return token


def verify_token(received: str, expected: str) -> bool:
    """Return True if ``received`` matches ``expected`` in constant time.

    Uses :func:`secrets.compare_digest` to avoid leaking match length through
    timing. An empty expected token never matches.
    """
    if not expected:
        return False
    return secrets.compare_digest(received, expected)


def _extract_bearer(request: Request) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header."""
    header = request.headers.get("Authorization")
    if header is None or not header.startswith(_BEARER_PREFIX):
        return None
    return header[len(_BEARER_PREFIX) :].strip()


def require_local_token(request: Request) -> None:
    """FastAPI dependency enforcing a valid local bearer token.

    Reads the expected token from the running process's settings via the
    application context, compares it in constant time against the request's
    bearer credential, and raises ``401`` on any mismatch or missing header.
    """
    expected = ensure_local_token(get_context().settings)
    received = _extract_bearer(request)
    if received is None or not verify_token(received, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid local API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
