"""Camera HTTP auth negotiation (#69 WP-69-H, with WP-69-U merged).

Pins:

* challenge parsing: repeated and merged ``WWW-Authenticate`` headers, quoted
  commas, and the malformed shapes that must never be answered (CAM-S1);
* the scheme choice: Digest over Basic, SHA-256 over MD5, no downgrade to Basic
  when a Digest offer cannot be answered, unknown schemes refused (CAM-S1);
* Basic over plain HTTP refused unless the camera opts in (CAM-S1 item 2, OD-3);
* Digest state reuse: a second capture is one request, not three (CAM-P1), and a
  stale or expired nonce costs one extra request, never a failure;
* the redirect-to-HTTPS hint, and that no secret reaches a log or a message.

The Digest computation is cross-checked against httpx's own implementation, and
the fake camera verifies headers with independent code
(:mod:`tests.unit.camera_auth_support`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from unittest.mock import patch

import httpx
import pytest

from tests.unit.camera_auth_support import FakeAxisCamera, parse_authorization
from timelapse_manager.cameras.base import (
    AuthCaptureError,
    OtherCaptureError,
    ValidationFailure,
)
from timelapse_manager.cameras.http_auth import (
    BASIC_OVER_HTTP_REFUSED,
    BasicPolicy,
    BasicRefusedError,
    CameraAuthState,
    DigestChallenge,
    UnsupportedAuthSchemeError,
    digest_authorization,
    parse_challenges,
)
from timelapse_manager.cameras.http_jpeg import http_get_image, https_redirect_hint

_GUARD = "timelapse_manager.cameras.http_jpeg._guard_snapshot_url"
_CAP = "timelapse_manager.cameras.http_jpeg._resolve_max_image_bytes"
_CREDS = ("timelapse", "s3cret-pw")
_HTTPS_SNAP = "https://driveway.cam.test/axis-cgi/jpg/image.cgi?resolution=1920x1080"
_HTTP_SNAP = "http://10.0.16.71/axis-cgi/jpg/image.cgi"


@pytest.fixture(autouse=True)
def _no_context() -> Iterator[None]:
    """http_get_image without a runtime context: no SSRF guard, a large cap."""
    with patch(_GUARD), patch(_CAP, return_value=64 * 1024 * 1024):
        yield


async def _get(
    camera: FakeAxisCamera,
    url: str,
    state: CameraAuthState,
    credentials: tuple[str, str] | None = _CREDS,
) -> bytes:
    async with httpx.AsyncClient(transport=camera.transport()) as client:
        return await http_get_image(client, url, credentials, 5.0, auth_state=state)


def _camera_state(allow_basic_over_http: bool = False) -> CameraAuthState:
    return CameraAuthState(BasicPolicy.for_camera(allow_basic_over_http))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseChallenges:
    def test_two_header_lines_give_two_challenges(self) -> None:
        challenges = parse_challenges(
            ['Basic realm="cam"', 'Digest realm="cam", nonce="n", qop="auth"']
        )
        assert [c.scheme for c in challenges] == ["basic", "digest"]
        assert challenges[1].params == {"realm": "cam", "nonce": "n", "qop": "auth"}
        assert all(c.valid for c in challenges)

    def test_one_merged_line_gives_two_challenges(self) -> None:
        (merged,) = [
            'Basic realm="cam", Digest realm="cam", nonce="n", '
            'qop="auth,auth-int", algorithm=SHA-256'
        ]
        basic, digest = parse_challenges([merged])
        assert (basic.scheme, dict(basic.params)) == ("basic", {"realm": "cam"})
        assert digest.scheme == "digest"
        assert digest.params["qop"] == "auth,auth-int"
        assert digest.params["algorithm"] == "SHA-256"

    def test_a_quoted_comma_does_not_start_a_challenge(self) -> None:
        (only,) = parse_challenges(['Digest realm="a, Basic realm=x", nonce="n"'])
        assert only.scheme == "digest"
        assert only.params["realm"] == "a, Basic realm=x"
        assert only.valid

    def test_escaped_quotes_are_unescaped(self) -> None:
        (only,) = parse_challenges([r'Digest realm="say \"hi\"", nonce="n"'])
        assert only.params["realm"] == 'say "hi"'

    def test_names_and_schemes_are_case_insensitive(self) -> None:
        (only,) = parse_challenges(['DIGEST Realm="r", NONCE="n"'])
        assert only.scheme == "digest"
        assert only.params == {"realm": "r", "nonce": "n"}

    def test_token68_and_bare_schemes(self) -> None:
        negotiate, basic = parse_challenges(["Negotiate YII=", "Basic"])
        assert (negotiate.scheme, negotiate.token68) == ("negotiate", "YII=")
        assert (basic.scheme, dict(basic.params)) == ("basic", {})

    @pytest.mark.parametrize(
        "header",
        [
            'Digest realm="a", realm="b", nonce="n"',  # repeated parameter
            'Digest realm="a", nonce="n\r\nX-Injected: 1"',  # control character
            'Digest realm="a", nonce=n"x',  # bad unquoted value
            'Digest realm="a" junk, nonce="n"',  # trailing text after a string
        ],
    )
    def test_ambiguous_challenges_are_invalid(self, header: str) -> None:
        (only,) = parse_challenges([header])
        assert only.scheme == "digest"
        assert only.valid is False
        assert DigestChallenge.from_challenge(only) is None

    def test_a_parameter_before_any_scheme_is_unusable(self) -> None:
        stray, basic = parse_challenges(['realm="x", Basic realm="c"'])
        assert (stray.scheme, stray.valid) == ("", False)
        assert (basic.scheme, basic.valid) == ("basic", True)

    def test_an_unterminated_quote_is_offered_but_unusable(self) -> None:
        (only,) = parse_challenges(['Digest realm="never closed, nonce="n"'])
        assert (only.scheme, only.valid) == ("digest", False)


class TestDigestChallenge:
    @pytest.mark.parametrize(
        ("params", "usable"),
        [
            ('realm="r", nonce="n"', True),
            ('realm="r", nonce="n", qop="auth"', True),
            ('realm="r", nonce="n", qop="auth-int, auth"', True),
            ('realm="r", nonce="n", qop="auth-int"', False),
            ('realm="r", nonce="n", algorithm=SHA-512-256', False),
            ('realm="r", nonce="n", algorithm=MD5-sess', False),  # sess needs qop
            ('realm="r", nonce="n", algorithm=MD5-sess, qop=auth', True),
            ('nonce="n"', False),
            ('realm="r"', False),
        ],
    )
    def test_usable_digest_offers(self, params: str, usable: bool) -> None:
        (challenge,) = parse_challenges([f"Digest {params}"])
        assert (DigestChallenge.from_challenge(challenge) is not None) is usable


# ---------------------------------------------------------------------------
# The header the client computes
# ---------------------------------------------------------------------------


def _httpx_digest_header(challenge_header: str, method: str, url: str) -> str:
    """What httpx's DigestAuth sends for this challenge (its cnonce fixed)."""
    auth = httpx.DigestAuth(*_CREDS)
    request = httpx.Request(method, url)
    flow = auth.sync_auth_flow(request)
    next(flow)
    response = httpx.Response(
        401, headers={"WWW-Authenticate": challenge_header}, request=request
    )
    with patch.object(httpx.DigestAuth, "_get_client_nonce", return_value=b"c0ffee"):
        retried = flow.send(response)
    return retried.headers["authorization"]


