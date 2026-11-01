"""Application settings models.

Settings form a small tree of nested sections under :class:`Settings`. Values
resolve from (lowest to highest precedence) built-in defaults, an optional
config file, environment variables, and explicit overrides. Environment
variables use the ``TLM_`` prefix with ``__`` to descend into nested sections,
e.g. ``TLM_SERVER__HTTP_PORT=9000`` sets ``settings.server.http_port``.

Only the environment binding is wired here; file loading and override
precedence live in :mod:`timelapse_manager.config.loader`.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from ..paths import default_database_url, default_state_dir

# State defaults resolve to an OS-appropriate, user-writable location outside any
# (read-only, relocatable) application bundle, rather than the current working
# directory -- a frozen or service-managed process has no meaningful CWD. Both
# remain overridable: ``TLM_DATABASE__URL`` for the database, ``TLM_PATHS__DATA_DIR``
# for the data directory. The default factories are pure (they create no
# directories); startup wiring creates the directory on first write.
#
# The database default is centralised in ``paths`` so this model and the engine
# module share one authoritative value and cannot drift apart.


def _coerce_str_list(value: Any) -> Any:
    """Accept either a list or an operator-friendly delimited string.

    pydantic-settings expects a JSON array for a ``list[str]`` field set via an
    environment variable, so a bare ``TLM_..=10.0.0.0/24`` would otherwise crash
    startup with an opaque parse error. Paired with :data:`NoDecode` (which stops
    pydantic-settings from JSON-decoding the env value first), this ``before``
    validator makes the field tolerant: a comma- or whitespace-separated string is
    split into a list, a JSON-array string is parsed, and a real list passed in
    code or a config file is returned unchanged. Empty fragments are dropped.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("["):
        # Preserve the JSON-array form (what pydantic-settings used to require).
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass  # Fall through and treat it as a delimited string.
    return [item for item in re.split(r"[,\s]+", text) if item]


# Loopback in both families: the default peer allowlist for the local API token
# and the metrics token.
_LOOPBACK_CLIENTS = ("127.0.0.1/32", "::1/128")


def _validate_networks(value: list[str]) -> list[str]:
    """Reject any entry that is not an IP address or CIDR block.

    Used for peer allowlists, where a typo must fail at startup rather than
    silently narrow (or, worse, be mistaken for) the intended set. Host bits are
    tolerated (``10.0.12.5/24`` means ``10.0.12.0/24``).
    """
    for entry in value:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise ValueError(f"not an IP address or CIDR block: {entry!r}") from exc
    return value


class ServerSettings(BaseModel):
    """HTTP/HTTPS listener configuration.

    The ``limit_*`` / ``timeout_*`` / ``*_event_size`` fields bound how much an
    unauthenticated peer can hold open or send before a request is fully formed,
    so a slowloris-style flood of slow or incomplete connections cannot exhaust
    the listener. They are passed straight through to the ASGI server.

    :param limit_concurrency: most simultaneous connections/tasks the server
        accepts before shedding load; further connections wait or are rejected.
    :param timeout_keep_alive_seconds: how long an idle keep-alive connection is
        held open before the server closes it.
    :param limit_max_requests: recycle a worker after this many requests
        (``None`` = never), bounding the lifetime of any single connection pool.
    :param h11_max_incomplete_event_size: ceiling, in bytes, on an incomplete
        HTTP event (e.g. a request line + headers that never finish), so a peer
        cannot dribble an unbounded header block to pin a connection.
    :param trusted_proxies: CIDR blocks / addresses of reverse proxies the service
        sits behind. ``X-Forwarded-For`` / ``X-Forwarded-Proto`` are honoured ONLY
        when the immediate peer is in this set; the client address and scheme are
        then taken from the left-most hop not itself a trusted proxy. Empty (the
        default) means the service is directly exposed: forwarded headers are
        ignored and the real peer address/scheme are authoritative, so a direct
        client cannot forge them.
    :param max_request_body_bytes: hard ceiling on the bytes of a single request
        body, enforced while the body streams in regardless of any (absent or
        lying) ``Content-Length``. A request exceeding it is rejected with a
        ``413`` before the oversized body is buffered.
    :param api_token_allowed_clients: CIDR blocks / addresses from which the
        local API bearer token is accepted at all. The default is loopback only,
        so the CLI keeps working over ``http://127.0.0.1`` while the token is
        refused (``403``, before it is compared) from every other host, including
        a trusted reverse proxy. A non-loopback entry is honoured only over
        HTTPS: directly on the HTTPS listener, or through a trusted proxy that
        asserts ``X-Forwarded-Proto: https`` (the forwarded client address is
        then the one checked).
    :param shutdown_grace_seconds: on a stop, how long each listener waits for
        open connections to finish before cancelling them (#67 R4). Keep it well
        under the systemd unit's ``TimeoutStopSec`` (45 s): the lifespan teardown
        (render cancel, capture stop) runs after it.
    """

    http_port: int = 8080
    https_port: int = 8443
    bind_address: str = "0.0.0.0"
    redirect_http_to_https: bool = True
    limit_concurrency: int = 256
    timeout_keep_alive_seconds: int = 5
    limit_max_requests: int | None = None
    h11_max_incomplete_event_size: int = 16 * 1024
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_request_body_bytes: int = 512 * 1024 * 1024

    _coerce_trusted_proxies = field_validator("trusted_proxies", mode="before")(
        _coerce_str_list
    )
    api_token_allowed_clients: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(_LOOPBACK_CLIENTS)
    )

    _coerce_api_token_allowed_clients = field_validator(
        "api_token_allowed_clients", mode="before"
    )(_coerce_str_list)
    _check_api_token_allowed_clients = field_validator("api_token_allowed_clients")(
        _validate_networks
    )

    # Ceiling for a request body that no per-route rule in
    # ``web/body_limit.py`` covers (JSON API bodies and the like). Never above
    # ``max_request_body_bytes``, which stays the hard ceiling for every route.
    max_default_body_bytes: int = Field(default=16 * 1024 * 1024, gt=0)

    # Host names (``Host`` header, port ignored) the service answers. ``*`` (the
    # default) answers any host, i.e. no check. A list rejects every other host
    # with ``400``; ``127.0.0.1``, ``localhost`` and ``::1`` are always allowed
    # on top of it so the CLI and local probes keep working. An entry is a host
    # name, an IP literal, or a ``*.example.com`` subdomain wildcard.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])

    _coerce_allowed_hosts = field_validator("allowed_hosts", mode="before")(
        _coerce_str_list
    )

    @field_validator("allowed_hosts")
    @classmethod
    def _normalise_allowed_hosts(cls, value: list[str]) -> list[str]:
        """Lower-case and validate ``allowed_hosts``; any ``*`` means all hosts.

        A typo must fail at startup, not answer ``400`` to every request after
        the setting is applied, so anything that is not a host name, an IP
        literal (IPv6 with or without brackets) or a ``*.domain`` wildcard is
        rejected: in particular a scheme, a port or a path.
        """
        if not value:
            raise ValueError("allowed_hosts is empty; use ['*'] to allow any host")
        normalised: list[str] = []
        for raw in value:
            entry = raw.strip().lower()
            entry = entry[:-1] if entry.endswith(".") else entry
            if entry == "*":
                return ["*"]
            if entry.startswith("[") and entry.endswith("]"):
                entry = entry[1:-1]
            try:
                normalised.append(str(ipaddress.ip_address(entry)))
                continue
            except ValueError:
                pass
            if not _ALLOWED_HOST_PATTERN.fullmatch(entry):
                raise ValueError(
                    f"not a host name, IP address or '*.domain' wildcard: {raw!r}"
                )
            normalised.append(entry)
        return normalised

    # How long each listener waits, once a stop begins, for open connections (a
    # download, a video stream) to finish before it cancels them (#67 R4). Both
    # listeners drain at the same time, and the lifespan teardown follows, so
    # this plus the teardown must fit the unit's ``TimeoutStopSec`` (45 s).
    shutdown_grace_seconds: int = Field(default=10, ge=1)


