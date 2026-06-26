"""Web-layer tests for the Network / SSRF allow-list settings panel (admin-only).

Covers:
- Admin-only gate: non-admin roles get 403 on the SSRF settings endpoint.
- Save round-trip: a submitted subnet persists (canonicalised) and is applied to
  the running policy immediately -- the live-update proof that maps directly to
  the reported defect (a private camera could not be added because the allow-list
  was unreachable from the UI).
- Normalisation: a bare host is stored as a ``/32``; a host-with-prefix collapses
  to its network.
- Config baseline: an environment/config-provided subnet is preserved alongside
  an admin-added one in the effective runtime list.
- Validation: an unparsable entry returns the full settings page (200) with an
  inline error and persists nothing.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import SsrfSettings
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _post_ssrf(
    client: TestClient, subnets_text: str, *, follow_redirects: bool = False
) -> Any:
    csrf = csrf_of(client, "/settings")
    return client.post(
        "/settings/ssrf",
        data={"csrf_token": csrf, "ssrf_allowed_private_subnets": subnets_text},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=follow_redirects,
    )


def _stored_subnets() -> list[str] | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        row = db.get(SsrfSettings, 1)
        return list(row.allowed_private_subnets) if row else None


def _runtime_subnets() -> list[str]:
    return list(get_context().settings.ssrf.allowed_private_subnets)


class TestSsrfSettingsAdminOnly:
    def test_viewer_post_is_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(
            "/settings/ssrf",
            data={"ssrf_allowed_private_subnets": "10.1.16.0/24"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_post_is_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(
            "/settings/ssrf",
            data={"ssrf_allowed_private_subnets": "10.1.16.0/24"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestSsrfSettingsSave:
    def test_save_persists_and_applies_live(self, admin_client: TestClient) -> None:
        """The reported defect: a subnet added in the UI must be honoured at once."""
        resp = _post_ssrf(admin_client, "10.1.16.0/24")
        assert resp.status_code == 303

        assert _stored_subnets() == ["10.1.16.0/24"]
        # Applied to the running policy without a restart -- this is what lets the
        # 10.1.16.30 camera be added immediately after saving.
        assert "10.1.16.0/24" in _runtime_subnets()

    def test_bare_host_is_stored_as_slash_32(self, admin_client: TestClient) -> None:
        resp = _post_ssrf(admin_client, "10.1.16.30")
        assert resp.status_code == 303
        assert _stored_subnets() == ["10.1.16.30/32"]

    def test_host_with_prefix_collapses_to_network(
        self, admin_client: TestClient
    ) -> None:
        resp = _post_ssrf(admin_client, "10.1.16.30/24")
        assert resp.status_code == 303
        assert _stored_subnets() == ["10.1.16.0/24"]

    def test_multiple_lines_dedupe_and_keep_order(
        self, admin_client: TestClient
    ) -> None:
        resp = _post_ssrf(admin_client, "10.1.16.0/24\n192.168.5.0/24\n10.1.16.0/24\n")
        assert resp.status_code == 303
        assert _stored_subnets() == ["10.1.16.0/24", "192.168.5.0/24"]

    def test_config_baseline_is_preserved_in_runtime(
        self, admin_client: TestClient
    ) -> None:
        """An env/config-provided subnet survives an admin edit (union semantics)."""
        # Simulate a subnet supplied by TLM_SSRF__ALLOWED_PRIVATE_SUBNETS at boot.
        get_context().ssrf_config_subnets = ("10.1.40.0/24",)

        resp = _post_ssrf(admin_client, "10.1.16.0/24")
        assert resp.status_code == 303

        runtime = _runtime_subnets()
        assert "10.1.40.0/24" in runtime  # baseline kept
        assert "10.1.16.0/24" in runtime  # admin addition applied
        # The DB row holds only the admin list, never the env baseline.
        assert _stored_subnets() == ["10.1.16.0/24"]

    def test_empty_clears_admin_list(self, admin_client: TestClient) -> None:
        _post_ssrf(admin_client, "10.1.16.0/24")
        assert _stored_subnets() == ["10.1.16.0/24"]

        resp = _post_ssrf(admin_client, "")
        assert resp.status_code == 303
        assert _stored_subnets() == []


class TestSsrfSettingsValidation:
    def test_invalid_entry_rejected_and_nothing_persisted(
        self, admin_client: TestClient
    ) -> None:
        resp = _post_ssrf(admin_client, "not-a-subnet")
        # Re-renders the full settings page with an inline error (no redirect).
        assert resp.status_code == 200
        assert "not a valid cidr or ip" in resp.text.lower()
        # Nothing was written.
        assert _stored_subnets() in (None, [])

    def test_one_bad_line_refuses_whole_save(self, admin_client: TestClient) -> None:
        resp = _post_ssrf(admin_client, "10.1.16.0/24\nbogus")
        assert resp.status_code == 200
        # The good line is not silently kept -- the save is refused as a unit.
        assert _stored_subnets() in (None, [])
