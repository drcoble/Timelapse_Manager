#!/usr/bin/env bash
# Export a tagged tree for the public repository.
#
# Usage: dev/export-public.sh [--repo DIR] [--allow-no-gitleaks] <tag> <out-dir>
#
# Takes `git archive <tag>` of the repository (default: the one this script
# lives in), then:
#   * drops the maintainer-only paths (.gitea/ and docs/security-audit.md);
#   * replaces CHANGELOG.md with a public changelog built from
#     docs/releases/<version>.md, newest version first;
#   * writes the sorted file list to <out-dir>.MANIFEST (next to the export, so
#     it is never copied into a repository by accident);
#   * runs the public-tree guard (tests/unit/test_public_tree.py) over the result,
#     with the private deny-list (TLM_PUBLIC_GUARD_LIST, default
#     .gitea/public-guard/deny.txt); the export fails when the list is missing;
#   * runs `gitleaks detect --no-git` over the result, honouring the
#     .gitleaksignore in the export. It fails on findings, and when gitleaks is
#     not installed unless --allow-no-gitleaks is given.
# Exits non-zero on any finding. <out-dir> must not exist or must be empty.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$(cd "${HERE}/.." && pwd)"
GUARD="${TOOL_ROOT}/tests/unit/test_public_tree.py"

GUARD_LIST="${TLM_PUBLIC_GUARD_LIST:-${TOOL_ROOT}/.gitea/public-guard/deny.txt}"

repo="${TOOL_ROOT}"
allow_no_gitleaks=0
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) repo="${2:?--repo needs a directory}"; shift 2 ;;
        --allow-no-gitleaks) allow_no_gitleaks=1; shift ;;
        -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit 0 ;;
        -*) echo "error: unknown option: $1" >&2; exit 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
[ "${#args[@]}" -eq 2 ] || { echo "usage: $0 [--repo DIR] [--allow-no-gitleaks] <tag> <out-dir>" >&2; exit 2; }
[ -f "${GUARD_LIST}" ] || {
    echo "error: the private deny-list is missing: ${GUARD_LIST} (set TLM_PUBLIC_GUARD_LIST)" >&2
    exit 2
}
tag="${args[0]}"
out="${args[1]}"

git -C "${repo}" rev-parse --verify --quiet "${tag}^{commit}" >/dev/null \
    || { echo "error: ${tag} is not a commit or tag in ${repo}" >&2; exit 2; }
if [ -e "${out}" ] && [ -n "$(ls -A "${out}" 2>/dev/null)" ]; then
    echo "error: ${out} exists and is not empty" >&2
    exit 2
fi
mkdir -p "${out}"
out="$(cd "${out}" && pwd)"

echo "==> exporting ${tag} to ${out}"
git -C "${repo}" archive --format=tar "${tag}" | tar -x -C "${out}"

# Maintainer-only paths.
rm -rf "${out}/.gitea" "${out}/docs/security-audit.md"

# Public changelog from the per-release notes.
echo "==> building CHANGELOG.md from docs/releases"
python3 - "${out}" <<'PY'
import re
import sys
from pathlib import Path

out = Path(sys.argv[1])
notes = sorted(
    (out / "docs" / "releases").glob("*.md") if (out / "docs" / "releases").is_dir() else []
)


def key(path: Path) -> tuple[int, ...]:
    # Order by the numeric parts of the version in the file name (0.10.0 > 0.9.0).
    return tuple(int(n) for n in re.findall(r"\d+", path.stem))


notes = [n for n in notes if n.name != "README.md"]
if not notes:
    sys.exit("error: no docs/releases/<version>.md files; cannot build CHANGELOG.md")
notes.sort(key=key, reverse=True)

parts = [
    "# Changelog\n\n"
    "Notable changes to Timelapse Manager, newest release first.\n"
]
for note in notes:
    lines = note.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("# "):  # the file's own title becomes the entry heading
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines.pop(0)
    body: list[str] = []
    fenced = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
        # Demote headings one level so they nest under the version heading.
        body.append("#" + line if line.startswith("#") and not fenced else line)
    parts.append(f"\n## {note.stem}\n\n" + "\n".join(body).rstrip() + "\n")
(out / "CHANGELOG.md").write_text("".join(parts), encoding="utf-8")
print(f"    {len(notes)} release(s): {', '.join(n.stem for n in notes)}")
PY

# Sorted file list, written beside the export.
manifest="${out}.MANIFEST"
(cd "${out}" && find . -type f -print | sed 's|^\./||' | LC_ALL=C sort) > "${manifest}"
echo "==> $(wc -l < "${manifest}" | tr -d ' ') files; manifest: ${manifest}"

status=0

echo "==> public-tree guard"
python3 -I "${GUARD}" --list "${GUARD_LIST}" --scan "${out}" || status=1

echo "==> gitleaks"
GITLEAKS="${TLM_GITLEAKS:-gitleaks}"
if command -v "${GITLEAKS}" >/dev/null 2>&1; then
    # Run from inside the export so finding fingerprints are relative paths
    # (that is what .gitleaksignore records).
    (cd "${out}" && "${GITLEAKS}" detect --no-git --source . --no-banner --redact \
        --gitleaks-ignore-path .gitleaksignore) || status=1
elif [ "${allow_no_gitleaks}" -eq 1 ]; then
    echo "WARNING: gitleaks is not installed; the secret scan was skipped (--allow-no-gitleaks)" >&2
else
    echo "error: gitleaks is not installed (install it, or pass --allow-no-gitleaks)" >&2
    status=1
fi

if [ "${status}" -ne 0 ]; then
    echo "export FAILED: fix the findings above (the export is left in ${out})" >&2
    exit 1
fi
echo "export clean: ${out}"
