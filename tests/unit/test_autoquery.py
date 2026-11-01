"""Unit tests for the consolidated camera auto-query and the VAPIX hostname parser.

``query_camera`` fans out the protocol probe, geolocation read, and hostname read
into one call and returns a single :class:`QueryResult` whose granular ``error_*``
fields let the UI render each probe's outcome independently. These tests patch the
clean seams (``detect_protocols`` and the adapter metadata reads) so no real
network or adapter work runs, and assert the aggregation contract:

* all-success populates every field and leaves every ``error_*`` None,
* a protocol that responds but yields no metadata reports ``no_location`` /
  ``no_hostname`` while leaving ``error_protocol`` None,
* nothing responding classifies ``error_protocol`` (unreachable / auth_failed)
  and reports the metadata probes as unavailable,
* one metadata read failing never sinks the other.

The VAPIX hostname-parser tests cover the ``Network.HostName`` extraction
(prefix tolerance, placeholder rejection, empty/missing values).

RFC-5737 documentation addresses (``192.0.2.x``) are used throughout; no real
camera is contacted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from timelapse_manager.cameras.autoquery import QueryResult, query_camera
from timelapse_manager.cameras.base import (
    GeoLocation,
    GeolocationAccessDenied,
    ValidationFailure,
    ValidationResult,
)
from timelapse_manager.cameras.clients import allow_uncontained_double
from timelapse_manager.cameras.probing import (
    Confidence,
    DetectionOutcome,
    ProtocolCandidate,
)
from timelapse_manager.cameras.vapix import hostname_from_params

_ADDRESS = "192.0.2.20"
_AUTOQUERY = "timelapse_manager.cameras.autoquery"


def _candidate(protocol: str, ok: bool, **kwargs: object) -> ProtocolCandidate:
    confidence = kwargs.pop(
        "confidence",
        Confidence.HIGH if protocol in ("vapix", "onvif") else Confidence.LOW,
    )
    return ProtocolCandidate(
        protocol=protocol,
        ok=ok,
        confidence=confidence,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _outcome(*, recommended: str | None, **oks: bool) -> DetectionOutcome:
    """Build a DetectionOutcome with one candidate per named protocol."""
    candidates = [_candidate(protocol, ok) for protocol, ok in oks.items()]
    return DetectionOutcome(candidates=candidates, recommended_primary=recommended)


def _mock_metadata_adapter(
    *, geo: GeoLocation | None, hostname: str | None
) -> MagicMock:
    """A stand-in adapter exposing the two metadata reads and a close()."""
    adapter = MagicMock()
    adapter.get_geolocation = AsyncMock(return_value=geo)
    adapter.get_device_hostname = AsyncMock(return_value=hostname)
    adapter.close = AsyncMock(return_value=None)
    return adapter


class TestHostnameFromParams:
    """The Axis Network.HostName param-group parser."""

    def test_extracts_hostname_with_root_prefix(self) -> None:
        params = {"root.Network.HostName": "porch-cam"}
        assert hostname_from_params(params) == "porch-cam"

    def test_extracts_hostname_without_root_prefix(self) -> None:
        params = {"Network.HostName": "garage-cam"}
        assert hostname_from_params(params) == "garage-cam"

    def test_trims_surrounding_whitespace(self) -> None:
        params = {"root.Network.HostName": "  lobby-cam  "}
        assert hostname_from_params(params) == "lobby-cam"

    def test_returns_none_when_absent(self) -> None:
        assert hostname_from_params({}) is None
        assert hostname_from_params({"root.Network.DNSServer": "10.0.0.1"}) is None

    def test_returns_none_when_empty_value(self) -> None:
        assert hostname_from_params({"root.Network.HostName": ""}) is None
        assert hostname_from_params({"root.Network.HostName": "   "}) is None

    def test_rejects_unconfigured_placeholders(self) -> None:
        for placeholder in ("<hostname>", "set hostname", "AXIS", "axis"):
            params = {"root.Network.HostName": placeholder}
            assert hostname_from_params(params) is None

    def test_does_not_match_nested_hostname_keys(self) -> None:
        # A different group ending in 'HostName' must not be mistaken for the
        # device's Network.HostName.
        params = {"root.Network.Resolver.HostName": "resolver-host"}
        assert hostname_from_params(params) is None


class TestQueryCameraAllSuccess:
    async def test_populates_every_field_no_errors(self) -> None:
        outcome = _outcome(recommended="vapix", vapix=True, onvif=False)
        adapter = _mock_metadata_adapter(
            geo=GeoLocation(latitude=34.1, longitude=-83.9, source="camera"),
            hostname="rooftop-cam",
        )
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(
                f"{_AUTOQUERY}._build_metadata_adapter",
                return_value=adapter,
            ),
        ):
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert isinstance(result, QueryResult)
        assert result.recommended_primary == "vapix"
        assert result.ok_count == 1
        assert result.discovered_hostname == "rooftop-cam"
        assert result.fetched_lat == pytest.approx(34.1)
        assert result.fetched_lon == pytest.approx(-83.9)
        assert result.error_protocol is None
        assert result.error_hostname is None
        assert result.error_geo is None
        # An auth-capable protocol succeeded, so no masked-credential check runs.
        assert result.auth_rejected is False
        # The transient metadata adapter is always closed.
        adapter.close.assert_awaited_once()


class TestQueryCameraProtocolOnly:
    """A protocol responds, but the metadata reads return nothing."""

    async def test_geo_and_hostname_both_unavailable(self) -> None:
        outcome = _outcome(recommended="vapix", vapix=True)
        adapter = _mock_metadata_adapter(geo=None, hostname=None)
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(
                f"{_AUTOQUERY}._build_metadata_adapter",
                return_value=adapter,
            ),
        ):
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.error_protocol is None
        assert result.ok_count == 1
        assert result.discovered_hostname is None
        assert result.fetched_lat is None
        assert result.fetched_lon is None
        assert result.error_geo == "no_location"
        assert result.error_hostname == "no_hostname"

    async def test_best_effort_protocol_has_no_metadata_query(self) -> None:
        # rtsp/http responded but expose no metadata read; no adapter is built.
        # The auth-capable protocols are re-checked for a masked credential
        # rejection; here they are merely unreachable, so auth_rejected stays False.
        outcome = _outcome(recommended="rtsp", vapix=False, onvif=False, rtsp=True)
        unreachable = ValidationResult(
            ok=False, reason=ValidationFailure.UNREACHABLE, message="no route"
        )
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(f"{_AUTOQUERY}.VapixAdapter") as vapix_cls,
            patch(f"{_AUTOQUERY}.OnvifAdapter") as onvif_cls,
        ):
            for cls in (vapix_cls, onvif_cls):
                cls.return_value.validate_connection = AsyncMock(
                    return_value=unreachable
                )
                cls.return_value.close = AsyncMock(return_value=None)
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.recommended_primary == "rtsp"
        assert result.ok_count == 1
        assert result.error_protocol is None
        assert result.discovered_hostname is None
        assert result.error_geo == "no_location"
        assert result.error_hostname == "no_hostname"
        assert result.auth_rejected is False

    async def test_best_effort_protocol_flags_masked_auth_rejection(self) -> None:
        # Wrong credentials must not be masked by an open connection-only port:
        # rtsp responds, but vapix rejected the credentials -> auth_rejected True
        # while the responding protocol is still offered (error_protocol stays None).
        # The reason comes from the probe's own candidate: nothing re-probes.
        outcome = DetectionOutcome(
            candidates=[
                _candidate("vapix", False, reason=ValidationFailure.AUTH),
                _candidate("onvif", False, reason=ValidationFailure.UNREACHABLE),
                _candidate("rtsp", True),
            ],
            recommended_primary="rtsp",
        )
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(f"{_AUTOQUERY}.VapixAdapter") as vapix_cls,
            patch(f"{_AUTOQUERY}.OnvifAdapter") as onvif_cls,
        ):
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.ok_count == 1
        assert result.recommended_primary == "rtsp"
        assert result.error_protocol is None
        assert result.auth_rejected is True
        assert result.error_geo == "no_location"
        assert result.error_hostname == "no_hostname"
        # No second round of logins against the camera (#69 CAM-P5).
        vapix_cls.assert_not_called()
        onvif_cls.assert_not_called()


class TestQueryCameraNoMetadataPartial:
    """Geo succeeds while hostname fails, and vice versa -- one never sinks both."""

    async def test_geo_ok_hostname_missing(self) -> None:
        outcome = _outcome(recommended="onvif", vapix=False, onvif=True)
        adapter = _mock_metadata_adapter(
            geo=GeoLocation(latitude=1.0, longitude=2.0, source="camera"),
            hostname=None,
        )
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(f"{_AUTOQUERY}._build_metadata_adapter", return_value=adapter),
        ):
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.fetched_lat == pytest.approx(1.0)
        assert result.error_geo is None
        assert result.discovered_hostname is None
        assert result.error_hostname == "no_hostname"

    async def test_hostname_ok_geo_missing(self) -> None:
        outcome = _outcome(recommended="vapix", vapix=True)
        adapter = _mock_metadata_adapter(geo=None, hostname="dome-1")
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(f"{_AUTOQUERY}._build_metadata_adapter", return_value=adapter),
        ):
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.discovered_hostname == "dome-1"
        assert result.error_hostname is None
        assert result.fetched_lat is None
        assert result.error_geo == "no_location"

    async def test_hostname_read_raising_degrades_to_no_hostname(self) -> None:
        outcome = _outcome(recommended="vapix", vapix=True)
        adapter = _mock_metadata_adapter(
            geo=GeoLocation(latitude=5.0, longitude=6.0, source="camera"),
            hostname=None,
        )
        # A hostname read that raises must be swallowed into a clean error code,
        # never propagated, and must not affect the geo result.
        adapter.get_device_hostname = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(f"{_AUTOQUERY}._build_metadata_adapter", return_value=adapter),
        ):
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.fetched_lat == pytest.approx(5.0)
        assert result.error_geo is None
        assert result.discovered_hostname is None
        assert result.error_hostname == "no_hostname"

    async def test_geo_access_denied_keeps_hostname(self) -> None:
        """A refused geo read reports access_denied and never sinks the hostname."""
        outcome = _outcome(recommended="vapix", vapix=True)
        adapter = _mock_metadata_adapter(geo=None, hostname="driveway-1")
        adapter.get_geolocation = AsyncMock(
            side_effect=GeolocationAccessDenied("vapix", "Administrator", 401)
        )
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(f"{_AUTOQUERY}._build_metadata_adapter", return_value=adapter),
        ):
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.error_geo == "access_denied"
        assert result.geo_required_role == "Administrator"
        assert result.fetched_lat is None
        assert result.fetched_lon is None
        assert result.discovered_hostname == "driveway-1"
        assert result.error_hostname is None
        adapter.close.assert_awaited_once()


class TestQueryCameraNothingResponds:
    """No protocol responds: classify the failure, metadata unavailable."""

    @staticmethod
    async def _query(**reasons: ValidationFailure | None) -> QueryResult:
        """Query with nothing responding; each auth-capable probe's reason."""
        outcome = DetectionOutcome(
            candidates=[
                _candidate("vapix", False, reason=reasons.get("vapix")),
                _candidate("onvif", False, reason=reasons.get("onvif")),
                _candidate("rtsp", False, reason=ValidationFailure.UNREACHABLE),
                _candidate("http", False, reason=ValidationFailure.AUTH),
            ],
            recommended_primary=None,
        )
        with (
            patch(
                f"{_AUTOQUERY}.detect_protocols",
                new=AsyncMock(return_value=outcome),
            ),
            patch(f"{_AUTOQUERY}.VapixAdapter") as vapix_cls,
            patch(f"{_AUTOQUERY}.OnvifAdapter") as onvif_cls,
        ):
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        vapix_cls.assert_not_called()
        onvif_cls.assert_not_called()
        return result

    async def test_unreachable_classification(self) -> None:
        result = await self._query(
            vapix=ValidationFailure.UNREACHABLE, onvif=ValidationFailure.UNREACHABLE
        )
        assert result.ok_count == 0
        assert result.recommended_primary is None
        assert result.error_protocol == "unreachable"
        assert result.error_hostname == "no_hostname"
        assert result.error_geo == "unreachable"
        assert result.discovered_hostname is None
        assert result.fetched_lat is None

    async def test_auth_failed_classification(self) -> None:
        # An AUTH on either truly-detected protocol wins over unreachable.
        result = await self._query(
            vapix=ValidationFailure.AUTH, onvif=ValidationFailure.UNREACHABLE
        )
        assert result.error_protocol == "auth_failed"
        assert result.error_geo == "unreachable"
        assert result.error_hostname == "no_hostname"

    async def test_timeout_classification(self) -> None:
        result = await self._query(
            vapix=ValidationFailure.TIMEOUT, onvif=ValidationFailure.TIMEOUT
        )
        assert result.error_protocol == "timeout"

    async def test_a_best_effort_auth_failure_is_not_classified(self) -> None:
        # Only the auth-capable probes classify; an HTTP 401 alone (the
        # helper's http candidate) with no reason elsewhere reads unreachable.
        result = await self._query()
        assert result.error_protocol == "unreachable"


