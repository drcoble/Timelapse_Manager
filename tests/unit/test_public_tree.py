"""Guard: the published tree must carry no private infrastructure details.

The tracked tree is exported to a public repository. This guard fails when a
tracked file (or file name) matches an entry of a private deny-list.

The deny-list itself is private: it is read from the file named by
``TLM_PUBLIC_GUARD_LIST``, or from ``.gitea/public-guard/deny.txt`` in a
maintainer checkout. Neither is part of the public export, so the checks that
need the list skip (with a reason) where it is absent. The mechanism tests below
use synthetic patterns only.

List format, one entry per line, tab separated; ``#`` starts a comment::

    deny<TAB>label<TAB>regex      matched case-insensitively, as a plain substring
    sample<TAB>label<TAB>text     must be flagged (a self-check of the list)

It also works as a command-line scanner, used by ``dev/export-public.sh`` and
``dev/publish-github-release.sh``::

    python3 tests/unit/test_public_tree.py --scan DIR [--binary] [--allow FILE]
    python3 tests/unit/test_public_tree.py --names < listing.txt
    python3 tests/unit/test_public_tree.py --tracked

``--list FILE`` overrides the list location. A missing list is an error (exit 2).

Patterns are plain substrings, with no word boundaries: an embedded name is
always flagged. ``--allow FILE`` adds exceptions, one ``path regex<TAB>line
regex`` per line; each must name one file and the exact text.

Not scanned in the tracked tree: ``.gitea/`` (maintainer-only), ``docs/
security-audit.md`` and ``CHANGELOG.md`` (both left out of the export; the
export builds a public changelog and scans that).

The module only needs the standard library, so it also runs without pytest.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIST_ENV = "TLM_PUBLIC_GUARD_LIST"
DEFAULT_LIST = ROOT / ".gitea" / "public-guard" / "deny.txt"

# Paths left out of the tracked-tree scan (maintainer-only or replaced on export).
EXCLUDED_PATHS: tuple[str, ...] = (
    ".gitea/",
    "docs/security-audit.md",
    "CHANGELOG.md",
)

_CHUNK = 8 * 1024 * 1024
_OVERLAP = 256


@dataclass
class Rules:
    """A compiled deny-list."""

    deny: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    deny_bytes: list[tuple[str, re.Pattern[bytes]]] = field(default_factory=list)
    samples: list[tuple[str, str]] = field(default_factory=list)
    allow: list[tuple[str, str]] = field(default_factory=list)

    def add(self, label: str, regex: str) -> None:
        self.deny.append((label, re.compile(regex, re.IGNORECASE)))
        self.deny_bytes.append((label, re.compile(regex.encode(), re.IGNORECASE)))


def list_path(explicit: Path | None = None) -> Path:
    """Where the private list lives: argument, then the environment, then default."""
    if explicit is not None:
        return explicit
    env = os.environ.get(LIST_ENV)
    return Path(env) if env else DEFAULT_LIST


def parse_rules(text: str) -> Rules:
    rules = Rules()
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 3 or parts[0] not in {"deny", "sample"}:
            raise ValueError(
                f"line {number}: expected 'deny|sample<TAB>label<TAB>value'"
            )
        kind, label, value = parts
        if kind == "deny":
            rules.add(label, value)
        else:
            rules.samples.append((label, value))
    if not rules.deny:
        raise ValueError("the list has no deny entries")
    return rules


def load_rules(explicit: Path | None = None) -> Rules:
    path = list_path(explicit)
    return parse_rules(path.read_text(encoding="utf-8"))


def _allowed(path: str, line: str, allow: Iterable[tuple[str, str]]) -> bool:
    return any(re.search(p, path) and re.search(ln, line) for p, ln in allow)


def scan_name(path: str, rules: Rules) -> list[str]:
    """Findings for a file name or listing entry."""
    return [
        f"{path}: [{label}] in the path" for label, rx in rules.deny if rx.search(path)
    ]


def scan_text(path: str, text: str, rules: Rules) -> list[str]:
    """Findings for one text file, as ``path:line: [label] text``."""
    out: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for label, rx in rules.deny:
            if rx.search(line) and not _allowed(path, line, rules.allow):
                out.append(f"{path}:{number}: [{label}] {line.strip()[:140]}")
    return out


def scan_bytes(path: str, data_path: Path, rules: Rules) -> list[str]:
    """Findings for a binary file, as ``path: [label] at byte N``."""
    out: list[str] = []
    seen: set[tuple[str, int]] = set()
    with data_path.open("rb") as handle:
        offset = 0
        tail = b""
        while chunk := handle.read(_CHUNK):
            buf = tail + chunk
            base = offset - len(tail)
            for label, rx in rules.deny_bytes:
                for m in rx.finditer(buf):
                    key = (label, base + m.start())
                    if key not in seen:
                        seen.add(key)
                        out.append(f"{path}: [{label}] at byte {base + m.start()}")
            tail = buf[-_OVERLAP:]
            offset += len(chunk)
    return out


def _is_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\0" in handle.read(8192)


def scan_file(rel: str, path: Path, rules: Rules, *, binary: bool = False) -> list[str]:
    """Findings for one file: its name and its contents."""
    found = scan_name(rel, rules)
    if _is_binary(path):
        if binary:
            found += scan_bytes(rel, path, rules)
    else:
        found += scan_text(
            rel, path.read_text(encoding="utf-8", errors="replace"), rules
        )
    return found


def scan_dir(root: Path, rules: Rules, *, binary: bool = False) -> list[str]:
    """Scan every regular file under *root*."""
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            found += scan_file(
                path.relative_to(root).as_posix(), path, rules, binary=binary
            )
    return found


def tracked_files(root: Path = ROOT) -> list[str]:
    """Tracked files that are subject to the guard."""
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="surrogateescape")
    names = [n for n in listing.split("\0") if n]
    return [
        n
        for n in names
        if not any(n == ex or n.startswith(ex) for ex in EXCLUDED_PATHS)
    ]


def scan_tracked(rules: Rules, root: Path = ROOT) -> list[str]:
    found: list[str] = []
    for rel in tracked_files(root):
        path = root / rel
        if path.is_file() and not path.is_symlink():
            found += scan_file(rel, path, rules)
    return found


def _load_allow(path: Path) -> list[tuple[str, str]]:
    """Read extra allow rules: one ``path regex<TAB>line regex`` per line."""
    rules: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip() and not raw.startswith("#"):
            left, _, right = raw.partition("\t")
            rules.append((left, right or "."))
    return rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", type=Path, metavar="DIR", help="scan a directory tree")
    group.add_argument(
        "--names", action="store_true", help="scan listing entries from stdin"
    )
    group.add_argument("--tracked", action="store_true", help="scan the tracked tree")
    parser.add_argument("--binary", action="store_true", help="also scan binary files")
    parser.add_argument(
        "--list", type=Path, metavar="FILE", help="the private deny-list"
    )
    parser.add_argument(
        "--allow",
        type=Path,
        metavar="FILE",
        help="extra allow rules, one 'path regex<TAB>line regex' per line",
    )
    args = parser.parse_args(argv)
    path = list_path(args.list)
    try:
        rules = load_rules(args.list)
    except OSError:
        print(
            f"public-tree guard: the private deny-list is missing: {path}\n"
            f"(set {LIST_ENV} or pass --list)",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(f"public-tree guard: bad deny-list {path}: {exc}", file=sys.stderr)
        return 2
    if args.allow:
        rules.allow += _load_allow(args.allow)
    if args.names:
        found = [
            hit
            for line in sys.stdin.read().splitlines()
            for hit in scan_name(line, rules)
        ]
    elif args.tracked:
        found = scan_tracked(rules)
    else:
        found = scan_dir(args.scan, rules, binary=args.binary)
    for hit in found:
        print(hit)
    if found:
        print(f"public-tree guard: {len(found)} finding(s)", file=sys.stderr)
        return 1
    print("public-tree guard: clean")
    return 0


# ---------------------------------------------------------------------------
# Tests. The mechanism tests use synthetic patterns only.
# ---------------------------------------------------------------------------

_SYNTHETIC = (
    "# synthetic list for the mechanism tests\n"
    "deny\tcorp name\tsecretcorp\n"
    "deny\thost name\thostx101\n"
    "deny\ttest net\t(?<![\\d.])198\\.18\\.\\d{1,3}\\.\\d{1,3}\n"
    "sample\tcorp name\tsecretcorp.example\n"
)


def _rules() -> Rules:
    return parse_rules(_SYNTHETIC)


def test_a_synthetic_list_flags_each_pattern() -> None:
    rules = _rules()
    for sample in ("secretcorp.example", "node hostx101", "addr 198.18.4.5"):
        assert scan_text("x.txt", f"{sample}\n", rules), sample


def test_matching_is_case_insensitive() -> None:
    assert scan_text("x.txt", "SecretCorp\n", _rules())


def test_embedded_names_are_flagged_without_word_boundaries() -> None:
    rules = _rules()
    for sample in ("xsecretcorpx", "SecretCorpCam", "hostx101a", "my-hostx1019"):
        assert scan_text("x.txt", f"{sample}\n", rules), sample
        assert scan_name(f"dir/{sample}.txt", rules), sample


def test_clean_text_is_not_flagged() -> None:
    ok = "ldap.example.com 192.0.2.10 10.0.5.20 alice bob Driveway Backyard"
    assert scan_text("x.txt", ok + "\n", _rules()) == []


def test_a_longer_dotted_number_is_not_a_synthetic_address() -> None:
    assert scan_text("x.txt", "v1.198.18.4.5 and 1198.18.4.5\n", _rules()) == []


def test_allow_rules_drop_only_their_exact_text() -> None:
    rules = _rules()
    rules.allow = [(r"^docs/a\.md$", r"^exact secretcorp line$")]
    exact = "exact secretcorp line"
    assert scan_text("docs/a.md", exact + "\n", rules) == []
    assert scan_text("docs/a.md", exact + " more\n", rules)
    assert scan_text("docs/b.md", exact + "\n", rules)


def test_a_directory_scan_reports_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / "bad.txt").write_text("a\nb hostx101 c\n", encoding="utf-8")
    hits = scan_dir(tmp_path, _rules())
    assert len(hits) == 1
    assert hits[0].startswith("bad.txt:2: [host name]")


def test_binary_files_are_scanned_only_on_request(tmp_path: Path) -> None:
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\0\1\2 secretcorp\0")
    assert scan_dir(tmp_path, _rules()) == []
    hits = scan_dir(tmp_path, _rules(), binary=True)
    assert len(hits) == 1
    assert "at byte" in hits[0]


def test_embedded_names_are_flagged_in_binary_scans(tmp_path: Path) -> None:
    blob = tmp_path / "blob.bin"
    for sample in (b"xsecretcorpy", b"AhostX1019"):
        blob.write_bytes(b"\0\1" + sample + b"\0")
        assert scan_dir(tmp_path, _rules(), binary=True), sample


def test_the_list_parser_rejects_bad_lines() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_rules("deny\tonly two fields\n")
    with pytest.raises(ValueError):
        parse_rules("# nothing but comments\n")


def test_command_line_exit_codes(tmp_path: Path) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text(_SYNTHETIC, encoding="utf-8")
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "a.txt").write_text("fine\n", encoding="utf-8")
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "a.txt").write_text("x secretcorp.example\n", encoding="utf-8")
    script = str(Path(__file__).resolve())

    def run(
        *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", script, *args],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": os.environ["PATH"], **(env or {})},
        )

    assert run("--scan", str(clean), "--list", str(deny)).returncode == 0
    bad = run("--scan", str(dirty), "--list", str(deny))
    assert bad.returncode == 1
    assert "a.txt:1:" in bad.stdout
    # The list can come from the environment.
    assert run("--scan", str(dirty), env={LIST_ENV: str(deny)}).returncode == 1
    # A missing list is an error, never a silent pass.
    missing = run("--scan", str(clean), "--list", str(tmp_path / "none.txt"))
    assert missing.returncode == 2
    assert "missing" in missing.stderr


# --- checks that need the real, private list ---------------------------------


def _real_rules_or_skip() -> Rules:
    import pytest

    path = list_path()
    if not path.is_file():
        pytest.skip(f"private deny-list not present ({path}); set {LIST_ENV}")
    return load_rules()


def _in_git_checkout() -> bool:
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0 and (ROOT / "dev" / "export-public.sh").is_file()


def test_the_private_list_flags_its_own_samples() -> None:
    rules = _real_rules_or_skip()
    assert rules.samples, "the private list has no sample entries"
    for label, sample in rules.samples:
        labels = {lab for lab, rx in rules.deny if rx.search(f"value: {sample} end")}
        assert label in labels, f"{label}: no match for the sample"


def test_the_private_list_allows_the_public_identity() -> None:
    rules = _real_rules_or_skip()
    for ok in (
        "https://github.com/drcoble/Timelapse_Manager",
        "Copyright 2026 Drew Coble",
        "ghcr.io/drcoble/timelapse_manager",
        "192.0.2.10 198.51.100.7 10.0.5.20",
    ):
        assert scan_text("x.txt", ok + "\n", rules) == [], ok


def test_the_tracked_tree_is_clean() -> None:
    rules = _real_rules_or_skip()
    if not _in_git_checkout():
        return  # an sdist or archive: nothing to list
    hits = scan_tracked(rules)
    assert not hits, "private details in tracked files:\n" + "\n".join(hits[:60])


def test_the_public_export_of_head_is_clean() -> None:
    _real_rules_or_skip()
    if not _in_git_checkout():
        return
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "export"
        result = subprocess.run(
            [
                str(ROOT / "dev" / "export-public.sh"),
                "--allow-no-gitleaks",
                "HEAD",
                str(out),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
        assert not (out / ".gitea").exists()
        assert not (out / "docs" / "security-audit.md").exists()


if __name__ == "__main__":
    sys.exit(main())
