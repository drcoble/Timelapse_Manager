"""Tests for the shared service scaffolding: Actor, Result, effects, logging."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from timelapse_manager.services._base import (
    Actor,
    Effect,
    Result,
    bound_logger,
    run_effects,
)


class TestActorOf:
    """Actor.of normalises either a web User row or an API principal."""

    def test_from_api_principal_shape(self) -> None:
        """A principal exposes its id as ``user_id``."""
        principal = SimpleNamespace(user_id=7, role="admin")
        actor = Actor.of(principal)
        assert actor == Actor(user_id=7, role="admin")

    def test_from_web_user_shape(self) -> None:
        """A web User row exposes its id as ``id``."""
        user = SimpleNamespace(id=3, role="operator")
        actor = Actor.of(user)
        assert actor == Actor(user_id=3, role="operator")

    def test_prefers_user_id_when_both_present(self) -> None:
        """When a subject carries both, the principal-style id wins."""
        hybrid = SimpleNamespace(user_id=9, id=1, role="admin")
        assert Actor.of(hybrid).user_id == 9

    def test_is_frozen(self) -> None:
        actor = Actor(user_id=1, role="admin")
        try:
            actor.user_id = 2  # type: ignore[misc]
        except AttributeError:
            return
        raise AssertionError("Actor should be frozen")


class TestResult:
    """Result pairs a value with an ordered tuple of effects."""

    def test_defaults_to_no_effects(self) -> None:
        result: Result[int] = Result(value=42)
        assert result.value == 42
        assert result.effects == ()

    def test_carries_effects(self) -> None:
        result = Result(value="x", effects=(Effect.CAPTURE_RECONCILE,))
        assert result.effects == (Effect.CAPTURE_RECONCILE,)


class TestRunEffects:
    """run_effects fires the real triggers and guards absent components."""

    def test_capture_reconcile_wakes_supervisor(self) -> None:
        supervisor = MagicMock()
        ctx = SimpleNamespace(capture_supervisor=supervisor, render_queue=None)
        run_effects((Effect.CAPTURE_RECONCILE,), ctx)  # type: ignore[arg-type]
        supervisor.notify_reconcile.assert_called_once_with()

    def test_render_notify_nudges_queue(self) -> None:
        queue = MagicMock()
        ctx = SimpleNamespace(capture_supervisor=None, render_queue=queue)
        run_effects((Effect.RENDER_NOTIFY,), ctx)  # type: ignore[arg-type]
        queue.notify.assert_called_once_with()

    def test_guards_none_supervisor(self) -> None:
        """A None supervisor is a safe no-op, mirroring the inline guards."""
        ctx = SimpleNamespace(capture_supervisor=None, render_queue=None)
        run_effects((Effect.CAPTURE_RECONCILE,), ctx)  # type: ignore[arg-type]

    def test_guards_none_queue(self) -> None:
        ctx = SimpleNamespace(capture_supervisor=None, render_queue=None)
        run_effects((Effect.RENDER_NOTIFY,), ctx)  # type: ignore[arg-type]

    def test_runs_all_effects_in_order(self) -> None:
        supervisor = MagicMock()
        queue = MagicMock()
        ctx = SimpleNamespace(capture_supervisor=supervisor, render_queue=queue)
        run_effects(
            (Effect.CAPTURE_RECONCILE, Effect.RENDER_NOTIFY),
            ctx,  # type: ignore[arg-type]
        )
        supervisor.notify_reconcile.assert_called_once_with()
        queue.notify.assert_called_once_with()

    def test_empty_effects_is_noop(self) -> None:
        ctx = SimpleNamespace(capture_supervisor=MagicMock(), render_queue=MagicMock())
        run_effects((), ctx)  # type: ignore[arg-type]
        ctx.capture_supervisor.notify_reconcile.assert_not_called()
        ctx.render_queue.notify.assert_not_called()


class TestBoundLogger:
    """bound_logger stamps fixed fields onto every record via the adapter."""

    def test_extra_carries_bound_fields(self) -> None:
        base = logging.getLogger("test.services.bound")
        adapter = bound_logger(base, project_id=5, camera_id=2)
        assert adapter.extra == {"project_id": 5, "camera_id": 2}

    def test_record_receives_bound_fields(self) -> None:
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        base = logging.getLogger("test.services.bound.record")
        base.handlers.clear()
        base.addHandler(_Capture())
        base.setLevel(logging.INFO)
        base.propagate = False

        adapter = bound_logger(base, project_id=11)
        adapter.info("hello")

        assert records and records[0].project_id == 11  # type: ignore[attr-defined]
