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

Until first-run setup is complete, the following paths remain reachable without
an account: `/first-run` (the setup form), `/static/` (CSS and JS assets),
`/healthz` (liveness probe), and `/api/` (the CLI bearer-token API). Everything
else is redirected to `/first-run`.

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
HTTP API section in the README) and is not governed by these roles.

---

## Sessions

Sessions are stored server-side. Only a high-entropy random token (never the
session data itself) is placed in the browser cookie. The token is hashed with
SHA-256 before being persisted; a database leak therefore exposes no usable
session credential.

### Cookie attributes

The session cookie is set with:
- `HttpOnly` — not accessible from JavaScript
- `Secure` — set only when the connection is effectively HTTPS (direct TLS or
  via a trusted `X-Forwarded-Proto` header from a reverse proxy)
- `SameSite` — default `lax` (configurable via `session.samesite`)
- `Path=/`

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

### Session rotation and revocation

- **On login**, the previous session token is revoked and a new one is minted
  (session fixation mitigation).
- **On logout**, the session is revoked server-side and the cookie is cleared.
- **On password change**, all sessions for the affected user are revoked
  simultaneously, so a stolen token cannot survive a credential change.
- An admin can also revoke all sessions for any user from the Users page.

---

## CSRF protection

All state-changing requests from cookie-authenticated sessions require a valid
CSRF synchronizer token. Safe methods (`GET`, `HEAD`, `OPTIONS`, `TRACE`) are
exempt.

The token is the per-session CSRF secret, which is generated when the session is
created and rotates whenever a login rotation occurs. The application embeds the
token in two places on every authenticated page:

- A `<meta name="csrf-token">` tag in the `<head>`.
- An `hx-headers` attribute on `<body>` that injects `X-CSRF-Token` into every
  HTMX request automatically.

Form submissions also include a hidden `csrf_token` field. The CSRF middleware
accepts the token from either the `X-CSRF-Token` header (the HTMX path) or the
`csrf_token` form field.

Bearer-token (CLI) requests carry no session cookie and are entirely exempt from
CSRF checks; there is no ambient credential for an attacker to ride.

A cookie-bearing request whose session has expired is also treated as exempt,
which means a user with a lapsed session can still POST to `/login` to re-authenticate.

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

- Whether the session cookie is issued with the `Secure` attribute.
- Whether the HTTP-to-HTTPS redirect fires (exempting requests already marked
  as HTTPS by the proxy).

In this mode, bind the HTTPS listener to loopback only (or disable it) and let
the proxy handle the public TLS. The local JSON API and `/healthz` remain
reachable over plain HTTP regardless.

---

## Pages

| URL | Roles | Description |
|---|---|---|
| `/first-run` | None | Initial admin setup (only reachable before an admin exists) |
| `/login` | None | Sign in |
| `/` or `/dashboard` | Any | Project status cards, live capture state |
| `/projects` | Any | Project list |
| `/projects/{id}` | Any | Project detail + recent renders |
| `/cameras` | Any | Registered cameras list |
| `/frames?project_id={id}` | Any | Paginated frame browser for a project |
| `/renders` | Any | Global render queue |
| `/renders/{id}/download` | Any | Download a completed render |
| `/renders/{id}/stream` | Any | Stream a completed H.264/MP4 render |
| `/settings` | Admin | View current configuration (read-only display) |
| `/users` | Admin | Manage local accounts |

"Any" means any authenticated user (Admin or Viewer). Unauthenticated requests
to all authenticated pages are redirected to `/login`.

The settings page displays current configuration values but does not apply
changes — configuration is resolved at startup from files and environment
variables and is not hot-reloaded via the UI.

---

## HTMX partial updates

Several mutations return HTML fragments for in-place DOM swaps rather than
full-page reloads:

- Dashboard project grid polls `/partials/projects` for live status updates.
- Individual project status cards poll `/partials/projects/{id}/status`.
- Camera row actions (validate, delete) return fragments.
- Frame tile actions (soft-delete, restore, timestamp edit) return the updated
  tile.
- User row actions (password reset, session revoke) return the updated row.
- Camera discovery returns a result list fragment.
