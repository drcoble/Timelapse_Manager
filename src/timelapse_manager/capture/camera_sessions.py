"""Per-camera capture sessions: cached configuration, a capture slot, PTZ state.

Every capture used to start with a thread hop that read the camera row (and,
for an inheriting camera, the default-credentials row) and decrypted the
password, then rebuilt everything the adapter had resolved last time (#69
CAM-P1). A camera's configuration changes rarely, so the
:class:`CameraSessionRegistry` keeps one :class:`CameraSession` per camera:

* **Configuration and credentials.** The result of the one-hop loader
  (:func:`~timelapse_manager.capture.camera_config.load_capture_config`) plus
  the credentials it resolves to, decrypted once. A cache hit costs no thread
  hop, no database read and no decrypt. A credential that cannot be decrypted
  is reported as a named *local* failure (CAM-R8) and never cached.
* **Adapter caches.** :class:`TtlCache` stores the adapters fill and read: the
  ONVIF profile token and media URIs, and the VAPIX stream-profile snapshot
  knobs. They expire after an hour and are dropped when a capture through them
  fails.
* **HTTP auth state.** A :class:`~timelapse_manager.cameras.http_auth.CameraAuthState`
  that remembers the scheme the camera negotiated (Digest, with its nonce and
  nonce count) so a capture sends it up front: one request per snapshot instead
  of three (CAM-P1). It carries the camera's Basic-over-HTTP policy (CAM-S1)
  and holds no password. It lives and dies with the session, so a config change
  or an authentication failure (both drop the session) starts it afresh.
* **Invalidation.** Explicit (:meth:`CameraSessionRegistry.invalidate` after an
  authentication failure) and by version: every reconcile pass reports each
  camera's :func:`~timelapse_manager.capture.camera_config.config_version`
  fingerprint to :meth:`CameraSessionRegistry.observe`, which drops any session
  whose fingerprint changed and any camera no longer in use. Camera edits and
  deletes wake the reconcile loop, so an edit reaches the next capture at once;
  any other change (the default credentials, a direct SQL edit) within one
  reconcile interval.

Per-camera *runtime* state lives apart from the cached configuration, so a
reload never resets it:

* **Capture slot (CAM-R4).** At most two captures run on one camera at a time,
  or one when any project on it uses PTZ (so one project cannot move the camera
  while another captures). The wait for a slot is bounded and sits outside the
  capture budget; a capture that cannot get a slot fails as a *local* "camera
  busy" error that camera health ignores.
* **PTZ position.** The last position a move confirmed, so an unchanged
  position is not re-sent within :data:`PTZ_POSITION_TRUST_SECONDS`.

Everything here runs on the event loop; the only blocking work, the load, goes
through the supervisor's executor seam.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Hashable,
    Iterable,
    Mapping,
)
from dataclasses import dataclass, field
from typing import Any

from ..cameras.base import CameraMissingError, OtherCaptureError
from ..cameras.http_auth import CameraAuthState
from ..cameras.registry import auth_state_for
from ..security.credential_binding import (
    current_allowed_subnets,
    default_still_bound,
    default_withheld,
    uses_bound_default,
)
from ..security.crypto import CredentialDecryptError
from .camera_config import CaptureCameraConfig, resolve_capture_credentials

logger = logging.getLogger(__name__)

#: How long an adapter-resolved value (ONVIF URI, VAPIX knobs) is trusted.
ADAPTER_CACHE_TTL_SECONDS = 3600.0
#: How long a confirmed PTZ position is trusted before the move is re-sent.
PTZ_POSITION_TRUST_SECONDS = 600.0
#: How soon a session whose default login was withheld asks the binding
#: question again (#269, #284). A name that does not resolve is not cached by the
#: resolution cache, so without this every use would be a real DNS lookup.
WITHHELD_RECHECK_SECONDS = 15.0
#: Concurrent captures on one camera with no PTZ project.
SHARED_SLOT_CAPACITY = 2

ConfigLoader = Callable[[int], CaptureCameraConfig | None]
BlockingRunner = Callable[..., Awaitable[Any]]


def ptz_position_of(project: Any) -> tuple[object, ...] | None:
    """Return a comparable key for the PTZ position a project asks for.

    Raw pan/tilt/zoom (any one set) wins over a named preset, the precedence
    the capture path and the project form use. ``None`` means the project does
    not position the camera. Accepts a capture target or a ``Project`` row.
    """
    pan = getattr(project, "ptz_pan", None)
    tilt = getattr(project, "ptz_tilt", None)
    zoom = getattr(project, "ptz_zoom", None)
    if pan is not None or tilt is not None or zoom is not None:
        return ("raw", pan, tilt, zoom)
    preset = getattr(project, "ptz_preset", None)
    if preset is not None:
        return ("preset", preset)
    return None


def describe_ptz_position(position: tuple[object, ...] | None) -> str:
    """Return a short human description of a :func:`ptz_position_of` key."""
    if position is None:
        return "no PTZ position"
    if position[0] == "preset":
        return f"preset {position[1]!r}"
    _, pan, tilt, zoom = position
    return f"pan {pan}, tilt {tilt}, zoom {zoom}"


class TtlCache:
    """A small key-value store whose entries expire (an ``AdapterCache``)."""

    def __init__(
        self,
        ttl_seconds: float = ADAPTER_CACHE_TTL_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an empty cache whose entries live ``ttl_seconds``."""
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[Hashable, tuple[object, float]] = {}

    def get(self, key: Hashable) -> object | None:
        """Return the value for ``key``, or ``None`` when missing or expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires = entry
        if self._clock() >= expires:
            del self._entries[key]
            return None
        return value

    def put(self, key: Hashable, value: object) -> None:
        """Store ``value`` under ``key`` for the cache's TTL."""
        self._entries[key] = (value, self._clock() + self._ttl)

    def drop(self, key: Hashable) -> None:
        """Forget ``key``."""
        self._entries.pop(key, None)

    def clear(self) -> None:
        """Forget every entry."""
        self._entries.clear()

    def __len__(self) -> int:
        """Return the number of stored (possibly expired) entries."""
        return len(self._entries)


