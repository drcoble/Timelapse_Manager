"""Tests for the service runner's connection, TLS and shutdown hardening.

Cover the slowloris/connection-exhaustion limits carried by the constructed
Uvicorn configs, the TLS 1.2 minimum-version floor applied to the HTTPS
listener's SSL context, the shutdown coordinator and grace (#67 R4), and the
hardware-encoding device check (#67 R15). The coordinator is driven by calling
its handler directly; ``tests/e2e/test_service_shutdown.py`` sends real signals
to a real service.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import ssl
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from timelapse_manager.config import ServerSettings, Settings
from timelapse_manager.config.settings import RenderSettings
from timelapse_manager.service import (
    _build_http_config,
    _build_https_config,
    _CoordinatedServer,
    _floor_tls_minimum_version,
    _ShutdownCancelFilter,
    _ShutdownCoordinator,
    check_hwaccel_device_access,
    hwaccel_device_nodes,
)
from timelapse_manager.service.tls import ensure_tls_cert

# Distinctive, non-default values so the assertions prove the settings are wired
# through rather than coinciding with Uvicorn's own defaults.
_HARDENED_SERVER = ServerSettings(
    limit_concurrency=42,
    timeout_keep_alive_seconds=3,
    limit_max_requests=1000,
    h11_max_incomplete_event_size=8192,
)


def _with_server(settings: Settings, server: ServerSettings) -> Settings:
    """Return a copy of ``settings`` with its server section replaced."""
    return settings.model_copy(update={"server": server})


def test_https_config_carries_connection_limits(settings: Settings) -> None:
    """The HTTPS config forwards every connection-limit setting to Uvicorn."""
    hardened = _with_server(settings, _HARDENED_SERVER)

    config = _build_https_config(FastAPI(), hardened, Path("cert.pem"), Path("key.pem"))

    assert config.limit_concurrency == 42
    assert config.timeout_keep_alive == 3
    assert config.limit_max_requests == 1000
    assert config.h11_max_incomplete_event_size == 8192


def test_http_config_carries_connection_limits(settings: Settings) -> None:
    """The plaintext config forwards the same connection limits to Uvicorn."""
    hardened = _with_server(settings, _HARDENED_SERVER)

    config = _build_http_config(FastAPI(), hardened)

    assert config.limit_concurrency == 42
    assert config.timeout_keep_alive == 3
    assert config.limit_max_requests == 1000
    assert config.h11_max_incomplete_event_size == 8192


def test_http_config_binds_loopback_when_redirect_disabled(
    settings: Settings,
) -> None:
    """With redirection off the plaintext port is confined to loopback."""
    server = _HARDENED_SERVER.model_copy(update={"redirect_http_to_https": False})
    config = _build_http_config(FastAPI(), _with_server(settings, server))

    assert config.host == "127.0.0.1"


def test_floor_tls_minimum_version_raises_floor_to_tls12() -> None:
    """The SSL-context factory floors the negotiated TLS version at 1.2."""
    base = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    base.minimum_version = ssl.TLSVersion.TLSv1  # an obsolete floor to be raised

    context = _floor_tls_minimum_version(None, lambda: base)  # type: ignore[arg-type]

    assert context is base
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_https_ssl_context_floors_tls_after_load(settings: Settings) -> None:
    """Loading the HTTPS config materializes a context with the TLS 1.2 floor.

    Exercises the full Uvicorn load path so the factory is verified as wired into
    the listener, not merely callable in isolation.
    """
    cert_path, key_path = ensure_tls_cert(settings)

    config = _build_https_config(FastAPI(), settings, cert_path, key_path)
    config.load()

    assert config.ssl is not None
    assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2


def test_both_configs_carry_the_shutdown_grace(settings: Settings) -> None:
    """Each listener cancels lingering connections after the grace (#67 R4)."""
    graced = _with_server(settings, ServerSettings(shutdown_grace_seconds=7))

    https = _build_https_config(FastAPI(), graced, Path("cert.pem"), Path("key.pem"))
    http = _build_http_config(FastAPI(), graced)

    assert https.timeout_graceful_shutdown == 7
    assert http.timeout_graceful_shutdown == 7


def test_shutdown_grace_defaults_to_ten_seconds() -> None:
    """The default leaves the unit's 45 s TimeoutStopSec room for teardown."""
    assert ServerSettings().shutdown_grace_seconds == 10


def test_shutdown_grace_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ServerSettings(shutdown_grace_seconds=0)


def _servers() -> tuple[uvicorn.Server, uvicorn.Server]:
    app = FastAPI()
    return uvicorn.Server(uvicorn.Config(app)), uvicorn.Server(uvicorn.Config(app))


def test_first_signal_stops_every_server_gracefully() -> None:
    """One SIGTERM asks both listeners to exit at once, without forcing."""
    https, http = _servers()
    coordinator = _ShutdownCoordinator((https, http))

    coordinator.handle_signal(signal.SIGTERM)

    assert coordinator.exit_requested
    assert https.should_exit and http.should_exit
    assert not https.force_exit and not http.force_exit


def test_second_signal_forces_every_server() -> None:
    """A second signal (any of the two) forces both listeners."""
    https, http = _servers()
    coordinator = _ShutdownCoordinator((https, http))

    coordinator.handle_signal(signal.SIGTERM)
    coordinator.handle_signal(signal.SIGINT)

    assert https.force_exit and http.force_exit


async def test_coordinator_installs_and_removes_loop_handlers() -> None:
    """Handlers go on the running loop and come off again; remove is idempotent."""
    loop = asyncio.get_running_loop()
    coordinator = _ShutdownCoordinator(_servers())
    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    try:
        assert coordinator.install(loop)
        try:
            assert signal.getsignal(signal.SIGTERM) not in (
                signal.SIG_DFL,
                signal.default_int_handler,
            )
        finally:
            coordinator.remove(loop)
        coordinator.remove(loop)
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    finally:
        for sig, handler in originals.items():
            signal.signal(sig, handler)


async def test_coordinator_falls_back_where_the_loop_has_no_signals() -> None:
    """A loop without signal support (Windows) keeps Uvicorn's own handling."""

    class _NoSignalLoop:
        def add_signal_handler(self, *args: object) -> None:
            raise NotImplementedError

        def remove_signal_handler(self, sig: int) -> bool:
            raise AssertionError("nothing was installed")

    coordinator = _ShutdownCoordinator(_servers())

    assert not coordinator.install(_NoSignalLoop())  # type: ignore[arg-type]


def test_coordinated_server_installs_no_signal_handlers() -> None:
    """Coordinated, a server leaves the process signal handlers alone."""
    server = _CoordinatedServer(uvicorn.Config(FastAPI()))
    server.signals_coordinated = True
    before = signal.getsignal(signal.SIGTERM)

    with server.capture_signals():
        assert signal.getsignal(signal.SIGTERM) is before


def test_uncoordinated_server_keeps_uvicorn_signal_handling() -> None:
    """Without the coordinator, Uvicorn's per-server handlers still apply."""
    server = _CoordinatedServer(uvicorn.Config(FastAPI()))
    before = signal.getsignal(signal.SIGTERM)

    with server.capture_signals():
        assert signal.getsignal(signal.SIGTERM) == server.handle_exit
    assert signal.getsignal(signal.SIGTERM) is before


async def test_lifespan_teardown_waits_for_the_hook() -> None:
    """The hook runs after the drain and before the lifespan shutdown."""
    calls: list[str] = []

    async def hook() -> None:
        calls.append("hook")

    class _Lifespan:
        async def shutdown(self) -> None:
            calls.append("lifespan")

    server = _CoordinatedServer(
        uvicorn.Config(FastAPI()), before_lifespan_shutdown=hook
    )
    server.servers = []
    server.lifespan = _Lifespan()  # type: ignore[assignment]

    await server.shutdown()

    assert calls == ["hook", "lifespan"]


def _render(**overrides: object) -> RenderSettings:
    return RenderSettings(**{"hwaccel_enabled": True, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"hwaccel_enabled": False, "hwaccel_api": "vaapi"}, []),
        ({"hwaccel_api": None}, []),
        ({"hwaccel_api": "bogus"}, []),
        ({"hwaccel_api": "vaapi"}, ["/dev/dri/renderD128"]),
        (
            {"hwaccel_api": "vaapi", "hwaccel_device": "/dev/dri/renderD129"},
            ["/dev/dri/renderD129"],
        ),
        ({"hwaccel_api": "qsv"}, ["/dev/dri/renderD128"]),
        ({"hwaccel_api": "qsv", "hwaccel_device": "hw"}, ["/dev/dri/renderD128"]),
        ({"hwaccel_api": "nvenc"}, ["/dev/nvidiactl", "/dev/nvidia0"]),
        (
            {"hwaccel_api": "nvenc", "hwaccel_device": "1"},
            ["/dev/nvidiactl", "/dev/nvidia1"],
        ),
    ],
)
def test_hwaccel_device_nodes(
    overrides: dict[str, object], expected: list[str]
) -> None:
    assert hwaccel_device_nodes(_render(**overrides)) == expected


