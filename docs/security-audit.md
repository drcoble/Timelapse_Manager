# Security Hardening Audit
**Date:** 2026-06-10
**Status:** final

This document records which controls were verified by the automated abuse and
integration test suite (runnable in CI), which risks remain partially covered
or open, and which gates could not be exercised in the build environment.

---

## 1. Verified Controls

The following controls are proven by the abuse/integration test suite and are
runnable in CI.

### 1.1 SSRF Two-Tier Deny-List (`security/ssrf.py`)

**Always-denied addresses (unconditional, no opt-in):**

The `_ALWAYS_DENIED` tuple contains:
- `127.0.0.0/8` (IPv4 loopback)
- `::1/128` (IPv6 loopback)
- `169.254.0.0/16` (link-local / cloud metadata range)
- `fe80::/10` (IPv6 link-local)
- `0.0.0.0/8` (unspecified IPv4)
- `::/128` (unspecified IPv6)

Additionally blocked via `ip.is_multicast`, `ip.is_reserved`, `ip.is_loopback`,
`ip.is_link_local`, and `ip.is_unspecified` property checks (these catch
addresses not covered by the tuple entries above, e.g. IPv4-mapped forms).

**Private address opt-in (`_PRIVATE_RANGES`):**

- `10.0.0.0/8`
- `172.16.0.0/12`
- `192.168.0.0/16`
- `100.64.0.0/10` (CGNAT)
- `fc00::/7` (IPv6 ULA)

Private ranges are blocked by default (`ssrf.allowed_private_subnets` defaults
to `[]`). An admin may opt specific subnets in via config; loopback, link-local,
and metadata addresses are never relaxed regardless.

**IPv4-mapped IPv6 bypass prevention:**

`_normalise()` collapses IPv4-mapped IPv6 addresses (e.g. `::ffff:127.0.0.1`)
to their IPv4 form before the deny-list is evaluated. The abuse test suite
(`tests/abuse/test_ssrf_deny_list.py`) exercises these bypass attempts
explicitly and confirms they are blocked.

**`resolve_and_check()` resolves all returned addresses:**

`socket.getaddrinfo` returns every address a hostname resolves to; all are
checked. A host that resolves to any single denied address is blocked even if
other addresses would be allowed.

**DNS rebinding caveat (check-time only):**

The guard validates at check time. There is no connect-time pinning that
prevents a hostname from resolving differently between the guard check and the
actual TCP connection. This is documented below under Residual Risks.

**Covering tests:** `tests/abuse/test_ssrf_deny_list.py` — loopback (IPv4 +
IPv6), link-local, cloud metadata address (`169.254.169.254`), unspecified,
multicast, IPv4-mapped IPv6 bypass attempts, private opt-in paths, webhook
surface hard-blocks private ranges, malformed CIDR entries safely dropped.

**Config keys:**
- `ssrf.allowed_private_subnets` — list of CIDR strings (default: `[]`)
- `ssrf.max_scan_hosts` — maximum hosts a unicast scan may target (default: 1024)
- Environment: `TLM_SSRF__ALLOWED_PRIVATE_SUBNETS`, `TLM_SSRF__MAX_SCAN_HOSTS`

### 1.2 Scan Bounding

`scan_range()` in `cameras/discovery.py` expands the entered CIDR or range into
a host list, then raises `ValueError` when `len(hosts) > ssrf.max_scan_hosts`
before sending any probes. Every probe target is then individually validated
against the deny-list; denied addresses are skipped silently. The bound is
enforced in code, not only documented.

**Covering tests:** `tests/unit/test_discovery.py` — test asserts that a range
expanding beyond `max_scan_hosts` raises `ValueError` before any probe is sent.

### 1.3 Camera Snapshot URL — Re-validated Before Each Fetch

`cameras/http_jpeg.py` calls `assert_allowed_url()` immediately before every
HTTP GET, using the camera/scan policy (private opt-in honoured, loopback/
link-local/metadata never relaxed). This closes the window where a hostname
that resolves harmlessly at camera-add time later resolves to an internal
address.

