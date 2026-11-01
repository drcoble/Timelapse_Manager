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
| `timestamp_format` | string | `"%Y-%m-%d %H:%M:%S"` | strftime pattern for the timestamp text: at most 64 characters, using letters, digits, space, `_ . / : -` and the directives `%a %A %b %B %d %H %I %j %m %M %p %S %y %Y %z %Z %%`. Anything else is rejected with 422. A stored render schedule with an invalid format renders with the default `%Y-%m-%d %H:%M:%S` and logs a warning |
| `timestamp_timezone` | string | `"UTC"` | IANA time-zone name used to localise the displayed timestamp |
| `text_enabled` | bool | `false` | Burn a fixed text caption into every output frame |
| `text_content` | string | `""` | The caption text (ignored when `text_enabled` is false). At most 1,000 characters; a longer value is rejected with 422 by the render API and by project `render_schedule` saves. A longer caption that was stored earlier is not changed and still renders |
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

**The browser decides.** The server serves every finished render inline, with
`Range` support and its real MIME type, and each viewer's browser reports
whether it can play it.

1. **At encode time** the render worker probes the finished file with `ffprobe`
   (installed beside the pinned ffmpeg; 10 s timeout) and stores two columns
   on the render job:
   - `codec_string`: the RFC 6381 codec string, built from ffprobe's
     `codec_name`, `profile`, `level`, `pix_fmt` and `codec_tag_string`
     (`encode/codec_string.py`). Examples: `avc1.64001F` (H.264 High 3.1),
     `hvc1.1.6.L93.B0` (H.265 Main 3.1), `vp09.00.10.08` (VP9 profile 0; libvpx
     signals no level, so level 1.0 is used), `av01.0.05M.08` (AV1 Main,
     `seq_level_idx` 5).
   - `mime_type`: `video/mp4`, `video/webm` or `video/x-matroska`.

   A failed probe leaves both `NULL` and logs a warning; it never fails the
   render. Renders made before these columns existed are probed once by a
   background task shortly after startup, one at a time, skipping missing
   files.
2. **In the page**, `static/js/playability.js` asks the browser about each
   render: `navigator.mediaCapabilities.decodingInfo()` with the codec string,
   size, frame rate and bitrate, falling back to
   `HTMLMediaElement.canPlayType()`. With no codec string recorded it checks the
   MIME type alone. The answer is cached per codec and size for the browser
   session. A render that passes shows the player; one that fails shows
   "This browser can't play <codec>" and the download link.
3. **Without JavaScript**, the page shows the player only for renders flagged
   `browser_streamable` (H.264 in MP4, the one combination every browser
   plays); every render keeps its download link. `browser_streamable` is kept
   for this and is otherwise informational.

**Encode flags for playback.** MP4 outputs are written with
`-movflags +faststart` (the `moov` index before the media, so playback and
seeking start without fetching the end of the file), and H.265 in MP4 is tagged
`-tag:v hvc1` (ffmpeg defaults to `hev1`, which Safari refuses) for every HEVC
encoder, software or hardware.

**What plays where:**

| Render | Plays in |
|---|---|
| H.264 / MP4 | every browser |
| AV1 / MP4 | Chrome, Edge, Firefox and Opera (software decode). **Safari only with an AV1 hardware decoder**: an M3 Mac or later, iPhone 15 Pro or later, M4 / A17 Pro iPads. Older Apple devices get the download. |
| H.265 / MP4 | Safari; Chrome and Edge where the device has a hardware HEVC decoder |
| VP9 / WebM | Chrome, Edge, Firefox, recent Safari |
| any / MKV | rarely: most browsers answer no for `video/x-matroska`, so MKV renders mostly show the download |

The probe is per device, so the page shows what is true for each viewer.

**Latest Frame player.** On a project's page, the Latest Frame links to the
project's newest finished render (exports excluded, file on disk). A click, or
Enter / Space, runs the same browser check and plays the render in place
(muted, with controls); Close or the end of the video brings the frame back.
If this browser can't play the codec, a note offers the download instead; it
does not fall back to an older render. Without JavaScript the link opens the
video file. With no finished render the frame is a plain image marked
"No render yet". Viewers can play renders too.

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
  before the cancel API call returns. A frame export (`kind` `export`, a zip
  built in a worker thread) cannot be interrupted mid-file; cancel waits for
  its thread, which sees the job `failed` before its final rename and discards
  the partial archive. An export that is still queued behind other long jobs is
  cancelled at once: the call returns without waiting, and the zip is never
  built.
- **Frame exports.** Exports have their own slot, `render.max_concurrent_exports`
  (default 1), so an export never holds a render's slot (`render.max_concurrent`)
  and a long export cannot delay a render. The zip stores JPEG, PNG and WebP
  files without deflating them (a JSON manifest is still deflated). It checks free
  disk space before it starts (the sum of the file sizes plus 1%; the export fails
  with `insufficient disk space`) and again every 200 files, and it never leaves a
  `.part` file: one is removed on any failure, and stale `export-*.zip.part` files
  are swept when the queue starts. Finished exports are deleted after
  `render.export_ttl_days` (default 7) by the hourly housekeeping, zip and row
  together; `0` keeps them. Each deletion writes an info event naming the TTL.
- **Frame window.** When the worker starts a job it records what goes into it:
  `frame_count`, `first_frame_at` and `last_frame_at` (naive UTC) and the
  computed chapters. An export records the frames it actually bundled. Jobs
  that ran before these were stored (migration 026) have none of them; the
  render card shows "not recorded".
