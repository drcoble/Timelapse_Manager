# Monitoring & Notifications

Timelapse Manager records structured events as it runs and can forward them to
external channels (email, webhook) based on configurable routing rules.

---

## Event log

Every significant operation — a capture gap, a camera reconnect, a render
completing, a storage warning, a user logging in — is written to the event log
as a row in the database. Each event has:

| Field | Description |
|---|---|
| `scope` | `"system"`, `"camera"`, or `"project"` |
| `scope_id` | Integer id of the scoped entity; `null` for system-scope events |
| `level` | `"info"`, `"warning"`, `"error"`, or `"critical"` |
| `message` | Human-readable description (secrets redacted before storage) |
| `type` | Optional dotted event-type identifier (stored in the event's JSON details) |
| `actor_user_id` | Set for events triggered by a user action; `null` for system/operational events |

### Event types

The following event types are defined:

| Type | Meaning |
|---|---|
| `capture.gap` | A restart gap was detected after a downtime |
| `capture.stalled` | Capture has not produced a frame within the expected window |
| `camera.reconnect` | The capture engine reconnected to a camera after an error |
| `camera.offline_threshold` | A camera has been unreachable for a sustained period |
| `storage.disk_low` | Free disk space dropped below the low watermark |
| `render.complete` | A render job finished successfully |
| `render.failed` | A render job failed |
| `render.cancelled` | A user cancelled a render (cancel button, or delete of a running render); `info` level, so it never raises an active alert or matches a `render.failed` rule |
| `render.requeued` | A clean service stop interrupted an encode and put it back to `pending` (once); it runs again at the next start. `info` level, so it never raises an active alert or matches a `render.failed` rule |
| `postaction.failed` | A post-render action (export, webhook, prune) failed |
| `security.auth_event` | A login, logout, or authentication failure |
| `audit.control_action` | An administrator performed a control action (settings change, user management, etc.) |
| `notify.delivery_failed` | Notification delivery to a channel failed after all retry attempts |

`notify.delivery_failed` is **never routed to a notification channel** — it is
recorded in the event log only. This prevents a failing channel from generating
an infinite cascade of new delivery attempts.

### Secret redaction

Event messages and metadata are redacted before being written to the database.
Credentials embedded in URLs (userinfo format), passwords, and other
secret-looking keys are replaced with a placeholder. No credential ever reaches
the event log.

---

## Active alerts

The event log doubles as the backing store for an **active-alerts** view: the
outstanding operational conditions an operator needs to act on, surfaced without
a separate table.

### What counts as an alert

An event is an **active alert** when:

- its `level` is at or above the alert threshold (`warning` by default — so
  `warning`, `error`, and `critical`), **and**
- it has not been cleared (`alert_cleared_at` is `NULL`).

The definition is **level-primary, not type-primary**. Most alertable conditions
(low disk, a frozen camera, a camera gone offline) are recorded as events that
carry only a `level` and a `reason` and have no event type, so a type filter
would miss them. The event type may be layered on as an optional filter later,
but it is never required for an event to be an alert.

### Clearing never deletes

Clearing an alert only sets three columns on the event row — the row itself is
never deleted, so the operational log stays complete:

| Column | Meaning |
|---|---|
| `alert_cleared_at` | When the alert left the active list. `NULL` = active. |
| `alert_cleared_by` | The user who manually cleared it. `NULL` for an auto-clear. |
| `alert_clear_reason` | `"manual"`, `"auto"` or `"project_deleted"`; `NULL` while active. |

Operators (and admins) can **clear one** alert by id or **clear all** active
alerts; each manual clear is attributed to the acting user. Clearing an
already-cleared alert, a non-alert (info-level) event, or an unknown id is a
no-op that reports zero cleared — it is never an error.

Deleting a project clears its open project-scoped alerts. They stay in the event history with
clear reason `project_deleted` and no clearing user, so they leave the Active
alerts badge instead of pointing at a project that no longer exists (#282).

Old events can be pruned only by the opt-in `monitoring.event_retention`, which
is off by default.

`GET /api/v1/alerts` takes a `limit` of 1 to 500 (default 100); a value outside
that range is a 422.

### Auto-clear on resolve

Some conditions emit a natural **resolve** signal once they recover. When such a
signal is observed, the matching active alerts for the **same scope** are cleared
automatically with `alert_clear_reason = "auto"` (and no `alert_cleared_by`). A
later recurrence simply logs a fresh event, re-raising the alert — a cleared
alert is never un-cleared.

Matching is by the `reason` marker plus `scope` + `scope_id`:

| Resolve signal (`reason`, `info` level) | Clears active alerts with `reason` |
|---|---|
| `disk_recovered` | `low_disk` |
| `camera_recovered` | `camera_offline` |
| `anchor_recovered` | `anchor_failed` |
| `events_available` | `events_unavailable` |

An exact-time anchor that still fails after its retries raises `anchor_failed`
(warning, project scope); the next successful capture of that anchor, on a
later day, emits `anchor_recovered`. Each failed attempt that will be retried
logs an `anchor_retry` info event.

#### Exact-time anchors and project activation

An anchor instant earlier than the project's creation, or earlier than the last
time it became active (created, resumed, started or reactivated), is not a missed
fire: no fire-log row and no alert is written for it, so a new or resumed
project no longer starts with a "missed anchor" warning for a time before it
existed or while it was paused. A miss while the project was active is still
recorded and alerted. The project's `active_since` time is kept in the database
(migration 041); an upgraded project counts from its creation time until it is
next activated (#280).

A camera's `camera_offline` alert (warning, camera scope) is raised when one of
its projects has failed `offline_failure_threshold` capture attempts in a row
with no success on the camera in between, or at once when the camera rejects the
credentials. A failure that is the server's own (a database lock, a full
connection pool, a credential that cannot be decrypted, no free capture slot)
does not count; the first of a streak logs an info `capture_local_error` system
event. A PTZ move the camera rejects is the project's problem and does not count
either. Deleting a camera clears its `camera_offline` alert.

A camera whose event subscription cannot be established raises
`events_unavailable` (warning, camera scope): after
`capture.offline_failure_threshold` consecutive subscribe failures of one event
listener, one alert is raised for the camera, even when several projects share
it. The next successful subscribe, once no listener on that camera is failing,
writes the info event `events_available`, which clears it; removing the camera's
last listener resolves it too. It is a separate track from `camera_offline`: it
never marks the camera offline, and a refused listener login does not raise
`camera_offline`. See [event-triggered capture](./cameras.md#event-triggered-capture).

A resolve event clears only alerts raised before it. A camera that goes offline,
recovers and goes offline again keeps its second alert.

`camera_offline` and `camera_recovered` are **camera-scoped**: health is tracked
per camera, so a camera shared by several projects raises one alert, and the
alert names the camera and its active projects. Every capture outcome on the
camera feeds it: interval captures, each exact-time anchor attempt (counted once,
after any retry is scheduled or finalised) and event-triggered captures. A
camera whose projects are all anchor-only can therefore go offline and recover.
An authentication failure raises the alert at once with `auth_failed`. Local
errors (a database error, a camera that no longer exists) do not count against
the camera; they write one system info event per streak. `anchor_failed` and
`anchor_recovered` stay project-scoped and describe one anchor. On upgrade,
active project-scoped `camera_offline` alerts from earlier versions are cleared
once and one info event records it. The Alerts panel shows "camera <name>" or
"project <name>" with a link.

Conditions with no natural recovery signal — a **frozen camera** and a **failed
render** — have no auto-clear entry: they stay active until cleared manually.

Two properties are essential to how auto-clear works:

- **Resolve signals are `info` level**, below the alert threshold. The evaluator
  therefore inspects *every* new event regardless of level — a level filter
  would never see a resolve and nothing would ever auto-clear.
- The evaluator runs inside the **notification dispatcher's poll** over persisted
  event rows. That poll is the one place that sees every event from both write
  paths (the typed `log_event` helper and the capture engine's untyped event
  writes) and runs once per new event, gated by the poll's high-water mark, so a
  resolve auto-clears exactly once. Auto-clear is independent of notification
  routing: an event routed to no channel still resolves its alert.

---

## Web UI: event views

Three surfaces expose event data in the web interface. All require an active
login.

### Events page — `/events`

Operational events visible to **any signed-in user** (both Admin and Viewer
roles). The page shows capture, camera, storage, render, and post-action events.

Audit and security events (`security.auth_event`, `audit.control_action`) are
**excluded** from this view — they appear only in the audit log below.

**Filter options (query parameters):**

| Parameter | Values | Default |
|---|---|---|
| `level` | `info`, `warning`, `error`, `critical` | all levels |
| `scope` | `system`, `camera`, `project` | all scopes |
| `at` | ISO-8601 date and time. Without an offset it is read in your display time zone | none (newest first) |
| `page` | integer ≥ 1 | `1` |

50 events per page, newest first.

### Audit log — `/events/audit`

**Admin-only.** Shows only security and control-action events
(`security.auth_event`, `audit.control_action`). A non-admin request receives
`403 Forbidden`.

**Filter options:**

| Parameter | Values | Default |
|---|---|---|
| `level` | `info`, `warning`, `error`, `critical` | all levels |
| `page` | integer ≥ 1 | `1` |

50 events per page, newest first.

### Status banner — `/partials/status`

A lazy HTMX partial loaded into every page after the initial render. It queries
the total count of `error`-and-above events in the log (no time window — total
across all history) and surfaces the count and the most recent message when at
least one such event exists.

The query is failure-isolated: if the status banner query fails for any reason,
it degrades to a blank (healthy) banner rather than failing the page.

---

## Notification dispatcher

A background asyncio task polls the event log for rows newer than a high-water
mark and fans matching events out to configured channels.

### Delivery semantics

- **Persisted delivery cursor.** The dispatcher records how far it has
  delivered in the singleton `notification_cursor` table (migration 036). Every
  event at or below the cursor has settled: delivered, failed after retries,
  debounced or routed nowhere. On the first start after the upgrade there is no
  row, and the dispatcher creates it at the newest event, so no history is
  replayed. A missing table or a cursor beyond the newest event (a restored
  older database) starts from the newest event.
- **Catch-up on start.** Events logged while the dispatcher was stopped, and
  sends cut off by shutdown, are delivered on the next start, provided they are
  younger than `monitoring.catchup_max_age_seconds` (default 3600). Older ones
  are skipped, with one INFO log line giving the count.
- **At-least-once delivery.** A rare duplicate email or webhook after a crash
  is preferred to a lost alert.
- **Bounded concurrency.** Sends share `monitoring.max_concurrent_sends`
  (default 4) slots, held per send attempt and not across retry backoff.
- **Bounded retry.** Each delivery attempt is wrapped in a per-send timeout
  (`channel_send_timeout_seconds`). On failure, the dispatcher retries up to
  `max_retries` total attempts (the first attempt counts toward this limit) with
  exponential backoff plus jitter. After all attempts fail, a
  `notify.delivery_failed` event is written to the log; that event is never
  itself routed to a channel.
- **Debounce / flap suppression.** A notification for the same
  `(event_type, scope, scope_id)` key is suppressed per channel if a
  notification for that key was sent within `debounce_window_seconds`. The
  suppression window resets after it elapses. The dispatcher reserves the key
  before it sends, so two events with the same key dispatched concurrently send
  once; the reservation is released if the send fails or is cancelled, and is
  recorded again after a successful send in case a prune dropped it during a long
  send (#149).
- **The dispatcher never crashes the application.** A channel fault is caught,
  retried, and — on final failure — recorded in the log. Errors at every layer
  are contained.

### Configuration live-reloading

Routing rules are re-read from the database on every poll cycle. A change saved
in the notification settings UI takes effect on the next poll without a restart.

SMTP and webhook settings are picked up by the dispatcher on its next poll
(within `monitoring.poll_interval_seconds`); no restart is needed. A failed
rebuild keeps the current channels and is retried on the next poll.

---

## Channels

Two outbound channels are available: **email** (SMTP) and **webhook** (HTTP
POST). Both are identified in routing rules by their short names.

### Email (`email`)

Delivers an event as a plain-text email via SMTP. Requires a server, a from
address, and at least one recipient; a partially configured SMTP setup yields no
channel rather than one that always fails.

**Security modes** (set the `smtp_security` field):

| Mode | Behavior |
|---|---|
| `none` | Connects in cleartext (suitable for a local relay) |
| `starttls` | Connects in cleartext then upgrades with STARTTLS |
| `tls` | Connects with implicit TLS (SMTPS) |

**Port defaults** when not set explicitly: `465` for `tls`; `587` for `none`
and `starttls`.

Saving the notification settings with any other `smtp_security` value, or none,
is rejected: the form comes back with a 422 and nothing is written (#214).

The SMTP password is held in memory for the channel's lifetime and is never
written to any log line. The blocking `smtplib` call runs in a worker thread so
it does not block the event loop; a socket timeout matching
`channel_send_timeout_seconds` bounds the connection.

**Email format:**

Subject: `[Timelapse Manager] <LEVEL>: <event_type>`

Body:
```
<message>
Type: <event_type>
Level: <level>
Scope: <scope> (id <scope_id>)
Time: <timestamp> UTC
```

### Webhook (`webhook`)

POSTs a JSON body to one or more configured URLs.

- Redirects are never followed.
- A per-request timeout matching `channel_send_timeout_seconds` is always set.
- Each target URL passes through the SSRF guard. Its DNS lookup runs in a worker
  thread and an allowed answer is cached for 60 s. The connection goes only to
  the address the guard validated, so a DNS-rebinding answer cannot redirect it;
  TLS SNI and the `Host` header keep the hostname.
- Environment proxy variables (`HTTP(S)_PROXY`) are ignored for webhooks. The CA
  bundle from `SSL_CERT_FILE` is still honoured.
- A non-2xx response or transport failure raises a recoverable error; the
  dispatcher retries with backoff.
- No credential embedded in a URL is written to any log line.

**JSON payload:**

```json
{
  "event_type": "camera.offline_threshold",
  "level": "error",
  "scope": "camera",
  "scope_id": 3,
  "message": "Camera has been unreachable for 30 minutes.",
  "timestamp": "2026-06-09T14:30:00",
  "metadata": {}
}
```

| Field | Type | Description |
|---|---|---|
| `event_type` | string | Dotted event-type identifier; empty string when none |
| `level` | string | `"info"`, `"warning"`, `"error"`, or `"critical"` |
| `scope` | string | `"system"`, `"camera"`, or `"project"` |
| `scope_id` | integer or null | Id of the scoped entity; null for system-scope events |
| `message` | string | Human-readable event description (already redacted) |
| `timestamp` | string | ISO-8601 naive UTC (no timezone suffix) |
| `metadata` | object or null | Free-form event details (already redacted); null when none |

---

## Routing rules

Routing rules determine which channels receive which events. Rules are stored as
a JSON array in the notification settings and are re-read on every dispatcher
poll cycle.

Each rule is an object with three keys:

| Key | Type | Description |
|---|---|---|
| `event_types` | array of strings | Event type identifiers this rule matches, or `["all"]` to match any type |
| `min_level` | string | Minimum severity required (`"info"`, `"warning"`, `"error"`, `"critical"`). A rule with no `min_level` matches all severities |
| `channels` | array of strings | Channel names to deliver to when the rule matches (`"email"`, `"webhook"`) |

A rule matches when **both** conditions hold: the event type is in
`event_types` (or `event_types` is `["all"]`), and the event level is at or
above `min_level`. The channels of all matching rules are unioned.

**Example — email on any error, webhook on storage events only:**

```json
[
  {
    "event_types": ["all"],
    "min_level": "error",
    "channels": ["email"]
  },
  {
    "event_types": ["storage.disk_low"],
    "min_level": "warning",
    "channels": ["webhook"]
  }
]
```

An empty rule list (or no rules configured) means no notifications are sent;
events are still written to the event log.

---

## Notification settings (Admin only)

The notification settings form is at **`/notification-settings`** and is
accessible only to Admin users.

It configures:

- **Enabled channels** — which channels (`email`, `webhook`) are active
- **SMTP settings** — server, port, security mode, username, password, from
  address, recipients (one per line in the form)
- **Webhook URLs** — one per line; delivered to for every event that matches a
  rule targeting the `webhook` channel
- **Routing rules** — entered as a JSON array in the form

### SMTP password masking

The SMTP password is **write-only** in the UI:

- When a password is stored, the settings form displays `***` in the password
  field, not the real value.
- **Saving the form with the field unchanged** (or blank) leaves the stored
  password intact — the `***` sentinel and blank values are both treated as
  "keep the stored secret."
- Only submitting a genuinely new, non-empty value overwrites the stored
  password.
- **Changing the SMTP target clears it.** If the host, port, security mode or
  username changes and the password field is blank (or `***`), the stored
  password is cleared rather than sent to the new target; enter the password in
  the same save. The same rule applies to the LDAP bind password when the
  servers, TLS mode, CA certificate path or bind DN change (see
  [Web UI](./web-ui.md#login-and-logout)).

The SMTP password and the webhook signing secret are encrypted at rest
(`enc:v1:` Fernet tokens) with the instance key, like camera and LDAP
credentials. See [Installation](./installation.md#upgrading-to-020) for the
startup key-integrity check and the `secrets` commands.

---

## Dispatcher runtime config (`monitoring` section)

These settings control the dispatcher's poll cadence and delivery behavior.
They are set in the config file or via environment variables, not in the web UI.
See [docs/configuration.md](./configuration.md#monitoring) for the full table
and env-var reference.

| Setting | Default | Description |
|---|---|---|
| `autostart` | `true` | Whether the dispatcher's poll loop starts with the service |
| `poll_interval_seconds` | `5.0` | How often the dispatcher polls for new events (seconds) |
| `max_retries` | `3` | Total delivery attempts per channel before giving up (includes the first attempt) |
| `retry_backoff_seconds` | `1.0` | Base delay between retries; grows exponentially per attempt, plus jitter |
| `debounce_window_seconds` | `60.0` | Per-channel suppression window (seconds) for repeated `(event_type, scope, scope_id)` notifications |
| `channel_send_timeout_seconds` | `10.0` | Hard ceiling (seconds) on a single channel send |
| `catchup_max_age_seconds` | `3600` | On startup, undelivered events younger than this are delivered; older ones are skipped and counted in one log line |
| `max_concurrent_sends` | `4` | Most notification sends in flight at once |
