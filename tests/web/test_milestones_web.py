"""Web route tests for the milestone CRUD surface.

Covers:
- operator creates milestone by frame index (table fragment returned)
- operator creates milestone by timestamp (table fragment returned)
- validation error when no position type given (inline error at HTTP 200)
- edit: operator edits label and position
- delete: operator deletes a milestone
- viewer gets 403 on all mutations
- router ordering: POST /projects/{id}/milestones reaches milestone handler
- attribution: Event written with actor_user_id = operator's user id
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Event, Milestone, Project, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_project(name: str = "ms-project") -> tuple[int, int]:
    """Insert a camera and project; return (camera_id, project_id).

    Works against the running app's context so it is suitable for all
    web-client fixture patterns.
    """
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        cam_id = cam.id
        proj = Project(
            camera_id=cam_id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=f"/tmp/{name}",
        )
        db.add(proj)
        db.flush()
        project_id = proj.id
    return cam_id, project_id


def _operator_user_id() -> int:
    """Return the id of the 'operator' user from the running context."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        user = db.query(User).filter(User.username == "operator").first()
        assert user is not None, "operator user not found"
        return user.id


def _milestone_count(project_id: int) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Milestone).filter(Milestone.project_id == project_id).count()


def _first_milestone(project_id: int) -> Milestone | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        row = (
            db.query(Milestone)
            .filter(Milestone.project_id == project_id)
            .order_by(Milestone.id)
            .first()
        )
        if row is None:
            return None
        # Detach from session so attributes are accessible after close.
        db.expunge(row)
        return row


def _events_for_project(project_id: int, message_fragment: str) -> list[Event]:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        rows = (
            db.query(Event)
            .filter(
                Event.scope == "project",
                Event.scope_id == project_id,
                Event.message.contains(message_fragment),
            )
            .all()
        )
        for row in rows:
            db.expunge(row)
        return rows


# ---------------------------------------------------------------------------
# TestMilestoneCreateByFrameIndex
# ---------------------------------------------------------------------------


class TestMilestoneCreateByFrameIndex:
    def test_operator_create_by_frame_index_returns_200(
        self, operator_client: TestClient
    ) -> None:
        _, project_id = _seed_project("ms-create-frame")
        csrf = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "First pour",
                "milestone_position_type": "frame",
                "milestone_frame_index": "0",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200

    def test_operator_create_by_frame_index_persists_milestone(
        self, operator_client: TestClient
    ) -> None:
        _, project_id = _seed_project("ms-create-frame-persist")
        csrf = csrf_of(operator_client, "/")

        operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "First pour",
                "milestone_position_type": "frame",
                "milestone_frame_index": "5",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        ms = _first_milestone(project_id)
        assert ms is not None
        assert ms.label == "First pour"
        assert ms.position_frame_index == 5
        assert ms.position_timestamp is None

    def test_operator_create_response_contains_table_fragment_id(
        self, operator_client: TestClient
    ) -> None:
        """On success the route returns the milestone table fragment."""
        _, project_id = _seed_project("ms-create-frame-frag")
        csrf = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "Roof done",
                "milestone_position_type": "frame",
                "milestone_frame_index": "10",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        # The fragment rendered at HTTP 200 must contain the label we submitted.
        assert resp.status_code == 200
        assert "Roof done" in resp.text


# ---------------------------------------------------------------------------
# TestMilestoneCreateByTimestamp
# ---------------------------------------------------------------------------


class TestMilestoneCreateByTimestamp:
    def test_operator_create_by_timestamp_persists_milestone(
        self, operator_client: TestClient
    ) -> None:
        _, project_id = _seed_project("ms-create-ts")
        csrf = csrf_of(operator_client, "/")

        operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "Delivery day",
                "milestone_position_type": "time",
                "milestone_timestamp": "2026-03-15T09:00",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        ms = _first_milestone(project_id)
        assert ms is not None
        assert ms.label == "Delivery day"
        assert ms.position_frame_index is None
        assert ms.position_timestamp is not None


# ---------------------------------------------------------------------------
# TestMilestoneCreateValidationErrors
# ---------------------------------------------------------------------------


