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
    deny-bin<TAB>label<TAB>regex  the same, and also safe to run over binary bytes
    sample<TAB>label<TAB>text     must be flagged (a self-check of the list)
    sample-bin<TAB>label<TAB>text must be flagged by a ``deny-bin`` entry

``deny`` entries run over text files and over the decoded Python code of a
PyInstaller bundle. Only ``deny-bin`` entries run over the raw bytes of other
binary files: short or numeric patterns (addresses, first names) match random
bytes in compiled libraries, time-zone data and the like, so a ``deny-bin``
entry must be a long, distinctive identifier. Every ``deny-bin`` entry also
applies to text.

It also works as a command-line scanner, used by ``dev/export-public.sh`` and
``dev/publish-github-release.sh``::

    python3 tests/unit/test_public_tree.py --scan DIR [--binary] [--allow FILE]
    python3 tests/unit/test_public_tree.py --names < listing.txt
    python3 tests/unit/test_public_tree.py --tracked

With ``--binary``, a PyInstaller executable (an appended CArchive) and a
standalone ``.pyz`` are decoded, since the code is zlib-compressed on disk and
invisible to a raw byte scan. Our own content gets the full ``deny`` set: the
project's modules (``--own PREFIX``, default ``timelapse_manager``, plus the
``__main__`` script), and any binary file or zip member whose path has a
component named for the package. Everything else (bundled libraries, ``tzdata/``,
``ffmpeg/``, other site-packages) gets only the ``deny-bin`` set, because short
patterns match their keyword lists, version strings and random bytes. Encoded
text lines (a PEM body with its END marker, or a long pure-base64 line) get only
the ``deny-bin`` set too; an unterminated ``-----BEGIN`` skips nothing.

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
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
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

_KINDS = frozenset({"deny", "deny-bin", "sample", "sample-bin"})

_CHUNK = 8 * 1024 * 1024
_OVERLAP = 256


@dataclass
class Rules:
    """A compiled deny-list."""

    deny: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    # The binary-safe patterns as text: for encoded lines, which skip the rest.
    deny_safe: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    # Every pattern, as bytes: for decoded code, which is ours.
    deny_code: list[tuple[str, re.Pattern[bytes]]] = field(default_factory=list)
    # Binary-safe patterns only: for the raw bytes of third-party binaries.
    deny_bytes: list[tuple[str, re.Pattern[bytes]]] = field(default_factory=list)
    samples: list[tuple[str, str]] = field(default_factory=list)
    bin_samples: list[tuple[str, str]] = field(default_factory=list)
    allow: list[tuple[str, str]] = field(default_factory=list)

    def add(self, label: str, regex: str, *, binary_safe: bool = False) -> None:
        text = (label, re.compile(regex, re.IGNORECASE))
        self.deny.append(text)
        if binary_safe:
            self.deny_safe.append(text)
        compiled = (label, re.compile(regex.encode(), re.IGNORECASE))
        self.deny_code.append(compiled)
        if binary_safe:
            self.deny_bytes.append(compiled)


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
        if len(parts) != 3 or parts[0] not in _KINDS:
            raise ValueError(
                f"line {number}: expected "
                "'deny|deny-bin|sample|sample-bin<TAB>label<TAB>value'"
            )
        kind, label, value = parts
        if kind in {"deny", "deny-bin"}:
            rules.add(label, value, binary_safe=kind == "deny-bin")
        else:
            rules.samples.append((label, value))
            if kind == "sample-bin":
                rules.bin_samples.append((label, value))
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


_PEM_BEGIN = re.compile(r"^\s*-----BEGIN ")
_PEM_END = re.compile(r"^\s*-----END ")
_BASE64_LINE = re.compile(r"^\s*[A-Za-z0-9+/]{40,}={0,2}\s*$")


