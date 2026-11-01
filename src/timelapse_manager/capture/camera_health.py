"""Per-camera capture health (#69, CAM-R6).

Capture outcomes used to drive the offline alert per *project*, and only from
the interval loop. Every deployed project is anchor-only, so the alert could
never fire, and a camera shared by four projects would have raised four alerts.
Health is now kept per *camera*: every outcome on a camera, whichever path
produced it (interval capture, an exact-time anchor attempt, an event-triggered
capture), feeds one :class:`CameraHealth`.

* ``offline_failure_threshold`` consecutive failed attempts **of one project**
  raise **one** ``camera_offline`` alert for the camera; the first success on
  the camera, from any project, emits ``camera_recovered``, which auto-clears
  it. Counting per project (#130) keeps one brief blip from tripping the alert
  on a camera whose projects fire together: four anchor projects that each
  fail once are one failed round, not four failures.
* An authentication failure raises the alert at once (CAM-R5): retrying a
  rejected password cannot succeed, and the operator has to act.
* A *local* failure (see :func:`is_local_failure`) is the server's fault, not
  the camera's. It does not count, and is reported once per streak as an
  informational system event instead.
* A *project* failure (see :func:`classify_failure`), such as the camera
  rejecting one project's PTZ move, says nothing about the camera and is not
  recorded at all (#131).

* The camera's *event listeners* have their own track, kept apart from all of the
  above (#69 CAM-R7, #67-R9). A camera whose events cannot be subscribed to may
  capture perfectly well, so listener failures never touch the offline latch.
  ``offline_failure_threshold`` consecutive subscribe failures of one listener
  raise one ``events_unavailable`` alert for the camera (several projects on it
  share it); the next successful subscribe, once no listener is failing,
  resolves it with ``events_available``.

The book is pure, in-memory state with no I/O. The supervisor owns one, asks it
for a :class:`HealthTransition` on every outcome, and writes the event. Every
call runs on the event loop and decides synchronously, so of several runners
sharing a camera exactly one sees each transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from sqlalchemy.exc import OperationalError

from ..cameras.base import (
    AuthCaptureError,
    PTZError,
    TimeoutCaptureError,
    UnreachableCaptureError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

TransitionKind = Literal[
    "offline", "recovered", "local", "events_unavailable", "events_available"
]
FailureKind = Literal["camera", "auth", "local", "project"]


def is_local_failure(exc: BaseException) -> bool:
    """Return whether ``exc`` is a server-side failure camera health ignores.

    An exception is local when it says so (a ``local`` attribute set to true:
    :class:`~timelapse_manager.cameras.base.CaptureError` raised with
    ``local=True``, or :class:`~timelapse_manager.cameras.base.CameraMissingError`)
    or when it is a SQLite ``OperationalError`` ("database is locked"), which no
    camera can cause.
    """
    if getattr(exc, "local", False) is True:
        return True
    return isinstance(exc, OperationalError)


def _ptz_cause(exc: PTZError) -> BaseException | None:
    """Return what a PTZ failure wraps, past every :class:`PTZError` layer.

    The adapter raises ``PTZError`` from the transport error when the request
    itself failed, and with no cause when the camera answered with an error
    body. The supervisor's ``_apply_ptz`` wraps that once more. ``None`` means
    the camera received the request and refused it.
    """
    current: BaseException | None = exc
    while isinstance(current, PTZError):
        current = current.__cause__
    return current


def classify_failure(exc: BaseException) -> FailureKind:
    """Classify a failed capture for camera health (#69 CAM-R6, #131).

    * ``"local"``: the server failed (:func:`is_local_failure`); not counted.
    * ``"auth"``: the camera rejected the credentials; raises the alert at once.
    * ``"project"``: the camera is up but refused something one project asked
      for -- a PTZ move it rejected (an error body or an error status), or PTZ
      on a camera without it. Not counted against the camera.
    * ``"camera"``: anything else; counts towards the offline threshold.

    A :class:`PTZError` is classified by what it wraps: an unreachable camera or
    a timeout is a camera failure and a rejected login is an auth failure,
    exactly as they would be without PTZ.
    """
    if is_local_failure(exc):
        return "local"
    if isinstance(exc, PTZError):
        cause = _ptz_cause(exc)
        if cause is None:
            return "project"
        if is_local_failure(cause):
            return "local"
        if isinstance(cause, AuthCaptureError):
            return "auth"
        if isinstance(cause, UnreachableCaptureError | TimeoutCaptureError):
            return "camera"
        return "project"
    if isinstance(exc, AuthCaptureError):
        return "auth"
    return "camera"


@dataclass
class CameraHealth:
    """The health of one camera, across every project that uses it.

    :param camera_id: the camera this record tracks.
    :param consecutive_failures: camera failures since the last success, across
        every project. Local and project failures do not count. Informational:
        the alert trips on ``project_streaks``.
    :param offline_alerted: whether a ``camera_offline`` alert stands for the
        current outage. The edge-trigger latch: set by the transition into
        offline, cleared by the first success after it.
    :param last_error: the most recent camera failure's (redacted) text.
    :param local_streak: whether the current run of local failures has already
        been reported. Cleared by the next success.
    :param project_streaks: consecutive camera failures per project (``None``
        for an outcome reported without one) since the camera's last success.
        The offline threshold applies to the longest streak (#130).
    :param listener_streaks: consecutive failed subscribes per project's event
        listener since that listener last subscribed. Apart from every field
        above: it never counts towards ``offline``.
    :param events_alerted: whether an ``events_unavailable`` alert stands for
        the camera (the latch, like ``offline_alerted``).
    """

    camera_id: int
    consecutive_failures: int = 0
    offline_alerted: bool = False
    last_error: str | None = None
    local_streak: bool = False
    project_streaks: dict[int | None, int] = field(default_factory=dict)
    listener_streaks: dict[int, int] = field(default_factory=dict)
    events_alerted: bool = False


@dataclass(frozen=True)
class HealthTransition:
    """A change of camera health that the supervisor turns into one event.

    ``kind`` is ``"offline"`` (raise the camera alert), ``"recovered"`` (resolve
        it), ``"local"`` (the first local failure of a streak),
    ``"events_unavailable"`` (raise the camera's listener alert) or
    ``"events_available"`` (resolve it). ``error`` is the
    redacted failure text (``None`` for a recovery); ``auth_failed`` marks an
    offline alert raised early by an authentication failure.
    ``consecutive_failures`` is the longest single-project streak for an
    ``offline`` transition (the count the threshold was compared with).
    """

    kind: TransitionKind
    camera_id: int
    consecutive_failures: int = 0
    error: str | None = None
    auth_failed: bool = False


class CameraHealthBook:
    """An in-memory registry of :class:`CameraHealth`, keyed by camera id."""

    def __init__(self, offline_failure_threshold: int) -> None:
        """Create an empty book.

        :param offline_failure_threshold: consecutive failed attempts of one
            project that raise the camera's offline alert; values below 1 are
            treated as 1.
        """
        self._threshold = max(1, offline_failure_threshold)
        self._cameras: dict[int, CameraHealth] = {}

    def _entry(self, camera_id: int) -> CameraHealth:
        entry = self._cameras.get(camera_id)
        if entry is None:
            entry = CameraHealth(camera_id=camera_id)
            self._cameras[camera_id] = entry
        return entry

    def health(self, camera_id: int) -> CameraHealth:
        """Return a copy of a camera's health (a fresh record if never seen)."""
        entry = self._cameras.get(camera_id)
        if entry is None:
            return CameraHealth(camera_id)
        return replace(
            entry,
            project_streaks=dict(entry.project_streaks),
            listener_streaks=dict(entry.listener_streaks),
        )

    def is_offline(self, camera_id: int) -> bool:
        """Return whether a ``camera_offline`` alert stands for ``camera_id``."""
        entry = self._cameras.get(camera_id)
        return entry is not None and entry.offline_alerted

    def seed(self, alerted_camera_ids: Iterable[int]) -> None:
        """Latch ``offline_alerted`` for cameras whose alert is still active.

        Called once when the supervisor starts, with the cameras that have an
        active camera-scoped ``camera_offline`` alert in the database. Without
        it, a restart during an outage forgets the alert, the recovery is never
        written, and the alert stands forever. Seeding only ever adds latches.
        """
        for camera_id in alerted_camera_ids:
            self._entry(camera_id).offline_alerted = True

    def seed_events(self, alerted_camera_ids: Iterable[int]) -> None:
        """Latch ``events_alerted`` for cameras whose listener alert is still active.

        The listener-track counterpart of :meth:`seed`: without it a restart
        during an events outage forgets the alert and the next successful
        subscribe never resolves it.
        """
        for camera_id in alerted_camera_ids:
            self._entry(camera_id).events_alerted = True

    def is_events_unavailable(self, camera_id: int) -> bool:
        """Return whether an ``events_unavailable`` alert stands for ``camera_id``."""
        entry = self._cameras.get(camera_id)
        return entry is not None and entry.events_alerted

    def record_listener_failure(
        self, camera_id: int, project_id: int, *, error: str
    ) -> HealthTransition | None:
        """Record a listener that failed to subscribe; return the alert it raises.

        One project's listener failing ``offline_failure_threshold`` times in a
        row, with no subscribe in between, raises ``events_unavailable`` for the
        camera. A project the camera's other listeners share raises nothing more
        while the alert stands.
        """
        entry = self._entry(camera_id)
        streak = entry.listener_streaks.get(project_id, 0) + 1
        entry.listener_streaks[project_id] = streak
        if entry.events_alerted or streak < self._threshold:
            return None
        entry.events_alerted = True
        return HealthTransition(
            kind="events_unavailable",
            camera_id=camera_id,
            consecutive_failures=streak,
            error=error,
        )

    def record_listener_success(
        self, camera_id: int, project_id: int
    ) -> HealthTransition | None:
        """Record a listener that subscribed; return the resolution it causes.

        The alert resolves once the camera has no listener left in a failing
        streak, so one project subscribing cannot hide another's outage.
        """
        entry = self._entry(camera_id)
        entry.listener_streaks.pop(project_id, None)
        if not entry.events_alerted or entry.listener_streaks:
            return None
        entry.events_alerted = False
        return HealthTransition(kind="events_available", camera_id=camera_id)

    def forget_listener(self, camera_id: int, project_id: int) -> None:
        """Drop a stopped listener's failure streak (it restarts or is gone)."""
        entry = self._cameras.get(camera_id)
        if entry is not None:
            entry.listener_streaks.pop(project_id, None)

    def resolve_events(self, camera_id: int) -> HealthTransition | None:
        """Resolve a camera's ``events_unavailable`` alert, if one stands.

        For the last listener on a camera being removed: nothing is left to
        subscribe and so to resolve it.
        """
        entry = self._cameras.get(camera_id)
        if entry is None or not entry.events_alerted or entry.listener_streaks:
            return None
        entry.events_alerted = False
        return HealthTransition(kind="events_available", camera_id=camera_id)

    def record_success(self, camera_id: int) -> HealthTransition | None:
        """Record a successful capture; return ``recovered`` after an outage."""
        entry = self._entry(camera_id)
        entry.consecutive_failures = 0
        entry.project_streaks.clear()
        entry.last_error = None
        entry.local_streak = False
        if not entry.offline_alerted:
            return None
        entry.offline_alerted = False
        return HealthTransition(kind="recovered", camera_id=camera_id)

    def record_failure(
        self,
        camera_id: int,
        *,
        error: str,
        auth: bool = False,
        local: bool = False,
        project_id: int | None = None,
    ) -> HealthTransition | None:
        """Record a failed capture and return the transition it causes, if any.

        The camera goes offline when one project has failed
        ``offline_failure_threshold`` times in a row with no success on the
        camera in between (#130). Several projects failing once each, as when
        four anchors on one camera hit the same brief blip, is one failed round
        and does not trip it; a real outage does, because every project keeps
        failing its own retries.

        :param error: the failure text, already redacted.
        :param auth: the camera rejected the credentials: raise the alert now
            instead of waiting for the threshold.
        :param local: a server-side failure: not counted; the first of a streak
            returns a ``local`` transition.
        :param project_id: the project whose attempt failed.
        """
        entry = self._entry(camera_id)
        if local:
            if entry.local_streak:
                return None
            entry.local_streak = True
            return HealthTransition(
                kind="local",
                camera_id=camera_id,
                consecutive_failures=entry.consecutive_failures,
                error=error,
            )
        entry.consecutive_failures += 1
        streak = entry.project_streaks.get(project_id, 0) + 1
        entry.project_streaks[project_id] = streak
        entry.last_error = error
        if entry.offline_alerted:
            return None
        longest = max(entry.project_streaks.values())
        if not auth and longest < self._threshold:
            return None
        entry.offline_alerted = True
        return HealthTransition(
            kind="offline",
            camera_id=camera_id,
            consecutive_failures=longest,
            error=error,
            auth_failed=auth,
        )

    def rearm(self, transition: HealthTransition) -> None:
        """Undo a transition whose event could not be written.

        The latch moved when the transition was decided. If the event write then
        fails, moving it back lets the next outcome on the camera try again:
        the next failure re-raises a lost ``offline``, the next success re-emits
        a lost ``recovered``, and the next local failure re-reports a lost
        ``local``.
        """
        entry = self._entry(transition.camera_id)
        if transition.kind == "offline":
            entry.offline_alerted = False
        elif transition.kind == "recovered":
            entry.offline_alerted = True
        elif transition.kind == "events_unavailable":
            entry.events_alerted = False
        elif transition.kind == "events_available":
            entry.events_alerted = True
        else:
            entry.local_streak = False
