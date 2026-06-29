# Timelapse Manager

Connects to network IP cameras, captures still images at configurable intervals
over hours to months, and assembles them into timelapse videos. Designed to run
unattended for long durations, surviving reboots and network interruptions.

Cross-platform (Windows, macOS, Linux) plus a Docker image. A single process
serves a **web UI**, a **CLI**, and a **background service** over a shared local
API.

## Tech stack

- **Python** (asyncio) backend on **FastAPI / Uvicorn**
- Server-rendered **Jinja2 + HTMX** web UI — no separate front-end build
- **SQLite** for application state; captured frames stored as image files on disk
- **FFmpeg** (bundled) invoked as a subprocess for encoding

## Features

- **Multi-protocol cameras** — HTTP/JPEG, RTSP, Axis VAPIX, and ONVIF, with
  ONVIF WS-Discovery and IP-range probing for network discovery.
- **Resilient long-running capture** — per-project capture loops with fault
  isolation, reconnect with exponential backoff, restart-gap logging (no
  backfill), and frozen-frame detection.
- **Flexible scheduling** — continuous interval, time-of-day windows,
  sunrise/sunset windows, day-of-week, and campaign date/frame bounds.
- **Solar & exact-time capture** — capture at an exact clock time or a solar
  event (solar noon, sunrise, sunset) computed from the camera's coordinates and
  shown in the camera's local timezone. A project can run *solar / scheduled
  times only* (single daily shots) instead of a continuous interval.
- **Event-triggered capture** — capture a frame on an ONVIF or VAPIX camera
  event (motion, tamper, analytics, etc.); available events are read from the
  selected camera.
- **Storage safeguards** — relocatable frame storage, auto-pause/resume on low
  disk (never deletes to reclaim space), and frame lifecycle (soft-delete,
  restore, permanent delete, upload, timestamp correction).
- **Video rendering** — FFmpeg encode with configurable codec/container/quality,
  optional hardware-accelerated encoding (auto-falls back to software),
  deflicker, burned-in overlays (timestamp / caption / watermark), and chapters
  (auto monthly/weekly or manual milestones).
- **Render automation** — bounded-concurrency render queue, a scheduler for
  recurring renders and archive snapshots, and post-render actions (export,
  webhook, prune).
- **Web UI over HTTPS** — dashboard, projects, cameras, frames, renders,
  settings, and user management, with a built-in auto-generated self-signed
  certificate.
- **Authentication & access control** — local username/password (Argon2id) and
  LDAP authentication, server-side sessions with CSRF protection, brute-force
  throttling, and three roles (Admin / Operator / Viewer) enforced
  deny-by-default. Users can change their own password; admins can set another
  user's password.
- **Monitoring & notifications** — structured operational event log plus an
  Admin-only audit log, a status banner, and a dispatcher that routes events to
  email (SMTP) and webhook channels with routing rules, bounded retry, and
  debounce.

**Status:** early development; all of the above is functional.

**V1 packaging** ships **linux/amd64** only: a PyInstaller self-contained
bundle (systemd install included) and a Docker image. macOS, Windows, and
linux/arm64 build targets are defined in the release workflow but are deferred
post-V1.

## Web UI

A server-rendered HTMX + Jinja2 interface is served over HTTPS at:

```
https://<host>:8443
```

**First-run setup:** on a fresh installation there are no default credentials.
Before the UI becomes usable, the application redirects every browser request
to a setup page at `/first-run` where you create the initial administrator
account. The admin password must be at least 12 characters. Setup completes
over HTTPS — the self-signed dev certificate is generated automatically on
first start if no certificate is configured.

**Roles:** the web UI enforces three roles. **Admin** has full control,
including managing user accounts and system settings. **Operator** can mutate
the operational surface (cameras, projects, renders, and frames) plus all read
operations, but cannot touch user accounts or system settings. **Viewer** has
read-only access. Authorization is deny-by-default server-side. Both local
username/password and LDAP authentication are supported. Signed-in users can
change their own password from **Preferences**, and admins can set another
user's password from the user-management screen.

**Sessions:** sessions are server-side and identified by a secure `HttpOnly`
cookie (`Secure` over HTTPS, `SameSite=lax` by default). Sessions expire after
30 minutes of inactivity or 24 hours of age; "remember me" extends the age cap
to 30 days. CSRF tokens are embedded on every page and verified on every
mutating request.