class TestDigestAuthorization:
    @pytest.mark.parametrize(
        "params",
        [
            'realm="AXIS_X", nonce="abc", qop="auth", opaque="op"',
            'realm="AXIS_X", nonce="abc", qop="auth", algorithm=SHA-256',
            'realm="AXIS_X", nonce="abc", qop="auth", algorithm=MD5-sess',
            'realm="AXIS_X", nonce="abc"',  # RFC 2069, no qop
        ],
    )
    def test_matches_httpx_for_the_same_cnonce(self, params: str) -> None:
        url = "https://cam.test/axis-cgi/jpg/image.cgi?resolution=640x480&x=a%20b"
        theirs = parse_authorization(
            _httpx_digest_header(f"Digest {params}", "GET", url)
        )
        (parsed,) = parse_challenges([f"Digest {params}"])
        challenge = DigestChallenge.from_challenge(parsed)
        assert challenge is not None
        ours = parse_authorization(
            digest_authorization(
                challenge,
                _CREDS,
                method="GET",
                uri=httpx.URL(url).raw_path.decode("ascii"),
                nonce_count=1,
                cnonce="c0ffee",
            )
        )
        assert ours["response"] == theirs["response"]
        assert ours["uri"] == theirs["uri"]

    def test_quotes_in_values_are_escaped(self) -> None:
        challenge = DigestChallenge(
            realm='r"x', nonce="n", algorithm="MD5", opaque=None, qop_auth=True
        )
        header = digest_authorization(
            challenge, ('us"er', "pw"), method="GET", uri="/", nonce_count=1
        )
        assert 'username="us\\"er"' in header
        assert 'realm="r\\"x"' in header

    def test_a_control_character_in_the_username_is_refused(self) -> None:
        challenge = DigestChallenge(
            realm="r", nonce="n", algorithm="MD5", opaque=None, qop_auth=True
        )
        with pytest.raises(AuthCaptureError, match="control character"):
            digest_authorization(
                challenge, ("u\r\nX: 1", "pw"), method="GET", uri="/", nonce_count=1
            )

    def test_the_password_is_never_in_the_header(self) -> None:
        state = _camera_state()
        header = state.respond(
            ['Digest realm="r", nonce="n", qop="auth"'], "GET", _HTTPS_SNAP, _CREDS
        )
        assert _CREDS[1] not in header