def test_inaccessible_hwaccel_device_logs_error_naming_the_drop_in(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing render node is reported with the drop-in that grants it."""
    missing = str(tmp_path / "renderD128")
    configured = settings.model_copy(
        update={"render": _render(hwaccel_api="vaapi", hwaccel_device=missing)}
    )

    with caplog.at_level(logging.ERROR, logger="timelapse_manager.service"):
        denied = check_hwaccel_device_access(configured)

    assert denied == [missing]
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert missing in message
    assert "timelapse-manager.service.d/hwaccel.conf" in message
    assert "PrivateDevices" in message
    # Diagnostic only: the configuration is left as it was.
    assert configured.render.hwaccel_enabled
    assert configured.render.hwaccel_device == missing


def test_accessible_hwaccel_device_logs_nothing(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    node = tmp_path / "renderD128"
    node.write_bytes(b"")
    configured = settings.model_copy(
        update={"render": _render(hwaccel_api="vaapi", hwaccel_device=str(node))}
    )

    with caplog.at_level(logging.DEBUG, logger="timelapse_manager.service"):
        assert check_hwaccel_device_access(configured) == []
    assert caplog.records == []


def test_hwaccel_check_is_silent_when_hardware_encoding_is_off(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The default (CPU-only) configuration checks and logs nothing."""
    assert not settings.render.hwaccel_enabled

    with caplog.at_level(logging.DEBUG, logger="timelapse_manager.service"):
        assert check_hwaccel_device_access(settings) == []
    assert caplog.records == []


async def test_startup_failure_exits_non_zero(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#177: a failed startup must not exit 0, or Restart=on-failure ignores it."""
    import socket

    import timelapse_manager.service as service_mod

    async def failing_app(scope: dict, receive: object, send: object) -> None:
        assert scope["type"] == "lifespan"
        await receive()  # type: ignore[operator]
        await send({"type": "lifespan.startup.failed", "message": "boom"})  # type: ignore[operator]

    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    monkeypatch.setattr(service_mod, "create_app", lambda *_a, **_k: failing_app)
    server = settings.server.model_copy(
        update={
            "bind_address": "127.0.0.1",
            "https_port": free_port(),
            "http_port": free_port(),
        }
    )
    # ``asyncio.timeout`` keeps ``_serve`` in this task, as ``asyncio.run`` does in
    # production. ``asyncio.wait_for`` runs it in a child task on 3.11, where a
    # ``SystemExit`` escapes to the event loop instead of reaching ``raises`` (#192).
    with pytest.raises(SystemExit) as excinfo:
        async with asyncio.timeout(30):
            await service_mod._serve(_with_server(settings, server))
    assert excinfo.value.code == 3


async def test_stop_signal_during_startup_is_a_clean_exit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop requested before startup finished is not a failure (exit 0)."""
    import timelapse_manager.service as service_mod

    original_install = service_mod._ShutdownCoordinator.install

    def install_and_request_stop(
        self: service_mod._ShutdownCoordinator, loop: asyncio.AbstractEventLoop
    ) -> bool:
        installed = original_install(self, loop)
        self.handle_signal(signal.SIGTERM)
        return installed

    async def never_ready_app(scope: dict, receive: object, send: object) -> None:
        await receive()  # type: ignore[operator]
        await send({"type": "lifespan.startup.failed", "message": "stopping"})  # type: ignore[operator]

    monkeypatch.setattr(
        service_mod._ShutdownCoordinator, "install", install_and_request_stop
    )
    monkeypatch.setattr(service_mod, "create_app", lambda *_a, **_k: never_ready_app)
    server = settings.server.model_copy(
        update={"bind_address": "127.0.0.1", "https_port": 0, "http_port": 0}
    )
    await asyncio.wait_for(
        service_mod._serve(_with_server(settings, server)), timeout=30
    )


async def test_key_integrity_refusal_exits_with_the_no_restart_status(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#67 S8: a key mismatch exits 78, which the unit does not restart on."""
    import socket

    import timelapse_manager.service as service_mod
    from timelapse_manager.security import key_integrity

    async def refusing_app(scope: dict, receive: object, send: object) -> None:
        await receive()  # type: ignore[operator]
        key_integrity._refused = True
        await send({"type": "lifespan.startup.failed", "message": "key"})  # type: ignore[operator]

    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    monkeypatch.setattr(service_mod, "create_app", lambda *_a, **_k: refusing_app)
    server = settings.server.model_copy(
        update={
            "bind_address": "127.0.0.1",
            "https_port": free_port(),
            "http_port": free_port(),
        }
    )
    try:
        with pytest.raises(SystemExit) as excinfo:
            async with asyncio.timeout(30):
                await service_mod._serve(_with_server(settings, server))
    finally:
        key_integrity.reset_key_integrity_refusal()
    assert excinfo.value.code == 78


def test_unit_file_does_not_restart_on_the_key_refusal_status() -> None:
    """The systemd unit stops restarting on status 78 (#67 S8)."""
    unit = (
        Path(__file__).resolve().parents[2]
        / "packaging"
        / "systemd"
        / "timelapse-manager.service"
    ).read_text(encoding="utf-8")
    assert "RestartPreventExitStatus=78" in unit


# ---------------------------------------------------------------------------
# CancelledError log noise when the shutdown grace runs out (#179)
# ---------------------------------------------------------------------------


async def _hanging_app(scope, receive, send):  # type: ignore[no-untyped-def]
    """An ASGI app whose requests never finish (a stuck download)."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            kind = message["type"]
            await send({"type": kind + ".complete"})
            if kind == "lifespan.shutdown":
                return
    await asyncio.sleep(3600)


async def _stop_with_one_hung_request(
    *, filter_: _ShutdownCancelFilter | None
) -> list[logging.LogRecord]:
    """Run a real server, hang one request, stop it past the grace; return records."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = uvicorn.Config(
        _hanging_app,
        host="127.0.0.1",
        port=port,
        timeout_graceful_shutdown=1,
        log_config=None,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    # A private handler on the logger itself: another test's uvicorn log config
    # may have set ``propagate`` off, which would hide the records from caplog.
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    collector = _Collect()
    server_logger = logging.getLogger("uvicorn.error")
    server_logger.addHandler(collector)
    if filter_ is not None:
        server_logger.addFilter(filter_)
    try:
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        while not server.server_state.tasks:
            await asyncio.sleep(0.01)
        server.should_exit = True
        async with asyncio.timeout(15):
            await task
        writer.close()
    finally:
        server_logger.removeHandler(collector)
        if filter_ is not None:
            server_logger.removeFilter(filter_)
    return records


def _cancelled_tracebacks(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [
        r
        for r in records
        if r.exc_info is not None and isinstance(r.exc_info[1], asyncio.CancelledError)
    ]


async def test_unfiltered_uvicorn_logs_the_cancelled_request() -> None:
    """Reproduces #179: without the filter the grace overrun logs a traceback."""
    records = await _stop_with_one_hung_request(filter_=None)
    assert len(_cancelled_tracebacks(records)) == 1


async def test_filter_drops_and_counts_the_shutdown_cancellation() -> None:
    server_stopping = _ShutdownCancelFilter(lambda: True)
    records = await _stop_with_one_hung_request(filter_=server_stopping)
    assert _cancelled_tracebacks(records) == []
    assert server_stopping.cancelled == 1


def _record(exc: BaseException | None) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc is not None else None
    return logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, "x", None, exc_info
    )


def test_filter_keeps_cancellation_outside_a_shutdown() -> None:
    running = _ShutdownCancelFilter(lambda: False)
    assert running.filter(_record(asyncio.CancelledError())) is True
    assert running.cancelled == 0


def test_filter_keeps_other_errors_during_a_shutdown() -> None:
    stopping = _ShutdownCancelFilter(lambda: True)
    assert stopping.filter(_record(RuntimeError("real bug"))) is True
    assert stopping.filter(_record(None)) is True
    assert stopping.cancelled == 0
