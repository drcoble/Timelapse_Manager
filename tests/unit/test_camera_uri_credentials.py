"""No login inside a camera address or URI (#69 CAM-S2, CAM-S7).

Covers the write boundary (:func:`normalize_camera_uris` through
``create_camera`` / ``update_camera``), the idempotent startup pass over stored
rows, and the adapter-side stripping that stops a legacy URL putting a
preemptive ``Authorization: Basic`` on the wire.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from timelapse_manager.cameras.http_jpeg import http_get_image, strip_url_userinfo
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Event, User
from timelapse_manager.security import hash_password
from timelapse_manager.security.crypto import decrypt_credentials, encrypt_credentials
from timelapse_manager.services._base import Actor
from timelapse_manager.services.cameras import (
    CameraSpec,
    create_camera,
    normalize_camera_uris,
    normalize_stored_camera_uris,
    update_camera,
)
from timelapse_manager.services.errors import ValidationError
from timelapse_manager.web.routers._viewmodels import _camera_view

_STRONG_PW = "CorrectHorse42!"


@pytest.fixture()
def session(migrated_factory) -> Session:  # type: ignore[no-untyped-def]
    db = migrated_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def actor(session: Session, settings_no_autostart: Settings) -> Actor:
    admin = User(
        username="root",
        auth_source="local",
        password_hash=hash_password(_STRONG_PW, settings_no_autostart.auth),
        role="admin",
        enabled=True,
    )
    session.add(admin)
    session.flush()
    return Actor.of(admin)


def _own_login() -> dict[str, object] | None:
    return encrypt_credentials({"username": "own", "password": "own-pw"})


class TestNormalizeCameraUris:
    def test_userinfo_moves_into_the_credential_document(self) -> None:
        spec = normalize_camera_uris(
            CameraSpec(name="c", snapshot_uri="http://u:p%40ss@10.0.16.5/snap.jpg")
        )
        assert spec.snapshot_uri == "http://10.0.16.5/snap.jpg"
        assert spec.credentials is not None
        assert spec.credentials["username"] == "u"
        assert decrypt_credentials(spec.credentials) == {
            "username": "u",
            "password": "p@ss",
        }
        # The stored password is ciphertext, never the plaintext.
        assert spec.credentials["password"] != "p@ss"
        assert spec.credentials_inherit_default is False

    def test_address_and_stream_uri_are_covered_too(self) -> None:
        spec = normalize_camera_uris(
            CameraSpec(
                name="c",
                address="u:p@10.0.16.5",
                stream_uri="rtsp://u:p@10.0.16.5:554/x",
            )
        )
        assert spec.address == "10.0.16.5"
        assert spec.stream_uri == "rtsp://10.0.16.5:554/x"

    def test_a_password_with_an_at_sign_is_taken_whole(self) -> None:
        spec = normalize_camera_uris(
            CameraSpec(name="c", stream_uri="rtsp://u:p@ss@10.0.16.5/x")
        )
        assert spec.stream_uri == "rtsp://10.0.16.5/x"
        assert decrypt_credentials(spec.credentials) == {
            "username": "u",
            "password": "p@ss",
        }

    def test_existing_credentials_make_it_a_422(self) -> None:
        with pytest.raises(ValidationError) as caught:
            normalize_camera_uris(
                CameraSpec(
                    name="c",
                    snapshot_uri="http://u:p@10.0.16.5/snap.jpg",
                    credentials=_own_login(),
                )
            )
        assert "remove the user:pass@" in caught.value.detail
        assert "p@" not in caught.value.detail
        assert caught.value.field == "snapshot_uri"

    def test_two_different_logins_are_a_422(self) -> None:
        with pytest.raises(ValidationError):
            normalize_camera_uris(
                CameraSpec(
                    name="c",
                    snapshot_uri="http://a:1@10.0.16.5/s",
                    stream_uri="rtsp://b:2@10.0.16.5/x",
                )
            )

    @pytest.mark.parametrize(
        "username", ["bad\r\nX-Evil: 1", "bad\nname", "a\x00b", "a\x7fb"]
    )
    def test_control_characters_in_the_username_are_a_422(self, username: str) -> None:
        with pytest.raises(ValidationError) as caught:
            normalize_camera_uris(
                CameraSpec(
                    name="c",
                    credentials=encrypt_credentials(
                        {"username": username, "password": "x"}
                    ),
                )
            )
        assert caught.value.field == "username"

    def test_a_control_character_in_a_uri_username_is_a_422(self) -> None:
        with pytest.raises(ValidationError):
            normalize_camera_uris(
                CameraSpec(name="c", snapshot_uri="http://a%0d%0ab:p@10.0.16.5/s")
            )

    def test_a_value_httpx_would_read_differently_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            normalize_camera_uris(
                CameraSpec(name="c", snapshot_uri="http://u:p@10.0.16.5 /s")
            )

    def test_a_clean_spec_is_returned_unchanged(self) -> None:
        spec = CameraSpec(name="c", snapshot_uri="http://10.0.16.5/s?e=a@b")
        assert normalize_camera_uris(spec) is spec

    def test_an_edit_only_examines_the_fields_it_sets(self) -> None:
        spec = CameraSpec(
            name="c",
            snapshot_uri="http://u:p@10.0.16.5/s",
            fields_set=frozenset({"name"}),
        )
        assert normalize_camera_uris(spec, editing=True) is spec

    def test_an_edit_respects_the_stored_credentials(self) -> None:
        spec = CameraSpec(
            snapshot_uri="http://u:p@10.0.16.5/s",
            fields_set=frozenset({"snapshot_uri"}),
        )
        with pytest.raises(ValidationError):
            normalize_camera_uris(spec, editing=True, stored_credentials=_own_login())
        moved = normalize_camera_uris(spec, editing=True, stored_credentials=None)
        assert {"credentials", "credentials_inherit_default"} <= moved.fields_set


class TestServiceWrites:
    def test_create_moves_the_login(self, session: Session, actor: Actor) -> None:
        camera = create_camera(
            session,
            actor,
            CameraSpec(name="c", snapshot_uri="http://u:p@10.0.16.5/s"),
        ).value
        assert camera.snapshot_uri == "http://10.0.16.5/s"
        assert decrypt_credentials(camera.credentials) == {
            "username": "u",
            "password": "p",
        }
        assert camera.credentials_inherit_default is False

    def test_create_with_a_login_and_credentials_is_a_422(
        self, session: Session, actor: Actor
    ) -> None:
        with pytest.raises(ValidationError):
            create_camera(
                session,
                actor,
                CameraSpec(
                    name="c",
                    snapshot_uri="http://u:p@10.0.16.5/s",
                    credentials=_own_login(),
                ),
            )
        assert session.scalar(select(func.count()).select_from(Camera)) == 0

    def test_update_checks_the_stored_credentials(
        self, session: Session, actor: Actor
    ) -> None:
        camera = create_camera(
            session, actor, CameraSpec(name="c", credentials=_own_login())
        ).value
        with pytest.raises(ValidationError):
            update_camera(
                session,
                actor,
                camera.id,
                CameraSpec(
                    snapshot_uri="http://u:p@10.0.16.5/s",
                    fields_set=frozenset({"snapshot_uri"}),
                ),
            )

    def test_update_moves_into_a_camera_without_credentials(
        self, session: Session, actor: Actor
    ) -> None:
        camera = create_camera(session, actor, CameraSpec(name="c")).value
        update_camera(
            session,
            actor,
            camera.id,
            CameraSpec(
                snapshot_uri="http://u:p@10.0.16.5/s",
                fields_set=frozenset({"snapshot_uri"}),
            ),
        )
        assert camera.snapshot_uri == "http://10.0.16.5/s"
        assert decrypt_credentials(camera.credentials) == {
            "username": "u",
            "password": "p",
        }


def _legacy_camera(
    session: Session, name: str, *, own: bool = False, **fields: str | None
) -> Camera:
    camera = Camera(
        name=name,
        protocol="http",
        credentials=_own_login() if own else None,
        credentials_inherit_default=True,
        **fields,
    )
    session.add(camera)
    session.flush()
    return camera


class TestStartupPass:
    def test_moves_the_login_of_a_camera_without_credentials(
        self, session: Session
    ) -> None:
        camera = _legacy_camera(session, "a", snapshot_uri="http://u:p@10.0.16.5/s")
        assert normalize_stored_camera_uris(session) == 1
        assert camera.snapshot_uri == "http://10.0.16.5/s"
        assert decrypt_credentials(camera.credentials) == {
            "username": "u",
            "password": "p",
        }
        assert camera.credentials_inherit_default is False

    def test_keeps_both_and_warns_once_when_the_camera_has_credentials(
        self, session: Session
    ) -> None:
        camera = _legacy_camera(
            session, "b", own=True, snapshot_uri="http://u:p@10.0.16.5/s"
        )
        assert normalize_stored_camera_uris(session) == 0
        assert normalize_stored_camera_uris(session) == 0
        assert camera.snapshot_uri == "http://u:p@10.0.16.5/s"
        assert decrypt_credentials(camera.credentials)["username"] == "own"  # type: ignore[index]
        warnings = list(
            session.execute(
                select(Event).where(Event.scope == "camera", Event.level == "warning")
            ).scalars()
        )
        assert len(warnings) == 1
        # The event names the camera and never a value from the URI.
        assert str(camera.id) in warnings[0].message
        assert "u:p" not in warnings[0].message

    def test_a_second_run_changes_nothing(self, session: Session) -> None:
        _legacy_camera(session, "a", snapshot_uri="http://u:p@10.0.16.5/s")
        assert normalize_stored_camera_uris(session) == 1
        assert normalize_stored_camera_uris(session) == 0

    def test_clean_rows_are_untouched(self, session: Session) -> None:
        camera = _legacy_camera(session, "c", snapshot_uri="http://10.0.16.5/s")
        assert normalize_stored_camera_uris(session) == 0
        assert camera.credentials is None
        assert camera.credentials_inherit_default is True

    def test_a_failing_row_is_skipped_and_logged_without_values(
        self,
        session: Session,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bad = _legacy_camera(
            session, "bad", snapshot_uri="http://u:secretpw@10.0.16.5/s"
        )
        good = _legacy_camera(session, "good", stream_uri="rtsp://v:w@10.0.16.6/x")
        real = encrypt_credentials

        def flaky(document):  # type: ignore[no-untyped-def]
            if document and document.get("username") == "u":
                raise RuntimeError("key provider unavailable: u:secretpw")
            return real(document)

        monkeypatch.setattr(
            "timelapse_manager.services.cameras.encrypt_credentials", flaky
        )
        with caplog.at_level(logging.WARNING):
            assert normalize_stored_camera_uris(session) == 1
        assert bad.snapshot_uri == "http://u:secretpw@10.0.16.5/s"
        assert bad.credentials is None
        assert good.stream_uri == "rtsp://10.0.16.6/x"
        assert str(bad.id) in caplog.text
        assert "secretpw" not in caplog.text


class TestAdapterStripsUserinfo:
    def test_strip_url_userinfo(self) -> None:
        assert strip_url_userinfo("https://u:p@10.0.16.5:8443/s?x=1") == (
            "https://10.0.16.5:8443/s?x=1"
        )
        assert strip_url_userinfo("http://10.0.16.5/s") == "http://10.0.16.5/s"

    @pytest.mark.asyncio()
    async def test_a_legacy_url_sends_no_preemptive_authorization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.conftest import make_jpeg

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=make_jpeg(64, 48))

        monkeypatch.setattr(
            "timelapse_manager.cameras.http_jpeg._guard_snapshot_url", AsyncMock()
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await http_get_image(
                client, "http://u:p@10.0.16.5/s", None, 5.0, max_bytes=100_000
            )
        assert len(seen) == 1
        assert "authorization" not in seen[0].headers
        assert seen[0].url.userinfo == b""

    @pytest.mark.asyncio()
    async def test_the_error_names_the_stripped_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from timelapse_manager.cameras.base import OtherCaptureError

        monkeypatch.setattr(
            "timelapse_manager.cameras.http_jpeg._guard_snapshot_url", AsyncMock()
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(404))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(OtherCaptureError) as caught:
                await http_get_image(
                    client, "http://u:p@10.0.16.5/s", None, 5.0, max_bytes=100_000
                )
        assert "u:p" not in str(caught.value)


class TestViewsNeverShowUserinfo:
    def test_the_camera_form_view_strips_userinfo(self) -> None:
        camera = Camera(
            id=1,
            name="c",
            protocol="http",
            address="u:p@10.0.16.5",
            snapshot_uri="http://u:p@10.0.16.5/s",
            stream_uri="rtsp://u:p@10.0.16.5/x",
        )
        view = _camera_view(camera)
        assert (view.address, view.snapshot_uri, view.stream_uri) == (
            "10.0.16.5",
            "http://10.0.16.5/s",
            "rtsp://10.0.16.5/x",
        )


class TestLegacyAddressNeverReachesTheWire:
    def test_adapters_drop_userinfo_from_the_address(self) -> None:
        from timelapse_manager.cameras.onvif import OnvifAdapter
        from timelapse_manager.cameras.vapix import VapixAdapter

        client = httpx.AsyncClient()
        vapix = VapixAdapter(client, "https://u:p@10.0.16.5:8443")
        onvif = OnvifAdapter(client, "u:p@10.0.16.5")
        assert vapix._address == "https://10.0.16.5:8443"  # noqa: SLF001
        assert onvif._address == "10.0.16.5"  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_a_soap_call_sends_no_preemptive_authorization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from timelapse_manager.cameras.onvif import OnvifAdapter, _device_url

        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                ssrf=SimpleNamespace(allowed_private_subnets=["10.0.0.0/8"])
            )
        )
        monkeypatch.setattr("timelapse_manager.runtime.get_context", lambda: ctx)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="<x/>")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = OnvifAdapter(client, "http://u:secretpw@10.0.16.5")
        await adapter._soap_call(_device_url(adapter._address), "<x/>")  # noqa: SLF001
        assert len(seen) == 1
        assert "authorization" not in seen[0].headers
        assert seen[0].url.userinfo == b""


class TestRtspErrorIsRedacted:
    @pytest.mark.asyncio()
    async def test_ffmpeg_stderr_naming_the_url_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from timelapse_manager.cameras.base import CaptureError
        from timelapse_manager.cameras.rtsp import RtspAdapter

        class _Process:
            returncode = 1

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b"rtsp://admin:hunter2@10.0.16.5/x: Connection refused\n"

        async def spawn(*_args: object, **_kwargs: object) -> _Process:
            return _Process()

        async def allow(_url: str) -> None:
            return None

        monkeypatch.setattr(
            "timelapse_manager.cameras.rtsp.asyncio.create_subprocess_exec", spawn
        )
        monkeypatch.setattr("timelapse_manager.cameras.rtsp._guard_stream_url", allow)
        adapter = RtspAdapter("rtsp://10.0.16.5/x", ("admin", "hunter2"))
        with pytest.raises(CaptureError) as caught:
            await adapter.capture()
        assert "hunter2" not in str(caught.value)
        assert "Connection refused" in str(caught.value)


class TestNoRedirectCarriesTheLogin:
    @pytest.mark.asyncio()
    async def test_a_redirect_to_another_host_is_not_followed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 30x never bounces the login to another origin."""
        from timelapse_manager.cameras.base import OtherCaptureError
        from timelapse_manager.cameras.clients import CameraHttpClients
        from timelapse_manager.config.settings import CaptureSettings

        monkeypatch.setattr(
            "timelapse_manager.cameras.http_jpeg._guard_snapshot_url", AsyncMock()
        )
        # The production client is built without redirect following...
        assert CameraHttpClients(CaptureSettings()).capture.follow_redirects is False

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(302, headers={"Location": "http://10.9.9.9/steal"})

        # ...and so is any client the adapter is given: nothing reaches 10.9.9.9.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OtherCaptureError):
                await http_get_image(
                    client,
                    "http://10.0.16.5/s",
                    ("u", "secret-pw"),
                    5.0,
                    max_bytes=100_000,
                )
        assert {request.url.host for request in seen} == {"10.0.16.5"}