class TestQueryCameraCredentialResolution:
    """The camera's own credentials win; the global default is the fallback."""

    async def test_own_credentials_passed_to_probe(self) -> None:
        outcome = _outcome(recommended=None, vapix=False)
        probe = AsyncMock(return_value=outcome)
        unreachable = ValidationResult(
            ok=False, reason=ValidationFailure.UNREACHABLE, message="x"
        )
        with (
            patch(f"{_AUTOQUERY}.detect_protocols", new=probe),
            patch(f"{_AUTOQUERY}.VapixAdapter") as vapix_cls,
            patch(f"{_AUTOQUERY}.OnvifAdapter") as onvif_cls,
        ):
            for cls in (vapix_cls, onvif_cls):
                cls.return_value.validate_connection = AsyncMock(
                    return_value=unreachable
                )
                cls.return_value.close = AsyncMock(return_value=None)
            await query_camera(
                address=_ADDRESS,
                credentials={"username": "own", "password": "pw"},
                http_client=MagicMock(),
                default_credentials=("default", "dpw"),
            )
        # detect_protocols(address, credentials, client, ...) -- the resolved
        # tuple is the camera's own pair, not the default.
        assert probe.await_args.args[1] == ("own", "pw")

    async def test_default_credentials_used_when_camera_has_none(self) -> None:
        outcome = _outcome(recommended=None, vapix=False)
        probe = AsyncMock(return_value=outcome)
        unreachable = ValidationResult(
            ok=False, reason=ValidationFailure.UNREACHABLE, message="x"
        )
        with (
            patch(f"{_AUTOQUERY}.detect_protocols", new=probe),
            patch(f"{_AUTOQUERY}.VapixAdapter") as vapix_cls,
            patch(f"{_AUTOQUERY}.OnvifAdapter") as onvif_cls,
        ):
            for cls in (vapix_cls, onvif_cls):
                cls.return_value.validate_connection = AsyncMock(
                    return_value=unreachable
                )
                cls.return_value.close = AsyncMock(return_value=None)
            await query_camera(
                address=_ADDRESS,
                credentials=None,
                http_client=allow_uncontained_double(MagicMock()),
                default_credentials=("default", "dpw"),
            )
        assert probe.await_args.args[1] == ("default", "dpw")


