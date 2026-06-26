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

import pytest

from timelapse_manager.cameras.autoquery import QueryResult, query_camera
from timelapse_manager.cameras.base import (
    GeoLocation,
    ValidationFailure,
    ValidationResult,
)
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
        params = {"root.Network.HostName": "front-door-cam"}
        assert hostname_from_params(params) == "front-door-cam"

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
        # rtsp responds, but vapix rejects the credentials -> auth_rejected True
        # while the responding protocol is still offered (error_protocol stays None).
        outcome = _outcome(recommended="rtsp", vapix=False, onvif=False, rtsp=True)
        auth = ValidationResult(ok=False, reason=ValidationFailure.AUTH, message="401")
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
            vapix_cls.return_value.validate_connection = AsyncMock(return_value=auth)
            vapix_cls.return_value.close = AsyncMock(return_value=None)
            onvif_cls.return_value.validate_connection = AsyncMock(
                return_value=unreachable
            )
            onvif_cls.return_value.close = AsyncMock(return_value=None)
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.ok_count == 1
        assert result.recommended_primary == "rtsp"
        assert result.error_protocol is None
        assert result.auth_rejected is True
        assert result.error_geo == "no_location"
        assert result.error_hostname == "no_hostname"


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


class TestQueryCameraNothingResponds:
    """No protocol responds: classify the failure, metadata unavailable."""

    async def test_unreachable_classification(self) -> None:
        outcome = _outcome(
            recommended=None, vapix=False, onvif=False, rtsp=False, http=False
        )
        # Both classifier adapters report a plain unreachable.
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
                instance = cls.return_value
                instance.validate_connection = AsyncMock(return_value=unreachable)
                instance.close = AsyncMock(return_value=None)
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.ok_count == 0
        assert result.recommended_primary is None
        assert result.error_protocol == "unreachable"
        assert result.error_hostname == "no_hostname"
        assert result.error_geo == "unreachable"
        assert result.discovered_hostname is None
        assert result.fetched_lat is None

    async def test_auth_failed_classification(self) -> None:
        outcome = _outcome(recommended=None, vapix=False, onvif=False)
        auth = ValidationResult(ok=False, reason=ValidationFailure.AUTH, message="401")
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
            # An AUTH on either truly-detected protocol wins over unreachable.
            vapix_cls.return_value.validate_connection = AsyncMock(return_value=auth)
            vapix_cls.return_value.close = AsyncMock(return_value=None)
            onvif_cls.return_value.validate_connection = AsyncMock(
                return_value=unreachable
            )
            onvif_cls.return_value.close = AsyncMock(return_value=None)
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.error_protocol == "auth_failed"
        assert result.error_geo == "unreachable"
        assert result.error_hostname == "no_hostname"

    async def test_timeout_classification(self) -> None:
        outcome = _outcome(recommended=None, vapix=False, onvif=False)
        timeout = ValidationResult(
            ok=False, reason=ValidationFailure.TIMEOUT, message="timed out"
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
                cls.return_value.validate_connection = AsyncMock(return_value=timeout)
                cls.return_value.close = AsyncMock(return_value=None)
            result = await query_camera(
                address=_ADDRESS, credentials=None, http_client=MagicMock()
            )
        assert result.error_protocol == "timeout"


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
                http_client=MagicMock(),
                default_credentials=("default", "dpw"),
            )
        assert probe.await_args.args[1] == ("default", "dpw")
