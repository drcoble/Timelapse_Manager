"""Camera add/edit form order, the default-credentials checkbox and its guard (#318)."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, CameraDefaultCredentials
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.crypto import encrypt_credentials

_ADDRESS = "192.168.1.50"
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}

# The order a user fills the form in, as markers unique to each control.
_ORDER = (
    'name="address"',
    'name="allow_basic_over_http"',
    "Query camera",
    'name="name"',
    'name="device_hostname"',
    'name="protocol"',
    "Advanced",
)


def _set_defaults(*, enabled: bool = True, username: str = "shared") -> None:
    with session_scope(get_context().session_factory) as db:
        row = db.get(CameraDefaultCredentials, 1)
        if row is None:
            row = CameraDefaultCredentials(id=1)
            db.add(row)
        row.enabled = enabled
        row.username = username
        row.password = None


def _seed_camera(*, inherit: bool, name: str = "order-cam") -> int:
    with session_scope(get_context().session_factory) as db:
        cam = Camera(
            name=name,
            address=_ADDRESS,
            protocol="vapix",
            credentials=encrypt_credentials(None),
            credentials_inherit_default=inherit,
        )
        db.add(cam)
        db.flush()
        return cam.id


def _stored(camera_id: int) -> Camera:
    with session_scope(get_context().session_factory) as db:
        cam = db.get(Camera, camera_id)
        assert cam is not None
        db.expunge(cam)
        return cam


def _checkbox(text: str) -> str | None:
    m = re.search(r'<input[^>]*name="credentials_inherit_default"[^>]*>', text, re.S)
    return m.group(0) if m else None


def _assert_in_order(text: str, markers: tuple[str, ...]) -> None:
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions), dict(zip(markers, positions, strict=True))


class TestFieldOrder:
    def test_add_form_order_with_defaults(self, admin_client: TestClient) -> None:
        _set_defaults()
        text = admin_client.get("/cameras/add-form").text
        _assert_in_order(
            text,
            (
                'name="address"',
                'name="credentials_inherit_default"',
                'name="username"',
                'name="password"',
                *_ORDER[1:],
            ),
        )

    def test_add_form_order_without_defaults(self, admin_client: TestClient) -> None:
        text = admin_client.get("/cameras/add-form").text
        _assert_in_order(
            text,
            ('name="address"', 'name="username"', 'name="password"', *_ORDER[1:]),
        )

    def test_edit_form_uses_the_same_order(self, admin_client: TestClient) -> None:
        _set_defaults()
        camera_id = _seed_camera(inherit=False)
        text = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        _assert_in_order(
            text,
            (
                'name="address"',
                'name="credentials_inherit_default"',
                'name="username"',
                'name="password"',
                *_ORDER[1:],
                "Latitude",
            ),
        )

    def test_certificate_pin_note_sits_between_protocol_and_advanced(
        self, admin_client: TestClient
    ) -> None:
        text = admin_client.get("/cameras/add-form").text
        _assert_in_order(text, ('name="protocol"', "save the camera first", "Advanced"))

    def test_edit_tls_section_sits_between_protocol_and_advanced(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(inherit=False)
        text = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert text.index('name="protocol"') < text.index("<details")

    def test_query_includes_the_whole_form(self, admin_client: TestClient) -> None:
        # hx-include="closest form" carries address, credentials, the Basic opt-in
        # and probe_camera_id, all of which now sit before the button.
        camera_id = _seed_camera(inherit=False)
        text = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        button = text.index('hx-post="/cameras/query"')
        assert 'hx-include="closest form"' in text[button : button + 400]
        for marker in (
            'name="address"',
            'name="username"',
            'name="password"',
            'name="allow_basic_over_http"',
            'name="probe_camera_id"',
        ):
            assert text.index(marker) < button, marker

    def test_every_accept_all_target_still_exists(
        self, admin_client: TestClient
    ) -> None:
        text = admin_client.get("/cameras/add-form").text
        for field in (
            "name",
            "device_hostname",
            "protocol",
            "snapshot_uri",
            "stream_uri",
            "latitude",
            "longitude",
            "geo_source",
        ):
            assert f'name="{field}"' in text, field


class TestButtonLabel:
    def test_add_button_says_add_camera(self, admin_client: TestClient) -> None:
        text = admin_client.get("/cameras/add-form").text
        assert re.search(
            r"<button type=\"submit\"[^>]*>\s*Add camera\s*</button>", text
        )
        assert "create camera" not in text.lower()

    def test_edit_button_says_save_changes(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(inherit=False)
        text = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert re.search(
            r"<button type=\"submit\"[^>]*>\s*Save changes\s*</button>", text
        )

    def test_cameras_page_has_no_create_camera_text(
        self, admin_client: TestClient
    ) -> None:
        assert "create camera" not in admin_client.get("/cameras").text.lower()


class TestInheritCheckboxVisibility:
    def test_checkbox_shown_ticked_when_defaults_exist(
        self, admin_client: TestClient
    ) -> None:
        _set_defaults()
        text = admin_client.get("/cameras/add-form").text
        box = _checkbox(text)
        assert box is not None and "checked" in box
        assert "shared" in text  # the default's username, never a password

    def test_no_checkbox_without_defaults(self, admin_client: TestClient) -> None:
        text = admin_client.get("/cameras/add-form").text
        assert _checkbox(text) is None
        assert "No default credentials are configured" not in text
        assert 'name="username"' in text and 'name="password"' in text

    def test_no_checkbox_when_defaults_disabled(self, admin_client: TestClient) -> None:
        _set_defaults(enabled=False)
        assert _checkbox(admin_client.get("/cameras/add-form").text) is None

    def test_credentials_are_not_hidden_or_disabled_by_markup(
        self, admin_client: TestClient
    ) -> None:
        # Without JS both stay visible: only the creds-inherit action hides them.
        _set_defaults()
        text = admin_client.get("/cameras/add-form").text
        creds = re.search(r'<div[^>]*id="cam-new-creds"[^>]*>', text)
        assert creds is not None and "hidden" not in creds.group(0)
        for field in ("username", "password"):
            tag = re.search(rf'<input[^>]*name="{field}"[^>]*>', text, re.S)
            assert tag is not None and "disabled" not in tag.group(0)

    def test_edit_after_defaults_removed_explains_and_shows_credentials(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(inherit=True)
        text = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert _checkbox(text) is None
        assert "configured any more" in " ".join(text.split())
        assert 'name="username"' in text

    def test_edit_of_a_non_inheriting_camera_has_no_stale_notice(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(inherit=False)
        text = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert "configured any more" not in " ".join(text.split())


class TestServerGuard:
    def _post(self, client: TestClient, url: str, data: dict[str, str]) -> None:
        csrf = csrf_of(client, "/cameras")
        resp = client.post(
            url,
            data={**data, "csrf_token": csrf},
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

    def test_create_ignores_flag_without_defaults(
        self, admin_client: TestClient
    ) -> None:
        self._post(
            admin_client,
            "/cameras",
            {"name": "crafted", "credentials_inherit_default": "on"},
        )
        with session_scope(get_context().session_factory) as db:
            cam = db.query(Camera).filter_by(name="crafted").one()
            assert cam.credentials_inherit_default is False

    def test_create_ignores_flag_when_defaults_disabled(
        self, admin_client: TestClient
    ) -> None:
        _set_defaults(enabled=False)
        self._post(
            admin_client,
            "/cameras",
            {"name": "crafted-off", "credentials_inherit_default": "on"},
        )
        with session_scope(get_context().session_factory) as db:
            cam = db.query(Camera).filter_by(name="crafted-off").one()
            assert cam.credentials_inherit_default is False

    def test_create_honours_flag_with_defaults(self, admin_client: TestClient) -> None:
        _set_defaults()
        self._post(
            admin_client,
            "/cameras",
            {"name": "legit", "credentials_inherit_default": "on"},
        )
        with session_scope(get_context().session_factory) as db:
            cam = db.query(Camera).filter_by(name="legit").one()
            assert cam.credentials_inherit_default is True

    def test_create_keeps_typed_credentials_without_defaults(
        self, admin_client: TestClient
    ) -> None:
        # The crafted flag must not make the typed credentials vanish either.
        self._post(
            admin_client,
            "/cameras",
            {
                "name": "typed",
                "credentials_inherit_default": "on",
                "username": "bob",
                "password": "pw",
            },
        )
        with session_scope(get_context().session_factory) as db:
            cam = db.query(Camera).filter_by(name="typed").one()
            assert cam.credentials_inherit_default is False
            assert cam.credentials and cam.credentials.get("username") == "bob"

    def test_edit_ignores_posted_flag_without_defaults(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(inherit=False)
        self._post(
            admin_client,
            f"/cameras/{camera_id}/edit",
            {
                "name": "order-cam",
                "address": _ADDRESS,
                "credentials_inherit_default": "on",
            },
        )
        assert _stored(camera_id).credentials_inherit_default is False

    def test_edit_keeps_stored_inherit_when_defaults_gone(
        self, admin_client: TestClient
    ) -> None:
        # The setting is not silently dropped by a save that adds no credentials.
        camera_id = _seed_camera(inherit=True)
        self._post(
            admin_client,
            f"/cameras/{camera_id}/edit",
            {"name": "order-cam", "address": _ADDRESS},
        )
        assert _stored(camera_id).credentials_inherit_default is True

    def test_edit_with_credentials_turns_inherit_off_when_defaults_gone(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(inherit=True)
        self._post(
            admin_client,
            f"/cameras/{camera_id}/edit",
            {
                "name": "order-cam",
                "address": _ADDRESS,
                "username": "bob",
                "password": "pw",
            },
        )
        cam = _stored(camera_id)
        assert cam.credentials_inherit_default is False
        assert cam.credentials and cam.credentials.get("username") == "bob"

    def test_edit_follows_checkbox_with_defaults(
        self, admin_client: TestClient
    ) -> None:
        _set_defaults()
        camera_id = _seed_camera(inherit=True)
        self._post(
            admin_client,
            f"/cameras/{camera_id}/edit",
            {"name": "order-cam", "address": _ADDRESS},
        )
        assert _stored(camera_id).credentials_inherit_default is False
