"""On-disk path layout for a project's frames, with relocatable storage.

There are two representations of a frame file's location and one rule that keeps
them coherent:

* The **physical** location is ``<frames root or project override>/<file>``. The
  per-project directory is computed by :func:`frame_dir`, which honours a
  project's explicit ``storage_path`` and otherwise falls back to a per-project
  sub-directory under the configured frames root. This layer is the single source
  of truth for that decision.
* The **stored** representation (the value persisted in a frame row) is kept
  *relative to the project's frame directory* for projects that use the default
  layout, so the whole frames tree can be relocated to a new root without
  rewriting any rows. Projects with an explicit ``storage_path`` (and any rows
  written before relative storage existed) store an **absolute** path instead,
  because a relative value cannot be re-anchored from the project id alone.

:func:`to_stored` encodes that rule on write; :func:`resolve_absolute` reverses
it on read, returning an absolute path whether the stored value is relative or
absolute. Callers that need a usable filesystem path always go through
:func:`resolve_absolute` and never interpret a stored value directly.
"""

from __future__ import annotations

import os
import os.path
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config import Settings


class _HasStorage(Protocol):
    """The minimal project shape the path helpers depend on.

    Both an ORM ``Project`` and the lightweight :class:`ProjectRef` below satisfy
    this, so the helpers never need to import the model or hold a live row.
    """

    @property
    def id(self) -> int: ...

    @property
    def storage_path(self) -> str | None: ...


@dataclass(frozen=True)
class ProjectRef:
    """A detached ``(id, storage_path)`` pair for path computation.

    Lets the capture loop and the writer derive paths from a snapshot rather than
    a live ORM row, mirroring the project's ``id`` / ``storage_path`` fields.
    """

    id: int
    storage_path: str | None = None


def frames_root(settings: Settings) -> Path:
    """Return the configured root directory all default-layout frames live under."""
    root = settings.paths.frames_root
    # Populated by the PathsSettings validator; assert keeps the type narrowed.
    assert root is not None
    return Path(root)


def frame_dir_under_root(root: Path, project: _HasStorage) -> Path:
    """Return a project's frame directory given an explicit frames ``root``.

    The root-based core of :func:`frame_dir`: honours the project's explicit
    ``storage_path`` and otherwise falls back to ``root / project.id``. Callers
    that already hold a frames root (the writer) use this so the per-project
    layout has a single definition.
    """
    if project.storage_path:
        return Path(project.storage_path)
    return root / str(project.id)


def frame_dir(settings: Settings, project: _HasStorage) -> Path:
    """Return the directory frames for ``project`` are written to and read from.

    Honours the project's explicit ``storage_path``; otherwise falls back to a
    per-project sub-directory under the configured frames root. This is the one
    place the physical per-project location is decided.
    """
    return frame_dir_under_root(frames_root(settings), project)


# Widths (px) the thumbnail route produces: the frame-grid tile (320, the
# default) and the dashboard card / project header (640, #70 F-P2).
DEFAULT_THUMBNAIL_WIDTH = 320
THUMBNAIL_WIDTHS: frozenset[int] = frozenset({DEFAULT_THUMBNAIL_WIDTH, 640})

# Marker in the name of a thumbnail ffmpeg is still writing
# (``<name>.tmp-<pid>-<random>.jpg``). Housekeeping removes old ones.
THUMBNAIL_TEMP_MARKER = ".tmp-"


def thumbnail_cache_root(settings: Settings) -> Path:
    """Return the directory every project's thumbnail cache lives under."""
    return Path(settings.paths.data_dir) / "thumbnails"


def thumbnail_cache_dir(settings: Settings, project_id: int) -> Path:
    """Return the on-disk cache directory for a project's frame thumbnails.

    Thumbnails are a regenerable derivative, so they live under the data
    directory (``<data_dir>/thumbnails/<project_id>``) rather than alongside the
    original frames -- keeping the frame store pristine and the cache trivially
    discardable. Code that reads, writes or deletes in it goes through
    :func:`contained_thumbnail_dir`.
    """
    return thumbnail_cache_root(settings) / str(int(project_id))


def contained_thumbnail_dir(settings: Settings, project_id: int) -> Path:
    """Return the real path of a project's thumbnail cache directory, or raise.

    The directory must resolve strictly inside :func:`thumbnail_cache_root`: a
    ``<project_id>`` entry that is a symlink to somewhere else (or to the root
    itself) raises :class:`PathContainmentError`, so thumbnail writes and
    deletes never leave the cache. The directory need not exist yet.

    :raises PathContainmentError: the directory resolves outside the cache root.
    """
    root = _real(thumbnail_cache_root(settings))
    real = _real(thumbnail_cache_dir(settings, project_id))
    if real == root or not _is_within(real, root):
        raise PathContainmentError(
            f"thumbnail cache of project {project_id} is outside the cache root"
        )
    return Path(real)


