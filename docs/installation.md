# Installation & Deployment

Timelapse Manager ships these deployment artifacts for **linux/amd64**:

- A **PyInstaller one-directory bundle** — a self-contained directory with a
  single `timelapse-manager` executable plus the bundled Python runtime, FFmpeg,
  templates, and migrations. No Python installation on the host required.
- A **Docker image** — a multi-stage `linux/amd64` image with the app source
  and a pinned static FFmpeg, served behind a reverse proxy for TLS termination.
- **Native packages** (`.deb` and `.rpm`) and a **Proxmox CT template**, built
  from the same bundle.

| Method | Use it for | Guide |
|---|---|---|
| Tarball + `install.sh` | Any systemd host | [Bundle install](#bundle-install), [systemd installation](#systemd-installation) |
| `.deb` / `.rpm` | Debian 13, EL9 and newer (download and install the file; there is no apt or dnf repository) | [install-deb-rpm.md](./install-deb-rpm.md) |
| Docker image | Container hosts | [install-docker.md](./install-docker.md) |
| Proxmox CT template | A ready-made container on Proxmox VE | [install-proxmox-ct.md](./install-proxmox-ct.md) |

macOS, Windows, and linux/arm64 bundle targets are present in the build
configuration but are deferred post-V1.

---

## Bundle install

### Download and extract

Download the versioned tarball from the
[GitHub Release](https://github.com/drcoble/Timelapse_Manager/releases)
(`https://github.com/drcoble/Timelapse_Manager/releases/download/v<version>/`;
verify it as described in [Verifying Releases](./verifying-releases.md)):

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

With no `--config` and no `TLM_CONFIG`, `run` also loads
`/etc/timelapse-manager/config.yml` (or the per-user file) when it exists and is
readable; see [CLI config file lookup](#cli-config-file-lookup).

Confirm it is healthy:

```sh
curl -k https://127.0.0.1:8443/healthz
# {"status":"ok","app_version":"...","ffmpeg_version":"...","ffmpeg_path":"...","db_status":"ok","alembic_revision":"...","components":{...},"checks":{...}}
```

Run this on the host itself. From any other address `/healthz` answers only
`{"status":"ok","db_status":"ok"}`. `status` is `ok`, `degraded` (200: something
has stalled; the `checks` map says what) or `failed` (503). To test it in a
script, use `jq -e '.status=="ok"'` or match the start of the body; a plain
substring match for `"status":"ok"` also matches inside `checks`.

`checks.event_loop_lag` is `degraded` when the last three 1 s `loop-lag` ticks
were each more than 1 s late, or when the latest tick was 5 s or more late. One
slow tick alone leaves it `ok`, so it does not flap a monitor.

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
   - Config: `/etc/timelapse-manager` (owned by root, group-readable by the service user; the service never writes there)
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
| `/etc/timelapse-manager` | `root:timelapse` (0750) | Config file and optional env overrides; read-only to the service |

### Configuration and ports

The systemd unit sets three environment variables unconditionally:

```
TLM_CONFIG=/etc/timelapse-manager/config.yml
TLM_PATHS__DATA_DIR=/var/lib/timelapse-manager
TLM_DATABASE__URL=sqlite:////var/lib/timelapse-manager/timelapse.db
```

The `timelapse-manager` CLI, run by hand, does not get these. It finds
`/etc/timelapse-manager/config.yml` by itself; see
[CLI config file lookup](#cli-config-file-lookup).

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

### First-run setup code

On a fresh install the first-run page asks for a one-time setup code. The
service writes it to `setup-code` in the data directory the first time the page
is opened and logs the path. Print it (creating it if needed) with:

```sh
sudo timelapse-manager setup-code                 # package install
docker compose exec app timelapse setup-code      # Docker
```

The file is removed once the administrator exists. Installs that already have
an administrator never create it. `timelapse-manager user create --role admin`
remains the headless alternative and bypasses first-run. See
[docs/web-ui.md](./web-ui.md#first-run-setup).

### Backups, restore and pre-migration snapshots

Before a migration, the service copies a database that is behind to
`/var/lib/timelapse-manager/timelapse.db.pre-<revision>-<UTC time>` (SQLite
backup API, integrity-checked, mode 0600). The newest three are kept, plus the
first copy taken at the current revision. If there is less free space than
twice the database plus its WAL, or the copy fails, the service does not start
and the database is not touched.

Back up at any time, with the service running:

```
sudo env TLM_DATABASE__URL=sqlite:////var/lib/timelapse-manager/timelapse.db \
    /opt/timelapse-manager/timelapse-manager db backup /root/timelapse-$(date +%F).db
```

Restore a backup or snapshot. The service must be stopped; `db restore`
refuses while anything answers on `127.0.0.1:<http_port>/healthz`:

```
sudo systemctl stop timelapse-manager
sudo env TLM_DATABASE__URL=sqlite:////var/lib/timelapse-manager/timelapse.db \
    /opt/timelapse-manager/timelapse-manager db restore <file>
sudo systemctl start timelapse-manager
```

The replaced database is kept as `timelapse.db.replaced-<time>`, with its
`-wal` file. Pending migrations run on the next start, behind a new snapshot.
Restoring the database does not restore frames on disk.

Back up the encryption key (`<data_dir>/.secret-key`, or the file named by
`secrets.key_file`) together with the database. A restored database whose
credentials the current key cannot decrypt stops the service at the next start
with exit status 78; restore the matching key first. See
[Encryption key integrity](#encryption-key-integrity).

### Restart and crash behavior

The unit restarts on failure with a 5-second delay. If the service fails more
than 5 times within 60 seconds, systemd stops retrying and surfaces the fault
(check `journalctl`). Adjust `StartLimitBurst` and `StartLimitIntervalSec` in
the unit file if needed.

A migration that cannot snapshot the database stops startup with `database
migration aborted before any change: ...` in the journal. Free space in the
state directory, then start the service again.

If the application cannot finish starting (for example, a startup step fails),
the service logs `service failed to start; exiting with status 3` and exits
with status 3. That is a failure to systemd, so `Restart=on-failure` restarts
the service after the 5-second delay. (A stop requested while the service is
still starting is a clean exit and is not restarted.)

If the encryption key is missing or does not match the stored credentials, the
service logs `Refusing to start: ...` and exits with status 78 instead. The unit
lists that status in `RestartPreventExitStatus`, so it is **not** restarted: a
restart cannot fix it. See
[Encryption key integrity](#encryption-key-integrity) for the recovery steps. A
hand-installed unit from an earlier version needs `RestartPreventExitStatus=78`
added.

`systemctl stop` or `restart` sends `SIGTERM`. The service stops both listeners
together and waits up to `server.shutdown_grace_seconds` (10 s) for open
connections. It then cancels any render in progress: ffmpeg is killed and the
partial file is removed. Finally it stops capture. The journal's last line is
`service stopped: both listeners closed, lifespan shut down`. The unit allows
45 s (`TimeoutStopSec`) before systemd sends `SIGKILL`. A hand-installed unit
from an earlier version has `TimeoutStopSec=30s`; update it to match.

**A render interrupted by a clean stop is requeued once.** If the stop finishes
within the grace, an encode job that was running goes back to `pending` and runs
again at the next start, with a `render.requeued` info event. It fails (and can
be rendered again) if a stop interrupts it a second time, if the stop overruns
the grace, or after a crash or `SIGKILL`, which the startup sweep fails as
before. Exports are never requeued: a stopped export fails and can be requested
again.

### Hardware-accelerated encoding (GPU access)

The unit runs with `PrivateDevices=true`, which hides `/dev/dri` and
`/dev/nvidia*` from the service. With `render.hwaccel_enabled` set, the service
logs an error at startup naming the device it cannot open. Grant access only on
a host that uses a GPU, with a drop-in. `install.sh` leaves drop-ins alone
across upgrades.

VA-API or Quick Sync (Intel/AMD):

```
sudo mkdir -p /etc/systemd/system/timelapse-manager.service.d
sudo tee /etc/systemd/system/timelapse-manager.service.d/hwaccel.conf <<'CONF'
[Service]
PrivateDevices=false
DeviceAllow=/dev/dri/renderD128 rw
SupplementaryGroups=render
CONF
sudo systemctl daemon-reload && sudo systemctl restart timelapse-manager
```

Use the render node you configured in `render.hwaccel_device` if it is not
`renderD128`. On distributions without a `render` group, use the group that
owns the node (`ls -l /dev/dri`).

NVIDIA (NVENC): the same file, with `DeviceAllow=/dev/nvidiactl rw`,
`DeviceAllow=/dev/nvidia0 rw` (your GPU index) and `DeviceAllow=/dev/nvidia-uvm
rw`, and no `SupplementaryGroups` line.

Remove the drop-in, then run `daemon-reload` and restart, to go back to the
default.

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

To run a released image from `ghcr.io/drcoble/timelapse_manager` instead, see
[Install with Docker](./install-docker.md), which has the digest pull and a
Compose file for the published image.

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
| `/data` | SQLite database, captured frames, renders and exports, `.secret-key`, auto-generated TLS cert, local API token |
| `/config` | Optional config file; any runtime key files you supply |

Point the app at a config file with:

```
TLM_CONFIG=/config/config.yml
```

**Key files** (TLS certificates, external credentials) should be mounted into
`/config` at runtime — **never baked into the image**.

### Pull by digest

Pin a release by the digest in its `image-digest.txt` (recommended for
production). See [Install with Docker](./install-docker.md#pull-by-digest).

---

## Upgrading to 0.2.0

These notes apply to a package install and to Docker. The full list of changes
is in the [changelog](../CHANGELOG.md); this section is what an operator has to
do or check. Commands are written for a package install (`timelapse-manager`);
the Docker image names the same CLI `timelapse`.

Before upgrading, take a database backup (see
[Backups, restore and pre-migration snapshots](#backups-restore-and-pre-migration-snapshots))
and **copy the encryption key with it** (see
[Encryption key integrity](#encryption-key-integrity)).

### Migrations

Migrations run on the first start, behind the pre-migration database snapshot.
A database from 0.1.x (the public `v0.1.0` is at revision 022) goes through
**twenty migrations, 023 to 042**; a database already at a 0.2.0 development
build runs only the ones it has not yet applied. Most only add a column, a table
or an index, but three groups need care:

- **Migrations 028, 029 and 031 rebuild tables.** SQLite copies each table into a
  new one with `AUTOINCREMENT`, so the ids of deleted rows are never handed out
  again. 031 rebuilds five tables at once, including `frame`, which is the
  largest table on a long-running install, so the first start can take noticeably
  longer there. The rebuilds keep every row
  and foreign key, check for dangling references before and after, and refuse to
  continue if the check fails. 030 adds a column to the `report` table and may
  rebuild it too.
- **Migration 025 can stop the upgrade.** It adds a case-insensitive username key.
  Directory (LDAP) accounts whose names differ only in case are merged into the
  oldest one, which takes the highest role. If two *local* accounts, or a local
  and a directory account, collide, the upgrade stops with an error naming them:
  rename or delete one on the old version and upgrade again.
- **Migrations 025, 028, 029 and 031 change existing rows or tables.** The rest
  leave existing rows as they are.

**Take the backup first** (see
[Backups, restore and pre-migration snapshots](#backups-restore-and-pre-migration-snapshots)).
The service also writes a verified snapshot,
`timelapse.db.pre-<revision>-<UTC time>`, before it migrates, needs about twice
the database size free, and does not start (leaving the database untouched) if
it cannot make the copy. To go back, stop the service and run
`timelapse-manager db restore <snapshot>`; the new code cannot run on the old
schema, so restore the old version's package with it.

| Migration | Adds or changes | Effect on existing data |
|---|---|---|
| `023_add_notification_webhook_signing_secret` | `notification_settings.webhook_signing_secret`, nullable | Webhooks stay unsigned until a secret is set. |
| `024_add_login_attempt` | `login_attempt` table | Empty. Backs the login throttle across restarts. |
| `025_add_user_username_key` | `user.username_key` (NOT NULL) and a unique index | **Every user row gets a key; duplicate directory accounts are merged, and local duplicates stop the upgrade** (see above). |
| `026_add_render_job_frame_window` | `render_job.frame_count`, `first_frame_at`, `last_frame_at`, nullable | Existing jobs show "not recorded". |
| `027_add_report_table` | `report` table | Empty. Stores problem reports. |
| `028_render_job_autoincrement` | **Rebuilds `render_job`** with `AUTOINCREMENT` | Rows are kept. The id counter is raised above the highest job id the event log records, so a deleted job's id is not reused. |
| `029_report_autoincrement` | **Rebuilds `report`** with `AUTOINCREMENT` | Rows are kept. |
| `030_add_report_anonymized` | `report.anonymized`, default false | Every existing report reads as not anonymized. May rebuild `report`. |
| `031_entity_autoincrement` | **Rebuilds `user`, `camera`, `project`, `milestone` and `frame`** with `AUTOINCREMENT` | Rows are kept. Each id counter is raised above the highest id the database or audit log records for it. Runs a foreign-key check before and after and refuses to continue on a dangling reference. |
| `032_render_job_codec` | `render_job.codec_string` and `mime_type`, nullable | Not probed here; the render worker fills them in one at a time after startup. Until then the page checks the MIME type only. |
| `033_event_hot_path_indexes` | Two `event` indexes | None; index only. |
| `034_exact_time_fire_retry` | `exact_time_fire.attempts` (default 1) and `next_attempt_at` | Existing rows read as one attempt. |
| `035_frame_read_path_indexes` | Two `frame` indexes | None; index only. |
| `036_notification_cursor` | `notification_cursor` table | Empty. The first start places the cursor at the newest event, so no history is sent. |
| `037_render_job_hot_path_indexes` | Two `render_job` indexes | None; index only. |
| `038_camera_allow_basic_over_http` | `camera.allow_basic_over_http`, default off | Every existing camera has it off. See [Camera authentication](#camera-authentication). |
| `039_camera_tls_pinned_cert` | `camera.tls_pinned_cert_pem`, nullable | No camera is pinned. Nothing runs at upgrade, at capture time or on first use. See [Camera certificate pinning](#camera-certificate-pinning). |
| `040_secrets_meta` | `secrets_meta` table (provider and key fingerprint) | Empty until the first start records the key. See [Encryption key integrity](#encryption-key-integrity). |
| `041_project_active_since` | `project.active_since`, nullable | Nothing is rewritten. A project with no value counts from its creation time until it is next resumed, started or reactivated, which stamps the column. See [Exact-time anchors and project activation](./monitoring.md#exact-time-anchors-and-project-activation). |
| `042_render_job_shutdown_requeues` | `render_job.shutdown_requeues`, `INTEGER NOT NULL DEFAULT 0` | Added in place; no table is rebuilt and no row is rewritten (every existing job has 0). It counts how often a service stop put the job back in the queue (at most once). |

### Encryption key integrity

The key that encrypts stored credentials lives outside the database: a key file
(`<data_dir>/.secret-key` unless `secrets.key_file` is set) or the OS keystore.
Version 0.2.0 records the key's provider and a fingerprint in `secrets_meta` and
checks them at every start.

- **First start after the upgrade.** An install with encrypted credentials has
  no record yet. The service tests the current key against one stored value per
  encrypted column; if it decrypts them, the key is recorded and startup
  continues. No action is needed. A fresh install with nothing encrypted creates
  the key (exclusively) and records it.
- **Later starts.** The recorded provider is used, and the key's fingerprint
  must match the record. A key that is missing or does not match, or a key file
  that is unreadable, readable by group or others, or malformed, stops startup.
  A zero-length key file counts as missing. A key is never created while
  encrypted values exist.
- **When it stops.** The journal has one CRITICAL message beginning `Refusing to
  start:` (the key location and the first 8 characters of the expected
  fingerprint; no key material, and the recovery steps), then `service refused
  to start over the encryption key; exiting with status 78 (no automatic
  restart)`. The exit status is **78**. The unit sets
  `RestartPreventExitStatus=78`, so `Restart=on-failure` does not loop on a
  fault only an operator can fix. A hand-installed unit needs that line in its
  `[Service]` section; `install.sh` re-renders the unit on upgrade. Other
  startup failures still exit with status 3 and restart.

**Recovery.**

1. See what the service sees:

   ```sh
   sudo timelapse-manager secrets status
   ```

   It prints the provider, the key's location (and mode), the recorded and
   current fingerprint prefixes, the number of encrypted values per column and a
   `state:` line. It prints no secrets, and exits 0 only when the state is `ok`.
2. **Preferred:** restore the original key from backup to the same path, mode
   `0600`, owned by the service account, and start the service.
3. **Key lost for good:** stop the service, reset, start it again:

   ```sh
   sudo systemctl stop timelapse-manager
   sudo timelapse-manager secrets reset --confirm
   sudo systemctl start timelapse-manager
   ```

   `reset` removes every stored encrypted value (camera passwords, the default
   camera credentials, the SMTP password, the webhook signing secret and the
   LDAP bind password) and records a new key. With a key file, the old file is
   kept beside the new one as `<key file>.replaced-<UTC time>`. It refuses
   without `--confirm`, and refuses while the service answers on loopback.
4. After a reset, sign in with a **local break-glass administrator**: the LDAP
   bind password is cleared, so directory login does not work until it is
   entered again. Re-enter the camera passwords, the default camera
   credentials, the SMTP password, the webhook secret and the LDAP bind
   password.

Started with `sudo`, the `secrets` commands run as the owner of the data
directory, so no root-owned key file or SQLite side file is created.
`timelapse-manager secrets rotate` replaces the key and re-encrypts every stored
value; it needs the service stopped and a key already recorded (start the
service once first). Back up the new key afterwards.

**Back up the key with the database.** A database backup restored without its
key is refused at the next start, and a restore with `db restore` needs the
matching key in place.

### Session cookie

Over HTTPS the session cookie is `__Host-tlm_session` (`__Host-` plus
`session.cookie_name`), with `Secure`, `Path=/` and no `Domain`. A browser
refuses to let a sibling subdomain set or overwrite such a cookie. Over plain
HTTP the cookie keeps its old name, `tlm_session`. Over HTTPS only the
`__Host-` name is read.

- **The name and `Secure` come from one decision:** TLS on the connection, or
  `X-Forwarded-Proto: https` from a peer listed in `server.trusted_proxies`.
  Behind a reverse proxy, the proxy must be in `server.trusted_proxies` and send
  the header. If it is not, the app sees HTTP and keeps the old, non-`Secure`
  name; nobody is locked out.
- **Sessions under the old name sign in once.** Over HTTPS the old name is not
  read at all, so a browser that still holds a session under `tlm_session` (one
  from before 0.2.0) is treated as signed out and signs in again; the new
  session is issued under the `__Host-` name. A cookie planted under the old
  name by another site is ignored. The old name is still the one in use over
  plain HTTP and with `session.host_prefix: false`.
- **A tab left open over the upgrade** holds the old CSRF token, and the bare
  session secret is no longer accepted. Its first form submit shows the reload
  prompt ("Your session changed in another tab"); reload the page once and
  repeat the action.
- **`session.accept_legacy_cookie` is gone.** A config file or
  `TLM_SESSION__ACCEPT_LEGACY_COOKIE` that still sets it is ignored; delete the
  line.
- **If the browser sends a duplicated session cookie, or one whose name is not an
  exact match,** the app refuses every session cookie of that name. The browser
  is signed out, and signing in again does not help. Clear the browser's cookies
  for the parent domain (for example `*.example.com`) and sign in.
- The login and first-run forms carry a per-visit token in a `tlm_presession`
  cookie (`__Host-tlm_presession` over HTTPS). A script that POSTs `/login`
  without loading the form first gets 403.

### Content-Security-Policy

Every response carries `script-src 'self'`: no inline scripts, no `on*=`
handlers and no `eval`. `style-src` still allows inline styles. htmx no longer
runs script tags in swapped content. A customised template must not add an
inline `<script>` or an `on*=` attribute; register a `data-action` in
`static/js/actions.js` instead.

### Camera authentication

- **Basic over plain HTTP** (migration 038). A camera at an `http://` address
  that offers only HTTP Basic stops capturing until *Allow Basic authentication
  over plain HTTP* is ticked on that camera, or its address becomes `https://`.
  The option is hidden while the address is `https://`. Digest cameras and
  `https://` cameras are not affected.
- **The default login is sent only to cameras inside
  `ssrf.allowed_private_subnets`.** A camera that inherits the saved default
  login gets it only when its address, snapshot URI and stream URI all resolve
  inside that list. **The setting defaults to empty, and an empty list means the
  default login is never sent.** A camera whose address resolves outside the
  list, a name with any address outside it (an AAAA record, for example) and a
  name that does not resolve are captured without the default login and fail
  authentication. Before upgrading, put the camera subnet in the list, for
  example `ssrf.allowed_private_subnets: ["192.168.1.0/24"]`, then check that each
  inheriting camera still captures. Query (web) and Detect (API) outside the
  list run without the default and say so; typed credentials are always sent.
- **Narrowing the list stops the default login at once.** A cached camera
  session that holds the default login is checked against the live
  `ssrf.allowed_private_subnets` each time it is used; one whose hosts are no
  longer inside is dropped and reloaded without the default. Sessions with their
  own credentials are not touched. A hostname camera whose name briefly fails to
  resolve loses the default the same way, and gets it back by itself: a session
  that was loaded without the default is asked again on later uses, at most
  every 15 s, so DNS recovering or the list being widened restores it with no
  edit or restart. Only cameras that inherit the default are affected.
- **The default login is also held to the subnets at the socket.** A request that
  carries it connects only when every address the host has is inside the list
  as it is now, and a kept-alive connection is checked again before each write,
  so a rebinding answer in the 60 s cache window, or a list narrowed while a
  connection is open, cannot carry the login out of the subnets. This reaches
  every route the login can take: the snapshot and SOAP requests, the VAPIX
  event WebSocket, and ffmpeg. ffmpeg does its own lookup, so for a camera that
  inherits the default and has a `rtsp://` stream URI addressed by name, the
  grab connects to the IP the name resolved to. **An `rtsps://` stream addressed
  by name cannot use the default login** (the certificate is checked against the
  name, so ffmpeg cannot be given the address): use an IP address in the URI or
  give the camera its own credentials. Cameras with their own credentials are
  not affected.
- **ONVIF addresses must name the camera's host.** A snapshot, stream, events or
  subscription address the camera reports for another host is refused; store the
  snapshot or stream URI on the camera form to accept it.
- **Editing a camera onto a new host with a blank password drops its stored
  login.** Changing only the scheme or port keeps it.
- **`user:pass@` in an address or URI** is moved into the encrypted credentials
  at startup (once), or refused with 422 on save.
- Discovery enrichment sends no credentials; use Query with typed credentials
  on the add form to fill the URIs of a camera that needs a login.

### Camera credentials and `/proc`

An RTSP grab passes the camera's login to ffmpeg in its command line, because
FFmpeg's RTSP input has no other way to take one (there is no auth option, and
`-i` cannot be read from a file). For the few seconds a grab runs, any local
user can read it from `ps` or `/proc/<pid>/cmdline`. Cameras that use HTTP
(snapshot, VAPIX, ONVIF) are not affected. On a host with login users other than
the administrators, mount `/proc` so a user sees only their own processes:

```
proc /proc proc defaults,hidepid=2,gid=<monitoring group> 0 0
```

in `/etc/fstab`, then `mount -o remount /proc`. `gid=` names a group that may
still see every process (a monitoring agent, for instance); `subset=pid` also
hides the non-process entries. systemd's `ProtectProc=` and `ProcSubset=` do not
help here: they change what the service sees of other processes, not what other
users see of the service. On a single-purpose VM this matters little. A camera
whose login must not be readable locally should have a read-only account.

### Camera traffic and proxies

Camera connections (capture, event listeners, Detect, Query, discovery,
validation) ignore `HTTP_PROXY`, `HTTPS_PROXY` and `NETRC`, because a proxy
would defeat address pinning. `SSL_CERT_FILE`, `SSL_CERT_DIR` and
`capture.tls_ca_bundle` still apply. A camera that can be reached only through
a proxy stops working.

### Frame exports

Finished frame exports are deleted after `render.export_ttl_days` (default **7**)
by the hourly housekeeping; the zip and its row both go. Set
`render.export_ttl_days: 0` (`TLM_RENDER__EXPORT_TTL_DAYS=0`) to keep exports
until they are deleted by hand. Exports also build on their own slot,
`render.max_concurrent_exports` (default 1), so one no longer holds a render's
slot.

A frame export that names its frames as an explicit id list longer than
`frames.max_range_ids` (default 50000) is refused with **413**
(`{"error": "Selection too large; narrow the selection."}`); a client that
posts long id lists must split them. An export that names a descriptor, such as
a whole project, is resolved on the server and is not capped. Lists up to the
limit work, including past 32,766 ids, which used to fail with a 500.

### LDAP and SMTP settings

- **Retargeting clears the stored password.** Saving LDAP settings with a
  different server URL, TLS mode, CA certificate path (`tls_ca_cert_path`) or
  bind DN, or SMTP settings with a different host, port, security mode or
  username, and a blank (or masked) password field, clears the stored password. Enter the password in the same save. The LDAP form
  says "Re-enter the bind password" when a bind DN has none stored. Test
  connection reads the saved settings only.
- **New LDAP configurations default to `ldaps`.** An existing configuration keeps
  the mode it has; an unknown mode is rejected. Administrators see a banner
  while LDAP runs with TLS mode `none`.
- **An unknown SMTP security mode is rejected.** The modes are `none`,
  `starttls` and `tls`. Any other value, or a missing one, returns the form with
  a 422 and nothing is saved.
- **Switching LDAP off signs out every directory user at save.** Saving the LDAP
  settings with LDAP off revokes all directory sessions in the same transaction,
  including your own if you signed in through the directory. Switch it off from
  a local administrator account.
- **A hung directory opens the breaker only when a canary also stalls.** If the
  directory accepts a connection but never answers the service bind, the
  breaker (`auth.ldap_breaker_seconds`) opens only when a fresh connection, a
  service bind and a base-scope search of the configured search base also stall.
  A crafted login name cannot open or hold it.
- **Locked accounts are revoked.** An account whose `auth.ldap_lock_attribute`
  (default `nsAccountLock`; empty turns the check off) is `TRUE` cannot sign in,
  and its live session is revoked at the next directory re-check. The bind
  account must be able to read the attribute (on FreeIPA, confirm it can read
  `nsAccountLock`); an attribute it cannot read is ignored.

### RTSP capture settings

RTSP grabs have three new settings (see
[capture](./configuration.md#capture)): `capture.rtsp_timeout_seconds` (20, at
most 45; a grab's whole budget is this plus 5 s, and replaces
`capture.timeout_seconds` for an RTSP camera when larger),
`capture.rtsp_jpeg_quality` (2) and `capture.rtsp_wait_for_keyframe` (false).
`rtsps://` streams now work. Interval cadence is a monotonic grid anchored at
the runner's start; see [Scheduling](./scheduling.md#schedule-driven-timing).

### Camera certificate pinning

An administrator can pin a saved camera to one exact TLS certificate from the
camera edit form. It is opt-in, per camera, and never automatic. A pinned camera
trusts only that certificate (neither `capture.tls_ca_bundle` nor
`SSL_CERT_FILE` can vouch for another), is reached over `https://` only and
cannot use RTSP. Unpinned cameras behave as before. See
[Web UI](./web-ui.md#certificate-pin).

A new pin, a re-pin or an unpin takes effect without a restart: reconcile
closes any pinned client whose pin no camera holds, after twice the client
timeout so requests in flight finish. **Query** on the camera edit form and
**Detect** in the API (`POST /api/v1/cameras/detect-protocol`, with the optional
`camera_id` of an existing camera) use that camera's pin as their only trust; an
unknown `camera_id` is ignored and the probe uses the shared trust.

### CLI config file lookup

When neither `--config` nor `TLM_CONFIG` names a file, the CLI (`timelapse-manager`
in a package install, `timelapse` in the Docker image) picks its config file in
this order. The first that exists and is readable by the calling account wins:

1. `--config PATH`
2. `$TLM_CONFIG`
3. `/etc/timelapse-manager/config.yml`
4. `$XDG_CONFIG_HOME/timelapse-manager/config.yml` (`~/.config/timelapse-manager/config.yml`
   when `XDG_CONFIG_HOME` is unset; a relative `XDG_CONFIG_HOME` is ignored)

A root-only system file is skipped for an ordinary user, who falls through to
the per-user file. This covers every CLI command, `run` included. The packaged
service is unchanged, because its unit sets `TLM_CONFIG`. `TLM_*` environment
variables still override file values. The "token file missing" error (exit 2)
now names the config file in use, or says none was found and how to supply one.

The unit also sets `TLM_PATHS__DATA_DIR` and `TLM_DATABASE__URL`, and a CLI run
by hand does not. A config file that does not set `paths.data_dir` leaves the
CLI looking in the calling account's default data directory
(`~/.local/share/timelapse-manager` on Linux), not the service's. Keep
`paths.data_dir` in `config.yml`, as the example in
[Configuration and ports](#configuration-and-ports) does, or export both
variables. `sudo` drops the environment; use `sudo -E` or pass them explicitly.

### Upgrade restart check (packages)

The `.deb` and `.rpm` upgrade script (`packaging/scripts/postinstall.sh`)
restarts the service after migrating and then watches it. `install.sh` waits and
reports the same way after it starts the service; the Docker image does not.

- **The script waits up to 15 s for the service to answer, and stops as soon as
  it does.** It polls up to 15 times, 1 s apart (`TLM_UPGRADE_HEALTH_POLLS` x
  `TLM_UPGRADE_HEALTH_INTERVAL`, from the environment of the package manager
  run), asking `/healthz` on the configured HTTP bind address and port
  (`server.bind_address` and `server.http_port`, read from the installed env
  file and `config.yml`; a wildcard bind is probed on `127.0.0.1` or `::1`). Any
  HTTP answer counts, including a redirect to HTTPS, a 503 while the service is
  not ready and a 400 from the host allowlist, because it proves the process is up. `active` alone
  does not end the wait, since the unit is `Type=simple` and reads as active
  before the application has started; `failed` ends it at once. **This needs
  `curl`.** Without it the script cannot see an answer, waits out the whole
  window (unless the unit fails) and then accepts a unit that is `active`.
- **Exit status 78.** A unit that ends `failed` with `ExecMainStatus=78` (the
  encryption key does not match the stored secrets) gets a boxed message on
  stderr with the recovery steps: `secrets status`, restore the original key
  file, or `secrets reset --confirm` and then `systemctl start`. It warns that
  `reset` also clears the LDAP bind password, so a local break-glass
  administrator must exist. See [Encryption key integrity](#encryption-key-integrity).
- **Any other result that is not active** (a crash loop, a slow start) gets a
  one-line message with the unit's state and last exit status, and a pointer to
  `journalctl -u timelapse-manager`. `install.sh` prints its own one-line
  warning in that case, and a hint to run `secrets status` for exit status 78.
- The script still exits 0 after either message: the files are in place whatever
  the service does. A failed migration is reported as before and the service is
  not restarted.

### Project storage paths and clones

A cloned project no longer copies the source's `storage_path`. It gets none and
uses the default layout, its own directory under the frames root, so it can
never write into, or delete from, its source's directory. The storage path rules
also refuse a path whose last component equals `render.output_subdir`, a
numbered direct child of the frames root other than the project's own id, and a
path another project already uses. Existing rows are never rewritten. At startup,
projects that share a path are logged as a WARNING (`storage_path policy: ...`)
along with the other paths the rules now refuse. See
[Storage](./storage.md#per-project-storage-override).

### Other behaviour changes

- A no-JavaScript frame import form must send `csrf_token` first (the built-in
  form does). Bearer-token API clients are unaffected.
- Uploaded files are spooled under `<data_dir>/tmp/` and removed afterwards; an
  import is refused (507) when free space would fall below
  `storage.low_watermark_bytes`.
- Sessions revoked or expired more than 7 days ago are purged hourly.
- **Unsafe `/api/v1` calls with no credentials are refused before their body is
  read.** A request whose method is not GET, HEAD, OPTIONS or TRACE and that
  carries neither a bearer value nor a session cookie is answered at once with
  what the bearer check gives: 403 from a peer outside
  `server.api_token_allowed_clients`, otherwise 401 with `WWW-Authenticate`.
  `GET` requests are unaffected, and `/api/v1/metrics` and
  `/api/v1/system/debug` keep their own access control. A request with a wrong
  bearer, from a refused peer or with a dead session cookie is also refused
  before its body is read, with the 403, 401 or 503 the route would give; the
  route repeats the same checks, so the two never disagree. The `/api/v1` API is
  bearer-only: a valid bearer token is always required, and a session cookie
  alone is refused with 401. A request that carries a valid bearer and a session
  cookie is judged by the cookie's user and role as well, and must carry a CSRF
  token (`X-CSRF-Token`) like any cookie-authenticated unsafe request. The web UI
  does not use this API.
- **Overlay captions are limited to 1,000 characters.** `text_content` longer
  than that is rejected with 422 by the render API and by project
  `render_schedule` saves. A longer caption that is already stored is not
  changed and still renders.
- **An invalid stored overlay timestamp format warns once per project per
  process,** and only while the timestamp overlay is on.
- **The web route `POST /cameras/detect-protocol` is removed.** The UI never
  posted to it. The JSON API route `POST /api/v1/cameras/detect-protocol` stays.
  A script that posted to the web route must use the API.

---

## Signature and checksum verification

Every release is published as a GitHub Release with a `SHA256SUMS` manifest that
covers all of its assets. For the full step-by-step guide, see
[docs/verifying-releases.md](./verifying-releases.md).

Quick reference:

| File | Purpose |
|---|---|
| `timelapse-manager-<version>-linux-amd64.tar.gz` | The bundle tarball |
| `timelapse-manager_<pkg-version>-1_amd64.deb`, `timelapse-manager-<pkg-version>-1.x86_64.rpm` | Native packages |
| `debian-13-timelapse-manager_<version>_amd64.tar.zst` (+ `.sha512`) | Proxmox CT template |
| `timelapse-manager-rpm-signing-key.asc` | Public key for the RPM signature |
| `cyclonedx-bom.json`, `sbom-image.cdx.json`, `sbom-lxc.cdx.json` | CycloneDX SBOMs (application and bundled FFmpeg; image; CT) |
| `image-digest.txt` | The published Docker image digest |
| `SHA256SUMS` | SHA-256 manifest over every other file in the release |

```sh
base=https://github.com/drcoble/Timelapse_Manager/releases/download/v<version>
curl -fsSLO "$base/SHA256SUMS"
# download the assets you need, then:
sha256sum --check --ignore-missing SHA256SUMS
```

The RPM is also signed; see the verification guide for `rpm -K` and the signing
key's fingerprint.

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
