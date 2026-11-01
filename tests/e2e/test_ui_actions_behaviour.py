"""Camera, user and frame controls behave without inline script (#70 WP-70-M2).

Their handlers were inline ``on*=`` attributes and ``<script>`` blocks; they are
now ``data-action`` controls registered in actions.js. Each converted control is
driven in a real page, and every flow must leave the page free of CSP violations
and page errors. Camera-facing routes (Query, Detect, Poll) are answered by
``page.route`` with fragments rendered in-process from the real templates.

Requires the ``ui`` marker. Uses ``logged_in_data_page`` (camera 1, project 1
with frames, and a second user ``viewer1``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.e2e.conftest import SecurityEvents
from timelapse_manager.web.dependencies import templates

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_CAMERA_ID = 1


def _render(name: str, **context: object) -> str:
    return templates.get_template(name).render(**context)


def _html(route: Any, body: str) -> None:
    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


def _candidate(protocol: str, snap: str, stream: str) -> dict[str, object]:
    return {
        "protocol": protocol,
        "ok": True,
        "snapshot_uri": snap,
        "stream_uri": stream,
        "confidence": "high",
        "detail": "",
    }


_CANDIDATES = [
    _candidate("vapix", "http://cam/snap.jpg", "rtsp://cam/vapix"),
    _candidate("onvif", "http://cam/onvif.jpg", "rtsp://cam/onvif"),
]


def _query_fragment() -> str:
    return _render(
        "_partials/camera_query_results.html",
        error=None,
        candidates=_CANDIDATES,
        recommended_primary="vapix",
        ok_count=2,
        discovered_hostname="porch-cam",
        fetched_lat=34.25,
        fetched_lon=-83.5,
        error_protocol=None,
        error_hostname=None,
        error_geo=None,
        geo_required_role=None,
        auth_rejected=False,
    )


@pytest.fixture()
def watched(
    logged_in_data_page: tuple[object, str],
    security_events: Callable[[object], SecurityEvents],
) -> Any:
    page, base = logged_in_data_page  # type: ignore[misc]
    events = security_events(page)
    events.clear()
    return page, base, events


def _assert_quiet(events: SecurityEvents) -> None:
    assert events.csp_violations == []
    assert events.page_errors == []


def _open_add_camera(page: Any, base: str) -> None:
    page.goto(f"{base}/cameras", wait_until="networkidle")
    page.locator("[hx-get='/cameras/add-form']").first.click()
    page.locator("#camera-form-card").wait_for(timeout=_TIMEOUT)


class TestCameraForm:
    def test_manual_hostname_inherit_and_cancel(self, watched: Any) -> None:
        page, base, events = watched
        _open_add_camera(page, base)

        # Typing a hostname marks its source manual again.
        source = page.locator("[name=device_hostname_source]")
        source.evaluate("el => { el.value = 'camera'; }")
        page.fill("[name=device_hostname]", "typed")
        assert source.input_value() == "manual"

        # No default credentials are configured here, so there is no checkbox
        # and the credential inputs are shown (#318; the toggle itself is in
        # test_camera_form_defaults.py).
        assert page.locator("[name=credentials_inherit_default]").count() == 0
        assert page.locator("[id$='-username']").is_visible()
        assert page.locator("[id$='-username']").is_enabled()

        # Cancel removes the card.
        page.locator("#camera-form-card button", has_text="Cancel").click()
        assert page.locator("#camera-form-card").count() == 0
        _assert_quiet(events)

    def test_basic_over_http_follows_the_address_scheme(self, watched: Any) -> None:
        page, base, events = watched
        _open_add_camera(page, base)
        group = page.locator("[id$='-allow-basic-group']")
        checkbox = page.locator("[name=allow_basic_over_http]")

        page.fill("[name=address]", "https://cam.example")
        assert group.is_hidden()
        page.fill("[name=address]", "http://cam.example")
        assert group.is_visible()
        page.fill("[name=address]", "cam.example")
        assert group.is_visible()

        # Hidden or shown, the field keeps its name and submits when ticked.
        checkbox.check()
        page.fill("[name=address]", "https://cam.example")
        assert group.is_hidden()
        assert page.evaluate(
            "() => new FormData(document.querySelector('#camera-form-card form'))"
            ".get('allow_basic_over_http')"
        )
        _assert_quiet(events)

    def test_query_results_fill_the_form(self, watched: Any) -> None:
        page, base, events = watched
        _open_add_camera(page, base)
        page.route("**/cameras/query", lambda route: _html(route, _query_fragment()))
        page.fill("[name=address]", "http://cam.example")
        page.locator("button", has_text="Query camera").click()
        radios = page.locator("#camera-query-results input[name=detected_protocol]")
        radios.first.wait_for(timeout=_TIMEOUT)

        # The recommended protocol is applied as the fragment loads (htmx fires
        # its load event after the short settle delay).
        page.wait_for_function(
            "() => document.querySelector('[name=protocol]').value === 'vapix'",
            timeout=_TIMEOUT,
        )
        assert page.locator("[name=stream_uri]").input_value() == "rtsp://cam/vapix"

        # Choosing another detected protocol refills the fields.
        page.locator("#camera-query-results input[value=onvif]").check()
        assert page.locator("[name=protocol]").input_value() == "onvif"
        assert (
            page.locator("[name=snapshot_uri]").input_value() == "http://cam/onvif.jpg"
        )

        # Accept all applies protocol, hostname (and Name) and location.
        page.locator("button", has_text="Accept all").click()
        assert page.locator("[name=device_hostname]").input_value() == "porch-cam"
        assert page.locator("[name=device_hostname_source]").input_value() == "camera"
        assert page.locator("[name=name]").input_value() == "porch-cam"
        assert page.locator("[name=latitude]").input_value() == "34.25"
        assert page.locator("[name=longitude]").input_value() == "-83.5"
        assert page.locator("[name=geo_source]").input_value() == "camera"

        # The single-field buttons work on their own.
        page.fill("[name=device_hostname]", "")
        page.locator("button", has_text="Use discovered hostname").click()
        assert page.locator("[name=device_hostname]").input_value() == "porch-cam"
        page.fill("[name=latitude]", "")
        page.locator("button", has_text="Use these coordinates").click()
        assert page.locator("[name=latitude]").input_value() == "34.25"
        _assert_quiet(events)

    def test_geo_use_values_fills_the_coordinates(self, watched: Any) -> None:
        page, base, events = watched
        page.goto(f"{base}/cameras", wait_until="networkidle")
        row = page.locator(f"#camera-row-{_CAMERA_ID}")
        row.locator(".row-actions-trigger").click()
        row.locator("button", has_text="Edit").click()
        page.locator(f"#cam-{_CAMERA_ID}-advanced > summary").click()
        fragment = _render(
            "_partials/camera_geo_poll_result.html",
            camera_id=_CAMERA_ID,
            error=None,
            required_role=None,
            fetched_lat=34.1,
            fetched_lon=-83.9,
            admin_read=False,
        )
        page.route(
            f"**/cameras/{_CAMERA_ID}/geolocation", lambda route: _html(route, fragment)
        )
        page.locator("button", has_text="Poll camera").click()
        page.locator("button", has_text="Use these values").click()
        assert page.locator("[name=latitude]").input_value() == "34.1"
        assert page.locator("[name=longitude]").input_value() == "-83.9"
        assert page.locator("[name=geo_source]").input_value() == "camera"
        assert page.locator("button", has_text="Use these values").count() == 0
        _assert_quiet(events)


class TestUsers:
    def test_password_mismatch_blocks_submit(self, watched: Any) -> None:
        page, base, events = watched
        page.goto(f"{base}/users/new", wait_until="networkidle")
        page.fill("#new_username", "mismatch")
        page.fill("#new_password", "correct horse battery")
        page.fill("#new_password_confirm", "different")
        page.locator("button", has_text="Create User").click()
        page.locator("#new-user-pw-error").wait_for(state="visible", timeout=_TIMEOUT)
        assert page.url.endswith("/users/new")
        assert page.evaluate("document.activeElement.id") == "new_password_confirm"
        _assert_quiet(events)

    def test_delete_asks_for_confirmation(self, watched: Any) -> None:
        page, base, events = watched
        page.goto(f"{base}/users", wait_until="networkidle")
        messages: list[str] = []

        def decide(accept: bool) -> Callable[[Any], None]:
            def handler(dialog: Any) -> None:
                messages.append(dialog.message)
                dialog.accept() if accept else dialog.dismiss()

            return handler

        row = page.locator("tr", has_text="viewer1")
        row.locator(".row-actions-trigger").hover()
        page.once("dialog", decide(False))
        row.get_by_role("menuitem", name="Delete").click()
        assert messages and "Permanently remove this user" in messages[0]
        assert page.locator("tr", has_text="viewer1").count() == 1

        row.locator(".row-actions-trigger").hover()
        page.once("dialog", decide(True))
        row.get_by_role("menuitem", name="Delete").click()
        page.wait_for_load_state("networkidle")
        assert page.locator("tr", has_text="viewer1").count() == 0
        _assert_quiet(events)

    def test_static_form_cancel_and_toast_dismiss(self, watched: Any) -> None:
        page, base, events = watched
        page.goto(f"{base}/users", wait_until="networkidle")
        page.evaluate(
            "() => { document.getElementById('add-user-form-static')"
            ".style.display = 'block'; }"
        )
        page.locator("#add-user-form-static button", has_text="Cancel").click()
        assert not page.locator("#add-user-form-static").is_visible()

        toast = _render("_partials/toast.html", msg={"type": "info", "message": "hi"})
        page.evaluate(
            "(html) => document.body.insertAdjacentHTML('beforeend', html)", toast
        )
        page.locator(".toast .toast-close").last.click()
        assert page.locator(".toast", has_text="hi").count() == 0
        _assert_quiet(events)


class TestFrames:
    def test_project_filter_submits_on_change(self, watched: Any) -> None:
        page, base, events = watched
        page.goto(f"{base}/frames?project_id=1", wait_until="domcontentloaded")
        with page.expect_navigation(timeout=_TIMEOUT):
            page.select_option("#frame-project-select", value="")
        assert "project_id=" in page.url
        _assert_quiet(events)

    def test_import_form_cancel_collapses_the_disclosure(self, watched: Any) -> None:
        page, base, events = watched
        page.goto(f"{base}/frames?project_id=1", wait_until="domcontentloaded")
        details = page.locator("details.frames-import-disclosure")
        # No marker triangle: the CSS moved out of an inline <style>.
        assert (
            page.evaluate(
                "() => getComputedStyle(document.querySelector("
                "'.frames-import-disclosure > summary')).listStyleType"
            )
            == "none"
        )
        details.locator("summary").click()
        assert details.evaluate("el => el.open")
        details.locator("button", has_text="Cancel").click()
        assert not details.evaluate("el => el.open")
        _assert_quiet(events)

    def test_frame_card_opens_drawer_but_controls_do_not(self, watched: Any) -> None:
        """#315: a click on the card text opens the drawer; the select checkbox
        and Remove keep their own action; there is no Info button."""
        page, base, events = watched
        page.goto(f"{base}/frames?project_id=1", wait_until="domcontentloaded")
        tile = page.locator(".frame-tile").first
        tile.wait_for(timeout=_TIMEOUT)
        assert page.locator(".frame-tile a", has_text="Info").count() == 0
        opened = (
            "() => document.querySelector('#drawer-main')"
            ".getAttribute('aria-hidden') === 'false'"
        )
        # Checkbox: toggles selection, the drawer stays shut.
        tile.locator(".frame-tile-select").click()
        assert tile.locator(".frame-select-cb").is_checked()
        assert not page.evaluate(opened)
        tile.locator(".frame-tile-select").click()
        # Remove: soft-deletes the tile in place, the drawer stays shut.
        tile.locator("button", has_text="Remove").click()
        page.locator(".frame-tile.soft-deleted").first.wait_for(timeout=_TIMEOUT)
        assert not page.evaluate(opened)
        # The timestamp line is part of the card: clicking it opens the drawer.
        other = page.locator(".frame-tile:not(.soft-deleted)").first
        # force: Playwright refuses a click the overlay link intercepts, which is
        # exactly the design; the real mouse click still lands on that link.
        other.locator(".frame-tile-meta > div").last.click(force=True)
        page.wait_for_function(opened, timeout=_TIMEOUT)
        assert page.locator("#drawer-title").inner_text().startswith("Frame #")
        _assert_quiet(events)

    def test_timestamp_row_edit_and_cancel(self, watched: Any) -> None:
        page, base, events = watched
        page.goto(f"{base}/frames?project_id=1", wait_until="domcontentloaded")
        page.locator(
            'a[hx-get^="/projects/1/frames/"][hx-get$="/drawer"]'
        ).first.click()
        edit = page.locator("#frame-timestamp-row [data-ts-edit]")
        edit.wait_for(timeout=_TIMEOUT)
        form = page.locator("#frame-timestamp-row [data-ts-form]")
        assert form.is_hidden()
        edit.click()
        assert form.is_visible()
        assert edit.is_hidden()
        form.locator("button", has_text="Cancel").click()
        assert form.is_hidden()
        assert edit.is_visible()
        _assert_quiet(events)
