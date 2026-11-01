"""Static checks on ``timelapse-manager.spec`` that guard the EL9 glibc floor."""

from __future__ import annotations

from pathlib import Path

_SPEC = (Path(__file__).resolve().parents[3] / "timelapse-manager.spec").read_text(
    encoding="utf-8"
)


def test_the_build_hosts_libgcc_s_is_not_bundled() -> None:
    """EL9's libgcc_s imports GLIBC_2.35 (backported); targets use their own."""
    assert '_SYSTEM_LIBS = {"libgcc_s.so.1"}' in _SPEC
    assert "a.binaries = [" in _SPEC and "not in _SYSTEM_LIBS" in _SPEC
    # The filter must run before the binaries reach COLLECT.
    assert _SPEC.index("not in _SYSTEM_LIBS") < _SPEC.index("coll = COLLECT(")
