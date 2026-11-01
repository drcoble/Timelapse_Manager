"""Content-hashed static asset URLs and the in-memory stylesheet bundle (#70).

Two problems are solved here, both without writing a byte to disk: the systemd
unit runs with ``ProtectSystem=strict`` and the PyInstaller bundle ships
``web/static`` read-only, so everything is derived in memory at start-up.

**Versioned URLs (F-R8).** :class:`AssetManifest` walks the static directory
once and records ``sha256(content)[:12]`` for every file. Templates call
``static_url("js/selection.js")``, which renders
``/static/js/selection.js?v=<hash>``. A file edited by an upgrade gets a new
hash, so the browser fetches it instead of reusing a stale cached copy. The
version string is deliberately *not* ``__version__``: a development build keeps
the same version while its files change. An unknown path raises
:class:`UnknownAssetError` at render time, so a typo fails the test suite
rather than shipping a broken link.

**Stylesheet bundle (F-P5).** ``css/app.css`` is a thin index of ``@import``
rules, which the browser fetches as a serial chain. The manifest resolves the
chain recursively, in source order (the order *is* the cascade), and holds the
concatenation under the virtual path ``css/app.bundle.css``. Every ``url(...)``
that points at a local asset (the web fonts) is rewritten to its versioned URL,
so the bundle's font URLs are byte-identical to the ``<link rel="preload">``
the templates emit. ``static_url("css/app.css")`` returns the bundle URL; the
raw ``app.css`` stays servable unchanged for anything that still asks for it.

**Serving.** :class:`VersionedStaticFiles` serves the bundle from memory (with
an ``ETag``) and everything else from disk, then sets ``Cache-Control``:
``immutable`` for a year **only** when the request's ``v`` equals the current
hash; a month for an unversioned font; ``no-cache`` otherwise, so a stale ``v``
revalidates and receives the current content.

The manifest is built when :func:`~.app_wiring.mount_web` runs, once per
process in production (a restart, including a ``uvicorn --reload`` restart,
rebuilds it).
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# The packaged static tree, and the URL prefix it is mounted under (see
# ``app_wiring.mount_web``).
PACKAGE_STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PREFIX = "/static/"

# The stylesheet entry point and the virtual path its resolved bundle is served
# under. ``static_url(BUNDLE_SOURCE)`` returns the bundle's URL.
BUNDLE_SOURCE = "css/app.css"
BUNDLE_PATH = "css/app.bundle.css"

# Hex digits of the sha256 kept in ``?v=``: 48 bits is ample to tell versions of
# one file apart, and keeps the URLs short.
_HASH_LEN = 12

# Cache-Control policies (see the module docstring).
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_FONT_UNVERSIONED = "public, max-age=2592000"
CACHE_REVALIDATE = "no-cache"

# A CSS string (either quote style, with escapes) or a comment. Matching both in
# one alternation lets comment stripping leave a ``/*`` inside a string alone.
_STRING_OR_COMMENT_RE = re.compile(
    r"""("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|/\*.*?\*/""",
    re.DOTALL,
)

# ``@import url('x.css') <media>;`` or ``@import 'x.css' <media>;``.
_IMPORT_RE = re.compile(
    r"""@import\s+
        (?:
            url\(\s*(?P<q1>["']?)(?P<url1>[^"')]*)(?P=q1)\s*\)
          | (?P<q2>["'])(?P<url2>[^"']*)(?P=q2)
        )
        \s*(?P<media>[^;]*);""",
    re.VERBOSE,
)

# ``url(x)``, ``url('x')`` or ``url("x")`` outside an ``@import``.
_URL_RE = re.compile(
    r"""url\(\s*(?:(?P<q>["'])(?P<quoted>.*?)(?P=q)|(?P<bare>[^"')\s]+))\s*\)""",
    re.DOTALL,
)

# ``url()`` targets that are not local files and are left exactly as written.
_PASSTHROUGH_PREFIXES = ("data:", "#", "http:", "https:", "//")


class AssetError(ValueError):
    """The static tree cannot be turned into a manifest or bundle."""


class UnknownAssetError(LookupError):
    """``static_url`` was asked for a path that is not in the static tree."""


def _digest(content: bytes) -> str:
    """Return the short content hash used as the ``v`` query value."""
    return hashlib.sha256(content).hexdigest()[:_HASH_LEN]


def _strip_comments(css: str) -> str:
    """Remove ``/* ... */`` comments from ``css`` without touching strings."""
    return _STRING_OR_COMMENT_RE.sub(lambda m: m.group(1) or "", css)


