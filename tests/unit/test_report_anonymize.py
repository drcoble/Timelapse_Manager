"""Unit tests for the report anonymizer and its fail-closed verifier (#21).

The pseudonymizer replaces identifying values with stable placeholders; the
verifier re-scans and refuses on anything left. Short or generic names (D16)
are replaced only in structured fields, refuse a GitHub report when they reach
free text, and are left alone in anonymize-only filing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from timelapse_manager.config.settings import (
    DatabaseSettings,
    LoggingSettings,
    PathsSettings,
    ReportingSettings,
    Settings,
)
from timelapse_manager.reports import anonymize
from timelapse_manager.reports.anonymize import (
    Entity,
    GenericNameError,
    Pseudonymizer,
    ReportAnonymizationError,
    collect_entities,
    is_weak,
    verify_all,
)
from timelapse_manager.reports.redaction import Scrubber

_SECRET = "Planted-S3cret-9911"


def _entities() -> list[Entity]:
    return [
        Entity("user", "carol-viewer"),
        Entity("user", "admin", weak=True),
        Entity("project", "Roofline Orchard"),
        Entity("camera", "Backyard Cam 7"),
        Entity("camera", "Pi", weak=True),
        *anonymize._host_entities("nas101.corp.example.com"),
        Entity("ip", "10.0.16.71"),
        Entity("path", "/srv/tlm-state", placeholder="/path/data"),
    ]


def _verifier(
    pseudonymizer: Pseudonymizer, *, refuse_weak: bool = True
) -> anonymize.Verifier:
    return pseudonymizer.verifier(Scrubber([_SECRET]), refuse_weak=refuse_weak)


# ---------------------------------------------------------------------------
# Weak names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Pi", "ab", "admin", "Camera", "2026", " test "])
def test_short_generic_and_numeric_names_are_weak(name: str) -> None:
    assert is_weak(name)


@pytest.mark.parametrize("name", ["Pie", "carol", "Roof", "garden-2026"])
def test_distinctive_names_are_not_weak(name: str) -> None:
    assert not is_weak(name)


# ---------------------------------------------------------------------------
# Free text
# ---------------------------------------------------------------------------


def test_replaces_entities_case_insensitively_with_stable_placeholders() -> None:
    p = Pseudonymizer(_entities())
    out = p.text(
        "carol-viewer opened ROOFLINE ORCHARD; Carol-Viewer again on Backyard Cam 7"
    )
    assert out == "user-1 opened project-1; user-1 again on camera-1"


def test_matches_are_bounded_by_non_alphanumerics() -> None:
    p = Pseudonymizer([Entity("project", "Roof")])
    # "_" and "-" separate words; letters and digits do not.
    assert p.text("Roof_0001.jpg roof-2 Roofline Roofs") == (
        "project-1_0001.jpg project-1-2 Roofline Roofs"
    )


def test_longest_entity_wins() -> None:
    p = Pseudonymizer([Entity("camera", "Roof"), Entity("camera", "Roof cam east")])
    assert p.text("Roof cam east and Roof") == "camera-1 and camera-2"


def test_url_encoded_and_json_escaped_forms_are_replaced() -> None:
    p = Pseudonymizer([Entity("camera", "Backyard Cam 7")])
    out = p.text("GET /c?name=Backyard%20Cam%207 or Backyard+Cam+7")
    assert "Backyard" not in out


def test_safety_net_patterns() -> None:
    p = Pseudonymizer([])
    out = p.text(
        "mail ops@corp.example ip 192.168.40.7 v6 fe80::1ff:fe23:4567:890a "
        "mac 00:1A:2B:3C:4D:5E host printer.lan home /home/alice/x /Users/jo/y "
        "url https://nas.example.org:8443/path rtsp://bob:pw@cam.example.org/s"
    )
    for leaked in (
        "ops@corp.example",
        "192.168.40.7",
        "fe80::1ff",
        "00:1A:2B",
        "printer.lan",
        "alice",
        "/Users/jo",
        "nas.example.org",
        "bob:pw",
        "cam.example.org",
    ):
        assert leaked not in out, leaked
    assert "https://host-" in out
    assert "rtsp://«anonymized»@host-" in out


def test_addresses_glued_to_words_are_replaced() -> None:
    p = Pseudonymizer([])
    out = p.text("cam_10.0.0.5.jpg host=192.168.1.20, v10.0.2.3")
    assert "10.0.0.5" not in out
    assert "192.168.1.20" not in out
    assert "10.0.2.3" not in out


def test_keys_are_replaced_only_when_wholly_a_strong_entity() -> None:
    p = Pseudonymizer(
        [Entity("camera", "Backyard Cam 7"), Entity("camera", "debug", weak=True)]
    )
    out = json.loads(p.member("x.json", b'{"Backyard Cam 7": 1, "debug": 2}'))
    assert set(out) == {"camera-1", "debug"}


def test_loopback_timestamps_and_versions_are_left_alone() -> None:
    p = Pseudonymizer([])
    text = (
        "2026-10-23 12:30:45,123 INFO bound 127.0.0.1 and ::1 on localhost, "
        "app 0.2.0 python 3.12.3 https://localhost:8443/x"
    )
    assert p.text(text) == text


def test_hosts_under_a_known_domain_and_the_domain() -> None:
    p = Pseudonymizer(anonymize._host_entities("nas101.corp.example.com"))
    out = p.text("ldap01.corp.example.com, nas101 and example.com")
    assert "ldap01" not in out
    assert "nas101" not in out
    assert "example.com" not in out


def test_paths_and_subpaths() -> None:
    p = Pseudonymizer([Entity("path", "/srv/tlm-state", placeholder="/path/data")])
    assert p.text("/srv/tlm-state/frames/a.jpg") == "/path/data/frames/a.jpg"


def test_two_thousand_line_log_is_fast() -> None:
    p = Pseudonymizer(
        _entities() + [Entity("camera", f"Camera Number {n}") for n in range(200)]
    )
    line = (
        "2026-10-23 12:00:00,000 INFO capture ok project Roofline Orchard camera "
        "Backyard Cam 7 at 10.0.16.71 file /srv/tlm-state/frames/x.jpg\n"
    )
    started = time.perf_counter()
    out = p.text(line * 2000)
    assert time.perf_counter() - started < 8.0
    assert "Roofline" not in out


# ---------------------------------------------------------------------------
# Structured fields and weak names
# ---------------------------------------------------------------------------


def test_weak_names_are_replaced_only_in_identity_fields() -> None:
    p = Pseudonymizer(_entities())
    data = json.dumps(
        {
            "username": "admin",
            "role": "admin",
            "name": "Pi",
            "message": "admin restarted Pi",
            "geolocation_latitude": 51.5,
            "geolocation_longitude": -0.12,
        }
    ).encode()
    out = json.loads(p.member("x.json", data))
    assert out["username"].startswith("user-")
    assert out["role"] == "admin"
    assert out["name"].startswith("camera-")
    # Free text keeps weak names: the log stays readable.
    assert out["message"] == "admin restarted Pi"
    assert out["geolocation_latitude"] is None
    assert out["geolocation_longitude"] is None


def test_github_mode_refuses_weak_names_in_free_text_and_names_them() -> None:
    p = Pseudonymizer(_entities())
    member = p.member(
        "events.json", json.dumps({"message": "Pi went offline"}).encode()
    )
    verifier = _verifier(p, refuse_weak=True)
    with pytest.raises(GenericNameError) as caught:
        verify_all(
            [lambda: verifier.check_member("events.json", member)],
            snapshot_optional=True,
        )
    message = caught.value.detail
    assert "camera name “Pi”" in message
    assert "rename that camera" in message
    assert "without the diagnostic snapshot" in message
    assert caught.value.hits[0].kind == "camera"


def test_weak_name_in_the_reporters_text_says_so() -> None:
    p = Pseudonymizer(_entities())
    verifier = _verifier(p)
    with pytest.raises(GenericNameError) as caught:
        verify_all(
            [lambda: verifier.check_text("title", "admin cannot log in")],
            snapshot_optional=True,
        )
    assert "user name “admin”" in caught.value.detail
    assert "your title or description" in caught.value.detail
    assert "Leave the name out" in caught.value.detail


def test_enumeration_values_never_trip_the_weak_check() -> None:
    p = Pseudonymizer(_entities())
    member = p.member(
        "request.json", json.dumps({"username": "admin", "role": "admin"}).encode()
    )
    verify_all(
        [lambda: _verifier(p).check_member("request.json", member)],
        snapshot_optional=True,
    )


def test_anonymize_only_mode_leaves_weak_names_and_passes() -> None:
    p = Pseudonymizer(_entities())
    verifier = _verifier(p, refuse_weak=False)
    verify_all(
        [lambda: verifier.check_text("log.txt", p.text("admin restarted Pi"))],
        snapshot_optional=True,
    )


def test_placeholders_do_not_trip_a_name_they_contain() -> None:
    # A camera literally called "camera" (weak) must not match "camera-1".
    p = Pseudonymizer([Entity("camera", "camera", weak=True), Entity("camera", "Yard")])
    verify_all(
        [lambda: _verifier(p).check_text("log.txt", p.text("Yard is up"))],
        snapshot_optional=True,
    )


def test_non_refusable_weak_os_user_never_refuses() -> None:
    p = Pseudonymizer([Entity("user", "pi", weak=True, refusable=False)])
    out = json.loads(p.member("r.json", json.dumps({"username": "pi"}).encode()))
    assert out["username"].startswith("user-")
    verify_all(
        [lambda: _verifier(p).check_text("log.txt", "sudo -u pi ran it")],
        snapshot_optional=False,
    )


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leftover",
    [
        "Roofline Orchard",
        "10.0.16.71",
        "nas101.corp.example.com",
        "/srv/tlm-state",
        "someone@example.org",
        "172.16.0.9",
        "aa:bb:cc:dd:ee:ff",
        "/home/alice",
        _SECRET,
    ],
)
def test_verifier_refuses_anything_left(leftover: str) -> None:
    p = Pseudonymizer(_entities())
    with pytest.raises(ReportAnonymizationError) as caught:
        verify_all(
            [lambda: _verifier(p).check_text("log.txt", f"x {leftover} y")],
            snapshot_optional=True,
        )
    assert not isinstance(caught.value, GenericNameError)
    # The value itself never goes into the message.
    assert leftover not in caught.value.detail


def test_verifier_refuses_a_key_that_is_an_entity() -> None:
    p = Pseudonymizer(_entities())
    data = json.dumps({"Roofline Orchard": 1}).encode()
    with pytest.raises(ReportAnonymizationError):
        _verifier(p).check_member("x.json", data)


def test_verifier_checks_the_decoded_url() -> None:
    p = Pseudonymizer(_entities())
    url = "https://github.com/o/r/issues/new?title=x&body=Roofline%20Orchard"
    with pytest.raises(ReportAnonymizationError):
        _verifier(p).check_url(url)


# ---------------------------------------------------------------------------
# Entity collection
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'tlm.db'}"),
        logging=LoggingSettings(file_sink=tmp_path / "logs" / "app.log"),
        paths=PathsSettings(data_dir=tmp_path / "state"),
        reporting=ReportingSettings(
            public_base_url="https://timelapse.corp.example.com",
            email_to=["maint@corp.example.com"],
        ),
        cameras=[{"name": "Config Gate Cam", "url": "rtsp://gate.example.lan/s"}],
    )


def test_collects_settings_machine_and_path_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anonymize.socket, "gethostname", lambda: "tlm-box-42")
    monkeypatch.setattr(anonymize.socket, "getfqdn", lambda: "tlm-box-42.lan")
    found = {(e.kind, e.value) for e in collect_entities(None, _settings(tmp_path))}
    assert ("host", "timelapse.corp.example.com") in found
    assert ("domain", "example.com") in found
    assert ("email", "maint@corp.example.com") in found
    assert ("camera", "Config Gate Cam") in found
    assert ("host", "gate.example.lan") in found
    assert ("host", "tlm-box-42") in found
    assert ("path", str(tmp_path / "state")) in found
    assert ("path", str(tmp_path / "logs" / "app.log")) in found


def test_reverse_dns_and_loopback_names_are_not_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert anonymize._host_entities("1.0.0.127.in-addr.arpa") == []
    assert anonymize._host_entities("localhost") == []
    assert anonymize._host_entities("127.0.0.1") == []


def test_generic_first_label_of_a_host_is_not_an_entity() -> None:
    found = {(e.kind, e.value) for e in anonymize._host_entities("timelapse.lan")}
    assert found == {("host", "timelapse.lan")}
    found = {(e.kind, e.value) for e in anonymize._host_entities("pi.home.example.com")}
    assert ("host", "pi") not in found
    assert ("domain", "example.com") in found


def test_weak_os_user_is_structured_only(tmp_path: Path) -> None:
    found = collect_entities(None, _settings(tmp_path), os_user="pi")
    [user] = [e for e in found if e.kind == "user"]
    assert user.weak and not user.refusable
