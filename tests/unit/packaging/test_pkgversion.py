"""PEP 440 to deb/rpm version mapping (#303).

The mapping must make a pre-release sort *below* its final in both dpkg and rpm
(PEP 440's own order), which a raw ``0.2.0.dev10`` does not. The ordering tests
run the corpus through a pure-Python port of each tool's comparison (so they run
on macOS) and, where the real tool exists, through ``dpkg --compare-versions`` and
``rpm``'s ``labelCompare`` / ``rpmdev-vercmp`` as well.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from functools import cmp_to_key
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "packaging" / "pkgversion.py"
_spec = importlib.util.spec_from_file_location("pkgversion", _SCRIPT)
assert _spec is not None and _spec.loader is not None
pkgversion = importlib.util.module_from_spec(_spec)
sys.modules["pkgversion"] = pkgversion
_spec.loader.exec_module(pkgversion)

# Ascending PEP 440 order.
_ORDERED = [
    "0.1.0",
    "0.1.1rc1",
    "0.1.1",
    "0.2.0.dev7",
    "0.2.0.dev10",
    "0.2.0rc1.dev3",
    "0.2.0rc1",
    "0.2.0rc6",
    "0.2.0",
    "0.2.0.post1",
    "0.2.0.post2",
    "0.2.1.dev1",
    "0.2.1",
    "0.10.0",
    "1.0",
    "1.0.1",
]


class TestMapping:
    @pytest.mark.parametrize(
        ("pep440", "version", "prerelease", "metadata", "full"),
        [
            ("0.2.0.dev10", "0.2.0", "dev10", "", "0.2.0~dev10"),
            ("0.2.0rc6", "0.2.0", "rc6", "", "0.2.0~rc6"),
            ("0.2.0rc1.dev3", "0.2.0", "rc1~dev3", "", "0.2.0~rc1~dev3"),
            ("0.2.0", "0.2.0", "", "", "0.2.0"),
            ("0.2.0.post1", "0.2.0", "", "post1", "0.2.0+post1"),
            ("0.2.0rc1.post1", "0.2.0", "rc1", "post1", "0.2.0~rc1+post1"),
            ("1.0", "1.0", "", "", "1.0"),
        ],
    )
    def test_table(
        self, pep440: str, version: str, prerelease: str, metadata: str, full: str
    ) -> None:
        mapped = pkgversion.map_version(pep440)
        assert (mapped.version, mapped.prerelease, mapped.metadata) == (
            version,
            prerelease,
            metadata,
        )
        assert mapped.full == full

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "v0.2.0",
            "1!0.2.0",
            "0.2.0+local",
            "0.2.0.post1.dev2",
            "0.2.0-rc1",
            "0.2.0a1",
            "0.2.0b2",
            "x",
        ],
    )
    def test_unsupported_versions_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="unsupported version"):
            pkgversion.map_version(bad)

    def test_cli_prints_each_field(self) -> None:
        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(_SCRIPT), *args],
                capture_output=True,
                text=True,
                check=False,
            )

        assert run("0.2.0.dev10").stdout == "0.2.0~dev10\n"
        assert run("0.2.0.dev10", "--field", "version").stdout == "0.2.0\n"
        assert run("0.2.0.dev10", "--field", "prerelease").stdout == "dev10\n"
        assert run("0.2.0", "--field", "metadata").stdout == "\n"
        bad = run("nonsense")
        assert bad.returncode == 2 and "unsupported version" in bad.stderr

    def test_the_project_version_maps(self) -> None:
        from timelapse_manager import __version__

        assert pkgversion.map_version(__version__).full


# ---------------------------------------------------------------------------
# Pure-Python ports of the two comparators
# ---------------------------------------------------------------------------


def _dpkg_order(char: str) -> int:
    """dpkg's per-character weight: ``~`` lowest, letters before others."""
    if char == "~":
        return -1
    if char.isdigit() or char == "":
        return 0
    if char.isalpha():
        return ord(char)
    return ord(char) + 256


def _dpkg_verrevcmp(a: str, b: str) -> int:
    """dpkg's ``verrevcmp`` for one version part (no epoch, no revision)."""
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (
            j < len(b) and not b[j].isdigit()
        ):
            ac = _dpkg_order(a[i]) if i < len(a) else 0
            bc = _dpkg_order(b[j]) if j < len(b) else 0
            if ac != bc:
                return -1 if ac < bc else 1
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return -1 if first_diff < 0 else 1
    return 0


def _dpkg_cmp(a: str, b: str) -> int:
    """Compare two ``upstream-revision`` strings the way dpkg does."""

    def split(version: str) -> tuple[str, str]:
        upstream, _, revision = version.rpartition("-")
        return (upstream, revision) if upstream else (version, "")

    (ua, ra), (ub, rb) = split(a), split(b)
    return _dpkg_verrevcmp(ua, ub) or _dpkg_verrevcmp(ra, rb)


