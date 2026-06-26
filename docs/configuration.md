# Configuration Reference

Timelapse Manager loads settings from up to four sources, resolved in this
order (highest precedence wins):

1. **Explicit flags** passed by the CLI or service launcher (`--config PATH`)
2. **Environment variables** prefixed with `TLM_`, using `__` to descend into
   nested sections (e.g. `TLM_SERVER__HTTP_PORT=9000`)
3. **Config file** — YAML (`.yaml` / `.yml`) or JSON (`.json`), loaded when
   `--config PATH` is given or `TLM_CONFIG` is set
4. **Built-in defaults** (documented below)

There is no default config file location. If neither `--config` nor `TLM_CONFIG`
is set, only defaults and environment variables are used. Invalid values fail
fast with a message naming the bad field path.

**List-valued settings via environment variables.** A setting that holds a list
(such as `ssrf.allowed_private_subnets`) accepts either a comma- or
whitespace-separated string *or* a JSON array when set through an environment
variable. Both of these are equivalent:

```
TLM_SSRF__ALLOWED_PRIVATE_SUBNETS=192.168.10.0/24, 192.168.1.0/24
TLM_SSRF__ALLOWED_PRIVATE_SUBNETS=["192.168.10.0/24", "192.168.1.0/24"]
```

A single value needs no brackets: `TLM_SSRF__ALLOWED_PRIVATE_SUBNETS=192.168.10.0/24`.
In a config file, use a normal YAML/JSON list.

---

## Config-file format

Both YAML and JSON are accepted; the file extension determines the parser.
The file must contain a top-level mapping. An empty file is valid and equivalent
to omitting the file.

### Example YAML config

```yaml
server:
  http_port: 8080
  https_port: 8443
  bind_address: "0.0.0.0"
  redirect_http_to_https: true

tls:
  cert_path: null     # path to PEM cert; null = auto-generate
  key_path: null      # path to PEM key; null = auto-generate
  auto_generate: true # generate self-signed cert on first start

session:
  cookie_name: tlm_session
  idle_timeout_seconds: 1800      # 30 minutes
  absolute_timeout_seconds: 86400     # 24 hours (non-persistent sessions)
  persistent_timeout_seconds: 2592000   # 30 days ("remember me" sessions)
  samesite: lax

auth:
  password_min_length: 12
  throttle_max_failures: 5
  throttle_window_seconds: 300
  argon2_memory_kib: 19456
  argon2_time_cost: 2
  argon2_parallelism: 1

database:
  url: "sqlite:///./data/timelapse.db"
  timeout: 30

logging:
  level: INFO
  format: json
  file_sink: null        # optional path, e.g. "./data/app.log"

paths:
  data_dir: "./data"
  frames_root: null      # defaults to data_dir/frames when unset
  token_file: null       # defaults to data_dir/.local-token when unset

capture:
  autostart: true
  timeout_seconds: 10.0
  default_interval_seconds: 60
  backoff_base_seconds: 1.0
  backoff_max_seconds: 300.0
  backoff_jitter_fraction: 0.1
  frozen_frame_enabled: true
  frozen_frame_threshold: 5
  max_idle_sleep_seconds: 300.0

storage:
  low_watermark_bytes: 1000000000    # 1 GB
  low_watermark_percent: 5.0
  resume_watermark_bytes: 2000000000 # 2 GB
  resume_watermark_percent: 10.0
  check_interval_seconds: 60.0

render:
  autostart: true
  max_concurrent: 1
  output_subdir: renders
  default_fps: 24.0
  webhook_timeout_seconds: 10.0
  font_path: null
  scheduler_check_interval_seconds: 60.0

monitoring:
  autostart: true
  poll_interval_seconds: 5.0
  max_retries: 3
  retry_backoff_seconds: 1.0
  debounce_window_seconds: 60.0
  channel_send_timeout_seconds: 10.0

cameras: []              # parsed but not yet provisioned to the database
projects: []             # parsed but not yet provisioned to the database
```

