# syntax=docker/dockerfile:1
#
# Build the linux/amd64 release bundle on Enterprise Linux 9 (glibc 2.34), so the
# tarball, the .deb, the .rpm and the Proxmox CT template all run on EL9 and
# newer. Built by the release workflow, or by hand:
#
#   docker buildx build --platform linux/amd64 -f packaging/bundle/Dockerfile \
#       --build-arg VERSION=<pep440> --secret id=gitea_token,env=GITEA_TOKEN \
#       --target export --output type=local,dest=out .
#
# Stages:
#   build   -- uv + the locked environment; packaging/release.sh freezes the
#              bundle with PyInstaller (pinned FFmpeg from ffmpeg-pin.json) and
#              writes the tarball.
#   verify  -- a clean EL9 image with no Python and no FFmpeg. It unpacks the
#              tarball (the shipped artifact), fails when any ELF file needs a
#              glibc newer than 2.34, checks the bundled FFmpeg is the pin and
#              starts the service until /healthz answers.
#   export  -- the tarball plus the two verification reports, and nothing else.
#
# Why Rocky Linux 9 and not a manylinux image: its glibc is exactly the floor we
# promise, so everything built and run here is compatible by construction, the
# verify stage runs on the target libc, and uv selects the same wheels an EL9
# host would. The manylinux images ship CPython builds without a shared libpython,
# which PyInstaller needs, and an older glibc than the floor needs no help from us.
# Rocky 9 is the open rebuild of RHEL 9 (the same ABI as AlmaLinux 9 and RHEL 9).
#
# The tests run in the workflow's `release` job; this build only freezes.

# rockylinux/rockylinux:9 (index digest, resolved 2026-10-29; Rocky 9.8).
# A maintainer's release pipeline pins the same image; refresh both deliberately.
ARG BUILD_IMAGE=rockylinux/rockylinux:9@sha256:8101994123cf3d0a8fee517bee7f39e555c7d92bd2d9eb3303cc988a0eeed00f
# uv 0.12.21: the same pin as docker/Dockerfile and UV_VERSION in the workflow.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------------------
FROM ${BUILD_IMAGE} AS build

# binutils: objdump/objcopy for PyInstaller. gcc/make: a source build of any
# dependency that lacks a wheel for this interpreter. xz/tar/gzip/findutils/
# util-linux: unpacking the pinned FFmpeg and release.sh's own tooling.
RUN dnf -y install --setopt=install_weak_deps=False \
        binutils gcc gcc-c++ make tar gzip xz findutils util-linux which curl-minimal \
    && dnf clean all

COPY --from=uv /uv /uvx /usr/local/bin/

# CPython 3.12 comes from uv (python-build-standalone, a shared libpython built
# against an old glibc), the same interpreter family the release job tests with.
ENV UV_PYTHON=3.12 \
    UV_LINK_MODE=copy

WORKDIR /src
COPY . .

ARG VERSION
ARG BUILD_SHA=unknown
# Optional private FFmpeg mirror URL, tried before the public pin URL.
ARG TLM_FFMPEG_MIRROR_URL=
RUN --mount=type=secret,id=gitea_token <<'EOF'
set -eu
[ -n "${VERSION}" ] || { echo "ERROR: pass --build-arg VERSION=<pep440>"; exit 1; }
# The pinned FFmpeg is fetched from the public URL in ffmpeg-pin.json. A private
# mirror can be tried first by passing --build-arg TLM_FFMPEG_MIRROR_URL=<url>
# and the buildx secret gitea_token; release.sh reads the token from
# GITEA_TOKEN and only sends it to that mirror URL.
if [ -f /run/secrets/gitea_token ]; then
    GITEA_TOKEN="$(cat /run/secrets/gitea_token)"
    export GITEA_TOKEN
fi
export TLM_BUILD_SHA="${BUILD_SHA}"
actual="$(UV_LOCKED=1 uv run python -c 'import timelapse_manager as t; print(t.__version__)')"
[ "${actual}" = "${VERSION}" ] || { echo "ERROR: source is ${actual}, expected ${VERSION}"; exit 1; }
./packaging/release.sh --skip-smoke
ls -l dist
EOF

# ---------------------------------------------------------------------------
FROM ${BUILD_IMAGE} AS verify

ARG VERSION
RUN dnf -y install --setopt=install_weak_deps=False binutils findutils tar gzip util-linux curl-minimal \
    && dnf clean all

COPY packaging/check-glibc-floor.sh /usr/local/bin/check-glibc-floor.sh
COPY ffmpeg-pin.json /verify/ffmpeg-pin.json
COPY --from=build /src/dist/timelapse-manager-${VERSION}-linux-amd64.tar.gz /verify/

RUN <<'EOF'
set -eu
cd /verify
tar -xzf "timelapse-manager-${VERSION}-linux-amd64.tar.gz"
bundle="/verify/timelapse-manager-${VERSION}-linux-amd64"

echo "== glibc floor =="
# No pipe: this shell has no pipefail, and a pipe into tee would hide a failure.
if ! check-glibc-floor.sh "${bundle}" 2.34 > /verify/glibc-floor.txt 2>&1; then
    cat /verify/glibc-floor.txt
    exit 1
fi
cat /verify/glibc-floor.txt

echo "== bundled FFmpeg =="
want="$(grep -o '"version": *"[^"]*"' /verify/ffmpeg-pin.json | head -n 1 | cut -d'"' -f4)"
"${bundle}/_internal/ffmpeg/ffmpeg" -version | head -n 1 | tee /verify/smoke.txt
grep -q "${want}" /verify/smoke.txt || { echo "ERROR: bundled ffmpeg is not ${want}"; exit 1; }

echo "== service smoke on a clean EL9 host =="
mkdir -p /verify/state
export TLM_PATHS__DATA_DIR=/verify/state
export TLM_DATABASE__URL=sqlite:////verify/state/timelapse.db
export TLM_SERVER__HTTP_PORT=18080
export TLM_SERVER__HTTPS_PORT=18443
setsid "${bundle}/timelapse-manager" run > /verify/app.log 2>&1 &
pid=$!
ok=0
for _ in $(seq 1 120); do
    if curl --fail --silent "http://127.0.0.1:18080/healthz" > /verify/healthz.json; then
        ok=1
        break
    fi
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
done
kill -TERM -- "-${pid}" 2>/dev/null || true
if [ "${ok}" -ne 1 ]; then
    echo "ERROR: /healthz did not answer from the frozen bundle"
    tail -n 60 /verify/app.log
    exit 1
fi
cat /verify/healthz.json
echo "healthz answered on EL9 ($(cat /etc/rocky-release))" >> /verify/smoke.txt
EOF

# ---------------------------------------------------------------------------
FROM scratch AS export
ARG VERSION
COPY --from=build /src/dist/timelapse-manager-${VERSION}-linux-amd64.tar.gz /
COPY --from=verify /verify/glibc-floor.txt /verify/smoke.txt /
