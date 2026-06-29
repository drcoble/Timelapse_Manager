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
authentication (probes unauthenticated first, then retries with the scheme
advertised in `WWW-Authenticate`).

**Required field:** `snapshot_uri` — the full URL of the snapshot endpoint,
e.g. `http://CAMERA_HOST/snapshot.jpg`.

### RTSP (`rtsp`)

Grabs a single frame from an RTSP stream by spawning an `ffmpeg` subprocess.
**`ffmpeg` must be on `PATH`** at runtime. The URL is passed directly to the
subprocess argument list (never via a shell), so special characters in the URL
do not introduce shell-injection risk.

**Required field:** `stream_uri` — the full RTSP URL, e.g.
`rtsp://CAMERA_HOST:554/stream`. Credentials are embedded in the URL as
`rtsp://USER:PASS@CAMERA_HOST/stream`.

### Axis VAPIX (`vapix`)

Captures stills from Axis cameras via the `/axis-cgi/jpg/image.cgi` snapshot
endpoint. Also queries `/axis-cgi/param.cgi` to retrieve supported resolutions,
compression range, and (when configured on the device) geolocation.
Supports Basic and Digest authentication.

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

### Unicast range scan (`scan_range`)

Sends a unicast WS-Discovery probe to each host in a CIDR block or dash-range
with bounded concurrency (default: 10 simultaneous probes). Works across routed
subnets where multicast does not reach. Via the API: `POST /api/v1/cameras/discover`
with `{"range": "192.168.1.0/24"}` or `{"range": "192.168.1.10-192.168.1.20"}`.

Discovery returns a list of found cameras with their `address`, `protocol`,
optional `snapshot_uri`, optional `stream_uri`, and optional `vendor` hint.
Snapshot and stream URIs are often absent at discovery time and are resolved
later by the ONVIF adapter.

---

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

For the full scheduling model, reliability behaviors (backoff configuration,
restart gap logging, frozen-frame detection), and worked examples, see
[docs/scheduling.md](./scheduling.md).

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
  collected best-effort by a single short read during capture and never delays
  or fails a capture; a failed or unavailable read simply leaves the column
  `NULL`. Today only the VAPIX adapter populates it (from the Axis `Image`
  parameter group); other protocols leave it `NULL`. The envelope looks like:

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

- **VAPIX**: reads `Geolocation.Latitude` / `Geolocation.Longitude` from the
  Axis parameter CGI, when configured on the device.
- **ONVIF**: calls the optional `GetGeoLocation` device operation; absent or
  unsupported responses are treated as "no location."
- **HTTP/JPEG, RTSP**: no geolocation metadata is available.

An operator can set a manual geolocation override on a camera record
(`geolocation_source = "manual"`). The manual override always wins over a
device-reported location. When `POST /api/v1/cameras/{id}/validate` is called
and the device reports a location, that location is persisted — but only if
the camera does not already have a manual override.