def _resolve_local(target: str, importer: str) -> str | None:
    """Resolve a CSS reference to a static-relative POSIX path.

    ``target`` is the raw ``url()`` / ``@import`` value as written in the file
    ``importer`` (itself static-relative). Returns ``None`` for a non-local
    target (``data:``, a fragment, an absolute URL) that must be left alone.
    Raises :class:`AssetError` when a local target escapes the static tree.
    """
    target = target.strip()
    if not target or target.lower().startswith(_PASSTHROUGH_PREFIXES):
        return None
    # Drop any query or fragment the author wrote; the manifest re-versions it.
    path = urllib.parse.urlsplit(target).path
    if path.startswith("/"):
        if not path.startswith(STATIC_PREFIX):
            raise AssetError(f"{importer}: {target!r} is outside {STATIC_PREFIX}")
        candidate = posixpath.normpath(path[len(STATIC_PREFIX) :])
    else:
        candidate = posixpath.normpath(
            posixpath.join(posixpath.dirname(importer), path)
        )
    if (
        candidate.startswith("../")
        or candidate in ("..", ".")
        or candidate.startswith("/")
    ):
        raise AssetError(f"{importer}: {target!r} escapes the static directory")
    return candidate


@dataclass(frozen=True)
class AssetManifest:
    """Content hashes for every static file plus the resolved CSS bundle.

    Build with :meth:`build`; the instance is immutable and safe to share
    between requests.

    :param hashes: static-relative POSIX path -> short content hash. Includes
        :data:`BUNDLE_PATH`, hashed over the bundle bytes.
    :param bundle: the resolved stylesheet bundle, UTF-8 encoded.
    """

    hashes: Mapping[str, str]
    bundle: bytes

    @property
    def bundle_hash(self) -> str:
        """The bundle's content hash (also its ``ETag`` value)."""
        return self.hashes[BUNDLE_PATH]

    @classmethod
    def build(cls, static_dir: Path) -> AssetManifest:
        """Hash every file under ``static_dir`` and resolve the CSS bundle.

        Files are hashed first, so the bundle can embed versioned font URLs;
        the bundle is hashed last, over its final bytes. Raises
        :class:`AssetError` if the bundle cannot be resolved, which fails
        start-up loudly rather than serving a page with no styles.
        """
        root = static_dir.resolve()
        hashes: dict[str, str] = {}
        for file in sorted(root.rglob("*")):
            if file.is_file():
                rel = file.relative_to(root).as_posix()
                hashes[rel] = _digest(file.read_bytes())
        if BUNDLE_PATH in hashes:
            raise AssetError(f"{BUNDLE_PATH} is reserved for the in-memory bundle")
        if BUNDLE_SOURCE not in hashes:
            raise AssetError(f"stylesheet entry point {BUNDLE_SOURCE} is missing")

        bundle = _BundleResolver(root, hashes).resolve(BUNDLE_SOURCE).encode("utf-8")
        hashes[BUNDLE_PATH] = _digest(bundle)
        return cls(hashes=MappingProxyType(hashes), bundle=bundle)

    def current_hash(self, path: str) -> str | None:
        """Return the current hash for a static-relative path, if known."""
        return self.hashes.get(path)

    def url(self, path: str) -> str:
        """Return the versioned URL for a static-relative path.

        ``css/app.css`` maps to the in-memory bundle. Raises
        :class:`UnknownAssetError` for a path not in the static tree.
        """
        key = BUNDLE_PATH if path == BUNDLE_SOURCE else path
        digest = self.hashes.get(key)
        if digest is None:
            raise UnknownAssetError(f"unknown static asset: {path!r}")
        return f"{STATIC_PREFIX}{urllib.parse.quote(key)}?v={digest}"