def _rpmvercmp(a: str, b: str) -> int:
    """A port of rpm's ``rpmvercmp`` (rpm 4.16, ``~`` supported, ``^`` omitted)."""
    if a == b:
        return 0
    i = j = 0
    while i < len(a) or j < len(b):
        while i < len(a) and not a[i].isalnum() and a[i] != "~":
            i += 1
        while j < len(b) and not b[j].isalnum() and b[j] != "~":
            j += 1
        a_tilde = i < len(a) and a[i] == "~"
        b_tilde = j < len(b) and b[j] == "~"
        if a_tilde or b_tilde:
            if not a_tilde:
                return 1
            if not b_tilde:
                return -1
            i += 1
            j += 1
            continue
        if i >= len(a) or j >= len(b):
            break
        start_i, start_j = i, j
        numeric = a[i].isdigit()
        pick = str.isdigit if numeric else str.isalpha
        while i < len(a) and pick(a[i]):
            i += 1
        while j < len(b) and pick(b[j]):
            j += 1
        seg_a, seg_b = a[start_i:i], b[start_j:j]
        if not seg_b:
            return 1 if numeric else -1
        if numeric:
            seg_a, seg_b = seg_a.lstrip("0"), seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
        if seg_a != seg_b:
            return 1 if seg_a > seg_b else -1
    if i >= len(a) and j >= len(b):
        return 0
    return -1 if i >= len(a) else 1


def _rpm_cmp(a: str, b: str) -> int:
    """Compare ``version-release`` strings: version first, then release."""
    (va, _, ra), (vb, _, rb) = a.rpartition("-"), b.rpartition("-")
    return _rpmvercmp(va, vb) or _rpmvercmp(ra, rb)


def _evr(pep440: str) -> str:
    return pkgversion.map_version(pep440).full + "-1"


class TestOrdering:
    def test_the_corpus_is_in_pep440_order(self) -> None:
        from packaging.version import Version

        versions = [Version(v) for v in _ORDERED]
        assert versions == sorted(versions)

    def test_the_raw_pep440_strings_would_sort_wrongly(self) -> None:
        """Why the mapping exists: dev/rc sort after the final in dpkg/rpm."""
        assert _dpkg_cmp("0.2.0.dev10-1", "0.2.0-1") > 0
        assert _dpkg_cmp("0.2.0rc6-1", "0.2.0-1") > 0
        assert _rpmvercmp("0.2.0.dev10", "0.2.0") > 0

    @pytest.mark.parametrize("compare", [_dpkg_cmp, _rpm_cmp], ids=["dpkg", "rpm"])
    def test_pure_python_comparators_keep_pep440_order(self, compare) -> None:  # noqa: ANN001
        mapped = [_evr(v) for v in _ORDERED]
        assert sorted(mapped, key=cmp_to_key(compare)) == mapped
        for lower, higher in zip(mapped, mapped[1:], strict=False):
            assert compare(lower, higher) < 0, (lower, higher)
            assert compare(higher, lower) > 0, (higher, lower)

    def test_comparators_agree_with_known_reference_results(self) -> None:
        """Guards the ports themselves (values from dpkg/rpm documentation)."""
        assert _dpkg_cmp("1.0~rc1-1", "1.0-1") < 0
        assert _dpkg_cmp("1.0~~-1", "1.0~-1") < 0
        assert _dpkg_cmp("1.0+b1-1", "1.0-1") > 0
        assert _dpkg_cmp("1.0-1", "1.0-1") == 0
        assert _rpmvercmp("1.0~rc1", "1.0") < 0
        assert _rpmvercmp("1.0~~", "1.0~") < 0
        assert _rpmvercmp("1.0.1", "1.0") > 0
        assert _rpmvercmp("1.0", "1.0") == 0

    @pytest.mark.skipif(shutil.which("dpkg") is None, reason="needs dpkg")
    def test_real_dpkg_agrees(self) -> None:
        mapped = [_evr(v) for v in _ORDERED]
        for lower, higher in zip(mapped, mapped[1:], strict=False):
            result = subprocess.run(
                ["dpkg", "--compare-versions", lower, "lt", higher], check=False
            )
            assert result.returncode == 0, (lower, higher)

    @pytest.mark.skipif(
        shutil.which("rpmdev-vercmp") is None
        and importlib.util.find_spec("rpm") is None,
        reason="needs rpm's python bindings or rpmdev-vercmp",
    )
    def test_real_rpm_agrees(self) -> None:
        mapped = [_evr(v) for v in _ORDERED]
        for lower, higher in zip(mapped, mapped[1:], strict=False):
            if importlib.util.find_spec("rpm") is not None:
                import rpm  # type: ignore[import-not-found,unused-ignore]

                def evr(text: str) -> tuple[str, str, str]:
                    version, _, release = text.rpartition("-")
                    return ("0", version, release)

                assert rpm.labelCompare(evr(lower), evr(higher)) < 0, (lower, higher)
            else:
                # rpmdev-vercmp exits 12 when the first argument is older.
                result = subprocess.run(
                    ["rpmdev-vercmp", lower, higher], check=False, capture_output=True
                )
                assert result.returncode == 12, (lower, higher)

    def test_nfpm_fields_join_to_the_sorted_form(self) -> None:
        """What nfpm renders (version~prerelease+metadata) is ``full``."""
        pattern = re.compile(r"^\d+(\.\d+)*(~[a-z0-9~]+)?(\+post\d+)?$")
        for value in _ORDERED:
            assert pattern.match(pkgversion.map_version(value).full), value
