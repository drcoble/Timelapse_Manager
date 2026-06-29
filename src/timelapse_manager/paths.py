"""Frozen-aware filesystem location helpers.

This module is the single authority for two questions the rest of the
application must answer consistently:

* **Where is the application running from?** When packaged as a one-directory
  bundle (for example by PyInstaller), code and read-only resources live inside
  the bundle, which may be installed anywhere and is treated as read-only. The
  :func:`is_frozen` / :func:`bundle_root` / :func:`resource_path` helpers locate
  bundled resources without assuming the current working directory.

* **Where should mutable state live?** A frozen or service-managed process has no
  meaningful working directory, so state (the database, captured frames, the
  local API token) must default to an OS-appropriate, user-writable location
  *outside* the bundle rather than ``./``. :func:`default_state_dir` and
  :func:`default_database_url` provide those defaults; every one of them remains
  overridable via configuration and environment variables.

Resolution here is **pure**: it computes locations but never creates
directories. Directory creation stays at the points that actually need to write
(startup wiring, token generation, certificate generation), so importing this
module or constructing settings has no filesystem side effects.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Application directory name used under OS-standard data locations. Kept lower
# case and hyphen-free so it is a clean directory component on every platform.
_APP_DIR_NAME = "timelapse-manager"


def is_frozen() -> bool:
    """Return whether the application is running from a frozen bundle.

    PyInstaller (and similar freezers) set ``sys.frozen`` and expose the bundle
    root via ``sys._MEIPASS``. Either signal is treated as frozen.
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    """Return the root directory bundled resources resolve against.

    When frozen, this is ``sys._MEIPASS`` -- the directory a one-directory
    freeze unpacks its data files into (under PyInstaller 6.x one-dir layouts
    this is the ``_internal`` directory beside the executable). The packaging
    spec lays bundled resources (templates, static assets, migrations, the
    ffmpeg pin, and the ffmpeg binary) into this root, so the resolver and the
    spec agree on a single location.

    When not frozen, this is the installed package's directory, so resources
    resolve relative to the source tree exactly as they do today.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return Path(meipass)
    # Not frozen: resolve relative to this package's directory.
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """Resolve a bundled, read-only resource path under the bundle root.

    Joins ``parts`` onto :func:`bundle_root`. The result is not guaranteed to
    exist; callers that require presence check and raise their own error.
    """
    return bundle_root().joinpath(*parts)


def _repo_root() -> Path:
    """Return the repository root in a source checkout.

    This package lives at ``<repo>/src/timelapse_manager``; the migrations and
    ``alembic.ini`` sit at ``<repo>``. Two parents up from this file is that
    root. Only meaningful in a source checkout; a freeze uses :func:`bundle_root`.
    """
    return Path(__file__).resolve().parent.parent.parent


def alembic_config_path() -> Path:
    """Resolve ``alembic.ini`` independent of the working directory.

    When frozen the packaging spec lays ``alembic.ini`` at the bundle root; in a
    source checkout it lives at the repository root. Probing the bundle first
    means a freeze works from any CWD, while a checkout still finds the file in
    development. The path is returned even if absent so the caller surfaces a
    clear Alembic error rather than a confusing ``None``.
    """
    bundled = bundle_root() / "alembic.ini"
    if bundled.is_file():
        return bundled
    return _repo_root() / "alembic.ini"


def alembic_script_location() -> Path:
    """Resolve the Alembic migrations directory independent of the CWD.

    Mirrors :func:`alembic_config_path`: the ``alembic`` directory is laid into
    the bundle root when frozen and lives at the repository root in a checkout.
    Returned so the caller can override ``script_location`` (which ``alembic.ini``
    records as a working-directory-relative ``alembic``) with an absolute path.
    """
    bundled = bundle_root() / "alembic"
    if bundled.is_dir():
        return bundled
    return _repo_root() / "alembic"


def default_state_dir() -> Path:
    """Return the default directory for mutable application state.

    State must never live inside the (read-only, relocatable) bundle, and a
    frozen or service-managed process has no useful working directory, so the
    default is an OS-standard, user-writable location:

    * **Windows:** ``%LOCALAPPDATA%\\timelapse-manager`` (or ``%APPDATA%``).
    * **macOS:** ``~/Library/Application Support/timelapse-manager``.
    * **Linux / other:** ``$XDG_DATA_HOME/timelapse-manager`` when set, else
      ``~/.local/share/timelapse-manager``.

    This is only the *default*; it is fully overridable via ``TLM_PATHS__DATA_DIR``
    (and the database URL via ``TLM_DATABASE__URL``). The directory is not created
    here -- resolution is side-effect free; startup wiring creates it on first
    write.
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / _APP_DIR_NAME
        return Path.home() / "AppData" / "Local" / _APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_DIR_NAME
    # Linux and other POSIX systems: follow the XDG base directory spec.
    xdg = os.environ.get("XDG_DATA_HOME")
    base_dir = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base_dir / _APP_DIR_NAME


def default_database_url() -> str:
    """Return the default SQLite URL, anchored under :func:`default_state_dir`.

    Centralised so the engine module and the settings model share one
    authoritative default and can never drift apart. SQLAlchemy's SQLite URL
    requires an absolute path to be written as ``sqlite:////abs/path`` (four
    slashes); :func:`pathlib.Path.as_uri` is avoided because it percent-encodes
    spaces, which SQLite's path handling does not expect.
    """
    db_path = default_state_dir() / "timelapse.db"
    return f"sqlite:///{db_path}"
