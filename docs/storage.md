# Storage & Frame Management

---

## Storage layout

Captured frames are stored as image files on disk alongside a `Frame` row in
the database for each one. The database records the sequence index, capture
timestamp, dimensions, file size, lifecycle state, and origin (`captured` or
`uploaded`); the raw bytes live in the file.

### Default layout

When a project has no explicit `storage_path`, frames go to:

```
<paths.frames_root>/<project_id>/<sequence_index>.jpg
```

for example:

```
data/frames/1/00000001.jpg
data/frames/1/00000002.jpg
data/frames/2/00000001.jpg
```

`paths.frames_root` defaults to `data/frames` relative to the working
directory. See [docs/configuration.md](./configuration.md) for how to set it.

### Per-project storage override

Set `storage_path` on a project to direct its frames to a different directory —
useful for a dedicated volume, a network share, or a separate disk per project.
When `storage_path` is set, frames go directly into that directory (not into a
`<project_id>/` sub-directory).

**Rules.** A custom storage path is checked when it is set or changed, in
the web settings form or `PATCH /api/v1/projects/{id}`:

- It must be absolute, with no control characters and no `..`.
- Neither it nor its parent (renders go to `<parent>/renders`) may be inside the
  data directory (the frames root is allowed), the config directory, or the
  token, key, certificate or database directories.
- It may not contain one of those directories, and may not be the frames root
  itself; use a sub-directory such as `<frames_root>/garden`.
- Its last component may not equal `render.output_subdir` (that directory holds
  rendered videos, and such a path would make its own render root equal its frame
  directory).
- It may not be a numbered direct child of the frames root other than the
  project's own id: that name is another project's default frame directory.
- It may not equal a path another project already uses, so two projects never
  write into one directory.
- Operators are further limited to `storage.allowed_roots`, which defaults to
  the frames root. Administrators are not.
- The resolved path (symlinks followed) is what is stored.
- The directory of the config file in use (from `--config` or `TLM_CONFIG`) is
  protected like the config directory.

Saving other settings without changing the path never re-checks it.

