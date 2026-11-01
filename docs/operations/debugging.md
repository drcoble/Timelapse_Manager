# Debugging a stalled or misbehaving process

This runbook is for a running `timelapse-manager` that is behaving wrongly. For
example: captures stop arriving while `systemctl is-active` still says
`active`, the web UI hangs, or CPU is pinned. The goal is to **collect evidence
before restarting**. A restart clears the state that explains the fault. That
is how a past stall lost its evidence.

Work through the steps in order. Each one is safe to run on a live production
service. None of them stop the process or change captured data.

> **Version warning.** Before **0.1.1**, `SIGUSR1` terminated the process.
> Before **0.2.0**, `SIGUSR2` terminated the process. Check the version first
> (`/healthz` → `app_version`, or the startup banner in the journal). Never send
> either signal to an older build.

The examples assume the packaged install: unit `timelapse-manager`, state in
`/var/lib/timelapse-manager`, and HTTP on port 8080. Adjust them if your install
differs.

```sh
PID="$(systemctl show -p MainPID --value timelapse-manager)"
TOKEN="$(sudo cat /var/lib/timelapse-manager/.local-token)"
API=http://127.0.0.1:8080/api/v1
```

## 1. Capture the runner and task state

### With the debug endpoints enabled (preferred)

Turn the endpoints on with `observability.debug_endpoints_enabled: true` (see
[Configuration](#configuration)). They are admin-only. The local token above is
an admin credential.

```sh
curl -s -H "Authorization: Bearer $TOKEN" "$API/system/debug/runners" > runners.json
curl -s -H "Authorization: Bearer $TOKEN" "$API/system/debug/tasks"   > tasks.json
curl -s -H "Authorization: Bearer $TOKEN" "$API/system/debug/threads" > threads.json
```

- **`runners`** returns one entry per capture runner. Each entry has:
  - every `CaptureState` field;
  - `last_cycle_at` and `last_cycle_age_seconds`, the heartbeat. A healthy
    runner's age stays below `capture.max_idle_sleep_seconds` plus one capture;
  - `restarts`, the number of wedge relaunches;
  - the last `plan`: the decision, `sleep_seconds`, `next_wake_at`,
    `next_change` and `next_anchor_wake`;
  - `latest_exact_time_fire`, the newest fire-log row, including `attempts`
    and `next_attempt_at` (set while a retry is pending).
- **`listeners`** (a sibling of `runners` in the same response) returns one entry
  per camera event listener (projects with event triggers). Each entry has
  `project_id`, `project_name`, `camera_id`, `task_name`, `task_done`,
  `transport` (`vapix-websocket` or `onvif-pullpoint`), `connected`,
  `connected_since`, `connected_for_seconds`, `last_event_at`,
  `last_progress_age_seconds`, `last_error` (redacted),
  `consecutive_failures`, `restarts` (relaunches of a dead listener),
  `dropped_events` (matched events dropped because the queue of 8 was full),
  `downgrades` (WebSocket failures that fell back to PullPoint) and
  `events_unavailable` (whether the camera's `events_unavailable` alert stands).
  Times are ages in seconds, not raw clock readings. "Subscribed and quiet" shows
  as `connected` with an old `last_event_at`; "never subscribed" as not
  `connected`. See [event-triggered capture](../cameras.md#event-triggered-capture).
- **`tasks`** returns every asyncio task by name. Each entry has:
  - `state`;
  - `awaiting`, the innermost await, for example `asyncio.tasks.sleep` with its
    delay, `asyncio.locks.Lock.acquire`, `asyncio.locks.Event.wait`, or
    `asyncio.threads.to_thread` with the function name;
  - `awaited_from`, the application line that is waiting;
  - the full `await_chain` and `stack`.

  The response also includes the thread-pool statistics and the event-loop
  lag (the last tick, and the worst tick of the last minute). `executors` lists
  each pool (`default`, `tlm-capture`, `tlm-jobs`) with `max_workers`, `active`
  and `queue_depth`; `executor` is the default pool alone, kept for existing
  readers. `active` is an exact count of work items in flight for `tlm-capture`
  and `tlm-jobs`. For the default pool it is an **estimate** derived from
  CPython's idle-worker bookkeeping, which can overstate busy workers after a
  burst, so that entry also carries the same number as `active_estimate`; read
  `queue_depth` and the thread stacks (step 2) before concluding the pool is
  busy.
- **`threads`** returns every thread's Python stack, innermost frame last.

Read responses are cached for one second.

**If `tasks` hangs, the event loop itself is blocked.** That is a finding in
its own right. Go to step 2.

The log also names database work done on the event loop, once per origin. A
request does so as `database query on the event loop in <function> (request
<id>)`. Background work, including startup, does so as `database work on the
event loop in <module:coroutine> (task <name>)`. Either one stalls every
request while it runs.

### Without the endpoints: `SIGUSR2`

Send the asyncio task dump to the journal:

```sh
sudo kill -USR2 "$PID"
```

The dump is logged at WARNING, so it is visible at any configured level:

- one `asyncio task name=… state=… awaiting=… at=… stack=…` line per task;
- one `<pool> executor … active=… queue_depth=…` line per pool (`default`,
  `tlm-capture`, `tlm-jobs`); the default pool's reads `active=N (estimate)`;
- one `thread ident=… native_id=… name=…` line per live thread. `SIGUSR1`
  (step 2) prints thread idents only, so this map, or `GET
  /api/v1/system/debug/threads`, is how you name them;
- a `capture runner project=… age=…s decision=… next_wake_at=…` line per
  runner.

With `logging.format: json`, each line also carries the full structured entry
under `diag`. The dump is bracketed by `asyncio task dump begin` / `end`.

The dump runs on the event loop. **If nothing appears, the loop is blocked.**
Go to step 2.

Task names to expect:

| Task | What it is |
|---|---|
| `https-server`, `http-server` | The two Uvicorn listeners |
| `capture-reconcile` | Supervisor reconcile tick (launches, stops, relaunches runners) |
| `capture-project-{id}` | One interval/anchor capture runner per project |
| `capture-listener-{id}` | One camera event listener per project with event triggers |
| `render-worker`, `render-job-{id}`, `render-export-{id}` | Render queue drain loop, each running render or export job, and an export's archive-building thread |
| `render-scheduler` | Recurring render scheduler |
| `notification-dispatcher`, `notification-send-{event id}` | Notification poll loop and each in-flight delivery |
| `loop-lag` | 1 s ticker behind `tlm_event_loop_lag_seconds` |
| `log-level-revert` | Present while a log-level override is active |

## 2. Thread stacks: `SIGUSR1`

```sh
sudo kill -USR1 "$PID"
```

`faulthandler` writes every thread's Python stack to stderr, which is the
journal. It runs from a C signal handler, so **it works even when the event
loop is blocked**. This is the step that shows what the default-executor
workers (`asyncio.to_thread`) are stuck in, such as a SQLite lock, a camera
socket, or a disk write.

`faulthandler` labels each stack `Thread 0x… (most recent call first)` with the
thread's ident in hex, not its name. To name a thread, convert the hex to
decimal (`printf '%d' 0x7f2c…`) and look it up in the `thread ident=…` lines of
the `SIGUSR2` dump, or read the names from `GET /api/v1/system/debug/threads`.

> Signal the main PID, as shown above, rather than using a plain
> `systemctl kill -s SIGUSR1 timelapse-manager`. By default, `systemctl kill`
> signals every process in the unit's cgroup, including running `ffmpeg`
> children (renders, RTSP grabs). For those processes, `SIGUSR1`/`SIGUSR2`
> mean terminate. If you do use `systemctl kill`, add `--kill-whom=main`.

## 3. Native and Python stacks: `py-spy` (optional)

[py-spy](https://github.com/benfred/py-spy) reads the process from outside, so
it works when the process cannot respond to anything. It is **not** shipped in
the bundle. Install it on the host only when you need it, for example
`pipx install py-spy` or a static release binary.

```sh
sudo py-spy dump --pid "$PID"
sudo py-spy dump --pid "$PID" --native   # include C frames (SQLite, OpenSSL, libc waits)
```

- **Run it as root.** The service runs under `NoNewPrivileges` and
  `ProtectSystem=strict` as a dedicated user, and reading another process's
  memory needs ptrace rights.
- **If the dump shows no Python frames**, the PID may be the PyInstaller
  bootloader. Dump its child as well:
  `for p in $(pgrep -P "$PID"); do sudo py-spy dump --pid "$p"; done`.
- **py-spy depends on the bundle's unstripped `libpython` symbols.** The
  PyInstaller spec keeps `strip=False` and `upx=False` for that reason. The
  release CI runs a non-blocking `py-spy dump` against every built bundle to
  catch a regression.

## 4. Collect the journal

```sh
journalctl -u timelapse-manager --since "-2h" --no-pager > journal.txt
```

Widen `--since` to cover the start of the problem, for example the last frame
time shown by `runners.json`. Include the dumps from steps 1–3.

## 5. Only then restart

```sh
sudo systemctl restart timelapse-manager
```

Attach the files from steps 1–4 to the issue.

## Turning up logging without a restart

`logging.level` is read once at startup. To raise one logger temporarily,
use the following request. It requires the debug endpoints.

```sh
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"level":"DEBUG","logger":"timelapse_manager.capture","minutes":30}' \
  "$API/system/debug/log-level"
```

- **`logger`** must be `timelapse_manager` or one of its children. The default
  is `timelapse_manager`.
- **`level`** is one of `DEBUG`, `INFO`, `WARNING` or `ERROR`.
- **`minutes`** is between 1 and 240. The default is 30.

The override reverts on its own when it expires. It also reverts on shutdown,
and it never touches the config file. Posting again for the same logger
replaces the level and the expiry but still reverts to the original level.
Each change is recorded in the audit log.

At `DEBUG`, `timelapse_manager.capture` logs these lines:

- one `runner cycle project=… open=… disk_ok=… next_change=… next_anchor_wake=…
  decision=… sleep=…` line per runner cycle;
- an `anchor evaluation project=… anchors=… due=…` line on every pass;
- an `anchor decision project=… anchor=… status=… claim=…` line for each due
  anchor.

None of these appear at `INFO`.

## Metrics for alerting

With `observability.metrics_enabled: true`, the admin-only `GET /api/v1/metrics`
also exposes the following stall-detection series:

| Series | Meaning | Suggested alarm |
|---|---|---|
| `tlm_runner_last_cycle_age_seconds{project,project_id}` | Seconds since the runner's last loop cycle | `> 600` |
| `tlm_runner_state{project,project_id,state}` | One-hot runner state (`running`, `idle`, `error`, `stopped`) | `state="error"` for long periods |
| `tlm_executor_queue_depth` | Work queued on the default executor | Sustained `> 0` |
| `tlm_executor_threads` | Default-executor worker threads | At the pool maximum together with queue depth |
| `tlm_event_loop_lag_seconds` | Worst lateness of the 1 s `loop-lag` tick over the last minute | `> 1` |

`/healthz` reports `checks.event_loop_lag` as `degraded` by a stricter rule than
this alarm: the last three ticks were each more than 1 s late, or the latest tick
was 5 s or more late. A blocked loop yields one late tick however long it
blocks, which is why a single tick of 5 s or more counts (#117).

## Configuration

```yaml
observability:
  metrics_enabled: false          # GET /api/v1/metrics
  debug_endpoints_enabled: false  # /api/v1/system/debug/...
```

The equivalent environment variables are
`TLM_OBSERVABILITY__METRICS_ENABLED` and
`TLM_OBSERVABILITY__DEBUG_ENDPOINTS_ENABLED`.

While disabled, the routes answer `404`. While enabled, they need an
administrator session or the local bearer token, and the dumps exist only in
memory and in the journal. The signals need no configuration: `SIGUSR1` is
installed at startup, and `SIGUSR2` once the server's event loop is running.

## What is safe to share in an issue

| Artifact | Safe to share? |
|---|---|
| `debug/runners`, `debug/tasks`, `debug/threads`, `SIGUSR1`/`SIGUSR2` dumps, `py-spy dump` output | **Yes.** These contain code locations, task and thread names, state values and timestamps. They never dump local variables. The only values read are a sleep delay and the name of the function a `to_thread` call is running. URL credentials in error text are redacted (`rtsp://***@host`). |
| `/metrics` output | **Yes.** Project names appear as labels. |
| `journalctl` output | **Usually.** Log records are redacted (URL userinfo, `token=`/`password=` query values, secret-named fields). Still skim it for camera hostnames or project names you would rather not publish. |
| `config.yml`, `timelapse-manager.env` | **Only after review.** These may hold a database URL with credentials and environment overrides. |
| `.local-token`, the database, the key file, `py-spy dump --locals` output | **Never.** The token is an admin credential. The database holds encrypted LDAP/SMTP/camera secrets. `--locals` can print secrets held in memory. |
