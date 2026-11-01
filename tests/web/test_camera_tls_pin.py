"""The camera form's certificate pin: fetch, confirm, pin, unpin (#69 CAM-S1).

Pinning is opt-in and explicit. These tests drive the routes with only the
handshake (``fetch_peer_certificate``) replaced by a certificate generated for
the test, so the address checks, role gate, CSRF, fingerprint comparison, the
service and the rendered fragment all run for real. They pin:

* administrators only; CSRF required; nothing happens on a viewer's or an
  operator's request, and no camera is contacted;
* fetch stores nothing -- a pin exists only after the confirm step, and only
  for the fingerprint that was confirmed (any spelling), re-checked against
  what the camera presents at that moment;
* the address rules (https only, SSRF) apply before any connection;
* nothing from a handshake or a certificate leaks into an error;
* the page never contains the certificate PEM, and the new markup has no
  inline script or handler.
"""

from __future__ import annotations

import re
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import csrf_of
from tests.unit.camera_pin_support import make_self_signed, pem_of
from tests.web.test_camera_manage import _seed_camera
from timelapse_manager.cameras.tls import format_fingerprint, pem_fingerprint
from timelapse_manager.db.models import Camera, Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_FETCH = "timelapse_manager.web.routers.cameras.fetch_peer_certificate"
_ADDRESS = "https://192.168.1.50"
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture(autouse=True)
def _private_cameras_allowed(web_client: TestClient) -> None:
    """Let the administrator's allow-list admit the test cameras' 192.168/16."""
    get_context().settings.ssrf.allowed_private_subnets = ["192.168.0.0/16"]


def _der(tmp_path: Path, label: str = "cam") -> tuple[bytes, str]:
    """Return a generated certificate as ``(der, pem)``."""
    pem = pem_of(make_self_signed(tmp_path / label, label))
    return ssl.PEM_cert_to_DER_cert(pem), pem


@contextmanager
def _presenting(der: bytes) -> Iterator[AsyncMock]:
    with patch(_FETCH, new=AsyncMock(return_value=der)) as mock:
        yield mock


def _seed_http_camera(snapshot_uri: str) -> int:
    """Insert an HTTP-protocol camera that is reached through its snapshot URI."""
    with session_scope(get_context().session_factory) as db:
        camera = Camera(
            name=f"http-{abs(hash(snapshot_uri)) % 10_000}",
            protocol="http",
            snapshot_uri=snapshot_uri,
            credentials_inherit_default=False,
        )
        db.add(camera)
        db.flush()
        return camera.id


def _pin_of(camera_id: int) -> str | None:
    with session_scope(get_context().session_factory) as db:
        return db.get(Camera, camera_id).tls_pinned_cert_pem  # type: ignore[union-attr]


def _post(client: TestClient, path: str, **data: str):
    return client.post(
        path,
        data={"csrf_token": csrf_of(client, "/cameras"), **data},
        headers=_FORM,
    )


def _pinned_camera(tmp_path: Path, name: str = "pinned") -> tuple[int, str]:
    pem = pem_of(make_self_signed(tmp_path / name, name))
    camera_id = _seed_camera(name=name, address=_ADDRESS)
    with session_scope(get_context().session_factory) as db:
        db.get(Camera, camera_id).tls_pinned_cert_pem = pem  # type: ignore[union-attr]
    return camera_id, pem