The ONVIF adapter's snapshot path goes through `http_get_image()` and therefore
carries the same guard.

Redirects are not followed (`follow_redirects=False` on all outbound
`httpx.AsyncClient` instances), so a 30x cannot bounce a request to a denied
host after the guard passes.

**Covering tests:** `tests/abuse/test_ssrf_deny_list.py` confirms the hard
private block on the outbound path.

### 1.4 Webhook / Post-Render Actions — Full Deny-List, No Private Opt-In

`render/post_actions.py::validate_outbound_url()` routes every webhook target
through `assert_allowed_url(url, allow_private=False)`. No private subnet can
be reached by the webhook surface regardless of `ssrf.allowed_private_subnets`.

Webhook client: `httpx.AsyncClient(timeout=timeout, follow_redirects=False)`.

Supported post-render action types are `export`, `external_trigger`, and
`prune`. An unknown type (including any hypothetical `shell_command`) is a
no-op: `_dispatch_action()` logs it as unknown and takes no action.

The `export` post-render action is skipped when `running_under_docker()` returns
`True` (checked via `/.dockerenv` and `/proc/1/cgroup`), as a container has no
host export directory. The `external_trigger` (webhook) and `prune` actions have
no host-path dependency and run regardless; the webhook still routes through
`validate_outbound_url()` (deny-list, no private opt-in) and prune is still
confined to the project's render root.

### 1.5 Credential Encryption at Rest (`security/crypto.py`)

Fernet symmetric encryption is applied to all credential secret fields before
database persistence. Secret fields are identified by `_is_secret_field()`,
which matches keys containing "password", "secret", or "token" as a
case-insensitive substring. Usernames are not encrypted.

Ciphertext format: `enc:v1:<base64-fernet-token>`. Legacy plaintext values
pass through `decrypt_secret()` unchanged (backward compatibility with
pre-encryption rows).

Fernet is non-deterministic: identical plaintext values produce distinct
ciphertexts on each write.

**Covering tests:**
- `tests/integration/security/test_camera_encryption.py` — password column
  holds ciphertext, username stays readable, round-trip decryption to plaintext,
  legacy plaintext row passthrough.
- `tests/integration/security/test_notification_encryption.py` — SMTP password
  column holds ciphertext with `enc:v1:` prefix, `load_settings()` masks
  password with sentinel, blank/sentinel submission keeps existing encrypted
  password, Fernet non-determinism.

### 1.6 Key Provider (`security/keystore.py`)

Key provisioning tries the OS keystore (via `keyring`) and falls back to a
0600 key file at `<data_dir>/.secret-key`.

On POSIX, `KeyFileProvider`:
- Creates the key file with `os.open(O_CREAT|O_TRUNC, 0o600)`, followed by a
  defensive `os.chmod(0o600)`.
- `_verify_permissions()` raises `PermissionError` if the key file is group- or
  world-readable (`stat_mode & 0o077 != 0`). This check is skipped on Windows
  (`os.name == "nt"`).

The key file path is git-ignored (confirmed in `.gitignore`).

**Covering tests:**
- `tests/unit/security/test_keystore.py` — 0600 mode verified on POSIX,
  group-readable (0o640) and world-readable (0o644) key files raise
  `PermissionError`, rotation changes key file content, old key cannot decrypt
  after rotation.
- `tests/integration/security/test_portability.py` — encrypt/decrypt works
  across platforms, `build_key_provider(use_os_keystore=False)` returns
  `KeyFileProvider`.

### 1.7 Log Redaction (`logging.py`)

`redact_text()` applies two regex patterns to every log message:
- `_URL_USERINFO_PATTERN`: scrubs `scheme://user:pass@` to `scheme://***@`
  (covers `rtsp://`, `http://`, `https://`, `ftp://`).
- `_QUERY_SECRET_PATTERN`: scrubs values of query parameters named token,
  secret, key, password, passwd, pwd, sig, signature, auth, or credential.

`redact()` applies recursive structural redaction to log extra fields, masking
values under secret-looking keys.

