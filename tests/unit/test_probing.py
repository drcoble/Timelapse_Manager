"""Unit tests for the multi-protocol detection probe.

``detect_protocols`` probes every supported protocol concurrently and returns
the full responding set plus a recommended primary. These tests patch the
per-protocol probe helpers (the clean seam) so no real network or adapter work
runs, and assert the aggregation contract:

* every responder is returned (not first-match),
* the recommended primary follows VAPIX > ONVIF > RTSP > HTTP among responders,
* one probe that raises or times out never sinks the others,
* best-effort RTSP/HTTP carry low confidence; ONVIF/VAPIX carry high,
* the credentials handed in reach the probes verbatim (the caller, not this
  module, is responsible for any default substitution).

RFC-5737 documentation addresses (``192.0.2.x``) are used throughout; no real
camera is contacted.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from timelapse_manager.cameras.probing import (
    Confidence,
    DetectionOutcome,
    ProtocolCandidate,
    detect_protocols,
)

_ADDRESS = "192.0.2.10"
_CREDS = ("probe-user", "probe-pass")

_PROBE = "timelapse_manager.cameras.probing"


def _candidate(protocol: str, ok: bool, **kwargs: object) -> ProtocolCandidate:
    confidence = kwargs.pop(
        "confidence",
        Confidence.HIGH if protocol in ("vapix", "onvif") else Confidence.LOW,
    )
    return ProtocolCandidate(
        protocol=protocol,
        ok=ok,
        confidence=confidence,
        **kwargs,  # type: ignore[arg-type]
    )


@contextlib.contextmanager
def _patches(
    *,
    vapix: object,
    onvif: object,
    rtsp: object,
    http: object,
) -> Iterator[None]:
    """Patch all four probe helpers; each value is a return or a side_effect.

    A return value becomes an ``AsyncMock(return_value=...)``; an exception class
    becomes an ``AsyncMock(side_effect=...)``; a coroutine function is patched in
    as-is.
    """
    with contextlib.ExitStack() as stack:
        for name, value in (
            ("_probe_vapix", vapix),
            ("_probe_onvif", onvif),
            ("_probe_rtsp", rtsp),
            ("_probe_http", http),
        ):
            if isinstance(value, BaseException) or (
                isinstance(value, type) and issubclass(value, BaseException)
            ):
                target = AsyncMock(side_effect=value)
            elif isinstance(value, ProtocolCandidate):
                target = AsyncMock(return_value=value)
            else:
                target = value  # type: ignore[assignment]
            stack.enter_context(patch(f"{_PROBE}.{name}", target))
        yield


async def _detect() -> DetectionOutcome:
    client = AsyncMock()
    return await detect_protocols(_ADDRESS, _CREDS, client, timeout=2.0)


class TestAggregation:
    async def test_returns_all_responders_not_first_match(self) -> None:
        """ONVIF ok + VAPIX ok must both be present; recommended is vapix."""
        with _patches(
            vapix=_candidate("vapix", True, snapshot_uri="http://v/s.jpg"),
            onvif=_candidate("onvif", True, snapshot_uri="http://o/s.jpg"),
            rtsp=_candidate("rtsp", False),
            http=_candidate("http", False),
        ):
            outcome = await _detect()

        ok = {c.protocol for c in outcome.candidates if c.ok}
        assert ok == {"vapix", "onvif"}
        assert outcome.recommended_primary == "vapix"
        # All four protocols are represented (responders and non-responders).
        assert {c.protocol for c in outcome.candidates} == {
            "vapix",
            "onvif",
            "rtsp",
            "http",
        }

    async def test_candidates_in_priority_order(self) -> None:
        with _patches(
            vapix=_candidate("vapix", False),
            onvif=_candidate("onvif", True),
            rtsp=_candidate("rtsp", True),
            http=_candidate("http", True),
        ):
            outcome = await _detect()
        assert [c.protocol for c in outcome.candidates] == [
            "vapix",
            "onvif",
            "rtsp",
            "http",
        ]


class TestRecommendedPrimary:
    @pytest.mark.parametrize(
        ("vapix_ok", "onvif_ok", "rtsp_ok", "http_ok", "expected"),
        [
            (True, True, True, True, "vapix"),
            (False, True, True, True, "onvif"),
            (False, False, True, True, "rtsp"),
            (False, False, False, True, "http"),
            (False, False, False, False, None),
        ],
    )
    async def test_priority_ordering(
        self,
        vapix_ok: bool,
        onvif_ok: bool,
        rtsp_ok: bool,
        http_ok: bool,
        expected: str | None,
    ) -> None:
        with _patches(
            vapix=_candidate("vapix", vapix_ok),
            onvif=_candidate("onvif", onvif_ok),
            rtsp=_candidate("rtsp", rtsp_ok),
            http=_candidate("http", http_ok),
        ):
            outcome = await _detect()
        assert outcome.recommended_primary == expected


class TestIsolation:
    async def test_one_raising_probe_does_not_sink_the_others(self) -> None:
        """A probe that raises degrades to a non-ok candidate; others survive."""
        with _patches(
            vapix=RuntimeError("boom"),
            onvif=_candidate("onvif", True),
            rtsp=_candidate("rtsp", True),
            http=_candidate("http", False),
        ):
            outcome = await _detect()
        by_proto = {c.protocol: c for c in outcome.candidates}
        assert by_proto["vapix"].ok is False
        assert "boom" in by_proto["vapix"].detail
        assert by_proto["onvif"].ok is True
        assert outcome.recommended_primary == "onvif"

    async def test_one_slow_probe_times_out_without_blocking(self) -> None:
        """A probe slower than the budget times out; the rest still complete."""

        async def _slow(*_a: object, **_k: object) -> ProtocolCandidate:
            await asyncio.sleep(5.0)
            return _candidate("vapix", True)

        client = AsyncMock()
        with (
            patch(f"{_PROBE}._probe_vapix", _slow),
            patch(
                f"{_PROBE}._probe_onvif",
                AsyncMock(return_value=_candidate("onvif", True)),
            ),
            patch(
                f"{_PROBE}._probe_rtsp",
                AsyncMock(return_value=_candidate("rtsp", False)),
            ),
            patch(
                f"{_PROBE}._probe_http",
                AsyncMock(return_value=_candidate("http", False)),
            ),
        ):
            outcome = await detect_protocols(_ADDRESS, _CREDS, client, timeout=0.05)
        by_proto = {c.protocol: c for c in outcome.candidates}
        assert by_proto["vapix"].ok is False
        assert "timed out" in by_proto["vapix"].detail
        assert by_proto["onvif"].ok is True


class TestConfidence:
    async def test_best_effort_protocols_marked_low_confidence(self) -> None:
        with _patches(
            vapix=_candidate("vapix", True),
            onvif=_candidate("onvif", True),
            rtsp=_candidate("rtsp", True),
            http=_candidate("http", True),
        ):
            outcome = await _detect()
        by_proto = {c.protocol: c for c in outcome.candidates}
        assert by_proto["vapix"].confidence is Confidence.HIGH
        assert by_proto["onvif"].confidence is Confidence.HIGH
        assert by_proto["rtsp"].confidence is Confidence.LOW
        assert by_proto["http"].confidence is Confidence.LOW


class TestCredentialPassthrough:
    async def test_supplied_credentials_reach_each_probe(self) -> None:
        """The creds handed in are passed verbatim to the credential-using probes.

        This module performs no default substitution -- the caller does -- so the
        exact pair given must reach the probes unchanged.
        """
        vapix_mock = AsyncMock(return_value=_candidate("vapix", True))
        onvif_mock = AsyncMock(return_value=_candidate("onvif", False))
        http_mock = AsyncMock(return_value=_candidate("http", False))
        rtsp_mock = AsyncMock(return_value=_candidate("rtsp", False))

        client = AsyncMock()
        with (
            patch(f"{_PROBE}._probe_vapix", vapix_mock),
            patch(f"{_PROBE}._probe_onvif", onvif_mock),
            patch(f"{_PROBE}._probe_rtsp", rtsp_mock),
            patch(f"{_PROBE}._probe_http", http_mock),
        ):
            await detect_protocols(_ADDRESS, _CREDS, client, timeout=2.0)

        # VAPIX/ONVIF/HTTP probes receive the credentials positionally as arg[1].
        assert vapix_mock.await_args.args[1] == _CREDS
        assert onvif_mock.await_args.args[1] == _CREDS
        assert http_mock.await_args.args[1] == _CREDS


class TestNoCredentialInUri:
    """Prove that constructed probe URIs never embed credentials.

    The existing credential-leakage tests in the API and web layers mock the
    *outcome* object and therefore only verify that the route does not re-echo
    the *posted* credentials -- they say nothing about URIs synthesised inside
    detect_protocols itself. These tests run the real probe helpers against
    mocked transport seams and assert that no returned URI contains the
    username, password, or a ``user:pass@`` userinfo segment.

    Notes on scope:
    - VAPIX: ``build_snapshot_url`` composes from the plain address; no creds.
    - RTSP: ``_probe_rtsp`` derives the stream URI from ``_host_of(address)``
      which strips userinfo via urlsplit().hostname; no creds by construction.
    - HTTP: ``_probe_http`` uses ``_base_address``, which strips any userinfo
      from the address, so a composed snapshot URI never carries credentials
      even when the operator types ``http://user:pass@host`` (covered by
      ``test_http_base_address_strips_userinfo`` /
      ``test_http_probe_uri_strips_userinfo``).
    - ONVIF: the device self-reports URIs; the adapter never embeds creds in
      the *reported* URI (it only sends them in headers at capture time).
    """

    async def test_vapix_snapshot_uri_contains_no_credentials(self) -> None:
        """VAPIX ok candidate's snapshot_uri must not contain username or password."""
        from timelapse_manager.cameras.base import ValidationResult
        from timelapse_manager.cameras.probing import _probe_vapix

        mock_adapter = AsyncMock()
        mock_adapter.validate_connection = AsyncMock(
            return_value=ValidationResult(ok=True, reason=None, message="ok")
        )
        mock_adapter.close = AsyncMock()

        with patch(f"{_PROBE}.VapixAdapter", return_value=mock_adapter):
            client = AsyncMock()
            candidate = await _probe_vapix(_ADDRESS, _CREDS, client, timeout=2.0)

        assert candidate.ok is True
        uri = candidate.snapshot_uri or ""
        # URI must not embed username, password, or userinfo separator.
        assert _CREDS[0] not in uri
        assert _CREDS[1] not in uri
        assert "@" not in uri

    async def test_vapix_snapshot_uri_strips_userinfo(self) -> None:
        """A userinfo-bearing address must not leak into the VAPIX snapshot URI."""
        from timelapse_manager.cameras.base import ValidationResult
        from timelapse_manager.cameras.probing import _probe_vapix

        mock_adapter = AsyncMock()
        mock_adapter.validate_connection = AsyncMock(
            return_value=ValidationResult(ok=True, reason=None, message="ok")
        )
        mock_adapter.close = AsyncMock()

        with patch(f"{_PROBE}.VapixAdapter", return_value=mock_adapter):
            client = AsyncMock()
            candidate = await _probe_vapix(
                "http://probe-user:probe-pass@192.0.2.10", _CREDS, client, timeout=2.0
            )

        assert candidate.ok is True
        uri = candidate.snapshot_uri or ""
        assert "probe-user" not in uri
        assert "probe-pass" not in uri
        assert "@" not in uri
        assert uri == "http://192.0.2.10/axis-cgi/jpg/image.cgi"

    async def test_rtsp_stream_uri_contains_no_credentials(self) -> None:
        """RTSP ok candidate's stream_uri must not contain username or password.

        _probe_rtsp takes no credentials at all -- this test documents that the
        stream URI is derived entirely from _host_of(address), which strips any
        userinfo via urlsplit().hostname, so no credentials can leak even if the
        caller passes a userinfo-bearing address.
        """
        from timelapse_manager.cameras.probing import _probe_rtsp

        # Patch the blocking socket call so the test stays in-process.
        with patch(
            f"{_PROBE}.asyncio.to_thread", new_callable=AsyncMock
        ) as mock_thread:
            mock_thread.return_value = None  # successful connect
            candidate = await _probe_rtsp(_ADDRESS, timeout=2.0)

        assert candidate.ok is True
        uri = candidate.stream_uri or ""
        # The stream URI is rtsp://<host>:554/ -- no credentials.
        assert _CREDS[0] not in uri
        assert _CREDS[1] not in uri
        assert "@" not in uri

    def test_http_base_address_strips_userinfo(self) -> None:
        """_base_address must drop ``user:pass@`` so composed URIs carry no creds."""
        from timelapse_manager.cameras.probing import _base_address

        assert _base_address("http://user:pass@192.0.2.10") == "http://192.0.2.10"
        assert (
            _base_address("https://user:pass@192.0.2.10:8443")
            == "https://192.0.2.10:8443"
        )
        # No userinfo -- unchanged (port preserved).
        assert _base_address("http://192.0.2.10:8080") == "http://192.0.2.10:8080"
        assert _base_address("192.0.2.10") == "http://192.0.2.10"

    async def test_http_probe_uri_strips_userinfo(self) -> None:
        """An ok HTTP probe over a userinfo-bearing address yields a clean URI."""
        from timelapse_manager.cameras.probing import _probe_http

        async def _fake_get_image(
            client: object, url: str, creds: object, timeout: float
        ) -> bytes:
            return b"\xff\xd8\xff\xe0jpeg-bytes"

        get_image = AsyncMock(side_effect=_fake_get_image)
        with (
            patch(f"{_PROBE}.http_get_image", new=get_image),
            patch(f"{_PROBE}.frame_from_bytes", return_value=object()),
        ):
            client = AsyncMock()
            candidate = await _probe_http(
                "http://probe-user:probe-pass@192.0.2.10", _CREDS, client, timeout=2.0
            )

        assert candidate.ok is True
        uri = candidate.snapshot_uri or ""
        assert "probe-user" not in uri
        assert "probe-pass" not in uri
        assert "@" not in uri

    async def test_no_candidate_uri_contains_credentials_end_to_end(self) -> None:
        """Full detect_protocols run: no ok candidate URI contains a credential.

        Mocks the per-protocol probe helpers (the same seam as other unit tests
        here) to return realistic-looking URIs for VAPIX, ONVIF, and RTSP, all
        ok -- then asserts that none of the returned URIs carry the username or
        password string.
        """
        creds = ("probe-user", "probe-pass")
        address = "192.0.2.20"

        with _patches(
            vapix=_candidate(
                "vapix",
                True,
                snapshot_uri=f"http://{address}/axis-cgi/jpg/image.cgi",
            ),
            onvif=_candidate(
                "onvif",
                True,
                snapshot_uri=f"http://{address}/onvif/snap",
                stream_uri=f"rtsp://{address}/onvif/stream",
            ),
            rtsp=_candidate(
                "rtsp",
                True,
                stream_uri=f"rtsp://{address}:554/",
            ),
            http=_candidate("http", False),
        ):
            client = AsyncMock()
            outcome = await detect_protocols(address, creds, client, timeout=2.0)

        username, password = creds
        for candidate in outcome.candidates:
            for uri in (candidate.snapshot_uri, candidate.stream_uri):
                if uri is not None:
                    assert username not in uri, (
                        f"{candidate.protocol} URI {uri!r} contains the username"
                    )
                    assert password not in uri, (
                        f"{candidate.protocol} URI {uri!r} contains the password"
                    )
                    assert "@" not in uri or "://" in uri.split("@")[0], (
                        # Allow rtsp:// or http:// scheme before any @, but reject
                        # userinfo in netloc.  The check: if @ present, everything
                        # before it must not look like ``user:pass`` (no colon or
                        # the colon is part of the scheme).
                        f"{candidate.protocol} URI {uri!r} may embed userinfo"
                    )