class TestQueryCameraLoginCount:
    """A camera given wrong credentials sees one failed login per protocol.

    #69 CAM-P5: the query used to re-run the VAPIX and ONVIF probes to recover
    their failure reasons, and the HTTP probe sent the credentials to every
    common snapshot path, so one "Query" cost an Axis camera up to eight failed
    logins and brought its brute-force lockout closer. Now the reasons travel on
    the candidates, ONVIF stops after its first failed call, and the HTTP paths
    get no credentials once VAPIX has rejected them.
    """

    @staticmethod
    def _protocol_of(request: httpx.Request) -> str:
        path = request.url.path
        if path.startswith("/onvif/"):
            return "onvif"
        if path.startswith("/axis-cgi/"):
            return "vapix"
        return "http"

    async def test_a_rejecting_camera_gets_one_login_per_protocol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from timelapse_manager.cameras import http_jpeg, probing
        from timelapse_manager.cameras.onvif import OnvifAdapter

        # A unit test has no application context: skip the SSRF guards and the
        # context-read body cap, and keep the RTSP probe off the network.
        monkeypatch.setattr(http_jpeg, "_guard_snapshot_url", AsyncMock())
        monkeypatch.setattr(http_jpeg, "_resolve_max_image_bytes", lambda: 1 << 20)
        monkeypatch.setattr(OnvifAdapter, "_guard_soap_url", AsyncMock())
        monkeypatch.setattr(
            probing,
            "_probe_rtsp",
            AsyncMock(return_value=_candidate("rtsp", False)),
        )

        logins: dict[str, int] = {"vapix": 0, "onvif": 0, "http": 0}
        requests: dict[str, int] = {"vapix": 0, "onvif": 0, "http": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            protocol = self._protocol_of(request)
            requests[protocol] += 1
            carries_login = "authorization" in request.headers or (
                b"wrong-user" in request.content
            )
            if carries_login:
                logins[protocol] += 1
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Digest realm="axis", nonce="n1", qop="auth"'
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await query_camera(
                address=_ADDRESS,
                credentials={"username": "wrong-user", "password": "wrong-pass"},
                http_client=client,
                timeout=2.0,
            )

        assert result.ok_count == 0
        assert result.error_protocol == "auth_failed"
        assert logins == {"vapix": 1, "onvif": 1, "http": 0}
        # Every HTTP path was still probed, anonymously.
        assert requests["http"] == len(probing.HTTP_SNAPSHOT_PATHS)

    async def test_http_paths_get_credentials_when_no_protocol_rejected_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain HTTP camera (no VAPIX, no ONVIF) still gets its login."""
        from timelapse_manager.cameras import http_jpeg, probing
        from timelapse_manager.cameras.onvif import OnvifAdapter

        monkeypatch.setattr(http_jpeg, "_guard_snapshot_url", AsyncMock())
        monkeypatch.setattr(http_jpeg, "_resolve_max_image_bytes", lambda: 1 << 20)
        monkeypatch.setattr(OnvifAdapter, "_guard_soap_url", AsyncMock())
        monkeypatch.setattr(
            probing,
            "_probe_rtsp",
            AsyncMock(return_value=_candidate("rtsp", False)),
        )
        logins: list[str] = []
        # Digest: a probe never sends Basic over plain HTTP (#69 CAM-S1, OD-3).
        challenge = {"WWW-Authenticate": 'Digest realm="cam", nonce="n1", qop="auth"'}

        def handler(request: httpx.Request) -> httpx.Response:
            protocol = self._protocol_of(request)
            if protocol != "http":
                return httpx.Response(404)
            if "authorization" in request.headers:
                logins.append(request.url.path)
            return httpx.Response(401, headers=challenge)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await query_camera(
                address=_ADDRESS,
                credentials={"username": "u", "password": "wrong"},
                http_client=client,
                timeout=2.0,
            )

        assert result.ok_count == 0
        # One credentialed try on the first path; once rejected, no more.
        assert logins == [probing.HTTP_SNAPSHOT_PATHS[0]]

    async def test_basic_only_over_plain_http_sends_no_password_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#69 CAM-S1 (OD-3): a probe refuses Basic over http:// before sending."""
        from timelapse_manager.cameras import http_jpeg, probing
        from timelapse_manager.cameras.onvif import OnvifAdapter

        monkeypatch.setattr(http_jpeg, "_guard_snapshot_url", AsyncMock())
        monkeypatch.setattr(http_jpeg, "_resolve_max_image_bytes", lambda: 1 << 20)
        monkeypatch.setattr(OnvifAdapter, "_guard_soap_url", AsyncMock())
        monkeypatch.setattr(
            probing,
            "_probe_rtsp",
            AsyncMock(return_value=_candidate("rtsp", False)),
        )
        authorizations: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "authorization" in request.headers:
                authorizations.append(request.headers["authorization"])
            if self._protocol_of(request) != "http":
                return httpx.Response(404)
            return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="c"'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await query_camera(
                address=_ADDRESS,
                credentials={"username": "u", "password": "secret"},
                http_client=client,
                timeout=2.0,
            )

        assert result.ok_count == 0
        assert authorizations == []
        http = next(c for c in result.candidates if c.protocol == "http")
        assert "https://" in http.detail
        assert "Basic" in http.detail


