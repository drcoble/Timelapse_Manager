"""Deterministic waits for the row-click tests (#343).

A test that proves a click did NOT open a row has nothing to wait for, so a
fixed sleep either races the page or wastes time. These helpers replace it with
two positive signals:

* ``arm_click_probe`` installs a document click listener after ``row-click.js``
  has bound its own (listeners on one target run in registration order). Once the
  probe has seen the click, the helper has already run and any navigation or htmx
  request it caused has been started.
* ``settle`` waits for that probe, one animation frame, and an idle network, so a
  request or navigation a wrong forward started has landed before the caller
  asserts it did not happen.

``wait_ready`` also waits for load and web fonts, so a drag measured from a cell's
bounding box is not thrown off by a late layout shift.
"""

from __future__ import annotations

from typing import Any

_TIMEOUT = 30_000

_PROBE_JS = """() => {
    window.__rowClickSeen = 0;
    document.addEventListener('click', () => { window.__rowClickSeen += 1; });
}"""

_SETTLE_JS = "() => window.__rowClickSeen > 0 && (document.readyState === 'complete')"


def wait_ready(page: Any, row_selector: str) -> None:
    """Wait until the page is fully loaded, fonts are in and the row is visible."""
    page.wait_for_load_state("load", timeout=_TIMEOUT)
    page.evaluate("() => document.fonts.ready.then(() => true)")
    page.locator(row_selector).first.wait_for(state="visible", timeout=_TIMEOUT)


def arm_click_probe(page: Any) -> None:
    """Start counting clicks that reach the document (after row-click.js)."""
    page.evaluate(_PROBE_JS)


def settle(page: Any) -> None:
    """Wait until a click was fully handled and nothing is left in flight."""
    page.wait_for_function(_SETTLE_JS, timeout=_TIMEOUT)
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => r(true)))")
    page.wait_for_load_state("networkidle", timeout=_TIMEOUT)
