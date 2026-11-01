"""Regression tests for the PR #31 security review of the anonymizer (#21).

Each test reproduces one reviewed leak or fail-open. They failed against
bc81e2c and pass after the fix:

1. unknown absolute paths (storage, export, certs, fonts, ``/var/home``);
3. LDAP ``dc=`` runs;
4. membership oracle in the reporter's own text;
5. URL credentials containing ``/``;
6. separator, HTML, casefold, MAC and non-ASCII e-mail variants;
8. independent generic verifier checks (URL hosts, paths, ``dc=``, keys).

Findings 2 (entity source failure), 7 (log line) and the reporter name in
the stored row are covered in ``tests/web/test_report_github.py``.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from timelapse_manager.reports import anonymize
from timelapse_manager.reports.anonymize import (
    Entity,
    GenericNameError,
    Pseudonymizer,
    ReportAnonymizationError,
    verify_all,
)
from timelapse_manager.reports.redaction import Scrubber


def _verify(p: Pseudonymizer, check: str, data: str, *, weak: bool = True) -> None:
    verifier = p.verifier(Scrubber([]), refuse_weak=weak)
    if check == "member":
        verify_all(
            [lambda: verifier.check_member("x.json", data.encode())],
            snapshot_optional=True,
        )
    else:
        verify_all([lambda: verifier.check_text(check, data)], snapshot_optional=True)


def _refused(p: Pseudonymizer, where: str, data: str) -> None:
    with pytest.raises(ReportAnonymizationError) as caught:
        _verify(p, where, data)
    assert not isinstance(caught.value, GenericNameError)


# ---------------------------------------------------------------------------
# 1. Paths
# ---------------------------------------------------------------------------

_PRIVATE_PATHS = (
    "/mnt/nas-media/SmithFamily/roofline",
    "/media/alice-usb/timelapses",
    "/srv/certs/alice-home.pem",
    "/home/alice/fonts/Inter.ttf",
    "/var/home/alice/x.log",
)


def test_path_fields_are_replaced_outright() -> None:
    p = Pseudonymizer([])
    member = {
        "project": {
            "storage_path": _PRIVATE_PATHS[0],
            "post_render_actions": [
                {"type": "export", "destination": _PRIVATE_PATHS[1]}
            ],
        },
        "latest_frames": [{"file_path": f"{_PRIVATE_PATHS[0]}/frames/1.jpg"}],
        "render_jobs": [{"output_file_path": f"{_PRIVATE_PATHS[0]}/out.mp4"}],
        "tls": {"cert_path": _PRIVATE_PATHS[2], "key_path": "relative/alice.key"},
        "render": {"font_path": _PRIVATE_PATHS[3]},
        "ldap": {"tls_ca_cert_path": "/etc/ssl/smith-ca.pem"},
    }
    out = p.member("project.json", json.dumps(member).encode()).decode()
    for secret in ("nas-media", "SmithFamily", "alice", "smith-ca", "certs"):
        assert secret not in out, secret
    _verify(p, "member", out)


def test_unknown_paths_in_free_text_are_replaced() -> None:
    p = Pseudonymizer([])
    out = p.text(" ".join(f"wrote {path}" for path in _PRIVATE_PATHS))
    for secret in ("nas-media", "SmithFamily", "alice", "certs"):
        assert secret not in out, secret
    _verify(p, "log.txt", out)


def test_generic_system_and_app_paths_are_kept() -> None:
    p = Pseudonymizer([])
    text = (
        "ran /usr/bin/ffmpeg from /opt/timelapse-manager/_internal/x.py; "
        "GET /settings/reports and /projects/3/frames; image/png 1/2"
    )
    assert p.text(text) == text
    _verify(p, "log.txt", text)


def test_app_users_route_is_not_a_home_directory() -> None:
    """Found by a flaky full-suite run: ``/users/3/edit`` tripped the verifier."""
    p = Pseudonymizer([])
    text = json.dumps({"recent_requests": [{"path": "/users/3/edit"}]})
    assert json.loads(p.member("request.json", text.encode())) == json.loads(text)
    _verify(p, "member", text)
    _refused(p, "log.txt", "opened /Users/alice/x")


def test_verifier_refuses_an_unknown_absolute_path() -> None:
    _refused(Pseudonymizer([]), "log.txt", "saved to /mnt/private-media/x")


# ---------------------------------------------------------------------------
# 3. LDAP dc= runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dn",
    [
        "uid=x,cn=accounts,dc=example,dc=com",
        "UID=x, CN=accounts, DC=example, DC=com",
        "cn=users,dc=acme,dc=org",
    ],
)
def test_dc_runs_are_replaced(dn: str) -> None:
    p = Pseudonymizer(anonymize._host_entities("ldap01.example.com"))
    out = p.text(f"bind {dn} failed")
    assert "example" not in out
    assert "acme" not in out
    _verify(p, "log.txt", out)


def test_verifier_refuses_a_dc_run() -> None:
    _refused(Pseudonymizer([]), "log.txt", "base dc=acme,dc=org")


# ---------------------------------------------------------------------------
# 4. Membership oracle in the reporter's own text
# ---------------------------------------------------------------------------


def test_reporter_text_uses_kindless_uncorrelated_placeholders() -> None:
    p = Pseudonymizer([Entity("project", "Garden"), Entity("user", "site-admin")])
    zip_text = p.text("Garden by site-admin")
    typed = p.user_text("Garden and site-admin, not Gardn")
    assert typed == "name-1 and name-2, not Gardn"
    assert "project-" not in typed and "user-" not in typed
    # Numbering is separate from the zip's, so the two cannot be lined up.
    assert zip_text == "project-1 by user-1"


# ---------------------------------------------------------------------------
# 5. URL credentials
# ---------------------------------------------------------------------------


def test_url_credentials_with_slash_are_removed() -> None:
    p = Pseudonymizer([])
    out = p.text("rtsp://bob:pa/ss@cam.example.org/stream")
    assert "bob" not in out
    assert "pa" not in out.replace("«anonymized»", "")
    assert "cam.example.org" not in out


def test_verifier_refuses_url_credentials() -> None:
    _refused(Pseudonymizer([]), "log.txt", "rtsp://bob:pw@host-1/s")


# ---------------------------------------------------------------------------
# 6. Variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "North_Side",
        "NorthSide",
        "North  Side",
        "North\nSide",
        "north-side",
        "NORTH SIDE",
    ],
)
def test_separator_variants(variant: str) -> None:
    p = Pseudonymizer([Entity("project", "North Side")])
    assert "east" not in p.text(f"x {variant} y").casefold()


def test_html_entity_variant() -> None:
    p = Pseudonymizer([Entity("camera", "Back & Yard")])
    assert "Back" not in p.text("<td>Back &amp; Yard</td>")


def test_casefold_variant() -> None:
    p = Pseudonymizer([Entity("project", "Straße Cam")])
    assert "STRASSE" not in p.text("STRASSE CAM failed")


@pytest.mark.parametrize(
    "mac", ["001122AABBCC", "0011.22aa.bbcc", "b8:a4:4f:b4:8e:95", "B8-A4-4F-B4-8E-95"]
)
def test_mac_forms(mac: str) -> None:
    p = Pseudonymizer([])
    out = p.text(f"serial {mac} up")
    assert mac not in out
    _verify(p, "log.txt", out)


def test_axis_host_is_replaced_whole() -> None:
    p = Pseudonymizer([])
    out = p.text("connected to axis-b8a44fb48e95 ok")
    assert "b8a44fb48e95" not in out
    assert "axis-" not in out


def test_long_numbers_are_not_macs() -> None:
    p = Pseudonymizer([])
    assert p.text("wrote 104857600000 bytes") == "wrote 104857600000 bytes"


def test_non_ascii_email_domain() -> None:
    p = Pseudonymizer([])
    out = p.text("mail jo@bücher.de now")
    assert "bücher" not in out
    _verify(p, "log.txt", out)


# ---------------------------------------------------------------------------
# 8. Independent generic verifier checks
# ---------------------------------------------------------------------------


def test_verifier_refuses_a_leftover_url_host() -> None:
    _refused(Pseudonymizer([]), "log.txt", "GET https://nas.private-net.example/x")


def test_verifier_refuses_a_weak_name_as_a_json_key() -> None:
    p = Pseudonymizer([Entity("camera", "Pi", weak=True)])
    with pytest.raises(GenericNameError):
        _verify(p, "member", json.dumps({"Pi": {"ok": True}}))


def test_app_route_roots_cover_every_route() -> None:
    """The free-text path allow-list knows every first segment the app serves."""
    from timelapse_manager.app import create_app
    from timelapse_manager.config.settings import Settings

    app = create_app(Settings())
    roots = {
        str(getattr(route, "path", "")).strip("/").split("/", 1)[0]
        for route in app.routes
    }
    assert roots - {""} <= anonymize.APP_ROUTE_ROOTS


# ---------------------------------------------------------------------------
# Re-review of b5f1811 (R1-R4, ReDoS, performance)
# ---------------------------------------------------------------------------

_SPACED = "/mnt/nas media/Smith Family"


@pytest.mark.parametrize(
    "line",
    [
        f"wrote frame {_SPACED}/2026/f.jpg",
        "ffmpeg -i '/srv/My Videos/in.mp4' out",
        'ffmpeg -i "/srv/My Videos/in.mp4" out',
        "wrote /srv/Smith & Sons/x/f.jpg",
        "wrote /srv/Smith (old)/x/f.jpg",
        "wrote /srv/Smith,Family/x/f.jpg",
        "wrote /srv/a=Smith/x/f.jpg",
        "wrote /srv/Smith#1/x/f.jpg",
        "wrote /srv/Smith?/x/f.jpg",
    ],
)
def test_r1_paths_with_spaces_and_punctuation(line: str) -> None:
    p = Pseudonymizer([Entity("path", _SPACED, placeholder="/path/storage")])
    out = p.text(line)
    for word in ("Smith", "Family", "Videos", "nas media", "Sons"):
        assert word not in out, (word, out)
    _verify(p, "log.txt", out)


def test_r1_known_spaced_path_keeps_app_names_only() -> None:
    p = Pseudonymizer([Entity("path", _SPACED, placeholder="/path/storage")])
    assert p.text(f"wrote {_SPACED}/2026/f.jpg in 2 s") == (
        "wrote /path/storage/2026/f.jpg in 2 s"
    )
    # A single word before the end of the line goes with the path (it
    # could be the rest of an unquoted file name).
    assert p.text(f"wrote {_SPACED}/2026/f.jpg now") == "wrote /path/1"


def test_r1_known_path_glued_to_more_words_is_replaced_whole() -> None:
    p = Pseudonymizer([Entity("path", _SPACED, placeholder="/path/storage")])
    out = p.text(f"wrote {_SPACED} old/f.jpg")
    assert "old" not in out and "Smith" not in out
    _verify(p, "log.txt", out)


def test_r1_verifier_refuses_a_path_tail_after_a_placeholder() -> None:
    p = Pseudonymizer([Entity("path", _SPACED, placeholder="/path/storage")])
    p.text(_SPACED)  # issue the placeholder
    _refused(p, "log.txt", "wrote /path/storage media/Smith Family/f.jpg")


def test_r2_real_directory_under_a_route_name_is_replaced() -> None:
    p = Pseudonymizer([])
    out = p.text("rendered /projects/AcmeCorp/out/x.mp4 ok")
    assert "AcmeCorp" not in out
    _refused(p, "log.txt", "rendered /projects/AcmeCorp/out/x.mp4 ok")


def test_r2_exact_route_shapes_are_kept() -> None:
    p = Pseudonymizer([])
    text = (
        "GET /projects/3/frames/12/thumbnail and "
        "/settings/reports/TLM-20261023-0001/download and /users/5/edit"
    )
    assert p.text(text) == text
    _verify(p, "log.txt", text)


def test_r3_dot_dot_never_rides_on_an_allowed_prefix() -> None:
    p = Pseudonymizer([])
    out = p.text("loaded /opt/timelapse-manager/../../srv/AcmeCorp/y")
    assert "AcmeCorp" not in out
    _refused(p, "log.txt", "loaded /opt/timelapse-manager/../../srv/AcmeCorp/y")


def test_r4_known_path_under_a_system_prefix_is_replaced(tmp_path: object) -> None:
    font = "/usr/share/fonts/AcmeCorp-Brand/x.ttf"
    found = anonymize._path_entities_from([("font", font)])
    p = Pseudonymizer(found)
    out = p.text(f"font {font} loaded")
    assert "AcmeCorp" not in out
    _refused(p, "log.txt", f"font {font} loaded")


_PATHOLOGICAL = {
    "schemes": "a://" * 25_000,
    "word": "x" * 100_000,
    "ats": "a@" * 50_000,
    "spaced-path": "/a b" * 25_000,
    "paren-path": "/a(" * 33_000,
    "dc": "dc=a," * 20_000,
    "dots": "a." * 50_000,
    "colons": "a:" * 50_000,
    "digits": "1." * 50_000,
    "open-quote": "'/" + "a " * 50_000,
}


@pytest.mark.parametrize(
    "payload", list(_PATHOLOGICAL.values()), ids=list(_PATHOLOGICAL)
)
def test_redos_pathological_input_is_fast(payload: str) -> None:
    import time

    p = Pseudonymizer(
        [
            Entity("project", "North Side"),
            Entity("path", _SPACED, placeholder="/path/s"),
        ]
    )
    verifier = p.verifier(Scrubber([]), refuse_weak=True)
    started = time.perf_counter()
    out = p.text(payload)
    with contextlib.suppress(ReportAnonymizationError):
        verifier.check_text("log.txt", out)
    with contextlib.suppress(ReportAnonymizationError):
        verifier.check_text("log.txt", payload)
    assert time.perf_counter() - started < 6.0


def test_many_entities_and_lines_stay_fast() -> None:
    import time

    entities = [Entity("project", f"Project number {n}") for n in range(3000)]
    entities += [Entity("user", f"user.name{n}") for n in range(3000)]
    entities += [Entity("camera", f"Cam {n} east") for n in range(3000)]
    p = Pseudonymizer(entities)
    lines = "".join(
        f"2026-10-23 12:00:{n % 60:02d},000 INFO Project number {n} by user.name{n} "
        f"on Cam {n} east wrote /srv/x/{n}.jpg\n"
        for n in range(4000)
    )
    started = time.perf_counter()
    out = p.text(lines)
    p.verifier(Scrubber([]), refuse_weak=True).check_text("log.txt", out)
    assert time.perf_counter() - started < 8.0


def test_database_url_path_is_replaced_and_verifies() -> None:
    """Regression: a lone ``/`` before a placeholder is not a leftover path."""
    p = Pseudonymizer([Entity("path", "/srv/tlm/tlm.db", placeholder="/path/db")])
    out = p.text("sqlite:////srv/tlm/tlm.db")
    assert out == "sqlite:////path/db"
    _verify(p, "log.txt", out)


def test_every_route_has_a_recognised_shape() -> None:
    """R2: each route, with sample ids, is an exact route shape."""
    import re as _re

    from timelapse_manager.app import create_app
    from timelapse_manager.config.settings import Settings

    samples = {"key": "TLM-20261023-0001", "action": "pause"}
    for route in create_app(Settings()).routes:
        path = str(getattr(route, "path", ""))
        if not path.strip("/") or "{path" in path:
            continue
        concrete = _re.sub(
            r"\{([a-z_]+)(?::[a-z]+)?\}",
            lambda m: samples.get(m.group(1), "7"),
            path,
        )
        assert anonymize._is_route_shape(concrete), path


# ---------------------------------------------------------------------------
# Re-review of bf93031: overlapping names, route queries, trailing words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entities", "line", "leak"),
    [
        (
            [Entity("project", "Back Yard"), Entity("camera", "Backyard Smith Cam")],
            "camera Backyard Smith Cam offline",
            "Smith",
        ),
        (
            [Entity("project", "North Side"), Entity("project", "Northside Garden")],
            "Northside Garden paused",
            "Garden",
        ),
        (
            # "ab" vs "a b cd" against "ab cd": the joined spelling of a
            # shorter name must not win over the spaced longer one.
            [Entity("project", "Northyard"), Entity("project", "North Yard East")],
            "x Northyard East y",
            "East",
        ),
        (
            [Entity("camera", "Front"), Entity("camera", "Front Gate Cam")],
            "Front_Gate_Cam failed",
            "Gate",
        ),
    ],
)
def test_overlapping_names_take_the_longest(
    entities: list[Entity], line: str, leak: str
) -> None:
    p = Pseudonymizer(entities)
    for out in (p.text(line), p.user_text(line)):
        assert leak not in out, out
        _verify(p, "log.txt", out)


def test_verifier_refuses_a_name_cut_short() -> None:
    """Independent of the matcher: a placeholder plus what follows it must not
    complete a longer known name."""
    p = Pseudonymizer(
        [Entity("project", "Back Yard"), Entity("camera", "Backyard Smith Cam")]
    )
    short = p.whole_value("Back Yard")
    assert short is not None
    _refused(p, "log.txt", f"camera {short} Smith Cam offline")


@pytest.mark.parametrize(
    "line",
    [
        "GET /projects/3?x=/mnt/secret/dir",
        "GET /projects/3#/mnt/secret/dir",
    ],
)
def test_route_with_a_path_in_its_query_is_not_a_route(line: str) -> None:
    p = Pseudonymizer([])
    out = p.text(line)
    assert "secret" not in out
    _verify(p, "log.txt", out)
    _refused(p, "log.txt", line)


def test_route_with_a_plain_query_is_kept() -> None:
    p = Pseudonymizer([])
    line = "GET /projects/3/frames?page=2"
    assert p.text(line) == line
    _verify(p, "log.txt", line)


@pytest.mark.parametrize(
    "line",
    [
        "copied to /srv/nas share/Smith Family",
        "copied to /srv/nas share/Smith Family Photos",
        "x '/srv/nas share/Smith Family' y",
        "saved /srv/x/out.mp4 Smith",
    ],
)
def test_trailing_words_of_a_spaced_path_are_replaced(line: str) -> None:
    p = Pseudonymizer([])
    out = p.text(line)
    assert "Smith" not in out and "Family" not in out, out
    _verify(p, "log.txt", out)


def test_stack_frame_lines_are_not_trailing_words() -> None:
    """``file:line function`` (thread dumps) keeps the function name."""
    p = Pseudonymizer([])
    out = p.text("/srv/app/x.py:336 threads_snapshot")
    assert out.endswith(":336 threads_snapshot")
    _verify(p, "log.txt", out)


def test_verifier_refuses_a_word_after_a_path_at_end_of_line() -> None:
    p = Pseudonymizer([])
    p.text("/srv/x")  # issue /path/1
    _refused(p, "log.txt", "copied to /path/1 Family")