def thumbnail_file_name(frame_id: int, width: int) -> str:
    """Return the cache file name of a frame's thumbnail at ``width`` px.

    ``<frame_id>.jpg`` for the default width (the name before sizes existed) and
    ``<frame_id>_<width>.jpg`` otherwise.

    :raises ValueError: ``width`` is not one of :data:`THUMBNAIL_WIDTHS`.
    """
    if width not in THUMBNAIL_WIDTHS:
        raise ValueError(f"unsupported thumbnail width {width}")
    frame = int(frame_id)
    if width == DEFAULT_THUMBNAIL_WIDTH:
        return f"{frame}.jpg"
    return f"{frame}_{width}.jpg"


def uses_default_layout(project: _HasStorage) -> bool:
    """Return whether ``project`` stores frames under the shared frames root.

    A project with an explicit ``storage_path`` does not, so its frames are
    stored as absolute paths (they cannot be re-anchored from the project id).
    """
    return not project.storage_path


def to_stored(project: _HasStorage, absolute_path: Path) -> str:
    """Return the value to persist for a frame at ``absolute_path``.

    For a default-layout project the path is stored *relative to* the project's
    frame directory, so the frames tree stays relocatable. For a project with an
    explicit ``storage_path`` the absolute path is stored as-is, because a
    relative value cannot be resolved from the project id alone.
    """
    if uses_default_layout(project):
        return absolute_path.name
    return str(absolute_path)


def resolve_absolute(settings: Settings, project_id: int, stored_path: str) -> Path:
    """Return an absolute filesystem path for a stored frame location.

    A stored value that is already absolute is returned unchanged -- this covers
    both projects with an explicit ``storage_path`` and legacy rows written
    before relative storage existed. A relative value is anchored under the
    default per-project frame directory (``frames_root / project_id``); such
    values are only ever written for default-layout projects, so anchoring from
    the project id alone is correct.
    """
    candidate = Path(stored_path)
    if candidate.is_absolute():
        return candidate
    return frames_root(settings) / str(project_id) / candidate


# ---------------------------------------------------------------------------
# storage_path policy (#67 S5) and frame-path containment (#67 S16)
# ---------------------------------------------------------------------------

# The environment variable the service unit uses to name its config file; its
# directory is protected like the data directory, as is the directory of a file
# passed with ``--config`` (recorded on the loaded settings).
_CONFIG_PATH_ENV = "TLM_CONFIG"

# Longest storage_path accepted, in encoded bytes (Linux PATH_MAX).
_MAX_STORAGE_PATH_BYTES = 4096

# Unicode line and paragraph separators: not C0/C1 controls, but some parsers
# treat them as line breaks, so they are refused with the controls.
_LINE_SEPARATORS = frozenset("\u2028\u2029")


class StoragePathError(ValueError):
    """A proposed ``storage_path`` breaks the storage policy.

    The message is safe to show to the user who submitted the path.
    """


class PathContainmentError(Exception):
    """A stored frame path resolves outside its project's frame directories."""


