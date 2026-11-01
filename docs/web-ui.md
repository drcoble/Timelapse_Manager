# Web UI Reference

Timelapse Manager includes a server-rendered web interface built on Jinja2
templates and [HTMX](https://htmx.org/) for partial page updates. No separate
front-end build step is needed. The UI is the primary human interface for
managing cameras, projects, frames, renders, users, and settings.

---

## Accessing the UI

The web UI is served exclusively over **HTTPS**. Open a browser at:

```
https://<host>:8443
```

The default HTTPS port is `8443`; change it with `server.https_port` in your
config or with the `TLM_SERVER__HTTPS_PORT` environment variable.

An HTTP listener also binds on port `8080` (configurable via `server.http_port`)
and redirects every browser request to HTTPS with a `308 Permanent Redirect`. The
redirect preserves the HTTP method and body, so a form POST that lands on the
plaintext port is replayed on HTTPS without data loss.

**Redirect exemptions:** `/api/v1/` routes and `/healthz` are never redirected.
The CLI talks to the local JSON API over loopback HTTP using a bearer token; a
`308` redirect would strip the `Authorization` header on the cross-scheme hop and
break the CLI. Liveness probes must also remain reachable over plain HTTP.

---

## First-run setup

On a fresh installation, **no administrator account exists**. There are no
default credentials. Before the UI can be used for anything else, the
first-run gate middleware redirects every request to `/first-run`, where you
create the initial admin account by entering a username and password (minimum 12
characters by default; see `auth.password_min_length`).

Once the form is submitted successfully you are signed in automatically and
redirected to the dashboard. The `/first-run` page refuses to create a second
admin if one already exists, so the route cannot be replayed to add unauthorized
accounts.

The form also asks for a **one-time setup code**, so only someone who can read
the server's files can create the first administrator. The service writes it
to `setup-code` in its data directory (`/var/lib/timelapse-manager/setup-code`
on a package install, `/data/setup-code` in Docker) the first time the page is
opened, and logs the path (`journalctl -u timelapse-manager`). Get it with:

    sudo timelapse-manager setup-code                     # package install
    docker compose exec app timelapse setup-code          # Docker

or `sudo cat /var/lib/timelapse-manager/setup-code`. The command creates the
code if the page has not been opened yet. The file is deleted once the
administrator is created. A wrong or missing code is refused with 403.

Until first-run setup is complete, the following paths remain reachable without
an account: `/first-run` (the setup form), `/static/` (CSS and JS assets),
`/healthz` (readiness probe; the full body only from this host), and `/api/` (the CLI bearer-token API). Everything
else is redirected to `/first-run`.

The gate fails safe. While the setup state cannot be read (a database error),
`GET` and `HEAD` requests pass and every other method is refused with `503`
(`setup state unavailable`). Once an administrator exists the process stops
querying: that fact is latched for the life of the process.

`GET /first-run` and `GET /login` set a random `tlm_presession` cookie
(`HttpOnly`, `SameSite=Strict`; `__Host-tlm_presession` over HTTPS) and put a
token derived from it in the form's hidden `csrf_token` field. The `POST` must
present a token that verifies against that cookie, or it is refused with 403 and
the form is shown again with "Please try again". This closes login CSRF: a
hostile page cannot sign a visitor in as the attacker. A script that POSTs
`/login` without loading the form first is refused.

---

## Authentication

### Login and logout

Sign in at `/login` with a local username and password. On success a session
cookie is set and you are redirected to the dashboard. Sign out via the user
menu in the top-right corner (the sign-out button is a POST form protected by a
CSRF token).

**Brute-force protection:** failed login attempts are counted per source IP and
per submitted username within a sliding window. When either count reaches the
configured threshold the source is throttled. A successful login clears the
counter, so a genuine user is not penalised for earlier typos. The default window
is 5 minutes and the threshold is 5 failures (see `auth.throttle_window_seconds`
and `auth.throttle_max_failures`).

After several failed sign-ins for a username, the next attempt takes a little
longer (up to 30 seconds). If the directory server is down, the page says so;
local accounts can still sign in. After a directory outage the login page keeps
answering "directory unavailable" for up to `auth.ldap_breaker_seconds`
(default 60) before the directory is asked again.

**Directory (LDAP) settings.** New LDAP configurations default to TLS mode
`ldaps`; an unknown mode is rejected and nothing is saved. Administrators see a
warning in the status banner while LDAP is enabled with TLS mode `none`. Changing
the server URLs, the TLS mode, the CA certificate path or the bind DN with a
blank bind-password field clears the stored bind password (the form then says "Re-enter the bind
password"); the same rule applies to the SMTP host, port, security mode and
username in the notification settings. Test connection uses the saved settings
only. An account whose `auth.ldap_lock_attribute` (default `nsAccountLock`) is
`TRUE` in the directory cannot sign in, and its live session is revoked at the
next directory re-check; the bind account must be allowed to read that
attribute. Saving the LDAP settings with directory authentication off revokes
every directory session in the same transaction, including your own if you
signed in through the directory (#216). Keep one local administrator and make
that change from it: a local login never depends on the directory.

### Roles

Three roles are enforced by the web UI:

| Role | Access |
|---|---|
| **Admin** | Full control: everything an operator can do, plus managing user accounts (create, edit role, reset passwords, disable/enable, delete), revoking sessions, and the settings and notification-settings pages. |
| **Operator** | Mutates the operational surface: creating/editing/deleting cameras, managing projects (create, edit, clone, lifecycle, delete), triggering renders, and editing/restoring/deleting frames — plus all read operations. Cannot touch user accounts or system settings (`403`). |
| **Viewer** | Read-only: dashboard, project list and detail, camera list, frame browser, and render list. Mutating actions are blocked with a `403`. |

Authorization is deny-by-default: a route admits only the roles listed on it;
any other authenticated role is rejected with `403`.

The local JSON API (`/api/v1/`) uses a separate bearer-token mechanism (see the
HTTP API section in the README) and is not governed by these roles. It is
bearer-only: a valid bearer token is always required, and a session cookie alone
is refused with `401`. A cookie is honoured there only together with a valid
bearer, and then the cookie's user and role apply and a CSRF token is required.

---

## Sessions

Sessions are stored server-side. Only a high-entropy random token (never the
session data itself) is placed in the browser cookie. The token is hashed with
SHA-256 before being persisted; a database leak therefore exposes no usable
session credential.

### Cookie attributes

The session cookie is set with:
- **Name** — `__Host-tlm_session` (`__Host-` plus `session.cookie_name`) when the
  connection is effectively HTTPS, otherwise `tlm_session`. A browser refuses to
  let a sibling subdomain set or overwrite a `__Host-` cookie.
- `HttpOnly` — not accessible from JavaScript
- `Secure` — set only when the connection is effectively HTTPS (direct TLS or
  via a trusted `X-Forwarded-Proto` header from a reverse proxy). The name and
  `Secure` come from the same decision, so a prefixed cookie is never sent
  without `Secure`.
- `SameSite` — default `lax` (configurable via `session.samesite`)
- `Path=/`, and no `Domain` attribute

**The old name.** Over HTTPS only `__Host-tlm_session` is read; the unprefixed
`tlm_session` is not looked at, so a session left under it (from before 0.2.0)
signs in once and a cookie planted under that name by another site is ignored.
Plain HTTP and `session.host_prefix: false` use the unprefixed name. Tabs opened
before the upgrade need one reload for a fresh CSRF token.

The security cookies are read from the raw `Cookie` header with exact name
matching. A duplicated cookie, or one whose name is not an exact match, is
treated as absent and nothing authenticates. The browser is signed out until its
cookies for the parent domain (for example `*.example.com`) are cleared; signing
in again does not help. No access is gained.

**Redirect targets.** `next` on the login page, the problem report's `page` and
`from`, and the `HX-Current-URL` header go through one rule: the value must start
with exactly one `/`; a `//host`, a backslash, a control character, a space, a
non-ASCII character, a `scheme:` form, a `.` or `..` segment, a `/partials/` or
`/alerts/` path, or a value over 2048 characters (500 for the report form) is
refused, and so is any of these after one round of percent-decoding. A refused
target is ignored. `HX-Current-URL` is trusted only when it
is `http(s)` with no host or exactly this request's `Host`.

### Session lifetime

A session can expire in two independent ways:

- **Idle timeout** — if a session has not been accessed for longer than
  `session.idle_timeout_seconds` (default 30 minutes / 1800 s), it expires.
  This applies to all sessions, including persistent ones.
- **Creation-anchored cap** — a session also expires once it is older than a
  fixed cap measured from creation:
  - Regular sessions: `session.absolute_timeout_seconds` (default 24 hours /
    86400 s).
  - "Remember me" sessions: `session.persistent_timeout_seconds` (default
    30 days / 2592000 s). A persistent session receives a `max_age` cookie
    attribute so it survives a browser restart.

The **"remember me"** checkbox on the login form selects the persistent cap.
Without it the cookie is a session cookie (no `max_age`) and is subject to the
shorter absolute cap.

**Idle sign-out and background refreshes.** The pages that refresh themselves
(the alerts badge, the dashboard, the renders list, the new-frames and
new-events pills) do not count as activity. A page left open with no keyboard,
mouse, touch, scroll or focus input signs out after the idle timeout, and
sign-in returns you to the same page. This holds on the dashboard too: the time
ribbons that a dashboard refresh brings in are passive as well (a `load`-triggered
element inside a refreshed region is), so they no longer keep an unattended
dashboard signed in (#278). Refreshing pauses while the browser tab is
hidden and runs once when you return to it. For a wall display that should stay
signed in, set `session.keepalive_on_poll: true`. The dashboard refresh is
cheap when nothing changed: the server answers an empty 204 and the page keeps
what it shows, including every card's time ribbon (a ribbon reloads when its
frames or campaign dates change, and at least hourly while the campaign runs).

### Session rotation and revocation

- **On login**, the previous session token is revoked and a new one is minted
  (session fixation mitigation).
- **On logout**, the session is revoked server-side and the cookie is cleared.
- **On password change**, all sessions for the affected user are revoked
  simultaneously, so a stolen token cannot survive a credential change.
- An admin can also revoke all sessions for any user from the Users page.
- Session rows that were revoked or expired more than 7 days ago are purged by
  the hourly housekeeping.
- A request resolves its session once and shares the answer between the CSRF
  layer and the endpoint. After a request body has been read (a long upload), the
  endpoint re-checks live that the session is still unrevoked, the user is still
  enabled and the role is still allowed, so a revoke, disable or demotion that
  lands mid-upload takes effect before the request acts.

---

## CSRF protection

All state-changing requests from cookie-authenticated sessions require a valid
CSRF synchronizer token. Safe methods (`GET`, `HEAD`, `OPTIONS`, `TRACE`) are
exempt.

Each session has a CSRF secret, which is generated when the session is created
and rotates whenever a login rotation occurs. The page never carries the raw
secret: every response embeds a masked token, a fresh random mask over the
secret, so the value differs from one response to the next and its length is
constant (BREACH hardening). The application embeds the token in a
`<meta name="csrf-token">` tag in the `<head>`. `js/csrf.js` reads that tag at
request time and sends it as `X-CSRF-Token` on every HTMX request, including
background polls.

Form submissions also include a hidden `csrf_token` field. The CSRF middleware
accepts the token from either the `X-CSRF-Token` header (the HTMX path) or the
`csrf_token` form field. It accepts only a masked token; the bare session secret
is refused, so a page rendered before 0.2.0 needs one reload.

A request whose `Sec-Fetch-Site` header is `cross-site` or `same-site` is
refused with 403 (`CSRF validation failed.`) before the token is checked. The
service is served from one origin, and a sibling site on the same domain is not
that origin. Browsers set the header themselves and page script cannot change
it. `same-origin`, `none` and an absent header (an older browser, the CLI) go
on to the token check. If the session lookup itself fails, the check denies the
request.

**Token rotation.** The token changes meaning only when the session changes,
for example when another tab signs out and in again. Open pages notice on their
next alerts refresh: the server answers with a `tlm:csrf` event carrying the new
token, and the page updates the meta tag and every `csrf_token` field. A form
also copies the current token into its field as it is submitted, so dashboard
card actions keep working without a reload. A request already in flight when
the token rotates can still get the 403 and the "Your session changed in
another tab" toast with its Reload button. Nothing is retried, because a
mutation is never replayed; use Reload and repeat the action.

Bearer-token (CLI) requests carry no session cookie and are entirely exempt from
CSRF checks; there is no ambient credential for an attacker to ride.

A cookie-bearing request whose session has expired is also treated as exempt,
but the sign-in forms have their own protection: a `POST` to `/login` or
`/first-run` must carry the per-visit token described under
[First-run setup](#first-run-setup).

The frame import form posts `multipart/form-data`. Its `csrf_token` must be the
**first** part: the middleware reads only that part, with the same parser the
route uses, before any file is spooled, and a later duplicate `csrf_token` is
ignored. The built-in form does this; a custom no-JavaScript client must too. A
bearer-token (CLI) request needs none.

---

## Content-Security-Policy

Every response, including the 500 page, carries the same policy. Its script
rule is `script-src 'self'`; `style-src` is `'self' 'unsafe-inline'`. There is
no `'unsafe-inline'` or `'unsafe-eval'` for scripts: every script is a file under
`static/js`, no template carries an inline `<script>` or an `on*=` attribute, and
htmx is configured with `allowEval: false` and `allowScriptTags: false`, so a
swapped fragment cannot run script. The theme preference and the viewer's time
zone reach the page as `data-theme-pref` and `data-viewer-tz` attributes on
`<html>`, read by `theme-init.js` (blocking, in `<head>`) and `timezone.js`.

A customised template must not add an inline `<script>` or an `on*=` handler.
New behaviour registers a `data-action` in `static/js/actions.js` and is
attached with `data-action="..."` on the element. A contract test
(`tests/web/test_frontend_contracts.py`) fails on any inline script, handler or
`<style>` in the templates.

---

## Built-in TLS

### Certificate resolution

The service manages TLS directly; no external terminator is required. On startup
it resolves the TLS certificate and key in this order:

1. **Explicit pair** — if `tls.cert_path` and `tls.key_path` are both set and
   both files exist, that pair is used as-is.
2. **Auto-generate** — otherwise, if `tls.auto_generate` is `true` (the
   default), a self-signed RSA-2048 certificate is generated into the data
   directory (`data_dir/tls-cert.pem` and `data_dir/tls-key.pem`) on first
   start, and reused on subsequent starts. The private key is written with
   owner-only permissions (`0600`).
3. **Error** — if neither a usable pair nor auto-generation is available, the
   service refuses to start.

### Self-signed certificate limitations

The auto-generated certificate is valid for `localhost`, `127.0.0.1`, and `::1`
only. It is suitable for a single-host installation where you access the UI via
`localhost`. For any other hostname, or for a public-facing deployment, supply
your own certificate via `tls.cert_path` and `tls.key_path`.

Browsers will show a security warning for the self-signed certificate. In a
development or local environment you can add a one-time exception. In production,
replace it with a CA-signed certificate or use a reverse proxy that terminates
TLS and sets `X-Forwarded-Proto: https`.

### Running behind a reverse proxy

If a TLS-terminating reverse proxy (nginx, Caddy, Traefik, etc.) sits in front
of the application, configure the proxy to set the `X-Forwarded-Proto: https`
header. The application uses this header to determine the effective scheme,
which controls:

- Whether the session cookie is issued with the `Secure` attribute, and whether
  its name is the `__Host-` form (`__Host-tlm_session`). The proxy must be listed
  in `server.trusted_proxies`; otherwise the app sees plain HTTP and keeps the
  unprefixed, non-`Secure` cookie.
- Whether the HTTP-to-HTTPS redirect fires (exempting requests already marked
  as HTTPS by the proxy).

In this mode, bind the HTTPS listener to loopback only (or disable it) and let
the proxy handle the public TLS. The local JSON API and `/healthz` remain
reachable over plain HTTP regardless.

The app does not compress responses. Enable gzip or brotli for `text/css`,
`application/javascript` and `application/json` at the proxy (owner action O1).
Do not override the app's `Cache-Control` headers.

---

## Caching

Pages and JSON responses are sent `Cache-Control: no-store`, so the browser
keeps no copy of signed-in content. Back after sign-out goes to the login page.
Frame images and thumbnails are cached privately for 24 hours. Thumbnails come
in two widths: 320 px for the frame grid and 640 px for dashboard cards and the
project header. They are generated on first view and kept under
`<data_dir>/thumbnails/<project id>/`. When the server is busy generating
others (`render.thumbnail_concurrency`), the full-size frame is shown for that
one view. Static files
have content-hashed URLs (`?v=...`) and are cached for a year. A new release
changes the URLs, so no cache clear is needed after an upgrade.

---

## Errors and connection problems

- Failed actions show a toast at the bottom right. Error toasts stay until
  dismissed.
- A server error quotes its request id; include it in a problem report.
- While the server is unreachable, a "Connection lost - retrying" banner shows
  at the top of the page; it clears by itself once the server answers.
- After "Your session changed in another tab", use Reload. It appears when
  an action is sent with a CSRF token from before the session changed, which
  can also happen to a request that was in flight at that moment.
- Requests time out after 30 s. Discovery, Query camera and the LDAP test wait
  up to 5 min; camera location reads wait up to 45 s.
- An action button is disabled while its request runs, so a double click runs
  it once. Offset, Undo and Export also send an `Idempotency-Key`; the server
  replays the first result for a repeated key (kept for 10 minutes, per user).
  Plain forms lock on the first submit, and a form or button marked
  `data-allow-repeat` is exempt.
- The frame export status keeps polling across a page reload. It stops after
  three failed checks in a row, at once when the session has ended, and after
  30 minutes; the export itself keeps running and is listed under Exports on
  the project's Frames tab.
- Without JavaScript, a camera's Delete submits the row form as a GET to
  `/cameras/{id}/delete-confirm`, a full page that asks first and names the
  camera; nothing is deleted by that request, and the token is not in the URL.
  Its Delete button posts to `/cameras/{id}/delete` and returns to the Cameras
  page with a banner: "Camera deleted.", or "not deleted: it is used by N
  project(s)". With JavaScript the row asks with `hx-confirm` and deletes in
  place. A camera that is already gone returns to the Cameras page with the "no
  longer exists" banner. (#123)
- Only one administrator read ("Read with admin account") per camera runs at
  a time; a second one reports that a read is already running. The typed
  password is cleared after every attempt, including a failed one.

---

## Pages

| URL | Roles | Description |
|---|---|---|
| `/first-run` | None | Initial admin setup (only reachable before an admin exists) |
| `/login` | None | Sign in |
| `/` or `/dashboard` | Any | Project status cards, live capture state |
| `/projects` | Any | Project list |
| `/projects/{id}` | Any | Project detail: recent renders (Renders tab) and frame exports (Frames tab) |
| `/cameras` | Any | Registered cameras list |
| `/frames?project_id={id}` | Any | Paginated frame browser for a project |
| `/renders` | Any | Global render queue (renders only; exports are on each project's Frames tab) |
| `/renders/{id}/card` | Any | Render card (details; Play/Download; Cancel/Delete for operators) |
| `/frames/export/{id}/card` | Any | Frame-export card (details; Download/Delete for operators) |
| `/renders/{id}/download` | Any | Download a completed render |
| `/renders/{id}/stream` | Any | Stream a completed H.264/MP4 render |
| `/settings` | Admin | View current configuration (read-only display) |
| `/users` | Admin | Manage local accounts |

"Any" means any authenticated user (Admin or Viewer). Unauthenticated requests
to all authenticated pages are redirected to `/login`.

The settings page displays current configuration values but does not apply
changes — configuration is resolved at startup from files and environment
variables and is not hot-reloaded via the UI.

### Projects page

`/projects` lists each project's name, camera, status, frame count and capture
interval. The **Interval** column shows the interval in the largest whole unit
(`30s`, `15m`, `2h`, `1d`) and `—` when the project has no interval. There is no
Actions column: click anywhere on a row to open the project. The name is a real
link, so Ctrl/Cmd-click or middle-click on it opens a new tab and keyboard users
reach it with Tab and Enter. Selecting text in a row does not open the project.
Camera and project rows share one script, `row-click.js`.

### Cameras page

`/cameras` lists the registered cameras. An operator or administrator clicks a
row, or presses Enter on its name, to open that camera's edit form. The row's
`⋯` menu still offers Edit, Test connection and Delete, and selecting text in a
row does not open the camera. Viewers see the list read-only, and their rows
are not clickable.

### Renders page

The Renders page shows the 50 most recent renders, newest first. **Load more**,
below the list, adds the next 50 older renders. While any render is queued or
running, the list refreshes every 10 seconds. When nothing is queued or running
it stops refreshing, so an idle Renders tab sends no requests. It refreshes once
when you switch back to the tab, which shows a render started elsewhere (for
example by a schedule). Reload the page to see newly started renders straight
away.

### Frames page

`/frames` loads 60 frames at a time and appends the next batch as you scroll.
After 10 batches (600 tiles) it stops appending and ends the grid with a
"Jump further back" link, so a long scroll cannot slow the page down. The link
reloads the grid from the oldest loaded frame: with `?at=<capture time of the
oldest loaded frame, ISO with offset>` for one project, or with
`?before=<cursor>` for All Projects (or when the oldest frame has no time).
Off-screen tiles skip layout, and Remove and Restore are buttons that post
from the tile (the page's CSRF header is added by `csrf.js`); without
JavaScript a tile is the thumbnail link to the frame's detail page, which has
its own forms. Choosing All Projects in the project picker loads the global grid. A batch of
60 tiles is kept small: the grid sets the htmx target and push-url once, and a
tile carries only what it needs (#198). The "N new frames" pill counts frames
newer than the newest frame seen since the page loaded. The baseline only grows:
loading an older batch or jumping to an older window does not lower it, so the
pill never counts frames the page already had (#199, #262).

**Frame card and drawer.** Clicking anywhere on a frame card opens that frame's
drawer, which shows the frame, its timestamp edit and its scene metadata (a
collapsed Scene metadata section, open when there is metadata). The selection
checkbox and Remove/Restore keep their own actions and do not open the drawer.
There is no separate Info button or metadata panel below the grid.

**Row-action menus.** In a table, a row's open actions menu is placed
`position: fixed` so the table's edge does not clip it, and flips upward when
there is no room below. Without JavaScript the menu keeps its old placement and
opens on focus (#210).

**Editing a frame's time.** The drawer's timestamp field reads and shows the
time in your display time zone and names it. The edit is stored as UTC (#124).

**Importing frames.** The import form accepts up to 200 files per request. The
upload is streamed to `<data_dir>/tmp/upload-<id>/` while it arrives, the work
runs on a background executor, and the spool is removed afterwards (also on
error, cancellation or a client disconnect). Size limits apply to the bytes
received: a file over `capture.max_image_bytes` or a body over
`server.max_request_body_bytes` is refused, and an import is refused with an
inline error (HTTP 507) when free space would drop below
`storage.low_watermark_bytes`. Each image's capture time is its Exif time. A
time that carries its own UTC offset (`OffsetTimeOriginal`, else `OffsetTime`)
is converted using it; a time without one is read in the optional **Camera time
zone** field (an IANA name such as `America/New_York`; an unknown name is an
inline error), else in the project's schedule time zone, else as UTC. Existing
frames are not changed. A file with no readable time uses the import time and is
flagged as inferred.

### Camera form

The form is ordered as it is filled in: **Address**, then the credentials, then
the *Allow Basic authentication over plain HTTP* option (for an `http://`
address), then **Query camera**, then the name, hostname and protocol that Query
proposes, and the collapsed **Advanced** section last. Query comes after the
credentials so they can be entered before probing. The submit button reads
**Add camera** (or **Save changes** when editing).

The **Use default credentials** checkbox appears only when default credentials
are configured in Settings. With none configured, the username and password
fields are shown directly, and a submitted inherit flag is ignored by the
server. A camera that is set to inherit but whose defaults were removed shows
its credential fields and a notice to enter credentials or set the defaults
again.

Validate and Query time out with an inline message (Query after 20 s). The
form has no Detect button: the protocol probe lives in Query on the web and in
`POST /api/v1/cameras/detect-protocol` in the API.

When the form edits a saved camera it sends that camera's id as `probe_camera_id`,
so Query uses the camera's certificate pin as its only trust (#233).

Query sends the form's *Allow Basic authentication over plain HTTP* choice with
the probe, so a camera that offers only Basic over `http://` can be queried when
the box is ticked; without it Query never sends Basic over plain HTTP.

Query probes each protocol once. It sends credentials to the generic HTTP
snapshot paths only when VAPIX and ONVIF did not reject them.

The *Allow Basic authentication over plain HTTP* group is hidden while the
address is `https://` (it is hidden, not disabled, so a ticked box still
submits); an `http://` or scheme-less address shows it.

The Discover dialog's range field is prefilled with the first
`ssrf.allowed_private_subnets` entry that fits `ssrf.max_scan_hosts`. An empty
multicast result says that multicast does not cross routed VLANs and to scan a
range instead.

#### Certificate pin

Administrators see a **Certificate pin** section in the camera edit form (an
operator sees the pinned state but no buttons). It is opt-in per camera and
needs an `https://` address and no stream URI:

1. **Fetch the camera certificate** shows the certificate the camera presents:
   its SHA-256 fingerprint, subject, issuer and validity. It is a handshake
   only; it sends no request and no credentials, and stores nothing.
2. Compare the fingerprint with the one on the camera, then **Pin this
   certificate**. The server fetches the certificate again and stores it only if
   it still has the confirmed fingerprint. An expired or not-yet-valid
   certificate cannot be pinned.
3. **Remove pin** returns the camera to normal verification.

A pinned camera trusts that one certificate only (the host name is not checked,
and neither `capture.tls_ca_bundle` nor `SSL_CERT_FILE` can vouch for another
certificate), is reached over `https://` only and cannot use RTSP: saving a
stream URI, or an `http://` or scheme-less address, on a pinned camera is
refused. If the camera's certificate changes or expires, captures fail with
`pinned certificate not accepted (it changed or expired); re-pin it in the camera
form`. The form shows the pinned fingerprint, never the certificate itself.

### Header and drawer

The header user menu opens on click or keyboard focus, not on hover, so it cannot
sit under a resting pointer. A closed drawer is `inert` and emptied: its body is
cleared when it closes, so anything typed into a drawer form and not saved is
discarded when the drawer closes (Escape included). On touch screens (coarse
pointer) controls are at least 44 px, the Actions column of a table stays visible on phones,
and a skip link jumps to the main content.

### Project form

When the chosen camera is used by other active projects with a PTZ position, the
PTZ picker shows a warning naming them. On a project's settings form the picker
leaves out the project being edited, so a project is never listed as its own
conflict (#167).

---

## Time zones

Every timestamp in the web UI is shown in your display time zone, set under
**Preferences**. While none is set, the UI uses UTC and fills the zone from your
browser on the next page. Once set, the zone stays until you change it on
Preferences, even on a device in another zone. If your browser reports a
different zone, Preferences offers it with **Use it**. Saving a blank zone takes
the browser's zone again.

Times you type are read in the same zone: the **Jump to date & time** boxes on
Frames and Events, a milestone's date and time and the frame drawer's timestamp
edit. The labels and the Events and Audit time columns name the zone. A label
that stands for many dates names the zone itself (`America/New_York`); a label
next to one date, such as the milestone edit and the drawer edit, gives the
abbreviation in force on that date (`EST` or `EDT`). On the first page after the
browser fills an empty zone, the page is still drawn in UTC and its labels say
UTC; it is not reloaded (#125). At a daylight-saving change, a repeated
hour means its first occurrence, and a skipped hour is read with the offset in
force before it. A link whose `at=` carries an explicit offset (for example
`2026-01-01T08:00+00:00`) keeps that offset, so links shared between users point
at the same moment. Capture schedules and project date bounds are configured in
UTC, or in the schedule's own time zone.

---

## HTMX partial updates

Several mutations return HTML fragments for in-place DOM swaps rather than
full-page reloads:

- Dashboard project grid polls `/partials/projects` for live status updates.
- Individual project status cards poll `/partials/projects/{id}/status`.
- Camera row actions (validate, delete) return fragments.
- Camera location (edit form, Advanced → Location). **Poll camera** returns
  the reported coordinates, "no location", "could not reach", or, when the
  camera refuses the read for its account (Axis requires an Administrator), a
  warning naming that role. The warning offers **Read with admin account**: an
  administrator username and password used once for that request and not
  saved, logged or echoed. It returns the same fragment, so **Use these
  values** applies the coordinates. **Query camera** shows the same refusal
  and points to this flow. See [Cameras → Geolocation](cameras.md#geolocation).
- Frame tile actions (soft-delete, restore, timestamp edit) return the updated
  tile.
- User row actions (password reset, session revoke) return the updated row.
- Camera discovery returns a result list fragment.
- Render and export cards. Clicking "Render #N" on the project Renders tab or
  on `/renders` (or "Export #N" in the project Frames tab's Exports list)
  loads the card fragment into a centered dialog that sits outside the
  self-refreshing render list, so the 10-second refresh cannot close it. The
  card shows the project, who triggered the job (or "schedule"), its times and
  duration, output settings, the frame window and video length, chapters,
  overlays, the output file and its size (stat-ed when the card opens, with a
  "File missing" warning), and the failure reason of a failed job. Viewers can
  open, play and download; Cancel and Delete appear for operators and admins.
  Delete asks for confirmation inside the card (naming the file and size,
  warning for archive renders, and saying a running job will be stopped
  first); on success the dialog closes, the row disappears and a toast
  confirms. Without JavaScript the card link opens the card as a page.
- Clicking **Render now** again with the same settings while that render is
  queued shows the queued render with an "already queued" notice.
