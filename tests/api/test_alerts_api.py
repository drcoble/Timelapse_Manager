"""API-level tests for the active-alerts endpoints.

Covers: listing active alerts (warning+ uncleared, info excluded), clear one,
clear all, idempotency on already-cleared / non-alert ids, attribution recorded,
and operator/admin gating (viewer/denied blocked with 403).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.principal import (
    Principal,
    require_operator_or_admin_principal,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _override_admin(app: object) -> None:
    app.dependency_overrides[require_operator_or_admin_principal] = lambda: Principal(  # type: ignore[attr-defined]
        user_id=1, role="admin"
    )


def _override_operator(app: object) -> None:
    app.dependency_overrides[require_operator_or_admin_principal] = lambda: Principal(  # type: ignore[attr-defined]
        user_id=1, role="operator"
    )


def _override_deny(app: object) -> None:
    def _deny() -> Principal:
        raise HTTPException(status_code=403, detail="forbidden")

    app.dependency_overrides[require_operator_or_admin_principal] = _deny  # type: ignore[attr-defined]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _seed_event(
    factory: sessionmaker,  # type: ignore[type-arg]
    *,
    level: str = "warning",
    message: str = "alert",
    scope_id: int | None = 1,
) -> int:
    with session_scope(factory) as session:
        event = Event(
            scope="project",
            scope_id=scope_id,
            level=level,
            message=message,
            timestamp=_now(),
        )
        session.add(event)
        session.flush()
        return event.id


class TestListAlerts:
    def test_lists_active_alerts_excluding_info(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        cam_auth_token: str,
    ) -> None:
        _seed_event(migrated_factory, level="warning", message="w")
        _seed_event(migrated_factory, level="info", message="i")
        resp = migrated_client.get("/api/v1/alerts", headers=_auth(cam_auth_token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["alerts"][0]["message"] == "w"
        assert body["alerts"][0]["level"] == "warning"


class TestClearOne:
    def test_clear_one_records_attribution(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        cam_auth_token: str,
    ) -> None:
        _override_admin(migrated_client.app)
        event_id = _seed_event(migrated_factory, level="error")
        resp = migrated_client.post(
            f"/api/v1/alerts/{event_id}/clear", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 1
        with session_scope(migrated_factory) as session:
            row = session.get(Event, event_id)
            assert row is not None  # row retained
            assert row.alert_cleared_at is not None
            assert row.alert_cleared_by == 1
            assert row.alert_clear_reason == "manual"

    def test_clear_already_cleared_is_idempotent(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        cam_auth_token: str,
    ) -> None:
        _override_admin(migrated_client.app)
        event_id = _seed_event(migrated_factory, level="warning")
        first = migrated_client.post(
            f"/api/v1/alerts/{event_id}/clear", headers=_auth(cam_auth_token)
        )
        assert first.json()["cleared"] == 1
        second = migrated_client.post(
            f"/api/v1/alerts/{event_id}/clear", headers=_auth(cam_auth_token)
        )
        assert second.status_code == 200
        assert second.json()["cleared"] == 0

    def test_clear_info_event_is_noop(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        cam_auth_token: str,
    ) -> None:
        _override_admin(migrated_client.app)
        event_id = _seed_event(migrated_factory, level="info")
        resp = migrated_client.post(
            f"/api/v1/alerts/{event_id}/clear", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 0

    def test_clear_unknown_id_is_noop(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        _override_admin(migrated_client.app)
        resp = migrated_client.post(
            "/api/v1/alerts/9999/clear", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 0

    def test_operator_allowed(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        event_id = _seed_event(migrated_factory, level="warning")
        resp = migrated_client.post(
            f"/api/v1/alerts/{event_id}/clear", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 1

    def test_viewer_denied(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        cam_auth_token: str,
    ) -> None:
        _override_deny(migrated_client.app)
        event_id = _seed_event(migrated_factory, level="warning")
        resp = migrated_client.post(
            f"/api/v1/alerts/{event_id}/clear", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 403


class TestClearAll:
    def test_clear_all(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        cam_auth_token: str,
    ) -> None:
        _override_admin(migrated_client.app)
        _seed_event(migrated_factory, level="warning", message="a")
        _seed_event(migrated_factory, level="critical", message="b")
        _seed_event(migrated_factory, level="info", message="c")
        resp = migrated_client.post(
            "/api/v1/alerts/clear-all", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 2
        # The active list is now empty.
        listed = migrated_client.get("/api/v1/alerts", headers=_auth(cam_auth_token))
        assert listed.json()["total"] == 0

    def test_clear_all_denied_for_viewer(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        _override_deny(migrated_client.app)
        resp = migrated_client.post(
            "/api/v1/alerts/clear-all", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 403