def _identity(path: str) -> tuple[int, int] | None:
    """Return ``(st_dev, st_ino)`` for an existing ``path``, or ``None``."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


def _is_within(path: str, root: str) -> bool:
    """Return whether real path ``path`` is ``root`` or lies beneath it.

    Both arguments must already be :func:`os.path.realpath` results. The string
    test runs first and is component-wise, so ``/data/frames-old`` is not inside
    ``/data/frames``, and a root of ``/`` contains everything.

    Strings alone are not enough: ``realpath`` does not canonicalise case, so on
    a case-insensitive filesystem (APFS, SMB/CIFS) ``/Var/Lib/x`` names the same
    directory as ``/var/lib/x`` under a different string, and a bind mount gives
    a directory a second name. So when the strings disagree, filesystem identity
    decides: ``path`` is inside ``root`` when ``path`` or any existing ancestor
    of it is the same directory (``st_dev``, ``st_ino``) as ``root``. A path that
    does not exist yet is judged by its existing ancestors. A ``root`` that does
    not exist has no identity, so only the string test applies to it.
    """
    if path == root or os.path.commonpath((path, root)) == root:
        return True
    root_id = _identity(root)
    if root_id is None:
        return False
    current = path
    while True:
        if _identity(current) == root_id:
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _real(path: str | os.PathLike[str]) -> str:
    """Return the canonical form of ``path`` (symlinks resolved, non-strict).

    Components that do not exist yet are kept literally, so a directory the
    capture writer will create later still gets a definite answer.
    """
    return os.path.realpath(os.fspath(path))


def _protected_dirs(settings: Settings) -> list[str]:
    """Return the real directories no project storage may overlap.

    The data directory (database, key file, token, TLS material, thumbnails,
    reports) heads the list; the rest cover deployments that move one of those
    files elsewhere, plus the directory of the config file in use, whether it came
    from ``--config`` or ``TLM_CONFIG``. The
    filesystem root is never listed (by name or by identity): a file configured
    at ``/`` would otherwise forbid every path.
    """
    from ..config.loader import settings_config_file
    from ..db.migrate import sqlite_file

    data_dir = Path(settings.paths.data_dir)
    candidates: list[Path] = [data_dir, data_dir / "thumbnails", data_dir / "reports"]
    files: list[Path | str | None] = [
        settings.paths.token_file,
        settings.tls.cert_path,
        settings.tls.key_path,
        settings.secrets.key_file,
        settings.observability.metrics_token_file,
        settings.reporting.gitea.token_file,
        sqlite_file(settings.database.url),
        # The file the settings were loaded from (``--config`` or ``TLM_CONFIG``),
        # plus ``TLM_CONFIG`` itself for settings built without the loader (#172).
        settings_config_file(settings),
        os.environ.get(_CONFIG_PATH_ENV) or None,
    ]
    for file_path in files:
        if file_path is not None:
            candidates.append(Path(os.path.abspath(os.fspath(file_path))).parent)
    filesystem_root = _identity(os.sep)
    seen: list[str] = []
    for candidate in candidates:
        real = _real(os.path.abspath(candidate))
        if real == os.sep or real in seen:
            continue
        identity = _identity(real)
        if identity is not None and identity == filesystem_root:
            continue
        seen.append(real)
    return seen


def _overlaps_protected(path: str, protected: Iterable[str], frames: str) -> str | None:
    """Return the protected directory ``path`` falls inside, or ``None``.

    ``frames`` (the real frames root) is carved out of every protected directory
    that *contains* it: the default layout keeps the frames tree inside the data
    directory, and a custom path inside that tree is legitimate. A protected
    directory that sits inside the frames tree gets no carve-out.
    """
    for directory in protected:
        if not _is_within(path, directory):
            continue
        carved_out = (
            _is_within(frames, directory)
            and not _is_within(directory, frames)
            and _is_within(path, frames)
        )
        if not carved_out:
            return directory
    return None


def _allowed_roots(settings: Settings) -> list[str]:
    """Return the real directories operators may use; default ``[frames_root]``."""
    configured = settings.storage.allowed_roots
    roots = [frames_root(settings)] if configured is None else list(configured)
    return [_real(root) for root in roots]


def _check_storage_path_text(text: str) -> None:
    """Reject a storage path that is malformed before any filesystem lookup.

    Checked on the raw text, before :func:`os.path.realpath` sees it: control
    characters (a NUL would make the lookup raise, and a line break would split
    an ffmpeg concat entry), text that is not valid UTF-8, a
    relative path, and any ``..`` component.
    """
    if any(
        ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in _LINE_SEPARATORS
        for ch in text
    ):
        raise StoragePathError("storage path must not contain control characters")
    try:
        # Strict UTF-8, not os.fsencode: surrogateescape would let a lone
        # surrogate through, which the database then cannot store.
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StoragePathError("storage path contains invalid characters") from exc
    if len(encoded) > _MAX_STORAGE_PATH_BYTES:
        raise StoragePathError(
            f"storage path is longer than {_MAX_STORAGE_PATH_BYTES} bytes"
        )
    if not os.path.isabs(text):
        raise StoragePathError("storage path must be an absolute path")
    if ".." in text.replace("\\", "/").split("/"):
        raise StoragePathError("storage path must not contain '..'")


def validate_storage_path(
    raw: str | None,
    settings: Settings,
    *,
    is_admin: bool,
    project_id: int | None = None,
    other_storage_paths: Iterable[str] = (),
) -> str | None:
    """Check a proposed project ``storage_path`` and return the value to store.

    Blank (or ``None``) means the default layout and returns ``None``. Anything
    else must be an absolute path with no control characters and no ``..``
    component. It is then resolved with :func:`os.path.realpath` (symlinks in
    the existing part followed), and the resolved path is what gets stored, so
    the later containment checks compare like with like.

    The resolved path **and its parent** are checked, because renders are
    written beside the frames (``<parent>/<render.output_subdir>``):

    * neither may lie inside the data directory, unless inside the frames root;
    * neither may lie inside the config directory, the token, key or
      certificate directories, ``<data_dir>/thumbnails`` or
      ``<data_dir>/reports``;
    * the path may not contain one of those directories or be (or contain) the
      frames root itself;
    * for a non-admin, both must also lie inside ``storage.allowed_roots``;
    * it may not collide with the default layout (#171): its last component may
      not be ``render.output_subdir`` (that directory holds rendered videos), and
      a direct child of the frames root may not be named with a number other
      than ``project_id`` (``<frames_root>/<n>`` is project ``n``'s default
      directory; a project that is not yet saved has no id, so any number is
      refused);
    * it may not equal a path in ``other_storage_paths`` (the paths other
      projects already use), so two projects never write into one directory.

    :raises StoragePathError: the path breaks the policy; the message says why
        without listing the server's directories.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    _check_storage_path_text(text)
    try:
        real = _real(text)
    except (OSError, ValueError) as exc:
        raise StoragePathError("storage path cannot be resolved") from exc
    parent = os.path.dirname(real)

    frames = _real(frames_root(settings))
    protected = _protected_dirs(settings)
    if any(_is_within(directory, real) for directory in protected):
        raise StoragePathError(
            "storage path must not contain the application's data or config directory"
        )
    if _is_within(frames, real):
        raise StoragePathError(
            "storage path must be a sub-directory of the frames root, not the "
            "frames root itself or a directory above it"
        )
    for candidate in (real, parent):
        if _overlaps_protected(candidate, protected, frames) is not None:
            raise StoragePathError(
                "storage path (or its parent, where renders are written) must "
                "not be inside the application's data, config, token or "
                "certificate directories"
            )
    _check_layout_collision(real, frames, settings, project_id)
    if any(_same_real_path(real, other) for other in other_storage_paths):
        raise StoragePathError("storage path is already used by another project")
    if not is_admin:
        roots = _allowed_roots(settings)
        if not any(
            _is_within(real, root) and _is_within(parent, root) for root in roots
        ):
            raise StoragePathError(
                "storage path (and its parent) must be inside a storage root an "
                "administrator has allowed (storage.allowed_roots); ask an "
                "administrator to set it"
            )
    return real


def _same_real_path(real: str, other: str) -> bool:
    """Return whether the stored path ``other`` resolves to ``real``.

    A stored value that cannot be resolved (a legacy row with an embedded NUL)
    is not the same path as anything; it must not break the check.
    """
    try:
        return real == _real(other)
    except (OSError, ValueError):
        return False


def _check_layout_collision(
    real: str, frames: str, settings: Settings, project_id: int | None
) -> None:
    """Refuse a ``storage_path`` that lands on a directory the default layout owns.

    ``real`` and ``frames`` are real paths. See :func:`validate_storage_path`.

    :raises StoragePathError: the path collides with the render directory or with
        another project's default frame directory.
    """
    name = os.path.basename(real)
    if name == settings.render.output_subdir:
        raise StoragePathError(
            f"storage path must not be named {name!r}: that directory holds "
            "rendered videos"
        )
    numbered = os.path.dirname(real) == frames and name.isascii() and name.isdigit()
    if numbered and (project_id is None or int(name) != project_id):
        raise StoragePathError(
            "storage path must not be a numbered directory of the frames "
            "root: those are the default directories of projects"
        )


def frame_boundaries(settings: Settings, project: _HasStorage) -> tuple[str, ...]:
    """Return the real directories a project's frame files may live in.

    Its current frame directory (the ``storage_path`` override, or the default
    per-project directory) and the default per-project directory under the
    frames root: frames captured before a ``storage_path`` was set keep relative
    paths, which still resolve there. Compute once per batch and pass it to
    :func:`resolve_contained_frame_path`.
    """
    current = _real(frame_dir(settings, project))
    default = _real(frames_root(settings) / str(project.id))
    return (current,) if current == default else (current, default)


def resolve_contained_frame_path(
    settings: Settings,
    project: _HasStorage,
    stored_path: str,
    *,
    boundaries: tuple[str, ...] | None = None,
) -> Path:
    """Return the real on-disk path of a stored frame, or raise.

    The stored value is resolved (:func:`resolve_absolute`), every symlink is
    followed (:func:`os.path.realpath`), and the result must lie inside one of
    the project's :func:`frame_boundaries`. A traversal, an absolute escape or a
    symlink pointing elsewhere raises :class:`PathContainmentError`. Existence is
    not checked; callers that serve or bundle the file check that themselves.

    The real path is returned so the caller opens exactly the file that was
    checked, rather than following the symlinks again later.

    :raises PathContainmentError: the path escapes the project's directories or
        cannot be resolved.
    """
    bounds = (
        boundaries if boundaries is not None else frame_boundaries(settings, project)
    )
    candidate = resolve_absolute(settings, project.id, stored_path)
    try:
        real = _real(candidate)
    except (OSError, ValueError) as exc:
        raise PathContainmentError(
            f"frame path for project {project.id} cannot be resolved"
        ) from exc
    if not any(_is_within(real, bound) for bound in bounds):
        raise PathContainmentError(
            f"frame path for project {project.id} is outside its frame directory"
        )
    return Path(real)