# A lower-case DNS host name, as the web layer accepts it in a ``Host`` header
# (``web.middleware.request_host``) and as an ``allowed_hosts`` entry.
HOST_NAME_PATTERN = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")
# One ``allowed_hosts`` entry: a host name, optionally behind a leading ``*.``.
_ALLOWED_HOST_PATTERN = re.compile(r"(\*\.)?" + HOST_NAME_PATTERN.pattern)


class TlsSettings(BaseModel):
    """TLS certificate material for the built-in HTTPS listener.

    When ``cert_path`` and ``key_path`` both point at existing files, that
    pair is used directly. Otherwise, if ``auto_generate`` is set, a self-signed
    certificate (valid for ``localhost``/loopback) is generated into the data
    directory on first start so HTTPS works out of the box on a fresh install;
    if it is not set, the absence of a usable certificate is a hard error.

    :param cert_path: filesystem path to a PEM certificate, or ``None``.
    :param key_path: filesystem path to the matching PEM private key, or ``None``.
    :param auto_generate: whether to generate a self-signed certificate when an
        explicit pair is not provided.
    """

    cert_path: str | None = None
    key_path: str | None = None
    auto_generate: bool = True


class DatabaseSettings(BaseModel):
    """Database connection configuration.

    ``url`` is the single authoritative database location for the application.
    """

    url: str = Field(default_factory=default_database_url)
    timeout: int = 30