---

## Settings sections

### `server`

| Key | Default | Description |
|---|---|---|
| `http_port` | `8080` | Port for the HTTP listener |
| `https_port` | `8443` | Port for the HTTPS listener |
| `bind_address` | `"0.0.0.0"` | Network interface to bind |
| `redirect_http_to_https` | `true` | Redirect plain HTTP to HTTPS with a `308` |

The service binds both listeners from a single process. With
`redirect_http_to_https: true` (the default), the HTTP port bounces browser
traffic to HTTPS; the HTTP port is still bound to the full `bind_address` so
the redirect works. With `redirect_http_to_https: false`, the HTTP port is
confined to loopback (`127.0.0.1`) so no plaintext port is exposed to the
network. The CLI JSON API (`/api/v1/`) and `/healthz` are exempt from the
redirect regardless of this setting.

**Environment variable examples:**
```
TLM_SERVER__HTTP_PORT=9000
TLM_SERVER__HTTPS_PORT=8443
TLM_SERVER__BIND_ADDRESS=127.0.0.1
TLM_SERVER__REDIRECT_HTTP_TO_HTTPS=false
```

---

### `tls`

Controls how the built-in HTTPS listener obtains its TLS certificate and key.

| Key | Default | Description |
|---|---|---|
| `cert_path` | `null` | Path to a PEM certificate file. When set, `key_path` must also be set. |
| `key_path` | `null` | Path to the matching PEM private key file. |
| `auto_generate` | `true` | Generate a self-signed certificate on first start when no explicit pair is provided. The generated certificate is stored in `data_dir` and reused on subsequent starts. |

**Resolution order on startup:**

1. If both `cert_path` and `key_path` are set and the files exist, that pair
   is used as-is.
2. Otherwise, if `auto_generate` is `true`, a self-signed RSA-2048 certificate
   is generated into `data_dir/tls-cert.pem` and `data_dir/tls-key.pem`. The
   private key is written with owner-only permissions (`0600`).
3. Otherwise the service refuses to start with an error.

The auto-generated certificate is valid for `localhost`, `127.0.0.1`, and `::1`
only. Supply an explicit certificate for any other hostname or for
public-facing deployments.

**Environment variable examples:**
```
TLM_TLS__CERT_PATH=/etc/timelapse/tls/cert.pem
TLM_TLS__KEY_PATH=/etc/timelapse/tls/key.pem
TLM_TLS__AUTO_GENERATE=false
```

**Example config-file snippet:**
```yaml
tls:
  cert_path: "/etc/timelapse/tls/cert.pem"
  key_path: "/etc/timelapse/tls/key.pem"
  auto_generate: false
```

See [docs/web-ui.md](./web-ui.md) for the full TLS story, the reverse-proxy
setup, and the redirect/exemption behavior.

---

### `session`

Controls server-side login sessions and the browser cookie.

| Key | Default | Description |
|---|---|---|
| `cookie_name` | `"tlm_session"` | Name of the session cookie. |
| `idle_timeout_seconds` | `1800` | Inactivity ceiling (seconds) from last activity. A session that has not been touched for longer than this expires. Applies to all sessions, including persistent ones. |
| `absolute_timeout_seconds` | `86400` | Age ceiling (seconds) for a regular (non-persistent) session, measured from creation. A session that has been continuously active but reaches this age is still expired. |
| `persistent_timeout_seconds` | `2592000` | Age ceiling (seconds) for a "remember me" session, measured from creation. Used in place of `absolute_timeout_seconds` when the user checked "Remember me" at login. |
| `samesite` | `"lax"` | `SameSite` attribute on the session cookie. Accepted values: `lax`, `strict`, `none`. |

**Timeout relationship:** idle timeout applies first, independent of the
creation-anchored cap. A "remember me" session only lengthens the
creation-anchored cap; it is still subject to the idle timeout.

