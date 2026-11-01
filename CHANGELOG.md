# Changelog

Notable changes to Timelapse Manager, newest release first.

## 0.2.0

Released 2026-10-31.

0.2.0 is a hardening and packaging release. It adds native packages and a
ready-made container template, tightens the defaults that protect your cameras
and sessions, and makes long-running installs safer to upgrade and recover. It
has **breaking changes**: read the upgrade checklist before you upgrade.

### Highlights

- **New ways to install.** A `.deb` (Debian 13), a signed `.rpm` (EL9 and newer),
  a Docker image on `ghcr.io/drcoble/timelapse_manager` and a Proxmox CT template
  now sit beside the tarball. All are attached to the GitHub Release.
- **Safer upgrades.** Before it migrates, the service writes a verified snapshot
  of the database beside it (`timelapse.db.pre-<revision>-<time>`) and refuses to
  start, leaving the database untouched, if it cannot. `timelapse-manager db
  backup` and `db restore` back up a running service and restore with it stopped.
  A package upgrade now checks that the service came back.
- **Encryption key protection.** The service records which key protects the
  stored credentials. A missing or different key stops startup with a clear
  message (exit status 78, not restarted) instead of silently breaking your
  camera and LDAP passwords. New `timelapse-manager secrets status | rotate |
  reset`.
- **Problem reports.** A **Report a problem** link captures a redacted diagnostic
  snapshot of the app at the moment you see a fault, for download or delivery by
  email, webhook or issue tracker, with an anonymized option.
- **Camera certificate pinning.** An administrator can pin a camera to one exact
  TLS certificate.
- **Better health reporting.** `/healthz` reports `ok`, `degraded` or `failed`,
  with per-signal checks, and new Prometheus series cover render orphans,
  encode age and the notification dispatcher.
- **More reliable capture, rendering and alerts.** Exact-time anchors retry
  inside their grace window; a render interrupted by a service stop is queued
  again once; alerts for an offline camera are per camera; notifications missed
  while the service was stopped are delivered when it starts; the render panel
  suggests a frame rate from the project's capture interval, and scheduled
  renders can pick one automatically.
- **Unchanging ids and case-insensitive users.** Deleted projects, cameras,
  frames, users and renders no longer have their ids reused, and usernames are
  compared case-insensitively.
- **Opt-in event retention** (`monitoring.event_retention.*`, off by default) and
  frame exports that expire after 7 days (`render.export_ttl_days`).

### Upgrade checklist