`JsonFormatter.format()` applies `redact_text()` to the message and `redact()`
to structured extras. The `httpx` and `httpcore` loggers are capped at
`WARNING` to prevent URL leakage through library debug output.

**Covering tests:** `tests/integration/security/test_redaction_abuse.py` —
userinfo scrubbed from multiple URL schemes, query-string secret params
scrubbed, non-secret params preserved, structured extras with secret keys
masked, `JsonFormatter` integration tested.

### 1.8 FFmpeg Subprocess Hardening

**Argv-only invocation:**

FFmpeg is launched via `asyncio.create_subprocess_exec(*argv)` throughout.
`cameras/rtsp.py` uses `asyncio.create_subprocess_exec` for the single-frame
RTSP grab. No shell string is ever constructed.

**Allowlist before spawn:**

Every codec, container, filter name, and numeric parameter is validated against
`encode/allowlist.py` before a process is started:
- `CODEC_ENCODERS`: `h264` → `libx264`, `h265`/`hevc` → `libx265`,
  `vp9`/`libvpx-vp9` → `libvpx-vp9`
- `CONTAINER_MUXERS`: `mp4` → `mp4`, `mkv` → `matroska`, `webm` → `webm`
- `ALLOWED_FILTERS`: `deflicker`, `drawtext`, `scale`, `fps`, `format`,
  `setpts`, `overlay` — this is a `frozenset`; any filter name outside it
  raises before spawn
- FPS: 0.1–240.0; dimensions: 2–16384 (even only); CRF: 0–63;
  bitrate_kbps: 1–1,000,000

**Output-path confinement:**

`FfmpegEncoder._confine_output()` resolves the output path and checks
`resolved.is_relative_to(spec.project_render_root.resolve())`. A path resolving
outside the project render root raises `EncoderError` before spawn. This uses
`pathlib.Path.is_relative_to()`, not a string prefix comparison.

**Covering tests (output-path confinement):**
- `tests/abuse/test_subprocess_hardening.py` — asserts `EncoderError` with
  message "outside the project render root" for traversal attempts.
- `tests/integration/encode/test_ffmpeg_render.py` — integration test
  `test_output_path_traversal_outside_render_root_rejected`.

**Overlay image confinement:**

`overlay.resolve_overlay_image()` uses `Path.resolve()` +
`Path.is_relative_to()` to confine the overlay image path to the project's
render root. Symlink escapes are caught because `resolve()` follows symlinks
before the `is_relative_to()` check.

**Overlay text escaping (`encode/overlay.py`):**

- `escape_drawtext()`: replaces `'` with typographic apostrophe, doubles `\`,
  escapes `:` as `\:`, `%` as `\\\\%`
- `escape_timestamp_format()`: triples `\:` escape, escapes `'` as `\'`,
  passes `%` format directives through
- `escape_path_for_filter()`: doubles `\`, drops `'` entirely

`textfile=` is not used anywhere in the codebase (confirmed by test assertion
in `tests/abuse/test_subprocess_hardening.py`).

**Concat list escaping:**

Frame paths from the database are written into the concat list with
`_escape_concat_path()`, which escapes `'` to `'\''`. Each entry is wrapped in
`file '<...>'`; no bare `file <path>` line is written.

**No arbitrary execution:**

`tests/abuse/test_no_arbitrary_execution.py` performs a static AST audit of
all `.py` files under `src/`:
- No `shell=True` in any `subprocess.*` call
- No `create_subprocess_shell`
- No `os.system`
- No `os.popen`
- Files that legitimately spawn subprocesses are declared in an explicit
  allowlist (`encode/ffmpeg_impl.py`, `cameras/rtsp.py`, `service/tls.py`,
  `version.py`); any new spawn site added outside this list fails CI.

**Covering tests:** `tests/abuse/test_subprocess_hardening.py` —
codec injection attempts (`"h264;rm -rf /"`, null-byte), container injection,
unlisted filter names (`geq`, `movie`, `amovie`, `zmq`, `sendcmd`), drawtext
escaping boundary probing, overlay path traversal and symlink escape attempts.

### 1.9 Secure Defaults

