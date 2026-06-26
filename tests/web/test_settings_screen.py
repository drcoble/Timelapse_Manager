"""Web tests for the Settings tab-bar + Account preferences segmented control.

Settings adopts a client-side Ph-3 tab-bar (System / Notifications / LDAP /
Credentials): all four panels render in place and toggle via tabs.js. The
per-user theme control on the account page becomes a 3-way segmented control.
One role-client per test.
"""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from timelapse_manager.runtime import get_context, set_context


def test_settings_renders_four_tab_panels(admin_client: TestClient) -> None:
    """All four settings panels render under one tablist for client-side tabs."""
    html = admin_client.get("/settings").text
    assert '<div class="tabs" role="tablist"' in html
    panels = ("#tab-system", "#tab-notifications", "#tab-ldap", "#tab-credentials")
    for target in panels:
        assert f'data-tab-target="{target}"' in html
        assert f'id="{target.lstrip("#")}"' in html
    # System is the default-visible panel; the others start hidden.
    assert 'id="tab-notifications" class="tab-panel" role="tabpanel"' in html
    assert "hidden" in html


def test_settings_panels_keep_their_own_forms(admin_client: TestClient) -> None:
    """LDAP and Credentials retain their own forms/endpoints inside the panels."""
    html = admin_client.get("/settings").text
    assert 'id="settings-form"' in html  # System
    assert 'id="notifications-form"' in html  # Notifications (no-op /settings audit)
    assert 'id="ldap-settings-form"' in html  # LDAP partial intact
    assert 'id="camera-credentials-form"' in html  # Credentials partial intact


def test_settings_system_panel_has_readonly_banner(admin_client: TestClient) -> None:
    """System fields are env-resolved → an explicit read-only note is shown."""
    html = admin_client.get("/settings").text
    assert "resolved from configuration and environment at startup" in html


def _with_env_overrides(paths: frozenset[str]) -> None:
    """Install a context carrying the given env-provenance leaf paths.

    The web app's lifespan builds the context with empty provenance; the System
    panel reads provenance from the context, so a test stubs it here rather than
    relying on a process-level env var (settings were already resolved at boot).
    """
    ctx = get_context()
    set_context(dataclasses.replace(ctx, env_overrides=paths))


def test_settings_system_shows_env_badge_on_env_sourced_field(
    admin_client: TestClient,
) -> None:
    """An env-sourced System field carries a per-field env chip beside its label."""
    _with_env_overrides(frozenset({"server.http_port"}))
    html = admin_client.get("/settings").text
    # The HTTP Port field's label carries the chip; an editable field that the
    # environment did not set (HTTPS Port) carries none.
    assert "HTTP Port" in html
    assert '<span class="badge badge-env"' in html
    https_label = html.split("HTTPS Port", 1)[1].split("</label>", 1)[0]
    assert "badge-env" not in https_label


def test_settings_system_no_provenance_degrades_to_banner(
    admin_client: TestClient,
) -> None:
    """With no provenance signal, no chips render and the banner remains."""
    _with_env_overrides(frozenset())
    html = admin_client.get("/settings").text
    assert '<span class="badge badge-env"' not in html
    # The read-only banner is the fallback and is never removed.
    assert "resolved from configuration and environment at startup" in html


def test_settings_is_admin_only(viewer_client: TestClient) -> None:
    """A viewer cannot reach the admin Settings page."""
    resp = viewer_client.get("/settings", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


def test_account_preferences_uses_segmented_theme_control(
    admin_client: TestClient,
) -> None:
    """Theme is a 3-way segmented radiogroup (System / Light / Dark)."""
    html = admin_client.get("/account/preferences").text
    assert 'class="segmented" role="radiogroup"' in html
    assert 'id="theme-segment"' in html
    for value in ("system", "light", "dark"):
        assert f'type="radio" name="theme" value="{value}"' in html
    # Default theme is reflected as the checked segment.
    assert "checked" in html
    # Timezone input + auto-detect affordance remain.
    assert 'id="timezone-input"' in html
    assert 'id="timezone-detect-btn"' in html