# ---------------------------------------------------------------------------
# Choosing a scheme
# ---------------------------------------------------------------------------


class TestSchemeChoice:
    @pytest.mark.parametrize(
        "challenges",
        [
            ['Basic realm="c"', 'Digest realm="c", nonce="n", qop="auth"'],
            ['Digest realm="c", nonce="n", qop="auth"', 'Basic realm="c"'],
            ['Basic realm="c", Digest realm="c", nonce="n", qop="auth"'],
            ['Digest realm="c", nonce="n", qop="auth", Basic realm="c"'],
        ],
    )
    @pytest.mark.parametrize("url", [_HTTPS_SNAP, _HTTP_SNAP])
    def test_basic_plus_digest_selects_digest(
        self, challenges: list[str], url: str
    ) -> None:
        state = _camera_state(allow_basic_over_http=True)
        header = state.respond(challenges, "GET", url, _CREDS)
        assert header.startswith("Digest ")
        assert state.remembered_scheme(url) == "digest"

    def test_sha256_beats_md5_whatever_the_order(self) -> None:
        header = _camera_state().respond(
            [
                'Digest realm="c", nonce="n", qop="auth", algorithm=MD5',
                'Digest realm="c", nonce="n", qop="auth", algorithm=SHA-256',
            ],
            "GET",
            _HTTPS_SNAP,
            _CREDS,
        )
        assert "algorithm=SHA-256" in header

    @pytest.mark.parametrize("challenges", [["Negotiate"], ["NTLM", "Bearer x=y"], []])
    def test_an_unknown_scheme_is_an_auth_failure(self, challenges: list[str]) -> None:
        with pytest.raises(UnsupportedAuthSchemeError) as caught:
            _camera_state().respond(challenges, "GET", _HTTPS_SNAP, _CREDS)
        assert caught.value.reason is ValidationFailure.AUTH

    def test_an_unanswerable_digest_never_downgrades_to_basic(self) -> None:
        state = _camera_state(allow_basic_over_http=True)
        with pytest.raises(AuthCaptureError, match="Digest") as caught:
            state.respond(
                ['Digest realm="c", nonce="n", qop="auth-int"', 'Basic realm="c"'],
                "GET",
                _HTTPS_SNAP,
                _CREDS,
            )
        assert not isinstance(caught.value, BasicRefusedError)
        assert state.remembered_scheme(_HTTPS_SNAP) is None

    def test_an_unreadable_challenge_never_downgrades_to_basic(self) -> None:
        with pytest.raises(AuthCaptureError, match="cannot read"):
            _camera_state(allow_basic_over_http=True).respond(
                ['Basic realm="c"', 'Dgest realm="c" ,, x=="'],
                "GET",
                _HTTPS_SNAP,
                _CREDS,
            )


