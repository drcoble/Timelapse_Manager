# Installation & Deployment

Timelapse Manager ships two deployment artifacts for **linux/amd64**:

- A **PyInstaller one-directory bundle** — a self-contained directory with a
  single `timelapse-manager` executable plus the bundled Python runtime, FFmpeg,
  templates, and migrations. No Python installation on the host required.
- A **Docker image** — a multi-stage `linux/amd64` image with the app source
  and a pinned static FFmpeg, served behind a reverse proxy for TLS termination.

macOS, Windows, and linux/arm64 bundle targets are present in the build
configuration but are deferred post-V1.

---

## Bundle install

### Download and extract

Download the versioned tarball from the [GitHub releases page](https://github.com/drcoble/Timelapse_Manager/releases):

```
timelapse-manager-<version>-linux-amd64.tar.gz
```

Extract it:

```sh
tar -xzf timelapse-manager-<version>-linux-amd64.tar.gz
cd timelapse-manager-<version>-linux-amd64
```

The extracted directory contains the `timelapse-manager` executable and all
runtime support files. The bundle is self-contained and relocatable — it does
not require any system Python or system FFmpeg.

> **Note:** the `packaging/install.sh` installer and systemd unit template are
> part of the source repository (`packaging/`), not bundled inside the tarball
> itself. The systemd installation path below assumes you have cloned the
> repository alongside the extracted bundle, or that you copy the `packaging/`
> directory from the repository next to the extracted bundle directory.

### Run without installing (foreground)

To try the bundle without a system-level install:

```sh
./timelapse-manager run
```

The service starts and listens on HTTP port **8080** and HTTPS port **8443**.
All state defaults to a `data/` directory relative to the current working
directory. Override paths via environment variables:

```sh
TLM_PATHS__DATA_DIR=/srv/tlm-data \
TLM_DATABASE__URL=sqlite:////srv/tlm-data/timelapse.db \
./timelapse-manager run
```

Confirm it is healthy:

```sh
curl -k https://127.0.0.1:8443/healthz
# {"app_version":"...","ffmpeg_version":"...","ffmpeg_path":"...","db_status":"ok","alembic_revision":"..."}
```

---

## systemd installation

`packaging/install.sh` (in the repository's `packaging/` directory) automates
the systemd setup. With the repository checked out alongside the extracted
tarball, run it as root from inside the extracted bundle directory:

```sh
# From the extracted bundle directory, with packaging/ adjacent or from the repo:
sudo ./packaging/install.sh
```

**What it does:**

1. Creates a dedicated system group and unprivileged, no-login service user
   (`timelapse` / `timelapse`, configurable).
2. Creates the install, state, and config directories:
   - Install: `/opt/timelapse-manager` (owned root, read-only to the service)
   - State: `/var/lib/timelapse-manager` (owned by the service user)
   - Config: `/etc/timelapse-manager` (owned by the service user)
3. Copies the bundle into the install directory.
4. Renders the systemd unit template and writes it to
   `/etc/systemd/system/timelapse-manager.service`.
5. Runs `systemctl enable --now timelapse-manager`.

The installer is **idempotent**: re-running it upgrades the installed files in
place without recreating the service account or destroying existing state.

**Override installation paths** before running (all optional):

```sh
TLM_SERVICE_USER=tlm \
TLM_SERVICE_GROUP=tlm \
TLM_INSTALL_DIR=/opt/timelapse-manager \
TLM_STATE_DIR=/var/lib/timelapse-manager \
TLM_CONFIG_DIR=/etc/timelapse-manager \
sudo -E ./packaging/install.sh
```

### Service account and directories

| Path | Owner | Purpose |
|---|---|---|
| `/opt/timelapse-manager` | root | Bundle files (read-only to service) |
| `/var/lib/timelapse-manager` | `timelapse` | SQLite database, frames, TLS cert, local token |
| `/etc/timelapse-manager` | `timelapse` | Config file and optional env overrides |

### Configuration and ports

The systemd unit sets two environment variables unconditionally:

```
TLM_CONFIG=/etc/timelapse-manager/config.yml
TLM_PATHS__DATA_DIR=/var/lib/timelapse-manager
```

It also reads an optional environment file at startup:

```
/etc/timelapse-manager/timelapse-manager.env
```

A missing file is not an error. Use this file for runtime overrides —
ports, log level, etc. — as `KEY=VALUE` lines. Example:

```
TLM_SERVER__HTTP_PORT=8080
TLM_SERVER__HTTPS_PORT=8443
TLM_LOGGING__LEVEL=WARNING
```

For a full YAML config file, drop it at
`/etc/timelapse-manager/config.yml`. Example minimum config:

```yaml
server:
  http_port: 8080
  https_port: 8443
  redirect_http_to_https: true

paths:
  data_dir: /var/lib/timelapse-manager

database:
  url: sqlite:////var/lib/timelapse-manager/timelapse.db
```

See [docs/configuration.md](./configuration.md) for all settings.

### Manage the service

```sh
# Status and logs
systemctl status timelapse-manager
journalctl -u timelapse-manager -f

# Stop / start / restart
systemctl stop timelapse-manager
systemctl start timelapse-manager
systemctl restart timelapse-manager

# Disable on-boot
systemctl disable timelapse-manager
```

### Restart and crash behavior

The unit restarts on failure with a 5-second delay. If the service fails more
than 5 times within 60 seconds, systemd stops retrying and surfaces the fault
(check `journalctl`). Adjust `StartLimitBurst` and `StartLimitIntervalSec` in
the unit file if needed.

### Ports (systemd / bare-metal)

The systemd deployment serves both listeners:

| Port | Protocol | Purpose |
|---|---|---|
| 8080 | HTTP | Redirects browsers to HTTPS; API and `/healthz` are exempt |
| 8443 | HTTPS | Web UI (self-signed cert auto-generated on first start) |

The default non-privileged ports (8080/8443) do not require
`CAP_NET_BIND_SERVICE`. If you reconfigure to 80/443, add
`AmbientCapabilities=CAP_NET_BIND_SERVICE` to the unit's `[Service]` section.

### Key files

A runtime key file (e.g. an API credential or TLS private key you supply)
should be mounted or placed under `/etc/timelapse-manager` and pointed to via
the config file. **Never bake secrets into the unit file** — the installer
never writes them there.

---

## Docker

### docker compose (recommended)

The repository includes a ready-to-use Compose file. Build context is the
repository root (one level above `docker/`):

```sh
docker compose -f docker/docker-compose.yml up -d
```

Or, if you are pulling a released image from GHCR:

```yaml
# docker-compose.yml (using a published image)
services:
  app:
    image: ghcr.io/drcoble/timelapse_manager:v<version>
    ports:
      - "8080:8080"
    environment:
      TLM_PATHS__DATA_DIR: "/data"
      TLM_DATABASE__URL: "sqlite:////data/timelapse.db"
      TLM_SERVER__REDIRECT_HTTP_TO_HTTPS: "false"
    volumes:
      - tlm-data:/data
      - tlm-config:/config
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "--fail", "--silent", "http://localhost:8080/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  tlm-data:
  tlm-config:
```

### Ports (Docker)

The container image exposes only **port 8080** over plain HTTP.
`TLM_SERVER__REDIRECT_HTTP_TO_HTTPS` is set to `false` in the image — the
HTTP→HTTPS redirect is **disabled** by default. TLS is expected to be
terminated by a reverse proxy in front of the container.

> **Behind a reverse proxy:** configure the proxy to forward
> `X-Forwarded-Proto: https`. The app uses this header for secure-cookie and
> redirect decisions. Without it, browser sessions may not behave correctly
> over HTTPS.

Do not enable the in-app redirect in the container unless you also expose and
configure port 8443 — otherwise browsers are bounced to a port the container
does not serve.

### Volumes

| Mount point | Purpose |
|---|---|
| `/data` | SQLite database, captured frames, auto-generated TLS cert, local API token |
| `/config` | Optional config file; any runtime key files you supply |

Point the app at a config file with:

```
TLM_CONFIG=/config/config.yml
```

**Key files** (TLS certificates, external credentials) should be mounted into
`/config` at runtime — **never baked into the image**.

### Pull by digest

To pin a specific image version by digest (recommended for production):

```sh
# The exact digest for a release is in image-digest.txt (see Verification).
docker pull ghcr.io/drcoble/timelapse_manager@sha256:<digest>
```

---

## Signature and checksum verification

Every release on GitHub includes integrity and provenance material so you can
confirm you received exactly what was built and signed. For the full
step-by-step verification guide, see
[docs/verifying-releases.md](./verifying-releases.md).

Quick reference:

| File | Purpose |
|---|---|
| `timelapse-manager-<version>-linux-amd64.tar.gz` | The bundle tarball |
| `cyclonedx-bom.json` | CycloneDX SBOM (Python deps + bundled FFmpeg) |
| `image-digest.txt` | The published Docker image digest |
| `SHA256SUMS` | SHA-256 manifest over all three artifacts |
| `SHA256SUMS.asc` | Detached GPG signature over `SHA256SUMS` |
| `cosign.pub` | Public key for verifying the GHCR image signature |
| `KEYS` | Project GPG public key |

### Verify the bundle (GPG + checksums)

```sh
# Import the project GPG key (published as a release asset).
curl -fsSL https://github.com/drcoble/Timelapse_Manager/releases/latest/download/KEYS \
  | gpg --import

# 1. Verify the GPG signature over the manifest.
gpg --verify SHA256SUMS.asc SHA256SUMS

# 2. Verify each artifact against the manifest.
sha256sum --check SHA256SUMS
```

### Verify the Docker image (cosign)

```sh
# Download the cosign public key (also a release asset).
curl -fsSLO https://github.com/drcoble/Timelapse_Manager/releases/latest/download/cosign.pub

# Verify the image at the digest recorded in the release.
cosign verify --key cosign.pub \
  ghcr.io/drcoble/timelapse_manager@sha256:<digest>
```

After verification, pull and run by the authenticated digest:

```sh
docker pull ghcr.io/drcoble/timelapse_manager@sha256:<digest>
```

Pulling by digest guarantees you receive the exact image that was built,
scanned, and signed during the release — not a tag that could be moved.

---

## Tier sizing guidance

Frame storage accumulates continuously for the entire deployment lifetime. Size
the storage volume before deploying.

**Frame storage estimate** (from [docs/storage.md](./storage.md)):

- JPEG at 1080p: roughly 150 KB – 500 KB per frame, depending on scene
  complexity and encoder settings.
- Frames per day: `86400 / capture_interval_seconds` (e.g. every 5 minutes →
  288 frames/day).
- Daily volume: `avg_bytes_per_frame × frames_per_day`.
- Project lifetime volume: daily volume × number of days.

Add margin above the resume watermark (default: 2 GB / 10%). See
[docs/storage.md](./storage.md) for watermark settings.

**CPU and RAM rules of thumb:**

- **Capture** is I/O-bound (one HTTP/RTSP grab per interval per project) and
  light on CPU. A single-core host handles many concurrent cameras.
- **Rendering** is CPU-intensive and single-threaded per job (FFmpeg encodes
  one file at a time by default — `render.max_concurrent` defaults to 1).
  Encoding a long project can saturate a core for minutes to hours; schedule
  renders outside peak capture windows if needed.
- **Web UI and API** are served by a single Uvicorn worker and are effectively
  idle between requests.
- **RAM:** dominated by the frozen Python runtime at startup plus render job
  working sets during encoding. Size conservatively; the render worker is the
  primary memory consumer during active encoding.

**Network:**

- One camera at a short interval (e.g. 5 seconds) and 1080p JPEG generates
  roughly 100–360 MB/hour of inbound traffic from the camera.
- Scales linearly with camera count × resolution × inverse of capture interval.
- Rendered video exports and webhook notifications add outbound traffic.

**Bundle and image size:**

The PyInstaller bundle is sizable because it embeds the full CPython runtime
plus all dependencies, plus the static FFmpeg binary. Plan for a few hundred
megabytes of disk space for the bundle itself, separate from frame storage.
