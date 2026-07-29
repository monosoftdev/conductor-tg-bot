"""Lease-enforced reconciliation of one poller task per bound session.

Under multi-tenancy the reconcile query is the only cross-tenant read in the
turn machinery: it runs on the **system** pool and joins ``tenants``, so
suspending a tenant or stamping ``auth_failed_at`` stops that tenant's polling
on the next pass with no code path involved and nobody else affected.

Each poller then runs inside its own tenant's database scope, using its own
tenant's Conductor client, so everything below this module stays single-tenant
in shape.

Two ceilings keep one customer from crowding out the rest: a per-tenant
``max_pollers`` and a process-wide :data:`GLOBAL_MAX_POLLERS`, allocated
round-robin by least-recently-served tenant. Because the reconcile query orders
*active* turn states first within each tenant, a tenant that hits its ceiling
still gets its busy sessions polled — it is the idle ones that wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, cast

from ctb.conductor.errors import AuthFatal
from ctb.conductor.pool import ClientPool, MissingKeyError
from ctb.db.connection import Database, tenant_scope
from ctb.db.repo import (
    deliveries,
    lease,
    sessions,
    tenancy,
    transcript,
    wizard,
)
from ctb.db.repo.sessions import SessionRow
from ctb.logging import get_logger
from ctb.turn.session_poller import ActionSink, SessionPoller
from ctb.turn.state import Evidence

__all__ = [
    "GLOBAL_MAX_POLLERS",
    "MAX_RESTART_BACKOFF_S",
    "RECONCILE_INTERVAL_S",
    "PollerFactory",
    "Supervisor",
]

_log = get_logger(__name__)

RECONCILE_INTERVAL_S: Final = lease.HEARTBEAT_MS / 1000.0
MAX_RESTART_BACKOFF_S: Final = 60.0
MAINTENANCE_INTERVAL_S: Final = 24 * 60 * 60.0

#: Idle Conductor clients are swept far more often than retention runs: each
#: one holds a decrypted API key in an httpx header until it goes.
CLIENT_SWEEP_INTERVAL_S: Final = 300.0

#: Hard ceiling on concurrent pollers across every tenant. Reaching it logs
#: ``supervisor.pollers_starved``, which is the signal to shard the lease.
GLOBAL_MAX_POLLERS: Final = 200

type Sleeper = Callable[[float], Awaitable[None]]
type PollerFactory = Callable[[str, uuid.UUID], SessionPoller]


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
        clients: ClientPool,
        db: Database,
        system_db: Database,
        *,
        action_sink: ActionSink | None = None,
        poller_factory: PollerFactory | None = None,
        holder: str | None = None,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_pollers: int = GLOBAL_MAX_POLLERS,
    ) -> None:
        self.clients = clients
        #: Tenant-scoped: what the pollers read and write, under RLS.
        self.db = db
        #: BYPASSRLS: the reconcile query, the lease, and maintenance.
        self.system_db = system_db
        self.action_sink = action_sink
        self.holder = holder or lease.instance_id()
        self._sleep = sleep
        self._clock = clock
        self._max_pollers = max(1, max_pollers)
        self._poller_factory = poller_factory or self._make_poller
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pollers: dict[str, SessionPoller] = {}
        self._restart: dict[str, _Restart] = {}
        #: session id -> tenant, so a cancellation can be scoped to one tenant.
        self._tenant_of: dict[str, uuid.UUID] = {}
        self._lease: lease.Lease | None = None
        self._runner: asyncio.Task[None] | None = None
        self._running = False
        self._auth_fatal: set[uuid.UUID] = set()
        self._auth_notified: set[uuid.UUID] = set()
        self._last_served: dict[uuid.UUID, float] = {}
        #: Tenant rows for this pass: quotas, and the sealed key the pool needs.
        self._tenant_rows: dict[uuid.UUID, tenancy.TenantRow] = {}
        self._next_maintenance_at = 0.0
        self._next_sweep_at = 0.0

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
    def auth_fatal_tenants(self) -> frozenset[uuid.UUID]:
        """Tenants whose key was rejected. Only *their* pollers are stopped.

        A second in-flight request can prove a 401 raced with a successful
        request after one poller already raised ``AuthFatal``. The client clears
        its counter on that 2xx; clear the latch too, or that tenant stays dark
        until the next deployment.
        """
        recovered = {
            tenant_id
            for tenant_id in self._auth_fatal
            if self._auth_failures(tenant_id) == 0
        }
        if recovered:
            self._auth_fatal -= recovered
            self._auth_notified -= recovered
            _log.info("supervisor.auth_recovered", tenants=len(recovered))
        # A client whose counter is non-zero but that has not yet raised
        # counts too: the poller may still be mid-tick.
        rejecting = {
            tenant_id
            for tenant_id in set(self._tenant_of.values())
            if self._auth_failures(tenant_id) > 0
        }
        return frozenset(self._auth_fatal | rejecting)

    def _auth_failures(self, tenant_id: uuid.UUID) -> int:
        client = self.clients.peek(tenant_id)
        return 0 if client is None else client.auth_failures

    def is_auth_fatal(self, tenant_id: uuid.UUID) -> bool:
        return tenant_id in self.auth_fatal_tenants

    def _make_poller(self, session_id: str, tenant_id: uuid.UUID) -> SessionPoller:
        client = self.clients.peek(tenant_id)
        if client is None:  # pragma: no cover - _spawn primes the pool first
            raise MissingKeyError(f"no Conductor client for tenant {tenant_id}")
        row = self._tenant_rows.get(tenant_id)
        return SessionPoller(
            client,
            self.db,
            session_id,
            action_sink=self.action_sink,
            max_pending_deliveries=None if row is None else row.max_pending_deliveries,
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
                    if acquired:
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
        if not acquired:
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
        tenant_id = self._tenant_of.get(session_id)
        if poller is None or self._lease is None:
            return False
        if tenant_id is not None and self.is_auth_fatal(tenant_id):
            return False
        await poller.dispatch(evidence)
        return True

    async def _renew_or_acquire(self) -> bool:
        if self._lease is None:
            current = await lease.acquire(self.system_db, holder=self.holder)
        else:
            current = await lease.heartbeat(self.system_db, holder=self.holder)
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

        # Rebuilt every pass: a suspension or a rotated key must be seen now.
        self._tenant_rows.clear()
        rows = await sessions.list_bound(self.system_db)
        # Load the quotas *before* selecting. `_quota` reads this cache, and
        # filling it lazily inside `_spawn` — which runs after — meant every
        # tenant silently got the global ceiling instead of its own.
        for tenant_id in {row.tenant_id for row in rows if row.tenant_id}:
            await self._tenant(tenant_id)
        wanted = {row.id: row for row in self._select(rows)}

        for session_id in tuple(self._tasks):
            task = self._tasks[session_id]
            if session_id not in wanted:
                await self._drop(session_id, cancel=True)
                self._restart.pop(session_id, None)
                continue
            if task.done():
                await self._observe_completion(session_id, task)

        for tenant_id in self.auth_fatal_tenants:
            await self._cancel_tenant(tenant_id)

        now = self._clock()
        for session_id in sorted(wanted):
            row = wanted[session_id]
            if session_id in self._tasks or row.tenant_id is None:
                continue
            if self.is_auth_fatal(row.tenant_id):
                continue
            restart = self._restart.get(session_id)
            if restart is not None and now < restart.not_before:
                continue
            await self._spawn(row)

    def _select(self, rows: Sequence[SessionRow]) -> list[SessionRow]:
        """Fair share: round-robin across tenants, least recently served first.

        ``rows`` arrives grouped by tenant and, within a tenant, active turn
        states first — so a tenant at its ceiling still gets its busy sessions.
        """
        by_tenant: dict[uuid.UUID, list[SessionRow]] = {}
        for row in rows:
            if row.tenant_id is None:  # pragma: no cover - column is NOT NULL
                continue
            by_tenant.setdefault(row.tenant_id, []).append(row)
        if not by_tenant:
            return []

        order = deque(sorted(by_tenant, key=lambda t: self._last_served.get(t, 0.0)))
        budget = {t: self._quota(t) for t in by_tenant}
        queues = {t: iter(v) for t, v in by_tenant.items()}
        chosen: list[SessionRow] = []
        while order and len(chosen) < self._max_pollers:
            tenant_id = order.popleft()
            if budget[tenant_id] <= 0:
                continue
            row = next(queues[tenant_id], None)
            if row is None:
                continue
            chosen.append(row)
            budget[tenant_id] -= 1
            self._last_served[tenant_id] = self._clock()
            order.append(tenant_id)

        starved = len(rows) - len(chosen)
        if starved:
            # Never truncate silently: a queue that looks covered but is not is
            # the failure mode that takes weeks to notice.
            _log.warning(
                "supervisor.pollers_starved",
                starved=starved,
                running=len(chosen),
                tenants=len(by_tenant),
            )
        return chosen

    def _quota(self, tenant_id: uuid.UUID) -> int:
        row = self._tenant_rows.get(tenant_id)
        return self._max_pollers if row is None else row.max_pollers

    async def _run_maintenance_if_due(self) -> None:
        now = self._clock()
        if now >= self._next_sweep_at:
            self._next_sweep_at = now + CLIENT_SWEEP_INTERVAL_S
            try:
                await self.clients.sweep()
            except Exception as exc:  # pragma: no cover - sweep is best effort
                _log.warning("supervisor.client_sweep_failed", error=repr(exc))
        if now < self._next_maintenance_at:
            return
        self._next_maintenance_at = now + MAINTENANCE_INTERVAL_S
        try:
            # System-scoped: retention is a platform job, not a tenant's.
            removed_transcript = await transcript.prune(self.system_db)
            # Terminal deliveries had no retention at all, so one permanent
            # Telegram refusal left /health reading "degraded" for the life of
            # the database — a past event reported forever as a live problem.
            # `payload_json` is also the same customer content transcripts are
            # pruned for, so this closes two holes with one sweep.
            removed_deliveries = await deliveries.prune_terminal(self.system_db)
            removed_wizards = await wizard.prune_expired(self.system_db)
            removed_tokens = await tenancy.prune_enrollment_tokens(self.system_db)
        except Exception as exc:
            # Retention is important, but a transient maintenance failure must
            # not stop transcript delivery. Retry at the next reconcile.
            self._next_maintenance_at = now
            _log.error("supervisor.maintenance_failed", error=repr(exc))
            return
        _log.info(
            "supervisor.maintenance_complete",
            transcript_rows=removed_transcript,
            delivery_rows=removed_deliveries,
            wizard_rows=removed_wizards,
            enrollment_tokens=removed_tokens,
        )

    async def _spawn(self, row: SessionRow) -> None:
        """Start one poller inside its tenant's database scope.

        The scope is entered *around* the poller's whole lifetime, not per
        statement, so every read and write it makes is filtered by row-level
        security exactly as a handler's would be.
        """
        if self._lease is None or row.tenant_id is None:
            return
        session_id = row.id
        tenant_id = row.tenant_id
        tenant = await self._tenant(tenant_id)
        if tenant is None:
            return
        try:
            await self.clients.get(tenant)
            poller = self._poller_factory(session_id, tenant_id)
        except MissingKeyError:
            # No key yet, or it was revoked. Not an error: the tenant simply
            # is not polling until they set one.
            return
        except Exception as exc:
            _log.error(
                "supervisor.client_unavailable",
                session_id=session_id,
                tenant=tenant.slug,
                error=repr(exc),
            )
            return

        async def run_scoped() -> None:
            async with tenant_scope(tenant_id):
                await poller.run()

        task = asyncio.create_task(run_scoped(), name=f"ctb-session:{session_id}")
        self._pollers[session_id] = poller
        self._tasks[session_id] = task
        self._tenant_of[session_id] = tenant_id
        self.clients.pin(tenant_id)
        _log.info(
            "supervisor.poller_started", session_id=session_id, tenant=tenant.slug
        )

    async def _tenant(self, tenant_id: uuid.UUID) -> tenancy.TenantRow | None:
        cached = self._tenant_rows.get(tenant_id)
        if cached is not None:
            return cached
        row = await tenancy.get(self.system_db, tenant_id)
        if row is not None:
            self._tenant_rows[tenant_id] = row
        return row

    async def _observe_completion(
        self, session_id: str, task: asyncio.Task[None]
    ) -> None:
        self._tasks.pop(session_id, None)
        self._pollers.pop(session_id, None)
        # Release the client pin here too, not only in `_drop`. A poller that
        # ends by itself — crashed, or returned because its session was unbound
        # — never passes through `_drop`, so the pin would never come back and
        # `_spawn` would take a second one on the next pass. A tenant whose pin
        # count can no longer reach zero is one whose decrypted API key
        # `ClientPool.sweep` can never evict, for the life of the process.
        tenant_id = self._tenant_of.pop(session_id, None)
        if tenant_id is not None:
            self.clients.unpin(tenant_id)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self._restart.pop(session_id, None)
            return
        if isinstance(error, AuthFatal):
            _log.error(
                "supervisor.auth_fatal", session_id=session_id, tenant=str(tenant_id)
            )
            if tenant_id is None:  # pragma: no cover - set by _spawn
                return
            self._auth_fatal.add(tenant_id)
            # Only this tenant stops. Everyone else keeps polling.
            await self._cancel_tenant(tenant_id)
            await tenancy.mark_auth_failed(
                self.system_db, tenant_id, reason="Conductor rejected the API key"
            )
            notify = getattr(self.action_sink, "auth_fatal", None)
            if callable(notify) and tenant_id not in self._auth_notified:
                self._auth_notified.add(tenant_id)
                try:
                    await cast(Callable[[str, uuid.UUID], Awaitable[None]], notify)(
                        session_id, tenant_id
                    )
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
        tenant_id = self._tenant_of.pop(session_id, None)
        if tenant_id is not None:
            self.clients.unpin(tenant_id)
        task = self._tasks.pop(session_id, None)
        if task is None:
            return
        if cancel and not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cancel_tenant(self, tenant_id: uuid.UUID) -> int:
        """Stop one tenant's pollers. A rejected key is not everyone's outage."""
        doomed = [
            session_id
            for session_id, owner in self._tenant_of.items()
            if owner == tenant_id and session_id in self._tasks
        ]
        for session_id in doomed:
            await self._drop(session_id, cancel=True)
        return len(doomed)

    async def _cancel_all(self) -> None:
        """Lease loss and shutdown only. An auth failure cancels one tenant."""
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
        for tenant_id in tuple(self._tenant_of.values()):
            self.clients.unpin(tenant_id)
        self._tenant_of.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _release_lease(self) -> None:
        current, self._lease = self._lease, None
        if current is None:
            return
        try:
            await lease.release(self.system_db, holder=self.holder)
        except Exception as exc:
            _log.warning(
                "supervisor.lease_release_failed",
                holder=self.holder,
                error=repr(exc),
            )
