"""The camera form's Poll camera and admin-read buttons in a real page (#70 WP-70-D).

Drives Chromium against a live server seeded with VAPIX camera 1. The camera
address is not a real device, so the camera-facing routes are answered by
``page.route`` with fragments rendered in-process from the real template:

* a double click on **Read location** sends one POST, and the button is
  disabled while it runs;
* a failed admin read (a 500, so htmx does not swap) still empties the typed
  administrator password, and an error toast shows;
* a double click on **Poll camera** sends one GET.

Requests are held by storing the ``Route`` and answering it later, never by a
sleep inside the handler (that would serialise the clicks; see
``test_double_submit.py``).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from timelapse_manager.web.dependencies import templates

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_HOLD_MS = 400
_CAMERA_ID = 1
_AID = f"cam-{_CAMERA_ID}-geo-admin"
_POLL_URL = f"**/cameras/{_CAMERA_ID}/geolocation"
_ADMIN_URL = f"**/cameras/{_CAMERA_ID}/geolocation/admin-read"
_ADMIN_PASS = "typed-admin-pass-7f1e"


def _fragment(**context: object) -> str:
    base: dict[str, object] = {
        "camera_id": _CAMERA_ID,
        "error": None,
        "required_role": None,
        "fetched_lat": None,
        "fetched_lon": None,
        "admin_read": False,
    }
    base.update(context)
    return templates.get_template("_partials/camera_geo_poll_result.html").render(
        **base
    )


_ACCESS_DENIED = _fragment(error="access_denied", required_role="Administrator")
_LOCATION = _fragment(fetched_lat=34.1, fetched_lon=-83.9, admin_read=True)


def _fulfil_html(route: Any, body: str, status: int = 200) -> None:
    route.fulfill(status=status, content_type="text/html; charset=utf-8", body=body)


def _open_edit_form(page: Any, base: str) -> None:
    page.goto(f"{base}/cameras", wait_until="networkidle")
    row = page.locator(f"#camera-row-{_CAMERA_ID}")
    row.locator(".row-actions-trigger").click()
    row.locator("button", has_text="Edit").click()
    page.locator(f"#cam-{_CAMERA_ID}-advanced > summary").click()
    page.locator("button", has_text="Poll camera").wait_for(timeout=_TIMEOUT)


def _show_admin_form(page: Any) -> None:
    """Poll camera answered with ``access_denied``: the admin-read form appears."""
    page.route(_POLL_URL, lambda route: _fulfil_html(route, _ACCESS_DENIED))
    page.locator("button", has_text="Poll camera").click()
    page.locator(f"#{_AID}-password").wait_for(state="attached", timeout=_TIMEOUT)
    page.unroute(_POLL_URL)
    page.locator("summary", has_text="Read with admin account").click()
    page.fill(f"#{_AID}-username", "root")
    page.fill(f"#{_AID}-password", _ADMIN_PASS)


def _requests(page: Any, method: str, suffix: str) -> list[Any]:
    seen: list[Any] = []

    def on_request(request: Any) -> None:
        if request.method == method and request.url.split("?")[0].endswith(suffix):
            seen.append(request)

    page.on("request", on_request)
    return seen


class TestAdminReadButton:
    def test_double_click_sends_one_read(
        self, logged_in_data_page: tuple[Any, str]
    ) -> None:
        page, base = logged_in_data_page
        _open_edit_form(page, base)
        _show_admin_form(page)

        posts = _requests(page, "POST", "/geolocation/admin-read")
        held: list[Any] = []
        page.route(_ADMIN_URL, lambda route: held.append(route))
        button = page.locator("button", has_text="Read location")
        button.dblclick()
        page.wait_for_timeout(_HOLD_MS)
        assert len(held) == 1, "the second click of the double click sent a request"
        assert button.is_disabled()

        for route in held:
            _fulfil_html(route, _LOCATION)
        page.unroute(_ADMIN_URL)
        page.locator("text=Camera reports:").wait_for(timeout=_TIMEOUT)
        page.wait_for_timeout(_HOLD_MS)
        assert len(posts) == 1
        # Only the two admin fields travel; the password never reaches a URL.
        assert _ADMIN_PASS not in posts[0].url

    def test_failed_read_clears_the_password_and_shows_a_toast(
        self, logged_in_data_page: tuple[Any, str]
    ) -> None:
        page, base = logged_in_data_page
        _open_edit_form(page, base)
        _show_admin_form(page)

        page.route(
            _ADMIN_URL,
            lambda route: route.fulfill(
                status=500,
                headers={"X-Request-ID": "rid-admin-500"},
                content_type="text/plain",
                body="Internal Server Error",
            ),
        )
        page.locator("button", has_text="Read location").click()
        page.locator("#toast-region .toast[role=alert]").first.wait_for(
            timeout=_TIMEOUT
        )
        # No swap happened (a 500), yet the typed password is gone.
        password = page.locator(f"#{_AID}-password")
        assert password.count() == 1
        assert password.input_value() == ""
        assert page.locator(f"#{_AID}-username").input_value() == "root"
        assert page.locator("button", has_text="Read location").is_enabled()


class TestPollCameraButton:
    def test_double_click_sends_one_poll(
        self, logged_in_data_page: tuple[Any, str]
    ) -> None:
        page, base = logged_in_data_page
        _open_edit_form(page, base)

        gets = _requests(page, "GET", f"/cameras/{_CAMERA_ID}/geolocation")
        held: list[Any] = []
        page.route(_POLL_URL, lambda route: held.append(route))
        button = page.locator("button", has_text="Poll camera")
        button.dblclick()
        page.wait_for_timeout(_HOLD_MS)
        assert len(held) == 1
        assert button.is_disabled()

        for route in held:
            _fulfil_html(route, _ACCESS_DENIED)
        page.unroute(_POLL_URL)
        page.locator(f"#{_AID}-password").wait_for(state="attached", timeout=_TIMEOUT)
        page.wait_for_timeout(_HOLD_MS)
        assert len(gets) == 1
        assert button.is_enabled()


class TestGenericHtmxGuard:
    def test_test_connection_is_disabled_while_it_runs(
        self, logged_in_data_page: tuple[Any, str]
    ) -> None:
        """forms.js disables a mutating htmx form's submit button until it ends.

        "Test connection" carries no ``hx-disabled-elt`` of its own, so this is
        the generic guard (and its 45 s timeout from #98).
        """
        page, base = logged_in_data_page
        page.goto(f"{base}/cameras", wait_until="networkidle")
        row = page.locator(f"#camera-row-{_CAMERA_ID}")
        form = row.locator(f'form[hx-post="/cameras/{_CAMERA_ID}/validate"]')
        assert form.get_attribute("hx-request") == '{"timeout":45000}'

        posts = _requests(page, "POST", f"/cameras/{_CAMERA_ID}/validate")
        held: list[Any] = []
        page.route(f"**/cameras/{_CAMERA_ID}/validate", lambda r: held.append(r))
        row.locator(".row-actions-trigger").click()
        button = row.locator("button", has_text="Test connection")
        button.dblclick()
        page.wait_for_timeout(_HOLD_MS)
        assert len(held) == 1, "the second click sent a second request"
        assert button.is_disabled()

        for route in held:
            _fulfil_html(route, "<span>OK: reachable</span>")
        page.unroute(f"**/cameras/{_CAMERA_ID}/validate")
        page.locator(f"#camera-test-result-{_CAMERA_ID}", has_text="OK").wait_for(
            timeout=_TIMEOUT
        )
        assert len(posts) == 1
        page.wait_for_function(
            f"() => !document.querySelector("
            f"'#camera-row-{_CAMERA_ID} form[hx-post$=\"/validate\"] button').disabled",
            timeout=_TIMEOUT,
        )