**Environment variable examples:**
```
TLM_SESSION__COOKIE_NAME=tlm_session
TLM_SESSION__IDLE_TIMEOUT_SECONDS=900
TLM_SESSION__ABSOLUTE_TIMEOUT_SECONDS=28800
TLM_SESSION__PERSISTENT_TIMEOUT_SECONDS=1296000
TLM_SESSION__SAMESITE=strict
```

**Example config-file snippet:**
```yaml
session:
  cookie_name: tlm_session
  idle_timeout_seconds: 1800     # 30 minutes
  absolute_timeout_seconds: 86400    # 24 hours
  persistent_timeout_seconds: 2592000  # 30 days
  samesite: lax
```

---

### `auth`

Controls password policy, brute-force throttling, and Argon2 hashing cost.

| Key | Default | Description |
|---|---|---|
| `password_min_length` | `12` | Minimum length required for a new password. |
| `throttle_max_failures` | `5` | Failed login attempts permitted from one source IP (or for one submitted username) within the sliding window before further attempts are throttled. |
| `throttle_window_seconds` | `300` | Sliding window (seconds) over which failures are counted. A successful login clears the counters for that IP and username. |
| `argon2_memory_kib` | `19456` | Argon2id memory cost in KiB. Raise on more capable hardware. |
| `argon2_time_cost` | `2` | Argon2id iteration count. |
| `argon2_parallelism` | `1` | Argon2id degree of parallelism (lanes). |

The Argon2 defaults are tuned to be acceptable on a small single-board computer
while still resisting offline attacks. If you raise the cost parameters after
deployment, existing password hashes are transparently re-hashed at the higher
cost on the user's next successful login.

**Environment variable examples:**
```
TLM_AUTH__PASSWORD_MIN_LENGTH=16
TLM_AUTH__THROTTLE_MAX_FAILURES=3
TLM_AUTH__THROTTLE_WINDOW_SECONDS=600
TLM_AUTH__ARGON2_MEMORY_KIB=65536
TLM_AUTH__ARGON2_TIME_COST=3
TLM_AUTH__ARGON2_PARALLELISM=2
```

**Example config-file snippet:**
```yaml
auth:
  password_min_length: 12
  throttle_max_failures: 5
  throttle_window_seconds: 300
  argon2_memory_kib: 19456
  argon2_time_cost: 2
  argon2_parallelism: 1
```

---

### `database`

| Key | Default | Description |
|---|---|---|
| `url` | `"sqlite:///./timelapse.db"` | SQLAlchemy database URL |
| `timeout` | `30` | Connection timeout in seconds |

**Environment variable examples:**
```
TLM_DATABASE__URL=sqlite:///./data/timelapse.db
TLM_DATABASE__TIMEOUT=60
```

---

### `logging`

| Key | Default | Choices | Description |
|---|---|---|---|
| `level` | `"INFO"` | `DEBUG` `INFO` `WARNING` `ERROR` | Minimum log level |
| `format` | `"json"` | `json` `text` | Log output format |
| `file_sink` | `null` | — | Path to write logs to a file; `null` = stderr only |

**Environment variable examples:**
```
TLM_LOGGING__LEVEL=DEBUG
TLM_LOGGING__FORMAT=text
TLM_LOGGING__FILE_SINK=./data/app.log
```

---

### `paths`

| Key | Default | Description |
|---|---|---|
| `data_dir` | `"./data"` | Base directory for runtime data |
| `frames_root` | `data_dir/frames` | Root directory for captured frames |
| `token_file` | `data_dir/.local-token` | Path to the local API bearer-token file |

`frames_root` and `token_file` are derived from `data_dir` when left unset.
Set them explicitly only when you need them at a different location.

**Environment variable examples:**
```
TLM_PATHS__DATA_DIR=/var/lib/timelapse
TLM_PATHS__FRAMES_ROOT=/mnt/nas/frames
```

---

### `capture`

Controls the behaviour of the background capture engine.