class TestBasicPolicy:
    """Owner decision OD-3: Basic over plain HTTP only with the camera's opt-in."""

    def test_basic_only_over_https_is_used(self) -> None:
        header = _camera_state().respond(
            ['Basic realm="c"'], "GET", _HTTPS_SNAP, _CREDS
        )
        assert header.startswith("Basic ")

    def test_basic_only_over_http_is_refused_for_a_camera(self) -> None:
        with pytest.raises(BasicRefusedError) as caught:
            _camera_state().respond(['Basic realm="c"'], "GET", _HTTP_SNAP, _CREDS)
        assert caught.value.message == BASIC_OVER_HTTP_REFUSED
        assert caught.value.reason is ValidationFailure.AUTH
        assert _CREDS[1] not in caught.value.message

    def test_basic_only_over_http_is_allowed_with_the_flag(self) -> None:
        header = _camera_state(allow_basic_over_http=True).respond(
            ['Basic realm="c"'], "GET", _HTTP_SNAP, _CREDS
        )
        assert header.startswith("Basic ")

    def test_a_probe_never_sends_basic_over_http(self) -> None:
        with pytest.raises(BasicRefusedError, match="https://"):
            CameraAuthState().respond(['Basic realm="c"'], "GET", _HTTP_SNAP, _CREDS)

    async def test_a_refused_offer_sends_no_password(self) -> None:
        camera = FakeAxisCamera(offer_digest=False)
        with pytest.raises(BasicRefusedError):
            await _get(camera, _HTTP_SNAP, _camera_state())
        assert len(camera.requests) == 1
        assert "authorization" not in camera.requests[0].headers


# ---------------------------------------------------------------------------
# Reuse across requests (CAM-P1)
# ---------------------------------------------------------------------------


class TestDigestReuse:
    @pytest.mark.parametrize(
        ("basic_first", "merged"), [(True, False), (False, False), (True, True)]
    )
    async def test_second_capture_is_one_request(
        self, basic_first: bool, merged: bool
    ) -> None:
        camera = FakeAxisCamera(basic_first=basic_first, merged=merged)
        state = _camera_state()

        await _get(camera, _HTTPS_SNAP, state)
        assert len(camera.requests) == 2  # the challenge, then the answer
        await _get(camera, _HTTPS_SNAP, state)
        await _get(camera, _HTTPS_SNAP, state)

        assert len(camera.requests) == 4  # one each after the first
        assert camera.accepted == ["digest"] * 3
        counts = [
            parse_authorization(r.headers["authorization"])["nc"]
            for r in camera.requests
            if "authorization" in r.headers
        ]
        assert counts == ["00000001", "00000002", "00000003"]

    async def test_without_shared_state_every_capture_negotiates(self) -> None:
        camera = FakeAxisCamera()
        await _get(camera, _HTTPS_SNAP, CameraAuthState())
        await _get(camera, _HTTPS_SNAP, CameraAuthState())
        assert len(camera.requests) == 4

    async def test_stale_nonce_rechallenges_once(self) -> None:
        camera = FakeAxisCamera()
        state = _camera_state()
        await _get(camera, _HTTPS_SNAP, state)
        camera.rotate_nonce(stale=True)

        assert await _get(camera, _HTTPS_SNAP, state)
        assert len(camera.requests) == 4  # stale 401, then the fresh answer
        assert camera.accepted == ["digest", "digest"]
        assert await _get(camera, _HTTPS_SNAP, state)
        assert len(camera.requests) == 5  # the new nonce is reused

    async def test_plain_401_on_reuse_retries_once(self) -> None:
        camera = FakeAxisCamera()
        state = _camera_state()
        await _get(camera, _HTTPS_SNAP, state)
        camera.rotate_nonce(stale=False)  # expired without stale=true

        assert await _get(camera, _HTTPS_SNAP, state)
        assert len(camera.requests) == 4

    async def test_wrong_password_fails_after_one_answer_and_forgets(self) -> None:
        camera = FakeAxisCamera(password="other")
        state = _camera_state()
        with pytest.raises(AuthCaptureError, match=r"\(401\)"):
            await _get(camera, _HTTPS_SNAP, state)
        assert len(camera.requests) == 2
        assert state.remembered_scheme(_HTTPS_SNAP) is None

    async def test_remembered_auth_is_not_sent_to_another_host(self) -> None:
        camera = FakeAxisCamera()
        state = _camera_state()
        await _get(camera, _HTTPS_SNAP, state)
        await _get(camera, "https://elsewhere.cam.test/snap.jpg", state)
        assert "authorization" not in camera.requests[2].headers

    async def test_remembered_auth_is_not_sent_with_other_credentials(self) -> None:
        camera = FakeAxisCamera()
        state = _camera_state()
        await _get(camera, _HTTPS_SNAP, state)
        with pytest.raises(AuthCaptureError):
            await _get(camera, _HTTPS_SNAP, state, credentials=("timelapse", "x"))
        assert "authorization" not in camera.requests[2].headers

    async def test_remembered_basic_is_sent_up_front_over_https(self) -> None:
        camera = FakeAxisCamera(offer_digest=False)
        state = _camera_state()
        await _get(camera, _HTTPS_SNAP, state)
        await _get(camera, _HTTPS_SNAP, state)
        assert len(camera.requests) == 3
        assert camera.accepted == ["basic", "basic"]

    async def test_no_credentials_means_no_answer(self) -> None:
        camera = FakeAxisCamera()
        with pytest.raises(AuthCaptureError):
            await _get(camera, _HTTPS_SNAP, _camera_state(), credentials=None)
        assert len(camera.requests) == 1


