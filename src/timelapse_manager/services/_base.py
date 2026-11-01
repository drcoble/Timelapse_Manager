"""Shared scaffolding for application services.

A service function performs one write against a caller-supplied session and
returns a :class:`Result` pairing the produced value with the post-commit
:class:`Effect`\\ s the caller must run *after* it commits. The web and API
layers call the same service, commit, then invoke :func:`run_effects` to fire
the real background triggers (waking the capture supervisor, nudging the render
worker) that the request handlers previously called inline.

The :class:`Actor` captures who is performing the write, normalised from either
a web ``User`` row or an API principal so a service never has to care which
surface it was called from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from ..runtime import AppContext

T = TypeVar("T")


@dataclass(frozen=True)
class Actor:
    """The user on whose behalf a service performs a write.

    :param user_id: the acting user's id, recorded as the actor on audit events.
    :param role: the caller's role (``"admin"``/``"operator"``/...).
    """

    user_id: int
    role: str

    @classmethod
    def of(cls, subject: Any) -> Actor:
        """Build an :class:`Actor` from a web ``User`` row or an API principal.

        A web ``User`` exposes its id as ``.id``; an API principal exposes it as
        ``.user_id``. Both expose ``.role``. This reads whichever id attribute is
        present so a service can be handed either without the caller adapting it.
        """
        user_id = getattr(subject, "user_id", None)
        if user_id is None:
            user_id = subject.id
        return cls(user_id=int(user_id), role=str(subject.role))


class Effect(Enum):
    """A post-commit side effect a service asks the caller to run.

    Services perform no background signalling themselves: they return the
    effects a write implies, and the caller runs them via :func:`run_effects`
    once the transaction has committed (so the background component observes the
    committed row). The members mirror the inline triggers the request handlers
    ran before services existed.
    """

    #: Wake the capture supervisor so it reconciles its running loops to the DB
    #: (a project was created, edited, paused, resumed, or deleted).
    CAPTURE_RECONCILE = "capture_reconcile"
    #: Nudge the render worker so it picks up newly queued/updated work without
    #: waiting out its poll interval.
    RENDER_NOTIFY = "render_notify"


@dataclass
class Result(Generic[T]):
    """A service's return value paired with its post-commit effects.

    :param value: the value the service produced (an ORM row, an id, ...).
    :param effects: the effects the caller must run after committing, in order.
    """

    value: T
    effects: tuple[Effect, ...] = ()


def run_effects(effects: tuple[Effect, ...], ctx: AppContext) -> None:
    """Run each post-commit effect against the live application context.

    Called by the caller *after* ``session.commit()``. Each effect is guarded on
    the presence of the component it drives -- exactly as the inline request
    handlers guarded the supervisor/queue -- so running effects on a context
    whose background components were never constructed is a safe no-op.

    Safe to call from any thread: on the event loop (an ``async def`` route) or
    from a sync route in FastAPI's threadpool. The notifications it fires
    (:meth:`CaptureSupervisor.notify_reconcile`, :meth:`RenderQueue.notify`)
    marshal themselves onto their component's event loop (#66 L5).
    """
    for effect in effects:
        if effect is Effect.CAPTURE_RECONCILE:
            supervisor = ctx.capture_supervisor
            if supervisor is not None:
                supervisor.notify_reconcile()
        elif effect is Effect.RENDER_NOTIFY:
            queue = ctx.render_queue
            if queue is not None:
                queue.notify()


def bound_logger(
    logger: logging.Logger, **fields: Any
) -> logging.LoggerAdapter[logging.Logger]:
    """Return a logger adapter that stamps ``fields`` onto every record.

    Lets a service attach stable context (``project_id``, ``camera_id``) once and
    have every subsequent log line carry it as structured extras -- the JSON
    formatter emits arbitrary record attributes, so these appear as fields.
    """
    return logging.LoggerAdapter(logger, dict(fields))
