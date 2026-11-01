"""Unit tests for the in-memory Idempotency-Key cache (#70 F-R2, WP-70-C)."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse, Response

from timelapse_manager.web.idempotency import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    IdempotencyCache,
    Outcome,
    StoredResponse,
    cache_for,
    fingerprint_form,
    run_idempotent,
)

_OK = StoredResponse(status_code=200, media_type="application/json", body=b'{"ok":1}')


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestReserveCompleteRelease:
    def test_first_reserve_then_in_progress_then_replay(self) -> None:
        cache = IdempotencyCache()
        key = (1, "r", "k" * 8)
        assert cache.reserve(key, "fp") == (Outcome.RESERVED, None)
        assert cache.reserve(key, "fp") == (Outcome.IN_PROGRESS, None)
        cache.complete(key, _OK)
        assert cache.reserve(key, "fp") == (Outcome.REPLAY, _OK)

    def test_different_fingerprint_is_mismatch(self) -> None:
        cache = IdempotencyCache()
        key = (1, "r", "k" * 8)
        cache.reserve(key, "fp-a")
        assert cache.reserve(key, "fp-b") == (Outcome.MISMATCH, None)
        cache.complete(key, _OK)
        assert cache.reserve(key, "fp-b") == (Outcome.MISMATCH, None)

    def test_release_frees_a_reservation_for_retry(self) -> None:
        cache = IdempotencyCache()
        key = (1, "r", "k" * 8)
        cache.reserve(key, "fp")
        cache.release(key)
        assert len(cache) == 0
        assert cache.reserve(key, "fp") == (Outcome.RESERVED, None)

    def test_release_never_drops_a_completed_entry(self) -> None:
        cache = IdempotencyCache()
        key = (1, "r", "k" * 8)
        cache.reserve(key, "fp")
        cache.complete(key, _OK)
        cache.release(key)
        assert cache.reserve(key, "fp") == (Outcome.REPLAY, _OK)

    def test_complete_after_eviction_is_a_no_op(self) -> None:
        cache = IdempotencyCache(max_entries=1)
        first = (1, "r", "a" * 8)
        cache.reserve(first, "fp")
        cache.reserve((1, "r", "b" * 8), "fp")  # evicts `first`
        cache.complete(first, _OK)
        assert cache.reserve(first, "fp") == (Outcome.RESERVED, None)


class TestScoping:
    def test_users_are_isolated(self) -> None:
        cache = IdempotencyCache()
        cache.reserve((1, "r", "same-key"), "fp")
        cache.complete((1, "r", "same-key"), _OK)
        assert cache.reserve((2, "r", "same-key"), "fp") == (Outcome.RESERVED, None)

    def test_routes_are_isolated(self) -> None:
        cache = IdempotencyCache()
        cache.reserve((1, "offset", "same-key"), "fp")
        assert cache.reserve((1, "export", "same-key"), "fp") == (
            Outcome.RESERVED,
            None,
        )


class TestTtlAndLru:
    def test_entry_expires_after_ttl(self) -> None:
        clock = _Clock()
        cache = IdempotencyCache(ttl_seconds=600, clock=clock)
        key = (1, "r", "k" * 8)
        cache.reserve(key, "fp")
        cache.complete(key, _OK)
        clock.now += 599
        assert cache.reserve(key, "fp")[0] is Outcome.REPLAY
        clock.now += 1
        assert cache.reserve(key, "fp") == (Outcome.RESERVED, None)

    def test_stale_reservation_expires(self) -> None:
        """A request that never completed (worker killed) cannot block forever."""
        clock = _Clock()
        cache = IdempotencyCache(ttl_seconds=10, clock=clock)
        key = (1, "r", "k" * 8)
        cache.reserve(key, "fp")
        clock.now += 10
        assert cache.reserve(key, "fp") == (Outcome.RESERVED, None)

    def test_lru_evicts_least_recently_used(self) -> None:
        cache = IdempotencyCache(max_entries=2)
        a, b, c = ((1, "r", x * 8) for x in "abc")
        for k in (a, b):
            cache.reserve(k, "fp")
            cache.complete(k, _OK)
        cache.reserve(a, "fp")  # touch a: b is now least recently used
        cache.reserve(c, "fp")
        assert len(cache) == 2
        assert cache.reserve(a, "fp")[0] is Outcome.REPLAY
        assert cache.reserve(b, "fp") == (Outcome.RESERVED, None)

    @pytest.mark.parametrize("kwargs", [{"ttl_seconds": 0}, {"max_entries": 0}])
    def test_invalid_bounds_rejected(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            IdempotencyCache(**kwargs)


class TestConcurrency:
    def test_only_one_thread_reserves(self) -> None:
        cache = IdempotencyCache()
        key = (1, "r", "k" * 8)
        barrier = threading.Barrier(16)
        outcomes: list[Outcome] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            outcome, _ = cache.reserve(key, "fp")
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count(Outcome.RESERVED) == 1
        assert outcomes.count(Outcome.IN_PROGRESS) == 15


class TestFingerprint:
    def test_order_independent_and_ignores_csrf(self) -> None:
        a = fingerprint_form({"frame_ids": "1,2", "seconds": "60", "csrf_token": "x"})
        b = fingerprint_form({"seconds": "60", "frame_ids": "1,2", "csrf_token": "y"})
        assert a == b

    def test_value_change_changes_fingerprint(self) -> None:
        a = fingerprint_form({"frame_ids": "1,2", "seconds": "60"})
        b = fingerprint_form({"frame_ids": "1,2", "seconds": "-60"})
        assert a != b

    def test_field_boundaries_are_unambiguous(self) -> None:
        assert fingerprint_form({"a": "bc"}) != fingerprint_form({"ab": "c"})


def _request(headers: dict[str, str] | None = None) -> Any:
    """A minimal stand-in for a Starlette Request: headers plus app.state."""
    return SimpleNamespace(
        headers=headers or {},
        app=SimpleNamespace(state=SimpleNamespace()),
    )


class TestRunIdempotent:
    def test_without_header_runs_handler_every_time(self) -> None:
        calls: list[int] = []
        req = _request()

        def handler() -> Response:
            calls.append(1)
            return JSONResponse({"n": len(calls)})

        for _ in range(2):
            run_idempotent(req, user_id=1, route="r", form={}, handler=handler)
        assert len(calls) == 2
        assert not hasattr(req.app.state, "idempotency_cache")

    def test_replays_first_success(self) -> None:
        calls: list[int] = []
        req = _request({IDEMPOTENCY_HEADER: "abcdef12-3456"})

        def handler() -> Response:
            calls.append(1)
            return JSONResponse({"n": len(calls)}, status_code=202)

        first = run_idempotent(req, user_id=1, route="r", form={}, handler=handler)
        second = run_idempotent(req, user_id=1, route="r", form={}, handler=handler)
        assert calls == [1]
        assert second.status_code == 202
        assert second.body == first.body
        assert second.headers[REPLAYED_HEADER] == "true"
        assert REPLAYED_HEADER.lower() not in first.headers

    def test_error_response_is_not_stored(self) -> None:
        calls: list[int] = []
        req = _request({IDEMPOTENCY_HEADER: "abcdef12-3456"})

        def handler() -> Response:
            calls.append(1)
            return JSONResponse({"error": "no"}, status_code=400)

        run_idempotent(req, user_id=1, route="r", form={}, handler=handler)
        run_idempotent(req, user_id=1, route="r", form={}, handler=handler)
        assert len(calls) == 2
        assert len(cache_for(req)) == 0

    def test_exception_releases_the_key(self) -> None:
        req = _request({IDEMPOTENCY_HEADER: "abcdef12-3456"})

        def boom() -> Response:
            raise RuntimeError("db down")

        with pytest.raises(RuntimeError):
            run_idempotent(req, user_id=1, route="r", form={}, handler=boom)
        retried = run_idempotent(
            req, user_id=1, route="r", form={}, handler=lambda: JSONResponse({})
        )
        assert retried.status_code == 200

    def test_in_progress_duplicate_is_409(self) -> None:
        req = _request({IDEMPOTENCY_HEADER: "abcdef12-3456"})
        cache_for(req).reserve((1, "r", "abcdef12-3456"), fingerprint_form({}))
        resp = run_idempotent(
            req, user_id=1, route="r", form={}, handler=lambda: JSONResponse({})
        )
        assert resp.status_code == 409
        assert b"duplicate request in progress" in bytes(resp.body)

    def test_reused_key_with_other_body_is_422(self) -> None:
        req = _request({IDEMPOTENCY_HEADER: "abcdef12-3456"})
        run_idempotent(
            req, user_id=1, route="r", form={"s": "1"}, handler=lambda: JSONResponse({})
        )
        resp = run_idempotent(
            req, user_id=1, route="r", form={"s": "2"}, handler=lambda: JSONResponse({})
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "bad_key",
        ["short", "x" * 129, "has space-12345", "semi;colon-1234", "ünicode-12345"],
    )
    def test_malformed_key_is_400_and_handler_not_run(self, bad_key: str) -> None:
        calls: list[int] = []
        req = _request({IDEMPOTENCY_HEADER: bad_key})

        def handler() -> Response:
            calls.append(1)
            return JSONResponse({})

        resp = run_idempotent(req, user_id=1, route="r", form={}, handler=handler)
        assert resp.status_code == 400
        assert calls == []
