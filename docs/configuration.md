# Configuration Reference

Timelapse Manager loads settings from up to four sources, resolved in this
order (highest precedence wins):

1. **Explicit flags** passed by the CLI or service launcher (`--config PATH`)
2. **Environment variables** prefixed with `TLM_`, using `__` to descend into
   nested sections (e.g. `TLM_SERVER__HTTP_PORT=9000`)
3. **Config file** — YAML (`.yaml` / `.yml`) or JSON (`.json`), loaded when
   `--config PATH` is given or `TLM_CONFIG` is set, or found by the CLI (below)
4. **Built-in defaults** (documented below)

**Where the config file comes from.** The service reads a file only when
`--config` or `TLM_CONFIG` names one (the packaged unit sets `TLM_CONFIG`). The
CLI, `run` included, looks further when neither is set. In order, the first file
that exists and that the calling account can read is used:

1. `--config PATH`
2. `$TLM_CONFIG`
3. `/etc/timelapse-manager/config.yml`
4. `$XDG_CONFIG_HOME/timelapse-manager/config.yml`, or
   `~/.config/timelapse-manager/config.yml` when `XDG_CONFIG_HOME` is unset (a
   relative `XDG_CONFIG_HOME` is ignored)

If none exists, only defaults and environment variables are used. The unit also
sets `TLM_PATHS__DATA_DIR` and `TLM_DATABASE__URL`, which a CLI run by hand does
not get; keep `paths.data_dir` in the config file, or export both. See
[Installation](./installation.md#cli-config-file-lookup). Invalid values fail
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
  ldap_breaker_seconds: 60
  ldap_lock_attribute: nsAccountLock   # empty disables

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
  ws_read_timeout_seconds: 60.0
  ws_ping_interval_seconds: 30.0
  onvif_watchdog_seconds: 300.0
  max_image_bytes: 67108864       # 64 MiB
  max_ws_frame_bytes: 2097152     # 2 MiB
  rtsp_timeout_seconds: 20.0
  rtsp_jpeg_quality: 2
  rtsp_wait_for_keyframe: false

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

observability:
  metrics_enabled: false          # admin-only Prometheus /api/v1/metrics
  debug_endpoints_enabled: false  # admin-only /api/v1/system/debug/...

reporting:                # "Report a problem"; see docs/operations/problem-reports.md
  enabled: true           # local save is always on while this is true
  email_to: []
  webhook_url: null
  gitea:
    base_url: null
  github:                 # public GitHub hand-off; anonymizes every report
    enabled: true
    repo: "drcoble/Timelapse_Manager"
    labels: []
    repo: null
    token_file: null
    labels: []

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
| `limit_concurrency` | `256` | Most simultaneous connections the server accepts before shedding load; bounds a slow-connection flood. |
| `timeout_keep_alive_seconds` | `5` | Seconds an idle keep-alive connection is held open before the server closes it. |
| `limit_max_requests` | `null` | Recycle a worker after this many requests (`null` = never), bounding any single connection pool's lifetime. |
| `h11_max_incomplete_event_size` | `16384` | Byte ceiling on an incomplete HTTP request (line + headers that never finish), so a peer cannot dribble an unbounded header block to pin a connection. |
| `trusted_proxies` | `[]` | CIDR blocks/addresses of reverse proxies in front of the service. Uvicorn's own proxy-header handling is off, so a reverse proxy on the same host must be listed here. `X-Forwarded-For`/`X-Forwarded-Proto` are honoured only when the peer is in this set; client address/scheme then come from the left-most non-proxy hop. Empty = directly exposed (forwarded headers ignored). If a reverse proxy is missing here, every client it forwards shares the proxy's login-throttle budget, so one client's failed logins block everyone. The service logs a warning the first time such a proxy sends `X-Forwarded-For`. Accepts a comma/space-separated string or JSON array via env var. |
| `api_token_allowed_clients` | `["127.0.0.1/32", "::1/128"]` | Addresses/CIDRs from which the local API bearer token is accepted. Loopback callers may use plain HTTP; any other listed address must use HTTPS (directly, or via a proxy in `trusted_proxies`). Other peers get `403` before the token is examined. Accepts a comma-separated string or JSON array via env var. See [Local API token](#local-api-token). |
| `max_request_body_bytes` | `536870912` | Hard ceiling (512 MiB) on a request body, enforced as it streams regardless of `Content-Length`; an oversize request gets a `413` before being buffered. It is the frame-import limit and the cap on every per-route limit. |
| `max_default_body_bytes` | `16777216` | Body ceiling (16 MiB) for requests no per-route rule covers (e.g. JSON API bodies). Fixed per-route caps: `/login`, `/first-run`, `/logout` 64 KiB; other urlencoded forms 1 MiB; frame upload `capture.max_image_bytes` + 64 KiB; frame import `max_request_body_bytes`; `/report` its upload cap. A declared `Content-Length` over the cap is refused with `413` before reading. Never above `max_request_body_bytes`. |
| `allowed_hosts` | `["*"]` | Host names the service answers (`Host` header, port ignored, case-insensitive). `*` = any host, no check. A list answers `400` to any other host; `127.0.0.1`, `localhost` and `::1` are always allowed. Entries: host names, IP literals, or `*.example.com` (subdomains only; list `example.com` separately). Quote `"*"` in YAML. Accepts a comma-separated string or JSON array via env var. A malformed entry fails startup. |
| `shutdown_grace_seconds` | `10` | On a stop, how long each listener waits for open connections (downloads, video streams) to finish before cancelling them. Both listeners drain at the same time, then the lifespan teardown runs (render cancel, capture stop). A render the stop interrupts is requeued once if the stop finishes within this time; after the grace, a second interruption or a crash it fails. Keep it well under the unit's `TimeoutStopSec` (45 s). Minimum `1`. Env: `TLM_SERVER__SHUTDOWN_GRACE_SECONDS`. |

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
TLM_SERVER__SHUTDOWN_GRACE_SECONDS=10
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
| `cookie_name` | `"tlm_session"` | Name of the session cookie. Over HTTPS it is issued as `__Host-<cookie_name>` (see `host_prefix`). |
| `idle_timeout_seconds` | `1800` | Inactivity ceiling (seconds) from last activity. A session that has not been touched for longer than this expires. Applies to all sessions, including persistent ones. |
| `absolute_timeout_seconds` | `86400` | Age ceiling (seconds) for a regular (non-persistent) session, measured from creation. A session that has been continuously active but reaches this age is still expired. |
| `persistent_timeout_seconds` | `2592000` | Age ceiling (seconds) for a "remember me" session, measured from creation. Used in place of `absolute_timeout_seconds` when the user checked "Remember me" at login. |
| `samesite` | `"lax"` | `SameSite` attribute on the session cookie. Accepted values: `lax`, `strict`, `none`. |
| `keepalive_on_poll` | `false` | Whether background polls (alerts badge, dashboard and renders refresh, new-frames and new-events pills) keep the session alive. Off: a page left open with no input signs out after `idle_timeout_seconds`. A poll within one touch interval of real input always counts. The time ribbons a dashboard refresh loads are passive too, so an open dashboard signs out when idle. Turn on for an unattended wall display. |
| `host_prefix` | `true` | Issue the session cookie as `__Host-<cookie_name>` (`__Host-tlm_session`) whenever the request is effectively HTTPS: TLS on the connection, or `X-Forwarded-Proto` from a `server.trusted_proxies` peer. The cookie is then `Secure`, `Path=/` and has no `Domain`, so a sibling subdomain cannot set or overwrite it. Over plain HTTP the unprefixed name is used, because a browser rejects a `__Host-` cookie without `Secure`. `false` keeps the unprefixed name everywhere. Over HTTPS the unprefixed name is never read. |

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
TLM_SESSION__HOST_PREFIX=true
TLM_SESSION__ACCEPT_LEGACY_COOKIE=false
```

**Example config-file snippet:**
```yaml
session:
  cookie_name: tlm_session
  idle_timeout_seconds: 1800     # 30 minutes
  absolute_timeout_seconds: 86400    # 24 hours
  persistent_timeout_seconds: 2592000  # 30 days
  samesite: lax
  # Whether background polls (alerts badge, dashboard/renders refresh,
  # new-frames/new-events pills) keep the session alive. Off by default:
  # a page left open with no input signs out after idle_timeout_seconds.
  # A poll within one touch interval of real input always counts.
  # Turn on for an unattended wall display.
  # keepalive_on_poll: false
  # Over HTTPS the cookie is named __Host-<cookie_name>, and only that name is
  # read:
  # host_prefix: true
```

---

### `auth`

Controls password policy, brute-force throttling, and Argon2 hashing cost.

| Key | Default | Description |
|---|---|---|
| `password_min_length` | `12` | Minimum length required for a new password. |
| `throttle_max_failures` | `5` | Failed login attempts permitted from one source IP (or for one submitted username) within the sliding window before further attempts are throttled. |
| `throttle_window_seconds` | `300` | Sliding window (seconds) over which failures are counted. A successful login clears the counters for that IP and username. |
| `throttle_global_max_failures` | `100` | Failed logins from all sources within `throttle_window_seconds` at or above which every login is delayed by at least 1 s. |
| `local_backoff_after_failures` | `3` | Failed logins for one username (any source) within the window after which the next attempt for it waits 1 s, doubling per further failure, at most 30 s. It applies to every name alike (local, directory or unknown) and never refuses a correct password. |
| `ldap_breaker_seconds` | `60` | Minimum 1. How long the directory circuit breaker stays open after no directory server could be reached. While it is open a directory login answers "directory unavailable" at once and the periodic re-check of a live directory session is skipped (the session is kept), instead of every request waiting out the connect timeout. After the window one request probes the directory; any other result closes the breaker, so directory logins can still answer "unavailable" for up to this long after recovery. It opens when no server accepts a connection, and also when a server accepts but never answers the service-account bind; a stall after that bind (user search, user bind, group search), a protocol error, a rejected bind or a configuration fault never opens it. Saving the LDAP settings, or a successful connection test, resets it. Local break-glass logins are unaffected. |
| `ldap_lock_attribute` | `"nsAccountLock"` | Directory attribute that marks an account as locked. When its value is `TRUE` (any case) the account cannot sign in through the directory and a live directory session is revoked at its next re-check. It is an operational attribute, so the bind account must be allowed to read it; an attribute it cannot read is ignored. Empty disables the check. |
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
TLM_AUTH__LDAP_BREAKER_SECONDS=30
TLM_AUTH__LDAP_LOCK_ATTRIBUTE=nsAccountLock
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
  ldap_breaker_seconds: 60
  ldap_lock_attribute: nsAccountLock   # empty disables
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
| `ring_buffer_records` | `2000` | `0`–`100000` | Recent log records kept in memory for problem reports; `0` disables the buffer |

`level` is fixed at startup. To raise one logger to `DEBUG` temporarily
without a restart, use `POST /api/v1/system/debug/log-level`. It requires
`observability.debug_endpoints_enabled`; see
[docs/operations/debugging.md](./operations/debugging.md).

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
| `ws_read_timeout_seconds` | `60.0` | Silence budget (seconds) for a camera event WebSocket. A half-open or wedged socket that delivers no frame within this window is treated as dead and dropped, and the listener reconnects. A keepalive ping is sent partway through (see `ws_ping_interval_seconds`); any received frame resets the timer, so a quiet but live camera is never disconnected. |
| `ws_ping_interval_seconds` | `30.0` | How long the event WebSocket may sit silent before a keepalive ping is sent. Must be strictly less than `ws_read_timeout_seconds` (startup fails otherwise), so a post-ping window remains. |
| `onvif_watchdog_seconds` | `300.0` | Ceiling (seconds) on a single ONVIF PullPoint pull cycle (the long-poll plus any subscription renew). A half-open or wedged camera can stall a cycle past the per-request read timeout, hanging the event listener so it never reaches the re-subscribe backoff; a cycle that does not complete within this budget is treated as a dead subscription and the listener re-subscribes. Set comfortably above the long-poll hold so a quiet but live camera is never dropped mid-cycle. |
| `max_image_bytes` | `67108864` | Hard ceiling (bytes, default 64 MiB) on a single snapshot/HTTP-JPEG response body. Camera responses are untrusted; a capture whose body exceeds this is aborted and logged rather than buffered into memory. |
| `max_ws_frame_bytes` | `2097152` | Hard ceiling (bytes, default 2 MiB) on a single camera event WebSocket frame. Event metadata frames are small; a frame declaring a larger length is rejected before its payload is read. |
| `max_image_dimension_px` | `16000` | Hard ceiling on either pixel dimension of a captured or imported image. A rogue camera can report an enormous width/height whose decode would allocate gigabytes; a frame exceeding this on either axis is rejected before any pixel buffer is allocated. |
| `executor_workers` | unset | Threads in the dedicated capture pool (named tlm-capture) that runs every database and disk step of a capture, the event listeners' camera loads and manual captures. Unset means min(32, CPU count + 4): 6 on 2 vCPUs, 12 on 8. The database connection pool grows by the same number. Must be >= 1. Env: `TLM_CAPTURE__EXECUTOR_WORKERS`. |
| `offline_failure_threshold` | `3` | Consecutive failed capture attempts of one project that mark its camera offline (one alert per camera, whichever projects use it). |
| `http_max_connections` | `24` | Most connections the capture client opens at once. Covers 10 cameras with two concurrent requests each plus headroom; raise for a much larger fleet. |
| `http_keepalive_connections` | `12` | Idle connections kept for reuse (capped per client by its own maximum). Must not exceed `http_max_connections`. |
| `http_keepalive_expiry_seconds` | `30.0` | How long an idle camera connection is kept. |
| `event_http_max_connections` | `16` | Most connections the event-listener client opens (one long-poll per listener plus slack). |
| `probe_http_max_connections` | `12` | Most connections operator probes (Detect, query, discovery enrichment, validation, enumeration) open. |
| `tls_ca_bundle` | unset | PEM CA bundle that camera TLS connections (HTTP and the VAPIX event WebSocket) verify against. Unset: `SSL_CERT_FILE`, then `SSL_CERT_DIR`, then the bundled certifi store, as before; those variables keep working unchanged. Must be a readable file, or startup fails. Env: `TLM_CAPTURE__TLS_CA_BUNDLE`. Precedence per camera: the camera's own certificate pin (administrator-set, see [Web UI](./web-ui.md#certificate-pin)) is the only trust anchor and neither this bundle nor `SSL_CERT_FILE` can vouch for another certificate; otherwise this bundle; otherwise `SSL_CERT_FILE` / `SSL_CERT_DIR`, then certifi. |
| `rtsp_timeout_seconds` | `20.0` | Greater than 0, at most 45. ffmpeg socket I/O timeout for an RTSP grab. The grab's whole budget is this plus 5 s, and replaces `timeout_seconds` for an RTSP camera (and the ONVIF RTSP fallback) when larger. Env: `TLM_CAPTURE__RTSP_TIMEOUT_SECONDS`. |
| `rtsp_jpeg_quality` | `2` | 1-31. ffmpeg `-q:v` for the JPEG grabbed from an RTSP stream; lower is better quality. Env: `TLM_CAPTURE__RTSP_JPEG_QUALITY`. |
| `rtsp_wait_for_keyframe` | `false` | Skip non-key frames before decoding an RTSP grab, so a half-decoded P-frame is never returned. Can lengthen a grab by up to one GOP. Env: `TLM_CAPTURE__RTSP_WAIT_FOR_KEYFRAME`. |

Captures, event listeners and operator probes each use their own connection
pool, so a probe burst or event long-polls cannot make a scheduled capture wait
for a connection. A wait for a free connection is bounded at half of
`timeout_seconds`; when it runs out, the capture fails with a local "connection
pool exhausted" error that does not count toward the camera's offline alert.

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
TLM_CAPTURE__WS_READ_TIMEOUT_SECONDS=90.0
TLM_CAPTURE__WS_PING_INTERVAL_SECONDS=45.0
TLM_CAPTURE__MAX_IMAGE_BYTES=33554432
TLM_CAPTURE__MAX_WS_FRAME_BYTES=1048576
TLM_CAPTURE__TLS_CA_BUNDLE=/etc/timelapse-manager/ca-bundle.pem
TLM_CAPTURE__EXECUTOR_WORKERS=8
TLM_CAPTURE__RTSP_TIMEOUT_SECONDS=30.0
TLM_CAPTURE__RTSP_JPEG_QUALITY=2
TLM_CAPTURE__RTSP_WAIT_FOR_KEYFRAME=true
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
  rtsp_timeout_seconds: 20.0    # RTSP grab budget is this + 5 s
  rtsp_jpeg_quality: 2
  rtsp_wait_for_keyframe: false
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
| `allowed_roots` | unset (= `[paths.frames_root]`) | Absolute directories an operator may put a project's custom storage path in; the path and its parent must both be inside one of them. `[]` means only administrators can set a custom path. Env: `TLM_STORAGE__ALLOWED_ROOTS` (comma-separated or JSON array). |

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
  # allowed_roots: ["/srv/timelapse"]
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
| `stall_timeout_seconds` | `300` | Kill ffmpeg and fail the render when neither its frame count nor its output time has advanced for this many seconds. Must be > 0. |
| `max_job_seconds` | `0` | Wall-clock ceiling for one render, after which ffmpeg is killed and the job failed. `0` derives it: `max(3600, 2 x frame count)` seconds. |
| `jobs_executor_workers` | `2` | Threads in the dedicated pool for long render-side jobs: render preparation, export zips, post-render copies and prunes, and the codec backfill scan. At most this many run at once; later ones wait. The database connection pool grows by the same number. Must be >= 1. |
| `thumbnail_concurrency` | `2` | Most frame thumbnails generated (ffmpeg runs) at once. A request that waits more than 2 s for a slot is served the full-size frame, uncached, and the browser asks for the thumbnail again later. 1-64. Env: `TLM_RENDER__THUMBNAIL_CONCURRENCY`. |
| `max_concurrent_exports` | `1` | 1-16. Frame-export zips built at once. Exports have a slot of their own, separate from `max_concurrent`, so an export never holds a render's slot and a long export cannot delay a render. Env: `TLM_RENDER__MAX_CONCURRENT_EXPORTS`. |
| `export_ttl_days` | `7` | Days a finished frame export is kept. The hourly housekeeping then deletes its zip and its row (one info event names the TTL). `0` keeps exports until they are deleted by hand. Env: `TLM_RENDER__EXPORT_TTL_DAYS`. |

**Environment variable examples:**
```
TLM_RENDER__AUTOSTART=false
TLM_RENDER__MAX_CONCURRENT=2
TLM_RENDER__OUTPUT_SUBDIR=renders
TLM_RENDER__DEFAULT_FPS=30.0
TLM_RENDER__WEBHOOK_TIMEOUT_SECONDS=15.0
TLM_RENDER__FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
TLM_RENDER__SCHEDULER_CHECK_INTERVAL_SECONDS=120.0
TLM_RENDER__STALL_TIMEOUT_SECONDS=300
TLM_RENDER__MAX_JOB_SECONDS=0
TLM_RENDER__JOBS_EXECUTOR_WORKERS=2
TLM_RENDER__THUMBNAIL_CONCURRENCY=2
TLM_RENDER__MAX_CONCURRENT_EXPORTS=1
TLM_RENDER__EXPORT_TTL_DAYS=7
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
  stall_timeout_seconds: 300
  max_job_seconds: 0       # 0 = max(3600, 2 s per frame)
  jobs_executor_workers: 2
  thumbnail_concurrency: 2
  max_concurrent_exports: 1
  export_ttl_days: 7       # 0 keeps exports
```

The reason for a watchdog kill shows in the job's `render_failed` event and in
the service log (`killing ffmpeg pid=...: <reason> (last stderr: ...)`).

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
| `catchup_max_age_seconds` | `3600` | On startup, undelivered events younger than this (seconds, 0 or more) are delivered; older ones are skipped and counted in one log line. |
| `max_concurrent_sends` | `4` | Most notification sends in flight at once (1 or more). |
| `event_retention.info_days` | `0` | Delete `debug` and `info` events older than this many days. `0` keeps them forever (the default). |
| `event_retention.cleared_alert_days` | `0` | Delete alerts that were cleared more than this many days ago. `0` keeps them forever (the default). |
| `event_retention.batch_size` | `5000` | Rows deleted per transaction during the hourly pass (1-100000). One pass runs at most 20 batches; any remainder is deleted on later passes. |

Event retention is off by default. When enabled, it runs in the hourly
maintenance pass. It never deletes audit or security events
(`audit.control_action`, `security.auth_event`), uncleared alerts, or the newest
event.

**Environment variable examples:**
```
TLM_MONITORING__AUTOSTART=false
TLM_MONITORING__POLL_INTERVAL_SECONDS=10.0
TLM_MONITORING__MAX_RETRIES=5
TLM_MONITORING__RETRY_BACKOFF_SECONDS=2.0
TLM_MONITORING__DEBOUNCE_WINDOW_SECONDS=300.0
TLM_MONITORING__CHANNEL_SEND_TIMEOUT_SECONDS=15.0
TLM_MONITORING__EVENT_RETENTION__INFO_DAYS=365
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
  event_retention:
    info_days: 365
    cleared_alert_days: 365
```

See [docs/monitoring.md](./monitoring.md) for delivery semantics (retry,
debounce, at-least-once delivery, catch-up on startup), channel setup, routing rules,
and the webhook payload shape.

---

### `observability`

Operational surfaces for monitoring and debugging a running process. Both are
**off by default**. While off, each route answers `404` as though it did not
exist; when on, each requires an administrator, either a logged-in admin
session or the local API token (`Authorization: Bearer $(cat .local-token)`);
the metrics route also accepts the read-only metrics token (see below).
Neither route ever returns credentials or configuration secrets.

| Key | Default | Description |
|---|---|---|
| `metrics_enabled` | `false` | Serve the Prometheus exposition at `GET /api/v1/metrics`. It covers database counters and the stall-detection gauges: `tlm_runner_last_cycle_age_seconds`, `tlm_runner_state`, `tlm_executor_queue_depth`, `tlm_executor_threads`, `tlm_event_loop_lag_seconds`, `tlm_health_status`, `tlm_render_orphans`, `tlm_render_oldest_encoding_age_seconds` and `tlm_dispatcher_last_poll_age_seconds`. |
| `metrics_token_file` | `data_dir/.metrics-token` | File holding a read-only token that `/api/v1/metrics` accepts, for scrapers. Created by the service (mode `0600`) while `metrics_enabled` is set. It grants nothing else. See [Local API token](#local-api-token). |
| `metrics_allowed_clients` | `["127.0.0.1/32", "::1/128"]` | Addresses/CIDRs from which the metrics token is accepted (plain HTTP is allowed). Add the scraper's host to use it remotely. The metrics token from these addresses also unlocks the full `/healthz` body. |
| `debug_endpoints_enabled` | `false` | Serve the in-process debugging routes: `GET /api/v1/system/debug/tasks`, `/threads` and `/runners`, and `POST /api/v1/system/debug/log-level`. Read responses are cached for 1 s. |

**Environment variable examples:**
```
TLM_OBSERVABILITY__METRICS_ENABLED=true
TLM_OBSERVABILITY__DEBUG_ENDPOINTS_ENABLED=true
```

The `SIGUSR1` thread dump and the `SIGUSR2` asyncio task dump are always on and
need no configuration. See
[docs/operations/debugging.md](./operations/debugging.md) for the runbook.

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
| `allowed_private_subnets` | `[]` | CIDR blocks opted into for camera-add and scan targets, and for the private addresses camera connections may dial (re-read on each new connection). Never relaxes loopback/link-local/metadata, and never applies to webhooks. **Also the only place the saved default camera login is sent:** an inheriting camera gets it only when its address, snapshot URI and stream URI all resolve inside these subnets, so with the default `[]` no camera gets the default login. A camera's own credentials are not affected. |
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

### `reporting`

The user-initiated "Report a problem" feature. Every report is saved locally
under `<data_dir>/reports/` and listed for admins under **Settings → Problem
reports**; each other destination is optional and independent. See
[docs/operations/problem-reports.md](./operations/problem-reports.md) for the
full how-to and the snapshot contents.

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Show the "Report a problem" link and accept reports. While on, every report is stored locally. Off hides the link and answers `404`. |
| `email_to` | `[]` | Recipients of an email copy, sent through the SMTP server configured under notification settings. Comma/space-separated via env var. |
| `email_attach_max_bytes` | `5242880` | A zip larger than this is linked instead of attached. |
| `webhook_url` | `null` | Receives a JSON summary (no snapshot content), signed with the notification webhook signing secret when one is set. Passes the same SSRF guard as the notification webhooks. |
| `public_base_url` | `null` | External URL of this app (e.g. `https://timelapse.example.com`), used for download links in the email and webhook. |
| `gitea.base_url` | `null` | Gitea root, e.g. `https://git.example.com` (must be `http`/`https`; issue links are built from it). Trusted config: not run through the SSRF deny-list, because a self-hosted Gitea is normally on a private address. |
| `gitea.repo` | `null` | `owner/name` of the repository issues are filed in. |
| `gitea.token_file` | `null` | File holding an issue-write API token, read at send time and never stored. Keep it `0600`, owned by the service account. |
| `gitea.labels` | `[]` | Label names applied to each issue; names the repository lacks are skipped. |
| `gitea.timeout_seconds` | `30.0` | Ceiling on each Gitea API call. |
| `github.enabled` | `true` | The public GitHub hand-off. While on, **every** report (web, API, CLI) is anonymized, the screenshot is left out, and the result screen offers a prefilled `github.com/<repo>/issues/new` link the user submits from their own account. A short or generic name that would reach the issue refuses the report. Off: the form offers an optional "Anonymize private data" box instead. The app never holds a GitHub credential. |
| `github.repo` | `"drcoble/Timelapse_Manager"` | `owner/name` of the public repository on github.com. |
| `github.labels` | `[]` | Labels to prefill. Leave empty for a public project: with any label, GitHub answers `404` to users who cannot label issues, and no `labels` parameter is sent while the list is empty. Names must not contain `,`. |
| `retention_days` | `90` | Reports older than this are pruned (hourly housekeeping). |
| `retention_max_reports` | `50` | Beyond the newest this many, the oldest *delivered* reports are pruned; pending or failed ones wait for the age limit. |
| `rate_limit_per_hour` | `5` | Reports one user may file per rolling hour (`429` past it). |
| `max_upload_bytes` | `10485760` | Ceiling on the optional screenshot (PNG/JPEG/WebP/GIF only). |
| `max_text_bytes` | `20000` | Ceiling on the description, in UTF-8 bytes. |
| `log_window_minutes` | `30` | Log lines this many minutes either side of the reported time are included. |
| `log_max_lines` | `2000` | Ceiling on log lines in one report. |
| `delivery_timeout_seconds` | `30.0` | Ceiling on one email or webhook delivery. |

Gitea delivery is active only when `base_url`, `repo` and `token_file` are all
set. Anonymizing, the GitHub hand-off and short-name refusals are described in
[problem-reports.md](./operations/problem-reports.md#anonymized-reports-and-the-github-hand-off).

```yaml
reporting:
  public_base_url: "https://timelapse.example.com"
  gitea:
    base_url: "https://git.example.com"
    repo: "owner/timelapse-manager"
    token_file: "/etc/timelapse-manager/report-gitea-token"
    labels: ["bug"]
```

**Environment variable examples:**
```
TLM_REPORTING__EMAIL_TO=ops@example.com,dev@example.com
TLM_REPORTING__GITEA__REPO=owner/timelapse-manager
TLM_REPORTING__GITHUB__ENABLED=false
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

### `frames`

Limits on frame selections.

| Key | Default | Description |
|---|---|---|
| `max_range_ids` | `50000` | Minimum 1. The most frame ids a range selection may resolve to when the server materialises it into an explicit id list (`POST /frames/range/materialize`). A larger range is refused with 413 and "Range too large; narrow the selection". The same ceiling applies to an explicit `frame_ids` list posted to `POST /frames/export`: a longer one is refused with 413 and "Selection too large; narrow the selection." An export that names a descriptor (such as a whole project) is not capped. Env: `TLM_FRAMES__MAX_RANGE_IDS`. |

**Example config-file snippet:**
```yaml
frames:
  max_range_ids: 50000
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

**Who may use the token.** `server.api_token_allowed_clients` (list of
addresses/CIDRs; default `127.0.0.1/32`, `::1/128`; env
`TLM_SERVER__API_TOKEN_ALLOWED_CLIENTS="a,b"`).

- The CLI and other loopback callers may use plain
  `http://127.0.0.1:<http_port>`.
- Any other listed address must use HTTPS: directly on `https_port`, or through
  a proxy in `server.trusted_proxies` that sends `X-Forwarded-For` and
  `X-Forwarded-Proto: https`. The forwarded client address is the one checked.
- Requests from anywhere else get `403` before the token is examined.

**Reverse proxies.** The service ignores forwarded headers except from
`server.trusted_proxies`; Uvicorn's own proxy-header handling is off. A reverse
proxy on the same host must be listed in `trusted_proxies`. If it is not,
bearer calls relayed through it are refused, though the web UI keeps working.

**The token file.**

- The service creates `paths.token_file` (default `<data_dir>/.local-token`,
  mode 0600) at startup.
- Group or other permission bits are removed, with a warning. A file owned by
  another account is refused: API calls return `503` until the file is fixed.
- The CLI only reads the file. If the file is missing, start the service once;
  the CLI exits with status 2.

**Rotation.**

- `timelapse token rotate` replaces the token. The running service uses the new
  one from its next request, with no restart.
- Run it as the service account or as root; as root, the file keeps its owner.
- Deleting the file also revokes the token: the service writes a new one.

**Metrics token.**

- While `observability.metrics_enabled` is set, the service also creates a
  read-only token in `observability.metrics_token_file` (default
  `<data_dir>/.metrics-token`).
- `/api/v1/metrics` accepts it from `observability.metrics_allowed_clients`
  (default loopback). To let a Prometheus or OpenNMS scraper on another host
  use it, add that host.
- It is accepted over plain HTTP because it only reads metrics. Treat it as
  low-value but not public.
- It grants nothing else.
- Rotate it with `timelapse token rotate --metrics`.

To authenticate a manual API call:

```bash
TOKEN=$(cat ./data/.local-token)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/system
```
