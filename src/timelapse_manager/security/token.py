"""Local bearer-token authentication.

The local API is protected by a single high-entropy bearer token persisted to a
file readable only by the service account. The service creates the file at
startup (see the application lifespan); the CLI only reads it to authenticate
its loopback calls, and ``timelapse token rotate`` replaces it.

Three layers guard the token (#67 S1/S2):

* **Peer policy** (:func:`enforce_token_peer`). The token is accepted only from
  the peers in ``server.api_token_allowed_clients`` (loopback by default), and
  from a non-loopback peer only over HTTPS. The check runs *before* the token is
  compared, so a refused peer learns nothing about the token.
* **File safety** (:func:`ensure_token_file`). The file is created with
  ``O_EXCL`` at mode ``0600`` under a unique temporary name and hard-linked into
  place, so two concurrent creators agree on one token and no reader ever sees a
  partial file. A file with loose permissions is tightened; one owned by another
  account is refused (``503`` on bearer calls).
* **Load once, revalidate by stat** (:class:`TokenFileCache`). The running
  service reads the file once and re-reads it only when its inode or timestamps
  change, so a rotation takes effect on the next request without a restart.

A separate, read-only metrics token (``observability.metrics_token_file``) uses
the same file routines; ``security.principal.require_metrics_reader`` accepts it
on the metrics scrape only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path

from fastapi import HTTPException, Request, status

from ..config import Settings
from ..runtime import get_context
from .proxy_networks import (
    IpAddress,
    IpNetwork,
    in_networks,
    parse_address,
    parse_networks,
)

logger = logging.getLogger(__name__)

# Bytes of entropy for a freshly generated token (token_hex yields 2 hex chars
# per byte, so 32 bytes -> a 64-character hex string).
_TOKEN_BYTES = 32

# Owner read/write only; the token grants full local API access.
_TOKEN_FILE_MODE = 0o600

# Any group or other permission bit on a token file is too loose.
_LOOSE_MODE_BITS = 0o077

_BEARER_PREFIX = "Bearer "

# Default metrics-token location, relative to the data directory.
_METRICS_TOKEN_NAME = ".metrics-token"

# ``O_NOFOLLOW`` refuses a symlinked token file; ``O_CLOEXEC`` keeps the fd out
# of child processes (ffmpeg). Both are absent on Windows, where 0 is a no-op.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# Headers whose presence marks a request as relayed by a reverse proxy.
_FORWARDED_HEADERS = ("x-forwarded-for", "x-forwarded-proto", "forwarded")

PEER_REFUSED_DETAIL = "Local API token not accepted from this address."

# One refused-peer warning per (peer, client) per window, from a bounded table
# so a spray of source addresses cannot grow it without limit.
_PEER_WARNING_INTERVAL_SECONDS = 600.0
_PEER_WARNING_MAX_ENTRIES = 512


# Upper bound on a token file read; a real token is 64 characters.
_TOKEN_READ_LIMIT = 4096


class TokenFileError(Exception):
    """A token file exists but cannot be trusted or used.

    The message is safe to log: it names the file and the problem, never the
    token. ``wrong_owner`` marks the case the request path reports by name.
    """

    def __init__(self, message: str, *, wrong_owner: bool = False) -> None:
        super().__init__(message)
        self.wrong_owner = wrong_owner


# ---------------------------------------------------------------------------
# Token files
# ---------------------------------------------------------------------------


def _current_uid() -> int | None:
    """The process uid, or ``None`` on platforms without POSIX ownership."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


_FileKey = tuple[int, int, int, int]


