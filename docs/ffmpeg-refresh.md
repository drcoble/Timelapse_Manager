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

The CI Trivy gate scans the **Docker base image** (`python:3.12-slim-bookworm`)
for HIGH/CRITICAL CVEs, not the FFmpeg binary itself. The FFmpeg pin's
integrity guarantee is the SHA-256 verification at Docker build time (step 4)
plus the SBOM component with its recorded hash.

If the advisory also affects the Python base image, no Dockerfile edit is
needed: the base image is pinned by digest **at build time**, not in the
committed source. The `ARG PYTHON_IMAGE` default in `docker/Dockerfile` is an
all-zeros placeholder left in place on purpose so an un-pinned build fails
loudly; both the release CI and the local `make image` target resolve the live
`python:3.12-slim-bookworm` digest and pass it via `--build-arg`. Cutting a new
release (or rebuilding the image) therefore picks up the current base digest
automatically, with no source change to commit.

To see the digest a build would resolve to:

```sh
docker buildx imagetools inspect python:3.12-slim-bookworm \
  --format '{{.Manifest.Digest}}'
```

### 7. Publish

Commit the change (`ffmpeg-pin.json`) and push a new version tag. The SBOM is a
generated release artifact (written to the git-ignored `dist/` and regenerated
by the release CI on the tag), so it is not committed; the base image is pinned
by digest at build time, so `docker/Dockerfile` is not edited here either:

```sh
git add ffmpeg-pin.json
git commit -m "security: bump bundled FFmpeg to N-125000-gabcdef0123"
git tag v<next-version>
git push origin main v<next-version>
```

The release CI (`release.yml`) triggers on the tag and:

1. Runs unit tests
2. Builds and smoke-tests the PyInstaller bundle
3. Builds the Docker image (downloads and SHA-verifies the pinned FFmpeg) and
   runs the Trivy base-image CVE gate
4. Runs the systemd install test
5. Generates the SBOM, signs `SHA256SUMS`, and publishes the GitHub release

> **PyInstaller bundle gap:** as noted above, the release CI currently does not
> set `TLM_BUNDLE_FFMPEG_DIR` before building the bundle, so the released
> PyInstaller artifact uses the runner's PATH `ffmpeg` rather than the pin.
> Before a security-advisory release, add a step to the `bundle` job in
> `release.yml` that downloads + verifies the pinned build and sets
> `TLM_BUNDLE_FFMPEG_DIR` to its directory before running `./packaging/release.sh`.
> Until that is done, only the Docker image is guaranteed to ship the exact
> pinned binary.

---

## Summary of files touched

| File | Change |
|---|---|
| `ffmpeg-pin.json` | `version`, `url`, `sha256`, both `binaries.*` paths |
| `dist/cyclonedx-bom.json` | Regenerated by `make sbom` (git-ignored) — do not edit by hand |
| `SHA256SUMS` / `SHA256SUMS.asc` | Regenerated by `make sign` |
