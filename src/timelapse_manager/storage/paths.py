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


def thumbnail_cache_dir(settings: Settings, project_id: int) -> Path:
    """Return the on-disk cache directory for a project's frame thumbnails.

    Thumbnails are a regenerable derivative, so they live under the data
    directory (``<data_dir>/thumbnails/<project_id>``) rather than alongside the
    original frames -- keeping the frame store pristine and the cache trivially
    discardable.
    """
    return Path(settings.paths.data_dir) / "thumbnails" / str(project_id)


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
