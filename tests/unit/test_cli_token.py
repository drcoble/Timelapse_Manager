"""CLI token handling (#67 S2): read-only use, and ``token rotate``.

The CLI never creates the token file: only the service may, so the file is
owned by the service account (a CLI run as root would otherwise leave a
root-owned 0600 file the service cannot read). ``token rotate`` replaces an
existing file and prints no secret.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from timelapse_manager.cli import main
from timelapse_manager.security.token import ensure_token_file


def _config(tmp_path: Path) -> tuple[Path, Path]:
    """A config file whose data dir is under ``tmp_path``; returns (config, data)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = tmp_path / "config.yml"
    config.write_text(
        yaml.dump(
            {
                "paths": {"data_dir": str(data_dir)},
                "database": {"url": f"sqlite:///{tmp_path / 'db.sqlite'}"},
            }
        ),
        encoding="utf-8",
    )
    return config, data_dir


def _run(*args: str) -> int:
    with pytest.raises(SystemExit) as excinfo:
        main(list(args))
    code = excinfo.value.code
    return int(code) if code is not None else 0


@pytest.mark.parametrize(
    "command",
    [
        ("system", "info"),
        ("project", "status"),
        ("camera", "add", "--name", "cam"),
    ],
)
def test_service_commands_do_not_create_a_missing_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
) -> None:
    config, data_dir = _config(tmp_path)

    def no_network(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise AssertionError("the CLI must not call the service without a token")

    monkeypatch.setattr("timelapse_manager.cli.httpx.get", no_network)
    monkeypatch.setattr("timelapse_manager.cli.httpx.request", no_network)

    assert _run("--config", str(config), *command) == 2
    assert not (data_dir / ".local-token").exists()
    assert "start the service once" in capsys.readouterr().err


def test_service_commands_send_the_existing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, data_dir = _config(tmp_path)
    token = ensure_token_file(data_dir / ".local-token")
    seen: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return httpx.Response(200, json={"app_version": "x"})

    monkeypatch.setattr("timelapse_manager.cli.httpx.get", fake_get)
    assert _run("--config", str(config), "system", "info", "--json") == 0
    assert seen["url"].startswith("http://127.0.0.1:")
    assert seen["headers"] == {"Authorization": f"Bearer {token}"}


def test_token_rotate_replaces_the_token_and_prints_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, data_dir = _config(tmp_path)
    path = data_dir / ".local-token"
    before = ensure_token_file(path)

    assert _run("--config", str(config), "token", "rotate") == 0

    after = path.read_text(encoding="utf-8")
    out = capsys.readouterr()
    assert after != before
    assert after not in out.out + out.err
    assert before not in out.out + out.err
    assert str(path) in out.out
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_token_rotate_metrics_rotates_only_the_metrics_token(
    tmp_path: Path,
) -> None:
    config, data_dir = _config(tmp_path)
    local_before = ensure_token_file(data_dir / ".local-token")
    metrics_before = ensure_token_file(data_dir / ".metrics-token")

    assert _run("--config", str(config), "token", "rotate", "--metrics") == 0

    assert (data_dir / ".local-token").read_text(encoding="utf-8") == local_before
    assert (data_dir / ".metrics-token").read_text(encoding="utf-8") != metrics_before


def test_token_rotate_without_a_token_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, data_dir = _config(tmp_path)
    assert _run("--config", str(config), "token", "rotate") == 2
    assert not (data_dir / ".local-token").exists()
    assert "start the service once" in capsys.readouterr().err