# ---------------------------------------------------------------------------
# Redirect hint and secrets
# ---------------------------------------------------------------------------


class TestRedirectHint:
    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    async def test_same_host_redirect_to_https_says_change_the_address(
        self, status: int
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status, headers={"Location": f"https://10.0.16.71{request.url.path}"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OtherCaptureError) as caught:
                await http_get_image(client, _HTTP_SNAP, None, 5.0)
        assert caught.value.message == (
            "camera redirects to HTTPS; change the address to https://10.0.16.71"
        )

    @pytest.mark.parametrize(
        ("url", "location", "hint"),
        [
            ("http://cam.test/x", "/x", None),  # same scheme
            ("http://cam.test/x", "https://evil.test/x", None),  # other host
            ("https://cam.test/x", "https://cam.test/y", None),  # already https
            ("http://cam.test/x", "https://CAM.test:8443/x", "https://cam.test:8443"),
            ("http://[fd00::5]/x", "https://[fd00::5]/x", "https://[fd00::5]"),
        ],
    )
    def test_hint_only_for_same_host_http_to_https(
        self, url: str, location: str, hint: str | None
    ) -> None:
        result = https_redirect_hint(url, 302, location)
        if hint is None:
            assert result is None
        else:
            assert result is not None and result.endswith(hint)

    def test_other_statuses_keep_the_plain_message(self) -> None:
        assert https_redirect_hint("http://c/x", 200, "https://c/x") is None
        assert https_redirect_hint("http://c/x", 302, None) is None


class TestNoSecretsLogged:
    async def test_a_negotiated_capture_logs_no_secret(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        camera = FakeAxisCamera()
        state = _camera_state()
        with caplog.at_level(logging.DEBUG):
            await _get(camera, _HTTPS_SNAP, state)
            await _get(camera, _HTTPS_SNAP, state)
            camera.password = "changed"
            with pytest.raises(AuthCaptureError) as caught:
                await _get(camera, _HTTPS_SNAP, state)
        sent = [
            r.headers["authorization"]
            for r in camera.requests
            if "authorization" in r.headers
        ]
        for secret in (_CREDS[1], *sent, camera.nonce):
            assert secret not in caplog.text
            assert secret not in caught.value.message
        assert _CREDS[1] not in repr(state.__dict__)
