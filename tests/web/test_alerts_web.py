"""Web integration tests for the in-UI active-alerts area.

Covers:
- GET /alerts/summary: badge count for an authenticated user; neutral at zero;
  info-level events are not alerts.
- Clear controls present for operator/admin, ABSENT for a viewer.
- POST /alerts/{id}/clear: operator clears (persisted + fragment reflects it);
  viewer 403; missing CSRF rejected.
- POST /alerts/clear-all: operator clears all; viewer 403.
- Unauthenticated -> 303 redirect to /login like other authenticated routes.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import get_active_alerts, log_event
from timelapse_manager.runtime import get_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_alert(
    *,
    scope: str = "system",
    scope_id: int | None = None,
    level: str = "warning",
    message: str = "alert event",
) -> int:
    """Seed one event at the given level; return its event id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        log_event(
            db,
            scope=scope,
            scope_id=scope_id,
            level=level,
            message=message,
        )
        db.flush()
        row = (
            db.query(Event)
            .filter(Event.message == message)
            .order_by(Event.id.desc())
            .first()
        )
        assert row is not None
        return row.id


def _active_count() -> int:
    """Return the current total active-alert count straight from the backend."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        _, total = get_active_alerts(db)
        return total


# ---------------------------------------------------------------------------
# GET /alerts/summary — badge count
# ---------------------------------------------------------------------------


class TestAlertsSummary:
    def test_summary_returns_200_for_authenticated_user(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get("/alerts/summary", follow_redirects=False)
        assert resp.status_code == 200

    def test_badge_absent_when_no_alerts(self, viewer_client: TestClient) -> None:
        """At zero active alerts the count badge is not rendered."""
        resp = viewer_client.get("/alerts/summary", follow_redirects=False)
        assert resp.status_code == 200
        assert 'class="alerts-badge"' not in resp.text
        assert "0 active alerts" in resp.text

    def test_badge_shows_active_count(self, admin_client: TestClient) -> None:
        """Seeding warning+ events makes the badge show the correct count."""
        _seed_alert(level="warning", message="ALERT-A")
        _seed_alert(level="error", message="ALERT-B")
        _seed_alert(level="critical", message="ALERT-C")
        # An info-level event is below the threshold and must NOT count.
        _seed_alert(level="info", message="NOT-AN-ALERT")

        assert _active_count() == 3
        resp = admin_client.get("/alerts/summary", follow_redirects=False)
        assert resp.status_code == 200
        assert 'class="alerts-badge"' in resp.text
        assert ">3<" in resp.text
        assert "ALERT-A" in resp.text
        assert "ALERT-B" in resp.text
        assert "NOT-AN-ALERT" not in resp.text

    def test_aria_label_includes_count(self, admin_client: TestClient) -> None:
        """The focusable indicator carries the count in its accessible label."""
        _seed_alert(level="warning", message="ALERT-ARIA")
        _seed_alert(level="warning", message="ALERT-ARIA-2")
        resp = admin_client.get("/alerts/summary", follow_redirects=False)
        assert 'aria-label="Active alerts: 2"' in resp.text


# ---------------------------------------------------------------------------
# Clear controls — operator/admin only
# ---------------------------------------------------------------------------


class TestAlertsClearControlsVisibility:
    def test_clear_controls_present_for_admin(self, admin_client: TestClient) -> None:
        _seed_alert(level="warning", message="ALERT-ADMIN")
        resp = admin_client.get("/alerts/summary", follow_redirects=False)
        assert 'hx-post="/alerts/clear-all"' in resp.text
        assert "Dismiss" in resp.text

    def test_clear_controls_present_for_operator(
        self, operator_client: TestClient
    ) -> None:
        _seed_alert(level="warning", message="ALERT-OPERATOR")
        resp = operator_client.get("/alerts/summary", follow_redirects=False)
        assert 'hx-post="/alerts/clear-all"' in resp.text
        assert "Dismiss" in resp.text

    def test_clear_controls_absent_for_viewer(self, viewer_client: TestClient) -> None:
        """A viewer sees the alert list but no dismiss / clear-all affordances."""
        _seed_alert(level="warning", message="ALERT-VIEWER-VISIBLE")
        resp = viewer_client.get("/alerts/summary", follow_redirects=False)
        assert resp.status_code == 200
        # The alert itself is visible to the viewer ...
        assert "ALERT-VIEWER-VISIBLE" in resp.text
        # ... but no clear controls.
        assert 'hx-post="/alerts/clear-all"' not in resp.text
        assert "Dismiss" not in resp.text


# ---------------------------------------------------------------------------
# POST /alerts/{id}/clear — operator clears, viewer 403, CSRF required
# ---------------------------------------------------------------------------


class TestClearOne:
    def test_operator_clears_alert_and_fragment_reflects_it(
        self, operator_client: TestClient
    ) -> None:
        event_id = _seed_alert(level="warning", message="CLEAR-ME")
        _seed_alert(level="error", message="STAYS-ACTIVE")
        assert _active_count() == 2

        csrf = csrf_of(operator_client, "/")
        resp = operator_client.post(
            f"/alerts/{event_id}/clear",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # Backend state changed: one alert remains active.
        assert _active_count() == 1
        # The refreshed fragment reflects the new count and drops the cleared one.
        assert "CLEAR-ME" not in resp.text
        assert "STAYS-ACTIVE" in resp.text
        assert "1 active alert" in resp.text

    def test_viewer_forbidden_to_clear(self, viewer_client: TestClient) -> None:
        """A viewer with a valid CSRF token is still 403 (role gate, not CSRF)."""
        event_id = _seed_alert(level="warning", message="VIEWER-CANNOT-CLEAR")
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            f"/alerts/{event_id}/clear",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        # The alert was not cleared.
        assert _active_count() == 1

    def test_missing_csrf_rejected(self, operator_client: TestClient) -> None:
        """A clear POST with no CSRF token is rejected like other web POSTs."""
        event_id = _seed_alert(level="warning", message="CSRF-GUARDED")
        resp = operator_client.post(
            f"/alerts/{event_id}/clear",
            data={},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        # No clear happened.
        assert _active_count() == 1


# ---------------------------------------------------------------------------
# POST /alerts/clear-all — operator clears all, viewer 403
# ---------------------------------------------------------------------------


class TestClearAll:
    def test_operator_clears_all(self, operator_client: TestClient) -> None:
        _seed_alert(level="warning", message="ALL-A")
        _seed_alert(level="error", message="ALL-B")
        _seed_alert(level="critical", message="ALL-C")
        assert _active_count() == 3

        csrf = csrf_of(operator_client, "/")
        resp = operator_client.post(
            "/alerts/clear-all",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert _active_count() == 0
        assert "0 active alerts" in resp.text
        assert 'class="alerts-badge"' not in resp.text

    def test_viewer_forbidden_to_clear_all(self, viewer_client: TestClient) -> None:
        _seed_alert(level="warning", message="VIEWER-NO-CLEAR-ALL")
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/alerts/clear-all",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _active_count() == 1


# ---------------------------------------------------------------------------
# Unauthenticated access -> redirect to login
# ---------------------------------------------------------------------------


class TestAlertsUnauthenticated:
    def test_summary_redirects_anonymous_browser_to_login(
        self, anon_client: TestClient
    ) -> None:
        """A browser navigation (Accept: text/html) is redirected to login."""
        resp = anon_client.get(
            "/alerts/summary",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == f"/login?next={quote('/alerts/summary', safe='')}"
        )

    def test_summary_htmx_anonymous_gets_login_redirect(
        self, anon_client: TestClient
    ) -> None:
        """The HTMX poll for an anonymous client gets an HX-Redirect to login.

        This is the path the indicator actually exercises (the container polls
        with hx-get), so an expired session bounces the whole page to login
        rather than swapping the login form into the indicator slot. Crucially the
        post-login return-to is the page the user is ON (HX-Current-URL), never the
        polled /alerts/summary fragment -- landing on that bare partial was a bug.
        """
        resp = anon_client.get(
            "/alerts/summary",
            headers={
                "HX-Request": "true",
                "HX-Current-URL": "https://testserver/projects",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 204
        assert (
            resp.headers["HX-Redirect"] == f"/login?next={quote('/projects', safe='')}"
        )
        assert "alerts/summary" not in resp.headers["HX-Redirect"]

    def test_summary_htmx_poll_without_current_url_uses_bare_login(
        self, anon_client: TestClient
    ) -> None:
        """With no HX-Current-URL to recover the page, a bare /login is used.

        The login route then resolves that to the dashboard -- never the
        /alerts/summary fragment.
        """
        resp = anon_client.get(
            "/alerts/summary",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 204
        assert resp.headers["HX-Redirect"] == "/login"


# ---------------------------------------------------------------------------
# base.html: indicator wiring
# ---------------------------------------------------------------------------


class TestBaseHtmlAlertsIndicator:
    def test_base_html_contains_alerts_area_polling(
        self, admin_client: TestClient
    ) -> None:
        """base.html must wire the alerts indicator to poll the summary fragment."""
        resp = admin_client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert 'id="alerts-area"' in resp.text
        assert 'hx-get="/alerts/summary"' in resp.text
        assert "every 30s" in resp.text
