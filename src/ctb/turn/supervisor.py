"""Lease-enforced reconciliation of one poller task per bound session."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, cast

from ctb.conductor.client import ConductorClient
from ctb.conductor.errors import AuthFatal
from ctb.db.backup import create_backup
from ctb.db.connection import Database
from ctb.db.repo import lease, sessions, transcript, wizard
from ctb.logging import get_logger
from ctb.turn.session_poller import ActionSink, SessionPoller
from ctb.turn.state import Evidence

__all__ = [
    "MAX_RESTART_BACKOFF_S",
    "RECONCILE_INTERVAL_S",
    "PollerFactory",
    "Supervisor",
]

_log = get_logger(__name__)

RECONCILE_INTERVAL_S: Final = lease.HEARTBEAT_MS / 1000.0
MAX_RESTART_BACKOFF_S: Final = 60.0
MAINTENANCE_INTERVAL_S: Final = 24 * 60 * 60.0

type Sleeper = Callable[[float], Awaitable[None]]
type PollerFactory = Callable[[str], SessionPoller]


@dataclass(slots=True)
class _Restart:
    failures: int = 0
    not_before: float = 0.0


class Supervisor:
    """Own the singleton lease and reconcile DB bindings to asyncio tasks.

    A supervisor never creates a poller before acquiring the 15-second lease.
    Losing a heartbeat cancels all pollers immediately. A crashed session task
    is isolated from its peers and restarted with exponential backoff.
    """

    def __init__(
        self,
        client: ConductorClient,
        db: Database,
        *,
        action_sink: ActionSink | None = None,
        poller_factory: PollerFactory | None = None,
        holder: str | None = None,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.db = db
        self.action_sink = action_sink
        self.holder = holder or lease.instance_id()
        self._sleep = sleep
        self._clock = clock
        self._poller_factory = poller_factory or self._make_poller
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pollers: dict[str, SessionPoller] = {}
        self._restart: dict[str, _Restart] = {}
        self._lease: lease.Lease | None = None
        self._runner: asyncio.Task[None] | None = None
        self._running = False
        self._auth_fatal = False
        self._auth_notified = False
        self._next_maintenance_at = 0.0

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def session_ids(self) -> frozenset[str]:
        return frozenset(self._tasks)

    @property
    def has_lease(self) -> bool:
        return self._lease is not None

    @property
    def auth_fatal(self) -> bool:
        # A second in-flight request can prove that a 403 was a transient proxy
        # rejection after one poller has already raised ``AuthFatal``. The
        # client clears its counter on that 2xx; clear the supervisor latch as
        # well or every poller stays cancelled until the next deployment.
        if self._auth_fatal and self.client.auth_failures == 0:
            self._auth_fatal = False
            self._auth_notified = False
            _log.info("supervisor.auth_recovered")
        return self._auth_fatal or self.client.auth_failures > 0

    def _make_poller(self, session_id: str) -> SessionPoller:
        return SessionPoller(
            self.client,
            self.db,
            session_id,
            action_sink=self.action_sink,
        )

    async def start(self) -> None:
        """Start the reconciliation loop. Idempotent."""
        if self._runner is not None and not self._runner.done():
            return
        self._running = True
        self._runner = asyncio.create_task(self.run(), name="ctb-supervisor")

    async def stop(self) -> None:
        """Stop every poller, release our lease, and wait for clean shutdown."""
        self._running = False
        runner = self._runner
        self._runner = None
        current = asyncio.current_task()
        if runner is not None and runner is not current and not runner.done():
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        await self._cancel_all()
        await self._release_lease()

    async def run(self) -> None:
        """Acquire, heartbeat and reconcile until cancelled or stopped."""
        self._running = True
        try:
            while self._running:
                try:
                    acquired = await self._renew_or_acquire()
                    if acquired and not self.auth_fatal:
                        await self._reconcile_with_lease()
                    else:
                        await self._cancel_all()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # DB/lease failures are fail-closed: no lease, no pollers.
                    _log.error(
                        "supervisor.loop_failed",
                        holder=self.holder,
                        error=repr(exc),
                    )
                    self._lease = None
                    await self._cancel_all()
                await self._sleep(RECONCILE_INTERVAL_S)
        finally:
            await self._cancel_all()
            await self._release_lease()

    async def reconcile_once(self) -> bool:
        """One deterministic acquire/heartbeat/reconcile pass.

        Returns whether this instance held the lease for the pass.
        """
        acquired = await self._renew_or_acquire()
        if not acquired or self.auth_fatal:
            await self._cancel_all()
            return acquired
        await self._reconcile_with_lease()
        return True

    async def dispatch(self, session_id: str, evidence: Evidence) -> bool:
        """Send user-driven evidence to a live poller without racing its tick.

        ``False`` means the session is not currently supervised. Callers may
        retry after the next five-second reconciliation pass.
        """
        poller = self._pollers.get(session_id)
        if poller is None or self._lease is None or self.auth_fatal:
            return False
        await poller.dispatch(evidence)
        return True

    async def _renew_or_acquire(self) -> bool:
        if self._lease is None:
            current = await lease.acquire(self.db, holder=self.holder)
        else:
            current = await lease.heartbeat(self.db, holder=self.holder)
        if current is None:
            if self._lease is not None:
                _log.error("supervisor.lease_lost", holder=self.holder)
            self._lease = None
            await self._cancel_all()
            return False
        first = self._lease is None
        self._lease = current
        if first:
            _log.info("supervisor.lease_acquired", holder=self.holder)
        return True

    async def _reconcile_with_lease(self) -> None:
        """Reconcile only after the caller proved lease ownership."""
        if self._lease is None:  # pragma: no cover - defensive invariant
            await self._cancel_all()
            return
        await self._run_maintenance_if_due()

        bound = {row.id for row in await sessions.list_bound(self.db)}
        for session_id in tuple(self._tasks):
            task = self._tasks[session_id]
            if session_id not in bound:
                await self._drop(session_id, cancel=True)
                self._restart.pop(session_id, None)
                continue
            if task.done():
                await self._observe_completion(session_id, task)

        if self.auth_fatal:
            await self._cancel_all()
            return

        now = self._clock()
        for session_id in sorted(bound):
            if session_id in self._tasks:
                continue
            restart = self._restart.get(session_id)
            if restart is not None and now < restart.not_before:
                continue
            self._spawn(session_id)

    async def _run_maintenance_if_due(self) -> None:
        now = self._clock()
        if now < self._next_maintenance_at:
            return
        self._next_maintenance_at = now + MAINTENANCE_INTERVAL_S
        try:
            removed_transcript = await transcript.prune(self.db)
            removed_wizards = await wizard.prune_expired(self.db)
            backup = await create_backup(self.db)
        except Exception as exc:
            # Retention is important, but a transient maintenance failure must
            # not stop transcript delivery. Retry at the next reconcile.
            self._next_maintenance_at = now
            _log.error("supervisor.maintenance_failed", error=repr(exc))
            return
        _log.info(
            "supervisor.maintenance_complete",
            transcript_rows=removed_transcript,
            wizard_rows=removed_wizards,
            backup=str(backup),
        )

    def _spawn(self, session_id: str) -> None:
        if self._lease is None:
            return
        poller = self._poller_factory(session_id)
        task = asyncio.create_task(poller.run(), name=f"ctb-session:{session_id}")
        self._pollers[session_id] = poller
        self._tasks[session_id] = task
        _log.info("supervisor.poller_started", session_id=session_id)

    async def _observe_completion(
        self, session_id: str, task: asyncio.Task[None]
    ) -> None:
        self._tasks.pop(session_id, None)
        self._pollers.pop(session_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self._restart.pop(session_id, None)
            return
        if isinstance(error, AuthFatal):
            self._auth_fatal = True
            _log.error("supervisor.auth_fatal", session_id=session_id)
            await self._cancel_all()
            notify = getattr(self.action_sink, "auth_fatal", None)
            if callable(notify) and not self._auth_notified:
                self._auth_notified = True
                try:
                    await cast(Callable[[str], Awaitable[None]], notify)(session_id)
                except Exception as exc:
                    _log.error(
                        "supervisor.auth_notice_failed",
                        session_id=session_id,
                        error=repr(exc),
                    )
            return

        restart = self._restart.setdefault(session_id, _Restart())
        restart.failures += 1
        delay = min(MAX_RESTART_BACKOFF_S, float(2 ** (restart.failures - 1)))
        restart.not_before = self._clock() + delay
        _log.error(
            "supervisor.poller_crashed",
            session_id=session_id,
            failures=restart.failures,
            restart_in_s=delay,
            error=repr(error),
        )

    async def _drop(self, session_id: str, *, cancel: bool) -> None:
        poller = self._pollers.pop(session_id, None)
        if poller is not None:
            poller.request_stop()
        task = self._tasks.pop(session_id, None)
        if task is None:
            return
        if cancel and not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cancel_all(self) -> None:
        session_ids = tuple(self._tasks)
        if not session_ids:
            return
        for session_id in session_ids:
            poller = self._pollers.get(session_id)
            if poller is not None:
                poller.request_stop()
            task = self._tasks.get(session_id)
            if task is not None and not task.done():
                task.cancel()
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        self._pollers.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _release_lease(self) -> None:
        current, self._lease = self._lease, None
        if current is None:
            return
        try:
            await lease.release(self.db, holder=self.holder)
        except Exception as exc:
            _log.warning(
                "supervisor.lease_release_failed",
                holder=self.holder,
                error=repr(exc),
            )