**Cloning.** A clone does not copy `storage_path`. It gets none and uses the
default layout (`<paths.frames_root>/<project_id>/`), so it can never write
into, or delete from, its source's directory. Set a path on the clone afterwards
if it needs one. Clones made by earlier versions are not rewritten (#171).

**Existing paths on upgrade.** At startup every project's stored path is
checked. One the rules refuse, and any path that two projects share, produces
a journal WARNING starting `storage_path policy: ...`. Nothing changes: capture,
renders and frame serving keep using it. To clear the warning, move the frames
and set an allowed path, or clear the field to return to the default layout.
A path that cannot be resolved (for example one holding a NUL character) is
skipped, not fatal.

---

## Relocatable storage

Frame paths stored in the database are kept **relative to the project's frame
directory** for default-layout projects (those with no `storage_path`). The
stored value is just the bare filename — `00000001.jpg` — re-anchored at read
time from the configured `frames_root` and the project's id.

**Consequence:** you can move the entire frames tree to a new location (new
path, new volume, new host) and update `paths.frames_root` without touching any
database row.

Projects with an explicit `storage_path` store **absolute** paths instead,
because the bare filename cannot be re-anchored from the project id alone.
Those projects are not auto-relocatable in the same way; moving their frame
directory requires updating `storage_path` on the project.

Any rows written before the relative-storage change was introduced also store
absolute paths and resolve correctly alongside newer relative ones.

---

## Disk-space safeguard

Timelapse Manager never deletes frames automatically to reclaim space. Instead,
capture **pauses** when free space on a project's storage volume drops below
a low watermark, and **resumes** automatically once space recovers above a
higher resume watermark.

### How it works

Each project's capture loop checks free space on the volume before every
capture attempt. The check is throttled (once per `check_interval_seconds`,
cached in between) so it adds negligible overhead even on short capture
intervals.

**Pause** is triggered by the conservative (OR) condition:

- free space drops below `low_watermark_bytes` **or**
- free space drops below `low_watermark_percent` of the volume's total

**Resume** requires both conditions to clear:

- free space rises above `resume_watermark_bytes` **and**
- free space rises above `resume_watermark_percent` of the volume's total

The gap between the low and resume watermarks is a hysteresis band. A volume
hovering at the threshold does not flap the gate on and off — once paused,
capture stays paused until space genuinely recovers.

### Composed with the schedule gate

The disk gate is evaluated **inside** an open schedule window. A closed window
is already a schedule pause; the disk check is skipped in that case to avoid
unnecessary I/O. The two pauses are tracked independently: a project paused by
the disk gate resumes automatically when disk recovers, regardless of schedule
state.

### Events

Pause and resume transitions are recorded in the project's event log:

| Transition | Level | Message |
|---|---|---|
| Free space drops below low watermark | `warning` | "capture paused for project … free disk space below the low watermark on …" |
| Free space recovers above resume watermark | `info` | "capture resumed for project … free disk space recovered above the resume watermark on …" |

Each project on a shared volume logs its own pause/resume event independently.
A sustained low-disk condition produces one warning event per project, not one
per capture cycle.

### Keep-all guarantee

The disk-space safeguard never removes files to reclaim space. Frames that
exist on disk are never touched by this mechanism — only new captures are held
back. This is by design: storage is append-only except through an explicit
administrator action (see [Frame management](#frame-management) below).

### Settings

See the [`storage` section](./configuration.md#storage) in the configuration
reference for all five watermark and interval settings, their defaults, and the
environment variable names.

---

## Storage sizing

There is no formula that fits all cameras — frame size depends on resolution,
compression, and scene content. As a rough planning guide:

- **JPEG at 1080p:** commonly 150 KB – 500 KB per frame depending on scene
  complexity and encoder settings.
- **Frames per day:** see [How the growth rate is worked out](#how-the-growth-rate-is-worked-out)
  (an always-open interval project takes `86400 / capture_interval_seconds`,
  e.g. every 5 minutes → 288 frames/day).
- **Daily volume (rough):** `avg_bytes_per_frame × frames_per_day`.
- **Project lifetime volume:** daily volume × number of days.

Add margin for OS overhead and a buffer well above the resume watermark. The
two watermarks together define the minimum free space the volume must be able
to sustain; it is good practice to size the volume so normal operation stays
comfortably above `resume_watermark_bytes`.

### How the growth rate is worked out

The **Growth Rate** on a project's card and detail page, the **Projected at
Completion** figure, and the storage pre-flight on the New Project form all
start from the same number: how many captures the project plans per day
(`capture/cadence.py::captures_per_day`). It is counted from the full schedule,
by the rules the capture runner fires by:

- **Exact-time captures** (clock, solar noon, sunrise, sunset) each fire once
  per local day, in their own timezone (the schedule timezone for clock times,
  the camera's location for solar ones). They ignore the capture window, sun
  window and weekday selection. A solar capture on a camera with no
  coordinates, or a sunrise or sunset that does not happen that day (polar
  day or night), fires nothing and counts 0.
  A failed attempt is retried inside the 30-minute grace window after 15, 30,
  60, 120, 240 and 480 s (at most 7 attempts, about 16 minutes; an
  authentication failure is retried once). While a retry is pending the
  fire-log row has status `retry_pending`. An anchor instant earlier than the
  project's creation or its last activation is not a missed fire and gets no
  fire-log row (see [Exact-time anchors and project
  activation](./monitoring.md#exact-time-anchors-and-project-activation)).
- **Interval captures** happen only while the capture schedule is open. Each
  time the window opens the runner takes a frame at once, then one every
  interval until it closes. A 08:00–18:00 window with a 1-minute interval is
  600 frames a day; a 12:00–12:30 window with a 1-hour interval is 1.

The count runs over the next **7 days** from now (or from the start date, if
that is later), ends early at the end date, and is divided by the real length
of that period in days. A campaign that ends in two days still shows its real
daily rate.

- **Growth Rate** = average size of the project's own frames × captures per
  day. It shows nothing until the project has captured a frame, when it plans
  no capture (finished, past its end date, nothing enabled), and while it is
  paused, paused for low disk, or stopped. A project only waiting for its
  capture window to open keeps its rate.
- **Projected at Completion** = captures per day × campaign length in days ×
  average frame size, capped by the maximum frame count.
- **Pre-flight** (New Project form) = captures per day for the form's interval,
  schedule and exact-time captures × a default 512 KB per frame, compared with
  the free space under `paths.frames_root`.

---

## Frame management

The API provides fine-grained lifecycle control over individual frames. All
mutating operations require a valid bearer token **and** currently require that
token to be the local admin token (role-based access control is planned but not
yet implemented). Every mutating operation is recorded in the project's event
log with the acting user.

### Lifecycle states

| State | Meaning |
|---|---|
| `active` | Normal frame, included in default listings and renders |
| `soft_deleted` | Hidden from default listings; file kept on disk; recoverable |

A frame moves between `active` and `soft_deleted` via the soft-delete and
restore operations. A frame can be **permanently deleted** — removing both the
database row and the file on disk — but that requires an explicit confirm flag
and is irreversible.

### Frame fields

Each frame object returned by the API includes:

| Field | Type | Description |
|---|---|---|
| `id` | integer | Database row id |
| `project_id` | integer | Owning project |
| `sequence_index` | integer | Position in capture-time order within the project. A new frame gets the next index; an import renumbers the frames from its earliest imported capture time onward, so imported photos interleave chronologically. After a single upload, a timestamp edit or a bulk offset, the order is repaired the same way if the change left it out of order (#144). A suffix over 5,000 rows is renumbered in chunks that release the database write lock between them (#145). |
| `capture_timestamp` | ISO-8601 string or null | When the image was taken |
| `file_path` | string or null | Stored path (relative for default-layout projects) |
| `width` | integer or null | Image width in pixels |
| `height` | integer or null | Image height in pixels |
| `file_size_bytes` | integer or null | File size on disk |
| `capture_status` | string | Capture outcome (e.g. `captured`) |
| `origin` | string | How the frame arrived: `captured` or `uploaded` |
| `lifecycle_state` | string | `active` or `soft_deleted` |
| `dimension_mismatch` | boolean | True when this frame's dimensions differ from the project's most-common frame size (computed at read time, never stored; false when there is no baseline or when dimensions are unknown) |

### API endpoints

All endpoints are under `/api/v1` and require `Authorization: Bearer <token>`.

#### List frames

```
GET /api/v1/frames
```

Query parameters:

| Parameter | Required | Default | Description |
|---|---|---|---|
| `project_id` | yes | — | Project to list frames for |
| `limit` | no | `100` | Page size (1–500) |
| `offset` | no | `0` | Number of frames to skip |
| `include_deleted` | no | `false` | Include soft-deleted frames |

Returns frames ordered by `capture_timestamp` ascending, then `sequence_index`
ascending as a stable tie-break. The `dimension_mismatch` flag is computed
consistently across all pages from the project's predominant frame size.

```json
[
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
]
```

#### Soft-delete a frame

```
POST /api/v1/projects/{project_id}/frames/{frame_id}/soft-delete
```

Sets `lifecycle_state` to `soft_deleted`. The file remains on disk; the action
is reversible with restore. Returns the updated frame object. Writes an audit
event at level `info`.

#### Restore a frame

```
POST /api/v1/projects/{project_id}/frames/{frame_id}/restore
```

Returns a soft-deleted frame to `active`. Returns the updated frame object.
Writes an audit event at level `info`.

#### Permanently delete a frame

```
POST /api/v1/projects/{project_id}/frames/{frame_id}/permanent-delete?confirm=true
```

**Irreversible.** Removes the database row and unlinks the file from disk.
`confirm=true` is required; omitting it or passing `confirm=false` returns
`422`. Returns `204 No Content` on success. Writes an audit event at level
`warning` recording the file path.

#### Edit a frame's capture timestamp

```
PATCH /api/v1/projects/{project_id}/frames/{frame_id}
```

Request body (only `capture_timestamp` is accepted; any other field returns
`422`):

```json
{"capture_timestamp": "2026-06-09T14:30:00Z"}
```

Returns the updated frame object. Writes an audit event recording the previous
and new timestamp values. When the new time puts the frame out of order, the
project's browse order is repaired after the commit, so the `sequence_index` in
this response can be stale; a failing repair is logged and never raised (#144).

#### Upload a frame

```
POST /api/v1/projects/{project_id}/frames/upload?capture_timestamp=<iso>
```

Imports an externally supplied image as a frame for the project. The image is
sent as the **raw request body** (no multipart/form-data). Only JPEG and PNG are
accepted; the format is detected from the magic bytes in the body — no
`Content-Type` header is required.

Query parameters:

| Parameter | Required | Description |
|---|---|---|
| `capture_timestamp` | yes | ISO-8601 timestamp to record as the frame's capture time |
| `format` | no | Declared format: `jpeg` or `png`. Must agree with the actual bytes if supplied; a mismatch returns `422`. |

The frame is stored through the same atomic writer as captured frames, with
`origin` set to `"uploaded"`. Returns the frame object.

Example:

```bash
TOKEN=$(cat ./data/.local-token)
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  --data-binary @/path/to/frame.jpg \
  "http://localhost:8080/api/v1/projects/1/frames/upload?capture_timestamp=2026-06-09T14%3A30%3A00Z"
```

The body is streamed to a spool file under `<data_dir>/tmp/` (never buffered in
memory) and removed afterwards. A body larger than `capture.max_image_bytes` is
refused with `413` as soon as it passes the limit, and `507` is returned when
free space would fall below `storage.low_watermark_bytes`. A body that is not a
valid JPEG or PNG is a `422`.

#### Import frames

```
POST /api/v1/projects/{project_id}/frames/import
```

Imports a batch of image files as uploaded frames. The body is
`multipart/form-data` with one `files` part per file, and an optional text field
`exif_timezone`. The request is streamed to disk, with the size and file-count
limits enforced on the bytes received, and the import runs on the background
jobs executor. Each file is validated and its Exif capture time is read; the
project is then re-sequenced chronologically. A file that is not a readable
supported image is reported in `skipped` with a reason and the rest still
import.

Capture time resolution, in order:

1. the image's own UTC offset (`OffsetTimeOriginal`, else `OffsetTime`), when
   valid; an unusable offset (blank, malformed, out of range) is ignored;
2. the `exif_timezone` field (an IANA name such as `America/New_York`);
3. the project's schedule time zone;
4. otherwise the time is kept as it is (naive UTC).

A file with no readable time uses the import time and is flagged as inferred.
Existing frames are not changed.

| Status | When |
|---|---|
| `200` | Processed; the body has `imported_count`, `skipped_count` and `skipped` |
| `400` | Malformed multipart body |
| `413` | A file over `capture.max_image_bytes`, or a body over the request limit (the whole request fails) |
| `422` | More than 200 files, no `files` part, or an unknown `exif_timezone` |
| `507` | Free space would fall below `storage.low_watermark_bytes` |

A no-JavaScript web form posts to the same handler with a session cookie; its
`csrf_token` field must be the first part (see
[Web UI](./web-ui.md#csrf-protection)). The bearer-token API needs none.

### Frame write durability and orphan files

A frame is written to a temporary file, fsynced and renamed into place; the
directory entry is then fsynced (a failure of that step is logged and does not
fail the write), and the `Frame` row is committed inside the project's write
lock. If anything fails before the commit completes, the file is removed again;
a failure after the commit never deletes it. A crash between the file write and
the commit leaves a file with no row (an *orphan*), never a row that points at a
missing file.

```sh
timelapse-manager frames find-orphans [--project ID] [--min-age SECONDS]
```

`frames find-orphans` is read-only and offline: it lists files in the frame
directories that no frame refers to, and leftover `*.tmp` files, and deletes
nothing. It prints one line per file, `<frame|temp> project=<id> size=<bytes>
<path>`, then `N orphan file(s), B bytes; nothing was deleted`, and exits 0 whether
or not it found any (1 if the database does not exist). `--project` limits it to
one project's directory; `--min-age` (default 60 seconds) skips files modified
more recently, so a capture in progress is not reported. Reclaiming an orphan is
manual.
