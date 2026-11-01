"""Web integration tests for monitoring routes.

Covers:
- GET /events: viewer 200 (operational only, exclusion test with typeless rows)
- GET /events/audit: viewer 403 / admin 200
- GET /notification-settings: viewer 403 / admin 200
- POST /notification-settings: viewer 403 / admin 200; blank password keeps secret
- GET /partials/status: 200 + status-banner div even when forced to error
- base.html: hx-get="/partials/status" present for lazy load
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import EventType, log_event
from timelapse_manager.monitoring.settings_service import (
    MASK_SENTINEL,
    NotificationSettingsUpdate,
    update_settings,
)
from timelapse_manager.runtime import get_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_event(
    client: TestClient,
    *,
    scope: str = "system",
    scope_id: int | None = None,
    level: str = "info",
    message: str = "test event",
    event_type: str | None = None,
) -> None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        log_event(
            db,
            scope=scope,
            scope_id=scope_id,
            level=level,
            message=message,
            type=event_type,
        )


# ---------------------------------------------------------------------------
# GET /events — viewer sees operational only
# ---------------------------------------------------------------------------


class TestEventsPageViewerAccess:
    def test_viewer_gets_200_on_events_page(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/events", follow_redirects=False)
        assert resp.status_code == 200

    def test_events_page_excludes_audit_control_action_for_viewer(
        self, viewer_client: TestClient
    ) -> None:
        """Audit-typed rows must NOT appear in the /events view for a viewer."""
        _seed_event(
            viewer_client,
            event_type=EventType.AUDIT_CONTROL_ACTION.value,
            message="AUDIT-MARKER-EXCLUDED",
        )
        resp = viewer_client.get("/events", follow_redirects=False)
        assert resp.status_code == 200
        assert "AUDIT-MARKER-EXCLUDED" not in resp.text

    def test_events_page_excludes_security_auth_event_for_viewer(
        self, viewer_client: TestClient
    ) -> None:
        """Security-typed rows must NOT appear in the /events view for a viewer."""
        _seed_event(
            viewer_client,
            event_type=EventType.SECURITY_AUTH_EVENT.value,
            message="SECURITY-MARKER-EXCLUDED",
        )
        resp = viewer_client.get("/events", follow_redirects=False)
        assert "SECURITY-MARKER-EXCLUDED" not in resp.text

    def test_events_page_shows_operational_typed_row(
        self, viewer_client: TestClient
    ) -> None:
        """A typed operational event (capture.gap) IS visible on /events."""
        _seed_event(
            viewer_client,
            event_type=EventType.CAPTURE_GAP.value,
            message="CAPTURE-GAP-VISIBLE",
        )
        resp = viewer_client.get("/events", follow_redirects=False)
        assert "CAPTURE-GAP-VISIBLE" in resp.text

    def test_events_page_shows_typeless_operational_row(
        self, viewer_client: TestClient
    ) -> None:
        """A typeless operational row (no 'type' key) must NOT be dropped from /events.

        The NULL-tolerant exclusion filter must not silently drop rows whose
        event_metadata is NULL or whose metadata has no 'type' key — those are
        valid operational events (e.g. system startup, capture start).
        """
        _seed_event(
            viewer_client,
            event_type=None,  # no type — produces NULL metadata
            message="TYPELESS-OPERATIONAL-VISIBLE",
        )
        resp = viewer_client.get("/events", follow_redirects=False)
        assert "TYPELESS-OPERATIONAL-VISIBLE" in resp.text

    def test_events_page_four_rows_exclusion_test(
        self, viewer_client: TestClient
    ) -> None:
        """Seed all four row varieties and assert exactly the right ones are visible.

        Seeds:
          1. Typeless operational  → must appear
          2. Typed operational (capture.gap) → must appear
          3. Audit (audit.control_action) → must NOT appear
          4. Security (security.auth_event) → must NOT appear
        """
        _seed_event(viewer_client, event_type=None, message="ROW-TYPELESS-OPERATIONAL")
        _seed_event(
            viewer_client,
            event_type=EventType.CAPTURE_GAP.value,
            message="ROW-TYPED-OPERATIONAL",
        )
        _seed_event(
            viewer_client,
            event_type=EventType.AUDIT_CONTROL_ACTION.value,
            message="ROW-AUDIT-EXCLUDED",
        )
        _seed_event(
            viewer_client,
            event_type=EventType.SECURITY_AUTH_EVENT.value,
            message="ROW-SECURITY-EXCLUDED",
        )

        resp = viewer_client.get("/events", follow_redirects=False)
        assert resp.status_code == 200
        assert "ROW-TYPELESS-OPERATIONAL" in resp.text
        assert "ROW-TYPED-OPERATIONAL" in resp.text
        assert "ROW-AUDIT-EXCLUDED" not in resp.text
        assert "ROW-SECURITY-EXCLUDED" not in resp.text


# ---------------------------------------------------------------------------
# GET /events/audit — admin-only
# ---------------------------------------------------------------------------


class TestAuditEventsPage:
    def test_viewer_gets_403_on_audit_events(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/events/audit", follow_redirects=False)
        assert resp.status_code == 403

    def test_admin_gets_200_on_audit_events(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/events/audit", follow_redirects=False)
        assert resp.status_code == 200

    def test_admin_sees_audit_row_on_audit_page(self, admin_client: TestClient) -> None:
        _seed_event(
            admin_client,
            event_type=EventType.AUDIT_CONTROL_ACTION.value,
            message="AUDIT-ROW-VISIBLE-ADMIN",
        )
        resp = admin_client.get("/events/audit", follow_redirects=False)
        assert "AUDIT-ROW-VISIBLE-ADMIN" in resp.text


# ---------------------------------------------------------------------------
# GET /notification-settings — admin-only
# ---------------------------------------------------------------------------


class TestNotificationSettingsGet:
    def test_viewer_gets_403_on_notification_settings(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get("/notification-settings", follow_redirects=False)
        assert resp.status_code == 403

    def test_admin_gets_200_on_notification_settings(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/notification-settings", follow_redirects=False)
        assert resp.status_code == 200

    def test_notification_settings_page_shows_form(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/notification-settings", follow_redirects=False)
        assert "smtp" in resp.text.lower() or "notification" in resp.text.lower()


# ---------------------------------------------------------------------------
# POST /notification-settings — admin-only, password keep rule
# ---------------------------------------------------------------------------


class TestNotificationSettingsPost:
    def test_viewer_gets_403_posting_notification_settings(
        self, viewer_client: TestClient
    ) -> None:
        csrf = csrf_of(viewer_client, "/events")
        resp = viewer_client.post(
            "/notification-settings",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_admin_post_redirects_on_success(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/notification-settings")
        resp = admin_client.post(
            "/notification-settings",
            data={
                "csrf_token": csrf,
                "smtp_server": "mail.example.com",
                "smtp_port": "587",
                "smtp_security": "starttls",
                "smtp_username": "user",
                "smtp_password": "initial-password",
                "smtp_from_address": "from@example.com",
                "smtp_recipients": "ops@example.com",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    @pytest.mark.parametrize("mode", ["plain", "TLS", ""])
    def test_unknown_security_mode_is_an_inline_error_and_writes_nothing(
        self, admin_client: TestClient, mode: str
    ) -> None:
        """An unknown mode is an inline error, never saved as ``none`` (#214)."""
        from timelapse_manager.db.models import NotificationSettings

        ctx = get_context()
        csrf = csrf_of(admin_client, "/notification-settings")
        resp = admin_client.post(
            "/notification-settings",
            data={
                "csrf_token": csrf,
                "smtp_server": "mail.example.com",
                "smtp_port": "587",
                "smtp_security": mode,
                "smtp_username": "user",
                "smtp_password": "initial-password",
                "smtp_from_address": "from@example.com",
                "smtp_recipients": "ops@example.com",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 422
        assert "Unknown SMTP security mode" in resp.text
        assert "initial-password" not in resp.text
        with session_scope(ctx.session_factory) as db:
            assert db.get(NotificationSettings, 1) is None

    def test_missing_security_field_is_refused_not_defaulted_to_none(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/notification-settings")
        resp = admin_client.post(
            "/notification-settings",
            data={"csrf_token": csrf, "smtp_server": "mail.example.com"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 422

    def test_blank_password_post_keeps_stored_secret(
        self, admin_client: TestClient
    ) -> None:
        """Submitting blank password in the form leaves the stored password intact.

        The mutation confound-breaker: we also change smtp_server in the same POST
        and assert that it DID change, proving the update_settings call actually
        ran (i.e. the keep-rule preserved the password, not a silent form rejection).
        """
        # First: store a real password with a known server.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            update_settings(
                db,
                NotificationSettingsUpdate(
                    enabled_channels=[],
                    smtp_server="original.example.com",
                    smtp_port=587,
                    smtp_security="none",
                    smtp_username="user",
                    smtp_password="stored-real-password",
                    smtp_from_address="from@example.com",
                    smtp_recipients=["ops@example.com"],
                    webhook_urls=[],
                    routing_rules=[],
                ),
            )

        # Submit with a blank password field and the SMTP target unchanged, but a
        # CHANGED from-address. This proves update_settings executed (the sibling
        # changed) while the password was preserved (keep-rule applied, not a
        # rejection). Changing the target itself clears it: see the retarget test.
        csrf = csrf_of(admin_client, "/notification-settings")
        admin_client.post(
            "/notification-settings",
            data={
                "csrf_token": csrf,
                "smtp_server": "original.example.com",
                "smtp_port": "587",
                "smtp_security": "none",
                "smtp_username": "user",
                "smtp_from_address": "changed-from@example.com",  # mutated sibling
                "smtp_recipients": "ops@example.com",
                "smtp_password": "",  # blank — must keep stored value
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

        from timelapse_manager.db.models import NotificationSettings
        from timelapse_manager.security.crypto import decrypt_secret

        with session_scope(ctx.session_factory) as db:
            row = db.get(NotificationSettings, 1)
        assert row is not None
        # The sibling field change proves update_settings actually ran.
        assert row.smtp_from_address == "changed-from@example.com"
        # The password keep-rule preserved the stored secret (column holds ciphertext).
        assert decrypt_secret(row.smtp_password) == "stored-real-password"

    def test_retargeting_smtp_with_blank_password_clears_it(
        self, admin_client: TestClient
    ) -> None:
        """A new host with no new password must not inherit the old password (S9)."""
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            update_settings(
                db,
                NotificationSettingsUpdate(
                    enabled_channels=[],
                    smtp_server="original.example.com",
                    smtp_port=587,
                    smtp_security="none",
                    smtp_username="user",
                    smtp_password="stored-real-password",
                    smtp_from_address="from@example.com",
                    smtp_recipients=["ops@example.com"],
                    webhook_urls=[],
                    routing_rules=[],
                ),
            )
        csrf = csrf_of(admin_client, "/notification-settings")
        resp = admin_client.post(
            "/notification-settings",
            data={
                "csrf_token": csrf,
                "smtp_server": "changed.example.com",
                "smtp_port": "587",
                "smtp_security": "none",
                "smtp_username": "user",
                "smtp_from_address": "from@example.com",
                "smtp_recipients": "ops@example.com",
                "smtp_password": "***",  # the masked value the form round-trips
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        from timelapse_manager.db.models import NotificationSettings

        with session_scope(ctx.session_factory) as db:
            row = db.get(NotificationSettings, 1)
        assert row is not None
        assert row.smtp_server == "changed.example.com"
        assert row.smtp_password is None

    def test_password_not_in_notification_settings_page_html(
        self, admin_client: TestClient
    ) -> None:
        """The rendered settings page must never contain the plaintext password.

        The view layer masks the password to MASK_SENTINEL before it reaches
        the template, so the real credential must not appear in the HTML.
        """
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            update_settings(
                db,
                NotificationSettingsUpdate(
                    enabled_channels=[],
                    smtp_server="mail.example.com",
                    smtp_port=587,
                    smtp_security="none",
                    smtp_username="u",
                    smtp_password="do-not-show-this-password",
                    smtp_from_address="f@example.com",
                    smtp_recipients=[],
                    webhook_urls=[],
                    routing_rules=[],
                ),
            )

        resp = admin_client.get("/notification-settings", follow_redirects=False)
        assert "do-not-show-this-password" not in resp.text
        # The mask sentinel should be in the page (value="***").
        assert MASK_SENTINEL in resp.text


# ---------------------------------------------------------------------------
# POST /notification-settings — webhook signing secret keep rule
# ---------------------------------------------------------------------------


def _post_settings(client: TestClient, **fields: str) -> None:
    """POST the notification settings form with a fresh CSRF token."""
    csrf = csrf_of(client, "/notification-settings")
    base = {
        "csrf_token": csrf,
        "smtp_server": "mail.example.com",
        "smtp_port": "587",
        "smtp_security": "none",
        "smtp_from_address": "from@example.com",
        "smtp_recipients": "ops@example.com",
    }
    base.update(fields)
    client.post(
        "/notification-settings",
        data=base,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestWebhookSigningSecretPost:
    def test_saving_secret_encrypts_and_stores_it(
        self, admin_client: TestClient
    ) -> None:
        """A submitted signing secret is encrypted at rest and decrypts back."""
        from timelapse_manager.db.models import NotificationSettings
        from timelapse_manager.security.crypto import decrypt_secret, is_encrypted

        _post_settings(admin_client, webhook_signing_secret="top-secret-hmac-key")

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(NotificationSettings, 1)
        assert row is not None
        stored = row.webhook_signing_secret
        assert stored is not None
        assert stored != "top-secret-hmac-key", (
            "Signing secret stored as plaintext — encryption is not applied"
        )
        assert is_encrypted(stored)
        assert decrypt_secret(stored) == "top-secret-hmac-key"

    def test_sentinel_submission_keeps_stored_secret(
        self, admin_client: TestClient
    ) -> None:
        """Re-submitting the mask sentinel leaves the stored secret untouched.

        The sibling field (smtp_server) is changed in the same POST to prove the
        save actually ran rather than being silently rejected.
        """
        from timelapse_manager.db.models import NotificationSettings
        from timelapse_manager.monitoring.settings_service import MASK_SENTINEL
        from timelapse_manager.security.crypto import decrypt_secret

        _post_settings(admin_client, webhook_signing_secret="keep-this-secret")
        _post_settings(
            admin_client,
            smtp_server="changed.example.com",
            webhook_signing_secret=MASK_SENTINEL,
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(NotificationSettings, 1)
        assert row is not None
        # Sibling change proves the save ran; secret preserved by the keep-rule.
        assert row.smtp_server == "changed.example.com"
        assert decrypt_secret(row.webhook_signing_secret) == "keep-this-secret"

    def test_blank_submission_keeps_stored_secret(
        self, admin_client: TestClient
    ) -> None:
        """A blank signing-secret field leaves an existing stored secret intact."""
        from timelapse_manager.db.models import NotificationSettings
        from timelapse_manager.security.crypto import decrypt_secret

        _post_settings(admin_client, webhook_signing_secret="another-secret")
        _post_settings(admin_client, webhook_signing_secret="")

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(NotificationSettings, 1)
        assert row is not None
        assert decrypt_secret(row.webhook_signing_secret) == "another-secret"

    def test_unset_secret_stays_null_on_blank_submission(
        self, admin_client: TestClient
    ) -> None:
        """With nothing stored, a blank submission leaves the column NULL.

        The keep-rule (mirroring the SMTP password) never clears a *set* secret;
        "clearing" is only meaningful as the never-set case, where the column
        stays NULL and webhooks remain unsigned.
        """
        from timelapse_manager.db.models import NotificationSettings

        _post_settings(admin_client, webhook_signing_secret="")

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            row = db.get(NotificationSettings, 1)
        assert row is not None
        assert row.webhook_signing_secret is None

    def test_secret_not_rendered_in_page_html(self, admin_client: TestClient) -> None:
        """The rendered page masks the signing secret, never echoing plaintext."""
        from timelapse_manager.monitoring.settings_service import MASK_SENTINEL

        _post_settings(admin_client, webhook_signing_secret="do-not-echo-this-secret")

        resp = admin_client.get("/notification-settings", follow_redirects=False)
        assert "do-not-echo-this-secret" not in resp.text
        assert MASK_SENTINEL in resp.text


# ---------------------------------------------------------------------------
# GET /partials/status — failure-isolated
# ---------------------------------------------------------------------------


class TestStatusPartial:
    def test_status_partial_returns_200(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/partials/status", follow_redirects=False)
        assert resp.status_code == 200

    def test_status_partial_returns_status_banner_div(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get("/partials/status", follow_redirects=False)
        assert 'id="status-banner"' in resp.text

    def test_status_partial_returns_200_when_get_events_raises(
        self, viewer_client: TestClient
    ) -> None:
        """The status partial must degrade to 200 + benign banner on any query error.

        This proves the failure-isolation contract: a monitoring query hiccup
        must never propagate as a 500 to the caller.
        """
        with patch(
            "timelapse_manager.web.routers.events.get_events",
            side_effect=RuntimeError("simulated DB error"),
        ):
            resp = viewer_client.get("/partials/status", follow_redirects=False)
        assert resp.status_code == 200
        assert 'id="status-banner"' in resp.text

    def test_status_partial_shows_error_count_when_errors_exist(
        self, admin_client: TestClient
    ) -> None:
        """When error events exist, the banner shows a non-zero count."""
        _seed_event(admin_client, level="error", message="disk almost full")
        resp = admin_client.get("/partials/status", follow_redirects=False)
        assert resp.status_code == 200
        # The banner content (count > 0) should show "error" somewhere.
        assert "error" in resp.text.lower() or "1" in resp.text


# ---------------------------------------------------------------------------
# base.html: lazy status load via HTMX
# ---------------------------------------------------------------------------


class TestBaseHtmlStatusBannerLazyLoad:
    def test_dashboard_contains_hx_get_partials_status(
        self, admin_client: TestClient
    ) -> None:
        """base.html must include hx-get='/partials/status' for HTMX lazy load."""
        resp = admin_client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert 'hx-get="/partials/status"' in resp.text
