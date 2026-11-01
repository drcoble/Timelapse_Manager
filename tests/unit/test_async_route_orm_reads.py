"""An ``async def`` route never reads an ORM row's attributes on the loop (#66 L3).

Reading a column off an expired ORM row issues a synchronous ``SELECT`` on the
event loop. The runtime loop guard only sees reads that happen in a test, so this
static scan keeps the rule for every route: in an ``async def`` under
``web/routers`` and ``api``, no attribute is read off

* a parameter annotated with an ORM model (or the ``CurrentUser`` family),
* a name bound from ``await run_in_threadpool(...)`` / ``asyncio.to_thread(...)``
  that is an ORM row (``db.get(Model, ...)`` or a helper annotated to return one),

and ``Actor.of(...)`` is never *called* in the async body (hand the function to a
hop instead). Session work belongs in a sync helper run in the pool, which hands
back plain values (see ``web/routers/_values.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "timelapse_manager"
SCAN_DIRS = (SRC / "web" / "routers", SRC / "api")

ORM_TYPES = frozenset(
    {
        "User",
        "Camera",
        "Project",
        "RenderJob",
        "Frame",
        "CurrentUser",
        "AdminUser",
        "OperatorUser",
    }
)
HOPS = frozenset({"run_in_threadpool", "to_thread"})

#: ``(file, function, "name.attr")`` -> why it is safe. A camera passed to the
#: probes is detached (``db.expunge``) with its columns loaded, so a read cannot
#: refresh: it raises instead of issuing SQL. Never add an entry for an attached
#: row.
_DETACHED = "camera is detached (expunged) before the awaits; it cannot refresh"
KNOWN_ASYNC_ORM_READS: dict[tuple[str, str, str], str] = (
    {
        ("web/routers/_shared.py", fn, attr): _DETACHED
        for fn in (
            "_enumerate_event_topics",
            "_enumerate_stream_profiles",
            "_enumerate_ptz_presets",
        )
        for attr in ("camera.address", "camera.tls_pinned_cert_pem")
    }
    | {
        ("web/routers/cameras.py", fn, attr): _DETACHED
        for fn, attrs in {
            "validate_camera": ("camera.tls_pinned_cert_pem",),
            "camera_geolocation": ("camera.address", "camera.tls_pinned_cert_pem"),
            "camera_geolocation_admin_read": (
                "camera.protocol",
                "camera.address",
                "camera.tls_pinned_cert_pem",
            ),
            "_fetch_camera_certificate": (
                "camera.protocol",
                "camera.address",
                "camera.snapshot_uri",
                "camera.stream_uri",
            ),
        }.items()
        for attr in attrs
    }
    | {
        ("api/cameras.py", "validate_camera", "config.tls_pinned_cert_pem"): (
            "config is the camera row expunged by _load_camera_config"
        ),
    }
)


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _orm_positions(returns: ast.expr) -> frozenset[int] | None:
    """Which members of a helper's return value are ORM rows.

    ``None`` means the whole value is one (``-> Camera``, ``-> Camera | None``);
    a set holds the tuple indices for ``-> tuple[Camera, int]``; an empty set
    means no ORM row at all.
    """
    if (
        isinstance(returns, ast.Subscript)
        and isinstance(returns.value, ast.Name)
        and returns.value.id == "tuple"
        and isinstance(returns.slice, ast.Tuple)
    ):
        return frozenset(
            index
            for index, element in enumerate(returns.slice.elts)
            if _names(element) & ORM_TYPES
        )
    return None if _names(returns) & ORM_TYPES else frozenset()


def _orm_returning_helpers(tree: ast.Module) -> dict[str, frozenset[int] | None]:
    """Module-level sync functions that return an ORM row, by return position."""
    found: dict[str, frozenset[int] | None] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.returns is not None:
            positions = _orm_positions(node.returns)
            if positions is None or positions:
                found[node.name] = positions
    return found


def _is_hop(call: ast.AST) -> bool:
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name in HOPS


def _hop_orm_positions(
    call: ast.Call, helpers: dict[str, frozenset[int] | None]
) -> frozenset[int] | None | bool:
    """``False`` when the hop returns no ORM row, else its positions (see above)."""
    if not call.args:
        return False
    target = call.args[0]
    if isinstance(target, ast.Attribute) and target.attr == "get":
        is_row = len(call.args) > 1 and bool(_names(call.args[1]) & ORM_TYPES)
        return None if is_row else False
    if isinstance(target, ast.Name) and target.id in helpers:
        return helpers[target.id]
    return False


def _bound_orm_names(target: ast.expr, positions: frozenset[int] | None) -> set[str]:
    if isinstance(target, ast.Tuple) and positions is not None:
        return {
            element.id
            for index, element in enumerate(target.elts)
            if index in positions and isinstance(element, ast.Name)
        }
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _violations(path: Path) -> list[tuple[str, str, str, int]]:
    label = str(path.relative_to(SRC)) if path.is_relative_to(SRC) else path.name
    tree = ast.parse(path.read_text())
    helpers = _orm_returning_helpers(tree)
    out: list[tuple[str, str, str, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        orm_names: set[str] = set()
        for arg in fn.args.args + fn.args.kwonlyargs:
            if arg.annotation is not None and _names(arg.annotation) & ORM_TYPES:
                orm_names.add(arg.arg)
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await):
                call = node.value.value
                if isinstance(call, ast.Call) and _is_hop(call):
                    positions = _hop_orm_positions(call, helpers)
                    if positions is not False:
                        for target in node.targets:
                            orm_names |= _bound_orm_names(target, positions)  # type: ignore[arg-type]
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in orm_names
            ):
                out.append(
                    (label, fn.name, f"{node.value.id}.{node.attr}", node.lineno)
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "of"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "Actor"
            ):
                out.append((label, fn.name, "Actor.of(...)", node.lineno))
    return out


def _all_violations() -> list[tuple[str, str, str, int]]:
    found: list[tuple[str, str, str, int]] = []
    for directory in SCAN_DIRS:
        for path in sorted(directory.glob("*.py")):
            found.extend(_violations(path))
    return found


def test_no_async_route_reads_orm_attributes_on_the_loop() -> None:
    unexpected = [
        f"{file}:{line} {func}: {what}"
        for file, func, what, line in _all_violations()
        if (file, func, what) not in KNOWN_ASYNC_ORM_READS
    ]
    assert not unexpected, "ORM reads on the event loop:\n" + "\n".join(unexpected)


def test_allow_list_has_no_stale_entries() -> None:
    present = {(file, func, what) for file, func, what, _ in _all_violations()}
    stale = set(KNOWN_ASYNC_ORM_READS) - present
    assert not stale, f"stale allow-list entries: {sorted(stale)}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("async def r(user: CurrentUser):\n    return user.role\n", 1),
        ("async def r(user: CurrentUser):\n    return Actor.of(user)\n", 1),
        (
            "async def r(db, user):\n"
            "    return await run_in_threadpool(Actor.of, user)\n",
            0,
        ),
        (
            "async def r(db):\n"
            "    c = await run_in_threadpool(db.get, Camera, 1)\n"
            "    return c.address\n",
            1,
        ),
        (
            "def load(db) -> Project:\n    ...\n"
            "async def r(db):\n"
            "    p = await run_in_threadpool(load, db)\n"
            "    return p.name\n",
            1,
        ),
        (
            "def load(db) -> int:\n    ...\n"
            "async def r(db):\n"
            "    p = await run_in_threadpool(load, db)\n"
            "    return p.real\n",
            0,
        ),
    ],
)
def test_scanner_detects_the_patterns(
    tmp_path: Path, source: str, expected: int
) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(source)
    assert len(_violations(sample)) == expected