- **Deletion.** `DELETE /api/v1/renders/{id}` (or **Delete** on the render
  card) removes the job row and its output file. The file is unlinked only when
  it resolves inside the project's render root; a file that is already gone is
  not an error. A `pending` or `encoding` job is cancelled first (as above) and
  deleted only once it has stopped, so no FFmpeg process is ever left writing a
  file whose row is gone. Each delete writes the audit event `render N deleted
  by <user>` with the file name and size. Unlike the automatic prune, a manual
  delete also removes `archive` renders. Job ids are never reused (the table uses
  SQLite `AUTOINCREMENT`, migration 028), so "render N" in the audit log
  always means one job.
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

`fps` may also be the string `"auto"` (the schedule form's *Auto (suggested)*
option): each time the render starts, the frame rate is worked out from the
project's frames as described under *Suggested frame rate*. Schedules that
stored a number are unchanged.

`encoder`, `container`, `fps`, `resolution`, and `auto_prune` control the
output format of automatic renders. Schedules that predate these fields default
to H.264/MP4/24 fps/1080p with `auto_prune` enabled.

`overlay_config` (optional) holds the overlay fields listed under
[Render-time overlays](#render-time-overlays). Its `timestamp_format` follows
the same rules as the manual render API: an strftime pattern of at most 64
characters, using letters, digits, space, `_ . / : -` and the directives
`%a %A %b %B %d %H %I %j %m %M %p %S %y %Y %z %Z %%`. Anything else is rejected
with 422 on `PATCH /api/v1/projects/{id}`, even while the schedule is disabled,
and so is a `text_content` over 1,000 characters.
A stored schedule with an invalid format renders with the default
`%Y-%m-%d %H:%M:%S` and logs a warning, once per project per process and only
while the timestamp overlay is on; cloning a project resets it.

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
but any integer in range is accepted on save.

**Suggested frame rate.** The *Render now* panel pre-fills `fps` with a suggestion
and labels it with the expected video length. A typed value from 1 to 240 is still
accepted, and *Use suggested* restores the suggestion. The suggestion is based on
the **median gap between consecutive frames** of the frames the render would use
(so outages, restarts and weekday masks do not skew it). The gap is interpolated
linearly in log(interval) between these anchors, then snapped to the nearest of
8, 10, 12, 15, 20, 24 or 30 fps (a tie goes to the higher rate), so the result is
always 8 to 30:

| Interval between frames | Suggested fps |
|---|---|
| 1 minute or less, up to 10 minutes | 30 |
| 1 hour | 24 |
| 6 hours | 15 |
| 1 day | 12 |
| 1 week or more | 8 |

With fewer than two frames there is no suggestion. `GET
/api/v1/projects/{project_id}/renders/suggested-fps` (bearer) returns it, for the
project's frames or an optional `start` to `end` range (a bound without a zone is
UTC; `start` after `end` is a 422):

```json
{"fps": 12, "interval_seconds": 86400.0, "frame_count": 40, "expected_duration_seconds": 3.33}
```

`fps` and `interval_seconds` are `null` when there is no suggestion. The endpoint
only informs the caller: a render triggered through the API without `fps` still
uses `render.default_fps`. This is distinct from the manual
render API `fps` field, which accepts any float between 0.1 and 240 (out of
range, or `NaN`/`Infinity`, is a 422).

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

`/renders/{id}/download` and `/renders/{id}/stream` return `404` for a frame
export; exports are downloaded from the project's Frames tab.

### Stream

`GET /api/v1/renders/{id}/stream`

Serves any finished render inline (no attachment disposition) with its real
MIME type (`video/mp4`, `video/webm`, `video/x-matroska`), whatever its codec;
whether it plays is up to the browser (see
[Browser-streamable detection](#browser-streamable-detection)). It honours HTTP
`Range` headers and responds `206 Partial Content` with the requested byte
range, so browsers can seek without downloading the whole file. The
`Accept-Ranges: bytes` and `Content-Range` headers are included in the response.

A request with no valid `Range` header gets the full file (`200 OK`).
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
| `DELETE` | `/api/v1/renders/{id}` | Admin | Delete a render (or export) and its file; cancels it first if running |
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

If a manual render with the same output and overlay settings is still pending
or encoding, that job is returned (201) with `X-Render-Deduplicated: true`
instead of queuing another.

Render job response object:
```json
{
  "id": 42,
  "project_id": 1,
  "kind": "manual",
  "status": "pending",
  "output_file_path": null,
  "browser_streamable": null,
  "codec_string": null,
  "mime_type": null,
  "started_at": null,
  "completed_at": null,
  "created_at": "2026-06-10T12:00:00"
}
```

`kind` is `"manual"`, `"scheduled"`, or `"archive"`.
`status` is `"pending"`, `"encoding"`, `"done"`, or `"failed"`.
`browser_streamable` is set once encoding completes. `codec_string` and
`mime_type` are probed from the output when it is written; they stay `null`
until then, or when the probe failed.

**`POST /api/v1/renders/{id}/cancel`** — cancel a pending or in-flight render.
Returns the updated job object (status will be `"failed"`). A job already in
a terminal state (`done` or `failed`) is returned unchanged.

**`DELETE /api/v1/renders/{id}`** — delete a render job and its output file
(see **Deletion** under [The render queue](#the-render-queue)). Returns
`204 No Content`; `404` for an unknown id; `403` for a viewer; `409` if a
running job could not be stopped; `503` if a running job cannot be stopped
because the render worker is not available. Operators and admins only.

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

### System

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/system/render-activity` | Bearer | Render queue activity, for upgrade gating |

**`GET /api/v1/system/render-activity`** returns:
```json
{"live_tasks": 0, "pending_rows": 0, "encoding_rows": 0, "orphans": 0}
```

`orphans` counts `encoding` rows with no live task; the worker fails them on
its next cycle. An upgrade is safe when `live_tasks + pending_rows == 0`.
