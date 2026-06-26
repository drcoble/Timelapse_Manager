"""E2E tests for the drag-select escalation banner (render-only, no mutation).

A drag-select on the timeline produces a range descriptor; the selection spine
reveals the escalation banner and shows the bar's "≈N" estimate. A real pixel
drag is brittle, so these drive ``window.frameSelection.setDescriptor`` directly
(the same entry point the scrubber calls after its count round-trip) and assert
the resulting UI. No bulk button is clicked -- the descriptor->bulk mutation is
covered by the web tests.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (operator, so the escalation banner renders).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000

# Drive the spine exactly as scrubber.js does after counting a dragged span.
_SET_DESCRIPTOR_JS = """
() => {
  window.frameSelection.setDescriptor(
    {
      scope: "in_range",
      project_id: 1,
      time_range: { from: "2026-03-01T00:00:00Z", to: "2026-03-31T23:59:59Z" },
      filters: { include_deleted: false },
      deselected_ids: [],
    },
    4200,
    "2026-03-01 00:00 – 2026-03-31 23:59"
  );
}
"""


class TestDragSelectEscalationBanner:
    def test_banner_appears_with_buttons(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        banner = page.locator("#frames-escalation-banner")  # type: ignore[union-attr]
        # Present but hidden until a descriptor is set.
        banner.wait_for(state="attached", timeout=_TIMEOUT)
        assert banner.is_hidden()

        page.wait_for_function(  # type: ignore[union-attr]
            "() => window.frameSelection && window.frameSelection.setDescriptor",
            timeout=_TIMEOUT,
        )
        page.evaluate(_SET_DESCRIPTOR_JS)  # type: ignore[union-attr]

        banner.wait_for(state="visible", timeout=_TIMEOUT)
        # Both escalation buttons are present.
        assert (
            page.locator(  # type: ignore[union-attr]
                '#frames-escalation-banner [data-escalation-scope="in_range"]'
            ).count()
            == 1
        )
        assert (
            page.locator(  # type: ignore[union-attr]
                '#frames-escalation-banner [data-escalation-scope="in_project"]'
            ).count()
            == 1
        )
        # The banner count shows the approximate marker.
        count_text = page.locator(  # type: ignore[union-attr]
            "#frames-escalation-banner [data-escalation-count]"
        ).text_content()
        assert "≈4200" in count_text

    def test_action_bar_shows_approx_label(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        page.wait_for_function(  # type: ignore[union-attr]
            "() => window.frameSelection && window.frameSelection.setDescriptor",
            timeout=_TIMEOUT,
        )
        page.evaluate(_SET_DESCRIPTOR_JS)  # type: ignore[union-attr]

        bar = page.locator("#frames-action-bar")  # type: ignore[union-attr]
        bar.wait_for(state="visible", timeout=_TIMEOUT)
        # The selection-bar count uses the "≈N · <label>" descriptor format.
        page.wait_for_function(  # type: ignore[union-attr]
            "() => /≈4200/.test(document.querySelector("
            "'#frames-action-bar .selection-bar-count').textContent)",
            timeout=_TIMEOUT,
        )
        text = page.locator(  # type: ignore[union-attr]
            "#frames-action-bar .selection-bar-count"
        ).text_content()
        assert "·" in text

    def test_dismiss_hides_banner(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        page.wait_for_function(  # type: ignore[union-attr]
            "() => window.frameSelection && window.frameSelection.setDescriptor",
            timeout=_TIMEOUT,
        )
        page.evaluate(_SET_DESCRIPTOR_JS)  # type: ignore[union-attr]

        banner = page.locator("#frames-escalation-banner")  # type: ignore[union-attr]
        banner.wait_for(state="visible", timeout=_TIMEOUT)
        # The descriptor is exposed while in descriptor mode.
        assert page.evaluate(  # type: ignore[union-attr]
            "() => window.frameSelection.descriptor() !== null"
        )
        page.locator(  # type: ignore[union-attr]
            "#frames-escalation-banner [data-escalation-dismiss]"
        ).click()
        banner.wait_for(state="hidden", timeout=_TIMEOUT)
        # Dismiss tears the descriptor down: back to the empty state.
        page.wait_for_function(  # type: ignore[union-attr]
            "() => window.frameSelection.descriptor() === null",
            timeout=_TIMEOUT,
        )
