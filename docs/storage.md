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
- **Frames per day:** `86400 / capture_interval_seconds` (e.g. every 5 minutes
  → 288 frames/day).
- **Daily volume (rough):** `avg_bytes_per_frame × frames_per_day`.
- **Project lifetime volume:** daily volume × number of days.

Add margin for OS overhead and a buffer well above the resume watermark. The
two watermarks together define the minimum free space the volume must be able
to sustain; it is good practice to size the volume so normal operation stays
comfortably above `resume_watermark_bytes`.

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
| `sequence_index` | integer | Monotonically increasing write order |
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
and new timestamp values.

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
