"""Crash points and overlapping pollers — the exactly-once story, end to end.

PLAN §Verification, Phase 1, names four hazards this file has to close:

* **restart between the DB write and the POST** — the ledger row exists, the
  prompt may or may not. Boot re-POSTs the identical ``messageId`` and the API
  dedupes (probe assumption 7, passed twice), so exactly one prompt exists.
* **restart between the Telegram send and the DB write** — deliberately
  at-least-once, because a rare duplicate beats a silently lost reply. The
  ``content_hash`` guard skips the case it can prove, and no *row* is ever
  duplicated.
* **overlapping pollers** across a redeploy — two processes, one SQLite file.
  ``ON CONFLICT DO NOTHING`` plus the conditional ``pending -> sending`` claim
  make a duplicate send structurally impossible.
* **replay** — the same page fetched twice inserts nothing the second time.

Everything runs against the scripted fake API through the real client, with two
real SQLite connections to one file for the overlap tests.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from ctb.conductor.client import ConductorClient
from ctb.db.connection import Database
from ctb.db.migrate import apply_migrations
from ctb.db.repo import chats, deliveries, lease, prompts, sessions, transcript
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.deliveries import DeliveryKey, DeliveryRow
from ctb.settings import Settings
from ctb.turn import cursor
from ctb.turn.state import PostAmbiguous
from tests.fakes.fake_conductor import (
    Advance,
    FakeConductor,
    FakeSession,
    PostFailure,
    Tick,
    assistant,
    double_prompt,
    result,
    state_changed,
)

CHAT_ID = -1002000000000
THREAD_ID = 42

type ClientFactory = Callable[..., ConductorClient]


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def fake() -> FakeConductor:
    return FakeConductor()


@pytest.fixture
async def clients(settings: Settings) -> AsyncIterator[ClientFactory]:
    made: list[ConductorClient] = []

    def make(target: FakeConductor, *, max_attempts: int = 2) -> ConductorClient:
        instance = ConductorClient(
            settings,
            transport=target.transport(),
            sleep=_no_sleep,
            rng=random.Random(7),
            max_attempts=max_attempts,
        )
        made.append(instance)
        return instance

    try:
        yield make
    finally:
        for instance in made:
            await instance.aclose()


@pytest.fixture
def client(clients: ClientFactory, fake: FakeConductor) -> ConductorClient:
    return clients(fake)


async def bind(
    db: Database, session: FakeSession, *, thread_id: int = THREAD_ID
) -> None:
    await workspaces_repo.upsert(db, session.workspace_id, name=session.workspace.name)
    await sessions.upsert(
        db,
        session.session_id,
        workspace_id=session.workspace_id,
        chat_id=CHAT_ID,
        thread_id=thread_id,
        is_bound=True,
    )
    await chats.ensure(db, CHAT_ID, thread_id)


# ── a stand-in for the Telegram side of the outbox ───────────────────────────


class FakeTelegram:
    """Records what was actually put on the wire, duplicates included."""

    def __init__(self) -> None:
        self.sent: list[DeliveryKey] = []

    def send(self, row: DeliveryRow) -> int:
        self.sent.append(row.key)
        return 5_000 + len(self.sent)

    @property
    def duplicates(self) -> int:
        return len(self.sent) - len(set(self.sent))


async def deliver(
    db: Database,
    telegram: FakeTelegram,
    *,
    claim_id: str,
    limit: int = 20,
    crash_after: int | None = None,
) -> list[DeliveryRow]:
    """One outbox pass. ``crash_after`` drops the process before ``mark_sent``."""
    rows = await deliveries.claim(db, claim_id=claim_id, limit=limit)
    for index, row in enumerate(rows):
        tg_message_id = telegram.send(row)
        if crash_after is not None and index >= crash_after:
            return rows  # the bookkeeping write never happens
        await deliveries.mark_sent(db, row.key, tg_message_id=tg_message_id)
    return rows


async def states(db: Database, session_id: str) -> dict[str, int]:
    return await deliveries.counts_by_state(db, session_id)


# ── crash point 1: between the DB write and the POST ─────────────────────────


async def test_a_crash_before_the_post_leaves_exactly_one_prompt(
    db: Database, fake: FakeConductor, clients: ClientFactory
) -> None:
    """The ledger row is written first, so boot knows a prompt may be missing."""
    session = fake.add_session()
    await bind(db, session)
    client = clients(fake)

    # Process 1: row written, then the process dies before any HTTP happens.
    orphan = await prompts.create(
        db, session_id=session.session_id, body="finish the refactor"
    )
    assert fake.calls_to("/messages", method="POST") == []

    # Process 2 boots and recovers (machine transition 3).
    recoverable = await prompts.list_recoverable(db, session_id=session.session_id)
    assert [row.message_id for row in recoverable] == [orphan.message_id]
    for row in recoverable:
        await cursor.repost_prompt(client, db, row.message_id)

    # Process 3 boots again: nothing left to do.
    assert await prompts.list_recoverable(db, session_id=session.session_id) == []

    assert session.posted_ids == (orphan.message_id,)
    assert session.echo_count(orphan.message_id) == 1
    assert len(fake.calls_to("/messages", method="POST")) == 1

    drained = await cursor.drain(client, db, session.session_id)
    assert drained.witnessed == (orphan.message_id,)
    rows = await prompts.list_for_session(db, session.session_id)
    assert len(rows) == 1
    assert rows[0].state == "witnessed"


async def test_an_ambiguous_post_that_landed_is_never_duplicated(
    db: Database, fake: FakeConductor, clients: ClientFactory
) -> None:
    """The write took effect and the response was lost — the worst case.

    Re-POSTing the identical id produces one user echo, not two. That single
    measured fact is what the whole crash-safety design rests on.
    """
    session = fake.add_session()
    await bind(db, session)
    client = clients(fake, max_attempts=1)
    session.fail_next_post(PostFailure(status=500, landed=True))

    sent = await cursor.send_prompt(
        client,
        db,
        session_id=session.session_id,
        text="do the thing",
        max_attempts=1,
    )

    assert sent.posted is False
    assert isinstance(sent.evidence, PostAmbiguous)
    assert sent.evidence.message_id == sent.message_id
    row = await prompts.get(db, sent.message_id)
    assert row is not None
    assert row.state == "pending", "still recoverable — the fate is unknown"
    assert session.echo_count(sent.message_id) == 1, "it did land, invisibly"

    # Boot, or the next tick's RePost. Same id, always.
    again = await cursor.repost_prompt(client, db, sent.message_id)

    assert again.posted is True
    assert session.duplicate_posts == 1
    assert session.echo_count(sent.message_id) == 1
    assert session.posted_ids == (sent.message_id,)

    drained = await cursor.drain(client, db, session.session_id)
    echoes = [m for m in drained.messages if m.is_user_echo]
    assert len(echoes) == 1


async def test_an_ambiguous_post_that_did_not_land_still_arrives_once(
    db: Database, fake: FakeConductor, clients: ClientFactory
) -> None:
    session = fake.add_session()
    await bind(db, session)
    client = clients(fake, max_attempts=1)
    session.fail_next_post(PostFailure(status=503, landed=False))

    sent = await cursor.send_prompt(
        client, db, session_id=session.session_id, text="try me", max_attempts=1
    )
    assert sent.posted is False
    assert session.posted_ids == ()

    again = await cursor.repost_prompt(client, db, sent.message_id)

    assert again.posted is True
    assert session.posted_ids == (sent.message_id,)
    assert session.echo_count(sent.message_id) == 1
    assert session.duplicate_posts == 0


async def test_the_cursor_retry_loop_reuses_the_same_id(
    db: Database, fake: FakeConductor, clients: ClientFactory
) -> None:
    session = fake.add_session()
    await bind(db, session)
    client = clients(fake, max_attempts=1)
    session.fail_next_post(PostFailure(status=500, landed=True))
    posted_ids: list[Any] = []
    original = session._post_json

    def spy(body: Any) -> Any:
        posted_ids.append(body.get("messageId"))
        return original(body)

    session._post_json = spy  # type: ignore[method-assign]

    sent = await cursor.send_prompt(
        client, db, session_id=session.session_id, text="go", max_attempts=3
    )

    assert sent.posted is True
    assert sent.attempts == 2
    assert set(posted_ids) == {sent.message_id}, "one id, every attempt"
    assert session.echo_count(sent.message_id) == 1


async def test_two_prompts_are_two_prompts(
    db: Database, clients: ClientFactory
) -> None:
    """A second prompt while working is a *different* id — dedupe must not eat it."""
    scenario = double_prompt(advance=Advance.MANUAL)
    client = clients(scenario.fake)
    session = scenario.session
    await bind(db, session)

    first = await cursor.send_prompt(
        client, db, session_id=session.session_id, text="one"
    )
    second = await cursor.send_prompt(
        client, db, session_id=session.session_id, text="two"
    )

    assert first.message_id != second.message_id
    assert len(session.posted_ids) == 2
    assert session.duplicate_posts == 0

    for _ in range(len(session.script) + 2):
        session.tick()
        await cursor.drain(client, db, session.session_id)

    rows = await prompts.list_for_session(db, session.session_id)
    assert {row.state for row in rows} == {"witnessed"}
    assert len(rows) == 2
    assert await prompts.outstanding_count(db, session.session_id) == 0


# ── crash point 2: between the Telegram send and the DB write ────────────────


async def test_a_crash_after_sending_resends_rather_than_losing(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """At-least-once, chosen deliberately: no row is lost and no row duplicates."""
    session = fake.add_session(
        script=[Tick(emit=(state_changed(), assistant("the answer"), result("ok")))],
        advance=Advance.MANUAL,
    )
    await bind(db, session)
    session.tick()
    await cursor.drain(client, db, session.session_id)
    pending = await deliveries.pending_count(db, session.session_id)
    assert pending >= 1

    telegram = FakeTelegram()
    # A tight outbox: claim one, send it, die before recording the send.
    await deliver(db, telegram, claim_id="worker-a", limit=1, crash_after=0)
    assert len(telegram.sent) == 1
    assert (await states(db, session.session_id))["sending"] == 1

    # Boot: orphans are re-claimed, because a lost reply is worse than a repeat.
    recovery = await deliveries.recover_orphaned(db, claim_id="worker-b")
    assert len(recovery.claimed) == 1
    assert recovery.skipped == ()
    for row in recovery.claimed:
        await deliveries.mark_sent(db, row.key, tg_message_id=telegram.send(row))
    await deliver(db, telegram, claim_id="worker-b")

    assert telegram.duplicates == 1, "exactly one repeat, and only the one"
    counts = await states(db, session.session_id)
    assert counts.get("pending", 0) == 0
    assert counts.get("sending", 0) == 0
    assert counts["sent"] == pending

    rows = await deliveries.list_for_session(db, session.session_id, limit=1000)
    assert len({row.key for row in rows}) == len(rows), "no duplicate rows, ever"


async def test_recovery_skips_a_payload_already_on_the_wire(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """The common case of the crash window: identical content, already sent."""
    session = fake.add_session(seed=(assistant("hello"),))
    await bind(db, session)
    await cursor.drain(client, db, session.session_id)

    telegram = FakeTelegram()
    await deliver(db, telegram, claim_id="worker-a")
    sent_rows = await deliveries.list_for_session(
        db, session.session_id, state="sent", limit=10
    )
    assert sent_rows

    # A re-render of the very same content, stranded in `sending` by a crash.
    twin = sent_rows[0]
    await deliveries.enqueue(
        db,
        session_id=twin.session_id,
        message_id=twin.message_id + ":retry",
        chat_id=twin.chat_id,
        thread_id=twin.thread_id,
        session_index=twin.session_index,
        payload_json=twin.payload_json,
    )
    await deliveries.claim(db, claim_id="worker-a")

    recovery = await deliveries.recover_orphaned(db, claim_id="worker-b")

    assert recovery.claimed == ()
    assert len(recovery.skipped) == 1
    assert len(telegram.sent) == len(sent_rows)


# ── overlapping pollers: two processes, one file ─────────────────────────────


@pytest.fixture
async def second_db(db: Database) -> AsyncIterator[Database]:
    """A second connection to the same file — the redeploy overlap, for real."""
    other = await Database(Path(db.path)).connect()
    await apply_migrations(other)
    try:
        yield other
    finally:
        await other.close()


async def test_two_pollers_on_one_database_never_duplicate_a_delivery(
    db: Database,
    second_db: Database,
    fake: FakeConductor,
    clients: ClientFactory,
) -> None:
    """Both processes fetch the same page. Only one set of rows exists."""
    session = fake.add_session(
        seed=[assistant(f"chunk {i}") for i in range(6)],
        advance=Advance.MANUAL,
    )
    await bind(db, session)
    first = clients(fake)
    second = clients(fake)

    left, right = await asyncio.gather(
        cursor.drain(first, db, session.session_id),
        cursor.drain(second, second_db, session.session_id),
    )

    total = len(session.transcript)
    seen = left.n + right.n
    assert await transcript.count(db, session.session_id) == total
    assert left.recorded + right.recorded == total, "each message recorded once"
    assert left.duplicates + right.duplicates == seen - total, "the rest bounced"

    rows = await deliveries.list_for_session(db, session.session_id, limit=1000)
    assert len({row.key for row in rows}) == len(rows)
    assert all(row.state == "pending" for row in rows)

    # …and the conditional claim means only one of them sends each row.
    telegram = FakeTelegram()
    claimed_a, claimed_b = await asyncio.gather(
        deliveries.claim(db, claim_id="a"),
        deliveries.claim(second_db, claim_id="b"),
    )
    keys_a = {row.key for row in claimed_a}
    keys_b = {row.key for row in claimed_b}
    assert keys_a.isdisjoint(keys_b), "a row can only leave `pending` once"
    assert keys_a | keys_b == {row.key for row in rows}

    for row in [*claimed_a, *claimed_b]:
        telegram.send(row)
        await deliveries.mark_sent(db, row.key, tg_message_id=1)

    assert telegram.duplicates == 0
    counts = await states(db, session.session_id)
    assert counts.get("sent", 0) == len(rows)
    assert counts.get("pending", 0) == 0


async def test_only_one_supervisor_holds_the_lease(
    db: Database, second_db: Database
) -> None:
    """Defence in depth: the overlap should not even get as far as polling."""
    first = await lease.acquire(db, holder="instance-a")
    second = await lease.acquire(second_db, holder="instance-b")

    assert first is not None
    assert second is None
    assert await lease.heartbeat(second_db, holder="instance-b") is None
    assert await lease.heartbeat(db, holder="instance-a") is not None


async def test_a_second_poller_drains_what_the_first_already_recorded(
    db: Database,
    second_db: Database,
    fake: FakeConductor,
    clients: ClientFactory,
) -> None:
    """Sequential overlap: the loser's page is entirely duplicate, and harmless."""
    session = fake.add_session(seed=[assistant(f"m{i}") for i in range(4)])
    await bind(db, session)
    first = clients(fake)
    second = clients(fake)

    winner = await cursor.drain(first, db, session.session_id)
    loser = await cursor.drain(second, second_db, session.session_id)

    assert winner.recorded == 4
    assert loser.n == 0
    assert loser.deliveries_created == 0
    rows = await deliveries.list_for_session(db, session.session_id, limit=1000)
    assert len({row.key for row in rows}) == len(rows)