@dataclass
class CameraSession:
    """The cached state a capture on one camera is built from.

    :param camera_id: the camera.
    :param config_version: the fingerprint of the rows ``config`` was read from.
    :param config: the detached camera configuration (plain values).
    :param credentials: the resolved, decrypted ``(username, password)``, or
        ``None`` for a camera that uses none. Kept out of ``repr``.
    :param uri_cache: ONVIF profile token and media URIs.
    :param knob_cache: VAPIX stream-profile snapshot knobs, keyed by stream.
    :param auth_state: the camera's HTTP auth negotiation state; pass it to
        ``build_adapter(auth_state=...)``.
    """

    camera_id: int
    config_version: str | None
    config: CaptureCameraConfig
    credentials: tuple[str, str] | None = field(repr=False)
    uri_cache: TtlCache = field(default_factory=TtlCache, repr=False)
    knob_cache: TtlCache = field(default_factory=TtlCache, repr=False)
    auth_state: CameraAuthState = field(default_factory=CameraAuthState, repr=False)
    #: When the default login's binding was last decided for this session (its
    #: load, or the last re-check), on the registry's clock.
    binding_decided_at: float = 0.0


class _CameraRuntime:
    """Per-camera state that outlives any one cached configuration."""

    def __init__(self) -> None:
        self.exclusive = False
        self.semaphore = asyncio.Semaphore(SHARED_SLOT_CAPACITY)
        self.position: Hashable | None = None
        self.position_at = 0.0

    def semaphore_for(self, exclusive: bool) -> asyncio.Semaphore:
        """Return the slot semaphore for the camera's current PTZ mode.

        A mode change (a project on the camera gained or lost a PTZ position)
        starts a new semaphore; a capture holding the old one releases it.
        """
        if exclusive != self.exclusive:
            self.exclusive = exclusive
            self.semaphore = asyncio.Semaphore(1 if exclusive else SHARED_SLOT_CAPACITY)
        return self.semaphore


