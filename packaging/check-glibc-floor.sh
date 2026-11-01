#!/usr/bin/env bash
# Fail when any ELF file under a directory needs a glibc newer than a floor.
#
# Usage: packaging/check-glibc-floor.sh <dir> [max-version]
#
# The release bundle is built in an EL9 (glibc 2.34) container so that the
# tarball, .deb, .rpm and the CT template all run on Enterprise Linux 9 and
# newer. This gate runs `objdump -T` over every ELF file (the bundled FFmpeg and
# the shared libraries PyInstaller collects included), extracts the GLIBC_x.y
# symbol versions the files import, prints the highest, and exits 1 when it is
# above max-version (default 2.34). GLIBC_PRIVATE is ignored. A directory with no
# glibc-linked ELF file at all also fails: that means the scan found nothing to
# judge, not that the bundle is fine.
set -euo pipefail

dir="${1:?usage: $0 <dir> [max-version]}"
max="${2:-2.34}"

[ -d "$dir" ] || { echo "error: not a directory: $dir" >&2; exit 2; }
command -v objdump >/dev/null 2>&1 || { echo "error: objdump (binutils) not found on PATH" >&2; exit 2; }

highest=""
highest_file=""
offenders=""
elf_count=0
while IFS= read -r -d '' file; do
    # ELF magic 7f 45 4c 46 (od is coreutils; the EL9 verify image has no cmp).
    magic="$(LC_ALL=C head -c 4 "$file" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    [ "$magic" = "7f454c46" ] || continue
    elf_count=$((elf_count + 1))
    # A static binary makes objdump print "not a dynamic object": no symbols, no failure.
    versions="$(objdump -T "$file" 2>/dev/null | grep -o 'GLIBC_[0-9][0-9.]*' | sed 's/^GLIBC_//' | sort -uV || true)"
    [ -n "$versions" ] || continue
    file_max="$(printf '%s\n' "$versions" | tail -n 1)"
    # Remember every offender so one run shows them all, not just the worst.
    if [ "$(printf '%s\n%s\n' "$file_max" "$max" | sort -V | tail -n 1)" != "$max" ]; then
        offenders="${offenders}${file#"$dir"/} needs GLIBC_${file_max}"$'\n'
    fi
    if [ -z "$highest" ] || [ "$(printf '%s\n%s\n' "$highest" "$file_max" | sort -V | tail -n 1)" != "$highest" ]; then
        highest="$file_max"
        highest_file="$file"
    fi
done < <(find "$dir" -type f -print0)

if [ -z "$highest" ]; then
    echo "error: no GLIBC symbol versions found in $elf_count ELF file(s) under $dir" >&2
    exit 1
fi

echo "ELF files scanned: $elf_count"
echo "highest GLIBC symbol version required: $highest (${highest_file#"$dir"/})"
echo "floor: $max"
if [ "$(printf '%s\n%s\n' "$highest" "$max" | sort -V | tail -n 1)" != "$max" ]; then
    echo "FAIL: ${highest_file#"$dir"/} needs GLIBC_$highest, newer than the GLIBC_$max floor" >&2
    echo "Every file above the GLIBC_$max floor:" >&2
    printf '%s' "$offenders" >&2
    exit 1
fi
echo "OK: the bundle needs no glibc newer than $max"