class _BundleResolver:
    """Flatten an ``@import`` tree into one stylesheet, in cascade order."""

    def __init__(self, root: Path, hashes: Mapping[str, str]) -> None:
        self._root = root
        self._hashes = hashes

    def resolve(self, entry: str) -> str:
        """Return the fully expanded stylesheet rooted at ``entry``."""
        return self._expand(entry, ())

    def _read(self, rel: str) -> str:
        if rel not in self._hashes:
            raise AssetError(f"stylesheet {rel!r} does not exist")
        return (self._root / rel).read_text(encoding="utf-8")

    def _expand(self, rel: str, stack: tuple[str, ...]) -> str:
        if rel in stack:
            chain = " -> ".join((*stack, rel))
            raise AssetError(f"@import cycle: {chain}")
        text = _strip_comments(self._read(rel))
        parts: list[str] = []
        pos = 0
        for match in _IMPORT_RE.finditer(text):
            parts.append(self._rewrite_urls(text[pos : match.start()], rel))
            pos = match.end()
            url1 = match.group("url1")
            raw_target = url1 if url1 is not None else match.group("url2")
            target = _resolve_local(raw_target, rel)
            if target is None:
                raise AssetError(
                    f"{rel}: cannot bundle non-local @import {raw_target!r}"
                )
            media = match.group("media").strip()
            if media.startswith(("layer", "supports(")):
                raise AssetError(
                    f"{rel}: @import {raw_target!r} {media!r} is not supported"
                )
            inner = self._expand(target, (*stack, rel))
            parts.append(f"/* {target} */\n")
            # A media-qualified import keeps its condition by wrapping the
            # imported rules; an unqualified one is inlined as is.
            parts.append(f"@media {media} {{\n{inner}\n}}\n" if media else inner)
        parts.append(self._rewrite_urls(text[pos:], rel))
        return "".join(parts)

    def _rewrite_urls(self, css: str, rel: str) -> str:
        def _versioned(match: re.Match[str]) -> str:
            raw = match.group("quoted") if match.group("q") else match.group("bare")
            target = _resolve_local(raw, rel)
            if target is None:
                return match.group(0)
            digest = self._hashes.get(target)
            if digest is None:
                raise AssetError(f"{rel}: url({raw!r}) does not exist")
            return f'url("{STATIC_PREFIX}{urllib.parse.quote(target)}?v={digest}")'

        return _URL_RE.sub(_versioned, css)


# ---------------------------------------------------------------------------
# Process-wide manifest used by the templates' ``static_url`` global.
# ---------------------------------------------------------------------------

_manifest: AssetManifest | None = None


def install_manifest(manifest: AssetManifest) -> None:
    """Make ``manifest`` the one ``static_url`` and ``css_bundle`` read."""
    global _manifest  # noqa: PLW0603 -- one manifest per process, set at mount
    _manifest = manifest


def current_manifest() -> AssetManifest:
    """Return the installed manifest.

    ``mount_web`` installs it at start-up, so a request never builds one. A
    page rendered by an app that did not run ``mount_web`` (a bare application
    with only the error handlers installed) gets the package tree's manifest,
    built and installed on first use; it is identical to the one ``mount_web``
    would build.
    """
    if _manifest is None:
        install_manifest(AssetManifest.build(PACKAGE_STATIC_DIR))
    assert _manifest is not None
    return _manifest


def static_url(path: str) -> str:
    """Return the versioned URL of a static asset (the templates' global)."""
    return current_manifest().url(path)


def css_bundle() -> tuple[bytes, str]:
    """Return the in-memory stylesheet bundle and its content hash."""
    manifest = current_manifest()
    return manifest.bundle, manifest.bundle_hash


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """Whether an ``If-None-Match`` header matches ``etag`` (weak comparison)."""
    for candidate in if_none_match.split(","):
        tag = candidate.strip()
        if tag == "*" or tag.removeprefix("W/") == etag:
            return True
    return False


class VersionedStaticFiles(StaticFiles):
    """``StaticFiles`` that also serves the bundle and sets ``Cache-Control``."""

    def __init__(self, *, manifest: AssetManifest, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manifest = manifest

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve ``path`` (the bundle from memory), then apply the cache policy."""
        rel = path.replace(os.sep, "/")
        if rel == BUNDLE_PATH:
            response = self._bundle_response(scope)
        else:
            response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = self.cache_control_for(rel, scope)
        return response

    def cache_control_for(self, rel: str, scope: Scope) -> str:
        """Return the ``Cache-Control`` value for a static-relative path."""
        query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
        requested = query.get("v", [None])[0]
        current = self._manifest.current_hash(rel)
        if requested is not None and current is not None and requested == current:
            return CACHE_IMMUTABLE
        if requested is None and rel.startswith("fonts/"):
            return CACHE_FONT_UNVERSIONED
        return CACHE_REVALIDATE

    def _bundle_response(self, scope: Scope) -> Response:
        if scope["method"] not in ("GET", "HEAD"):
            raise HTTPException(status_code=405)
        etag = f'"{self._manifest.bundle_hash}"'
        if_none_match = Headers(scope=scope).get("if-none-match")
        if if_none_match is not None and _etag_matches(if_none_match, etag):
            return Response(status_code=304, headers={"ETag": etag})
        body = self._manifest.bundle
        headers = {"ETag": etag, "Content-Length": str(len(body))}
        content = b"" if scope["method"] == "HEAD" else body
        return Response(content=content, media_type="text/css", headers=headers)
