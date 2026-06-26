# Scheduling & Reliability

Timelapse Manager can restrict capture to configurable time windows — by clock
time, by sunrise/sunset, or by day of week — while remaining resilient to camera
outages, application restarts, and edge cases like polar sunrise anomalies.
Projects with no schedule configured capture continuously at a fixed interval,
which is the default.

---

## The schedule model

A project's capture schedule is a JSON object stored on the project record. Every
field is optional; absent fields fall back to their defaults.

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | When `false`, the schedule is ignored and capture runs 24/7 (same as no schedule) |
| `timezone` | string | `"UTC"` | IANA timezone name used to interpret all wall-clock windows and day-of-week values |
| `windows` | array of objects | `[]` | Wall-clock capture windows (see below) |
| `sun_window` | array of two anchor objects | `null` | Sunrise/sunset-relative window (see below) |
| `day_of_week_mask` | integer 0–127 | `127` | Bitmask of allowed weekdays; bit 0 = Monday through bit 6 = Sunday |
| `start_date` | ISO-8601 datetime | `null` | Start of the capture campaign (inclusive); `null` = no start bound |
| `end_date` | ISO-8601 datetime | `null` | End of the capture campaign (exclusive); `null` = no end bound |

### Always-open default

**No schedule, an empty schedule object `{}`, or `"enabled": false` all produce
the same result: the gate is always open and capture runs at a fixed interval.**
A schedule only closes the gate when it is enabled and has at least one
constraint (a window, a sun window, a day-of-week restriction, or a date bound).

### Wall-clock windows

`windows` is a list of time-range objects. Each object has two fields:

| Field | Format | Description |
|---|---|---|
| `start_time` | `"HH:MM"` | Window opens at this local time (24-hour) |
| `end_time` | `"HH:MM"` | Window closes at this local time (24-hour, exclusive) |

The window is in the project's `timezone`. A window whose `end_time` is less
than or equal to `start_time` wraps past local midnight (e.g. `22:00`–`02:00`
spans 4 hours across the day boundary). If `start_time` equals `end_time` the
window is treated as spanning a full 24 hours.

Multiple windows may be listed; the gate is open inside **any** of them.

### Sun window

`sun_window` is a two-element array: `[open_anchor, close_anchor]`. Each anchor
is an object:

| Field | Values | Description |
|---|---|---|
| `anchor` | `"sunrise"` or `"sunset"` | The astronomical event to anchor to |
| `offset_minutes` | integer | Minutes relative to the event; negative = before, positive = after |

**Sun windows require the camera's geolocation** (latitude and longitude). The
geolocation can come from the camera device itself (VAPIX and ONVIF adapters
report it) or from a manual override set on the camera record. If no location is
available, the sun window evaluates as closed; clock windows in the same schedule
are unaffected.

At polar latitudes where the sun never rises or never sets on a given day, the
evaluator falls back to solar elevation at local noon to determine whether it is
polar day (gate open) or polar night (gate closed).

### Day-of-week mask

`day_of_week_mask` is an integer from 0 to 127 where each bit represents one
weekday: bit 0 (value 1) = Monday, bit 1 (value 2) = Tuesday, …, bit 6
(value 64) = Sunday. To allow a set of days, sum their bit values:

| Days | Mask value |
|---|---|
| Every day | `127` (default) |
| Monday through Friday | `31` (1+2+4+8+16) |
| Saturday and Sunday only | `96` (32+64) |
| Monday, Wednesday, Friday | `21` (1+4+16) |

### Gate semantics

At any instant the capture gate is open when **all** of the following hold:

1. `enabled` is `true`
2. The instant is within `[start_date, end_date)`, if bounds are set
3. The local weekday is allowed by `day_of_week_mask`
4. The instant falls inside **any** clock window **or** the sun window (if
   neither is configured this sub-condition is vacuously true — the gate is open
   for the whole allowed day)

All window boundaries are half-open: the gate is open **at** the start instant
and closed **at** the end instant.

---

## Worked example

A project capturing Monday–Friday during daylight hours (or at least from
06:00–20:00 local time), with a campaign that runs from 1 June to 1 September:

```json
{
  "enabled": true,
  "timezone": "America/Chicago",
  "windows": [
    {"start_time": "06:00", "end_time": "20:00"}
  ],
  "sun_window": [
    {"anchor": "sunrise", "offset_minutes": -30},
    {"anchor": "sunset",  "offset_minutes": 45}
  ],
  "day_of_week_mask": 31,
  "start_date": "2026-06-01T00:00:00-05:00",
  "end_date":   "2026-09-01T00:00:00-05:00"
}
```

With this schedule:
- On a weekday in June, if sunrise is at 05:48 local time, the sun window opens
  at 05:18 (30 minutes before sunrise) and the clock window also opens at 06:00.
  Because the gate is open inside **either**, capture starts at 05:18.
- On a Saturday or Sunday the gate is closed all day (mask bit for those days
  is not set).
- After 1 September the campaign has ended and the gate stays permanently closed.

### Setting the schedule

Schedule editing via the web UI is not yet implemented. For now, the schedule
JSON can be written directly to the `schedule` column on the `project` table in
SQLite using the `sqlite3` CLI or any SQLite client. An API endpoint for setting
and updating project schedules is planned.

---

## Reliability behaviors

### Schedule-driven timing

Inside an open window, the supervisor captures frames at the project's configured
interval. Outside an open window the loop sleeps, waking at or before the next
window boundary. The sleep is capped by `capture.max_idle_sleep_seconds`
(default: 300 seconds) so the loop re-evaluates at least every few minutes — this
allows it to stay responsive to schedule changes and daylight-saving transitions
without waking every second. A long capture interval (longer than 300 seconds)
is unaffected: the loop still delivers exactly one capture per interval when the
gate is open.

### Reconnect with exponential backoff

When a capture fails (camera unreachable, network timeout, protocol error), the
supervisor does not retry immediately. It backs off using capped exponential
backoff with jitter:

- First retry delay: `capture.backoff_base_seconds` (default: `1.0` second)
- Each subsequent failure doubles the delay
- Delay is capped at `capture.backoff_max_seconds` (default: `300` seconds)
- Each delay is randomly scaled within ±`capture.backoff_jitter_fraction`
  (default: `0.1`, i.e. ±10%) so that multiple failing cameras do not retry
  in lockstep
- The first successful capture resets the backoff counter

The schedule gate is always authoritative: even if a backoff retry is due, the
supervisor will not capture outside an open window.

### Restart and downtime survival

When the application restarts, each project's capture loop resumes automatically.
The loop reads the timestamp of the project's most recent frame to detect any
downtime gap. If the gap is meaningful (more than approximately twice the project's
capture interval), an informational event is logged so the gap is visible in the
event log. Capture always resumes forward from the next sequence index; frames
that would have been captured during the downtime are **never synthesized** and
existing frames are **never overwritten**.

### Frozen-frame detection

After each successful capture, the supervisor hashes the image bytes. If
`capture.frozen_frame_enabled` is `true` (the default) and the same hash appears
`capture.frozen_frame_threshold` times in a row (default: 5 consecutive identical
frames), a warning event is logged for the project. The counter then resets, so
the next identical sequence must again reach the threshold before another warning
fires. **Capture is never stopped for a frozen camera** — when the camera
recovers and begins returning new frames, the loop continues normally.

---

## Related settings

All `capture.*` settings are described in the [configuration reference](./configuration.md#capture).
The settings most relevant to scheduling and reliability are:

| Setting | Default | Purpose |
|---|---|---|
| `capture.max_idle_sleep_seconds` | `300.0` | Maximum seconds the loop sleeps between re-evaluations |
| `capture.backoff_base_seconds` | `1.0` | First retry delay after a capture failure |
| `capture.backoff_max_seconds` | `300.0` | Cap on retry delay |
| `capture.backoff_jitter_fraction` | `0.1` | Fraction of delay to randomize (±10%) |
| `capture.frozen_frame_enabled` | `true` | Enable frozen-frame detection |
| `capture.frozen_frame_threshold` | `5` | Consecutive identical frames before a warning |
