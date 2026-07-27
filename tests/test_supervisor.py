"""Lease, reconciliation and crash-isolation checks for the supervisor."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import cast

from ctb.conductor.client import ConductorClient
from ctb.conductor.errors import AuthFatal
from ctb.conductor.pool import ClientPool
from ctb.db.connection import Database
from ctb.db.repo import sessions, tenancy, workspaces
from ctb.turn.session_poller import SessionPoller
from ctb.turn.state import Cancel, Evidence
from ctb.turn.supervisor import Supervisor
from tests.pg import BOOTSTRAP_TENANT_ID

WORKSPACE = "workspace-supervisor"


class ClientStub:
    auth_failures = 0


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
            403,
            {"userMessage": "transient proxy rejection"},
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

    async def get(self, _tenant: object) -> object:
        return self._client

    def peek(self, _tenant_id: uuid.UUID) -> object:
        return self._client

    def pin(self, tenant_id: uuid.UUID) -> None:
        self.pinned.append(tenant_id)

    def unpin(self, tenant_id: uuid.UUID) -> None:
        if tenant_id in self.pinned:
            self.pinned.remove(tenant_id)


async def test_only_lease_holder_spawns_and_reconciles_bindings(
    db: Database, system_db: Database,
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


async def test_lease_loss_cancels_every_poller(db: Database, system_db: Database) -> None:
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
    db: Database, system_db: Database,
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
    db: Database, system_db: Database,
) -> None:
    """One transient 403 must not silence every session until redeploy."""
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

    # …but the tenant itself stays stopped, because a rejected key is stamped
    # on the row and `list_bound` filters on it. That is deliberate: only a new
    # key should restart polling, and setting one clears the stamp.
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