- `ssrf.allowed_private_subnets` defaults to `[]` — all private addresses
  blocked without explicit admin opt-in.
- `ssrf.max_scan_hosts` defaults to 1024.
- `secrets.use_os_keystore` defaults to `True`.
- `secrets.keystore_service_name` defaults to `"timelapse-manager"`.
- `secrets.key_file` defaults to `None` (file fallback path derived from
  `data_dir`).

**Covering tests:** `tests/integration/security/test_secure_defaults.py` —
verifies the above defaults against the live settings classes.

---

## 2. Residual Risks / Partial Coverage

The following are real, documented gaps. They are not hidden.

### 2.1 DNS Rebinding — Check-Time Only

The SSRF guard validates at the time `resolve_and_check()` or
`assert_allowed_url()` is called. A hostname that resolves to a public IP at
check time could be made to resolve to a private/loopback IP at actual
connection time via DNS TTL manipulation ("DNS rebinding"). There is no
connect-time pinning to prevent this.

Mitigations in place: the snapshot URL is re-validated immediately before every
HTTP GET (not just at camera-add time), which reduces but does not eliminate
the window. Redirects are not followed.

**Updated (2026-06-11):** the RTSP/ONVIF **stream URI** path now re-validates the
host on **every** capture as well (see §2.2), so the previously-cached ONVIF
stream URI is no longer a rebinding hole. The general residual remains for the
brief check-to-connect window: full connect-time IP pinning (dial the validated
IP, preserve the hostname for TLS SNI) is not implemented; per-fetch/per-capture
re-validation is the accepted mitigation.

### 2.2 RTSP and ONVIF Stream URI — Not Guarded Before ffmpeg

The RTSP adapter (`cameras/rtsp.py`) passes `stream_url` directly to the ffmpeg
argv (`-i stream_url`) without routing it through `assert_allowed_url()` or
`resolve_and_check()`. The ONVIF adapter's stream URI (`_resolve_stream_uri()`)
follows the same path when initiating an RTSP grab.

The ONVIF adapter's **snapshot** path (`http_get_image()`) is guarded. The
camera address is validated at add time via `resolve_camera_host()`, but this
check is deferred for unresolvable hostnames, and does not prevent the ffmpeg
process from opening a stream URI targeting an internal host.

The ffmpeg RTSP grab itself is argv-only (no shell injection is possible), but
the network-level SSRF constraint is not enforced on the stream URI.

**RESOLVED (2026-06-11):** the stream URI host is now validated through the SSRF
guard before ffmpeg opens any socket. `RtspAdapter.capture()` calls
`_guard_stream_url()` on **every** capture (covering admin RTSP URLs and the
ONVIF RTSP fallback), and `OnvifAdapter._resolve_stream_uri()` validates the
device-supplied SOAP URI **before caching it** (defense-in-depth — a poisoned
URI is rejected and never stored). The host is extracted with
`urlsplit().hostname` (userinfo stripped, IPv6 unwrapped) and resolved via the
same camera policy (`allow_private` + `allowed_private_subnets`), so configured
private cameras still work; a blocked or unresolvable host fails closed as
`UnreachableCaptureError`. Covered by adversarial tests in
`tests/abuse/test_ssrf_stream_uri.py` and confirmed not to regress real-camera
RTSP capture (live suite green).

### 2.3 LDAP `bind_password` — Plaintext in Database

The `LdapSettings` model (`db/models/ldap_settings.py`) contains a
`bind_password` column. The model docstring explicitly notes that the column
"should be encrypted at rest" but states there is no LDAP settings service or
bind path implemented yet. No consumer is wired, so the field is never written
or read at this time. When LDAP is implemented, encryption must be applied to
this column before it is wired up.

### 2.4 `socket.getaddrinfo` — Synchronous on Async Path

`resolve_and_check()` calls `socket.getaddrinfo()`, which is a blocking
syscall. This is called from `validate_outbound_url()` in
`render/post_actions.py`, which runs on the asyncio event loop. A slow or
stalled DNS lookup will block the event loop for the duration of the call.