| Key | Default | Description |
|---|---|---|
| `autostart` | `true` | Start scheduled capture tasks when the service starts. The supervisor is always constructed so manual capture works; this only gates the background loops. |
| `timeout_seconds` | `10.0` | Per-frame ceiling (seconds) on a single capture attempt. A capture that exceeds this limit is skipped and logged as a gap; it does not block the loop or other projects. |
| `default_interval_seconds` | `60` | Fallback capture interval (seconds) for a project that does not specify its own interval. |
| `backoff_base_seconds` | `1.0` | First reconnect delay (seconds) after a transient capture failure. Subsequent failures double the delay. |
| `backoff_max_seconds` | `300.0` | Cap on reconnect delay so backoff never grows without bound. |
| `backoff_jitter_fraction` | `0.1` | Fraction of the computed delay to randomise (±fraction), so failing cameras do not retry in lockstep. |
| `frozen_frame_enabled` | `true` | Whether identical-frame ("frozen camera") detection runs after each successful capture. |
| `frozen_frame_threshold` | `5` | Number of consecutive identical frames that triggers a warning event. Capture continues regardless. |
| `max_idle_sleep_seconds` | `300.0` | Maximum seconds the capture loop sleeps between re-evaluations. Acts as a ceiling so closed schedule windows stay cancellable and config/clock drift is re-evaluated periodically. Does not cap the capture interval. |

**Environment variable examples:**
```
TLM_CAPTURE__AUTOSTART=false
TLM_CAPTURE__TIMEOUT_SECONDS=30.0
TLM_CAPTURE__DEFAULT_INTERVAL_SECONDS=300
TLM_CAPTURE__BACKOFF_BASE_SECONDS=2.0
TLM_CAPTURE__BACKOFF_MAX_SECONDS=120.0
TLM_CAPTURE__BACKOFF_JITTER_FRACTION=0.2
TLM_CAPTURE__FROZEN_FRAME_ENABLED=false
TLM_CAPTURE__FROZEN_FRAME_THRESHOLD=10
TLM_CAPTURE__MAX_IDLE_SLEEP_SECONDS=60.0
```

**Example config-file snippet:**
```yaml
capture:
  autostart: true
  timeout_seconds: 10.0
  default_interval_seconds: 60
  backoff_base_seconds: 1.0
  backoff_max_seconds: 300.0
  backoff_jitter_fraction: 0.1
  frozen_frame_enabled: true
  frozen_frame_threshold: 5
  max_idle_sleep_seconds: 300.0
```

See [docs/scheduling.md](./scheduling.md) for how these settings interact with
capture schedules and for a description of all reliability behaviors.

---

### `storage`

Controls the disk-space safeguard that pauses capture when a project's storage
volume runs low. Capture is paused when free space drops below the low
watermark and resumed only once it recovers above the (higher) resume
watermark; the gap between them is a hysteresis band. Nothing is ever deleted
to reclaim space — only new captures are held back.

**Pause logic (OR):** pauses when free space is below `low_watermark_bytes`
**or** below `low_watermark_percent` of total — whichever triggers first.

**Resume logic (AND):** resumes only when free space is above
**both** `resume_watermark_bytes` **and** `resume_watermark_percent`.

Each resume floor must be ≥ its corresponding low floor; an invalid combination
fails fast at startup.

| Key | Default | Description |
|---|---|---|
| `low_watermark_bytes` | `1000000000` (1 GB) | Pause when free bytes fall below this |
| `low_watermark_percent` | `5.0` | Pause when free percentage falls below this |
| `resume_watermark_bytes` | `2000000000` (2 GB) | Resume only once free bytes exceed this; must be ≥ `low_watermark_bytes` |
| `resume_watermark_percent` | `10.0` | Resume only once free percentage exceeds this; must be ≥ `low_watermark_percent` |
| `check_interval_seconds` | `60.0` | Minimum seconds between free-space probes per volume; the capture loop answers from a cached reading in between |

