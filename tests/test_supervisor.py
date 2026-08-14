"""Lease, reconciliation and crash-isolation checks for the supervisor."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import cast

from ctb.conductor.client import ConductorClient
from ctb.conductor.errors import AuthFatal
from ctb.conductor.pool import ClientPool
from ctb.db.connection import Database, now_ms
from ctb.db.repo import sessions, tenancy, workspaces
from ctb.turn.session_poller import ActionSink, SessionPoller
from ctb.turn.state import Cancel, Evidence
from ctb.turn.supervisor import Supervisor
from tests.pg import BOOTSTRAP_TENANT_ID

WORKSPACE = "workspace-supervisor"


class ClientStub:
    """Stands in for one tenant's :class:`ConductorClient`.

    ``get_me`` is the supervisor's corroboration probe, so what it answers is what
    decides whether a 401 stops the whole team or is written off as a blip.
    """

    def __init__(self, *, me_rejects: bool = True) -> None:
        self.auth_failures = 0
        self.me_rejects = me_rejects
        self.me_calls = 0

    async def get_me(self) -> object:
        self.me_calls += 1
        if self.me_rejects:
            raise AuthFatal(
                401,
                {"userMessage": "the key really is rejected"},
                method="GET",
                path="/me",
            )
        # A real 2xx zeroes the client's own counter; the latch reads it.
        self.auth_failures = 0
        return object()


class BlockingPoller:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.started = asyncio.Event()
        self.stopped = False
        self.evidence: list[Evidence] = []

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()

    def request_stop(self) -> None:
        self.stopped = True

    async def dispatch(self, evidence: Evidence) -> None:
        self.evidence.append(evidence)


class CrashPoller(BlockingPoller):
    async def run(self) -> None:
        self.started.set()
        raise RuntimeError("scripted crash")


class AuthCrashPoller(BlockingPoller):
    def __init__(self, session_id: str, client: ClientStub) -> None:
        super().__init__(session_id)
        self.client = client

    async def run(self) -> None:
        self.started.set()
        self.client.auth_failures = 1
        raise AuthFatal(
            401,
            {"userMessage": "transient authentication rejection"},
            method="GET",
            path="/sessions/{id}/status",
        )


class Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def seed_bound(db: Database, *session_ids: str) -> None:
    await workspaces.upsert(db, WORKSPACE, name="workspace")
    for index, session_id in enumerate(session_ids, start=1):
        await sessions.upsert(
            db,
            session_id,
            workspace_id=WORKSPACE,
            chat_id=-100_300,
            thread_id=index,
            is_bound=True,
        )


def factory_of(
    created: list[BlockingPoller],
) -> Callable[[str, uuid.UUID], SessionPoller]:
    def make(session_id: str, _tenant_id: uuid.UUID) -> SessionPoller:
        poller = BlockingPoller(session_id)
        created.append(poller)
        return cast(SessionPoller, poller)

    return make


class PoolStub:
    """A :class:`ClientPool` stand-in over one stub client."""

    def __init__(self, client: object) -> None:
        self._client = client
        self.pinned: list[uuid.UUID] = []
        self.forgotten: list[uuid.UUID] = []

    async def get(self, _tenant: object) -> object:
        return self._client

    def peek(self, _tenant_id: uuid.UUID) -> object:
        return self._client

    def pin(self, tenant_id: uuid.UUID) -> None:
        self.pinned.append(tenant_id)

    def unpin(self, tenant_id: uuid.UUID) -> None:
        if tenant_id in self.pinned:
            self.pinned.remove(tenant_id)

    async def forget(self, tenant_id: uuid.UUID) -> int:
        self.forgotten.append(tenant_id)
        return 1


async def test_only_lease_holder_spawns_and_reconciles_bindings(
    db: Database,
    system_db: Database,
) -> None:
    await seed_bound(db, "s1", "s2")
    first_created: list[BlockingPoller] = []
    second_created: list[BlockingPoller] = []
    client = cast(ConductorClient, ClientStub())
    first = Supervisor(
        cast(ClientPool, PoolStub(client)),
        db,
        system_db,
        holder="first",
        poller_factory=factory_of(first_created),
    )
    second = Supervisor(
        cast(ClientPool, PoolStub(client)),
        db,
        system_db,
        holder="second",
        poller_factory=factory_of(second_created),
    )

    assert await first.reconcile_once()
    assert not await second.reconcile_once()
    assert first.session_ids == {"s1", "s2"}
    assert second.task_count == 0
    assert await first.dispatch("s1", Cancel(requested_by=7))
    assert not await second.dispatch("s1", Cancel(requested_by=7))
    assert isinstance(first_created[0].evidence[0], Cancel)

    await sessions.unbind(db, "s2")
    assert await first.reconcile_once()
    assert first.session_ids == {"s1"}
    assert first_created[1].stopped

    await first.stop()
    await second.stop()


async def test_lease_loss_cancels_every_poller(
    db: Database, system_db: Database
) -> None:
    await seed_bound(db, "s1")
    created: list[BlockingPoller] = []
    supervisor = Supervisor(
        cast(ClientPool, PoolStub(ClientStub())),
        db,
        system_db,
        holder="owner",
        poller_factory=factory_of(created),
    )
    assert await supervisor.reconcile_once()
    await asyncio.sleep(0)

    await system_db.execute(
        "UPDATE singleton_lease SET holder = ? WHERE name = ?",
        ("replacement", "supervisor"),
    )

    assert not await supervisor.reconcile_once()
    assert supervisor.task_count == 0
    assert created[0].stopped
    await supervisor.stop()


async def test_crashed_poller_restarts_after_exponential_backoff(
    db: Database,
    system_db: Database,
) -> None:
    await seed_bound(db, "s1")
    made: list[BlockingPoller] = []
    clock = Clock()

    def factory(session_id: str, _tenant_id: uuid.UUID) -> SessionPoller:
        poller: BlockingPoller
        poller = CrashPoller(session_id) if not made else BlockingPoller(session_id)
        made.append(poller)
        return cast(SessionPoller, poller)

    supervisor = Supervisor(
        cast(ClientPool, PoolStub(ClientStub())),
        db,
        system_db,
        holder="owner",
        poller_factory=factory,
        clock=clock,
    )
    assert await supervisor.reconcile_once()
    await asyncio.sleep(0)
    assert await supervisor.reconcile_once()
    assert len(made) == 1
    assert supervisor.task_count == 0

    clock.advance(1.0)
    assert await supervisor.reconcile_once()
    assert len(made) == 2
    assert supervisor.task_count == 1
    await supervisor.stop()


async def test_a_latched_auth_failure_clears_after_an_inflight_success(
    db: Database,
    system_db: Database,
) -> None:
    """One raced 401 must not silence every session until redeploy."""
    await seed_bound(db, "s1")
    client = ClientStub()
    made: list[BlockingPoller] = []

    def factory(session_id: str, _tenant_id: uuid.UUID) -> SessionPoller:
        poller: BlockingPoller
        poller = (
            AuthCrashPoller(session_id, client)
            if not made
            else BlockingPoller(session_id)
        )
        made.append(poller)
        return cast(SessionPoller, poller)

    supervisor = Supervisor(
        cast(ClientPool, PoolStub(client)),
        db,
        system_db,
        holder="owner",
        poller_factory=factory,
    )
    assert await supervisor.reconcile_once()
    await asyncio.sleep(0)
    assert await supervisor.reconcile_once()
    assert supervisor.auth_fatal_tenants == frozenset({BOOTSTRAP_TENANT_ID})
    assert supervisor.task_count == 0

    # A request already in flight returns 2xx. ConductorClient does this reset,
    # so the in-memory latch clears…
    client.auth_failures = 0
    assert supervisor.auth_fatal_tenants == frozenset()

    # …but the tenant itself stays stopped for the length of the retry window,
    # because the rejection is stamped on the row and `list_bound` filters on
    # it. Setting a new key clears the stamp and does not wait.
    assert await supervisor.reconcile_once()
    assert supervisor.task_count == 0

    await tenancy.set_conductor_key(
        system_db,
        BOOTSTRAP_TENANT_ID,
        ciphertext=b"sealed",
        kid="v1",
        fingerprint="fp",
    )
    assert await supervisor.reconcile_once()
    assert supervisor.task_count == 1
    assert len(made) == 2
    await supervisor.stop()


async def test_an_uncorroborated_401_does_not_stop_the_team(
    db: Database,
    system_db: Database,
) -> None:
    """One 401 through a proxy wobble must not stamp the tenant.

    This is the live outage: two teams were latched sixty-nine seconds apart by
    a single 401 each, either side of a 500 and a ReadTimeout, and stayed dark
    for four days while the same keys returned 200 to everything else. The
    probe is the difference — it answers 2xx, so nothing is stamped, nobody is
    told their key was rejected, and the poller comes back on plain backoff.
    """
    await seed_bound(db, "s1")
    client = ClientStub(me_rejects=False)
    made: list[BlockingPoller] = []
    clock = Clock()

    def factory(session_id: str, _tenant_id: uuid.UUID) -> SessionPoller:
        poller: BlockingPoller = (
            AuthCrashPoller(session_id, client)
            if not made
            else BlockingPoller(session_id)
        )
        made.append(poller)
        return cast(SessionPoller, poller)

    notices: list[str] = []

    class Sink:
        async def auth_fatal(self, session_id: str, _tenant: uuid.UUID) -> None:
            notices.append(session_id)

    supervisor = Supervisor(
        cast(ClientPool, PoolStub(client)),
        db,
        system_db,
        holder="owner",
        poller_factory=factory,
        action_sink=cast(ActionSink, Sink()),
        clock=clock,
    )
    assert await supervisor.reconcile_once()
    await asyncio.sleep(0)
    assert await supervisor.reconcile_once()

    assert client.me_calls == 1
    assert supervisor.auth_fatal_tenants == frozenset()
    assert notices == []
    row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
    assert row is not None and row.auth_failed_at is None

    # It restarts on the ordinary crash backoff, like any other failure.
    clock.advance(1.0)
    assert await supervisor.reconcile_once()
    assert supervisor.task_count == 1
    await supervisor.stop()


async def test_a_corroborated_latch_expires_instead_of_waiting_for_a_human(
    db: Database,
    system_db: Database,
) -> None:
    """The latch is a pause. Nothing but a redeploy used to lift it."""
    await seed_bound(db, "s1")
    client = ClientStub()
    made: list[BlockingPoller] = []

    def factory(session_id: str, _tenant_id: uuid.UUID) -> SessionPoller:
        poller: BlockingPoller = (
            AuthCrashPoller(session_id, client)
            if not made
            else BlockingPoller(session_id)
        )
        made.append(poller)
        return cast(SessionPoller, poller)

    pool = PoolStub(client)
    supervisor = Supervisor(
        cast(ClientPool, pool),
        db,
        system_db,
        holder="owner",
        poller_factory=factory,
    )
    assert await supervisor.reconcile_once()
    await asyncio.sleep(0)
    assert await supervisor.reconcile_once()

    row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
    assert row is not None and row.auth_failed_at is not None
    assert supervisor.task_count == 0

    # Backdate the stamp past the window: the same thing the wall clock does.
    await tenancy.mark_auth_failed(
        system_db,
        BOOTSTRAP_TENANT_ID,
        reason="rejected",
        at=now_ms() - tenancy.AUTH_RETRY_AFTER_MS - 1,
    )
    # The key works again by now, so the retry sticks.
    client.me_rejects = False
    client.auth_failures = 0

    assert await supervisor.reconcile_once()
    assert supervisor.task_count == 1
    # The cached client is dropped with the flag: the owner's likeliest reply
    # to "your key was rejected" is a new key, and a pooled client is a header
    # built from the old one.
    assert BOOTSTRAP_TENANT_ID in pool.forgotten
    await supervisor.stop()
