"""A failed HTMX request is visible (#70 F-R1).

Before ``htmx-errors.js`` a 4xx/5xx, a dropped connection or a timeout left the
page unchanged and silent. These drive the real page in Chromium:

* deleting a camera a project still uses -> an alert toast quoting the server's
  409 message, and the row stays;
* a bulk frame action that gets a 500 -> a toast with the request id;
* a dropped connection -> the shared "Connection lost" banner, cleared by the
  next request that gets any response;
* a request that outlives the htmx timeout -> a timeout toast;
* a 413 on a frame import -> "Upload too large", and the upload itself carries
  no client timeout (``data-htmx-timeout="none"``);
* a CSRF failure -> the "session changed" toast with a working Reload action.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_ALERT_TOAST = "#toast-region .toast[role=alert]"

# A 1x1 JPEG, enough for the file input to accept a selection.
_TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909"
    "080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30"
    "313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501"
    "010101010100000000000000000102030405060708090a0bffc400b5100002010303020403"
    "050504040000017d01020300041105122131410613516107227114328191a1082342b1c115"
    "52d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a53"
    "5455565758595a636465666768696a737475767778797a838485868788898a929394959697"
    "98999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8"
    "d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)


def _alert_toast(page: object, text: str) -> object:
    toast = page.locator(_ALERT_TOAST, has_text=text)  # type: ignore[attr-defined]
    toast.first.wait_for(timeout=_TIMEOUT)
    return toast.first


def _first_remove_button(page: object) -> object:
    button = page.locator(  # type: ignore[attr-defined]
        'button[hx-post*="/soft-delete"]'
    ).first
    button.wait_for(state="attached", timeout=_TIMEOUT)
    return button


class TestErrorFeedback:
    def test_camera_in_use_delete_shows_server_message(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/cameras", wait_until="networkidle")  # type: ignore[attr-defined]
        page.on("dialog", lambda dialog: dialog.accept())  # type: ignore[attr-defined]

        row = page.locator("#camera-row-1")  # type: ignore[attr-defined]
        row.locator(".row-actions-trigger").click()
        row.locator("button.danger", has_text="Delete").click()

        toast = _alert_toast(page, "used by 1 project(s)")
        assert "move or delete them first" in toast.text_content()  # type: ignore[attr-defined]
        assert page.locator("#camera-row-1").count() == 1  # type: ignore[attr-defined]

    def test_bulk_action_server_error_shows_request_id(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.route(  # type: ignore[attr-defined]
            "**/frames/bulk",
            lambda route: route.fulfill(
                status=500,
                headers={"X-Request-ID": "rid-bulk-500"},
                content_type="text/plain",
                body="Internal Server Error",
            ),
        )
        page.goto(f"{base}/frames?project_id=1", wait_until="networkidle")  # type: ignore[attr-defined]
        labels = page.locator(".frame-tile-select")  # type: ignore[attr-defined]
        labels.first.wait_for(timeout=_TIMEOUT)
        labels.nth(0).click()
        page.locator('#frames-action-bar [data-bulk-action="exclude"]').click()  # type: ignore[attr-defined]

        _alert_toast(page, "Server error (request id rid-bulk-500)")

    def test_dropped_connection_shows_banner_until_next_response(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1", wait_until="networkidle")  # type: ignore[attr-defined]
        banner = page.locator("#connection-banner")  # type: ignore[attr-defined]
        assert banner.is_hidden()

        page.route("**/soft-delete*", lambda route: route.abort())  # type: ignore[attr-defined]
        _first_remove_button(page).click(force=True)  # type: ignore[attr-defined]
        banner.wait_for(state="visible", timeout=_TIMEOUT)
        assert "Connection lost" in banner.text_content()
        # A dropped connection is the banner's job, not a per-request toast.
        assert page.locator(_ALERT_TOAST).count() == 0  # type: ignore[attr-defined]

        page.unroute("**/soft-delete*")  # type: ignore[attr-defined]
        _first_remove_button(page).click(force=True)  # type: ignore[attr-defined]
        banner.wait_for(state="hidden", timeout=_TIMEOUT)

    def test_request_past_the_timeout_shows_toast(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1", wait_until="networkidle")  # type: ignore[attr-defined]
        # The XHR timeout runs on the browser's own timer, which a fake clock
        # does not drive, so shorten the configured timeout instead of waiting
        # out the real 30 s (whose value test_csp_clean asserts).
        page.evaluate("() => { htmx.config.timeout = 300; }")  # type: ignore[attr-defined]
        page.route("**/soft-delete*", lambda route: None)  # type: ignore[attr-defined]

        _first_remove_button(page).click(force=True)  # type: ignore[attr-defined]

        _alert_toast(page, "took too long")

    def test_import_413_says_upload_too_large_and_has_no_timeout(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.route(  # type: ignore[attr-defined]
            "**/frames/import",
            lambda route: route.fulfill(
                status=413, content_type="text/plain", body="Request body too large."
            ),
        )
        page.goto(f"{base}/frames?project_id=1", wait_until="networkidle")  # type: ignore[attr-defined]
        page.evaluate(  # type: ignore[attr-defined]
            "() => document.addEventListener('htmx:beforeSend', (e) => {"
            " window.__importXhrTimeout = e.detail.xhr.timeout; })"
        )
        page.locator("summary", has_text="Import frames").click()  # type: ignore[attr-defined]
        page.locator("#frames-import-files").set_input_files(  # type: ignore[attr-defined]
            {"name": "one.jpg", "mimeType": "image/jpeg", "buffer": _TINY_JPEG}
        )
        page.locator("[data-import-submit]").click()  # type: ignore[attr-defined]

        _alert_toast(page, "Upload too large")
        assert page.evaluate("() => window.__importXhrTimeout") == 0  # type: ignore[attr-defined]

    def test_csrf_failure_offers_reload(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1", wait_until="networkidle")  # type: ignore[attr-defined]
        # Simulate a token rotated by another tab: csrf.js reads the meta tag
        # per request (WP-70-G), so a stale value there is what the next
        # request sends.
        page.evaluate(  # type: ignore[attr-defined]
            "() => document.querySelector('meta[name=\"csrf-token\"]')"
            ".setAttribute('content', 'stale-token')"
        )
        _first_remove_button(page).click(force=True)  # type: ignore[attr-defined]

        toast = _alert_toast(page, "session changed")
        # One toast for the failure, not a second one from htmx:responseError.
        assert page.locator(_ALERT_TOAST).count() == 1  # type: ignore[attr-defined]
        with page.expect_navigation(timeout=_TIMEOUT):  # type: ignore[attr-defined]
            toast.locator(".toast-action", has_text="Reload").click()  # type: ignore[attr-defined]
        assert page.locator(_ALERT_TOAST).count() == 0  # type: ignore[attr-defined]

    def test_toast_dismiss_button_removes_it(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/about", wait_until="networkidle")  # type: ignore[attr-defined]
        page.evaluate(  # type: ignore[attr-defined]
            "() => { window.tlmToast('<b>plain</b> text');"
            " window.tlmToast('<b>plain</b> text'); }"
        )

        toast = _alert_toast(page, "<b>plain</b> text")
        # Text, never markup; and a repeat refreshes one toast instead of stacking.
        assert page.locator(f"{_ALERT_TOAST} b").count() == 0  # type: ignore[attr-defined]
        assert page.locator(_ALERT_TOAST).count() == 1  # type: ignore[attr-defined]
        toast.locator(".toast-close").click()  # type: ignore[attr-defined]
        assert page.locator(_ALERT_TOAST).count() == 0  # type: ignore[attr-defined]