**Environment variable examples:**
```
TLM_STORAGE__LOW_WATERMARK_BYTES=500000000
TLM_STORAGE__LOW_WATERMARK_PERCENT=3.0
TLM_STORAGE__RESUME_WATERMARK_BYTES=1000000000
TLM_STORAGE__RESUME_WATERMARK_PERCENT=8.0
TLM_STORAGE__CHECK_INTERVAL_SECONDS=30.0
```

**Example config-file snippet:**
```yaml
storage:
  low_watermark_bytes: 1000000000
  low_watermark_percent: 5.0
  resume_watermark_bytes: 2000000000
  resume_watermark_percent: 10.0
  check_interval_seconds: 60.0
```

See [docs/storage.md](./storage.md) for how the safeguard works, how it
composes with the schedule gate, and storage sizing guidance.

---

### `render`

Controls the video render worker and scheduler.

The render worker drains a bounded queue of encode jobs so renders never starve
capture. The scheduler periodically enqueues recurring renders and archive
snapshots based on per-project cadences.

| Key | Default | Description |
|---|---|---|
| `autostart` | `true` | Whether the render worker and scheduler start when the service starts. Set to `false` in test environments to drive them manually. |
| `max_concurrent` | `1` | Maximum number of renders allowed to run at once. Renders are subprocess-bound; the cap bounds resource use and protects capture from being starved. |
| `output_subdir` | `"renders"` | Directory name, under a project's storage location, that produced video files are written to. Frames and renders are stored as siblings. |
| `default_fps` | `24.0` | Frame rate used when a render request does not specify `fps`. |
| `webhook_timeout_seconds` | `10.0` | Timeout (seconds) for a single post-render webhook HTTP POST (`external_trigger` action). |
| `font_path` | `null` | Path to a TrueType font file used by text and timestamp overlays. When unset, the encoder probes a platform default. Bundled deployments should set this explicitly. |
| `scheduler_check_interval_seconds` | `60.0` | How often (seconds) the render scheduler re-evaluates every project's render and archive cadence. |

**Environment variable examples:**
```
TLM_RENDER__AUTOSTART=false
TLM_RENDER__MAX_CONCURRENT=2
TLM_RENDER__OUTPUT_SUBDIR=renders
TLM_RENDER__DEFAULT_FPS=30.0
TLM_RENDER__WEBHOOK_TIMEOUT_SECONDS=15.0
TLM_RENDER__FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
TLM_RENDER__SCHEDULER_CHECK_INTERVAL_SECONDS=120.0
```

**Example config-file snippet:**
```yaml
render:
  autostart: true
  max_concurrent: 1
  output_subdir: renders
  default_fps: 24.0
  webhook_timeout_seconds: 10.0
  font_path: null          # probes a platform default when unset
  scheduler_check_interval_seconds: 60.0
```

See [docs/rendering.md](./rendering.md) for the full rendering pipeline,
overlay configuration, chapters, the render queue and scheduler, and post-
render actions.

---

### `monitoring`

Controls the notification dispatcher's poll cadence and per-delivery behavior.
This section governs only the dispatcher runtime; SMTP server settings, webhook
URLs, and routing rules are configured through the web UI at
`/notification-settings` (Admin only) and are stored in the database. See
[docs/monitoring.md](./monitoring.md) for the full notification system
reference.

| Key | Default | Description |
|---|---|---|
| `autostart` | `true` | Whether the dispatcher's poll loop starts when the service starts. Set to `false` in test environments to drive the dispatcher manually. |
| `poll_interval_seconds` | `5.0` | How often (seconds) the dispatcher polls the event log for new rows. |
| `max_retries` | `3` | Total delivery attempts per channel per event before giving up. The first attempt counts toward this limit. |
| `retry_backoff_seconds` | `1.0` | Base retry delay (seconds); grows exponentially with each attempt, plus jitter. |
| `debounce_window_seconds` | `60.0` | Per-channel suppression window (seconds). A notification for the same `(event_type, scope, scope_id)` key is suppressed if one was sent within this window. |
| `channel_send_timeout_seconds` | `10.0` | Hard ceiling (seconds) on a single channel send, enforced by the dispatcher so a hanging channel cannot block shutdown. |

