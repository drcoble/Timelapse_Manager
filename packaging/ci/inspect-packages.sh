#!/usr/bin/env bash
# Inspect the freshly built .deb and .rpm and verify the .rpm signature.
#
# Usage: packaging/ci/inspect-packages.sh --version <pep440> --deb <file> --rpm <file> \
#            --key-file <private-key> --pubkey-out <file>
#
# Environment: EL9_IMAGE, the digest-pinned Enterprise Linux 9 image used for
# the rpm side (rpm, gpg and the EL9 crypto policy, the same environment a
# dnf client has). Docker is driven with create/cp/start only, never with a
# bind mount: the job runs inside a container, so its paths are not host paths.
#
# .deb: version, maintainer, payload paths and maintainer scripts.
# .rpm: version, payload paths and scriptlets; the signing key's public half is
# exported to --pubkey-out (published next to the packages), imported into the
# EL9 rpm database, and `rpm -K` must report the package signature as good.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

version="" deb="" rpm="" key_file="" pubkey_out=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version) version="${2:?}"; shift 2 ;;
        --deb) deb="${2:?}"; shift 2 ;;
        --rpm) rpm="${2:?}"; shift 2 ;;
        --key-file) key_file="${2:?}"; shift 2 ;;
        --pubkey-out) pubkey_out="${2:?}"; shift 2 ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done
for v in version deb rpm key_file pubkey_out; do
    [ -n "${!v}" ] || { echo "error: --${v//_/-} is required" >&2; exit 2; }
done
if ! { [ -f "$deb" ] && [ -f "$rpm" ] && [ -f "$key_file" ]; }; then
    echo "error: --deb, --rpm and --key-file must exist" >&2
    exit 2
fi
: "${EL9_IMAGE:?EL9_IMAGE is not set}"

full="$(python3 "${HERE}/../pkgversion.py" "$version")"
fail=0
check() { # check <description> <haystack> <needle>
    # A here-string, not a pipe: with pipefail, grep -q exiting on an early
    # match SIGPIPEs printf on a long listing and the check fails falsely.
    if grep -qF -- "$3" <<<"$2"; then
        echo "ok:   $1"
    else
        echo "FAIL: $1 (expected to find: $3)" >&2
        fail=1
    fi
}

echo "==> ${deb}"
command -v dpkg-deb >/dev/null 2>&1 || { echo "error: dpkg-deb not found" >&2; exit 2; }
dpkg-deb -I "$deb"
deb_version="$(dpkg-deb -f "$deb" Version)"
if [ "$deb_version" = "${full}-1" ]; then
    echo "ok:   deb version ${deb_version}"
else
    echo "FAIL: deb version ${deb_version}, expected ${full}-1" >&2
    fail=1
fi
check "deb maintainer" "$(dpkg-deb -f "$deb" Maintainer)" "Drew Coble <drcoble@users.noreply.github.com>"
deb_list="$(dpkg-deb -c "$deb")"
check "deb unit path" "$deb_list" "./usr/lib/systemd/system/timelapse-manager.service"
check "deb PATH link" "$deb_list" "./usr/bin/timelapse-manager -> /opt/timelapse-manager/timelapse-manager"
check "deb bundled ffmpeg" "$deb_list" "./opt/timelapse-manager/_internal/ffmpeg/ffmpeg"
if grep -q "/opt/timelapse-manager/packaging" <<<"$deb_list"; then
    echo "FAIL: deb ships the tarball installer tree" >&2
    fail=1
fi
scripts="$(mktemp -d)"
trap 'rm -rf "$scripts"' EXIT
dpkg-deb -e "$deb" "${scripts}/control"
for s in preinst postinst prerm postrm; do
    if [ -s "${scripts}/control/${s}" ]; then
        echo "ok:   deb ${s} present"
    else
        echo "FAIL: deb ${s} missing" >&2
        fail=1
    fi
done

echo "==> ${rpm}"
cid="$(docker create --entrypoint bash "$EL9_IMAGE" -ceu '
    full="$1"
    command -v gpg >/dev/null 2>&1 || dnf -y -q install gnupg2 >/dev/null
    export GNUPGHOME="$(mktemp -d)"
    gpg --batch --quiet --import /tmp/signing-key.asc 2>/dev/null || true
    gpg --batch --armor --export > /tmp/signing-key.pub.asc
    [ -s /tmp/signing-key.pub.asc ] || { echo "FAIL: no public key could be exported" >&2; exit 1; }
    rpm --import /tmp/signing-key.pub.asc
    rpm -qpi /tmp/pkg.rpm
    got="$(rpm -qp --qf "%{VERSION}-%{RELEASE}" /tmp/pkg.rpm)"
    [ "$got" = "${full}-1" ] || { echo "FAIL: rpm version $got, expected ${full}-1" >&2; exit 1; }
    echo "ok:   rpm version $got"
    list="$(rpm -qpl /tmp/pkg.rpm)"
    for want in /usr/lib/systemd/system/timelapse-manager.service /usr/bin/timelapse-manager \
        /opt/timelapse-manager/_internal/ffmpeg/ffmpeg; do
        grep -qx -- "$want" <<<"$list" || { echo "FAIL: rpm lacks $want" >&2; exit 1; }
        echo "ok:   rpm ships $want"
    done
    if grep -q "^/opt/timelapse-manager/packaging" <<<"$list"; then
        echo "FAIL: rpm ships the tarball installer tree" >&2; exit 1
    fi
    scripts="$(rpm -qp --scripts /tmp/pkg.rpm)"
    for want in "preinstall scriptlet" "postinstall scriptlet" "preuninstall scriptlet" "postuninstall scriptlet"; do
        grep -q -- "$want" <<<"$scripts" || { echo "FAIL: rpm lacks the $want" >&2; exit 1; }
        echo "ok:   rpm has the $want"
    done
    check="$(rpm -K /tmp/pkg.rpm 2>&1)" || { echo "FAIL: rpm -K: $check" >&2; exit 1; }
    echo "$check"
    grep -qi "signatures OK" <<<"$check" || { echo "FAIL: the rpm signature did not verify" >&2; exit 1; }
    echo "ok:   rpm signature verified against the exported public key"
' bash "$full")"
cleanup_container() { docker rm -f "$cid" >/dev/null 2>&1 || true; rm -rf "$scripts"; }
trap cleanup_container EXIT
docker cp "$rpm" "${cid}:/tmp/pkg.rpm"
docker cp "$key_file" "${cid}:/tmp/signing-key.asc"
rpm_status=0
docker start -a "$cid" || rpm_status=$?
[ "$rpm_status" -eq 0 ] || fail=1
docker cp "${cid}:/tmp/signing-key.pub.asc" "$pubkey_out" || fail=1

if [ "$fail" -ne 0 ]; then
    echo "package inspection FAILED" >&2
    exit 1
fi
echo "package inspection passed"