class TestFetch:
    def test_shows_the_certificate_and_stores_nothing(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        der, pem = _der(tmp_path)
        camera_id = _seed_camera(name="fetch", address=_ADDRESS)
        with _presenting(der) as fetch:
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert resp.status_code == 200
        shown = format_fingerprint(pem_fingerprint(pem))
        assert shown in resp.text
        assert "AXIS cam" in resp.text
        assert "(self-signed)" in resp.text
        assert "Pin this certificate" in resp.text
        assert _pin_of(camera_id) is None
        # Dialled the validated address with no host name for an IP literal.
        args, kwargs = fetch.call_args
        assert args == ("192.168.1.50", 443)
        assert kwargs["server_hostname"] is None

    def test_uses_the_port_in_the_address(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_camera(name="port", address="https://192.168.1.50:8443")
        with _presenting(der) as fetch:
            _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert fetch.call_args.args == ("192.168.1.50", 8443)

    def test_an_http_camera_snapshot_uri_with_a_query_is_pinnable(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        # The normal Axis snapshot URI carries a query string; the host and port
        # come from the parse the HTTP client uses, not the VAPIX address parse.
        der, _ = _der(tmp_path)
        camera_id = _seed_http_camera(
            "https://192.168.1.50:8443/axis-cgi/jpg/image.cgi?resolution=640x480"
        )
        with _presenting(der) as fetch:
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert "Pin this certificate" in resp.text
        assert fetch.call_args.args == ("192.168.1.50", 8443)

    @pytest.mark.parametrize(
        "snapshot",
        [
            "https://192.168.1.50\\@10.0.0.1/snap.jpg",
            "https://user:pw@192.168.1.50/snap.jpg",
            "https://192.168.1.50 @10.0.0.1/snap.jpg",
        ],
    )
    def test_an_http_camera_with_an_ambiguous_snapshot_uri_is_refused(
        self, admin_client: TestClient, tmp_path: Path, snapshot: str
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_http_camera(snapshot)
        with _presenting(der) as fetch:
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert "ambiguous" in resp.text
        fetch.assert_not_called()

    @pytest.mark.parametrize(
        ("address", "protocol", "reason"),
        [
            ("http://192.168.1.50", "vapix", "https://"),
            ("192.168.1.50", "vapix", "https://"),
            ("https://192.168.1.50", "rtsp", "RTSP"),
            ("https://192.168.1.50", None, "protocol"),
        ],
    )
    def test_a_camera_that_cannot_hold_a_pin_is_refused_before_connecting(
        self,
        admin_client: TestClient,
        tmp_path: Path,
        address: str,
        protocol: str | None,
        reason: str,
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_camera(name="no-pin", address=address, protocol=protocol)
        with _presenting(der) as fetch:
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert resp.status_code == 200
        assert reason in resp.text
        assert "Pin this certificate" not in resp.text
        fetch.assert_not_called()

    def test_a_camera_with_a_stream_uri_is_refused_before_connecting(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_camera(name="has-stream", address=_ADDRESS)
        with session_scope(get_context().session_factory) as db:
            db.get(Camera, camera_id).stream_uri = "rtsp://192.168.1.50/s"  # type: ignore[union-attr]
        with _presenting(der) as fetch:
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert "stream URI" in resp.text
        assert "Pin this certificate" not in resp.text
        fetch.assert_not_called()

    def test_an_expired_certificate_is_shown_but_cannot_be_pinned(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        pem = pem_of(make_self_signed(tmp_path, "old", expired=True))
        der = ssl.PEM_cert_to_DER_cert(pem)
        camera_id = _seed_camera(name="expired", address=_ADDRESS)
        with _presenting(der):
            shown = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
            pinned = _post(
                admin_client,
                f"/cameras/{camera_id}/tls/pin",
                fingerprint=pem_fingerprint(pem),
            )
        assert "expired or not yet valid" in shown.text
        assert "Pin this certificate" not in shown.text
        assert "expired" in pinned.text
        assert _pin_of(camera_id) is None

    def test_a_denied_address_is_refused_before_connecting(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_camera(name="meta", address="https://169.254.169.254")
        with _presenting(der) as fetch:
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert "not allowed" in resp.text
        fetch.assert_not_called()

    @pytest.mark.parametrize(
        "address",
        [
            "https://192.168.1.50?x=1",
            "https://192.168.1.50#frag",
            "https://user:pw@192.168.1.50",
            "https://192.168.1.50\\@10.0.0.1",
        ],
    )
    def test_an_ambiguous_address_is_refused(
        self, admin_client: TestClient, tmp_path: Path, address: str
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_camera(name="ambiguous", address=address)
        with _presenting(der) as fetch:
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert "ambiguous" in resp.text
        fetch.assert_not_called()

    def test_a_handshake_failure_leaks_no_detail(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="down", address=_ADDRESS)
        secret = "[SSL: WRONG_VERSION_NUMBER] internal-detail-7c2f"
        with patch(_FETCH, new=AsyncMock(side_effect=OSError(secret))):
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert resp.status_code == 200
        assert "Could not read a certificate" in resp.text
        assert "internal-detail" not in resp.text
        assert "WRONG_VERSION" not in resp.text

    def test_a_timeout_is_the_same_fixed_message(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="slow", address=_ADDRESS)
        with patch(_FETCH, new=AsyncMock(side_effect=TimeoutError())):
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/fetch-certificate")
        assert "Could not read a certificate" in resp.text

    def test_a_missing_camera_is_a_404(self, admin_client: TestClient) -> None:
        resp = _post(admin_client, "/cameras/9999/tls/fetch-certificate")
        assert resp.status_code == 404


class TestPin:
    def _confirm(
        self, client: TestClient, camera_id: int, fingerprint: str, der: bytes
    ):
        with _presenting(der):
            return _post(
                client, f"/cameras/{camera_id}/tls/pin", fingerprint=fingerprint
            )

    @pytest.mark.parametrize(
        "spelling",
        [
            lambda fp: format_fingerprint(fp),
            lambda fp: fp,
            lambda fp: fp.upper(),
            lambda fp: " ".join(fp[i : i + 2] for i in range(0, 64, 2)),
        ],
        ids=["colon-upper", "lower-hex", "upper-hex", "space-separated"],
    )
    def test_pins_the_confirmed_fingerprint_in_any_spelling(
        self, admin_client: TestClient, tmp_path: Path, spelling
    ) -> None:
        der, pem = _der(tmp_path)
        camera_id = _seed_camera(name="pin-me", address=_ADDRESS)

        resp = self._confirm(
            admin_client, camera_id, spelling(pem_fingerprint(pem)), der
        )

        assert resp.status_code == 200
        assert "Certificate pinned." in resp.text
        assert _pin_of(camera_id) is not None
        assert pem_fingerprint(_pin_of(camera_id) or "") == pem_fingerprint(pem)
        assert format_fingerprint(pem_fingerprint(pem)) in resp.text
        with session_scope(get_context().session_factory) as db:
            audits = [
                e.message
                for e in db.scalars(select(Event).where(Event.scope_id == camera_id))
            ]
        assert any("pinned to certificate SHA-256" in m for m in audits)
        assert not any("BEGIN CERTIFICATE" in m for m in audits)

    def test_the_camera_presenting_something_else_now_is_not_pinned(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        _, confirmed_pem = _der(tmp_path, "a")
        other_der, other_pem = _der(tmp_path, "b")
        camera_id = _seed_camera(name="swapped", address=_ADDRESS)

        resp = self._confirm(
            admin_client, camera_id, pem_fingerprint(confirmed_pem), other_der
        )

        assert resp.status_code == 200
        assert "different certificate" in resp.text
        assert _pin_of(camera_id) is None
        # The new certificate is offered for a fresh confirmation instead.
        assert format_fingerprint(pem_fingerprint(other_pem)) in resp.text
        assert "Pin this certificate" in resp.text

    @pytest.mark.parametrize("bad", ["", "not-a-fingerprint", "ab" * 31])
    def test_a_malformed_fingerprint_pins_nothing(
        self, admin_client: TestClient, tmp_path: Path, bad: str
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_camera(name="bad-fp", address=_ADDRESS)
        resp = self._confirm(admin_client, camera_id, bad, der)
        assert resp.status_code == 200
        assert _pin_of(camera_id) is None

    def test_a_missing_fingerprint_field_pins_nothing(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_camera(name="no-fp", address=_ADDRESS)
        with _presenting(der):
            resp = _post(admin_client, f"/cameras/{camera_id}/tls/pin")
        assert resp.status_code == 200
        assert _pin_of(camera_id) is None

    def test_plain_http_camera_is_not_pinned(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        der, pem = _der(tmp_path)
        camera_id = _seed_camera(name="http-cam", address="http://192.168.1.50")
        resp = self._confirm(admin_client, camera_id, pem_fingerprint(pem), der)
        assert "https://" in resp.text
        assert _pin_of(camera_id) is None

    def test_a_failed_refetch_pins_nothing(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        _, pem = _der(tmp_path)
        camera_id = _seed_camera(name="gone", address=_ADDRESS)
        with patch(_FETCH, new=AsyncMock(side_effect=OSError("boom"))):
            resp = _post(
                admin_client,
                f"/cameras/{camera_id}/tls/pin",
                fingerprint=pem_fingerprint(pem),
            )
        assert "Could not read a certificate" in resp.text
        assert "boom" not in resp.text
        assert _pin_of(camera_id) is None

    def test_a_second_pin_replaces_the_first(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        camera_id, first = _pinned_camera(tmp_path, "first")
        der, pem = _der(tmp_path, "second")
        self._confirm(admin_client, camera_id, pem_fingerprint(pem), der)
        assert pem_fingerprint(_pin_of(camera_id) or "") == pem_fingerprint(pem)
        assert pem_fingerprint(first) != pem_fingerprint(pem)


class TestUnpin:
    def test_removes_the_pin(self, admin_client: TestClient, tmp_path: Path) -> None:
        camera_id, _ = _pinned_camera(tmp_path)
        resp = _post(admin_client, f"/cameras/{camera_id}/tls/unpin")
        assert resp.status_code == 200
        assert "Certificate pin removed." in resp.text
        assert _pin_of(camera_id) is None
        assert "Fetch the camera certificate" in resp.text

    def test_unpinning_an_unpinned_camera_is_harmless(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="none", address=_ADDRESS)
        assert _post(admin_client, f"/cameras/{camera_id}/tls/unpin").status_code == 200

    def test_a_missing_camera_is_a_404(self, admin_client: TestClient) -> None:
        assert _post(admin_client, "/cameras/9999/tls/unpin").status_code == 404


class TestAccessControl:
    @pytest.mark.parametrize("who", ["operator_client", "viewer_client"])
    def test_only_administrators_may_fetch_pin_or_unpin(
        self, who: str, request: pytest.FixtureRequest, tmp_path: Path
    ) -> None:
        client: TestClient = request.getfixturevalue(who)
        der, pem = _der(tmp_path)
        camera_id, existing = _pinned_camera(tmp_path, "locked")
        plain_id = _seed_camera(name="plain", address=_ADDRESS)
        with _presenting(der) as fetch:
            fetched = _post(client, f"/cameras/{plain_id}/tls/fetch-certificate")
            pinned = _post(
                client, f"/cameras/{plain_id}/tls/pin", fingerprint=pem_fingerprint(pem)
            )
            unpinned = _post(client, f"/cameras/{camera_id}/tls/unpin")
        assert {fetched.status_code, pinned.status_code, unpinned.status_code} == {403}
        fetch.assert_not_called()
        assert _pin_of(plain_id) is None
        assert _pin_of(camera_id) == existing

    def test_csrf_is_required(self, admin_client: TestClient, tmp_path: Path) -> None:
        der, pem = _der(tmp_path)
        camera_id, existing = _pinned_camera(tmp_path, "csrf")
        plain_id = _seed_camera(name="csrf-plain", address=_ADDRESS)
        with _presenting(der) as fetch:
            responses = [
                admin_client.post(
                    f"/cameras/{plain_id}/tls/fetch-certificate", headers=_FORM
                ),
                admin_client.post(
                    f"/cameras/{plain_id}/tls/pin",
                    data={"fingerprint": pem_fingerprint(pem)},
                    headers=_FORM,
                ),
                admin_client.post(f"/cameras/{camera_id}/tls/unpin", headers=_FORM),
            ]
        assert [r.status_code for r in responses] == [403, 403, 403]
        fetch.assert_not_called()
        assert _pin_of(plain_id) is None
        assert _pin_of(camera_id) == existing

    def test_the_token_may_travel_in_the_header_like_htmx_sends_it(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        camera_id, _ = _pinned_camera(tmp_path, "hdr")
        token = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/tls/unpin", headers={"X-CSRF-Token": token}
        )
        assert resp.status_code == 200
        assert _pin_of(camera_id) is None

    def test_the_routes_need_a_login(self, web_client: TestClient) -> None:
        for path in ("fetch-certificate", "pin", "unpin"):
            resp = web_client.post(
                f"/cameras/1/tls/{path}", headers=_FORM, follow_redirects=False
            )
            assert resp.status_code in (303, 401, 403)


class TestForm:
    def test_edit_form_has_the_admin_controls(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        camera_id = _seed_camera(name="form", address=_ADDRESS)
        html = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert f'hx-post="/cameras/{camera_id}/tls/fetch-certificate"' in html
        assert "Not pinned" in html

    def test_pinned_edit_form_shows_the_fingerprint_never_the_certificate(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        camera_id, pem = _pinned_camera(tmp_path)
        html = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert format_fingerprint(pem_fingerprint(pem)) in html
        assert f'hx-post="/cameras/{camera_id}/tls/unpin"' in html
        body = "".join(pem.splitlines()[1:-1])
        assert "BEGIN CERTIFICATE" not in html
        assert body[:40] not in html

    def test_an_operator_sees_the_state_but_not_the_buttons(
        self, operator_client: TestClient, tmp_path: Path
    ) -> None:
        camera_id, pem = _pinned_camera(tmp_path)
        html = operator_client.get(f"/cameras/{camera_id}/edit-form").text
        assert format_fingerprint(pem_fingerprint(pem)) in html
        assert "/tls/" not in html

    def test_buttons_send_only_their_own_parameters(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        # The section sits inside the edit form, whose password field must not
        # ride along with a pin request.
        camera_id, _ = _pinned_camera(tmp_path)
        html = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        for tag in re.findall(r"<button[^>]*/tls/[^>]*>", html, re.S):
            assert 'hx-params="none"' in tag or 'hx-params="fingerprint"' in tag
            assert 'type="button"' in tag

    def test_the_fetch_result_markup_has_no_inline_script_or_handlers(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        der, _ = _der(tmp_path)
        camera_id = _seed_camera(name="markup", address=_ADDRESS)
        with _presenting(der):
            html = _post(
                admin_client, f"/cameras/{camera_id}/tls/fetch-certificate"
            ).text
        assert "<script" not in html
        assert not re.search(r"\son[a-z]+\s*=", html)
        assert "javascript:" not in html

    def test_the_add_form_points_at_the_edit_form(
        self, admin_client: TestClient
    ) -> None:
        html = admin_client.get("/cameras/add-form").text
        assert "save the camera first" in html
        assert "/tls/" not in html

    def test_a_certificate_name_is_escaped(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "<img src=x onerror=alert(1)>")]
        )
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        camera_id = _seed_camera(name="xss", address=_ADDRESS)
        with _presenting(cert.public_bytes(serialization.Encoding.DER)):
            html = _post(
                admin_client, f"/cameras/{camera_id}/tls/fetch-certificate"
            ).text
        assert "<img" not in html
        assert "&lt;img" in html


class TestEditKeepsTheCamera:
    def _edit(self, client: TestClient, camera_id: int, **fields: str):
        return _post(
            client,
            f"/cameras/{camera_id}/edit",
            name=fields.pop("name", "pinned"),
            protocol=fields.pop("protocol", "vapix"),
            address=fields.pop("address", _ADDRESS),
            **fields,
        )

    def test_a_normal_edit_keeps_the_pin(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        camera_id, pem = _pinned_camera(tmp_path)
        resp = self._edit(admin_client, camera_id, name="renamed")
        assert resp.status_code == 200
        assert _pin_of(camera_id) == pem

    def test_an_edit_to_plain_http_is_refused_while_pinned(
        self, admin_client: TestClient, tmp_path: Path
    ) -> None:
        camera_id, pem = _pinned_camera(tmp_path)
        resp = self._edit(admin_client, camera_id, address="http://192.168.1.50")
        assert resp.status_code == 200
        assert "https://" in resp.text
        assert _pin_of(camera_id) == pem
        with session_scope(get_context().session_factory) as db:
            assert db.get(Camera, camera_id).address == _ADDRESS  # type: ignore[union-attr]