def _file_key(st: os.stat_result) -> _FileKey:
    """Identity of one version of a file: replaced or rewritten files differ."""
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def _read_validated(path: Path) -> tuple[str, _FileKey]:
    """Read a token file the service owns, enforcing owner and mode.

    The checks run on the opened descriptor, never on the path, so the file
    checked is the file read. A symlink, a non-regular file, a file owned by
    another account, or an empty file raises :class:`TokenFileError`; group or
    other permission bits are logged and stripped. A missing file raises
    :class:`FileNotFoundError` for the caller to decide on.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise TokenFileError(
            f"token file {path} cannot be opened ({type(exc).__name__})"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TokenFileError(f"token file {path} is not a regular file")
        uid = _current_uid()
        if uid is not None and st.st_uid != uid:
            raise TokenFileError(
                f"token file {path} has the wrong owner (uid {st.st_uid}, "
                f"the service runs as uid {uid})",
                wrong_owner=True,
            )
        if st.st_mode & _LOOSE_MODE_BITS:
            logger.warning(
                "token file %s had loose permissions %s; tightening to 0600",
                path,
                oct(stat.S_IMODE(st.st_mode)),
            )
            with contextlib.suppress(OSError, AttributeError):
                os.fchmod(fd, _TOKEN_FILE_MODE)
            st = os.fstat(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as handle:
            value = handle.read(_TOKEN_READ_LIMIT).strip()
    finally:
        os.close(fd)
    if not value:
        raise TokenFileError(f"token file {path} is empty")
    return value, _file_key(st)


def _dir_owner(directory: Path) -> int:
    """The owner uid of ``directory`` (the data directory, owned by the service)."""
    return os.stat(directory).st_uid


def read_token_file(path: Path) -> str:
    """Read a token file for a client (the CLI) without creating or changing it.

    The client is often root reading a service-owned file, so it does not
    require the file to be its own. It does refuse what it should not send
    anywhere: a symlink or non-regular file (opened with ``O_NOFOLLOW`` and
    checked on the descriptor), and, when running as root, a file not owned by
    the owner of its directory or readable by group or others. At most 4 KiB is
    read.

    :raises FileNotFoundError: the file does not exist.
    :raises PermissionError: the caller may not read it.
    :raises TokenFileError: it is unsafe or empty.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
    except (FileNotFoundError, PermissionError):
        raise
    except OSError as exc:
        # ELOOP from O_NOFOLLOW on a symlink lands here.
        raise TokenFileError(
            f"token file {path} cannot be opened ({type(exc).__name__})"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TokenFileError(f"token file {path} is not a regular file")
        if _current_uid() == 0:
            owner = _dir_owner(path.parent)
            if st.st_uid != owner:
                raise TokenFileError(
                    f"token file {path} is owned by uid {st.st_uid}, not by the "
                    f"owner of its directory (uid {owner})"
                )
            if st.st_mode & _LOOSE_MODE_BITS:
                raise TokenFileError(
                    f"token file {path} has loose permissions "
                    f"{oct(stat.S_IMODE(st.st_mode))}; expected 0600"
                )
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as handle:
            value = handle.read(_TOKEN_READ_LIMIT).strip()
    finally:
        os.close(fd)
    if not value:
        raise TokenFileError(f"token file {path} is empty")
    return value


def _fsync_dir(directory: Path) -> None:
    """Persist a directory entry change (best effort; not on every platform)."""
    with contextlib.suppress(OSError, AttributeError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_temp(
    path: Path, value: str, *, owner: tuple[int, int] | None = None
) -> Path:
    """Write ``value`` to a new, uniquely named ``0600`` file beside ``path``.

    ``O_EXCL | O_NOFOLLOW`` guarantees the file is ours and new (never a planted
    file or symlink); the mode is set at creation, so the content is never
    readable by others even for an instant (umask can only remove bits).
    ``owner`` (uid, gid) is applied with ``fchown`` on the open descriptor, so
    it reaches exactly this file. Removed again if anything fails.
    """
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
        _TOKEN_FILE_MODE,
    )
    try:
        try:
            if owner is not None:
                os.fchown(fd, *owner)
            os.write(fd, value.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    return temp


def _publish_exclusive(temp: Path, path: Path, value: str) -> bool:
    """Move a fully written temp file to ``path`` unless ``path`` exists.

    Returns ``True`` if this call created ``path``. A hard link is atomic and
    fails when the name is taken, so of two racing creators exactly one wins and
    the other reads the winner's token. On a filesystem without hard links, the
    final name is created directly with ``O_EXCL`` (still exclusive; a reader may
    briefly see it empty, which reads as an error rather than a wrong token).
    """
    try:
        os.link(temp, path)
        return True
    except FileExistsError:
        return False
    except OSError:
        pass  # No hard-link support: fall through to the direct exclusive create.
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
            _TOKEN_FILE_MODE,
        )
    except FileExistsError:
        return False
    try:
        os.write(fd, value.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def ensure_token_file(path: Path) -> str:
    """Return the token in ``path``, creating it safely if it does not exist.

    For the service only: an existing file must be owned by the process and is
    tightened to ``0600`` if needed (see :func:`_read_validated`). A new file is
    created with ``O_EXCL`` under a temporary name and linked into place, so a
    concurrent creator never overwrites it and nobody reads a partial token.

    :raises TokenFileError: the existing file is unusable (wrong owner, empty,
        not a regular file, unreadable).
    """
    try:
        return _read_validated(path)[0]
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(_TOKEN_BYTES)
    temp = _write_temp(path, token)
    if _publish_exclusive(temp, path, token):
        _fsync_dir(path.parent)
        return token
    # Lost a creation race: the winner's token is the token.
    return _read_validated(path)[0]


def rotate_token_file(path: Path) -> None:
    """Replace the token in an existing ``path`` with a fresh one, atomically.

    The new token is written to a ``0600`` temp file and renamed over the old
    one, so readers see either the old or the new token, never a mix. When run
    as root, the new file keeps the old file's owner and group, so the service
    account can still read it. A non-root caller may rotate only a file it owns,
    since the service refuses a token file owned by another account.

    :raises FileNotFoundError: there is no token file to rotate.
    :raises TokenFileError: the file is not a regular file, or belongs to
        another account and the caller is not root.
    """
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise TokenFileError(f"token file {path} is not a regular file")
    uid = _current_uid()
    if uid not in (None, 0) and st.st_uid != uid:
        raise TokenFileError(
            f"token file {path} belongs to uid {st.st_uid}; "
            "run this as that user or as root"
        )
    owner = (st.st_uid, st.st_gid) if uid == 0 else None
    temp = _write_temp(path, secrets.token_hex(_TOKEN_BYTES), owner=owner)
    try:
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    _fsync_dir(path.parent)


def local_token_path(settings: Settings) -> Path:
    """The local API token file (filled from ``data_dir`` by the settings)."""
    token_file = settings.paths.token_file
    assert token_file is not None  # populated by PathsSettings validator
    return token_file


def metrics_token_path(settings: Settings) -> Path:
    """The metrics token file: the configured path or ``<data_dir>/.metrics-token``."""
    configured = settings.observability.metrics_token_file
    return configured or settings.paths.data_dir / _METRICS_TOKEN_NAME


def ensure_local_token(settings: Settings) -> str:
    """Return the local API token, creating it safely if absent (service side).

    Thin wrapper over :func:`ensure_token_file` for ``settings.paths.token_file``.
    Idempotent across restarts and safe against concurrent creators.
    """
    return ensure_token_file(local_token_path(settings))


class TokenFileCache:
    """A token file read once and re-read only when the file changes.

    Each :meth:`get` costs one ``stat``; the file is opened again only when its
    device, inode, mtime or ctime differ from the cached read (rotation replaces
    the inode). A missing file is re-created, so deleting the file revokes the
    old token without a restart. Thread-safe: sync dependencies run in the
    worker threadpool.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # (file identity, token) as one tuple, so the lock-free fast path never
        # pairs a new identity with an old token.
        self._entry: tuple[_FileKey, str] | None = None

    @property
    def path(self) -> Path:
        """The token file this cache reads."""
        return self._path

    def get(self) -> str:
        """Return the current token.

        :raises TokenFileError: the file is unusable; callers answer ``503``.
        """
        entry = self._entry
        if entry is not None:
            with contextlib.suppress(FileNotFoundError):
                if _file_key(os.stat(self._path)) == entry[0]:
                    return entry[1]
        with self._lock:
            try:
                value, key = _read_validated(self._path)
            except FileNotFoundError:
                ensure_token_file(self._path)
                value, key = _read_validated(self._path)
            except TokenFileError:
                self._entry = None
                raise
            self._entry = (key, value)
            return value


def load_token_caches(
    settings: Settings,
) -> tuple[TokenFileCache, TokenFileCache | None]:
    """Create the service's token files and return their caches (startup).

    The local API token always; the metrics token only while the metrics
    endpoint is enabled. An unusable file is logged at ERROR (the problem only,
    never a token) and left for the request path to answer ``503``, so a bad
    token file does not stop capture.
    """
    local = TokenFileCache(local_token_path(settings))
    metrics = (
        TokenFileCache(metrics_token_path(settings))
        if settings.observability.metrics_enabled
        else None
    )
    for cache in (local, metrics):
        if cache is None:
            continue
        try:
            cache.get()
        except TokenFileError as exc:
            logger.error("API token unavailable: %s", exc)
    return local, metrics


def current_local_token() -> str:
    """The expected local API token, as a request dependency sees it.

    Uses the cache the lifespan installed; under alternative wiring (no
    lifespan) one is installed on first use.

    :raises HTTPException: ``503`` when the token file is unusable.
    """
    context = get_context()
    if context.local_token is None:
        context.local_token = TokenFileCache(local_token_path(context.settings))
    return _token_or_503(context.local_token)


def current_metrics_token() -> str:
    """The expected metrics token, as a request dependency sees it.

    :raises HTTPException: ``503`` when the token file is unusable.
    """
    context = get_context()
    if context.metrics_token is None:
        context.metrics_token = TokenFileCache(metrics_token_path(context.settings))
    return _token_or_503(context.metrics_token)


def _token_or_503(cache: TokenFileCache) -> str:
    try:
        return cache.get()
    except TokenFileError as exc:
        logger.error("API token unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            # Names no path or uid; the log line above has the details.
            detail=(
                "Local token file has the wrong owner."
                if exc.wrong_owner
                else "Local token file is unusable."
            ),
        ) from exc


# ---------------------------------------------------------------------------
# Peer policy
# ---------------------------------------------------------------------------

# Shared with the web layer's effective-client resolution (#174): one parser,
# one cache and one warning-once for a malformed ``trusted_proxies`` entry.
_IpAddress = IpAddress
_IpNetwork = IpNetwork


def _parse_networks(entries: Sequence[str]) -> list[_IpNetwork]:
    """Parse the allowed-client entries (validated at load; bad ones skipped)."""
    return parse_networks(entries, "allowed clients")


def _trusted_networks(entries: Sequence[str]) -> list[_IpNetwork]:
    """Parse ``server.trusted_proxies``, the same way the login throttle does."""
    return parse_networks(entries)


def peer_allowed(
    request: Request,
    allowed_clients: Sequence[str],
    *,
    trusted_proxies: Sequence[str],
    require_https: bool,
) -> bool:
    """Return whether a bearer token may be accepted from this request's peer.

    ``request.client`` is the TCP peer (the server runs with
    ``proxy_headers=False``, so it is never rewritten from headers):

    1. A peer that is not an IP literal is refused.
    2. A peer in ``trusted_proxies`` that relays the request (it sent
       forwarded headers) is judged only by an explicitly resolved client: the
       right-most ``X-Forwarded-For`` hop that is not itself a trusted proxy,
       which must be in ``allowed_clients``, with ``X-Forwarded-Proto: https``
       when ``require_https``. No such hop (no, empty or malformed XFF, only
       trusted hops, or ``Forwarded`` alone) is refused; nothing falls back to
       the proxy's own address. This applies to a loopback proxy too, so a
       same-host reverse proxy cannot make every remote caller look local. A
       loopback client forwarded by a non-loopback proxy is refused (it is the
       proxy host's loopback).
    3. A loopback peer that sends forwarded headers but is not a trusted proxy
       is refused: it relays for a client it does not identify to us.
    4. A loopback peer in ``allowed_clients`` is accepted on any scheme (the CLI
       over ``http://127.0.0.1``).
    5. Any other peer must be in ``allowed_clients`` and, when
       ``require_https``, connected over TLS.

    The forwarded client and scheme are parsed here, deliberately not read from
    ``request.state.effective_client_ip``: that value falls back to the peer
    address when no hop resolves, which is right for attribution but would
    fail open for authorization.
    """
    client = request.client
    peer = parse_address(client.host if client is not None else None)
    if peer is None:
        return False
    allowed = _parse_networks(allowed_clients)
    trusted = _trusted_networks(trusted_proxies)
    forwarded = any(header in request.headers for header in _FORWARDED_HEADERS)
    if forwarded and in_networks(peer, trusted):
        forwarded_client = _forwarded_client(request, trusted)
        if forwarded_client is None:
            # No untrusted X-Forwarded-For hop (absent, empty, malformed, only
            # trusted hops, or RFC 7239 ``Forwarded`` alone): unresolved.
            return False
        if forwarded_client.is_loopback and not peer.is_loopback:
            # A remote proxy's "loopback" client is that host's, not ours.
            return False
        return in_networks(forwarded_client, allowed) and (
            _forwarded_proto(request) == "https" or not require_https
        )
    if forwarded and peer.is_loopback:
        # A loopback peer that relays for someone else (a same-host proxy not
        # listed in trusted_proxies): the real client is unknown. The CLI and
        # local probes never send forwarded headers.
        return False
    if not in_networks(peer, allowed):
        return False
    if peer.is_loopback or not require_https:
        return True
    return request.url.scheme == "https"


def _forwarded_client(
    request: Request, trusted: Sequence[_IpNetwork]
) -> _IpAddress | None:
    """The right-most ``X-Forwarded-For`` hop that is not a trusted proxy.

    Proxies append the address they accepted the connection from on the right,
    so everything left of the first untrusted hop (reading right to left) is
    client-supplied. Returns ``None`` when there is no such hop, or when any
    hop on the way is malformed (the chain cannot be trusted then).
    """
    values = request.headers.getlist("x-forwarded-for")
    hops = [hop.strip() for value in values for hop in value.split(",")]
    for hop in reversed(hops):
        address = parse_address(hop)
        if address is None:
            return None
        if not in_networks(address, trusted):
            return address
    return None


def _forwarded_proto(request: Request) -> str:
    """``https`` only if the proxy's ``X-Forwarded-Proto`` says exactly that."""
    values = request.headers.getlist("x-forwarded-proto")
    if len(values) != 1:
        return "http"
    return "https" if values[0].split(",")[0].strip().lower() == "https" else "http"


class _PeerWarningLimiter:
    """Allows one warning per key per interval, remembering a bounded set of keys."""

    def __init__(self, interval_seconds: float, max_entries: int) -> None:
        self._interval = interval_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._last: OrderedDict[tuple[str, str], float] = OrderedDict()

    def should_log(self, key: tuple[str, str], now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._interval:
                return False
            self._last[key] = now
            self._last.move_to_end(key)
            while len(self._last) > self._max_entries:
                self._last.popitem(last=False)
            return True


_peer_warnings = _PeerWarningLimiter(
    _PEER_WARNING_INTERVAL_SECONDS, _PEER_WARNING_MAX_ENTRIES
)


def refuse_token_peer(
    request: Request,
    credential: str,
    setting: str = "server.api_token_allowed_clients",
) -> HTTPException:
    """Log (rate-limited) and build the ``403`` for a refused bearer peer.

    No ``WWW-Authenticate`` header: retrying with another token cannot help. The
    log names the peer, the forwarded client and the path (``%r``-quoted, so a
    crafted path cannot forge log lines), never a header value or token.
    """
    peer = request.client.host if request.client is not None else "unknown"
    forwarded = str(getattr(request.state, "effective_client_ip", "") or "-")
    if _peer_warnings.should_log((peer, forwarded)):
        logger.warning(
            "%s refused from peer %r (client %r) on %r; see %s",
            credential,
            peer,
            forwarded,
            request.url.path,
            setting,
        )
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=PEER_REFUSED_DETAIL
    )


def enforce_token_peer(request: Request, allowed_clients: Sequence[str]) -> None:
    """Raise ``403`` unless the local API token may be used from this peer.

    Call before comparing the token. See :func:`peer_allowed` for the rules;
    the local admin token always requires HTTPS off loopback.
    """
    settings = get_context().settings
    if not peer_allowed(
        request,
        allowed_clients,
        trusted_proxies=settings.server.trusted_proxies,
        require_https=True,
    ):
        raise refuse_token_peer(request, "local API token")


# ---------------------------------------------------------------------------
# Comparison and the router dependency
# ---------------------------------------------------------------------------


def verify_token(received: str, expected: str) -> bool:
    """Return True if ``received`` matches ``expected`` in constant time.

    Uses :func:`secrets.compare_digest` to avoid leaking match length through
    timing. An empty expected token never matches. Both sides are compared as
    UTF-8 bytes: ``compare_digest`` raises ``TypeError`` for a ``str`` with
    non-ASCII characters, which turned such a bearer token into a 500 (#115).
    """
    if not expected:
        return False
    return secrets.compare_digest(
        received.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def _extract_bearer(request: Request) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header."""
    header = request.headers.get("Authorization")
    if header is None or not header.startswith(_BEARER_PREFIX):
        return None
    return header[len(_BEARER_PREFIX) :].strip()


def invalid_token_error() -> HTTPException:
    """The ``401`` for a missing or wrong bearer token from an allowed peer."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid local API token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_local_token(request: Request) -> None:
    """FastAPI dependency enforcing a valid local bearer token.

    In order: the peer policy (``403``), the token file (``503`` if unusable),
    then a constant-time compare of the request's bearer credential (``401`` on
    a missing header or a mismatch).
    """
    enforce_token_peer(request, get_context().settings.server.api_token_allowed_clients)
    expected = current_local_token()
    received = _extract_bearer(request)
    if received is None or not verify_token(received, expected):
        raise invalid_token_error()
