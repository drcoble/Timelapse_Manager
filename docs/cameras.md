# Cameras & Capture

Timelapse Manager communicates with network cameras through a set of
protocol adapters. Each adapter implements a common interface — `capture()`,
`validate_connection()`, `get_geolocation()`, `capabilities()`, and `close()`
— so the rest of the application depends only on that shared contract and
never on protocol-specific details.

---

## Supported protocols

### HTTP/JPEG (`http`)

Captures a single still by issuing an HTTP `GET` against a snapshot URL and
treating the response body as an image. Supports HTTP Basic and Digest
authentication (the first request is answered from the camera's `401`
challenge: Digest is preferred, and Basic is used only over `https://` or when
the camera's *Allow Basic authentication over plain HTTP* option is ticked;
later requests reuse the negotiated scheme).

**Required field:** `snapshot_uri` — the full URL of the snapshot endpoint,
e.g. `http://CAMERA_HOST/snapshot.jpg`.

### RTSP (`rtsp`)

Grabs a single frame from an RTSP stream by spawning an `ffmpeg` subprocess.
**`ffmpeg` must be on `PATH`** at runtime. The URL is passed directly to the
subprocess argument list (never via a shell), so special characters in the URL
do not introduce shell-injection risk.

**Required field:** `stream_uri` — the full RTSP URL, e.g.
`rtsp://CAMERA_HOST:554/stream`. Credentials are embedded in the URL as
`rtsp://USER:PASS@CAMERA_HOST/stream`. (A login typed into a camera address or
URI is moved into the encrypted credentials when the camera is saved; see
[Credentials](#credentials).)

ffmpeg runs in its own process group and is killed (and reaped) on timeout,
cancellation and shutdown, so a grab never leaves a process behind. Audio,
subtitle and data streams are dropped, and `rtsps://` streams work with TLS
under ffmpeg's protocol whitelist. ffmpeg takes the login from the URL, so it
is in the command line while a grab runs; see
[Camera credentials and `/proc`](./installation.md#camera-credentials-and-proc).
Three settings tune a grab:

| Setting | Default | Effect |
|---|---|---|
| `capture.rtsp_timeout_seconds` | `20.0` (greater than 0, at most 45) | ffmpeg socket timeout. The grab's whole budget is this plus 5 s, and replaces `capture.timeout_seconds` for an RTSP camera when larger. |
| `capture.rtsp_jpeg_quality` | `2` (1-31) | ffmpeg `-q:v` for the grabbed JPEG; lower is better. |
| `capture.rtsp_wait_for_keyframe` | `false` | Skip non-key frames before decoding; can lengthen a grab by one GOP. |

Environment form: `TLM_CAPTURE__RTSP_TIMEOUT_SECONDS`,
`TLM_CAPTURE__RTSP_JPEG_QUALITY`, `TLM_CAPTURE__RTSP_WAIT_FOR_KEYFRAME`. The
dial to an RTSP host is made by ffmpeg itself, so it is not address-pinned (see
[Connections](#connections)).

### Axis VAPIX (`vapix`)

Captures stills from Axis cameras via the `/axis-cgi/jpg/image.cgi` snapshot
endpoint. Also queries `/axis-cgi/param.cgi` to retrieve supported resolutions
and compression range, and `/axis-cgi/geolocation/get.cgi` for the device
position (see [Geolocation](#geolocation)). Supports Basic and Digest
authentication (the first request is answered from the camera's `401`
challenge: Digest is preferred, and Basic is used only over `https://` or when
the camera's *Allow Basic authentication over plain HTTP* option is ticked;
later requests reuse the negotiated scheme).

**Required field:** `address` — the camera's IP or hostname. Optional:
`snapshot_uri` (explicit snapshot URL, bypassing the default CGI path),
`default_resolution` (e.g. `1920x1080`), `compression` (0–100 scale;
0 = least compression).

### ONVIF (`onvif`)

Connects to ONVIF-compliant cameras via WS-Discovery and SOAP. The adapter
resolves the camera's media profiles over SOAP, then fetches the snapshot URI
for that profile. If the device exposes no snapshot endpoint it falls back to a
single-frame RTSP grab — **which requires `ffmpeg` on `PATH`**. Resolved URIs
are cached on the adapter instance after the first lookup.

**The camera's own host.** A snapshot, stream, events (`GetServices` XAddr) or
PullPoint subscription address that the camera returns must name the camera's
configured host; otherwise the capture fails with "camera returned a ... URI for
another host" and nothing is sent to that host. Hosts are compared, not origins
(an `rtsp://` URI on port 554 for an `https://` camera is normal). A different
name is refused even if it currently resolves to the camera; an IP address that
the configured name resolves to is accepted, so a camera added by name that
reports its IP works. A camera behind NAT that reports another host needs its
snapshot or stream URI stored on the camera form: an operator-stored URI is used
as stored. An events XAddr on another host is ignored in favour of the
conventional URL on the configured address.

**The camera's clock.** The adapter asks the camera for its time
(`GetSystemDateAndTime`, sent unauthenticated) once per camera session and stamps
the WS-Security `Created` value in the camera's time, so a camera whose clock is
skewed still authenticates. A camera that does not answer, or is more than a day
off, gets no offset. A refused login re-measures only when the stored offset is
older than 5 minutes. PullMessages cycles last at least 1 s.

**Required field:** `address` — the camera's IP or hostname (the adapter
derives the ONVIF device service path automatically). Optional pre-configured
`snapshot_uri` and `stream_uri` skip the SOAP resolution step.

---

## Credentials

Credentials are accepted as a JSON object on camera creation, for example:

```json
{"username": "admin", "password": "changeme"}
```

Credentials are **write-only** — they are stored in the database but never
returned in API responses.

**Authentication schemes.** Cameras are asked to authenticate with the
strongest scheme they offer (Digest, SHA-256 before MD5, then Basic). Basic is
never a fallback from a Digest offer: it is used only when the camera offers no
Digest at all. HTTP Basic sends the password only base64-encoded, so it is used
only over `https://`, or over `http://` for a camera with *Allow Basic
authentication over plain HTTP* ticked (API: `allow_basic_over_http`, default
`false`). The refusal happens before any credential is sent. Query (web) sends
Basic over plain HTTP only when the form's option is ticked, exactly as capture
does for a saved camera; Detect (API) never sends Basic over plain HTTP. Without
the opt-in, the result says why. A camera that
offers only an unsupported scheme (Negotiate, NTLM) is reported as an
authentication failure. A camera at an `http://` address that redirects to
`https://` is reported with the advice to change the address. Moving a camera
to Digest-only needs no change here.

**The default login.** A camera whose form is set to inherit the saved default
login gets it only when the camera's address, snapshot URI and stream URI all
resolve inside `ssrf.allowed_private_subnets`. With the default empty list the
default login is never sent. A name with any address outside the list (an AAAA
record, for example), a public address or a name that does not resolve is
captured without it and fails authentication, so such a camera needs its own
credentials. A camera's own login is not affected, except that an edit that moves
the camera to a host it did not have drops its stored login when the password
field is left blank (the form comes back with a notice); changing only the scheme
or port keeps it. Query and Detect on an address outside the list run without the
default and say so; typed credentials are always sent. The discovery enrichment
sends no credentials, so use Query with typed credentials on the add form to
fill the URIs of a camera that needs a login.

**The default login is re-checked on every use.** A camera session that holds
the default login is checked against the live `ssrf.allowed_private_subnets`
each time capture uses it (a lookup in the shared 60 s resolution cache, no
extra thread hop on a hit). If the camera's hosts are no longer inside, the
session is dropped and the reload withholds the default, so narrowing the list
takes effect at once and not at the next edit or restart. Sessions with their
own credentials, or with no default, are never touched, and the reconcile
fingerprint is unchanged. Binding decides from the same DNS answer the
connection dials. A hostname camera whose name briefly fails to resolve loses
the default the same way, and gets it back by itself: a session loaded without
the default is asked again on later uses, at most every 15 s, so DNS recovering
or the list being widened restores it with no edit or restart (#223, #248, #269,
#284).

**The default login is also contained at the socket (#268).** The cache entry
lives 60 s, so it can expire between the check and the connect; a request that
carries the default therefore connects only when every address the host has is
inside the live `ssrf.allowed_private_subnets`, and each write to a socket,
pooled keep-alive connections included, is checked again against the list and
against the whole DNS answer the connection was opened for. The routes the login
can take are covered as follows:

| Route | How it is held to the subnets |
|---|---|
| Snapshot, VAPIX and ONVIF requests, event long-polls, Detect and Query | A client that connects and writes only inside the subnets |
| ffmpeg (`rtsp` cameras, the ONVIF RTSP fallback) | ffmpeg looks the name up itself, so it is handed the IP the name resolved to; an `rtsps://` URI addressed by name is refused |
| VAPIX event WebSocket | Refused before the socket opens unless every resolved address is inside |
| Discovery enrichment | Sends no credentials |

A camera with its own credentials is not affected. For a default-login camera
use an IP address, or `rtsp://`, in the stream URI; an `rtsps://` URI by name
fails the grab with "cannot be pinned to an address", and the fix is an IP
address in the URI or credentials on the camera itself.

**Never put `user:pass@` in an address or URI.** It is moved into the encrypted
credentials when the camera is saved (and once at startup for cameras that
already carry one), and refused with a 422 when the camera already has
credentials. A username that holds a control character is refused. Errors and
log lines that quote a camera URL are redacted, including a login whose password
contains an `@`.

### Certificate pinning

By default a camera's TLS certificate is verified like any other connection (see
the precedence below). An administrator can instead pin one saved camera to one
exact certificate: in the camera edit form, **Certificate pin** → **Fetch the
camera certificate** (a handshake only; no request, no credentials), compare the
SHA-256 fingerprint, **Pin this certificate**, or **Remove pin**. The
administrator-only web routes are `POST /cameras/{id}/tls/fetch-certificate`,
`/tls/pin` and `/tls/unpin`; `pin` carries only the confirmed fingerprint, and the
server fetches the certificate again and stores it only if it still matches.
Pinning is opt-in and migration 039 changes no existing camera.

Trust for a camera's TLS connections, in order:

1. the camera's pin, if it has one: the stored certificate is the **only** trust
   anchor, so neither `capture.tls_ca_bundle` nor `SSL_CERT_FILE` can vouch for
   another certificate, and the host name is not checked;
2. otherwise `capture.tls_ca_bundle`;
3. otherwise `SSL_CERT_FILE` / `SSL_CERT_DIR`, then certifi.

A pinned camera is `https://` only: the HTTP client and the VAPIX event
WebSocket refuse `http://`, and a pin is refused on a camera with an `http://` or
scheme-less address, an `http://` snapshot URI or any stream URI. A pinned ONVIF
camera does not fall back to RTSP (RTSP cannot be TLS-verified), so a missing
snapshot endpoint is a failure. When the certificate changes or expires, captures
fail with `pinned certificate not accepted (it changed or expired); re-pin it in
the camera form`. The API shows `tls_pinned_fingerprint`, never the certificate.

An ONVIF camera may report `http://` URIs. On a pinned camera they fail closed,
by design: a pin cannot cover plain HTTP, so such a URI is refused instead of
being fetched unpinned.

A pin, a re-pin or an unpin takes effect without a restart. Reconcile closes any
pinned client whose pin no camera holds, after a grace of twice the timeout so
in-flight requests finish. Query on the edit form of a saved camera and Detect
in the API with `camera_id` use that camera's pin as their only trust; an
unknown `camera_id` is ignored and the probe uses the shared trust (#233).

### Connections

A camera connection (capture, event listeners, Detect, Query, discovery,
validation) goes only to an address the SSRF check validated, tried in resolver
order, and never re-resolves the camera's name when it connects. TLS server name
and certificate host-name verification stay on the camera's name. A DNS answer
that changes is followed once the 60 s validated answer expires, on the next new
connection; a pooled keep-alive connection stays on its old address until it
expires. Camera clients ignore `HTTP_PROXY`, `HTTPS_PROXY` and `NETRC`, because a
proxy would defeat the pinning; `SSL_CERT_FILE` is still honoured. ffmpeg dials an
RTSP host by itself and cannot be pinned, except that it is given the resolved IP
when the camera uses the default login (see above). Redirects are never followed
by the HTTP clients. ffmpeg follows an RTSP redirect, and FFmpeg clears the
login when the redirect points at a different host or port (a test against
real ffmpeg, `tests/integration/test_rtsp_redirect_credentials.py`, checks it).

Addresses may be IPv6 literals, bracketed (`[fd00::1]`, `[fd00::1]:554`) or bare
(`fd00::1`, whose colons are not a port separator); the RTSP candidate URI
brackets an IPv6 host (#249).

**After upgrading,** check any camera at an `http://` address that offers only
Basic: it stops capturing until you tick *Allow Basic authentication over plain
HTTP* on the camera, or change its address to `https://`. Cameras at `https://`
addresses and cameras that offer Digest are not affected. The option is
migration 038 (`camera.allow_basic_over_http`, off for every existing camera).

---

## The `build_adapter` factory

The `build_adapter` function in `src/timelapse_manager/cameras/registry.py`
maps a camera's configured `protocol` to the correct concrete adapter. It is
the single place where the capture engine (and the API layer) obtain an adapter
instance; it performs no network I/O on construction.

Protocol-to-required-field summary:

| Protocol | Required | Optional |
|---|---|---|
| `http` | `snapshot_uri` | `credentials` |
| `rtsp` | `stream_uri` | `credentials` |
| `vapix` | `address` | `credentials`, `snapshot_uri`, `default_resolution` |
| `onvif` | `address` | `credentials`, `snapshot_uri`, `stream_uri` |

---

## Discovery

Two discovery mechanisms surface ONVIF cameras on the network. Neither raises
for ordinary network errors — both log and return what they found.

### Multicast (`discover_onvif`)

Sends a WS-Discovery Probe to the standard multicast group
`239.255.255.250:3702` and collects ProbeMatch replies. Finds cameras on the
local segment without needing their addresses, but multicast does not cross
routed subnets. Via the API: `POST /api/v1/cameras/discover` with an empty
body (or `{"range": null}`).

Multicast discovery lists a camera only when its reply advertises its own
address. A camera behind NAT, or one that advertises a hostname, will not
appear; use a range scan instead.

### Unicast range scan (`scan_range`)

Sends a unicast WS-Discovery probe to each host in a CIDR block or dash-range
with bounded concurrency (default: 10 simultaneous probes). Works across routed
subnets where multicast does not reach. Via the API: `POST /api/v1/cameras/discover`
with `{"range": "192.168.1.0/24"}` or `{"range": "192.168.1.10-192.168.1.20"}`.

Discovery returns a list of found cameras with their `address`, `protocol`,
optional `snapshot_uri`, optional `stream_uri`, and optional `vendor` hint.
Snapshot and stream URIs are often absent at discovery time and are resolved
later by the ONVIF adapter. Discovery enrichment sends no credentials, so it
does not fill the URIs of a camera that needs a login.

The WS-Discovery multicast and range probes and the RTSP port probe run on the
event loop without holding executor threads. In the web Discover dialog the range
field is prefilled with the first `ssrf.allowed_private_subnets` entry that fits
`ssrf.max_scan_hosts`, and an empty multicast result says that multicast does not
cross routed VLANs and to scan a range.

---

### Deleting a camera

`DELETE /api/v1/cameras/{id}` (and the web and CLI equivalents) returns
`409 {"detail": "Camera is used by N project(s); ...", "code": "conflict"}`
while any project references the camera. Move or delete those projects first.

## Capture engine

Capture is organized around **projects**. A project binds a camera, a capture
interval, and a storage path. Each active project with a configured interval
gets its own independent capture loop, supervised by `CaptureSupervisor` in
`src/timelapse_manager/capture/supervisor.py`.

### How a capture loop works

1. On startup (controlled by `capture.autostart`), the supervisor reads all
   active projects that have a capture interval and a bound camera, and launches
   one asyncio task per project.
2. Each task evaluates the project's schedule to determine whether the capture
   gate is open. Inside an open window it calls `adapter.capture()` at the
   project's interval, wrapped in `asyncio.wait_for` bounded by
   `capture.timeout_seconds`. Outside an open window the loop sleeps until the
   next window boundary. A timeout is logged as a gap; the loop continues to
   the next tick.
3. On a transient failure (unreachable camera, network error, timeout), the loop
   backs off with capped exponential backoff and jitter before retrying. The
   first successful capture resets the backoff counter. The schedule gate is
   always respected: no capture is attempted outside an open window, even during
   a pending backoff.
4. **Disk-space gate.** Inside an open schedule window, the loop also checks
   whether free space on the project's storage volume is above the configured
   low watermark. If not, capture is paused and a warning event is written to
   the project log. The loop resumes automatically once space recovers above
   the (higher) resume watermark. Nothing is ever deleted to reclaim space.
   See [docs/storage.md](./storage.md) for the watermark settings and how the
   gate works.
5. A captured frame is written atomically by `FrameWriter` in
   `src/timelapse_manager/capture/frame_writer.py`: image bytes go to a
   temporary file in the target directory, are fsynced, then renamed into place.
   Only after the file is durable is a `Frame` row inserted into the database.
6. Per-project isolation: exceptions in one project's loop are caught,
   recorded in the project's live state, and the loop continues — one
   misbehaving camera does not stall other projects.
7. `asyncio.CancelledError` is not caught, so shutdown via `supervisor.stop()`
   cancels tasks cleanly.

A frame whose image is truncated or undecodable (no readable dimensions, a zero
dimension, or a JPEG without its end-of-image marker) fails the capture like any
other camera error.

For the full scheduling model, reliability behaviors (backoff configuration,
restart gap logging, frozen-frame detection), and worked examples, see
[docs/scheduling.md](./scheduling.md).

**Frame cap.** A project's `max_frame_count` is read from the database, not
kept from the value the runner started with. The loop ends the campaign at
exactly the cap stored now, so raising the cap lets a running loop continue, and
lowering it below the current count stops the next cycle (#104). An uncapped
project does no extra database read. Anchor and event one-shot frames do not
enforce the cap inside the loop (as before); reconcile still ends those
campaigns from the live row.

### Event-triggered capture

A project can capture when its camera reports an event (a *trigger*: a topic, an
enabled flag and a per-trigger cooldown). Each project with triggers gets one
event listener on the camera (ONVIF PullPoint, or the VAPIX WebSocket with a
PullPoint fallback).

- **Rising edges only.** A trigger fires on the transition from not-active (or
  unknown) to active. A repeat of a state the camera is already in, such as a
  heartbeat or a re-announce after a reconnect, does not fire, even with a
  cooldown of 0. An ONVIF `Initialized` event sets the baseline and never fires;
  a stateless event (no active/inactive state) fires. The tracker is keyed on
  topic and source and keeps an entry for `max(4 x cooldown, 60 s)` from the
  transition that set it, and never longer than 600 s. It is bounded (at most 256
  entries, 32 per topic), because the camera is untrusted; an evicted entry is
  simply unknown again, so a still-active state may fire once more.
- **Capture queue.** Matched events go on a queue of 8 per listener and a
  consumer takes one capture at a time through the camera's capture slot. When
  the queue is full the oldest event is dropped (and counted), so a slow capture
  never stops the subscription. A failure of one capture is logged and dropped.
- **Pacing.** Subscriptions are at least `max(5 s, 2 x capture.backoff_base_seconds)`
  apart, however the last one ended, and the backoff starts over only after a
  subscription has lived 60 s. A refused login backs off to the ceiling and drops
  the camera's cached session, but does not count toward the camera's offline
  alert.
- **Supervision.** A listener that dies is relaunched after a backoff, with an
  error event and a restart count.
- **`events_unavailable`.** After `capture.offline_failure_threshold` consecutive
  subscribe failures of one listener, one camera-scoped warning alert with reason
  `events_unavailable` is raised (several projects can share the camera). The next
  successful subscribe writes `events_available`, which clears it. It never
  touches the `camera_offline` alert. See
  [Monitoring](./monitoring.md#auto-clear-on-resolve).
- **Downgrade.** When a VAPIX WebSocket failure falls back to PullPoint, one
  warning event is recorded per downgrade, until the WebSocket works again.

Each listener's state (transport, connected since, last event, last progress
age, redacted last error, failure streak, restarts, dropped events, downgrades)
is in the `listeners` list of `GET /api/v1/system/debug/runners`; see
[debugging](./operations/debugging.md).

### Frame storage

Each frame is stored as an image file. The destination directory is the
project's `storage_path` when set, or `paths.frames_root/<project_id>/`
otherwise. Files are named `00000001.jpg`, `00000002.jpg`, etc., by
sequence index. A corresponding `Frame` row in the database records the
sequence index, capture timestamp, dimensions, file path, and file size.

Each captured frame also records the camera stream it came from and, when the
camera exposes it, a snapshot of the scene at capture time:

- **`stream_id`** — the identifier of the named stream/profile the frame was
  captured from, snapshotted onto the frame so its provenance is fixed even if
  the project later selects a different stream. `NULL` for an uploaded frame or
  one taken from the camera's default stream.
- **`scene_metadata`** — a queryable JSON column holding a small *versioned
  envelope* of the camera's scene/image settings at capture time. It is
  collected best-effort by a single short read **after** the frame is captured,
  outside the capture timeout and bounded to 2.5 seconds, so it never fails a
  capture and delays one by at most that long; a slow, failed or unavailable
  read leaves the column `NULL`. Today only the VAPIX adapter populates it
  (from the Axis `Image` parameter group); other protocols leave it `NULL`. The
  envelope looks like:

  ```json
  {
    "schema_version": 1,
    "source": "vapix",
    "captured_resolution": "1920x1080",
    "appearance_resolution": "1920x1080",
    "compression": "30",
    "rotation": "180",
    "overlays": "all",
    "brightness": "50",
    "contrast": "50",
    "saturation": "50",
    "sharpness": "50",
    "exposure_value": "50"
  }
  ```

  `schema_version` lets readers evolve with the shape; `source` names the adapter
  that produced it; `captured_resolution` is the frame's own dimensions. The
  `appearance_resolution`, `compression`, `rotation`, and `overlays` fields come
  from the camera's appearance settings and are present across Axis firmware
  generations; the finer tuning fields (`brightness`, `contrast`, `saturation`,
  `sharpness`, `color_enabled`, `exposure_value`, `exposure_priority`) appear
  only when the camera reports them. All values are kept verbatim as the device
  returned them.

If the project's selected stream profile no longer exists on the camera, cannot
be read, or is not read within 2 seconds, the frame is captured from the
camera's default stream. One `stream_profile_degraded` warning event is recorded
per project; it is recorded again only after that project's capture runner
restarts.

---

## Querying a camera

When adding or editing a camera, the **Query camera** action probes the camera
in a single step and surfaces three kinds of information concurrently:

- **Supported protocols.** All four protocols are probed in parallel; each
  reports whether it responded, and a recommended primary protocol is derived.
- **Geolocation.** Device-reported latitude and longitude, when available.
- **Hostname.** The device-reported network hostname, when available.

Each probe degrades independently — one failing never blanks the others. The
action returns its results for the operator to review and selectively apply to
the form; nothing is persisted until the camera is saved.

Metadata reads (geolocation and hostname) are only possible for protocols that
expose a metadata API: VAPIX and ONVIF. When a best-effort protocol (HTTP/JPEG
or RTSP) is the only responder, the geolocation and hostname fields report "not
available."

The form is laid out so that the address and credentials come before the Query
action, and the default-credentials checkbox is shown only when default
credentials are configured in Settings (see [Web UI](web-ui.md#camera-form)).
Query also honours the form's *Allow Basic authentication over plain HTTP*
option, for the protocol probes and the geolocation and hostname reads.

Credentials used by the query match the add-camera form: explicitly entered
credentials are tried; when the form is set to inherit the global default, that
default is used as the fallback.

---

## Hostname

A camera's network hostname is stored alongside its address and geolocation.
Two columns on the camera record track it:

- **`device_hostname`** — the most recently known hostname, either
  device-reported (from a VAPIX or ONVIF metadata read) or set manually by an
  operator.
- **`device_hostname_source`** — `"camera"` when the value came from the
  device, `"manual"` when an operator set it.

This mirrors the geolocation source pattern: a manual value is always explicit;
a device-reported value is stored only when no manual value is present.

---

## Geolocation

Three adapters can report a camera's geographic position:

- **VAPIX**: reads the Axis Geolocation API, `/axis-cgi/geolocation/get.cgi`.
  On old firmware without that API it falls back to the legacy
  `param.cgi?group=Geolocation` group (current firmware has no such group).
  An unset position (Axis reports `0`/`0`) is treated as "no location".
- **ONVIF**: calls the optional `GetGeoLocation` device operation; absent or
  unsupported responses are treated as "no location." AXIS OS does not
  implement it: Axis cameras answer `ter:ActionNotSupported` even for an
  administrator (checked on 11.11 and 12.11 firmware, 2026-10-27).
- **HTTP/JPEG, RTSP**: no geolocation metadata is available.

### Camera account and geolocation

Axis serves the position to an **Administrator** account only ("Security
level: Administrator" in the VAPIX Geolocation API). Viewer and operator
accounts get HTTP `401` from `get.cgi`; the app treats `403` the same way.

- Capture, stream profiles, PTZ presets and events need only an **Operator**
  VAPIX account. Use one (least privilege); do not make the capture account an
  administrator just to read the position. Whether a Viewer account is enough
  for every capture feature has not been verified.
- The position is static, so read it once and store it. Capture uses only the
  stored latitude/longitude and never queries the device.
- When the camera refuses the read, **Poll camera** and **Query camera** say so
  and name the role needed, instead of "no location". The adapter raises
  `GeolocationAccessDenied` (the `get_geolocation() -> GeoLocation | None`
  contract is unchanged) and logs the refusal at INFO with the device and
  status only, never the account.
- To fill in the position, either enter the coordinates manually, or use
  **Read with admin account** (below the Poll camera warning) once.

**Read with admin account** (`POST /cameras/{id}/geolocation/admin-read`,
operators and admins, VAPIX cameras only) reads `get.cgi` once with an
administrator login typed into its own fields:

- The login is used for that one request and is not saved, logged, shown in
  the response, or written to the audit line. The camera's own account is not
  changed, and its fields (`geo_admin_username` / `geo_admin_password`) are
  kept apart from the camera credential fields.
- The camera address is used as stored, so an `https://` camera is read over
  HTTPS. It must be unambiguous: an address with `?`, `#`, `\` or embedded
  `user:pass@` is refused. The host then goes through the same SSRF guard as
  Poll camera, which fails closed here if the name does not resolve.
- Attempts are rate-limited by an in-memory limiter of their own, separate
  from the login throttle: 5 attempts per 5 minutes for each of the camera's
  resolved IP, the camera, and the operator. Each attempt counts from the
  moment it starts; only a login the camera accepted (a 2xx from `get.cgi`)
  is forgiven. The limiter resets when the service restarts.
- The result is the same fragment as Poll camera; **Use these values** copies
  the coordinates into the form, and Save stores them.

`POST /api/v1/cameras/{id}/validate` returns an optional `geolocation_status`
of `"ok"`, `"none"` or `"access_denied"`, so an API client can see why nothing
was stored.

An operator can set a manual geolocation override on a camera record
(`geolocation_source = "manual"`). The manual override always wins over a
device-reported location. When `POST /api/v1/cameras/{id}/validate` is called
and the device reports a location, that location is persisted — but only if
the camera does not already have a manual override.