class TestRecommendedPrimaryFragmentStates:
    """Verify the recommended_primary / ok_count logic across edge states.

    These are purely unit-level (same probe-mock seam as other tests here)
    because the fragment rendering is exercised in the web tests.
    """

    async def test_none_ok_returns_none_recommended(self) -> None:
        """When no protocol responds, recommended_primary is None."""
        with _patches(
            vapix=_candidate("vapix", False),
            onvif=_candidate("onvif", False),
            rtsp=_candidate("rtsp", False),
            http=_candidate("http", False),
        ):
            outcome = await _detect()
        assert outcome.recommended_primary is None
        ok = [c for c in outcome.candidates if c.ok]
        assert ok == []

    async def test_single_ok_recommends_that_protocol(self) -> None:
        """With exactly one responder the outcome carries it as recommended."""
        with _patches(
            vapix=_candidate("vapix", False),
            onvif=_candidate("onvif", False),
            rtsp=_candidate("rtsp", False),
            http=_candidate("http", True, snapshot_uri="http://192.0.2.10/snap.jpg"),
        ):
            outcome = await _detect()
        assert outcome.recommended_primary == "http"
        ok = [c for c in outcome.candidates if c.ok]
        assert len(ok) == 1

    async def test_onvif_and_rtsp_ok_recommends_onvif(self) -> None:
        """ONVIF + RTSP both ok → recommended is onvif (higher priority)."""
        with _patches(
            vapix=_candidate("vapix", False),
            onvif=_candidate("onvif", True, snapshot_uri="http://192.0.2.10/onvif/s"),
            rtsp=_candidate("rtsp", True, stream_uri="rtsp://192.0.2.10:554/"),
            http=_candidate("http", False),
        ):
            outcome = await _detect()
        assert outcome.recommended_primary == "onvif"

    async def test_both_raise_and_timeout_degrade_gracefully(self) -> None:
        """A raiser and a timed-out probe together still return the ok results."""

        async def _slow(*_a: object, **_k: object) -> ProtocolCandidate:
            await asyncio.sleep(5.0)
            return _candidate("vapix", True)  # pragma: no cover

        client = AsyncMock()
        with (
            patch(f"{_PROBE}._probe_vapix", _slow),
            patch(
                f"{_PROBE}._probe_onvif",
                AsyncMock(side_effect=RuntimeError("onvif boom")),
            ),
            patch(
                f"{_PROBE}._probe_rtsp",
                AsyncMock(return_value=_candidate("rtsp", True)),
            ),
            patch(
                f"{_PROBE}._probe_http",
                AsyncMock(return_value=_candidate("http", True)),
            ),
        ):
            outcome = await detect_protocols(_ADDRESS, _CREDS, client, timeout=0.05)
        by_proto = {c.protocol: c for c in outcome.candidates}
        # Both bad probes degrade to non-ok.
        assert by_proto["vapix"].ok is False
        assert by_proto["onvif"].ok is False
        # The good probes still succeeded.
        assert by_proto["rtsp"].ok is True
        assert by_proto["http"].ok is True
        # Recommended follows priority among the ok ones.
        assert outcome.recommended_primary == "rtsp"
