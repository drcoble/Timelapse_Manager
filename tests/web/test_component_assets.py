"""Component-library guardrails: new partials are imported, served, and the
signature selectors / JS modules are present.

These components are consumed by later screen phases; here we assert they ship
and don't regress. Interactive behaviour (drawer focus-trap, chip toggle) is
exercised in the phases that wire them into a page.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# partial filename -> a selector that must appear in it
SIGNATURES = {
    "chips.css": ".chip",
    "drawer.css": ".drawer",
    "modal.css": ".discover-modal",
    "inline-confirm.css": ".inline-confirm",
    "row-actions.css": ".row-actions-popover",
    "preflight-banner.css": ".preflight-banner",
    "scroll.css": ".new-frames-pill",
    "render-trigger.css": ".render-trigger-panel",
}

JS_MODULES = ["drawer.js", "chips.js"]


def test_app_css_imports_new_components(anon_client: TestClient) -> None:
    css = anon_client.get("/static/css/app.css").text
    for name in SIGNATURES:
        assert f"components/{name}" in css, f"app.css missing @import of {name}"


@pytest.mark.parametrize(("fname", "sig"), list(SIGNATURES.items()))
def test_component_partial_serves(
    anon_client: TestClient, fname: str, sig: str
) -> None:
    resp = anon_client.get(f"/static/css/components/{fname}")
    assert resp.status_code == 200
    assert sig in resp.text


@pytest.mark.parametrize("module", JS_MODULES)
def test_js_module_serves(anon_client: TestClient, module: str) -> None:
    resp = anon_client.get(f"/static/js/{module}")
    assert resp.status_code == 200
    assert len(resp.text) > 100


def test_provenance_badges_defined(anon_client: TestClient) -> None:
    css = anon_client.get("/static/css/components/badges.css").text
    for cls in (".badge-device", ".badge-manual", ".badge-env"):
        assert cls in css


def test_shell_loads_component_scripts(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    assert "/static/js/drawer.js" in html
    assert "/static/js/chips.js" in html
