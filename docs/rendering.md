# Video Rendering

Timelapse Manager renders a project's captured frames into a timelapse video
using FFmpeg as a subprocess. This page covers the full rendering pipeline:
how frames become a video file, what output settings are available, how the
render queue and scheduler work, post-render actions, and how to download or
stream a finished render.

---

## The render pipeline

A render reads all **active** (non-deleted) frames for a project in capture-
timestamp order and passes them to FFmpeg through a concat-demuxer list. The
source frame files are **never modified**; overlays are burned into the output
at encode time only.

### Steps

1. **Frame gathering.** Active frames are read in batches from the database in
   capture-timestamp order. Frames missing a timestamp or file path are
   skipped. Soft-deleted frames are excluded.
2. **Spec assembly.** Output settings, overlay config, and chapters are
   resolved into an immutable render spec.
3. **Validation.** The codec, container, and numeric parameters are checked
   against an allowlist before FFmpeg is ever spawned. An unsupported value is
   rejected immediately with a clear error naming the offending field.
4. **Encoding.** FFmpeg is invoked as a subprocess with an argument list —
   never a shell command — and produces the output file. Only software
   encoders are used; hardware-accelerated encoding is planned but not yet
   implemented.
5. **Result recording.** On success the job row is stamped `done` and the
   output path stored. On failure the job is stamped `failed` and any partial
   output file is removed.