def _opaque_lines(lines: list[str]) -> set[int]:
    """Indexes of lines that are encoded data: PEM bodies and pure base64 runs.

    A short pattern can match inside random base64 by chance, so such lines get
    only the binary-safe patterns. A PEM body counts only when its END marker
    is found; an unterminated BEGIN leaves the rest of the file scanned normally.
    """
    skip: set[int] = set()
    index = 0
    while index < len(lines):
        if _PEM_BEGIN.match(lines[index]):
            end = next(
                (j for j in range(index + 1, len(lines)) if _PEM_END.match(lines[j])),
                None,
            )
            if end is not None:
                skip.update(range(index + 1, end))
                index = end + 1
                continue
        elif _BASE64_LINE.match(lines[index]):
            skip.add(index)
        index += 1
    return skip


def scan_text(path: str, text: str, rules: Rules) -> list[str]:
    """Findings for one text file, as ``path:line: [label] text``."""
    out: list[str] = []
    lines = text.splitlines()
    opaque = _opaque_lines(lines)
    for number, line in enumerate(lines, start=1):
        patterns = rules.deny_safe if number - 1 in opaque else rules.deny
        for label, rx in patterns:
            if rx.search(line) and not _allowed(path, line, rules.allow):
                out.append(f"{path}:{number}: [{label}] {line.strip()[:140]}")
    return out


def scan_blob(
    path: str, data: bytes, patterns: list[tuple[str, re.Pattern[bytes]]]
) -> list[str]:
    """Findings for in-memory bytes, as ``path: [label] at byte N``."""
    out: list[str] = []
    for label, rx in patterns:
        out += [f"{path}: [{label}] at byte {m.start()}" for m in rx.finditer(data)]
    return out


def scan_bytes(
    path: str,
    data_path: Path,
    patterns: list[tuple[str, re.Pattern[bytes]]],
) -> list[str]:
    """Findings for a binary file, as ``path: [label] at byte N``."""
    out: list[str] = []
    seen: set[tuple[str, int]] = set()
    with data_path.open("rb") as handle:
        offset = 0
        tail = b""
        while chunk := handle.read(_CHUNK):
            buf = tail + chunk
            base = offset - len(tail)
            for label, rx in patterns:
                for m in rx.finditer(buf):
                    key = (label, base + m.start())
                    if key not in seen:
                        seen.add(key)
                        out.append(f"{path}: [{label}] at byte {base + m.start()}")
            tail = buf[-_OVERLAP:]
            offset += len(chunk)
    return out


# --- PyInstaller archives ---------------------------------------------------

_CARRIER_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
_COOKIE = struct.Struct("!8sIIII64s")
_TOC_HEAD = struct.Struct("!IIIIBc")
_CODE_TYPES = frozenset({b"s", b"S", b"m", b"M"})
_PYZ_TYPES = frozenset({b"z", b"Z"})
_PYZ_MAGIC = b"PYZ\0"
_TAIL = 16 * 1024
DEFAULT_OWN: tuple[str, ...] = ("timelapse_manager",)


def is_own_path(rel: str, own: tuple[str, ...]) -> bool:
    """True when a path (or zip member name) has a component named for our package."""
    return any(
        part == pkg or part.startswith(pkg + ".")
        for part in rel.replace("\\", "/").split("/")
        for pkg in own
    )


def _is_own(name: str, own: tuple[str, ...]) -> bool:
    leaf = name.rsplit("/", 1)[-1]
    return leaf == "__main__" or any(
        leaf == pkg or leaf.startswith(pkg + ".") for pkg in own
    )


def pyz_modules(blob: bytes) -> Iterable[tuple[str, bytes]]:
    """Yield ``(module name, marshalled code bytes)`` from a PYZ archive."""
    import marshal

    if blob[:4] != _PYZ_MAGIC:
        raise ValueError("not a PYZ archive")
    (toc_pos,) = struct.unpack("!i", blob[8:12])
    toc = marshal.loads(blob[toc_pos:])
    for name, (_kind, pos, length) in dict(toc).items():
        if length == 0:  # a namespace package: no code
            continue
        yield str(name), zlib.decompress(blob[pos : pos + length])


