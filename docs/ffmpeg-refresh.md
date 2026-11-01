# FFmpeg Refresh Process

When a security advisory affects the bundled FFmpeg version, follow this
checklist to update the pin, rebuild the artifacts, and publish a new release.

The **single source of truth** for the bundled FFmpeg is `ffmpeg-pin.json` at
the repository root.

- **Docker image:** the Dockerfile downloads and SHA-verifies the pin at build
  time — the container always ships the exact pinned binary.
- **SBOM:** `dev/gen_sbom.py` reads `ffmpeg-pin.json` and injects the FFmpeg
  component into `cyclonedx-bom.json` with the recorded version and hash.
- **PyInstaller bundle (known gap):** the release CI (`release.yml`) does not
  set `TLM_BUNDLE_FFMPEG_DIR` before running `release.sh`, so the PyInstaller
  spec falls back to `shutil.which("ffmpeg")` — the runner's PATH `ffmpeg`, not
  the pinned static build. As a result, the released bundle ships whatever
  `ffmpeg` the ubuntu-latest runner provides, which may differ from the pin.
  The SBOM's FFmpeg component (read from the pin) will not reflect the bundle's
  actual binary in this case. **A security advisory affecting the bundled FFmpeg
  requires also addressing this gap for the PyInstaller artifact.** Steps 4 and
  7 below describe the local fix path and flag the CI gap explicitly.

---

## Checklist

### 1. Locate the current pin

Read `ffmpeg-pin.json`:

```json
{
  "version": "<current-version>",
  "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/...",
  "sha256": "<hex>",
  "license": "GPL-3.0",
  "binaries": {
    "ffmpeg": "<tarball-path>/bin/ffmpeg",
    "ffprobe": "<tarball-path>/bin/ffprobe"
  }
}
```

Note the `version` string (a git-describe tag such as `N-124941-g54749da98a`)
and the tarball `url`.

The file may also carry an optional `mirror_urls` list: alternative locations
for the same tarball, tried in order, each checked against the same `sha256`.
The committed file lists the public BtbN URL first. A BtbN autobuild URL
disappears when BtbN prunes old builds, so a project that builds in its own
infrastructure can keep a copy of the pinned tarball on a mirror it controls
(for example a generic package on a self-hosted registry, `<registry>/<owner>`)
and have its build try that mirror before the public URL. The mirror is a
convenience, never a trust anchor: the SHA-256 in the pin is what is verified.

To have a build try a private mirror first without committing its address, set
`TLM_FFMPEG_MIRROR_URL` to the mirror's full tarball URL. `packaging/release.sh`
and both Dockerfiles (as the `TLM_FFMPEG_MIRROR_URL` build argument) put that URL
ahead of the pin's own list, and send the registry token (`GITEA_TOKEN`, or the
`gitea_token` build secret) to that URL only. When a mirror URL and a token are
both given, a failed mirror fails the build instead of silently falling back to
the public URL. `python3 packaging/refresh-ffmpeg-pin.py --mirror-url` prints the
mirror URL for the pinned build, given `TLM_REGISTRY_URL` and
`TLM_REGISTRY_OWNER`; `--apply` uploads a new build there (it also needs
`GITEA_TOKEN`) and commits only the public URL.

### 2. Identify the replacement build

Obtain a new static linux/amd64 FFmpeg build. The project uses GPL static
builds from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases).

Download the new tarball and verify it is intact before recording anything:

```sh
# Replace the URL with the new release URL
curl -fsSL -o ffmpeg-new.tar.xz \
  "https://github.com/BtbN/FFmpeg-Builds/releases/download/<date>/ffmpeg-<version>-linux64-gpl.tar.xz"

# Compute and record the SHA-256
sha256sum ffmpeg-new.tar.xz
# <new-sha256>  ffmpeg-new.tar.xz

# Spot-check the binary
tar -xJf ffmpeg-new.tar.xz --wildcards '*/bin/ffmpeg' -O | file -
# should print: ELF 64-bit LSB executable, x86-64

# Verify the version string
tar -xJf ffmpeg-new.tar.xz --wildcards '*/bin/ffmpeg' -O > /tmp/ffmpeg-new
chmod +x /tmp/ffmpeg-new
/tmp/ffmpeg-new -version | head -n1
```

### 3. Update ffmpeg-pin.json

Edit `ffmpeg-pin.json` with the new values:

- `version` — the version string from `ffmpeg -version` or the build tag
- `url` — the download URL of the new tarball
- `sha256` — the SHA-256 hex digest computed in step 2
- `binaries.ffmpeg` — the path to the `ffmpeg` binary **inside the tarball**
  (e.g. `ffmpeg-N-XXXXX-gYYYYYYY-linux64-gpl/bin/ffmpeg`)
- `binaries.ffprobe` — the path to the `ffprobe` binary inside the tarball

> **Important:** `binaries.ffmpeg` and `binaries.ffprobe` include a
> version-stamped directory prefix that changes with every build. If you update
> `version` and `url` but forget to update both `binaries.*` paths to match,
> the Docker build stage (`cp "/stage/extract/${ffmpeg_rel}" /opt/ffmpeg/bin/ffmpeg`)
> will fail with a "no such file" error at the correct `cp` line.

Example updated pin:

```json
{
  "version": "N-125000-gabcdef0123",
  "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-07-01-17-02/ffmpeg-N-125000-gabcdef0123-linux64-gpl.tar.xz",
  "sha256": "<new-sha256>",
  "license": "GPL-3.0",
  "binaries": {
    "ffmpeg": "ffmpeg-N-125000-gabcdef0123-linux64-gpl/bin/ffmpeg",
    "ffprobe": "ffmpeg-N-125000-gabcdef0123-linux64-gpl/bin/ffprobe"
  }
}
```