**Environment variable examples:**
```
TLM_MONITORING__AUTOSTART=false
TLM_MONITORING__POLL_INTERVAL_SECONDS=10.0
TLM_MONITORING__MAX_RETRIES=5
TLM_MONITORING__RETRY_BACKOFF_SECONDS=2.0
TLM_MONITORING__DEBOUNCE_WINDOW_SECONDS=300.0
TLM_MONITORING__CHANNEL_SEND_TIMEOUT_SECONDS=15.0
```

**Example config-file snippet:**
```yaml
monitoring:
  autostart: true
  poll_interval_seconds: 5.0
  max_retries: 3
  retry_backoff_seconds: 1.0
  debounce_window_seconds: 60.0
  channel_send_timeout_seconds: 10.0
```

See [docs/monitoring.md](./monitoring.md) for delivery semantics (retry,
debounce, at-most-once, no replay on startup), channel setup, routing rules,
and the webhook payload shape.

---

### `ssrf`

Outbound-request guard for server-originated, user-influenced calls (camera-add
URL probes, ONVIF/range scans, and webhook delivery). Loopback, link-local
(including the cloud metadata address `169.254.169.254`), and other special-use
ranges are always blocked. Because cameras normally live on private LANs, an
admin may opt **specific** private subnets into the allowed set for the
camera/scan surfaces; the webhook surface always uses the full deny-list.

| Key | Default | Meaning |
| --- | --- | --- |
| `allowed_private_subnets` | `[]` | CIDR blocks opted into for camera-add and scan targets. Never relaxes loopback/link-local/metadata, and never applies to webhooks. |
| `max_scan_hosts` | `1024` | Hard cap on hosts a single range scan may probe. |

**Environment variable examples:**
```
# Single subnet (no brackets needed):
TLM_SSRF__ALLOWED_PRIVATE_SUBNETS=192.168.10.0/24
# Multiple subnets, comma- or whitespace-separated:
TLM_SSRF__ALLOWED_PRIVATE_SUBNETS=192.168.10.0/24, 192.168.1.0/24
# Or the equivalent JSON array:
TLM_SSRF__ALLOWED_PRIVATE_SUBNETS=["192.168.10.0/24", "192.168.1.0/24"]
TLM_SSRF__MAX_SCAN_HOSTS=512
```

**Example config-file snippet:**
```yaml
ssrf:
  allowed_private_subnets:
    - "192.168.10.0/24"
    - "192.168.1.0/24"
  max_scan_hosts: 1024
```

---

### `cameras`

A list of camera definitions. Each entry is a free-form mapping; the schema
is validated when cameras are provisioned. This section is parsed and available
to the application, but provisioning cameras to the database from the config
file is not yet implemented.

```yaml
cameras:
  - name: "rooftop"
    address: "192.168.1.50"
    protocol: onvif
```

---

### `projects`

A list of project definitions. Parsed from the config file, but provisioning
projects to the database from the config file is not yet implemented.

```yaml
projects:
  - name: "roof-2026"
    camera: "rooftop"
    capture_interval_seconds: 300
```

---

## Local API token

On first start, the service generates a high-entropy bearer token (64 hex
characters) and writes it to `paths.token_file` (default:
`./data/.local-token`). The file is created with owner-only permissions
(mode `0600`) where the platform supports it; the loopback-only binding is
the primary protection on platforms that do not honor POSIX file modes
(such as Windows).

The token persists across restarts. The CLI reads the same file automatically
when running `timelapse system info`.

To authenticate a manual API call:

```bash
TOKEN=$(cat ./data/.local-token)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/system
```
