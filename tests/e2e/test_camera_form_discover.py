"""E2E for the address-first camera form and the centered Discover modal.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.

The Discover scan is exercised with a malformed range, which the server
rejects before any network probe runs — a deterministic swap into the modal's
results container that never touches the network.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestCameraFormAdvanced:
    def test_advanced_disclosure_toggles(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.click("button:has-text('Add Camera')")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#camera-form-container details", timeout=30000
        )
        details = page.locator("#camera-form-container details")  # type: ignore[union-attr]
        summary = details.locator("summary")
        # The snapshot URI lives inside Advanced; collapsed by default it is not
        # visible, and toggling the summary reveals it.
        snapshot = page.locator(  # type: ignore[union-attr]
            "#camera-form-container input[name='snapshot_uri']"
        )
        assert not snapshot.is_visible()
        summary.click()
        page.wait_for_selector(  # type: ignore[union-attr]
            "#camera-form-container input[name='snapshot_uri']:visible",
            timeout=10000,
        )
        assert snapshot.is_visible()
        summary.click()
        assert not snapshot.is_visible()

    def test_address_field_precedes_name_field(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.click("button:has-text('Add Camera')")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#camera-form-container input[name='address']", timeout=30000
        )
        addr = page.locator(  # type: ignore[union-attr]
            "#camera-form-container input[name='address']"
        ).bounding_box()
        name = page.locator(  # type: ignore[union-attr]
            "#camera-form-container input[name='name']"
        ).bounding_box()
        assert addr is not None and name is not None
        # Address sits above Name visually (address-first golden path).
        assert addr["y"] < name["y"]


class TestDiscoverModal:
    def test_modal_opens_centered_and_visible(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        dialog = page.locator("#discover-modal")  # type: ignore[union-attr]
        # Hidden until opened.
        assert dialog.get_attribute("aria-hidden") == "true"
        page.click("[data-modal-open='#discover-modal']")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#discover-modal[aria-hidden='false']", timeout=10000
        )
        assert dialog.get_attribute("aria-hidden") == "false"
        assert dialog.is_visible()

    def test_scan_swaps_results_into_modal(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.click("[data-modal-open='#discover-modal']")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#discover-modal[aria-hidden='false']", timeout=10000
        )
        # A malformed range is rejected before any network scan runs, yielding a
        # deterministic error fragment in the modal's results container.
        page.fill("#discover-modal input[name='scan_range']", "192.168.1")  # type: ignore[union-attr]
        page.click("#discover-modal button:has-text('Scan Network')")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#scan-results .alert", timeout=15000
        )
        assert page.locator("#scan-results").inner_text()  # type: ignore[union-attr]

    def test_escape_closes_modal(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.click("[data-modal-open='#discover-modal']")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#discover-modal[aria-hidden='false']", timeout=10000
        )
        page.keyboard.press("Escape")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#discover-modal[aria-hidden='true']", timeout=10000
        )
        assert (
            page.locator("#discover-modal").get_attribute(  # type: ignore[union-attr]
                "aria-hidden"
            )
            == "true"
        )

    def test_backdrop_click_closes_modal(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.click("[data-modal-open='#discover-modal']")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#discover-modal[aria-hidden='false']", timeout=10000
        )
        # Click the backdrop at a corner, away from the centered dialog.
        page.click(  # type: ignore[union-attr]
            ".modal-backdrop", position={"x": 5, "y": 5}
        )
        page.wait_for_selector(  # type: ignore[union-attr]
            "#discover-modal[aria-hidden='true']", timeout=10000
        )
        assert (
            page.locator("#discover-modal").get_attribute(  # type: ignore[union-attr]
                "aria-hidden"
            )
            == "true"
        )

    def test_focus_trapped_while_open(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.click("[data-modal-open='#discover-modal']")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#discover-modal[aria-hidden='false']", timeout=10000
        )
        # Tab through several elements; focus must never leave the dialog.
        for _ in range(8):
            page.keyboard.press("Tab")  # type: ignore[union-attr]
            inside = page.evaluate(  # type: ignore[union-attr]
                "() => { var m = document.getElementById('discover-modal');"
                " return m.contains(document.activeElement); }"
            )
            assert inside is True
