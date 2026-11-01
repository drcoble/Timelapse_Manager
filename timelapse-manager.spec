# PyInstaller build specification for Timelapse Manager.
#
# Produces a one-directory, console application whose executable is named
# ``timelapse-manager``. The frozen entry point is the CLI, so the same binary
# serves every subcommand; in particular ``timelapse-manager run`` runs the
# service in the foreground (what a service manager's ExecStart invokes), and
# ``timelapse-manager migrate`` applies database migrations from any working
# directory.
#
# Resource placement is deliberate and must agree with how the application
# resolves resources at runtime (see ``timelapse_manager/paths.py`` and
# ``ffmpeg_pin.py``):
#
#   * Resources resolved against the *bundle root* (``sys._MEIPASS``) are placed
#     at ``.``: ``ffmpeg-pin.json``, ``alembic.ini``, the whole ``alembic/``
#     migrations tree, and the bundled ffmpeg under ``ffmpeg/``.
#   * Resources resolved against the *package directory* via ``__file__`` are
#     placed under ``timelapse_manager/...``: the web ``templates/`` and
#     ``static/`` trees keep their package-relative location so Jinja2 and the
#     static mount find them when frozen.
#
# Mutable state (database, captured frames, local token) is never bundled; it
# defaults to an OS-standard writable directory outside this (read-only) bundle
# and is overridable via ``TLM_*`` environment variables.
#
# Bundled ffmpeg sourcing: a self-contained Linux release lays the pinned static
# ffmpeg/ffprobe (the build named in ``ffmpeg-pin.json``) into ``ffmpeg/``. The
# binary to embed is taken from the ``TLM_BUNDLE_FFMPEG_DIR`` environment
# variable when set (a directory containing ``ffmpeg``[/``ffprobe``]); otherwise
# the spec falls back to the ``ffmpeg``/``ffprobe`` found on PATH, which is what
# a local macOS build uses to validate that resolution wiring works when frozen.
# (Container deployments do not use this path at all -- they set the
# ``render.ffmpeg_binary`` knob and point at a fixed in-image path.)

import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

spec_dir = Path(SPECPATH).resolve()
pkg_dir = spec_dir / "src" / "timelapse_manager"


# --- Data files -------------------------------------------------------------

datas = []

# Bundle-root resources (resolved against sys._MEIPASS at runtime).
datas.append((str(spec_dir / "ffmpeg-pin.json"), "."))
datas.append((str(spec_dir / "alembic.ini"), "."))
# The entire migrations tree: env.py, script.py.mako, and every versions/*.py.
# Alembic needs the env module and the mako template to run from script_location,
# not just the revision files.
datas.append((str(spec_dir / "alembic"), "alembic"))

# Package-relative web resources (resolved against __file__ at runtime), so they
# must keep their position under the package directory inside the bundle.
datas.append((str(pkg_dir / "web" / "templates"), "timelapse_manager/web/templates"))
datas.append((str(pkg_dir / "web" / "static"), "timelapse_manager/web/static"))

# The IANA time-zone database, shipped as the ``tzdata`` package's data files, so
# zoneinfo resolves zones inside the frozen bundle on hosts that lack a system
# zoneinfo tree. Viewer-local timestamp rendering depends on this; without it a
# minimal host would fall back to UTC-only or error on an unknown zone.
datas += collect_data_files("tzdata")


# --- Bundled ffmpeg ---------------------------------------------------------
#
# Lay ffmpeg (and ffprobe when available) into ``ffmpeg/`` at the bundle root,
# which is exactly where ``resolve_ffmpeg_binary`` looks when frozen. The source
# directory is configurable so a Linux release build can drop the pinned static
# binary in; a local build falls back to whatever is on PATH for validation.

def _ffmpeg_source(name):
    """Locate an ffmpeg-family binary to embed, or return None.

    Honors ``TLM_BUNDLE_FFMPEG_DIR`` (a directory holding the binaries); falls
    back to the binary on PATH so a local validation build still embeds one.
    """
    suffix = ".exe" if os.name == "nt" else ""
    staging = os.environ.get("TLM_BUNDLE_FFMPEG_DIR")
    if staging:
        candidate = Path(staging) / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    return found


_ffmpeg_binaries = []
for _name in ("ffmpeg", "ffprobe"):
    _src = _ffmpeg_source(_name)
    if _src is not None:
        # Placed as data (not a PyInstaller binary) so it lands verbatim at the
        # exact bundle path the resolver expects, without dependency analysis.
        _ffmpeg_binaries.append((_src, "ffmpeg"))
datas += _ffmpeg_binaries


# --- Hidden imports ---------------------------------------------------------
#
# Modules reached only by dynamic import (string names, plugin registries, ASGI
# server internals) are invisible to static analysis and must be declared.

hiddenimports = []
# Uvicorn loads its protocol/loop/lifespan implementations by name.
hiddenimports += collect_submodules("uvicorn")
# Pydantic v2 / pydantic-settings and their compiled core.
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("pydantic_settings")
# SQLAlchemy resolves dialects (the SQLite one in particular) dynamically.
hiddenimports += collect_submodules("sqlalchemy.dialects")
# Alembic loads the migration environment and runtime by name.
hiddenimports += collect_submodules("alembic")
# The application's own packages, so dynamically referenced submodules (routers,
# channels, migration env) are all present in the freeze.
hiddenimports += collect_submodules("timelapse_manager")
# tzdata exposes per-region subpackages that zoneinfo imports by name.
hiddenimports += collect_submodules("tzdata")
# Explicit leaf modules that are imported lazily within functions.
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]


# --- Analysis / build -------------------------------------------------------

block_cipher = None

a = Analysis(
    [str(pkg_dir / "cli" / "__main__.py")],
    pathex=[str(spec_dir / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# System runtime libraries the bundle must NOT carry. PyInstaller collects
# libgcc_s.so.1 from the build host, and on Enterprise Linux 9 that copy imports
# GLIBC_2.35 symbols (Red Hat backports _dl_find_object into its glibc 2.34), so
# shipping it would break hosts running an earlier EL9 minor release. Every target
# (EL9, Debian, the container and CT images) has its own libgcc_s matching its own
# glibc, which the frozen executable then loads from the system.
_SYSTEM_LIBS = {"libgcc_s.so.1"}
a.binaries = [b for b in a.binaries if os.path.basename(b[0]) not in _SYSTEM_LIBS]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="timelapse-manager",
    debug=False,
    bootloader_ignore_signals=False,
    # strip/upx must stay off: py-spy (docs/operations/debugging.md) needs the
    # unstripped libpython symbols to find the interpreter state. The release
    # CI's py-spy smoke step catches a regression.
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    # Keep off for py-spy, as on the EXE above.
    strip=False,
    upx=False,
    upx_exclude=[],
    name="timelapse-manager",
)