def carchive_members(path: Path) -> Iterable[tuple[str, bytes, bytes]]:
    """Yield ``(name, type code, data)`` for each entry of an appended CArchive.

    Yields nothing when *path* has no PyInstaller cookie near its end.
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - _TAIL))
        tail = handle.read()
        at = tail.rfind(_CARRIER_MAGIC)
        if at < 0 or len(tail) - at < _COOKIE.size:
            return
        _magic, pkg_len, toc_off, toc_len, _pyver, _lib = _COOKIE.unpack(
            tail[at : at + _COOKIE.size]
        )
        end = size - len(tail) + at + _COOKIE.size
        start = end - pkg_len
        if start < 0 or toc_off + toc_len > pkg_len:
            raise ValueError("inconsistent PyInstaller cookie")
        handle.seek(start + toc_off)
        toc = handle.read(toc_len)
        pos = 0
        while pos < len(toc):
            entry_len, offset, dlen, _ulen, flag, code = _TOC_HEAD.unpack_from(toc, pos)
            name = toc[pos + _TOC_HEAD.size : pos + entry_len].split(b"\0")[0]
            pos += entry_len
            if code not in _CODE_TYPES | _PYZ_TYPES:
                continue
            handle.seek(start + offset)
            data = handle.read(dlen)
            yield (
                name.decode("utf-8", "replace"),
                code,
                (zlib.decompress(data) if flag else data),
            )


def scan_decoded(
    rel: str, path: Path, rules: Rules, own: tuple[str, ...] = DEFAULT_OWN
) -> list[str]:
    """Findings inside a PyInstaller archive, a ``.pyz`` or a ``.zip``.

    The code in these is compressed on disk, so a raw byte scan cannot see it.
    """
    try:
        return _scan_decoded(rel, path, rules, own)
    except (
        ValueError,
        KeyError,
        EOFError,
        struct.error,
        zlib.error,
        zipfile.BadZipFile,
    ):
        # A corrupt archive must not pass as clean.
        return [f"{rel}: [unreadable archive] could not be decoded for the scan"]


def _scan_decoded(
    rel: str, path: Path, rules: Rules, own: tuple[str, ...]
) -> list[str]:
    out: list[str] = []
    whole = is_own_path(rel, own)  # a file in our own tree: every member is ours
    if path.suffix == ".zip":
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    mine = whole or is_own_path(info.filename, own)
                    out += scan_blob(
                        f"{rel}!{info.filename}",
                        archive.read(info),
                        rules.deny_code if mine else rules.deny_bytes,
                    )
        return out
    if path.suffix == ".pyz":
        blob = path.read_bytes()
        members = [(n, b"m", d) for n, d in pyz_modules(blob)]
    else:
        members = []
        for name, code, data in carchive_members(path):
            if code in _PYZ_TYPES:
                members += [(f"{name}/{n}", b"m", d) for n, d in pyz_modules(data)]
            else:
                members.append((name, code, data))
    for name, _code, data in members:
        patterns = rules.deny_code if whole or _is_own(name, own) else rules.deny_bytes
        out += scan_blob(f"{rel}!{name}", data, patterns)
    return out


def _is_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\0" in handle.read(8192)


def scan_file(
    rel: str,
    path: Path,
    rules: Rules,
    *,
    binary: bool = False,
    own: tuple[str, ...] = DEFAULT_OWN,
) -> list[str]:
    """Findings for one file: its name and its contents."""
    found = scan_name(rel, rules)
    if _is_binary(path):
        if binary:
            found += scan_decoded(rel, path, rules, own)
            # Our own files get the whole list; third-party binaries (bundled
            # libraries, time-zone data, ffmpeg) only the binary-safe patterns.
            patterns = rules.deny_code if is_own_path(rel, own) else rules.deny_bytes
            found += scan_bytes(rel, path, patterns)
    else:
        found += scan_text(
            rel, path.read_text(encoding="utf-8", errors="replace"), rules
        )
    return found


def scan_dir(
    root: Path,
    rules: Rules,
    *,
    binary: bool = False,
    own: tuple[str, ...] = DEFAULT_OWN,
) -> list[str]:
    """Scan every regular file under *root*."""
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            found += scan_file(
                path.relative_to(root).as_posix(), path, rules, binary=binary, own=own
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
        "--own",
        action="append",
        metavar="PREFIX",
        help="a package whose bundled modules get the full list "
        f"(repeatable; default {', '.join(DEFAULT_OWN)})",
    )
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
        found = scan_dir(
            args.scan, rules, binary=args.binary, own=tuple(args.own or DEFAULT_OWN)
        )
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
    "deny-bin\tcorp name\tsecretcorp\n"
    "deny-bin\thost name\thostx101\n"
    "deny\ttest net\t(?<![\\d.])198\\.18\\.\\d{1,3}\\.\\d{1,3}\n"
    "deny\tshort name\tbobby\n"
    "sample-bin\tcorp name\tsecretcorp.example\n"
    "sample\ttest net\t198.18.4.5\n"
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


def pyz_bytes(modules: dict[str, bytes]) -> bytes:
    """A PYZ archive holding the given (already marshalled) module bodies."""
    import marshal

    body = b""
    toc = []
    header = 12
    for name, code in modules.items():
        packed = zlib.compress(code)
        toc.append((name, (0, header + len(body), len(packed))))
        body += packed
    return (
        b"PYZ\0"
        + b"\xa7\x0d\x0d\x0a"
        + struct.pack("!i", header + len(body))
        + (body + marshal.dumps(toc))
    )


def carchive_bytes(
    entries: list[tuple[str, bytes, bytes]], *, lead: bytes = b""
) -> bytes:
    """An executable-shaped file: *lead*, a CArchive of ``(name, type, data)``."""
    blob = b""
    toc = b""
    for name, code, data in entries:
        packed = zlib.compress(data)
        raw = name.encode() + b"\0"
        length = _TOC_HEAD.size + len(raw)
        length += -length % 16
        toc += _TOC_HEAD.pack(length, len(blob), len(packed), len(data), 1, code)
        toc += raw.ljust(length - _TOC_HEAD.size, b"\0")
        blob += packed
    total = len(blob) + len(toc) + _COOKIE.size
    cookie = _COOKIE.pack(
        _CARRIER_MAGIC, total, len(blob), len(toc), 312, b"libpython3.12.so"
    )
    return lead + blob + toc + cookie


def _bundle(
    root: Path, modules: dict[str, bytes], *, lead: bytes = b"\x7fELF\0\0"
) -> Path:
    exe = root / "app"
    exe.write_bytes(carchive_bytes([("PYZ.pyz", b"z", pyz_bytes(modules))], lead=lead))
    return exe


def test_a_name_planted_in_a_pyinstaller_archive_is_found(tmp_path: Path) -> None:
    _bundle(
        tmp_path,
        {"timelapse_manager.mod": b"\xe3 host hostx101 here", "pkg.ok": b"nothing"},
    )
    hits = scan_dir(tmp_path, _rules(), binary=True)
    assert len(hits) == 1, hits
    assert hits[0].startswith("app!PYZ.pyz/timelapse_manager.mod: [host name]")


def test_the_whole_list_runs_over_decoded_code(tmp_path: Path) -> None:
    # Short patterns are right for our own code, even though they are not for
    # third-party binaries.
    _bundle(tmp_path, {"timelapse_manager.mod": b"\xe3 addr 198.18.4.5 and bobby"})
    labels = {
        h.split("[", 1)[1].split("]")[0]
        for h in scan_dir(tmp_path, _rules(), binary=True)
    }
    assert labels == {"test net", "short name"}


def test_third_party_modules_get_only_the_binary_safe_patterns(tmp_path: Path) -> None:
    # A bundled library's keyword list or version string must not fail the scan,
    # but a long identifier in it still does.
    _bundle(
        tmp_path,
        {"lexers.names": b"\xe3 bobby 198.18.4.5", "lexers.bad": b"\xe3 hostx101"},
    )
    hits = scan_dir(tmp_path, _rules(), binary=True)
    assert len(hits) == 1, hits
    assert "lexers.bad: [host name]" in hits[0]
    assert scan_dir(tmp_path, _rules(), binary=True, own=("lexers",))


def test_a_clean_pyinstaller_archive_is_clean(tmp_path: Path) -> None:
    _bundle(tmp_path, {"timelapse_manager.mod": b"\xe3 timelapse_manager.security"})
    assert scan_dir(tmp_path, _rules(), binary=True) == []


def test_a_planted_name_is_invisible_to_a_raw_scan_only_because_it_is_compressed(
    tmp_path: Path,
) -> None:
    exe = _bundle(tmp_path, {"timelapse_manager.mod": b"\xe3 host hostx101 here" * 20})
    assert b"hostx101" not in exe.read_bytes()


def test_a_corrupt_pyinstaller_archive_is_a_finding(tmp_path: Path) -> None:
    exe = _bundle(tmp_path, {"pkg.mod": b"\xe3 fine"})
    data = bytearray(exe.read_bytes())
    data[8] ^= 0xFF  # flip a byte of the compressed PYZ
    exe.write_bytes(bytes(data))
    hits = scan_dir(tmp_path, _rules(), binary=True)
    assert hits and "unreadable archive" in hits[0]


def test_short_patterns_stay_off_third_party_binaries(tmp_path: Path) -> None:
    # Time-zone-like data and a library with address-shaped and name-shaped bytes.
    (tmp_path / "zone").write_bytes(b"TZif2\0\0\0" + b"198.18.4.5" + b"\0bobby\0")
    (tmp_path / "lib.so").write_bytes(b"\x7fELF\0 198.18.7.7 BobbyX \0")
    assert scan_dir(tmp_path, _rules(), binary=True) == []
    # The same text in a text file is still flagged.
    (tmp_path / "notes.txt").write_text("198.18.4.5\n", encoding="utf-8")
    assert scan_dir(tmp_path, _rules(), binary=True)


def test_a_long_identifier_planted_in_a_binary_is_still_found(tmp_path: Path) -> None:
    (tmp_path / "lib.so").write_bytes(b"\x7fELF\0..hostx101..\0")
    hits = scan_dir(tmp_path, _rules(), binary=True)
    assert len(hits) == 1
    assert "[host name] at byte" in hits[0]


def test_a_pem_body_is_skipped_but_its_header_lines_are_read() -> None:
    body = "xx198.18.4.5xx" + "A" * 20
    pem = f"-----BEGIN CERTIFICATE-----\n{body}\nQUJD\n-----END CERTIFICATE-----\n"
    assert scan_text("c.pem", pem, _rules()) == []
    after = pem + "198.18.4.5\n"
    assert len(scan_text("c.pem", after, _rules())) == 1
    # A deny string inside the PEM markers' own lines is still seen.
    marked = "-----BEGIN secretcorp-----\nAAAA\n-----END secretcorp-----\n"
    assert scan_text("c.pem", marked, _rules())


def test_an_unterminated_pem_begin_skips_nothing() -> None:
    text = "-----BEGIN CERTIFICATE-----\nAAAA\nsecretcorp here\nbobby\n"
    hits = scan_text("c.pem", text, _rules())
    assert len(hits) == 2, hits


def test_a_long_identifier_inside_an_encoded_line_is_flagged() -> None:
    run = "O5S3HWCntZznKWlXWpuTekMwGwPXYshA" + "hostx101" + "ApqrZORK15FTAaggiG6cX0S5y2"
    assert scan_text("x.txt", run + "\n", _rules())
    pem = f"-----BEGIN X-----\n{run}\n-----END X-----\n"
    assert scan_text("x.pem", pem, _rules())


def test_a_short_run_inside_a_terminated_pem_body_is_not_flagged() -> None:
    body = "A" * 20 + "198.18.4.5" + "bobby" + "B" * 20
    pem = f"-----BEGIN X-----\n{body}\n-----END X-----\n"
    assert scan_text("x.pem", pem, _rules()) == []


def test_own_binary_files_get_the_whole_list(tmp_path: Path) -> None:
    own = tmp_path / "_internal" / "timelapse_manager"
    own.mkdir(parents=True)
    third = tmp_path / "_internal" / "tzdata"
    third.mkdir()
    blob = b"\x7fELF\0 198.18.4.5 bobby \0"
    (own / "data.bin").write_bytes(blob)
    (third / "zone").write_bytes(blob)
    hits = scan_dir(tmp_path, _rules(), binary=True)
    assert hits and all(
        h.startswith("_internal/timelapse_manager/data.bin") for h in hits
    )
    labels = {h.split("[", 1)[1].split("]")[0] for h in hits}
    assert labels == {"test net", "short name"}


def test_own_zip_members_get_the_whole_list(tmp_path: Path) -> None:
    import zipfile as zf

    with zf.ZipFile(tmp_path / "lib.zip", "w", zf.ZIP_DEFLATED) as z:
        z.writestr("timelapse_manager/a.pyc", b"\xe3 198.18.4.5")
        z.writestr("otherlib/b.pyc", b"\xe3 198.18.4.5")
    hits = scan_dir(tmp_path, _rules(), binary=True)
    assert len(hits) == 1
    assert "lib.zip!timelapse_manager/a.pyc" in hits[0]


def test_a_pure_base64_run_is_skipped_but_a_readable_line_is_not() -> None:
    run = "O5S3HWCntZznKWlXWpuTekMwGwPXYshA" + "bobby" + "ApqrZORK15FTAaggiG6cX0S5y2"
    assert scan_text("x.txt", run + "\n", _rules()) == []
    assert scan_text("x.txt", "see bobby, " + run + "\n", _rules())
    assert scan_text("x.txt", "bobby\n", _rules())


def test_a_zip_member_is_scanned_with_the_whole_list(tmp_path: Path) -> None:
    import zipfile as zf

    with zf.ZipFile(tmp_path / "lib.zip", "w", zf.ZIP_DEFLATED) as z:
        z.writestr("a.pyc", b"\xe3 hostx101")
        z.writestr("b.pyc", b"\xe3 fine")
    hits = scan_dir(tmp_path, _rules(), binary=True)
    assert len(hits) == 1
    assert hits[0].startswith("lib.zip!a.pyc: [host name]")


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


def test_the_private_list_binary_samples_are_flagged_by_binary_safe_patterns() -> None:
    rules = _real_rules_or_skip()
    assert rules.bin_samples, "the private list has no sample-bin entries"
    assert rules.deny_bytes, "the private list has no deny-bin entries"
    for label, sample in rules.bin_samples:
        hits = {
            lab for lab, rx in rules.deny_bytes if rx.search(f"x {sample} y".encode())
        }
        assert label in hits, f"{label}: a binary-safe pattern must match the sample"


def test_the_private_binary_safe_patterns_are_long_identifiers() -> None:
    rules = _real_rules_or_skip()
    for label, rx in rules.deny_bytes:
        for alt in rx.pattern.split(b"|"):
            plain = re.sub(rb"\\.|\[[^\]]*\]|[()?!<=:]", b"x", alt)
            assert len(plain) >= 5, f"{label}: {alt!r} is too short to scan binaries"


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
