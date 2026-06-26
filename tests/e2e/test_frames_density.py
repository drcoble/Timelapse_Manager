"""E2E for the frame-grid display-density toggle.

The toggle (Comfortable / Compact / Filmstrip) sets data-density on the grid
and remembers the choice in localStorage with no server round-trip, so it
survives a reload. Deterministic (purely client-side).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (project id 1, single-project, so the controls show).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000


class TestDensityToggle:
    def test_density_changes_and_persists(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        grid = page.locator("#frame-grid")  # type: ignore[union-attr]
        grid.wait_for(timeout=_TIMEOUT)
        # Defaults to comfortable.
        assert grid.get_attribute("data-density") == "comfortable"

        # Switch to Compact. The segmented radio is visually hidden, so click
        # its label (which checks the radio) rather than the input directly.
        page.locator(  # type: ignore[union-attr]
            '.segmented-option:has(input[value="compact"])'
        ).click()
        page.wait_for_function(  # type: ignore[union-attr]
            "() => document.getElementById('frame-grid')"
            ".getAttribute('data-density') === 'compact'",
            timeout=_TIMEOUT,
        )

        # The choice survives a reload (localStorage), and the radio reflects it.
        page.reload(wait_until="domcontentloaded")  # type: ignore[union-attr]
        page.wait_for_function(  # type: ignore[union-attr]
            "() => document.getElementById('frame-grid')"
            ".getAttribute('data-density') === 'compact'",
            timeout=_TIMEOUT,
        )
        assert page.locator(  # type: ignore[union-attr]
            'input[name="frame-density"][value="compact"]'
        ).is_checked()

    def test_filmstrip_switches_grid_to_row(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        page.locator(  # type: ignore[union-attr]
            '.segmented-option:has(input[value="filmstrip"])'
        ).click()
        # Filmstrip lays the grid out as a horizontal flex row.
        page.wait_for_function(  # type: ignore[union-attr]
            "() => getComputedStyle(document.getElementById('frame-grid'))"
            ".display === 'flex'",
            timeout=_TIMEOUT,
        )