class TestMilestoneCreateValidationErrors:
    def test_no_position_type_returns_inline_error_at_200(
        self, operator_client: TestClient
    ) -> None:
        """Missing position_type → validation error fragment at HTTP 200."""
        _, project_id = _seed_project("ms-val-no-type")
        csrf = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "Unlabelled",
                "milestone_position_type": "",  # no position type chosen
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        # HTMX idiom: validation error is always HTTP 200 so the swap fires.
        assert resp.status_code == 200
        # No milestone must have been created.
        assert _milestone_count(project_id) == 0

    def test_no_label_returns_inline_error_at_200(
        self, operator_client: TestClient
    ) -> None:
        _, project_id = _seed_project("ms-val-no-label")
        csrf = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "",
                "milestone_position_type": "frame",
                "milestone_frame_index": "3",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        assert resp.status_code == 200
        assert _milestone_count(project_id) == 0

    def test_negative_frame_index_returns_inline_error_at_200(
        self, operator_client: TestClient
    ) -> None:
        _, project_id = _seed_project("ms-val-neg-frame")
        csrf = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "Bad index",
                "milestone_position_type": "frame",
                "milestone_frame_index": "-1",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        assert resp.status_code == 200
        assert _milestone_count(project_id) == 0


# ---------------------------------------------------------------------------
# TestMilestoneEdit
# ---------------------------------------------------------------------------