6. **Post-render actions.** After a successful encode, configured follow-up
   actions run (see [Post-render actions](#post-render-actions)).

---

## Output settings

Every render request carries an `output` block with the following fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `fps` | float | `render.default_fps` (24.0) | Output frame rate |
| `width` | int | `1920` | Output width in pixels (must be even) |
| `height` | int | `1080` | Output height in pixels (must be even) |
| `codec` | string | `"h264"` | Video codec |
| `container` | string | `"mp4"` | Container format |
| `crf` | int or null | null | Constant-rate-factor quality (0–63). Preferred over `bitrate_kbps` when set. |
| `bitrate_kbps` | int or null | null | Target bitrate in kbps. Used when `crf` is not set. When neither is set the encoder's built-in default applies. |
| `deflicker` | bool | `false` | Whether to run FFmpeg's `deflicker` filter to reduce inter-frame flicker |
| `auto_chapters` | string or null | null | Automatic chapter granularity: `"monthly"`, `"weekly"`, or null |

**Supported codecs:** `h264` (H.264/AVC), `h265` / `hevc` (H.265/HEVC),
`vp9`, `av1` (AV1/SVT-AV1). `libx264`, `libx265`, `libvpx-vp9`, and
`libsvtav1` are accepted as aliases.

**Supported containers:** `mp4`, `mkv`, `webm`.

**AV1 container restriction.** AV1 encodes may only be muxed into `mp4` or
`mkv`. Pairing `av1` with `webm` is rejected at validation.

Chapters cannot be embedded in WebM; a render that requests chapters with a
WebM container is rejected at validation.

---

## Deflicker

Setting `deflicker: true` inserts FFmpeg's `deflicker` filter before the first
stage of the filtergraph. This reduces visible flicker caused by exposure and
white-balance variation between frames — particularly noticeable in long outdoor
captures. It adds processing time proportional to the number of frames.

---

## Render-time overlays

Up to three overlay layers can be burned into the output video. The source
frame files are **never modified**; overlays are rendered at encode time. All
enabled overlays share a single `placement` corner.

| Field | Type | Default | Description |
|---|---|---|---|
| `timestamp_enabled` | bool | `false` | Burn the frame's real capture time into each output frame |
| `timestamp_format` | string | `"%Y-%m-%d %H:%M:%S"` | strftime pattern for the timestamp text |
| `timestamp_timezone` | string | `"UTC"` | IANA time-zone name used to localise the displayed timestamp |
| `text_enabled` | bool | `false` | Burn a fixed text caption into every output frame |
| `text_content` | string | `""` | The caption text (ignored when `text_enabled` is false) |
| `image_enabled` | bool | `false` | Composite a watermark or logo image into every output frame |
| `image_path` | string or null | null | Path to the watermark image; must be inside the project's render directory |
| `placement` | string | `"top_left"` | Corner for all enabled overlays: `top_left`, `top_right`, `bottom_left`, `bottom_right` |

**Timestamp overlay.** The timestamp burned into each output frame shows the
true wall-clock time at which that frame was captured, localised to
`timestamp_timezone`. This is not the video playback time; it is the actual
date and time the camera snapped the image. A single FFmpeg `drawtext` filter
handles all frames regardless of project length. A TrueType font is required;
the encoder probes a platform default when `render.font_path` is not configured.

**Text overlay.** A fixed caption string, such as a site name or camera label,
drawn at the chosen corner. When both timestamp and text are enabled they are
stacked so they do not overlap.

**Image overlay.** A watermark or logo composited at the chosen corner. The
image file path must resolve inside the project's render directory; paths
outside it are rejected.

---

## Chapters

Chapters embed named markers in the output file for easy navigation in video
players and browsers. Two sources of chapters are supported:

- **Automatic calendar chapters.** Set `auto_chapters` to `"monthly"` or
  `"weekly"`. The renderer places a chapter at the first frame of each new
  calendar month or ISO week the capture timestamps cross. Labels are formatted
  as `"January 2026"` (monthly) or `"Week of 2026-01-05"` (weekly).
- **Manual milestones.** Milestones placed on a project's timeline (see
  [Milestones API](#milestones)) become chapters at the corresponding frame's
  playback offset. A milestone label overrides an automatic chapter label at
  the same frame.

Both sources are combined and sorted by playback offset. Chapters require the
container to support them: `mp4` and `mkv` do; `webm` does not and is rejected
at validation.

---

## Browser-streamable detection

After encoding, the render engine records whether the output can play natively
in a browser `<video>` element without plugins. The only combination currently
flagged as browser-streamable is **H.264 in MP4**. All other codec/container
combinations — H.265, VP9, AV1, MKV, WebM — are treated as download-only.
The `stream` endpoint uses this flag to decide between inline byte-range
streaming and a file download response. In particular, AV1 and VP9 renders
are always download-only regardless of container.

---

## The render queue

Renders are executed by a background worker that drains a database-backed
queue of pending jobs. Key design points:

- **Bounded concurrency.** At most `render.max_concurrent` renders run at
  once (default: 1). The cap bounds resource use and ensures renders never
  starve capture.
- **Job kinds.** Each job carries one of three kinds:
  - `manual` — triggered by a `POST /api/v1/projects/{id}/renders` request.
  - `scheduled` — enqueued automatically by the render scheduler on the
    project's recurring render cadence.
  - `archive` — enqueued automatically on the project's archive snapshot
    cadence; archive renders are never pruned by the `prune` post-action.
- **Job statuses.** A job moves through: `pending` → `encoding` → `done` or
  `failed`. These are the only states; no retry is performed — a failed job
  is a failed job.
- **Cancellation.** A `pending` job can be cancelled before it starts; an
  `encoding` job's FFmpeg process is killed, any partial output file is
  removed, and the job is recorded as `failed`. Cancellation is complete
  before the cancel API call returns.
- **Startup recovery.** Any job left in `encoding` at startup (from a prior
  crash or unclean shutdown) is immediately swept to `failed` so the project
  is available for a fresh render.

---

## The render scheduler

For each active project, the render scheduler periodically checks whether a
recurring render or archive snapshot is due and enqueues a job when so. The
scheduler is separate from the worker queue: it only enqueues; the bounded
worker drains.

**Cadence shape.** Both `render_schedule` and `archive_schedule` on a project
are JSON documents. The full shape for `render_schedule` is:

```json
{
  "enabled": true,
  "interval_seconds": 86400,
  "encoder": "libx264",
  "container": "mp4",
  "fps": 24,
  "resolution": "1920x1080",
  "auto_prune": true
}
```

An absent schedule, `"enabled": false`, or a missing or non-positive interval
means the schedule is off. The scheduler checks against the database (not in-
memory state), so downtime does not reset the clock — the last job's creation
timestamp is the cadence anchor.

`encoder`, `container`, `fps`, `resolution`, and `auto_prune` control the
output format of automatic renders. Schedules that predate these fields default
to H.264/MP4/24 fps/1080p with `auto_prune` enabled.

**Encoder/container combinations for project render settings.** The available
encoders are `libx264` (H.264), `libx265` (H.265), `libvpx-vp9` (VP9), and
`libsvtav1` (AV1). Not all combinations are muxable:

| Container | Supported encoders |
|---|---|
| `mp4` | `libx264`, `libx265`, `libsvtav1` |
| `mkv` | `libx264`, `libx265`, `libvpx-vp9`, `libsvtav1` |
| `webm` | `libvpx-vp9` |

**Frame rate.** `fps` must be a whole number between 1 and 240 (inclusive).
The project settings UI derives a short list of suggested frame rates from the
project's capture interval — slower cadences lean toward lower suggestions —
but any integer in range is accepted on save. This is distinct from the manual
render API `fps` field, which accepts any float.

**Double-enqueue guard.** If a job of the same kind is already in `pending` or
`encoding` state for the project, the scheduler skips that cycle to avoid
piling up jobs while a slow render is in progress.

The scheduler check interval is controlled by
`render.scheduler_check_interval_seconds` (default: 60 seconds).

---

## Auto-prune of recurring renders

When a `scheduled` or `archive` render completes, and `auto_prune` is enabled
on the corresponding schedule, the engine automatically retains only the **most
recent** render of that kind for the project and deletes all older ones of the
same kind. Scope is per-kind: a `scheduled` completion deletes only older
`scheduled` renders; an `archive` completion deletes only older `archive`
renders. Neither kind ever deletes the other's outputs.

Key properties:

- **Manual renders are never affected.** A manual render never triggers
  auto-prune and is never deleted by it.
- **N=1.** Exactly the newest render of the same kind is kept; all prior
  completed renders of that kind are removed (output file and job row).
- **Captured frames are never touched.** Auto-prune only removes render output
  files, confined to the project's render directory. Frame image files are
  never considered.
- **Default on.** A schedule with no `auto_prune` key is treated as enabled. A
  project whose schedule predates the feature therefore starts with auto-prune
  active. Set `"auto_prune": false` to disable it.
- **Failure-isolated.** A failure in auto-prune is logged and recorded as a
  project event but never affects the render result; the render is already
  committed as `done` before pruning runs.

Auto-prune is separate from the configurable `prune` post-render action (which
keeps a caller-specified count, is manual-trigger-exempt, and excludes archive
renders from its pool). Both can be active on the same project; they operate
independently.

---

## Post-render actions

After a successful encode, a project may run one or more built-in follow-up
actions. Actions are configured on the project's `post_render_actions` field as
a list of spec objects. Three action types are supported:

### `export`

Copies the rendered video file to a destination directory, leaving the original
in place.

```json
{"type": "export", "destination": "/path/to/publish-dir"}
```

The destination directory is created if it does not exist. The file is copied
with its original name.

### `external_trigger`

POSTs a JSON notification to a webhook URL.

```json
{"type": "external_trigger", "url": "https://example.com/hooks/render-done"}
```

The request payload:

```json
{
  "event": "render_completed",
  "project_id": 1,
  "render_id": 42
}
```

The request carries a timeout (`render.webhook_timeout_seconds`, default: 10 s)
and does not follow redirects. A non-2xx response is treated as a failure.

### `prune`

Deletes old non-archive render outputs and their job rows beyond a keep count.

```json
{"type": "prune", "keep": 5}
```

Keeps the newest `keep` completed, non-archive renders for the project and
removes the rest. Output files are only deleted when their stored path resolves
inside the project's render directory; a path that escapes the directory is
skipped (only the database row is removed). **Archive renders and captured
frames are never touched.**

### Action behaviour

- **Failure-isolated.** A failure in any action is logged and written to the
  project event log, but it never fails the render (the video is already
  produced) and never stops the remaining actions from running.
- **Export disabled under Docker.** The **export** action is skipped when the
  service runs inside a Docker container, because it has no access to an
  arbitrary host export directory (the destination would resolve inside the
  container and be lost on restart). The **webhook** and **prune** actions have
  no host-path dependency and run normally in a container.
- **No arbitrary commands.** Only the three built-in action types above are
  supported; unknown action types are logged and ignored.

---

## Downloading and streaming renders

Two endpoints serve finished renders:

### Download

`GET /api/v1/renders/{id}/download`

Serves the output file as an attachment. Works for all codec/container
combinations. Requires a bearer token.

### Stream

`GET /api/v1/renders/{id}/stream`

For browser-streamable renders (H.264/MP4 only), honours HTTP `Range` headers
and responds `206 Partial Content` with the requested byte range. This allows
browsers to seek within the video without downloading the whole file. The
`Accept-Ranges: bytes` and `Content-Range` headers are included in the response.

For renders that are not browser-streamable, or for requests with no valid
`Range` header, the endpoint falls back to serving the full file (`200 OK`).
Requires a bearer token.

---

## Milestones

Milestones are named markers placed on a project's capture timeline. They
become chapters in the rendered video.

Each milestone needs either a `position_frame_index` (zero-based frame ordinal
in the project's capture sequence) or a `position_timestamp` (ISO-8601 UTC
datetime). When both are provided, the frame index takes precedence.

If a milestone's frame has been soft-deleted, the chapter snaps forward to the
next active frame. A milestone that matches no active frame is omitted from the
chapter list.

---

## HTTP API

All render and milestone endpoints require `Authorization: Bearer <token>`.
Mutating endpoints (marked **Admin** below) additionally require the local
administrator token (currently the same token; role-based access control is
planned but not yet implemented).

### Renders

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/api/v1/projects/{project_id}/renders` | Admin | Trigger a manual render |
| `GET` | `/api/v1/projects/{project_id}/renders` | Bearer | List a project's render jobs, newest first |
| `GET` | `/api/v1/renders/{id}` | Bearer | Get a single render job's status |
| `POST` | `/api/v1/renders/{id}/cancel` | Admin | Cancel a pending or in-flight render |
| `GET` | `/api/v1/renders/{id}/download` | Bearer | Download the output file |
| `GET` | `/api/v1/renders/{id}/stream` | Bearer | Stream for inline playback (HTTP Range / 206) |

**`POST /api/v1/projects/{project_id}/renders`** — trigger a manual render.
Returns `201 Created` with the job object.

Request body:
```json
{
  "output": {
    "fps": 24.0,
    "width": 1920,
    "height": 1080,
    "codec": "h264",
    "container": "mp4",
    "crf": null,
    "bitrate_kbps": null,
    "deflicker": false,
    "auto_chapters": null
  },
  "overlay": {
    "timestamp_enabled": false,
    "timestamp_format": "%Y-%m-%d %H:%M:%S",
    "timestamp_timezone": "UTC",
    "text_enabled": false,
    "text_content": "",
    "image_enabled": false,
    "image_path": null,
    "placement": "top_left"
  }
}
```

All fields are optional and default as documented above. The target is
validated before any job row is created; an unsupported codec, container, or
parameter returns `400 Bad Request`.

Render job response object:
```json
{
  "id": 42,
  "project_id": 1,
  "kind": "manual",
  "status": "pending",
  "output_file_path": null,
  "browser_streamable": null,
  "started_at": null,
  "completed_at": null,
  "created_at": "2026-06-10T12:00:00"
}
```

`kind` is `"manual"`, `"scheduled"`, or `"archive"`.
`status` is `"pending"`, `"encoding"`, `"done"`, or `"failed"`.
`browser_streamable` is set once encoding completes.

**`POST /api/v1/renders/{id}/cancel`** — cancel a pending or in-flight render.
Returns the updated job object (status will be `"failed"`). A job already in
a terminal state (`done` or `failed`) is returned unchanged.

### Milestones

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/api/v1/projects/{project_id}/milestones` | Admin | Create a milestone |
| `GET` | `/api/v1/projects/{project_id}/milestones` | Bearer | List a project's milestones |
| `DELETE` | `/api/v1/projects/{project_id}/milestones/{id}` | Admin | Delete a milestone |

**`POST /api/v1/projects/{project_id}/milestones`** — place a milestone.
Returns `201 Created`.

Request body:
```json
{
  "label": "Foundation poured",
  "position_frame_index": 1200,
  "position_timestamp": null
}
```

At least one of `position_frame_index` or `position_timestamp` must be set.

Milestone response object:
```json
{
  "id": 7,
  "project_id": 1,
  "label": "Foundation poured",
  "position_frame_index": 1200,
  "position_timestamp": null
}
```

**`DELETE /api/v1/projects/{project_id}/milestones/{id}`** — delete a
milestone. Returns `204 No Content`.