# ── replay ───────────────────────────────────────────────────────────────────


async def test_a_forced_redrain_of_recorded_content_queues_nothing_new(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """``ForceDrain`` on boot re-reads pages we may already hold. It must be free."""
    session = fake.add_session(seed=[assistant(f"m{i}") for i in range(5)])
    await bind(db, session)
    await cursor.drain(client, db, session.session_id)
    before = await deliveries.list_for_session(db, session.session_id, limit=1000)

    # Rewind the cursor the way a restored-from-backup DB would, and re-drain.
    await sessions.update(
        db, session.session_id, cursor_message_id=None, cursor_session_index=-1
    )
    again = await cursor.drain(client, db, session.session_id)

    assert again.n == 5
    assert again.recorded == 0, "every message was already stored"
    assert again.deliveries_created == 0, "and every delivery already queued"
    after = await deliveries.list_for_session(db, session.session_id, limit=1000)
    assert [row.key for row in before] == [row.key for row in after]


async def test_payloads_are_hashed_so_the_boot_guard_can_compare_them(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(seed=(assistant("hashable"),))
    await bind(db, session)

    await cursor.drain(client, db, session.session_id)

    rows = await deliveries.list_for_session(db, session.session_id, limit=10)
    assert rows
    for row in rows:
        assert row.content_hash
        assert row.content_hash == deliveries.content_hash(row.payload_json or "")
        payload = json.loads(row.payload_json or "{}")
        assert payload["index"] == row.part_index
