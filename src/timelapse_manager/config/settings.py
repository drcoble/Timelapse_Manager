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

import json
import re
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


class ServerSettings(BaseModel):
    """HTTP/HTTPS listener configuration."""

    http_port: int = 8080
    https_port: int = 8443
    bind_address: str = "0.0.0.0"
    redirect_http_to_https: bool = True


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
    """Logging level, output format, and optional file destination."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] = "json"
    file_sink: Path | None = None


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
    """

    autostart: bool = True
    poll_interval_seconds: float = 5.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    debounce_window_seconds: float = 60.0
    channel_send_timeout_seconds: float = 10.0


class ObservabilitySettings(BaseModel):
    """Operational observability surfaces exposed by the process.

    :param metrics_enabled: whether the Prometheus metrics endpoint is served.
        Off by default: while disabled the endpoint is invisible (it answers as
        though the route did not exist). When enabled it requires an
        administrator, since the listener binds all interfaces and there is no
        unauthenticated path to it.
    """

    metrics_enabled: bool = False


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
    """

    cookie_name: str = "tlm_session"
    idle_timeout_seconds: int = 1800
    absolute_timeout_seconds: int = 86400
    persistent_timeout_seconds: int = 2592000
    samesite: Literal["lax", "strict", "none"] = "lax"
    ldap_revalidation_interval_seconds: int = 900


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
    """

    password_min_length: int = 12
    throttle_max_failures: int = 5
    throttle_window_seconds: int = 300
    argon2_memory_kib: int = 19456
    argon2_time_cost: int = 2
    argon2_parallelism: int = 1


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

    # Declarative definitions parsed from config but not provisioned to the
    # database in this phase.
    cameras: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