class TestMilestoneEdit:
    def _create_milestone(self, client: TestClient, project_id: int) -> int:
        csrf = csrf_of(client, "/")
        client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "Original label",
                "milestone_position_type": "frame",
                "milestone_frame_index": "1",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        ms = _first_milestone(project_id)
        assert ms is not None
        return ms.id

    def test_edit_updates_label(self, operator_client: TestClient) -> None:
        _, project_id = _seed_project("ms-edit-label")
        ms_id = self._create_milestone(operator_client, project_id)
        csrf = csrf_of(operator_client, "/")

        operator_client.post(
            f"/projects/{project_id}/milestones/{ms_id}/edit",
            data={
                "milestone_label": "Revised label",
                "milestone_position_type": "frame",
                "milestone_frame_index": "1",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            ms = db.get(Milestone, ms_id)
            assert ms is not None
            assert ms.label == "Revised label"

    def test_edit_changes_position_type(self, operator_client: TestClient) -> None:
        _, project_id = _seed_project("ms-edit-pos")
        ms_id = self._create_milestone(operator_client, project_id)
        csrf = csrf_of(operator_client, "/")

        # Switch from frame-indexed to timestamp.
        operator_client.post(
            f"/projects/{project_id}/milestones/{ms_id}/edit",
            data={
                "milestone_label": "Revised label",
                "milestone_position_type": "time",
                "milestone_timestamp": "2026-06-01T12:00",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            ms = db.get(Milestone, ms_id)
            assert ms is not None
            assert ms.position_frame_index is None
            assert ms.position_timestamp is not None

    def test_edit_returns_200(self, operator_client: TestClient) -> None:
        _, project_id = _seed_project("ms-edit-200")
        ms_id = self._create_milestone(operator_client, project_id)
        csrf = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/milestones/{ms_id}/edit",
            data={
                "milestone_label": "Updated",
                "milestone_position_type": "frame",
                "milestone_frame_index": "2",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestMilestoneDelete
# ---------------------------------------------------------------------------


class TestMilestoneDelete:
    def _create_milestone(self, client: TestClient, project_id: int) -> int:
        csrf = csrf_of(client, "/")
        client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "To delete",
                "milestone_position_type": "frame",
                "milestone_frame_index": "0",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        ms = _first_milestone(project_id)
        assert ms is not None
        return ms.id

    def test_delete_removes_milestone(self, operator_client: TestClient) -> None:
        _, project_id = _seed_project("ms-delete")
        ms_id = self._create_milestone(operator_client, project_id)
        csrf = csrf_of(operator_client, "/")

        operator_client.post(
            f"/projects/{project_id}/milestones/{ms_id}/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            assert db.get(Milestone, ms_id) is None

    def test_delete_returns_200(self, operator_client: TestClient) -> None:
        _, project_id = _seed_project("ms-delete-200")
        ms_id = self._create_milestone(operator_client, project_id)
        csrf = csrf_of(operator_client, "/")

        resp = operator_client.post(
            f"/projects/{project_id}/milestones/{ms_id}/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestMilestoneViewerForbidden
# ---------------------------------------------------------------------------


class TestMilestoneViewerForbidden:
    def test_viewer_cannot_create_milestone(self, viewer_client: TestClient) -> None:
        _, project_id = _seed_project("ms-viewer-create")
        csrf = csrf_of(viewer_client, "/")

        resp = viewer_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "Viewer attempt",
                "milestone_position_type": "frame",
                "milestone_frame_index": "0",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 403

    def test_viewer_cannot_edit_milestone(self, viewer_client: TestClient) -> None:
        """A viewer trying to edit a milestone (any id) gets 403."""
        _, project_id = _seed_project("ms-viewer-edit")
        # Seed a milestone directly via DB (no operator session needed in this client).
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            ms = Milestone(
                project_id=project_id,
                user_id=1,  # sentinel; FK satisfied via ensure_sentinel_admin in create
                label="Pre-existing milestone",
                position_frame_index=1,
            )
            db.add(ms)
            db.flush()
            ms_id = ms.id

        csrf_v = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            f"/projects/{project_id}/milestones/{ms_id}/edit",
            data={
                "milestone_label": "Viewer overwrite",
                "milestone_position_type": "frame",
                "milestone_frame_index": "1",
                "csrf_token": csrf_v,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 403

    def test_viewer_cannot_delete_milestone(self, viewer_client: TestClient) -> None:
        """A viewer trying to delete a milestone (any id) gets 403."""
        _, project_id = _seed_project("ms-viewer-delete")
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            ms = Milestone(
                project_id=project_id,
                user_id=1,
                label="To delete",
                position_frame_index=0,
            )
            db.add(ms)
            db.flush()
            ms_id = ms.id

        csrf_v = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            f"/projects/{project_id}/milestones/{ms_id}/delete",
            data={"csrf_token": csrf_v},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestMilestoneAttribution
# ---------------------------------------------------------------------------


class TestMilestoneAttribution:
    def test_create_writes_event_with_operator_user_id(
        self, operator_client: TestClient
    ) -> None:
        _, project_id = _seed_project("ms-attr")
        op_user_id = _operator_user_id()
        csrf = csrf_of(operator_client, "/")

        operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "Attributed",
                "milestone_position_type": "frame",
                "milestone_frame_index": "3",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        events = _events_for_project(project_id, "created")
        assert len(events) >= 1
        assert any(e.actor_user_id == op_user_id for e in events)

    def test_create_sets_milestone_user_id_to_operator(
        self, operator_client: TestClient
    ) -> None:
        _, project_id = _seed_project("ms-attr-uid")
        op_user_id = _operator_user_id()
        csrf = csrf_of(operator_client, "/")

        operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "Mine",
                "milestone_position_type": "frame",
                "milestone_frame_index": "0",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        ms = _first_milestone(project_id)
        assert ms is not None
        assert ms.user_id == op_user_id


# ---------------------------------------------------------------------------
# TestRouterOrdering
# ---------------------------------------------------------------------------


class TestRouterOrdering:
    """POST /projects/{id}/milestones must reach the milestone handler, not
    the single-segment catch-all action handler in the projects router."""

    def test_post_milestones_path_is_not_captured_by_project_action_catchall(
        self, operator_client: TestClient
    ) -> None:
        """'milestones' is not a valid project action; if routing were wrong,
        this would 404 (unknown action). The milestone handler creates the row
        or returns a validation error — either outcome at HTTP 200 proves the
        correct handler ran."""
        _, project_id = _seed_project("ms-router-order")
        csrf = csrf_of(operator_client, "/")

        # Send a form that will produce a validation error (no label) so we
        # do not need to assert on DB state — just the response status.
        resp = operator_client.post(
            f"/projects/{project_id}/milestones",
            data={
                "milestone_label": "",  # triggers validation error in milestone handler
                "milestone_position_type": "frame",
                "milestone_frame_index": "0",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        # If the catch-all had fired, 'milestones' would not be a known action
        # and the response would be 404. The milestone handler returns 200 (form
        # error fragment) or any non-404 success status.
        assert resp.status_code != 404