class LoggingSettings(BaseModel):
    """Logging level, output format, and optional file destination.

    :param ring_buffer_records: how many recent log records the in-memory ring
        buffer keeps for problem reports (#8). The buffer is how a report gets
        the app's own log without reading the journal, which the systemd sandbox
        may not allow. ``0`` disables it.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] = "json"
    file_sink: Path | None = None
    ring_buffer_records: int = Field(default=2000, ge=0, le=100_000)


class PathsSettings(BaseModel):
    """Filesystem locations the application reads and writes.

    ``frames_root`` and ``token_file`` default to locations under ``data_dir``
    when not set explicitly. This section intentionally does not carry a
    database path; the database location lives solely in
    :class:`DatabaseSettings` so the two can never disagree.
    """

    data_dir: Path = Field(default_factory=default_state_dir)
    frames_root: Path | None = None
    token_file: Path | None = None

    @model_validator(mode="after")
    def _derive_paths(self) -> PathsSettings:
        """Fill ``frames_root`` / ``token_file`` from ``data_dir`` if unset."""
        if self.frames_root is None:
            self.frames_root = self.data_dir / "frames"
        if self.token_file is None:
            self.token_file = self.data_dir / ".local-token"
        return self


class CaptureSettings(BaseModel):
    """Capture engine behaviour.

    :param autostart: whether the capture supervisor starts scheduled capture
        tasks when the application starts. The supervisor is always constructed
        so manual capture works; this only gates the background loops.
    :param timeout_seconds: per-frame ceiling on a single capture attempt. A
        capture exceeding this is skipped (a gap is logged) so one slow camera
        never blocks the loop or other projects.
    :param default_interval_seconds: fallback interval for a capture target that
        does not specify its own interval.
    :param backoff_base_seconds: first reconnect delay after a transient capture
        failure; subsequent failures grow this exponentially.
    :param backoff_max_seconds: ceiling on the reconnect delay so backoff never
        grows without bound.
    :param backoff_jitter_fraction: fraction of the computed delay to randomise
        (``±fraction``) so failing peers do not retry in lockstep.
    :param frozen_frame_enabled: whether identical-frame ("frozen camera")
        detection runs after each successful capture.
    :param frozen_frame_threshold: number of consecutive identical frames that
        triggers a warning event; capture continues regardless.
    :param offline_failure_threshold: number of consecutive failed capture
        attempts after which the camera is considered offline and a warning alert
        is raised. The first success after that emits a recovery (info) event
        that auto-clears the alert. ``1`` raises on the first failure; capture
        keeps retrying with backoff regardless of this value.
    :param max_idle_sleep_seconds: ceiling on any single sleep the capture loop
        performs, so a long closed window stays cancellable and config/clock
        drift is re-evaluated periodically.
    :param reconcile_interval_seconds: how often the supervisor re-reads the set
        of qualifying projects and converges its running capture tasks to it, so
        a project created (or archived) while the service is running starts (or
        stops) capturing without a restart. A project create can wake this loop
        early; this value is the periodic ceiling that guarantees convergence.
    :param ws_read_timeout_seconds: silence budget for a camera event WebSocket.
        A half-open or wedged camera socket can otherwise hang the listener task
        forever, never reaching the re-subscribe backoff. The reader idles for at
        most this long without a frame before the socket is treated as dead and
        dropped; the existing listener loop then reconnects.
    :param ws_ping_interval_seconds: how long the event WebSocket may sit silent
        before a keepalive ping is sent. A pong (or any frame) resets the silence
        timer, so a legitimately quiet but live camera is never disconnected.
        Should be smaller than ``ws_read_timeout_seconds``.
    :param onvif_watchdog_seconds: ceiling on a single ONVIF PullPoint pull cycle
        (the long-poll plus any subscription renew). A half-open or wedged camera
        can otherwise stall a cycle indefinitely -- past httpx's per-read timeout,
        which a slow trickle keeps resetting -- hanging the event listener so it
        never reaches the re-subscribe backoff. A cycle that does not complete
        within this budget is treated as a dead subscription and the listener
        re-subscribes. Set comfortably above the long-poll hold so a quiet but
        live camera is never dropped mid-cycle.
    :param max_image_bytes: hard ceiling on a single snapshot/HTTP-JPEG response
        body. Camera responses are untrusted; a capture whose body exceeds this is
        aborted (the frame is skipped, a gap logged) rather than buffered into
        memory, bounding the OOM exposure from one hostile or malfunctioning host.
    :param max_ws_frame_bytes: hard ceiling on a single event WebSocket frame.
        Event metadata frames are small; a frame declaring a length above this is
        rejected before its payload is read, so an untrusted 64-bit frame length
        cannot drive an unbounded allocation.
    :param max_image_dimension_px: hard ceiling on either pixel dimension of a
        captured image. A rogue or malfunctioning camera can report enormous
        width/height in its image header; decoding or thumbnailing such a frame
        would allocate gigabytes. A frame whose width or height exceeds this is
        rejected before any pixel buffer is allocated.
    :param rtsp_timeout_seconds: ffmpeg socket I/O timeout for an RTSP grab. The
        grab's whole budget is this plus 5 s, and it replaces ``timeout_seconds``
        for an RTSP camera when larger.
    :param rtsp_jpeg_quality: ffmpeg ``-q:v`` for the grabbed JPEG (1-31, lower
        is better).
    :param rtsp_wait_for_keyframe: whether a grab discards non-key frames first.
    """

    autostart: bool = True
    timeout_seconds: float = 10.0
    default_interval_seconds: int = 60
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 300.0
    backoff_jitter_fraction: float = 0.1
    frozen_frame_enabled: bool = True
    frozen_frame_threshold: int = 5
    offline_failure_threshold: int = 3
    max_idle_sleep_seconds: float = 300.0
    reconcile_interval_seconds: float = 20.0
    ws_read_timeout_seconds: float = 60.0
    ws_ping_interval_seconds: float = 30.0
    onvif_watchdog_seconds: float = Field(default=300.0, gt=0)
    max_image_bytes: int = 64 * 1024 * 1024
    max_ws_frame_bytes: int = 2 * 1024 * 1024
    max_image_dimension_px: int = 16000

    @model_validator(mode="after")
    def _check_ws_keepalive(self) -> CaptureSettings:
        """The keepalive ping must fire strictly before the read budget ends.

        Equality would leave no post-ping window (the ping is sent but never
        waited on), so the keepalive would be inert; require a real gap.
        """
        if self.ws_ping_interval_seconds >= self.ws_read_timeout_seconds:
            raise ValueError(
                "ws_ping_interval_seconds must be < ws_read_timeout_seconds"
            )
        return self

    # Camera HTTP clients (#69 CAM-P2, CAM-P1): captures, event listeners and
    # operator probes each get their own connection pool, so a probe burst or a
    # set of event long-polls never makes a capture wait for a connection. The
    # defaults cover 10 cameras with two concurrent requests each plus headroom;
    # a much larger fleet needs the capture limits raised.
    # Most connections the capture client opens at once.
    http_max_connections: int = Field(default=24, gt=0)
    # Idle connections the clients keep open for reuse (capped per client by
    # its own ``max_connections``).
    http_keepalive_connections: int = Field(default=12, ge=0)
    # How long an idle kept-alive camera connection is kept, in seconds.
    http_keepalive_expiry_seconds: float = Field(default=30.0, ge=0)
    # Most connections the event-listener client opens at once (one long-poll
    # per listener, plus slack).
    event_http_max_connections: int = Field(default=16, gt=0)
    # Most connections the probe client (Detect, query, discovery enrichment,
    # enumeration) opens at once.
    probe_http_max_connections: int = Field(default=12, gt=0)
    # Optional PEM CA bundle that camera TLS connections verify against (#69
    # CAM-S1). Unset, cameras are verified the way httpx verifies by default:
    # ``SSL_CERT_FILE``, then ``SSL_CERT_DIR``, then certifi. Setting it lets
    # a private camera CA apply to camera connections only.
    tls_ca_bundle: Path | None = None
    # Threads in the dedicated capture executor (#66 C3): every blocking capture
    # hop (database reads and writes, frame writes, event-listener loads) runs
    # there, so capture never queues behind the loop's default executor. Unset
    # means ``min(32, cpu_count + 4)``, the default executor's own size (6 on 2
    # vCPUs, 12 on 8). Thread names start ``tlm-capture``. The database
    # connection pool grows by the same number.
    executor_workers: int | None = Field(default=None, ge=1)
    # RTSP grabs (#69 CAM-R3). Each grab runs one ffmpeg child, bounded by the
    # socket timeout below plus 5 s of wall clock; the capture budget of an RTSP
    # camera is that total, so ``timeout_seconds`` never cuts a grab short.
    # Socket I/O timeout passed to ffmpeg, in seconds. At most 45 s, so the whole
    # grab (this plus 5 s) fits inside the margin the exact-time anchor attempt
    # allows beyond ``timeout_seconds`` (60 s), with room for the frame write.
    rtsp_timeout_seconds: float = Field(default=20.0, gt=0, le=45)
    # JPEG quality for the grabbed frame, ffmpeg ``-q:v`` (2 best .. 31 worst).
    rtsp_jpeg_quality: int = Field(default=2, ge=1, le=31)
    # Skip frames until a keyframe, so a grab does not return a half-decoded
    # P-frame. Off by default: it can lengthen a grab by one GOP.
    rtsp_wait_for_keyframe: bool = False

    @model_validator(mode="after")
    def _check_http_pools(self) -> CaptureSettings:
        """Keep-alive must fit in the capture pool; the CA bundle must be readable.

        The bundle is checked here so a wrong path is a clear startup error,
        not a TLS failure on every capture.
        """
        if self.http_keepalive_connections > self.http_max_connections:
            raise ValueError(
                "http_keepalive_connections must be <= http_max_connections"
            )
        bundle = self.tls_ca_bundle
        if bundle is not None and not (bundle.is_file() and os.access(bundle, os.R_OK)):
            raise ValueError(
                f"capture.tls_ca_bundle {str(bundle)!r} is not a readable file"
            )
        return self


class RenderSettings(BaseModel):
    """Video render orchestration behaviour.

    The render worker drains a bounded queue of encode jobs so renders never
    starve capture; the scheduler periodically enqueues recurring renders and
    archive snapshots.

    :param max_concurrent: most renders allowed to run at once. Renders are
        subprocess-bound, but the cap still bounds resource use and keeps the
        event loop responsive to capture.
    :param output_subdir: directory name, under a project's storage location,
        that produced videos are written to.
    :param default_fps: frame rate used for a render that does not specify one.
    :param webhook_timeout_seconds: ceiling on a single post-render webhook POST.
    :param font_path: TrueType font for text/timestamp overlays. When unset the
        encoder probes a platform default; bundled deployments should set it.
    :param scheduler_check_interval_seconds: how often the scheduler re-evaluates
        every project's render and archive cadence.
    :param autostart: whether the render worker and scheduler loops run on
        startup. On in production; tests disable it so an inert queue/scheduler
        can be driven deterministically.
    :param ffmpeg_binary: explicit path to the ffmpeg executable. When unset
        (the default), a packaged release uses the ffmpeg bundled with it and a
        development checkout falls back to ``ffmpeg`` on ``PATH``. Set this to
        point at a specific binary -- for example a container image that copies a
        pinned static ffmpeg to a fixed path. ``ffprobe`` is resolved beside it.
    :param hwaccel_enabled: master switch for GPU-accelerated encoding. Off by
        default, which keeps encoding entirely on the CPU (software encoders).
        When on, the encoder probes the local ffmpeg once at first use to learn
        which hardware encoders exist and, for each render, uses a hardware
        encoder when one is available for the requested codec, otherwise falls
        back to software automatically. Software encoding is always the
        guaranteed fallback: enabling this never causes a render to fail merely
        because hardware is missing.
    :param hwaccel_api: which GPU encode API to use when ``hwaccel_enabled`` is
        on. One of ``"nvenc"`` (NVIDIA), ``"qsv"`` (Intel Quick Sync), or
        ``"vaapi"`` (Linux VA-API, typically AMD/Intel), or ``None`` to leave
        hardware encoding effectively off even if the master switch is on. A
        codec with no hardware encoder for the chosen API (for example VP9, or
        AV1 under NVENC) silently uses software for that render.
    :param hwaccel_device: optional device selector passed to the chosen API. For
        VA-API this is the render node path, e.g. ``"/dev/dri/renderD128"``; for
        NVENC it is the GPU index, e.g. ``"0"``. When unset the API's own default
        device is used. Ignored on the software path.
    :param stall_timeout_seconds: the render watchdog kills ffmpeg, removes the
        partial output and fails the job when neither its frame count nor its
        output time has advanced for this long (#67 R2).
    :param max_job_seconds: wall-clock ceiling for one render, after which the
        watchdog kills ffmpeg and fails the job. ``0`` (the default) derives it
        from the frame count: ``max(3600, 2 * frames)`` seconds.
    :param jobs_executor_workers: threads in the dedicated pool that runs long
        render-side jobs -- render preparation, export zips, post-render copies
        and prunes, the codec backfill scan -- so they neither starve capture nor
        tie up the request threadpool (#66 C3). Two long jobs beyond this queue
        behind each other. The connection pool grows by the same number.
    :param thumbnail_concurrency: the most frame thumbnails generated (ffmpeg
        runs) at once across the process (#67 P3). A request that waits more
        than two seconds for a slot is served the full-size frame instead,
        uncached, so the browser asks for the thumbnail again later.
    :param max_concurrent_exports: the most frame-export zips built at once
        (#67 P7). Exports have a slot of their own, so one never holds the render
        slot (``max_concurrent``) and a long export cannot delay a render.
    :param export_ttl_days: days a finished frame export is kept before the
        hourly housekeeping deletes its zip and row (#67 P7). ``0`` keeps exports
        until they are deleted by hand.
    """

    autostart: bool = True
    max_concurrent: int = 1
    output_subdir: str = "renders"
    default_fps: float = 24.0
    webhook_timeout_seconds: float = 10.0
    font_path: str | None = None
    scheduler_check_interval_seconds: float = 60.0
    ffmpeg_binary: str | None = None
    # Selects the encoder engine behind the Encoder interface. The interface
    # admits interchangeable engines; only the bundled FFmpeg engine ships today,
    # so this defaults to "ffmpeg" and an unknown value fails loudly at startup.
    encoder_engine: str = "ffmpeg"
    # Hardware-accelerated encoding. Off by default (CPU-only); software encoding
    # is always the guaranteed fallback so enabling this can never fail a render.
    hwaccel_enabled: bool = False
    hwaccel_api: str | None = None
    hwaccel_device: str | None = None
    # Render watchdog (#67 R2): a stalled or overlong ffmpeg is killed and the
    # job failed with the reason, instead of holding the render slot forever.
    stall_timeout_seconds: float = Field(default=300.0, gt=0)
    max_job_seconds: float = Field(default=0.0, ge=0)
    # Dedicated long-job executor (#66 C3); thread names start ``tlm-jobs``.
    jobs_executor_workers: int = Field(default=2, ge=1)
    # Thumbnail generation cap (#67 P3).
    thumbnail_concurrency: int = Field(default=2, ge=1, le=64)
    # Own slot for export zips, and their retention (#67 P7).
    max_concurrent_exports: int = Field(default=1, ge=1, le=16)
    export_ttl_days: int = Field(default=7, ge=0)


class EventRetentionSettings(BaseModel):
    """Opt-in pruning of old event rows (issue #66, C4).

    Off by default: both windows are ``0``, which keeps every event forever, so
    an upgrade changes nothing until the operator opts in. The hourly
    housekeeping pass deletes in bounded batches, so one pass never holds the
    write lock for long. Audit and security events and uncleared alerts are
    always kept, whatever the windows.

    :param info_days: delete ``debug`` and ``info`` events older than this many
        days. ``0`` keeps them forever.
    :param cleared_alert_days: delete alerts cleared more than this many days
        ago (by ``alert_cleared_at``). ``0`` keeps them forever.
    :param batch_size: rows deleted per transaction.
    """

    info_days: int = Field(default=0, ge=0)
    cleared_alert_days: int = Field(default=0, ge=0)
    batch_size: int = Field(default=5000, ge=1, le=100_000)


class MonitoringSettings(BaseModel):
    """Notification dispatch behaviour.

    The dispatcher polls the event log for new rows and fans matching events out
    to the configured channels. These values bound that loop's cadence and its
    per-delivery effort.

    :param autostart: whether the dispatcher's poll loop runs on startup. On in
        production; tests disable it so an inert dispatcher can be driven
        deterministically through its single-pass entry point.
    :param poll_interval_seconds: how often the loop polls for new events.
    :param max_retries: total delivery attempts per channel before a delivery
        failure is recorded (the first attempt counts toward this).
    :param retry_backoff_seconds: base delay between retries; grows exponentially
        with the attempt number plus jitter.
    :param debounce_window_seconds: per-channel suppression window keyed on the
        event type and scope, so a flapping condition does not notify repeatedly.
    :param channel_send_timeout_seconds: hard ceiling on a single channel send,
        enforced by the dispatcher so a hanging channel cannot block shutdown.
    :param event_retention: opt-in pruning of old events; off by default (see
        :class:`EventRetentionSettings`).
    :param catchup_max_age_seconds: on startup the dispatcher delivers the
        events it had not yet delivered when it last stopped, but only those
        logged within this many seconds; older ones are skipped (and counted in
        one log line), so a long outage does not replay stale alerts.
    :param max_concurrent_sends: the most channel sends in flight at once, so a
        burst of events cannot open an unbounded number of SMTP or HTTP
        connections.
    """

    autostart: bool = True
    poll_interval_seconds: float = 5.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    debounce_window_seconds: float = 60.0
    channel_send_timeout_seconds: float = 10.0
    event_retention: EventRetentionSettings = Field(
        default_factory=EventRetentionSettings
    )
    catchup_max_age_seconds: float = Field(default=3600.0, ge=0)
    max_concurrent_sends: int = Field(default=4, ge=1)


class ObservabilitySettings(BaseModel):
    """Operational observability surfaces exposed by the process.

    :param metrics_enabled: whether the Prometheus metrics endpoint is served.
        Off by default: while disabled the endpoint is invisible (it answers as
        though the route did not exist). When enabled it requires an
        administrator, since the listener binds all interfaces and there is no
        unauthenticated path to it.
    :param debug_endpoints_enabled: whether the in-process debugging routes
        (``/api/v1/system/debug/...``: asyncio tasks, thread stacks, capture
        runners, and the temporary log-level override) are served. Off by
        default and gated exactly like the metrics endpoint: 404 while
        disabled, administrator-only when enabled.
    :param metrics_token_file: file holding the read-only metrics bearer token,
        created (``0600``) at startup while ``metrics_enabled`` is set. ``None``
        means ``<data_dir>/.metrics-token``. The token grants the metrics scrape
        and nothing else.
    :param metrics_allowed_clients: CIDR blocks / addresses from which the
        metrics token is accepted (loopback by default). Plain HTTP is allowed
        for it, because it only reads; behind a trusted proxy the forwarded
        client address is the one checked.
    """

    metrics_enabled: bool = False
    debug_endpoints_enabled: bool = False
    metrics_token_file: Path | None = None
    metrics_allowed_clients: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(_LOOPBACK_CLIENTS)
    )

    _coerce_metrics_allowed_clients = field_validator(
        "metrics_allowed_clients", mode="before"
    )(_coerce_str_list)
    _check_metrics_allowed_clients = field_validator("metrics_allowed_clients")(
        _validate_networks
    )


class ReportingGiteaSettings(BaseModel):
    """Optional Gitea issue delivery for problem reports.

    Delivery is active only when ``base_url``, ``repo`` and ``token_file`` are
    all set. The token is read from ``token_file`` at send time and is never
    stored in the database or a report. ``base_url`` comes from the config file
    (an operator-trusted source), so it is not run through the webhook SSRF
    deny-list: a self-hosted Gitea normally lives on a private address.

    :param base_url: the Gitea root, e.g. ``https://git.example.com``.
    :param repo: ``owner/name`` of the repository issues are filed in.
    :param token_file: file holding an API token with issue-write access on
        ``repo`` only. Keep it ``0600`` and owned by the service account.
    :param labels: label names applied to each issue. Names the repository does
        not define are skipped.
    :param timeout_seconds: ceiling on each Gitea API call.
    """

    base_url: str | None = None
    repo: str | None = None
    token_file: Path | None = None
    labels: Annotated[list[str], NoDecode] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30.0, gt=0)

    _coerce_labels = field_validator("labels", mode="before")(_coerce_str_list)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        """Require an http(s) URL with a host; the issue link is built from it."""
        if value is None or value == "":
            return None
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base_url must be an http(s) URL")
        return value

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str | None) -> str | None:
        """Require ``owner/name`` so the API paths cannot be bent."""
        if value is None or value == "":
            return None
        parts = value.split("/")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", value) or any(
            part in (".", "..") for part in parts
        ):
            raise ValueError("repo must be 'owner/name'")
        return value

    @property
    def configured(self) -> bool:
        """Whether every field issue creation needs is present."""
        return bool(self.base_url and self.repo and self.token_file)


# ``owner/name`` on github.com. GitHub owners are letters, digits and hyphens;
# repository names add ``.`` and ``_``.
_GITHUB_REPO_PATTERN = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")


class ReportingGithubSettings(BaseModel):
    """The public GitHub hand-off for problem reports (#21).

    While enabled, **every** report is anonymized, and its result screen offers
    an "Open on GitHub" link: a prefilled ``github.com/<repo>/issues/new`` page
    the user reviews and submits from their own GitHub account. The app never
    holds a GitHub credential and never calls GitHub. Short or generic names
    (a user ``admin``, a camera ``Pi``) that would appear in the public text
    refuse the report until they are renamed; see
    ``docs/operations/problem-reports.md``.

    :param enabled: offer the hand-off and anonymize every report. When off,
        the form shows an optional "Anonymize private data" box instead.
    :param repo: ``owner/name`` of the public repository on github.com.
    :param labels: label names to prefill. Empty by default and then no
        ``labels`` parameter is sent: GitHub answers 404 to a prefilled label
        from a user who cannot label issues in ``repo``, which is every
        end user of a public project.
    """

    enabled: bool = True
    repo: str = "drcoble/Timelapse_Manager"
    labels: Annotated[list[str], NoDecode] = Field(default_factory=list)

    _coerce_labels = field_validator("labels", mode="before")(_coerce_str_list)

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        """Require ``owner/name``, so the issue URL's path cannot be bent."""
        if not _GITHUB_REPO_PATTERN.fullmatch(value) or value.split("/")[1] in (
            ".",
            "..",
        ):
            raise ValueError("repo must be 'owner/name' on github.com")
        return value

    @field_validator("labels")
    @classmethod
    def _check_labels(cls, value: list[str]) -> list[str]:
        """Labels are joined with commas in the URL, so none may hold one."""
        labels = [label.strip() for label in value]
        if any(not label or "," in label for label in labels):
            raise ValueError("labels must be non-empty and must not contain ','")
        return labels


class ReportingSettings(BaseModel):
    """The user-initiated "Report a problem" feature (#8).

    A report is always saved locally (under ``<data_dir>/reports``) and listed
    for admins; each other destination is optional and independent.

    :param enabled: show the "Report a problem" link and accept reports. When
        on, every report is stored locally whatever else is configured.
    :param email_to: recipients for an email copy, sent through the SMTP
        server configured under notification settings.
    :param email_attach_max_bytes: a zip larger than this is not attached to
        the email; the message carries a download link instead.
    :param webhook_url: a URL that receives a signed JSON summary (signed with
        the notification webhook signing secret, when one is set). Passes the
        same SSRF guard as the notification webhooks.
    :param public_base_url: the external URL of this app (e.g.
        ``https://timelapse.example.com``), used to build download links in the
        email and webhook. Without it the message says where to find the report.
    :param gitea: optional issue creation; see :class:`ReportingGiteaSettings`.
    :param github: the public GitHub hand-off and forced anonymizing; see
        :class:`ReportingGithubSettings`.
    :param retention_days: reports older than this are pruned by the hourly
        housekeeping pass.
    :param retention_max_reports: beyond the newest this many, the oldest
        reports whose delivery finished are pruned; pending or failed ones
        are kept until they reach ``retention_days``.
    :param rate_limit_per_hour: reports one user may file per rolling hour.
    :param max_upload_bytes: ceiling on the optional image upload.
    :param max_text_bytes: ceiling on the free-text description (UTF-8 bytes).
    :param log_window_minutes: log lines within this many minutes either side
        of the reported time are included.
    :param log_max_lines: ceiling on log lines in one report.
    :param delivery_timeout_seconds: ceiling on one email or webhook delivery.
    """

    enabled: bool = True
    email_to: Annotated[list[str], NoDecode] = Field(default_factory=list)
    email_attach_max_bytes: int = Field(default=5 * 1024 * 1024, ge=0)
    webhook_url: str | None = None
    public_base_url: str | None = None
    gitea: ReportingGiteaSettings = Field(default_factory=ReportingGiteaSettings)
    github: ReportingGithubSettings = Field(default_factory=ReportingGithubSettings)
    retention_days: int = Field(default=90, ge=1)
    retention_max_reports: int = Field(default=50, ge=1)
    rate_limit_per_hour: int = Field(default=5, ge=1)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_text_bytes: int = Field(default=20_000, ge=1)
    log_window_minutes: int = Field(default=30, ge=1)
    log_max_lines: int = Field(default=2000, ge=1)
    delivery_timeout_seconds: float = Field(default=30.0, gt=0)

    _coerce_email_to = field_validator("email_to", mode="before")(_coerce_str_list)


class SessionSettings(BaseModel):
    """Web login session and cookie behaviour.

    Server-side sessions are keyed by a high-entropy token whose hash is stored
    in the database; only the raw token lives in the browser cookie. The three
    timeout values bound a session along two independent axes:

    * **Idle** -- a session expires once it has gone untouched for longer than
      ``idle_timeout_seconds`` (measured from its last activity). Applies to all
      sessions, including persistent ("remember me") ones.
    * **Absolute** -- a non-persistent session expires once it is older than
      ``absolute_timeout_seconds`` (measured from creation), regardless of
      activity, so a stolen-but-active session cannot live forever.
    * **Persistent** -- a "remember me" session uses ``persistent_timeout_seconds``
      as its creation-anchored cap instead of the absolute one, trading a longer
      lifetime for the convenience of staying signed in across restarts.

    A directory-backed ("LDAP") session carries one extra control. Because such a
    session can outlive a directory change (a user disabled or moved between
    groups), it is periodically re-evaluated against the directory:
    ``ldap_revalidation_interval_seconds`` bounds how stale that view may get. The
    re-check runs at most once per interval per session (gated on a stored
    timestamp), so it adds a directory round-trip only on the rare request that
    falls due, never on every page load. Local sessions are never re-evaluated.

    :param cookie_name: the session cookie's name.
    :param idle_timeout_seconds: inactivity ceiling, from last activity.
    :param absolute_timeout_seconds: age ceiling for a non-persistent session.
    :param persistent_timeout_seconds: age ceiling for a persistent session.
    :param samesite: ``SameSite`` attribute the cookie is issued with.
    :param ldap_revalidation_interval_seconds: how often a directory-backed
        session is re-checked against the directory; the upper bound on how long a
        deprovisioning or role change can take to be reflected in a live session.
        Conservative by default (15 minutes) to bound directory load.
    :param keepalive_on_poll: whether a background poll (a request marked
        ``X-TLM-Passive: 1``: the alerts badge, the dashboard and renders
        refresh, the new-frames/new-events pills) keeps the session alive. Off by
        default, so a page left open with no input still signs out after
        ``idle_timeout_seconds``; turn it on for an unattended wall display. A
        poll sent within one touch interval of real input always counts as
        activity.
    :param host_prefix: whether the session cookie is issued as
        ``__Host-<cookie_name>`` whenever the request is effectively HTTPS (TLS on
        the connection, or ``X-Forwarded-Proto`` from a ``server.trusted_proxies``
        peer). The prefix makes a browser refuse a cookie of that name set from a
        sibling subdomain or without ``Secure``, which closes cookie tossing and
        session fixation from a neighbouring host. Over plain HTTP the cookie keeps
        its unprefixed name, because browsers reject a ``__Host-`` cookie without
        ``Secure``. Over HTTPS the unprefixed name is never read. Turn off only to
        keep the old name everywhere.
    """

    cookie_name: str = "tlm_session"
    idle_timeout_seconds: int = 1800
    absolute_timeout_seconds: int = 86400
    persistent_timeout_seconds: int = 2592000
    samesite: Literal["lax", "strict", "none"] = "lax"
    ldap_revalidation_interval_seconds: int = 900
    keepalive_on_poll: bool = False
    host_prefix: bool = True


class AuthSettings(BaseModel):
    """Password policy, brute-force throttling, and password-hashing cost.

    The Argon2 parameters are tuned to be acceptable on a small single-board
    computer (such as a Raspberry Pi) while still resisting offline cracking;
    they are configurable so a more capable host can raise the cost.

    :param password_min_length: minimum length a new password must meet.
    :param throttle_max_failures: failed login attempts permitted from one
        source within the window before requests are throttled.
    :param throttle_window_seconds: sliding window over which failures are
        counted toward ``throttle_max_failures``.
    :param argon2_memory_kib: Argon2 memory cost in KiB.
    :param argon2_time_cost: Argon2 number of iterations.
    :param argon2_parallelism: Argon2 degree of parallelism (lanes).
    :param throttle_global_max_failures: failed logins, from all sources
        together within the window, at or above which every login is delayed by
        at least the first step of the per-username schedule.
    :param local_backoff_after_failures: failed logins for one username (from
        any source) within the window after which the next attempt for it is
        delayed: 1 s, doubling per further failure, at most 30 s. The delay
        never refuses: a correct password still signs in once it has passed.
        It applies to every submitted name alike (local, directory or unknown),
        so timing does not reveal which names are accounts.
    :param ldap_breaker_seconds: how long the directory circuit breaker stays
        open after the directory proves unreachable (no server accepted a
        connection, or every server stalled before the service-account bind
        completed, #202). While it is open, a directory login answers "directory
        unavailable" at once, and the periodic directory re-check of a live
        session is skipped (the session is kept; the re-check runs again once the
        breaker closes), instead of every request waiting out the connect
        timeout. After the window, one request probes the directory; any result
        other than "no server reachable" closes the breaker, and another outage
        re-opens it. Only an unreachable directory opens it: a protocol error, a
        rejected bind or a configuration fault does not. A session's own idle and
        absolute timeouts apply throughout.
    :param ldap_lock_attribute: the directory attribute that marks an account as
        locked (``nsAccountLock`` on FreeIPA / 389 Directory Server). It is
        requested explicitly because it is an operational attribute a plain ``*``
        search does not return. When its value is ``TRUE`` (any case) the
        account counts as locked: a directory login is refused and a live
        directory session is revoked at its next re-check. An attribute the bind
        account cannot read is simply absent and changes nothing. Empty disables
        the check.
    """

    password_min_length: int = 12
    throttle_max_failures: int = 5
    throttle_window_seconds: int = 300
    argon2_memory_kib: int = 19456
    argon2_time_cost: int = 2
    argon2_parallelism: int = 1
    throttle_global_max_failures: int = Field(default=100, ge=1)
    local_backoff_after_failures: int = Field(default=3, ge=1)
    ldap_breaker_seconds: int = Field(default=60, ge=1)
    ldap_lock_attribute: str = "nsAccountLock"


class StorageSettings(BaseModel):
    """Disk-space pause thresholds for the capture engine.

    Capture is *paused* when free space on a project's storage volume falls below
    the low watermark and *resumed* only once it recovers above the higher resume
    watermark; the gap between the two is a hysteresis band that prevents the gate
    from flapping. Each watermark has a byte floor and a percentage floor:

    * **Pause** triggers when free space is below the low byte floor **or** below
      the low percentage floor -- whichever is hit first (the conservative
      reading).
    * **Resume** requires free space above **both** resume floors.

    Low space only *pauses* capture; nothing is ever deleted to reclaim space.

    :param low_watermark_bytes: pause when free bytes fall below this.
    :param low_watermark_percent: pause when free percentage falls below this.
    :param resume_watermark_bytes: resume only once free bytes exceed this; must
        be at least ``low_watermark_bytes``.
    :param resume_watermark_percent: resume only once free percentage exceeds
        this; must be at least ``low_watermark_percent``.
    :param check_interval_seconds: minimum seconds between free-space probes per
        volume; the capture loop answers from a cached reading in between, so the
        check never adds I/O to a short capture interval.
    :param allowed_roots: absolute directories an *operator* may place a
        project's ``storage_path`` in (the path and its parent must both lie
        inside one of them). ``None`` (the default) means ``[paths.frames_root]``;
        an empty list lets only administrators set a custom path. Administrators
        are not limited by this list, but every caller is still kept out of the
        data, config, token and certificate directories (#67 S5).
    """

    low_watermark_bytes: int = 1_000_000_000
    low_watermark_percent: float = 5.0
    resume_watermark_bytes: int = 2_000_000_000
    resume_watermark_percent: float = 10.0
    check_interval_seconds: float = 60.0

    @model_validator(mode="after")
    def _check_hysteresis(self) -> StorageSettings:
        """Ensure each resume floor sits at or above its low floor."""
        if self.resume_watermark_bytes < self.low_watermark_bytes:
            raise ValueError("resume_watermark_bytes must be >= low_watermark_bytes")
        if self.resume_watermark_percent < self.low_watermark_percent:
            raise ValueError(
                "resume_watermark_percent must be >= low_watermark_percent"
            )
        return self

    allowed_roots: Annotated[list[Path] | None, NoDecode] = None

    _coerce_allowed_roots = field_validator("allowed_roots", mode="before")(
        _coerce_str_list
    )

    @field_validator("allowed_roots")
    @classmethod
    def _check_allowed_roots_absolute(
        cls, value: list[Path] | None
    ) -> list[Path] | None:
        """Require every allowed root to be an absolute path.

        A relative root would be resolved against whatever working directory the
        service happens to start in, so it is refused at load time instead.
        """
        if value is None:
            return None
        for root in value:
            if not root.is_absolute():
                raise ValueError(f"storage.allowed_roots entry {root} must be absolute")
        return value


class SsrfSettings(BaseModel):
    """Outbound-request guard policy for server-originated, user-influenced calls.

    A single guard mediates camera-add URL probes, ONVIF/range scans, and outbound
    webhook delivery. Loopback, link-local (incl. cloud metadata 169.254.169.254),
    and other special-use ranges are always blocked. Cameras normally live on
    private LANs, so an admin may opt **specific** private subnets into the allowed
    set for the camera/scan surfaces; the webhook surface always uses the full
    deny-list regardless of this opt-in.

    :param allowed_private_subnets: CIDR blocks (e.g. ``192.168.1.0/24``) an admin
        opts into for camera-add and scan targets; never relaxes loopback/
        link-local/metadata, and never applies to the webhook surface.
    :param max_scan_hosts: hard cap on the number of hosts a single range scan may
        probe, so a scan cannot be used as a wide network-sweep amplifier.
    """

    allowed_private_subnets: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    max_scan_hosts: int = 1024

    _coerce_subnets = field_validator("allowed_private_subnets", mode="before")(
        _coerce_str_list
    )


class SecretsSettings(BaseModel):
    """Where the at-rest encryption key lives and how it is obtained.

    Stored credentials (SMTP/webhook/camera/LDAP) are encrypted at rest; this
    section configures storage of the *key* that protects them. The key is taken
    from the host OS secret store when available, falling back to a
    restricted-permission key file for headless/Docker deployments.

    :param use_os_keystore: try the OS secret store (Keychain / Credential Manager
        / Secret Service) first; set ``False`` to force the key-file fallback on
        headless hosts where no Secret Service is reachable.
    :param keystore_service_name: service/account name for the OS keystore item.
    :param key_file: restricted-permission (``0600``) fallback key file; defaults
        to ``<data_dir>/.secret-key`` when unset. Created owner-only; the app
        refuses to start if it is found group- or world-readable.
    """

    use_os_keystore: bool = True
    keystore_service_name: str = "timelapse-manager"
    key_file: Path | None = None


class FramesSettings(BaseModel):
    """Limits on frame selections (issue #67, P9).

    :param max_range_ids: the most frame ids a range selection may resolve to
        when the server materialises it into an explicit id list. A larger range
        is refused with ``413`` so one request cannot build a multi-megabyte list.
    """

    max_range_ids: int = Field(default=50_000, ge=1)


class Settings(BaseSettings):
    """Root application settings.

    Sections are nested models; environment variables bind with the ``TLM_``
    prefix and ``__`` nesting delimiter.
    """

    model_config = SettingsConfigDict(
        env_prefix="TLM_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    tls: TlsSettings = Field(default_factory=TlsSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    ssrf: SsrfSettings = Field(default_factory=SsrfSettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
    reporting: ReportingSettings = Field(default_factory=ReportingSettings)
    frames: FramesSettings = Field(default_factory=FramesSettings)

    # Declarative definitions parsed from config but not provisioned to the
    # database in this phase.
    cameras: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
