"""API tests for the milestone endpoints under /api/v1/projects/{id}/milestones.

Covers:
- POST creates a milestone (frame-indexed or timestamp)
- POST returns 422 when neither position field supplied
- PATCH updates label and/or position
- PATCH returns 404 when milestone belongs to a different project
- PATCH returns 422 when update would leave no position at all
- GET lists milestones in creation order
- 401 without token
"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Milestone, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.principal import (
    Principal,
    require_operator_or_admin_principal,
)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _override_operator(app: object) -> None:
    """Override the mutation principal with a real operator principal."""
    app.dependency_overrides[require_operator_or_admin_principal] = lambda: Principal(  # type: ignore[attr-defined]
        user_id=1, role="operator"
    )


def _clear_overrides(app: object) -> None:
    app.dependency_overrides.clear()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


def _seed_project(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    name: str = "ms-api-proj",
) -> int:
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        project_id = proj.id
        (frames_root / str(project_id)).mkdir(parents=True, exist_ok=True)
    return project_id


def _seed_milestone(
    factory: sessionmaker,  # type: ignore[type-arg]
    project_id: int,
    *,
    label: str = "Test milestone",
    frame_index: int | None = 5,
    timestamp: datetime | None = None,
) -> int:
    from timelapse_manager.security.principal import ensure_sentinel_admin

    with session_scope(factory) as session:
        # Milestone.user_id is a FK to user; materialise the sentinel so the FK holds.
        user_id = ensure_sentinel_admin(session)
        ms = Milestone(
            project_id=project_id,
            user_id=user_id,
            label=label,
            position_frame_index=frame_index,
            position_timestamp=timestamp,
        )
        session.add(ms)
        session.flush()
        ms_id = ms.id
    return ms_id


# ---------------------------------------------------------------------------
# Tests: 401 without token
# ---------------------------------------------------------------------------


class TestMilestoneAPIAuth:
    def test_post_milestone_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.post(
            "/api/v1/projects/1/milestones",
            json={"label": "x", "position_frame_index": 0},
        )
        assert resp.status_code == 401

    def test_patch_milestone_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.patch(
            "/api/v1/projects/1/milestones/1",
            json={"label": "x"},
        )
        assert resp.status_code == 401

    def test_get_milestones_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.get("/api/v1/projects/1/milestones")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/projects/{id}/milestones
# ---------------------------------------------------------------------------


class TestCreateMilestone:
    def test_create_by_frame_index_returns_201(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-post-frame"
            )
            resp = migrated_client.post(
                f"/api/v1/projects/{project_id}/milestones",
                json={"label": "Foundation poured", "position_frame_index": 10},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 201
        finally:
            _clear_overrides(migrated_client.app)

    def test_create_by_frame_index_returns_milestone_json(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-post-json"
            )
            resp = migrated_client.post(
                f"/api/v1/projects/{project_id}/milestones",
                json={"label": "Steel up", "position_frame_index": 42},
                headers=_auth(cam_auth_token),
            )
            body = resp.json()
            assert body["label"] == "Steel up"
            assert body["position_frame_index"] == 42
            assert body["position_timestamp"] is None
            assert body["project_id"] == project_id
        finally:
            _clear_overrides(migrated_client.app)

    def test_create_by_timestamp_returns_201(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-post-ts"
            )
            resp = migrated_client.post(
                f"/api/v1/projects/{project_id}/milestones",
                json={
                    "label": "Delivery day",
                    "position_timestamp": "2026-03-15T09:00:00Z",
                },
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 201
            body = resp.json()
            assert body["position_frame_index"] is None
            assert body["position_timestamp"] is not None
        finally:
            _clear_overrides(migrated_client.app)

    def test_create_without_any_position_returns_422(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-post-nopos"
            )
            resp = migrated_client.post(
                f"/api/v1/projects/{project_id}/milestones",
                json={"label": "No position"},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 422
        finally:
            _clear_overrides(migrated_client.app)

    def test_create_for_nonexistent_project_returns_404(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            resp = migrated_client.post(
                "/api/v1/projects/99999/milestones",
                json={"label": "Ghost", "position_frame_index": 0},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 404
        finally:
            _clear_overrides(migrated_client.app)


# ---------------------------------------------------------------------------
# Tests: PATCH /api/v1/projects/{id}/milestones/{milestone_id}
# ---------------------------------------------------------------------------


class TestPatchMilestone:
    def test_patch_updates_label(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-patch-label"
            )
            ms_id = _seed_milestone(migrated_factory, project_id, label="Old label")

            resp = migrated_client.patch(
                f"/api/v1/projects/{project_id}/milestones/{ms_id}",
                json={"label": "New label"},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 200
            assert resp.json()["label"] == "New label"
        finally:
            _clear_overrides(migrated_client.app)

    def test_patch_updates_position_frame_index(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-patch-pos"
            )
            ms_id = _seed_milestone(migrated_factory, project_id, frame_index=1)

            resp = migrated_client.patch(
                f"/api/v1/projects/{project_id}/milestones/{ms_id}",
                json={"position_frame_index": 99},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 200
            assert resp.json()["position_frame_index"] == 99
        finally:
            _clear_overrides(migrated_client.app)

    def test_patch_milestone_in_wrong_project_returns_404(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """A milestone from project A must not be patchable via project B's URL."""
        _override_operator(migrated_client.app)
        try:
            project_a = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-xproj-a"
            )
            project_b = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-xproj-b"
            )
            ms_id = _seed_milestone(migrated_factory, project_a)

            # Request milestone from project A via project B's URL.
            resp = migrated_client.patch(
                f"/api/v1/projects/{project_b}/milestones/{ms_id}",
                json={"label": "Cross-project attempt"},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 404
        finally:
            _clear_overrides(migrated_client.app)

    def test_patch_clearing_only_position_returns_422(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """Setting position_frame_index=null on a frame-only milestone must
        return 422."""
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-patch-422"
            )
            # Milestone has frame_index only; no timestamp.
            ms_id = _seed_milestone(
                migrated_factory, project_id, frame_index=5, timestamp=None
            )

            # Clearing the only position field leaves neither frame nor timestamp.
            resp = migrated_client.patch(
                f"/api/v1/projects/{project_id}/milestones/{ms_id}",
                json={"position_frame_index": None},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 422
        finally:
            _clear_overrides(migrated_client.app)

    def test_patch_omitting_position_preserves_existing_position(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """A PATCH that omits both position fields must not change the position."""
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-patch-preserve"
            )
            ms_id = _seed_milestone(migrated_factory, project_id, frame_index=7)

            resp = migrated_client.patch(
                f"/api/v1/projects/{project_id}/milestones/{ms_id}",
                json={"label": "Label only change"},
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["position_frame_index"] == 7
        finally:
            _clear_overrides(migrated_client.app)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/projects/{id}/milestones
# ---------------------------------------------------------------------------


class TestListMilestones:
    def test_list_milestones_returns_in_creation_order(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-list-order"
            )
            _seed_milestone(migrated_factory, project_id, label="First", frame_index=1)
            _seed_milestone(migrated_factory, project_id, label="Second", frame_index=2)
            _seed_milestone(migrated_factory, project_id, label="Third", frame_index=3)

            resp = migrated_client.get(
                f"/api/v1/projects/{project_id}/milestones",
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 200
            labels = [m["label"] for m in resp.json()]
            assert labels == ["First", "Second", "Third"]
        finally:
            _clear_overrides(migrated_client.app)

    def test_list_milestones_returns_empty_list_for_project_with_none(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        try:
            project_id = _seed_project(
                migrated_factory, settings_no_autostart, name="ms-list-empty"
            )
            resp = migrated_client.get(
                f"/api/v1/projects/{project_id}/milestones",
                headers=_auth(cam_auth_token),
            )
            assert resp.status_code == 200
            assert resp.json() == []
        finally:
            _clear_overrides(migrated_client.app)