Practical impact is limited (post-render webhook calls are infrequent and
timeouts are bounded), but this is a correctness gap for a production asyncio
application.

**RESOLVED (2026-06-11):** an async wrapper `resolve_and_check_async()` runs the
blocking resolution via `asyncio.to_thread()`. The webhook dispatch
(`render/post_actions.py`) and the new stream-URI capture-path guard both use the
off-loaded path, so DNS resolution no longer blocks the event loop. The
synchronous `resolve_and_check()` API is retained for synchronous callers
(e.g. camera add-time validation).

### 2.5 Key File Permission Check — POSIX Only

`KeyFileProvider._verify_permissions()` is skipped on Windows (`os.name ==
"nt"`). On Windows the key file's access control depends entirely on OS-level
ACL configuration; the application does not validate it.

### 2.6 Behavioral Secure Defaults — Covered by Earlier Suites

The behavioral (fresh-instance) secure defaults are not re-asserted by the
Phase-09 abuse suite (`test_secure_defaults.py` verifies the *config-field*
defaults and the SSRF default-block behavior); they are covered end-to-end by
the following existing suites, which remain green:
- **First-run admin setup gate / no default credentials** —
  `tests/web/test_first_run.py` (a fresh instance redirects to setup before any
  control endpoint is served; initial admin creation), plus
  `tests/web/test_login_session.py` (authentication, no built-in account).
- **HTTP-to-HTTPS redirect (plaintext listener serves only the redirect)** —
  `tests/web/test_tls_redirect.py`.
- **Self-signed TLS certificate auto-generation on first start** —
  exercised by the service smoke path (`tests/integration/test_smoke_server.py`)
  and the dev-cert generation tests.
- **CLI bearer token required over loopback HTTP (loopback is not a bypass)** —
  `tests/unit/test_token.py`, `tests/web/test_cli_coexistence.py`,
  `tests/integration/test_api.py`.

---

## 3. Blocked / Not-Executed Gates

### 3.1 Raspberry Pi / ARM64 Hardware Soak

The performance and memory footprint targets for Raspberry Pi 4 (and equivalent
ARM64 hardware) have not been validated. No soak test has run on real Pi-class
hardware. See [`docs/pi-footprint.md`](./pi-footprint.md) for the method and
the results table (budgets recorded; measurements pending).

### 3.2 OS Keystore Round-Trip (Headless / CI)

`OsKeystoreProvider` requires a functioning OS keystore service (e.g. GNOME
Keyring, macOS Keychain, Windows Credential Manager). In a headless CI
environment no keystore daemon is available, so the round-trip probe in
`build_key_provider()` falls back to `KeyFileProvider` automatically. The OS
keystore path is therefore not exercised by CI; it requires a desktop or
specifically configured environment.

### 3.3 Cross-Platform CI (Windows and macOS)

Portability tests are written and pass on Linux. Windows and macOS CI runs are
not part of the current build matrix; those platforms are deferred to post-V1.

### 3.4 Least-Privilege Service User — Declared, Not Exercised

The packaging artifacts *declare* least-privilege execution: the systemd unit
sets a dedicated non-root `User=` with hardening directives and `ReadWritePaths`
scoped to the state directories, and the Docker image runs as a non-root UID
with volume-owned state. The build environment has no systemd or Docker runtime,
so the full run-as-non-root cycle (capture → encode → serve with no root, the
service user owning state/secret paths) was **not exercised**. This is a
declared-but-unverified gate pending a real Linux host / container runtime.

### 3.5 No Secret Baked Into an Image Layer — Docker-Blocked

The image is designed to never bake a secret into a layer (the encryption key
lives in the OS keystore or a mounted `0600` key file, never in the image). The
layer-inspection check (`docker save | tar -t` for the key-file path / key
material) requires a Docker daemon and was not run here; it is part of the
release pipeline.

---

## Claims That Could Not Be Confirmed Against Code

None. All specific claims in this document were verified against source files
before writing. If the reviewer identifies any discrepancy, it should be
resolved before this document is treated as a final sign-off.