### 4. Build and test locally

**Docker image** — the Dockerfile downloads and SHA-verifies the pin
automatically. Build and confirm:

```sh
docker compose -f docker/docker-compose.yml build
docker run --rm --entrypoint /opt/ffmpeg/bin/ffmpeg \
  timelapse-manager:latest -version | head -n1
# Should print the new version string
```

**PyInstaller bundle** — the release.sh script calls PyInstaller; the spec
sources the FFmpeg binary from the `TLM_BUNDLE_FFMPEG_DIR` environment
variable (if set) or falls back to whatever `ffmpeg` is on PATH. To bundle
the new pinned binary locally, extract it from the tarball and point
the env variable at the directory:

```sh
mkdir -p /tmp/ffmpeg-stage
tar -xJf ffmpeg-new.tar.xz \
  "ffmpeg-N-XXXXX-gYYYYYYY-linux64-gpl/bin/ffmpeg" \
  "ffmpeg-N-XXXXX-gYYYYYYY-linux64-gpl/bin/ffprobe" \
  --strip-components=2 \
  -C /tmp/ffmpeg-stage

TLM_BUNDLE_FFMPEG_DIR=/tmp/ffmpeg-stage make release
```

Running `make release` also runs the local smoke test (starts the frozen
bundle and checks `/healthz`). Confirm that `ffmpeg_version` in the response
matches the new version.

```sh
# Check the healthz response after smoke starts:
# {"ffmpeg_version":"N-125000-...","ffmpeg_path":".../_internal/ffmpeg/ffmpeg", ...}
```

### 5. Regenerate the SBOM and sign

```sh
make sbom    # writes dist/cyclonedx-bom.json, including the updated FFmpeg component
make sign    # writes SHA256SUMS and SHA256SUMS.asc
```

Confirm the SBOM includes the new FFmpeg version:

```sh
python3 -c "
import json
bom = json.load(open('dist/cyclonedx-bom.json'))
ffmpeg = next(c for c in bom['components'] if c['name'] == 'ffmpeg')
print(ffmpeg['version'])
"
```

### 6. Run the CVE scan

The CI Trivy gate scans the **Docker base image** (`python:3.12-slim-trixie`)
for HIGH/CRITICAL CVEs, not the FFmpeg binary itself. The FFmpeg pin's
integrity guarantee is the SHA-256 verification at Docker build time (step 4)
plus the SBOM component with its recorded hash.

If the advisory also affects a base image, edit the digest defaults in
`docker/Dockerfile`. `PYTHON_IMAGE` (runtime and builder),
`FFMPEG_BASE_IMAGE` (the FFmpeg download stage) and `UV_IMAGE` are each pinned to
a real digest, and a test refuses an unpinned or all-zeros value. Release CI and
`make image` build with those defaults unless a `--build-arg` overrides them, so
a base-image refresh is a committed change, not a build-time lookup.

To see the current digest of a tag:

```sh
docker buildx imagetools inspect python:3.12-slim-trixie \
  --format '{{.Manifest.Digest}}'
```

Commit the new digest and its date comment, then run the CVE scan again.

### 7. Publish

Commit the change (`ffmpeg-pin.json`) and push a new version tag. The SBOM is a
generated release artifact (written to the git-ignored `dist/` and regenerated
by the release CI on the tag), so it is not committed; the base image is pinned
by digest in `docker/Dockerfile`, which is edited only when a base image needs
refreshing (step 6):

```sh
git add ffmpeg-pin.json
git commit -m "security: bump bundled FFmpeg to N-125000-gabcdef0123"
git tag v<next-version>
git push origin main v<next-version>
```

The release CI triggers on the tag and:

1. Runs lint, format, typecheck and the unit tests on Python 3.12 and 3.11, and
   writes the CycloneDX SBOM.
2. Builds the PyInstaller bundle in an Enterprise Linux 9 container with the
   pinned FFmpeg, fails if any file needs a glibc newer than 2.34, and
   smoke-tests `/healthz` on a clean EL9 image.
3. Builds the `.deb` and the signed `.rpm`, and the Proxmox CT template, then
   scans the template (fixable HIGH/CRITICAL findings fail the release) and
   writes its SBOM.
4. Builds the Docker image (downloads and SHA-verifies the pinned FFmpeg),
   checks it, scans it with Trivy (fixable HIGH/CRITICAL findings fail the
   release), writes its SBOM and pushes it.
5. Publishes every file, then uploads `SHA256SUMS` over the whole set last and
   verifies the published release.

The files and the image are then attached to the GitHub Release (see
[Verifying Releases](./verifying-releases.md)), whose own workflow only
verifies them; it builds nothing.

A manual run with `publish=false` does everything except publish.

The tarball, `.deb`, `.rpm`, CT template and image all ship the pinned FFmpeg:
`release.sh` stages the build named in `ffmpeg-pin.json` before freezing the bundle.

---

## Summary of files touched

| File | Change |
|---|---|
| `ffmpeg-pin.json` | `version`, `url`, `sha256`, both `binaries.*` paths |
| `dist/cyclonedx-bom.json` | Regenerated by `make sbom` (git-ignored) — do not edit by hand |
| `SHA256SUMS` / `SHA256SUMS.asc` | Regenerated by `make sign` |
