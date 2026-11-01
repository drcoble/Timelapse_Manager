# Problem reports

"Report a problem" lets anyone who notices something wrong in the web UI tell
the maintainers about it, without shell access to the host. When the report is
filed, the app captures a **redacted diagnostic snapshot** of its own state at
that moment and stores it with what the user wrote. Admins can then download
it, or have it delivered by email, by webhook, or as an issue in Gitea.

Problems are often first noticed by an operator, after which someone with shell
access gathers the evidence by hand, and a restart can destroy that evidence. A
report captures the evidence at the moment the problem is seen.

- [For users: filing a report](#for-users-filing-a-report)
- [For admins: the report list](#for-admins-the-report-list)
- [Configuration](#configuration)
- [What a snapshot contains](#what-a-snapshot-contains)
- [What is removed (redaction)](#what-is-removed-redaction)
- [Anonymized reports and the GitHub hand-off](#anonymized-reports-and-the-github-hand-off)
- [When the web UI is down: the CLI](#when-the-web-ui-is-down-the-cli)
- [API](#api)

## For users: filing a report

Any signed-in user can file a report, whatever their role.

1. Click **Report a problem**. The link is at the bottom of every page, and on
   the **About** page.
2. The form is filled in from the page you were on:
   - the **area** (capture, project page, renders, cameras, login/LDAP, other);
   - the **related project and camera**, if you opened the form from a project
     page or a project's frames.
3. Give it a **title**. Say **what happened and what you expected**.
4. Adjust **When** if the problem happened earlier. It can be up to 24 hours ago,
   in your own time zone.
5. Optionally attach a **screenshot**: PNG, JPEG, WebP or GIF, up to 10 MB.
   While the GitHub hand-off is on (the default) there is no screenshot field:
   see [Anonymized reports](#anonymized-reports-and-the-github-hand-off).
6. Leave **Include a diagnostic snapshot** ticked unless you have a reason not
   to. Open **What the snapshot contains** to see the list.
   Viewers can file reports, but only operators and administrators can attach
   the diagnostic snapshot; a viewer's report contains their own words and
   request context.
7. With the GitHub hand-off off, you may tick **Anonymize private data**.
8. Click **Send report**.

The app then shows a report id such as `TLM-20261023-0007`, and where the report
is being sent. Quote the id when you talk to the maintainers. Your last few
report ids are listed at the bottom of the form. An id is never issued twice: the
number after the date is one more than the highest that day has ever had,
including reports that were later deleted or pruned.

Each user can file 5 reports an hour. The description can be up to 20,000
bytes.

## For admins: the report list

Go to **Settings → Problem reports**, or open `/settings/reports` directly. Only
admins can see this list, and it includes reports from every user. Each row
shows:

- the report id, title, who filed it, the area and project, and when it was filed;
- an **Anonymized** badge when the report was filed anonymized: its zip, title
  and description are then the anonymized ones, and no un-anonymized copy
  exists;
- a status per destination (**Saved**, **Email**, **Webhook**, **Issue**) of
  `ok`, `pending` or `failed`. A failure shows its last error: an error class
  or an HTTP status, never a URL or a token.

The **⋯** menu on each row has these actions:

| Action | What it does |
|---|---|
| **Download** | The report zip. |
| **Resend** | Delivers the report again to one configured destination, or to all of them. |
| **Create issue** | Files the Gitea issue for a report that does not have one yet, for example a report filed before Gitea was configured, or one whose issue creation failed. |
| **Delete** | Removes the row and its zip. You confirm it inline first. |

A Gitea delivery that created the issue but failed to upload the zip keeps the
issue number. A resend then only uploads the zip, so it does not file a second
issue.

Resend, Create issue and Delete each write an audit event (Events → Audit).
Filing a report does too: `user 'x' filed problem report TLM-…`.

### Retention

Retention runs once an hour, from the render scheduler's housekeeping pass
(so only while `render.autostart` is on). It never runs when a report is
filed, so filing reports can never push anyone else's report out. Both limits
are configurable.

- Reports older than **90 days** are pruned, whatever their delivery state.
- Beyond the newest **50**, the oldest reports are pruned **only if their
  delivery finished** (`ok`). A report still pending or failed is kept for an
  admin to resend until it reaches the age limit.

Pruning removes both the row and the zip, and writes one audit event naming
the pruned report ids (`retention pruned N problem report(s): TLM-…`).

## Configuration

Reports are configured in the `reporting:` block. The full key list is in
[configuration.md](../configuration.md#reporting).

```yaml
reporting:
  enabled: true                       # local save is always on while enabled
  public_base_url: "https://timelapse.example.com"   # for download links
  email_to: ["maintainers@example.com"]               # optional
  webhook_url: "https://hooks.example.com/tlm"        # optional
  gitea:                                              # optional
    base_url: "https://git.example.com"
    repo: "owner/timelapse-manager"
    token_file: "/etc/timelapse-manager/report-gitea-token"
    labels: ["bug"]
  github:                                             # on by default
    enabled: true                                     # anonymize every report
    repo: "drcoble/Timelapse_Manager"
    labels: []                                        # no labels= parameter
```

### Destinations

Each destination is configured on its own.

- **Local save.** This is always on while `enabled` is true. Zips are stored in
  `<data_dir>/reports/<report id>.zip`, with mode `0640`, in a `0750` directory.
- **Email.** Mail is sent through the SMTP server configured under
  **Settings → Notifications**, to the addresses in `email_to`. The zip is
  attached when it is under `email_attach_max_bytes` (5 MB by default).
  Otherwise the email carries a download link, which needs `public_base_url`.
- **Webhook.** The webhook receives a JSON summary: the report id, title, area,
  reporter, project, times, version, download link and issue link. It does
  **not** receive the snapshot itself.
  - When a notification webhook signing secret is set, the request is signed
    with it, in the same `X-Signature-256: sha256=…` form as notification
    webhooks.
  - The URL passes the same SSRF guard as the notification webhooks. That guard
    blocks loopback, link-local and private addresses.
- **Gitea.** The app creates an issue in `repo`, then uploads the zip as an
  issue attachment.
  - The issue body has the user's text, a short summary (version, area, project
    and time), and the report id.
  - The token is read from `token_file` each time a report is sent. It is never
    stored in the database or in a report.
  - `base_url` comes from the config file, an operator-trusted source, so it is
    **not** run through the SSRF deny-list. A self-hosted Gitea usually has a
    private address.
  - Every request sends `User-Agent: timelapse-manager/<version>`. Some Gitea
    front ends reject the default Python user agent.

Deliveries run in the background after the report is saved, so a slow mail
server or Gitea cannot hold up the person filing the report. If a delivery
fails, the report stays saved and is flagged `failed` in the list, where an
admin can resend it.

### The Gitea token file

Use a **dedicated, low-privilege** token that only needs issue write access on
the one repository. Create the token as the Gitea repository owner, then:

```sh
sudo install -o timelapse -g timelapse -m 0600 /dev/null \
  /etc/timelapse-manager/report-gitea-token
sudoedit /etc/timelapse-manager/report-gitea-token   # paste the token, one line
sudo systemctl restart timelapse-manager
```

The service account (`timelapse`) must be able to read the file. Nobody else
should be able to. The app reads the token each time it sends, so a rotated
token takes effect without a restart. Changing `reporting:` itself does need a
restart.

The Gitea attachment upload only accepts `.zip` if the server's
`[attachment] ALLOWED_TYPES` includes it, which Gitea's default list does. The
upload is also limited by Gitea's `[attachment] MAX_SIZE`.

## What a snapshot contains

The snapshot is taken on the server, in-process, when the report is submitted.
A report zip contains:

| File | Contents |
|---|---|
| `report.json` | What the user wrote: title, description, area, when it happened, related project and camera, reporter's username and role. |
| `request.json` | The page URL (path and query only), the browser user agent, the username and role, and the ids of the reporter's last few requests. Their log lines carry the same `request_id`. |
| `app.json` | App version and build, database (Alembic) revision, ffmpeg version, PID, process start time and uptime, the host's OS and kernel, CPU count and load, memory, and free disk on the state and frames volumes. |
| `config.json` | The effective configuration, redacted. |
| `supervisor.json` | Every capture runner's state (including `last_cycle_at`), heartbeat age and last plan. Also the runner count against the active-project count, the event listeners, and when the last reconcile finished. |
| `diagnostics.json` | The [asyncio task dump](./debugging.md) (each task's innermost await), the default-executor statistics, every thread's stack, and event-loop lag. If the dump fails, this file says it is unavailable and the report is still filed. |
| `project.json` | For the related project: its settings (schedule, anchors, render settings), the latest 20 frames (metadata only), `exact_time_fire` rows from the last 7 days, `render_job` rows, and its last 100 events. |
| `camera.json` | For the related camera: its non-secret fields only. |
| `events.json` | The last 200 events across the whole system. |
| `log.txt` | The app's own log within ±30 minutes of the reported time, up to 2,000 lines. It is read from `logging.file_sink` when one is configured. Otherwise it comes from an in-memory ring buffer (`logging.ring_buffer_records`, 2,000 records by default), because the systemd sandbox may not let the service read the journal. |
| `attachment.<ext>` | The user's screenshot, byte for byte, when one was attached. It is always renamed: the user's file name is never used. |
| `manifest.json` | The file list and a note on redaction. |

If the user unticks **Include a diagnostic snapshot**, the zip holds only
`report.json`, `request.json`, the attachment and the manifest.

**No frame or image data** is ever included, except the user's own screenshot.

## What is removed (redaction)

Redaction is the same for every role. It happens in three layers.

1. **Allow-listed fields.**
   - Sections copy named fields, never whole credential rows.
   - Camera credentials are reported only as `has_credentials: true/false`.
   - URLs inside a project's post-render actions are cut down to the scheme and
     host.
2. **Pattern redaction.** This is the same `redact()` used by the logs, the
   event log and the debug endpoints. It masks, as `***`:
   - any value stored under a key that names a password, token, secret or key;
   - URL credentials (`rtsp://user:pass@host` becomes `rtsp://***@host`);
   - secret-looking query parameters (`?token=…`).
3. **Value scrub.** Before anything is zipped, the app collects the actual
   secret values it can see. It replaces each one with `«redacted»` wherever it
   appears in any file, raw, JSON-escaped or URL-encoded. This catches a secret
   that has leaked into an error message or a log line as plain text. The
   values collected are:
   - the LDAP bind password;
   - the SMTP password;
   - the webhook signing secret and every notification webhook URL;
   - every camera's stored credentials, and the default camera credentials;
   - the at-rest encryption (Fernet) key and its key file;
   - the local API token (`.local-token`);
   - the Gitea report token;
   - the password in the database URL;
   - the reporter's session cookie.

   The user's screenshot is not altered.

The redaction tests plant each of these secrets in the config, the database
and the log. They then check that none of them appears in the raw bytes of any
file in the zip. They do this for both the web path and the CLI path.

A report that is **not anonymized** can still contain operational detail:
host name, file paths, project and camera names, camera IP addresses,
usernames and log messages. Treat those zips as internal documents.

## Anonymized reports and the GitHub hand-off

A report can be **anonymized**, so it can be shared outside the install, and
handed to the public project on GitHub as an issue the user files from their
own GitHub account.

- **GitHub hand-off on (`reporting.github.enabled: true`, the default).**
  *Every* report is anonymized: the web form, the API and the CLI alike. The
  form has no tick box; it says the report will be anonymized and prepared as
  a public issue on `github.com/<repo>`. After filing, the result screen offers:
  - **Open on GitHub**: a prefilled
    `https://github.com/<repo>/issues/new?title=…&body=…` page, opened in a new
    tab. Nothing is posted until the user reviews it and clicks submit on
    GitHub, under their own account. The app never holds a GitHub credential
    and never calls GitHub.
  - **Download anonymized zip**, to drag into the GitHub issue if wanted
    (GitHub cannot take an attachment from a URL).
  - a notice that the issue is public and must be reviewed first.

  The issue body holds the anonymized description, the area, the app version
  and build, an anonymized environment summary (OS, Python, ffmpeg, database
  revision) and the report id. An audit event `report.github_prepared` is
  written when the screen is shown; whether the user then submits is not known.
- **GitHub hand-off off.** The form shows an optional **Anonymize private
  data** box. A ticked report is anonymized the same way, with no GitHub link.
  The CLI takes `--anonymize`; the API takes `"anonymize": true`.

In both cases:

- **The screenshot is left out.** An image cannot be anonymized. Attach one on
  GitHub by hand after checking it.
- **One zip.** The stored zip, title and description are the anonymized ones;
  no un-anonymized copy is kept. Email, webhook and Gitea deliveries send
  what is stored. That includes the reporter: the **Filed by** column and the
  reporter in the email, webhook and Gitea issue are their placeholder
  (`user-2`); the admin list adds the account id (`user #5`).
- `report.json` carries `"anonymized": true`, and `manifest.json` says so.
- **The reporter can download it.** `GET /report/<id>/download` serves an
  anonymized report's zip to the user who filed it and to admins (other users
  get `403`). For a report that is not anonymized it answers `404`; that zip
  stays admin-only.

  This is a deliberate trade-off. The zip holds the whole anonymized snapshot
  (see [What a snapshot contains](#what-a-snapshot-contains)): the effective
  configuration, the last 200 events from every scope, the app log window,
  runner and task state, and the related project and camera. A viewer or
  operator cannot see most of that in the app. The zip is what the reporter
  attaches to the **public** GitHub issue, so it must be safe to publish
  anyway; the result screen says so. The route only ever serves a report
  whose row is marked anonymized, so an un-anonymized zip can never leave
  through it.

### What anonymizing replaces

Anonymizing runs after the secret scrub above, over every file in the zip, the
title and description, and the issue text. Each value gets a stable
placeholder within one report (`user-1`, `project-2`, `camera-1`, `host-3`,
`ip-1`, `email-1`, `dn-1`, `/path/data`), so the log stays readable.

- **Values the install knows.** Usernames; project and camera names (database
  and config file); camera addresses, host names and the hosts in their URLs;
  this machine's host name and FQDN; the hosts in `public_base_url`, the
  webhook and Gitea settings, the LDAP servers and the SMTP server; mail
  addresses in the settings; LDAP bind and search DNs; every path the
  settings name (data, frames, database, log, key, token, TLS certificate and
  key, overlay font, …), each project's storage path and export destinations,
  the LDAP CA file, and the service account's home directory. A host name also
  covers its first label (`nas101`, unless short or generic) and its domain
  (`example.com`), any other host under that domain, and the domain's LDAP
  form (`dc=example,dc=com`).
- **Every absolute path.** Any path in a log line, event or error is replaced
  (`/path/3`) unless it is one of these, which name nothing private:
  - `/usr/{bin,sbin,lib,lib64,libexec,share}/`, `/bin/`, `/sbin/`, `/lib/`,
    `/lib64/`: distribution files (ffmpeg, the Python library);
  - `/opt/timelapse-manager/`: the packaged app, the same on every install;
  - `/proc/self/` and `/dev/null`;
  - a path with exactly the shape of one of the app's own URL paths: a route
    root, then only ids, report ids and the routes' own words
    (`/settings/reports`, `/projects/3/frames/12/thumbnail`). A directory that
    merely starts like a route (`/projects/AcmeCorp/out/x.mp4`) is replaced.

  `/usr/local`, `/etc/timelapse-manager` and `/tmp` are deliberately not on
  the list: names under them are the operator's. A path with a `.` or `..`
  segment is never on it. A path the install itself names (a font under
  `/usr/share/fonts/…`, say) is replaced even under an allowed directory.
  Below the data, frames, storage, export and log directories the app names
  files itself, so only names are replaced there
  (`/path/frames/2026/project-1_0001.jpg`). Every path-valued field
  (`*_path`, `*_dir`, `destination`, …) is replaced whole.

  Paths with spaces and punctuation: a quoted path (`'/srv/My Videos/in.mp4'`)
  is taken whole. Unquoted, a directory name may hold single spaces and
  `& ( ) , = # ?` as long as the path goes on with another `/`
  (`/mnt/nas media/Family Photos/2026/f.jpg` is one path; prose up to a later
  `/` may be swallowed with it, which is the safe direction). A path whose
  directories hold spaces runs to the end of its line (or closing quote),
  and a single word between any replaced path and the end of its line is
  taken with it. A route keeps its query (`/projects/3/frames?page=2`) only
  when the query holds no `/`.
- **Patterns, as a safety net.** IPv4 and IPv6 addresses (not loopback); MAC
  addresses with colons or dashes, Cisco-dotted (`0011.22aa.bbcc`) or bare
  (`001122aabbcc`, when it has both letters and digits) and hosts named after
  one (`axis-b8a44fb48e95`); e-mail addresses (Unicode domains too); the host
  of every URL, and everything before its last `@` (credentials); any LDAP
  `dc=` run; host names under `.lan`, `.local`, `.home`, `.internal`, `.corp`
  and `home.arpa`.
- **Camera locations.** Latitude and longitude fields are emptied.

Matches ignore case (with Unicode case folding, so `Straße` matches
`STRASSE`) and stop at letters and digits, so a project `Roof` is replaced in
`Roof_0001.jpg` but not inside `Roofline`. A name matches with any separator
between its words (`Garden Gate`, `Garden_Gate`, `GardenGate`, `garden-gate`, `Garden%20Gate`)
and in its JSON-, URL- and HTML-escaped forms (`Back &amp; Yard`). Where names
overlap, the longest wins (`Backyard Garden Cam` before `Back Yard`), and the
final check refuses a placeholder that, with the text after it, would spell a
longer known name.

**The reporter's own words.** In the title and description every replaced
value becomes a kind-less `name-1`, `name-2`, numbered apart from the
snapshot's placeholders. Someone typing guesses sees only *that* a word was
replaced, not whether it is a user, a camera or a host, and cannot line
`name-1` up with `user-3` in the zip. That a typed word is *some* name this
install knows is still visible, by design (the text must be anonymized and
shown back); each guess costs a report against the hourly limit.

### Fail closed

After anonymizing, the app scans the result again, in every file (values and
keys), the title and description, and the GitHub URL, for every known value
and every secret, and, independently of what the install knows, for any
leftover e-mail, MAC, IP address, home directory, private-TLD host, URL host,
URL credentials, `dc=` run or absolute path outside the list above. **Any hit,
any error while anonymizing, or any source of names that cannot be read (the
user table, the settings, …) refuses the report**: the form shows an inline error
(`422` for the API and a no-JS form, exit status `1` for the CLI) and nothing
is stored or delivered. The message never repeats the value. A refused report
still counts toward the reporter's hourly limit (it built a full snapshot): it
leaves a `report.refused` audit event holding only the report id and the error
code, and its id is never issued again.

### Short or generic names

A user called `admin`, or a camera called `Pi`, cannot be replaced in free
text without wrecking the log (`admin` is also a role; `Pi` is a word). So a
name is treated as **short or generic** when it is shorter than 3 characters,
all digits, or one of a small list of common words (`admin`, `root`, `user`,
`test`, `camera`, `default`, `system`, `server`, `localhost`, log levels and
the app's own states, …). Such a name is replaced only where it is a whole
structured value: a `username`, `name` or `hostname` field.

- **GitHub hand-off on:** if such a name still appears in free text (a log
  line, an event message, the title or description), the report is
  **refused**. An administrator's error names it and says what to do, for
  example:

  > This report can't be prepared for GitHub, so nothing was saved: the camera
  > name “Pi” (in the diagnostic snapshot) is too short or too common to
  > anonymize reliably, and would appear in the public issue. To fix it, an
  > administrator can rename that camera. You can also send the report
  > without the diagnostic snapshot. …

  A viewer or operator gets the **kinds only** ("A user name and a camera
  name on this server are too short or too generic …; ask an administrator
  to rename them"): naming the values would tell them other users' names. For
  a name in their own title or description not even the kind is given ("Your
  title or description contains a short or generic name this server knows").
  The service log records the refusal at `WARNING` with kinds and counts only
  (`refused for GitHub: short or generic 1 camera name`), never the value,
  which would otherwise land in the next report's log and refuse that one
  too. An administrator who files a report sees the names in the form error;
  so does the CLI (root).

  Only names that exist on this install count: current users (not the
  built-in API account `system`), projects, cameras (database and config
  file) and the configured hosts. And only when the name actually appears in
  this report's free text: a camera `Pi` that is only ever named in a
  `"name"` field files fine. Values that are app vocabulary (`"role":
  "admin"`, `"level": "error"`) are never counted.

  How to clear a refusal:
  - **Camera or project:** rename it to something more distinctive.
  - **User:** usernames cannot be changed in the app. Create an account with
    a more distinctive name and delete the old one; a directory (LDAP)
    account is renamed in the directory. Until then, untick **Include a
    diagnostic snapshot**, or keep the name out of the title and description.
  - **Host:** give the machine (or camera) a more distinctive host name.
  - Or turn the hand-off off (`reporting.github.enabled: false`) to file
    reports privately, with the **Anonymize private data** box available.

  On a real install the usual trigger is the first admin account being called
  `admin`: its name is in the audit events of everything it did, so every
  GitHub report with a snapshot is refused until that account is replaced.
- **Anonymize-only (hand-off off):** the report is not public, so short or
  generic names are left in free text and not checked.
- **CLI:** the `sudo` user's own name (`SUDO_USER`) is replaced, but when it is
  short or generic (a stock Raspberry Pi `pi`) it never refuses the bundle.

### What anonymizing cannot catch

Anonymizing is best effort, which is why the issue is reviewed by the user
before it is submitted.

- Free text that names nothing the install knows: a camera's own error
  message quoting its serial number or a location, a host name that is not in
  any setting and has no recognisable domain, a person's name in a project
  description.
- A short or generic name in free text, in anonymize-only filing (above).
- A bare 12-character MAC made only of digits (`001122334455`): it cannot be
  told from a 12-digit number such as a byte count, so it is left. MACs with
  colons, dashes or dots, and bare ones with letters, are replaced.
- In an unquoted path with no spaces in its directories, a last segment with
  spaces in mid-line keeps the words after the first space
  (`/srv/x/My File.jpg and more` keeps `File.jpg and more`).
- Whether a word the reporter typed is *some* name this install knows: it is
  replaced (with a kind-less `name-N`) and the reporter sees that. Each guess
  costs a report against the hourly limit.
- The screenshot, which is therefore always left out.

### Limits of the hand-off

- The encoded URL is kept to at most **7,000 characters**. A longer
  description is trimmed and the body says "Description truncated; paste the
  rest." The stored report keeps the whole (anonymized) description, which
  the reporter can copy from the downloaded zip's `report.json`.
- **No labels by default.** GitHub answers `404` to a prefilled `labels=`
  from a user who cannot label issues in the repository, which is every end
  user of a public project. `reporting.github.labels` exists for a repository
  where reporters can label; leave it empty otherwise.
- `repo` must be `owner/name` on github.com.

## When the web UI is down: the CLI

If the UI or login is broken (for example a hung login), build the same bundle on the host:

```sh
sudo env $(systemctl show -p Environment --value timelapse-manager) \
  /opt/timelapse-manager/timelapse-manager report \
  --title "Login page returns 500" \
  [--description "…"] [--area login] [--project 3] [--out /tmp/report.zip]
```

`sudo` drops the service's environment. The packaged unit sets `TLM_CONFIG`,
`TLM_PATHS__DATA_DIR` and `TLM_DATABASE__URL` with `Environment=` lines, and
`systemctl show` passes those through. If `/etc/timelapse-manager/timelapse-manager.env`
overrides paths, pass those variables as well. The command refuses to run when
the database file it resolves does not exist, instead of creating an empty one.

- It is **root-only** because it reads the state directory. It works with the
  service stopped.
- While it reads the state, it switches to the owner of the state directory,
  so any SQLite `-wal`/`-shm` file it causes is owned by the service account.
- It is **read-only on the state**. It adds no report row and writes nothing
  under the state directory. The zip goes to `--out` (default
  `./<report id>.zip`) with mode `0600`, and the report id has the form
  `TLM-YYYYMMDD-CLIhhmmss`. It is written to a temporary file and renamed
  into place, and the command refuses an `--out` that is a symlink or not a
  regular file, so a planted link cannot redirect the root write.
- The live sections (capture runners, task dump) are marked unavailable,
  because the CLI is not the service process.
- `log.txt` comes from `logging.file_sink` when one is configured. Otherwise
  use `journalctl -u timelapse-manager` alongside the bundle.
- Redaction is the same as for a web report. The CLI decrypts stored secrets
  only to scrub them, using the key file in the state directory.
- `--anonymize` anonymizes the bundle as described in
  [Anonymized reports](#anonymized-reports-and-the-github-hand-off). It is
  forced on while `reporting.github.enabled` is true. A bundle that cannot be
  proved clean is refused with exit status `1` and nothing is written.

To deliver the bundle, attach it to an issue by hand.

## API

`POST /api/v1/reports` files a report for scripted or monitoring use. It
needs the local bearer token, and the report is attributed to the sentinel
administrator. It takes no attachment.

```sh
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"capture lag over 60 s","area":"capture","project_id":3}' \
  http://127.0.0.1:8080/api/v1/reports
# {"report_id":"TLM-20261023-0004","saved":true,"delivering_to":["gitea"],
#  "anonymized":true,"github_issue_url":"https://github.com/…/issues/new?…"}
```

The response codes are:

| Code | Meaning |
|---|---|
| `201` | The report was filed. |
| `401` | The bearer token is missing or wrong. |
| `413` | The description is too long. |
| `422` | The input is invalid, for example a time outside the last 24 hours, or an anonymized report was refused (nothing stored). |
| `429` | The hourly limit has been reached. |
| `404` | Reporting is disabled. |