class CameraSessionRegistry:
    """Caches one :class:`CameraSession` per camera and owns its runtime state."""

    def __init__(
        self,
        loader: ConfigLoader,
        run_blocking: BlockingRunner,
        *,
        clock: Callable[[], float] = time.monotonic,
        allowed_subnets: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        """Create an empty registry; performs no I/O.

        :param loader: the synchronous one-hop camera loader (the supervisor's
            ``_load_capture_config``); returns ``None`` for a deleted camera.
        :param run_blocking: the supervisor's executor seam, used for the load.
        :param clock: monotonic time source (TTL and PTZ trust).
        :param allowed_subnets: the live ``ssrf.allowed_private_subnets`` policy,
            read each time a session holding the default login is used (#223).
            ``None`` reads the policy the loader's credential binding reads.
        """
        self._allowed_subnets = allowed_subnets
        self._loader = loader
        self._run_blocking = run_blocking
        self._clock = clock
        self._sessions: dict[int, CameraSession] = {}
        self._load_locks: dict[int, asyncio.Lock] = {}
        self._generations: dict[int, int] = {}
        self._known_versions: dict[int, str | None] = {}
        self._runtimes: dict[int, _CameraRuntime] = {}
        self._ptz_cameras: frozenset[int] = frozenset()
        self.loads = 0

    # -- configuration cache -------------------------------------------------

    def cached(self, camera_id: int) -> CameraSession | None:
        """Return the cached session for ``camera_id`` without loading."""
        return self._sessions.get(camera_id)

    async def get(self, camera_id: int) -> CameraSession:
        """Return the camera's session, loading it off the loop on a miss.

        Concurrent misses on one camera (four anchors firing together on a
        cold cache) wait for one load instead of each making their own.

        :raises CameraMissingError: the camera no longer exists (local).
        :raises OtherCaptureError: (``local=True``) a stored credential cannot
            be decrypted with the current key; the message says so (CAM-R8).
            Nothing is cached, so the next capture tries again.
        """
        session = self._sessions.get(camera_id)
        if session is not None and await self._binding_holds(session):
            return session
        lock = self._load_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            session = self._sessions.get(camera_id)
            if session is not None and await self._binding_holds(session):
                return session
            return await self._load_session(camera_id)

    async def _binding_holds(self, session: CameraSession) -> bool:
        """Whether a cached session may keep the credentials it was built with.

        Always true unless the session holds the **global default** (#248,
        #223): that was released because the camera's hosts were inside
        ``ssrf.allowed_private_subnets`` when it loaded, so it is re-decided for
        the policy and DNS answer now, from the shared resolution cache the
        connections dial from. When it no longer holds the session is dropped and
        the caller reloads, which withholds the default. Only sessions using the
        default pay for this, and only a cache lookup per use; the reconcile
        fingerprint stays untouched, so no reconcile pass invalidates anything.
        """
        if default_withheld(session.config):
            return await self._withheld_verdict_holds(session)
        if not uses_bound_default(session.config):
            return True
        if await default_still_bound(
            session.config, (self._allowed_subnets or current_allowed_subnets)()
        ):
            return True
        logger.warning(
            "camera session dropped camera=%s: its hosts are no longer inside "
            "the allowed private subnets, so the default login is withheld",
            session.camera_id,
        )
        if self._sessions.get(session.camera_id) is session:
            self.invalidate(session.camera_id)
        return False

    async def _withheld_verdict_holds(self, session: CameraSession) -> bool:
        """Whether a session built **without** the default may keep going without it.

        The mirror of the check above (#269): the loader withheld the default
        (a host outside the subnets, or a name that did not resolve at that
        moment), and a session is cached for as long as the camera's rows are
        unchanged, so without this the verdict would stand until an edit or a
        restart. Each use asks the same question again; once the default would
        be bound the session is dropped and the caller reloads with it. Costs a
        shared-cache lookup per use, only for these cameras. Failed lookups are
        not cached, so the question is asked at most once per
        :data:`WITHHELD_RECHECK_SECONDS` per session (#284): a camera whose name
        does not resolve costs one lookup per interval, not one per use.
        """
        now = self._clock()
        if now - session.binding_decided_at < WITHHELD_RECHECK_SECONDS:
            return True
        session.binding_decided_at = now
        if not await default_still_bound(
            session.config, (self._allowed_subnets or current_allowed_subnets)()
        ):
            return True
        logger.info(
            "camera session dropped camera=%s: its hosts are inside the allowed "
            "private subnets now, so the default login is released",
            session.camera_id,
        )
        if self._sessions.get(session.camera_id) is session:
            self.invalidate(session.camera_id)
        return False

    async def _load_session(self, camera_id: int) -> CameraSession:
        """Load, build and (when still current) cache one camera's session."""
        generation = self._generations.get(camera_id, 0)
        try:
            loaded = await self._run_blocking(self._load, camera_id)
        except CredentialDecryptError as exc:
            raise OtherCaptureError(str(exc), local=True) from exc
        if loaded is None:
            raise CameraMissingError(camera_id)
        config, credentials = loaded
        self.loads += 1
        session = CameraSession(
            camera_id=camera_id,
            config_version=getattr(config, "config_version", None),
            config=config,
            credentials=credentials,
            uri_cache=TtlCache(clock=self._clock),
            knob_cache=TtlCache(clock=self._clock),
            auth_state=auth_state_for(config),
            binding_decided_at=self._clock(),
        )
        if self._may_store(camera_id, generation, session):
            self._sessions[camera_id] = session
        return session

    def _load(
        self, camera_id: int
    ) -> tuple[CaptureCameraConfig, tuple[str, str] | None] | None:
        """Read the camera and resolve its credentials. Runs in a worker thread."""
        config = self._loader(camera_id)
        if config is None:
            return None
        return config, resolve_capture_credentials(config)

    def _may_store(
        self, camera_id: int, generation: int, session: CameraSession
    ) -> bool:
        """Whether a just-loaded session is still current enough to cache.

        Not when it was invalidated while loading, nor when the last reconcile
        pass saw a different version than the one loaded (the load read the row
        before an edit committed). The capture that loaded it still uses it.
        """
        if self._generations.get(camera_id, 0) != generation:
            return False
        known = self._known_versions.get(camera_id)
        return known is None or known == session.config_version

    def invalidate(self, camera_id: int) -> None:
        """Drop a camera's cached session; the next capture reloads it."""
        self._sessions.pop(camera_id, None)
        self._generations[camera_id] = self._generations.get(camera_id, 0) + 1

    def invalidate_all(self) -> None:
        """Drop every cached session."""
        for camera_id in list(self._sessions) + list(self._generations):
            self.invalidate(camera_id)

    def observe(self, versions: Mapping[int, str | None]) -> None:
        """Reconcile the cache with the versions a reconcile pass just read.

        ``versions`` maps every camera an active project uses to its current
        :func:`~timelapse_manager.capture.camera_config.config_version`. A
        session whose version differs is dropped, and so is one for a camera
        that is no longer in use (deleted, or its projects paused), so no
        session outlives a check.
        """
        self._known_versions = dict(versions)
        for camera_id, session in list(self._sessions.items()):
            in_use = camera_id in versions
            if not in_use or versions[camera_id] != session.config_version:
                logger.debug("camera session invalidated camera=%s", camera_id)
                self.invalidate(camera_id)

    # -- capture slot ----------------------------------------------------------

    def set_ptz_cameras(self, camera_ids: Iterable[int]) -> None:
        """Record which cameras have a project with a PTZ position."""
        self._ptz_cameras = frozenset(camera_ids)

    def _runtime(self, camera_id: int) -> _CameraRuntime:
        runtime = self._runtimes.get(camera_id)
        if runtime is None:
            runtime = _CameraRuntime()
            self._runtimes[camera_id] = runtime
        return runtime

    @contextlib.asynccontextmanager
    async def capture_slot(
        self, camera_id: int, *, exclusive: bool, wait_seconds: float
    ) -> AsyncIterator[None]:
        """Hold one of the camera's capture slots for the body of the block.

        Exclusive (one capture at a time) when ``exclusive`` is set or any
        project on the camera has a PTZ position; otherwise shared by up to
        :data:`SHARED_SLOT_CAPACITY` captures. Waiting is FIFO and bounded.

        :raises OtherCaptureError: (``local=True``) no slot came free within
            ``wait_seconds``.
        """
        runtime = self._runtime(camera_id)
        semaphore = runtime.semaphore_for(exclusive or camera_id in self._ptz_cameras)
        try:
            async with asyncio.timeout(wait_seconds):
                await semaphore.acquire()
        except TimeoutError:
            raise OtherCaptureError(
                f"camera busy: no capture slot came free within {wait_seconds:.0f}s",
                local=True,
            ) from None
        try:
            yield
        finally:
            semaphore.release()

    # -- PTZ position ------------------------------------------------------------

    def position_confirmed(self, camera_id: int, position: Hashable) -> bool:
        """Whether ``position`` is where a recent confirmed move left the camera."""
        runtime = self._runtimes.get(camera_id)
        if runtime is None or runtime.position != position:
            return False
        return self._clock() - runtime.position_at < PTZ_POSITION_TRUST_SECONDS

    def note_position(self, camera_id: int, position: Hashable | None) -> None:
        """Record the position a move just confirmed (``None``: unknown)."""
        runtime = self._runtime(camera_id)
        runtime.position = position
        runtime.position_at = self._clock()