class TestBasicOverHttpOptIn:
    """#332: Query honours the form's Basic-over-HTTP opt-in like capture (OD-3)."""

    async def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        address: str,
        allow_basic_over_http: bool,
        only: str = "http",
    ) -> tuple[QueryResult, list[str]]:
        from timelapse_manager.cameras import http_jpeg, probing
        from timelapse_manager.cameras.onvif import OnvifAdapter

        monkeypatch.setattr(http_jpeg, "_guard_snapshot_url", AsyncMock())
        monkeypatch.setattr(http_jpeg, "_resolve_max_image_bytes", lambda: 1 << 20)
        monkeypatch.setattr(OnvifAdapter, "_guard_soap_url", AsyncMock())
        monkeypatch.setattr(probing, "frame_from_bytes", lambda _b: None)
        monkeypatch.setattr(
            probing,
            "_probe_rtsp",
            AsyncMock(return_value=_candidate("rtsp", False)),
        )
        sent: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            protocol = (
                "vapix"
                if path.startswith("/axis-cgi/")
                else "onvif"
                if path.startswith("/onvif/")
                else "http"
            )
            if protocol != only:
                return httpx.Response(404)
            auth = request.headers.get("authorization")
            if auth is not None:
                sent.append(f"{request.url.scheme}:{auth.split()[0]}")
                if auth.startswith("Basic ") and path == probing.HTTP_SNAPSHOT_PATHS[0]:
                    return httpx.Response(200, content=b"jpegbytes")
            return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="c"'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await query_camera(
                address=address,
                credentials={"username": "u", "password": "secret"},
                http_client=client,
                timeout=2.0,
                allow_basic_over_http=allow_basic_over_http,
            )
        return result, sent

    async def test_opt_in_sends_basic_to_a_plain_http_camera(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, sent = await self._run(
            monkeypatch, address=f"http://{_ADDRESS}", allow_basic_over_http=True
        )
        assert sent == ["http:Basic"]
        http = next(c for c in result.candidates if c.protocol == "http")
        assert http.ok

    async def test_without_opt_in_no_basic_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, sent = await self._run(
            monkeypatch, address=f"http://{_ADDRESS}", allow_basic_over_http=False
        )
        assert sent == []
        http = next(c for c in result.candidates if c.protocol == "http")
        assert not http.ok
        assert "Basic" in http.detail

    async def test_opt_in_reaches_the_vapix_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, with_opt = await self._run(
            monkeypatch,
            address=f"http://{_ADDRESS}",
            allow_basic_over_http=True,
            only="vapix",
        )
        _, without = await self._run(
            monkeypatch,
            address=f"http://{_ADDRESS}",
            allow_basic_over_http=False,
            only="vapix",
        )
        assert "http:Basic" in with_opt
        assert without == []

    async def test_opt_in_never_downgrades_an_https_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, sent = await self._run(
            monkeypatch, address=f"https://{_ADDRESS}", allow_basic_over_http=True
        )
        assert sent and all(s.startswith("https:") for s in sent)
