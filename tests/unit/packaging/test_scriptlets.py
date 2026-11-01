"""Behaviour of the deb/rpm maintainer scripts (#67 R7, R16, R6 step 3).

Each script is run under ``sh`` with its fixed paths pointed into ``tmp_path``
and stub ``systemctl``/``su``/account tools on ``PATH`` that record their calls.

* ``preremove`` stops and disables the service only on a real removal (deb
  ``remove``/``purge``, rpm ``0``), never on an upgrade.
* ``postremove`` always reloads systemd and ``try-restart``s on an upgrade.
* ``postinstall`` changes ownership only of entries that are wrong (no
  full-tree ``chown -R``), never through a symlink, and on a failed migration
  skips the restart and exits non-zero.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "packaging" / "scripts"
_PINNED_PATH = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_RECORDER = """#!/bin/sh
echo "{name} $*" >> "{log}"
exit 0
"""

# su -s /bin/sh -c "<command>" <user>: record it, then run the command.
_SU = """#!/bin/sh
echo "su $*" >> "{log}"
exec /bin/sh -c "$4"
"""

# chown: record the working directory and arguments, then really run it (the
# test only ever asks for the caller's own user and one of its groups).
_CHOWN = """#!/bin/sh
echo "chown $PWD $*" >> "{log}"
exec {real} "$@"
"""

# systemctl: state-changing calls go to the main log; the read-only health queries
# the postinstall polls (is-active, show) are answered from FAKE_UNIT_STATES (one
# word per poll, the last repeating) and FAKE_EXEC_MAIN_STATUS, and logged apart,
# so call-list assertions stay exact. A unit that reports "active" for several
# polls before it fails is what a Type=simple service does when the key check
# refuses startup a few seconds after the fork.
_SYSTEMCTL = """#!/bin/sh
case "$1" in
    is-active)
        echo "systemctl $*" >> "{log}.queries"
        n=$(cat "{log}.polls" 2>/dev/null || echo 0)
        n=$((n + 1))
        echo "$n" > "{log}.polls"
        state=$(echo "${{FAKE_UNIT_STATES:-active}}" | cut -d' ' -f"$n")
        [ -n "$state" ] || state=$(echo "${{FAKE_UNIT_STATES:-active}}" \\
            | awk '{{print $NF}}')
        echo "$state"
        [ "$state" = active ] ;;
    show)
        echo "systemctl $*" >> "{log}.queries"
        echo "ExecMainStatus=${{FAKE_EXEC_MAIN_STATUS:-0}}" ;;
    *)
        echo "systemctl $*" >> "{log}" ;;