Work through this before upgrading an existing install. The full details are in
[Upgrading to 0.2.0](../installation.md#upgrading-to-020).

1. **Back up the database and copy the encryption key with it**
   (`<data_dir>/.secret-key`). Leave free space of about twice the database size
   for the automatic snapshot.
2. **Expect the migrations.** A 0.1.x database runs migrations 023 to 042 on the
   first start. Migrations 028, 029 and 031 rebuild tables, so the first start
   takes longer on a large database. Migration 025 stops the upgrade if two local
   accounts, or a local and a directory account, differ only in case: rename or
   delete one first.
3. **Put your camera subnets in `ssrf.allowed_private_subnets`.** It defaults to
   empty, and an empty list means the saved default camera login is never sent.
   Cameras that inherit the default login will fail to authenticate until the
   subnet is listed.
4. **Reverse proxy:** list it in `server.trusted_proxies` and have it send
   `X-Forwarded-Proto`. If you set `server.allowed_hosts`, include every name
   and address that reaches the service.
5. **Scripts that call the API from another machine** must be listed in
   `server.api_token_allowed_clients` and use HTTPS. The token is accepted only
   from loopback by default. The CLI no longer creates the token file; start the
   service once to create it.
6. **Monitors that read `/healthz` remotely** now get only `status` and
   `db_status`. A match on `"db_status":"ok"` still works. Do not match
   `"status":"ok"` as a plain substring of the full body.
7. **HTTP cameras that offer only Basic authentication** stop capturing until
   you tick *Allow Basic authentication over plain HTTP* on the camera or move it
   to `https://`.
8. **Hand-installed systemd units** need `TimeoutStopSec=45` and
   `RestartPreventExitStatus=78`. `install.sh` and the packages update the unit
   for you.
9. **Check no render is running** before you stop the service
   (`GET /api/v1/system/render-activity`).
10. **After the upgrade,** users sign in once (the session cookie is renamed over
    HTTPS), and a browser tab left open across the upgrade needs one reload.
    Unattended wall displays that relied on background refreshes to stay signed
    in need `session.keepalive_on_poll: true`.

### Breaking changes

- The API bearer token is accepted only from loopback by default, and over
  HTTPS from other allowed addresses. The `/api/v1` API is bearer-only.
- Forwarded headers are honoured only from `server.trusted_proxies`.
- `/healthz` shows remote callers a short body.
- First-run setup needs a one-time setup code (`timelapse-manager setup-code`).
- The session cookie is `__Host-tlm_session` over HTTPS; the old name is not read.
- The content security policy is `script-src 'self'`: customised templates must
  not use inline scripts or `on*=` handlers.
- Basic authentication over plain HTTP is refused by default, and the saved
  default camera login is sent only inside `ssrf.allowed_private_subnets`.
- Camera and webhook connections ignore `HTTP_PROXY`, `HTTPS_PROXY` and `NETRC`.
- Changing the LDAP servers, TLS mode, CA path or bind DN, or the SMTP host,
  port, security mode or username, with the password field blank now clears the
  stored password: re-enter it in the same save.
- A missing or different encryption key stops the service (exit status 78).
- Repeated failed logins get a growing delay (up to 30 s), not a lockout.
- The manual capture API returns specific status codes (400, 404, 409, 502, 503,
  504, 507) where it used to return 500; deleting a camera that a project uses
  returns 409.
- An idle browser page signs out after the idle timeout (30 minutes by default).
- The web route `POST /cameras/detect-protocol` is removed; the API route stays.
- The config directory `/etc/timelapse-manager` is now root-owned and group
  readable by the service, which can no longer rewrite its own configuration.

### Security hardening

- Request bodies are capped per route, and unauthenticated unsafe API requests
  are refused before their body is read.
- The bearer token file is created safely at startup, and there is a separate
  read-only metrics token.
- Camera discovery replies and camera responses are bounded and parsed
  defensively; camera XML is parsed with `defusedxml`.
- Webhooks connect only to the address the SSRF guard approved, and the saved
  camera login is held to the allowed subnets at the socket.
- Render overlay formats and captions are validated strictly, and ffmpeg inputs
  are restricted to local files.
- An account locked in the directory is signed out and refused.
- Problem reports filed by viewers do not include the system snapshot.

### Install

Download from the
[GitHub Release](https://github.com/drcoble/Timelapse_Manager/releases) and
verify against `SHA256SUMS` (see [Verifying Releases](../verifying-releases.md)):

```sh
apt install ./timelapse-manager_0.2.0-1_amd64.deb
dnf install ./timelapse-manager-0.2.0-1.x86_64.rpm      # after rpm --import of the signing key
docker pull ghcr.io/drcoble/timelapse_manager:0.2.0
```

There is no apt or dnf repository for this release: to upgrade, download the new
package and install it over the old one. Guides:
[packages](../install-deb-rpm.md), [Docker](../install-docker.md),
[Proxmox CT](../install-proxmox-ct.md), [tarball](../installation.md).

## 0.1.0

The first public release. It connects to network IP cameras, captures still
images at configurable intervals over hours to months, and assembles them into
timelapse videos, running unattended through reboots and network interruptions.

### What it does

- **Cameras:** HTTP/JPEG, RTSP, Axis VAPIX and ONVIF, with ONVIF discovery and IP
  range probing.
- **Capture:** per-project capture loops with fault isolation, reconnect with
  backoff, restart-gap logging and frozen-frame detection. Schedules by interval,
  time-of-day window, sunrise or sunset, day of week and campaign bounds, plus
  exact-time and solar captures and capture on camera events.
- **Storage:** relocatable frame storage, auto-pause and resume on low disk
  (it never deletes to reclaim space), and a frame lifecycle (soft delete,
  restore, permanent delete, upload, timestamp correction).
- **Rendering:** FFmpeg encoding with configurable codec, container and quality,
  optional hardware acceleration, deflicker, overlays and chapters; a render
  queue, scheduled renders and post-render actions.
- **Web UI over HTTPS** with a built-in self-signed certificate, a CLI and a
  background service sharing one local API.
- **Access control:** local (Argon2id) and LDAP sign-in, server-side sessions
  with CSRF protection, brute-force throttling and Admin, Operator and Viewer
  roles.
- **Monitoring and notifications:** an event log, an admin audit log and email
  and webhook notification channels with routing rules.

### Install

Linux amd64: a self-contained bundle tarball with a systemd installer, and a
Docker image. Later releases add `.deb` and `.rpm` packages and a Proxmox CT
template; see the [0.2.0 release notes](./0.2.0.md) for those and for the
upgrade steps from 0.1.0.

This was an early-development release. Upgrade to 0.2.0 for the security and
reliability changes described there.