**CLI coexistence:** the local JSON API (`/api/v1/`) and `/healthz` are exempt
from the HTTPS redirect so the CLI can communicate with the service over
loopback HTTP using its bearer token, unchanged from before.

See [docs/web-ui.md](./docs/web-ui.md) for the full reference: access,
first-run flow, roles, session lifetime, CSRF, built-in TLS, reverse-proxy
setup, and page inventory.

## Development

```sh
# Install uv, then:
make bootstrap   # create virtualenv and install deps from uv.lock
make migrate     # apply database migrations (creates all core tables)
make run         # start the app at https://localhost:8443 (generates dev cert if missing)
```

On first `make run` a self-signed certificate for `localhost` is generated into
`./data/`. Your browser will show a security warning; add a one-time exception
to proceed. The CLI is unaffected (it talks to the local API over plain HTTP on
port 8080).

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full developer guide.

## Installation & Deployment

Three deployment paths are available. All V1 artifacts target **linux/amd64**.

### Bundle + systemd

Download the tarball from the [GitHub releases page](https://github.com/drcoble/Timelapse_Manager/releases)
and clone the repository alongside it. Run the installer as root from the
extracted bundle directory:

```sh
tar -xzf timelapse-manager-<version>-linux-amd64.tar.gz
cd timelapse-manager-<version>-linux-amd64
sudo ./packaging/install.sh   # packaging/ comes from the repository
```

The installer creates a dedicated service user, copies the bundle to
`/opt/timelapse-manager`, writes a hardened systemd unit, and enables the
service on boot. State lives in `/var/lib/timelapse-manager`; config goes in
`/etc/timelapse-manager`.

To run the bundle directly without installing:

```sh
./timelapse-manager run          # serves HTTPS on 8443, HTTP on 8080
```

### Docker

```sh
# From the repository root
docker compose -f docker/docker-compose.yml up -d

# Or pull a released image directly
docker pull ghcr.io/drcoble/timelapse_manager:v<version>
```

The container image serves plain HTTP on port **8080**. TLS is expected to be
terminated by a reverse proxy in front of the container; configure the proxy to
forward `X-Forwarded-Proto: https`.

### Verify release signatures

```sh
gpg --verify SHA256SUMS.asc SHA256SUMS   # verify the manifest signature
sha256sum --check SHA256SUMS             # verify each artifact
```

See [docs/installation.md](./docs/installation.md) for the full deployment
guide: bundle install, systemd setup, Docker compose, port and volume
configuration, signature verification, and storage sizing.

See [docs/ffmpeg-refresh.md](./docs/ffmpeg-refresh.md) for the process to
update the bundled FFmpeg when a security advisory is published.

## Configuration

Settings are loaded from (lowest to highest precedence): built-in defaults, an
optional config file (YAML or JSON), environment variables (`TLM_*` prefix with
`__` nesting), and explicit CLI flags. There is no default config file path; pass
`--config PATH` or set `TLM_CONFIG=PATH`.

Key sections: `server` (ports, bind address, HTTP-to-HTTPS redirect), `tls`
(certificate paths, auto-generate), `session` (cookie name, idle/absolute/
persistent timeouts, SameSite), `auth` (password minimum length, brute-force
throttle, Argon2 cost), `database` (SQLite URL), `logging` (level, format,
optional file sink), `paths` (data directory, frames root, token file),
`capture` (autostart, timeout, default interval, backoff, frozen-frame
detection, max idle sleep), `storage` (disk-space watermarks and probe interval
for the capture pause gate), `render` (concurrency, default fps, output
directory, webhook timeout, overlay font, scheduler interval), `monitoring`
(dispatcher autostart, poll interval, retry count and backoff, debounce window,
send timeout).

See [docs/configuration.md](./docs/configuration.md) for the full section
reference, all defaults, and an example config file.

## Command-line interface

Two console scripts are installed:

- **`timelapse`** — control and inspect a running instance
- **`timelapse-daemon`** — run the service in the foreground

```sh
timelapse version                   # print the application version
timelapse --config cfg.yaml config show          # show resolved config (secrets redacted)
timelapse --config cfg.yaml config show --json   # same, as JSON
timelapse system info               # fetch live system info from the running service
timelapse system info --json        # same, as JSON
timelapse migrate                   # apply database migrations

timelapse-daemon                    # start the service (HTTPS on 8443, HTTP redirect on 8080)
timelapse-daemon --config cfg.yaml  # start with a custom config file
```

`timelapse system info` connects to the service on `http://127.0.0.1:<http_port>`
and authenticates using the local bearer token read from `paths.token_file`
(default: `./data/.local-token`).

## Cameras & capture

Timelapse Manager supports four camera protocols: **HTTP/JPEG** (generic
snapshot URL), **RTSP** (single-frame grab via `ffmpeg`), **Axis VAPIX**
(Axis snapshot CGI with resolution/compression control), and **ONVIF** (SOAP
media-profile resolution). RTSP requires `ffmpeg` on `PATH`; ONVIF falls back
to a single-frame RTSP grab when the device exposes no snapshot endpoint, which
also requires `ffmpeg`.

Capture is per-**project** — a project binds a camera, a capture interval, and
a storage path. On startup the capture supervisor launches one background loop
per active project. Each loop evaluates the project's capture schedule and, when
the gate is open, grabs a frame at the project's interval, writes it atomically
to disk, and records a `Frame` row in the database. Per-project fault isolation
means one misbehaving camera never stalls others. On transient camera errors the
loop reconnects with capped exponential backoff. After a restart it resumes
forward, logging any downtime gap without backfilling or overwriting existing
frames. A frozen-camera detector warns when a camera returns identical frames
repeatedly. Projects with no schedule capture continuously at a fixed interval.

Beyond fixed-interval capture, a project can capture at exact clock times or at
solar events (solar noon, sunrise, sunset) derived from the camera's
coordinates — optionally as the *only* capture trigger, for a single daily shot.
A project can also capture in response to an ONVIF or VAPIX camera event such as
motion, tamper, or analytics detections; the available events are read from the
selected camera.

See [docs/scheduling.md](./docs/scheduling.md) for the scheduling model, worked
examples, and full reliability behavior.

Cameras can be discovered on the local network with ONVIF multicast
(WS-Discovery), or by unicast-probing an explicit IP range or CIDR.

See [docs/cameras.md](./docs/cameras.md) for protocol details, discovery, and
how the capture engine works.

## Storage & frame management

Captured frames are stored as image files under `paths.frames_root/<project_id>/`
by default; a per-project `storage_path` override directs frames to a different
directory or volume. File paths are stored relative to the project's frame
directory (default layout only), so the entire frames tree can be moved to a
new root without rewriting any database rows.

**Disk-space safeguard:** capture pauses automatically when free space on a
project's storage volume drops below a configurable low watermark, and resumes
without intervention once space recovers above a higher resume watermark. The
system never deletes frames to reclaim space — only new captures are held
back. Pause and resume transitions are written to the project event log.

**Frame lifecycle:** frames have an `active` or `soft_deleted` state. Soft
deletion hides a frame from default listings but keeps its file on disk and is
fully reversible. Permanent deletion (file + row, explicit confirm required,
admin only) is also available for frames that must be removed entirely. Frames
can be uploaded directly via the API (raw JPEG/PNG body, with a caller-supplied
capture timestamp) and any frame's capture timestamp can be corrected.

See [docs/storage.md](./docs/storage.md) for the full storage layout, watermark
details, sizing guidance, and the complete frame-management API reference.

## Video generation

Timelapse Manager renders a project's captured frames into a timelapse video
by invoking FFmpeg as a subprocess. Only active (non-deleted) frames are
included, assembled in capture-timestamp order. Source frame files are never
modified.

Output settings are configurable per render: frame rate, resolution (must be
even dimensions), codec (`h264`, `h265`/`hevc`, `vp9`, `av1`), container
(`mp4`, `mkv`, `webm`), and quality (`crf` for constant quality or
`bitrate_kbps` for a target bitrate). AV1 encodes are muxable into `mp4` and
`mkv` only. Optional hardware-accelerated encoding is supported and probed at
runtime; it automatically falls back to software encoding when the host or
bundled FFmpeg build cannot provide it.

Three optional overlay layers can be burned into the output: a **timestamp**
showing each frame's true capture time (not playback time), a fixed **text
caption**, and a **watermark image**. All overlays share a placement corner
(`top_left`, `top_right`, `bottom_left`, `bottom_right`). A **deflicker**
filter is also available to reduce inter-frame flicker.

**Chapters** embed named navigation markers in the output. They can be
generated automatically at monthly or weekly calendar boundaries, or placed
manually as **milestones** on the project's timeline. Chapters require an `mp4`
or `mkv` container (WebM does not support chapters).

Renders are processed by a **background queue** with bounded concurrency
(default: one render at a time) so renders never starve capture. A
**scheduler** periodically enqueues recurring renders and archive snapshots
based on per-project cadences. After a successful render, optional
**post-render actions** can export the file to a directory, POST a webhook
notification, or prune old non-archive renders.

H.264/MP4 renders are flagged as browser-streamable and support HTTP `Range`
requests for inline browser playback via the stream endpoint.

See [docs/rendering.md](./docs/rendering.md) for the full pipeline, all output
settings, overlay configuration, chapters, the queue and scheduler, post-render
actions, and the download/stream endpoints.

## Monitoring & notifications

Timelapse Manager maintains a structured event log of operational activity —
capture gaps, camera reconnects, render results, storage warnings — and can
forward events to external channels based on configurable routing rules.

**Web routes:**

| Route | Access | Description |
|---|---|---|
| `/events` | Any signed-in user | Operational event log (capture, camera, storage, render). Filterable by scope and severity. |
| `/events/audit` | Admin only | Audit log of security and control-action events (logins, settings changes). |
| `/notification-settings` | Admin only | Configure SMTP email and webhook channels, routing rules, and enabled channels. |

A **status banner** is lazily loaded on every page after login. It surfaces the
count of error-and-above events in the log and the most recent error message, if
any. If the banner query fails, the page degrades to a healthy/empty banner.

**Notification channels:** `email` (SMTP) and `webhook` (HTTP POST JSON). Both
are configured via the notification settings form. Routing rules map event
types and a minimum severity to one or more channels. Rules are re-evaluated on
every dispatcher poll cycle; transport settings (SMTP server, webhook URLs)
take effect on the next application restart.

**Delivery behavior:** the dispatcher sends only events that occur after startup
(no replay of historical events). Failed deliveries are retried up to the
configured limit with exponential backoff, then recorded as a
`notify.delivery_failed` event in the log — never re-notified. Repeated
notifications for the same event type and scope are suppressed within a
configurable debounce window.

See [docs/monitoring.md](./docs/monitoring.md) for the full reference: event
types, web views, channel setup, routing rules, webhook payload shape, SMTP
configuration, the masked-password save rule, and delivery semantics.

## HTTP API

All `/api/v1/*` routes require `Authorization: Bearer <token>`. The bearer
token is generated on first start and written to `paths.token_file` (default:
`./data/.local-token`).

### Infrastructure

| Endpoint | Auth | Description |
|---|---|---|
| `GET /healthz` | None | Liveness probe |
| `GET /api/v1/system` | Bearer token | System info (versions, config summary) |

`/healthz` returns `app_version`, `ffmpeg_version`, `ffmpeg_path`, `db_status`,
and `alembic_revision`. `ffmpeg_path` is the resolved path of the FFmpeg the app
will invoke — the bundled binary in a release, so the shipped encoder is
identifiable in the field. `/api/v1/system` returns the same version fields plus
a non-secret config summary.

### Cameras

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/cameras` | `POST` | Create a camera |
| `/api/v1/cameras` | `GET` | List all cameras |
| `/api/v1/cameras/{id}` | `GET` | Get a single camera |
| `/api/v1/cameras/{id}` | `DELETE` | Delete a camera |
| `/api/v1/cameras/{id}/validate` | `POST` | Probe reachability and authentication |
| `/api/v1/cameras/{id}/capture` | `POST` | Capture a single frame now |
| `/api/v1/cameras/{id}/capture-status` | `GET` | Live capture status for all projects backed by the camera |
| `/api/v1/cameras/discover` | `POST` | Discover cameras on the network |

**`POST /api/v1/cameras`** — create a camera.

Request body:
```json
{
  "name": "rooftop",
  "protocol": "vapix",
  "address": "CAMERA_HOST",
  "credentials": {"username": "admin", "password": "CAMERA_PASS"},
  "snapshot_uri": null,
  "stream_uri": null,
  "default_resolution": null,
  "geolocation_latitude": null,
  "geolocation_longitude": null,
  "geolocation_source": null
}
```

Supported protocols: `http`, `rtsp`, `vapix`, `onvif`. Credentials are
write-only and are never returned in responses.

**`POST /api/v1/cameras/{id}/validate`** — probes the camera and returns:
```json
{"ok": true, "reason": null, "message": "snapshot retrieved successfully"}
```

On failure, `reason` is one of `"auth"`, `"unreachable"`, `"timeout"`,
`"unsupported_codec"`, or `"other"`. A device-reported geolocation (when the
device supports it and no manual override exists) is persisted as a side-effect.

**`POST /api/v1/cameras/{id}/capture`** — triggers an immediate single-frame
capture. The frame must belong to a project (a project binds the camera to a
storage path), so a `project_id` is required:

```json
{"project_id": 1}
```

Response:
```json
{
  "frame_id": 42,
  "project_id": 1,
  "sequence_index": 7,
  "file_path": "/data/frames/1/00000007.jpg",
  "width": 1920,
  "height": 1080,
  "file_size_bytes": 204800,
  "captured_at": "2026-06-09T14:30:00.123456+00:00"
}
```

**`GET /api/v1/cameras/{id}/capture-status`** — returns the live capture state
for every project backed by the camera:

```json
{
  "camera_id": 3,
  "projects": [
    {
      "project_id": 1,
      "camera_id": 3,
      "state": "running",
      "last_success_at": "2026-06-09T14:30:00+00:00",
      "last_error_at": null,
      "last_error": null,
      "frames_captured": 120
    }
  ]
}
```

`state` is `"running"`, `"error"`, `"idle"`, or `"stopped"`.

**`POST /api/v1/cameras/discover`** — discover cameras on the network.

With no body (or `{"range": null}`): ONVIF multicast on the local segment.
With a range: unicast-probe each host in the CIDR or dash-range.

```json
{"range": "192.168.1.0/24"}
```

Response is a list of discovered cameras:
```json
[
  {
    "address": "CAMERA_HOST",
    "protocol": "onvif",
    "snapshot_uri": null,
    "stream_uri": null,
    "vendor": "Axis"
  }
]
```

### Frames

Read endpoints require a bearer token. Mutating endpoints (marked **Admin**)
require administrator privileges. Fine-grained roles (Admin / Operator / Viewer)
are enforced on the web UI; the local JSON API authenticates with a single
admin-level bearer token and is therefore treated as the administrator.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `GET /api/v1/frames` | `GET` | Bearer token | List a project's frames in capture order, paginated |
| `GET /api/v1/frames/capture-status` | `GET` | Bearer token | Live capture state for a project |
| `POST /api/v1/projects/{project_id}/frames/{frame_id}/soft-delete` | `POST` | Admin | Hide a frame; file kept on disk; reversible |
| `POST /api/v1/projects/{project_id}/frames/{frame_id}/restore` | `POST` | Admin | Restore a soft-deleted frame to active |
| `POST /api/v1/projects/{project_id}/frames/{frame_id}/permanent-delete` | `POST` | Admin | Irreversibly remove row and file (requires `?confirm=true`) |
| `PATCH /api/v1/projects/{project_id}/frames/{frame_id}` | `PATCH` | Admin | Correct a frame's capture timestamp |
| `POST /api/v1/projects/{project_id}/frames/upload` | `POST` | Admin | Import a raw JPEG/PNG as an uploaded frame |

**`GET /api/v1/frames?project_id=1&limit=100&offset=0&include_deleted=false`**
— returns up to 500 frames per page in capture order (oldest first). `limit`
defaults to 100; maximum is 500. Pass `include_deleted=true` to include
soft-deleted frames.

Each frame object:
```json
{
  "id": 42,
  "project_id": 1,
  "sequence_index": 7,
  "capture_timestamp": "2026-06-09T14:30:00",
  "file_path": "00000007.jpg",
  "width": 1920,
  "height": 1080,
  "file_size_bytes": 204800,
  "capture_status": "captured",
  "origin": "captured",
  "lifecycle_state": "active",
  "dimension_mismatch": false
}
```

`file_path` is stored as a bare filename for default-layout projects (see
[docs/storage.md](./docs/storage.md)). `origin` is `"captured"` or
`"uploaded"`. `lifecycle_state` is `"active"` or `"soft_deleted"`.
`dimension_mismatch` is `true` when this frame's dimensions differ from the
project's most-common frame size.

**`GET /api/v1/frames/capture-status?project_id=1`** — returns the live
capture state for a single project:

```json
{
  "project_id": 1,
  "camera_id": 3,
  "state": "running",
  "last_success_at": "2026-06-09T14:30:00+00:00",
  "last_error_at": null,
  "last_error": null,
  "frames_captured": 120
}
```

Returns `"state": "idle"` with zero counts when the project has no running
capture task.

For the full frame management API (soft-delete, restore, permanent delete,
upload, timestamp correction) see [docs/storage.md](./docs/storage.md).

### Renders and milestones

Read endpoints require a bearer token. Mutating endpoints (marked **Admin**)
require the admin-level local token. (Fine-grained roles are enforced on the web
UI; the JSON API uses a single admin-level token.)

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/v1/projects/{project_id}/renders` | `POST` | Admin | Trigger a manual render (returns `201`) |
| `/api/v1/projects/{project_id}/renders` | `GET` | Bearer | List a project's render jobs, newest first |
| `/api/v1/renders/{id}` | `GET` | Bearer | Get a single render job's status |
| `/api/v1/renders/{id}/cancel` | `POST` | Admin | Cancel a pending or in-flight render |
| `/api/v1/renders/{id}/download` | `GET` | Bearer | Download the output file |
| `/api/v1/renders/{id}/stream` | `GET` | Bearer | Stream for inline playback (HTTP Range / 206 for H.264/MP4) |
| `/api/v1/projects/{project_id}/milestones` | `POST` | Admin | Create a milestone (returns `201`) |
| `/api/v1/projects/{project_id}/milestones` | `GET` | Bearer | List a project's milestones |
| `/api/v1/projects/{project_id}/milestones/{id}` | `DELETE` | Admin | Delete a milestone (returns `204`) |

See [docs/rendering.md](./docs/rendering.md) for full request/response shapes,
all output settings, overlay options, and post-render action configuration.

## Security

### SSRF protection

All outbound HTTP requests (camera snapshot fetches, webhook deliveries) pass
through a two-tier SSRF deny-list before a connection is made:

- **Always blocked:** loopback (`127.0.0.0/8`, `::1/128`), link-local /
  cloud-metadata (`169.254.0.0/16`, `fe80::/10`), unspecified, multicast, and
  reserved ranges. These cannot be relaxed.
- **Private addresses (RFC 1918, CGNAT, IPv6 ULA) are blocked by default.**
  For LAN cameras that live on a private subnet, opt specific subnets in via
  config:

```yaml
ssrf:
  allowed_private_subnets:
    - "192.168.1.0/24"
    - "10.0.1.0/24"
```

Or via environment variable:

```sh
TLM_SSRF__ALLOWED_PRIVATE_SUBNETS='["192.168.1.0/24"]'
```

Private opt-in applies to camera fetches only. The webhook surface always uses
the full deny-list with no private relaxation. Redirects are never followed on
outbound requests.

### Credential encryption at rest

Camera passwords, SMTP passwords, and webhook URLs are encrypted before storage
using Fernet symmetric encryption (`enc:v1:` prefix). The encryption key is
stored in the OS keystore (macOS Keychain, GNOME Keyring, Windows Credential
Manager) when available, and falls back to a `0600` key file at
`<data_dir>/.secret-key`. The key file is git-ignored. Usernames are stored
in plaintext for display.

To control key storage:
```yaml
secrets:
  use_os_keystore: true         # try OS keystore first (default)
  key_file: /path/to/.secret-key  # explicit path (optional)
```

Environment: `TLM_SECRETS__USE_OS_KEYSTORE`, `TLM_SECRETS__KEY_FILE`.

### Log redaction

Credential material is scrubbed from logs before any log sink writes it:
URL userinfo (`scheme://user:pass@host`) is replaced with `scheme://***@host`,
and query-string parameters with secret-sounding names (token, password,
api_key, secret, sig, etc.) have their values masked. The `httpx` library
logger is capped at `WARNING` to suppress URL-containing debug lines.

### Security audit

See [`docs/security-audit.md`](./docs/security-audit.md) for the full
security audit: verified controls, residual risks, and known gaps.

## License

Licensed under the **Apache License, Version 2.0**. You may use, modify, and
distribute this software, including in commercial and closed-source products,
subject to the terms of the license. See [`LICENSE`](./LICENSE) for the full
text and [`NOTICE`](./NOTICE) for attribution.

Copyright 2026 Drew Coble.

This project bundles [FFmpeg](https://ffmpeg.org/), which is licensed separately
under the LGPL/GPL; see FFmpeg's own license for its terms.