esac
exit 0
"""

# curl: log the URL, then exit with the next code in FAKE_CURL_RESULTS (one per
# call, the last repeating). Real curl exits 0 for ANY HTTP response (200, 400,
# 503) and 7 for a refused connection, so the stub speaks in exit codes. The
# default 7 keeps the state-based tests independent of the probe.
_CURL = """#!/bin/sh
echo "curl $*" >> "{log}.curl"
n=$(cat "{log}.curlpolls" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "{log}.curlpolls"
code=$(echo "${{FAKE_CURL_RESULTS:-7}}" | cut -d' ' -f"$n")
[ -n "$code" ] || code=$(echo "${{FAKE_CURL_RESULTS:-7}}" | awk '{{print $NF}}')
exit "$code"
"""

_FAKE_EXE = """#!/bin/sh
echo "exe $*" >> "{log}"
exit "${{FAKE_MIGRATE_RC:-0}}"
"""


@dataclass
class Harness:
    tmp: Path
    bin_dir: Path
    log: Path
    state: Path
    config: Path
    exe: Path
    run_dir: Path

    def calls(self, prefix: str = "") -> list[str]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [line for line in lines if line.startswith(prefix)]

    def tools_only_dir(self) -> Path:
        """A PATH holding the stubs plus the base tools but no curl."""
        only = self.tmp / "bin-nocurl"
        only.mkdir(exist_ok=True)
        for entry in self.bin_dir.iterdir():
            if entry.name != "curl" and not (only / entry.name).exists():
                (only / entry.name).symlink_to(entry)
        for tool in (
            "sh", "sed", "awk", "cat", "cut", "tail", "sleep", "find", "chmod",
            "dirname", "echo", "printf", "env", "grep", "tr", "head",
        ):  # fmt: skip
            found = shutil.which(tool, path="/usr/bin:/bin:/usr/sbin:/sbin")
            if found and not (only / tool).exists():
                (only / tool).symlink_to(found)
        assert shutil.which("curl", path=str(only)) is None
        return only

    def run(
        self,
        script: str,
        *args: str,
        user: str = "timelapse",
        group: str = "timelapse",
        env: dict[str, str] | None = None,
        without_curl: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        text = (_SCRIPTS / script).read_text(encoding="utf-8")
        assert _PINNED_PATH in text or script not in (
            "postinstall.sh",
            "preinstall.sh",
        )
        path = (
            str(self.tools_only_dir())
            if without_curl
            else f"{self.bin_dir}:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        text = text.replace(_PINNED_PATH, f"PATH={path}")
        text = text.replace("/var/lib/timelapse-manager", str(self.state))
        text = text.replace("/etc/timelapse-manager", str(self.config))
        text = text.replace("/run/systemd/system", str(self.run_dir))
        text = text.replace("/opt/timelapse-manager/timelapse-manager", str(self.exe))
        # The harness is not root: the "root" owner of the config tree is the caller.
        text = text.replace("CONFIG_OWNER=root", f"CONFIG_OWNER={_me()[0]}")
        text = text.replace("SERVICE_USER=timelapse", f"SERVICE_USER={user}")
        text = text.replace("SERVICE_GROUP=timelapse", f"SERVICE_GROUP={group}")
        copy = self.tmp / script
        copy.write_text(text, encoding="utf-8")
        full_env = {
            "PATH": path,
            "TLM_UPGRADE_HEALTH_INTERVAL": "0",
            **(env or {}),
        }
        return subprocess.run(
            ["sh", str(copy), *args],
            capture_output=True,
            text=True,
            env=full_env,
            check=False,
            timeout=60,
        )

    def curl_urls(self) -> list[str]:
        """The URL of each health probe the script made, in order."""
        log = self.tmp / "calls.log.curl"
        if not log.exists():
            return []
        return [line.split()[-1] for line in log.read_text().splitlines()]


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _write_exec(bin_dir / "systemctl", _SYSTEMCTL.format(log=log))
    _write_exec(bin_dir / "curl", _CURL.format(log=log))
    for name in ("getent", "id", "groupadd", "useradd"):
        _write_exec(bin_dir / name, _RECORDER.format(name=name, log=log))
    _write_exec(bin_dir / "su", _SU.format(log=log))
    real_chown = shutil.which("chown", path="/usr/sbin:/usr/bin:/sbin:/bin")
    assert real_chown is not None
    _write_exec(bin_dir / "chown", _CHOWN.format(log=log, real=real_chown))
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "etc"
    config.mkdir()
    exe = tmp_path / "timelapse-manager"
    _write_exec(exe, _FAKE_EXE.format(log=log))
    # Present by default: systemd is the running init. A test removes it to
    # model an image/rootfs build or a chroot.
    run_dir = tmp_path / "run-systemd-system"
    run_dir.mkdir()
    return Harness(tmp_path, bin_dir, log, state, config, exe, run_dir)


def _me() -> tuple[str, str]:
    """The caller's user name and the group of a file it creates here."""
    return pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name


class TestPreremove:
    @pytest.mark.parametrize("arg", ["remove", "purge", "0", None])
    def test_real_removal_stops_and_disables(
        self, harness: Harness, arg: str | None
    ) -> None:
        result = harness.run("preremove.sh", *([arg] if arg is not None else []))
        assert result.returncode == 0, result.stderr
        assert harness.calls("systemctl") == [
            "systemctl stop timelapse-manager.service",
            "systemctl disable timelapse-manager.service",
        ]

    @pytest.mark.parametrize(
        "args",
        [
            ("upgrade", "0.2.0"),
            ("failed-upgrade", "0.1.1"),
            ("deconfigure", "in-favour", "x", "1.0"),
            ("1",),
            ("2",),
        ],
    )
    def test_upgrade_leaves_the_service_alone(
        self, harness: Harness, args: tuple[str, ...]
    ) -> None:
        result = harness.run("preremove.sh", *args)
        assert result.returncode == 0, result.stderr
        assert harness.calls("systemctl") == []


class TestPostremove:
    @pytest.mark.parametrize("args", [("upgrade", "0.2.0"), ("1",), ("2",)])
    def test_upgrade_reloads_and_try_restarts(
        self, harness: Harness, args: tuple[str, ...]
    ) -> None:
        result = harness.run("postremove.sh", *args)
        assert result.returncode == 0, result.stderr
        assert harness.calls("systemctl") == [
            "systemctl daemon-reload",
            "systemctl try-restart timelapse-manager.service",
        ]

    @pytest.mark.parametrize(
        "args",
        [("remove",), ("purge",), ("0",), (), ("abort-install",), ("failed-upgrade",)],
    )
    def test_removal_only_reloads(
        self, harness: Harness, args: tuple[str, ...]
    ) -> None:
        result = harness.run("postremove.sh", *args)
        assert result.returncode == 0, result.stderr
        assert harness.calls("systemctl") == ["systemctl daemon-reload"]


class TestPreinstall:
    """The service account exists before the package's files are laid down."""

    @pytest.mark.parametrize(
        "args", [("install",), ("upgrade", "0.2.0"), ("1",), ("2",)]
    )
    def test_creates_group_and_user(
        self, harness: Harness, args: tuple[str, ...]
    ) -> None:
        # The stubs answer "not found" (exit 0 from getent/id is recorded only), so
        # force the missing case with failing lookups.
        for name in ("getent", "id"):
            _write_exec(
                harness.bin_dir / name,
                f'#!/bin/sh\necho "{name} $*" >> "{harness.log}"\nexit 1\n',
            )
        result = harness.run("preinstall.sh", *args)
        assert result.returncode == 0, result.stderr
        assert harness.calls("groupadd") == ["groupadd --system timelapse"]
        (useradd,) = harness.calls("useradd")
        assert "--system --gid timelapse" in useradd and useradd.endswith(" timelapse")
        assert "--no-create-home" in useradd
        assert harness.calls("systemctl") == []

    def test_is_idempotent_when_the_account_exists(self, harness: Harness) -> None:
        result = harness.run("preinstall.sh", "upgrade", "0.2.0")
        assert result.returncode == 0, result.stderr
        assert harness.calls("groupadd") == []
        assert harness.calls("useradd") == []


class TestPostinstallWithoutRunningSystemd:
    """An image or rootfs build has no systemd manager to talk to."""

    def test_enables_but_does_not_restart_or_wait(self, harness: Harness) -> None:
        harness.run_dir.rmdir()
        user, group = _me()
        result = harness.run(
            "postinstall.sh",
            "configure",
            user=user,
            group=group,
            env={"FAKE_UNIT_STATES": "failed", "FAKE_EXEC_MAIN_STATUS": "78"},
        )
        assert result.returncode == 0, result.stderr
        assert harness.calls("systemctl") == [
            "systemctl enable timelapse-manager.service"
        ]
        # No health wait, no misleading "not running" message, no probes.
        assert not (harness.tmp / "calls.log.queries").exists()
        assert harness.curl_urls() == []
        assert result.stderr == ""
        # The migration still runs: the database is ready for first boot.
        assert harness.calls("exe") == ["exe migrate"]

    def test_failed_migration_still_fails_the_script(self, harness: Harness) -> None:
        harness.run_dir.rmdir()
        user, group = _me()
        result = harness.run(
            "postinstall.sh",
            "configure",
            user=user,
            group=group,
            env={"FAKE_MIGRATE_RC": "1"},
        )
        assert result.returncode == 1
        assert "DATABASE MIGRATION FAILED" in result.stderr


class TestPostinstallMigration:
    @pytest.mark.parametrize("args", [("configure", "0.1.1"), ("1",), ("2",)])
    def test_successful_migration_restarts(
        self, harness: Harness, args: tuple[str, ...]
    ) -> None:
        user, group = _me()
        result = harness.run("postinstall.sh", *args, user=user, group=group)
        assert result.returncode == 0, result.stderr
        assert harness.calls("exe") == ["exe migrate"]
        (su_call,) = harness.calls("su ")
        assert su_call.endswith(f" {user}")
        assert f"TLM_DATABASE__URL=sqlite:///{harness.state}/timelapse.db" in su_call
        assert harness.calls("systemctl") == [
            "systemctl daemon-reload",
            "systemctl enable timelapse-manager.service",
            "systemctl restart timelapse-manager.service",
        ]

    def test_failed_migration_skips_restart_and_fails(self, harness: Harness) -> None:
        user, group = _me()
        result = harness.run(
            "postinstall.sh",
            "configure",
            "0.1.1",
            user=user,
            group=group,
            env={"FAKE_MIGRATE_RC": "1"},
        )
        assert result.returncode == 1
        assert "DATABASE MIGRATION FAILED" in result.stderr
        assert "db restore" in result.stderr
        assert harness.calls("exe") == ["exe migrate"]
        systemctl = harness.calls("systemctl")
        assert "systemctl restart timelapse-manager.service" not in systemctl
        assert not any("restart" in call for call in systemctl)

    def test_healthy_service_prints_no_warning(self, harness: Harness) -> None:
        user, group = _me()
        result = harness.run("postinstall.sh", "configure", user=user, group=group)
        assert result.returncode == 0, result.stderr
        assert "STARTUP REFUSED" not in result.stderr
        assert "not running after the upgrade" not in result.stderr

    def test_key_refusal_exit_78_prints_recovery_steps(self, harness: Harness) -> None:
        """#243: a unit failed with status 78 is reported distinctly."""
        user, group = _me()
        result = harness.run(
            "postinstall.sh",
            "configure",
            user=user,
            group=group,
            env={"FAKE_UNIT_STATES": "failed", "FAKE_EXEC_MAIN_STATUS": "78"},
        )
        assert result.returncode == 0, result.stderr
        assert "STARTUP REFUSED (exit 78)" in result.stderr
        assert "secrets status" in result.stderr
        assert "secrets reset --confirm" in result.stderr
        assert "LDAP bind password" in result.stderr
        assert "break-glass" in result.stderr
        assert "not running after the upgrade" not in result.stderr

    def test_refusal_after_several_active_polls_is_still_reported(
        self, harness: Harness
    ) -> None:
        """Type=simple: "active" for a while, then the key check exits 78.

        Declaring health after a few active polls would miss it.
        """
        user, group = _me()
        result = harness.run(
            "postinstall.sh",
            "configure",
            user=user,
            group=group,
            env={
                "FAKE_UNIT_STATES": "active active active active active failed",
                "FAKE_EXEC_MAIN_STATUS": "78",
            },
        )
        assert result.returncode == 0, result.stderr
        assert "STARTUP REFUSED (exit 78)" in result.stderr

    def test_other_failure_gets_a_generic_message(self, harness: Harness) -> None:
        user, group = _me()
        result = harness.run(
            "postinstall.sh",
            "configure",
            user=user,
            group=group,
            env={"FAKE_UNIT_STATES": "activating", "FAKE_EXEC_MAIN_STATUS": "3"},
        )
        assert result.returncode == 0, result.stderr
        assert "STARTUP REFUSED" not in result.stderr
        assert "not running after the upgrade" in result.stderr
        assert "state: activating, last exit status: 3" in result.stderr

    def test_failed_migration_does_not_poll_health(self, harness: Harness) -> None:
        user, group = _me()
        harness.run(
            "postinstall.sh",
            "configure",
            user=user,
            group=group,
            env={"FAKE_MIGRATE_RC": "1", "FAKE_UNIT_STATES": "failed"},
        )
        assert not (harness.tmp / "calls.log.queries").exists()

    def test_missing_bundle_skips_migration(self, harness: Harness) -> None:
        user, group = _me()
        harness.exe.unlink()
        result = harness.run("postinstall.sh", "configure", user=user, group=group)
        assert result.returncode == 0, result.stderr
        assert harness.calls("su ") == []


class TestPostinstallHealthProbe:
    """#273: the post-restart wait ends when /healthz answers, not after 15 s."""

    def _run(
        self, harness: Harness, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        user, group = _me()
        env = kwargs.pop("env", {})
        assert isinstance(env, dict)
        return harness.run(
            "postinstall.sh",
            "configure",
            user=user,
            group=group,
            env=env,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_stops_polling_as_soon_as_it_answers(self, harness: Harness) -> None:
        result = self._run(
            harness,
            env={"FAKE_UNIT_STATES": "active", "FAKE_CURL_RESULTS": "7 7 0"},
        )
        assert result.returncode == 0, result.stderr
        assert len(harness.curl_urls()) == 3
        assert (harness.tmp / "calls.log.polls").read_text().strip() == "3"
        assert result.stderr == ""

    @pytest.mark.parametrize("answer", ["0"])
    def test_any_http_answer_counts(self, harness: Harness, answer: str) -> None:
        """curl exits 0 for a 503 or a 400 too (no --fail), so those count."""
        text = (_SCRIPTS / "postinstall.sh").read_text(encoding="utf-8")
        assert "--fail" not in text and " -f " not in text
        result = self._run(harness, env={"FAKE_CURL_RESULTS": answer})
        assert result.returncode == 0, result.stderr
        assert len(harness.curl_urls()) == 1
        assert "not running" not in result.stderr

    def test_never_answering_waits_the_whole_window(self, harness: Harness) -> None:
        result = self._run(
            harness,
            env={"FAKE_UNIT_STATES": "activating", "FAKE_EXEC_MAIN_STATUS": "3"},
        )
        assert result.returncode == 0, result.stderr
        assert len(harness.curl_urls()) == 15
        assert "not running after the upgrade" in result.stderr

    def test_poll_ceiling_is_configurable(self, harness: Harness) -> None:
        result = self._run(
            harness,
            env={"FAKE_UNIT_STATES": "activating", "TLM_UPGRADE_HEALTH_POLLS": "4"},
        )
        assert result.returncode == 0, result.stderr
        assert len(harness.curl_urls()) == 4

    def test_active_but_silent_unit_is_not_a_failure(self, harness: Harness) -> None:
        """A wrong port guess must not report a healthy unit as failed."""
        result = self._run(harness, env={"FAKE_UNIT_STATES": "active"})
        assert result.returncode == 0, result.stderr
        assert len(harness.curl_urls()) == 15
        assert result.stderr == ""

    def test_failed_unit_ends_the_wait_without_probing_on(
        self, harness: Harness
    ) -> None:
        result = self._run(
            harness,
            env={
                "FAKE_UNIT_STATES": "active active failed",
                "FAKE_EXEC_MAIN_STATUS": "78",
            },
        )
        assert "STARTUP REFUSED (exit 78)" in result.stderr
        assert len(harness.curl_urls()) == 2

    def test_without_curl_falls_back_to_the_unit_state(self, harness: Harness) -> None:
        result = self._run(
            harness,
            without_curl=True,
            env={
                "FAKE_UNIT_STATES": "active active failed",
                "FAKE_EXEC_MAIN_STATUS": "78",
            },
        )
        assert result.returncode == 0, result.stderr
        assert "STARTUP REFUSED (exit 78)" in result.stderr
        assert harness.curl_urls() == []
        healthy = self._run(
            harness, without_curl=True, env={"FAKE_UNIT_STATES": "active"}
        )
        assert healthy.returncode == 0 and healthy.stderr == ""

    def test_defaults_probe_loopback_8080(self, harness: Harness) -> None:
        self._run(harness, env={"FAKE_CURL_RESULTS": "0"})
        assert harness.curl_urls() == ["http://127.0.0.1:8080/healthz"]

    @pytest.mark.parametrize(
        ("bind", "host"),
        [
            ("0.0.0.0", "127.0.0.1"),
            ("::", "[::1]"),
            ("10.0.5.20", "10.0.5.20"),
            ("fd00::5", "[fd00::5]"),
            ("localhost", "localhost"),
            ("evil;rm -rf /", "127.0.0.1"),
        ],
    )
    def test_bind_address_from_config_yml(
        self, harness: Harness, bind: str, host: str
    ) -> None:
        (harness.config / "config.yml").write_text(
            f"server:\n  http_port: 9090  # note\n  bind_address: '{bind}'\n"
            "other:\n  http_port: 1111\n",
            encoding="utf-8",
        )
        self._run(harness, env={"FAKE_CURL_RESULTS": "0"})
        assert harness.curl_urls() == [f"http://{host}:9090/healthz"]

    def test_env_file_overrides_config_yml(self, harness: Harness) -> None:
        (harness.config / "config.yml").write_text(
            "server:\n  http_port: 9090\n  bind_address: 10.0.0.9\n", encoding="utf-8"
        )
        (harness.config / "timelapse-manager.env").write_text(
            'TLM_LOGGING__LEVEL=WARNING\nTLM_SERVER__HTTP_PORT="7000"\n'
            "TLM_SERVER__BIND_ADDRESS=0.0.0.0\n",
            encoding="utf-8",
        )
        self._run(harness, env={"FAKE_CURL_RESULTS": "0"})
        assert harness.curl_urls() == ["http://127.0.0.1:7000/healthz"]

    @pytest.mark.parametrize("port", ["abc", "0", "70000", "80 80", ""])
    def test_unusable_port_falls_back_to_default(
        self, harness: Harness, port: str
    ) -> None:
        (harness.config / "timelapse-manager.env").write_text(
            f"TLM_SERVER__HTTP_PORT={port}\n", encoding="utf-8"
        )
        self._run(harness, env={"FAKE_CURL_RESULTS": "0"})
        assert harness.curl_urls() == ["http://127.0.0.1:8080/healthz"]

    def test_probe_is_bounded_and_ignores_proxies(self, harness: Harness) -> None:
        self._run(harness, env={"FAKE_CURL_RESULTS": "0"})
        call = (harness.tmp / "calls.log.curl").read_text()
        assert "--max-time" in call and "--connect-timeout" in call
        assert "--noproxy *" in call
        assert "-L" not in call.split()


_HELPERS = re.compile(
    r"^# --- BEGIN health helpers.*?^# --- END health helpers$", re.S | re.M
)


class TestInstallScriptHealth:
    """#266: install.sh reports a post-start failure with the shared helpers."""

    def _block(self, script: Path) -> str:
        match = _HELPERS.search(script.read_text(encoding="utf-8"))
        assert match is not None
        return match.group(0)

    def test_helpers_are_identical_in_both_scripts(self) -> None:
        assert self._block(_SCRIPTS.parent / "install.sh") == self._block(
            _SCRIPTS / "postinstall.sh"
        )

    def test_install_script_syntax(self) -> None:
        bash = shutil.which("bash")
        assert bash is not None
        subprocess.run([bash, "-n", str(_SCRIPTS.parent / "install.sh")], check=True)

    def _run_report(
        self, harness: Harness, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Run install.sh's report_service_health under bash -euo pipefail."""
        text = (_SCRIPTS.parent / "install.sh").read_text(encoding="utf-8")
        start = text.index("report_service_health() {")
        end = text.index("\n}\n", start) + 3
        script = (
            "set -euo pipefail\n"
            f"CONFIG_DIR={harness.config}\nSERVICE_NAME=timelapse-manager\n"
            f"SERVICE_USER=timelapse\nSTATE_DIR={harness.state}\n"
            "INSTALL_DIR=/opt/timelapse-manager\n"
            f"{self._block(_SCRIPTS.parent / 'install.sh')}\n"
            f"{text[start:end]}\nreport_service_health\necho rc=$?\n"
        )
        bash = shutil.which("bash")
        assert bash is not None
        return subprocess.run(
            [bash, "-c", script],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{harness.bin_dir}:/usr/sbin:/usr/bin:/sbin:/bin",
                "TLM_UPGRADE_HEALTH_INTERVAL": "0",
                **env,
            },
            check=False,
            timeout=60,
        )

    def test_healthy_start_is_silent(self, harness: Harness) -> None:
        result = self._run_report(
            harness, {"FAKE_UNIT_STATES": "active", "FAKE_CURL_RESULTS": "7 0"}
        )
        assert result.returncode == 0, result.stderr
        assert "rc=0" in result.stdout
        assert result.stderr == ""
        assert len(harness.curl_urls()) == 2

    def test_unhealthy_start_gets_a_generic_warning(self, harness: Harness) -> None:
        result = self._run_report(
            harness, {"FAKE_UNIT_STATES": "activating", "FAKE_EXEC_MAIN_STATUS": "3"}
        )
        assert result.returncode == 0, result.stderr
        assert "is not running after the install" in result.stderr
        assert "last exit status: 3" in result.stderr
        assert "journalctl -u timelapse-manager" in result.stderr
        assert "secrets status" not in result.stderr

    def test_key_refusal_points_at_secrets_status(self, harness: Harness) -> None:
        result = self._run_report(
            harness, {"FAKE_UNIT_STATES": "failed", "FAKE_EXEC_MAIN_STATUS": "78"}
        )
        assert result.returncode == 0, result.stderr
        assert "secrets status" in result.stderr


class TestPostinstallOwnership:
    def test_config_is_root_owned_not_service_owned(self) -> None:
        text = (_SCRIPTS / "postinstall.sh").read_text(encoding="utf-8")
        assert "CONFIG_OWNER=root" in text
        assert 'find "$CONFIG_DIR"' in text
        assert '-execdir chown -h "$CONFIG_OWNER":"$SERVICE_GROUP"' in text
        # The state tree is the only thing chowned to the service account.
        assert 'find "$STATE_DIR" "$CONFIG_DIR"' not in text

    def test_config_group_is_the_service_group_and_dir_is_0750(
        self, harness: Harness
    ) -> None:
        user, group = _me()
        own_gid = os.getgid()
        other_gids = [g for g in os.getgroups() if g != own_gid]
        if not other_gids:
            pytest.skip("needs a second group to create a wrongly-owned file")
        conf = harness.config / "config.yml"
        conf.write_text("paths: {}\n", encoding="utf-8")
        os.chown(conf, -1, other_gids[0])
        os.chown(harness.config, -1, own_gid)
        os.chown(harness.state, -1, own_gid)
        result = harness.run("postinstall.sh", "configure", user=user, group=group)
        assert result.returncode == 0, result.stderr
        assert conf.stat().st_gid == own_gid
        assert (harness.config.stat().st_mode & 0o777) == 0o750
        chowned = harness.calls("chown")
        assert any("config.yml" in c for c in chowned)

    def test_no_recursive_chown(self) -> None:
        text = (_SCRIPTS / "postinstall.sh").read_text(encoding="utf-8")
        assert "chown -R" not in text
        assert "-execdir chown -h" in text

    def test_correct_tree_is_not_touched(self, harness: Harness) -> None:
        user, group = _me()
        frames = harness.state / "frames" / "1"
        frames.mkdir(parents=True)
        for n in range(50):
            (frames / f"{n:04d}.jpg").write_bytes(b"jpg")
        for path in (harness.state, harness.config, *harness.state.rglob("*")):
            os.lchown(path, -1, os.getgid())
        result = harness.run("postinstall.sh", "configure", user=user, group=group)
        assert result.returncode == 0, result.stderr
        assert harness.calls("chown") == []

    def test_only_wrong_entries_change_and_never_through_a_symlink(
        self, harness: Harness
    ) -> None:
        user, group = _me()
        own_gid = os.getgid()
        other_gids = [g for g in os.getgroups() if g != own_gid]
        if not other_gids:
            pytest.skip("needs a second group to create a wrongly-owned file")
        other_gid = other_gids[0]

        for path in (harness.state, harness.config):
            os.chown(path, -1, own_gid)
        sub = harness.state / "frames"
        sub.mkdir()
        right = sub / "right.jpg"
        right.write_bytes(b"x")
        wrong = sub / "wrong.jpg"
        wrong.write_bytes(b"x")
        os.chown(sub, -1, own_gid)
        os.chown(right, -1, own_gid)
        os.chown(wrong, -1, other_gid)
        # A file outside the tree, reachable only through a planted symlink
        # whose own group is wrong: chown must change the link, not the target.
        outside = harness.tmp / "outside"
        outside.write_bytes(b"secret")
        os.chown(outside, -1, other_gid)
        link = harness.state / "link"
        link.symlink_to(outside)
        os.lchown(link, -1, other_gid)

        result = harness.run("postinstall.sh", "configure", user=user, group=group)
        assert result.returncode == 0, result.stderr

        chowned = " ".join(harness.calls("chown"))
        assert "wrong.jpg" in chowned and "link" in chowned
        assert "right.jpg" not in chowned
        assert all(" -h " in call for call in harness.calls("chown"))
        assert wrong.stat().st_gid == own_gid
        assert link.lstat().st_gid == own_gid
        assert outside.stat().st_gid == other_gid
        assert (harness.state.stat().st_mode & 0o777) == 0o750
