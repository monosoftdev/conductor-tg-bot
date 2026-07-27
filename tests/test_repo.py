"""Repository-layer tests against a real temp SQLite file.

The five proofs the design actually rests on, in order of how much damage their
failure would do:

1. Replaying a page records nothing twice and queues no duplicate delivery.
2. The cursor never advances past a message that failed to record.
3. Two claimers — including two *connections*, i.e. two processes — never get
   the same delivery row.
4. Two lease acquirers produce exactly one winner.
5. Content is capped at 64 KB and pruned after 30 days.

Everything else here is ordinary coverage of the SQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ctb.conductor.models import SessionStatusValue, TranscriptMessage
from ctb.db import MAX_CONTENT_BYTES, NO_THREAD_ID
from ctb.db.connection import Database, now_ms, tenant_scope
from ctb.db.errors import IntegrityError
from ctb.db.repo import (
    chats,
    deliveries,
    events,
    lease,
    prompts,
    sessions,
    tenancy,
    transcript,
    wizard,
    workspaces,
)
from ctb.db.repo.transcript import AdvanceItem, DeliveryDraft
from ctb.turn.state import TurnState
from tests.pg import BOOTSTRAP_TENANT_ID, app_dsn, worker_dsn

SESSION = "sess-1"
WORKSPACE = "ws-1"
CHAT = -1002000000000
TOPIC = 42

type MessageFactory = Callable[..., TranscriptMessage]


@pytest.fixture
async def seeded(db: Database) -> AsyncIterator[Database]:
    """A workspace + session, because everything else has an FK to them."""
    await workspaces.upsert(db, WORKSPACE, name="tg-demo", project_id="proj-1")
    await sessions.upsert(
        db,
        SESSION,
        workspace_id=WORKSPACE,
        agent="claude",
        model="sonnet",
        chat_id=CHAT,
        thread_id=TOPIC,
    )
    yield db


def _draft(part_index: int = 0, *, text: str = "payload") -> DeliveryDraft:
    return DeliveryDraft(
        chat_id=CHAT,
        thread_id=TOPIC,
        part_index=part_index,
        payload_json=f'{{"html":"{text}"}}',
    )


def _items(
    factory: MessageFactory, *indexes: int, with_delivery: bool = True
) -> tuple[AdvanceItem, ...]:
    return tuple(
        AdvanceItem(
            message=factory(index, session_id=SESSION, text=f"m{index}"),
            deliveries=(_draft(text=f"m{index}"),) if with_delivery else (),
        )
        for index in indexes
    )


# ── the atomic cursor advance ────────────────────────────────────────────────


async def test_advance_records_messages_deliveries_and_cursor(
    seeded: Database, message_factory: MessageFactory
) -> None:
    result = await transcript.advance_cursor(
        seeded, SESSION, _items(message_factory, 0, 2, 3)
    )

    assert result.recorded == 3
    assert result.duplicates == 0
    assert result.deliveries_created == 3
    assert result.advanced is True
    # sessionIndex is not gapless: 0, 2, 3 is a normal page.
    assert result.cursor_session_index == 3
    assert result.cursor_message_id == f"{SESSION}:4:0"

    row = await sessions.get(seeded, SESSION)
    assert row is not None
    assert row.cursor_session_index == 3
    assert row.seeded is True
    assert await transcript.count(seeded, SESSION) == 3
    assert await deliveries.pending_count(seeded, SESSION) == 3


async def test_replaying_a_page_is_a_no_op(
    seeded: Database, message_factory: MessageFactory
) -> None:
    """The single most important property: replay never duplicates a reply."""
    page = _items(message_factory, 0, 1, 2)
    first = await transcript.advance_cursor(seeded, SESSION, page)
    assert first.recorded == 3

    second = await transcript.advance_cursor(seeded, SESSION, page)

    assert second.recorded == 0
    assert second.duplicates == 3
    assert second.deliveries_created == 0
    assert second.deliveries_duplicate == 3
    assert second.advanced is False  # already there; the cursor did not move
    assert second.cursor_session_index == 2
    assert await transcript.count(seeded, SESSION) == 3
    assert await deliveries.pending_count(seeded, SESSION) == 3


async def test_cursor_never_advances_past_a_failed_insert(
    seeded: Database, message_factory: MessageFactory
) -> None:
    """A bad row rolls the whole page back — cursor included. No drops."""
    good = AdvanceItem(
        message=message_factory(0, session_id=SESSION), deliveries=(_draft(),)
    )
    poisoned = AdvanceItem(
        message=message_factory(1, session_id=SESSION),
        # 'telepathy' violates the CHECK on deliveries.kind.
        deliveries=(DeliveryDraft(chat_id=CHAT, kind="telepathy"),),
    )

    with pytest.raises(IntegrityError):
        await transcript.advance_cursor(seeded, SESSION, (good, poisoned))

    row = await sessions.get(seeded, SESSION)
    assert row is not None
    assert row.cursor_session_index == -1
    assert row.cursor_message_id is None
    assert await transcript.count(seeded, SESSION) == 0
    assert await deliveries.pending_count(seeded, SESSION) == 0

    # And the retry on the next poll succeeds cleanly.
    ok = await transcript.advance_cursor(seeded, SESSION, (good,))
    assert ok.recorded == 1
    assert ok.cursor_session_index == 0


async def test_a_message_for_an_unknown_session_is_never_silently_dropped(
    db: Database, message_factory: MessageFactory
) -> None:
    """The other half of the same guarantee: FK violations must raise, not skip.

    ``INSERT OR IGNORE`` would swallow this and let the cursor march on;
    ``ON CONFLICT DO NOTHING`` only forgives a uniqueness conflict.
    """
    ghost = AdvanceItem(message=message_factory(0, session_id="ghost"))
    with pytest.raises(IntegrityError):
        await transcript.advance_cursor(db, "ghost", (ghost,))
    assert await transcript.count(db) == 0


async def test_enqueue_surfaces_a_constraint_violation(seeded: Database) -> None:
    with pytest.raises(IntegrityError):
        await deliveries.enqueue(
            seeded,
            session_id=SESSION,
            message_id="card-1",
            chat_id=CHAT,
            kind="telepathy",
        )


async def test_cursor_is_monotonic(
    seeded: Database, message_factory: MessageFactory
) -> None:
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 5))
    stale = await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 2))

    assert stale.recorded == 1  # the message is still recorded…
    assert stale.advanced is False  # …but the cursor does not rewind
    assert stale.cursor_session_index == 5


async def test_advance_sorts_by_session_index(
    seeded: Database, message_factory: MessageFactory
) -> None:
    out_of_order = _items(message_factory, 7, 1, 4)
    result = await transcript.advance_cursor(seeded, SESSION, out_of_order)
    assert result.cursor_session_index == 7


async def test_advance_reports_turn_and_prompt_witnesses(
    seeded: Database, message_factory: MessageFactory
) -> None:
    echo = message_factory(
        0, session_id=SESSION, kind="userMessage", turn_id="prompt-1"
    )
    reply = message_factory(1, session_id=SESSION, turn_id="prompt-1")
    result = await transcript.advance_cursor(
        seeded, SESSION, (AdvanceItem(message=echo), AdvanceItem(message=reply))
    )
    assert result.turn_ids == frozenset({"prompt-1"})
    assert result.content_ids == frozenset({"prompt-1"})


async def test_advance_rejects_a_foreign_session(
    seeded: Database, message_factory: MessageFactory
) -> None:
    foreign = AdvanceItem(message=message_factory(0, session_id="somebody-else"))
    with pytest.raises(ValueError, match="belongs to session"):
        await transcript.advance_cursor(seeded, SESSION, (foreign,))


async def test_advance_with_no_items_reports_the_current_cursor(
    seeded: Database, message_factory: MessageFactory
) -> None:
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 3))
    result = await transcript.advance_cursor(seeded, SESSION, ())
    assert result.cursor_session_index == 3
    assert result.recorded == 0
    assert result.advanced is False


async def test_seek_to_end_skips_history(seeded: Database) -> None:
    await sessions.seek_to_end(seeded, SESSION, message_id="m:99:0", session_index=99)
    row = await sessions.get(seeded, SESSION)
    assert row is not None
    assert row.seeded is True
    assert row.cursor_session_index == 99


async def test_list_for_turn_and_recent(
    seeded: Database, message_factory: MessageFactory
) -> None:
    items = (
        AdvanceItem(message=message_factory(0, session_id=SESSION, turn_id="t-a")),
        AdvanceItem(message=message_factory(1, session_id=SESSION, turn_id="t-b")),
        AdvanceItem(message=message_factory(2, session_id=SESSION, turn_id="t-b")),
    )
    await transcript.advance_cursor(seeded, SESSION, items)

    turn = await transcript.list_for_turn(seeded, SESSION, "t-b")
    assert [m.session_index for m in turn] == [1, 2]
    newest = await transcript.recent(seeded, SESSION, limit=2)
    assert [m.session_index for m in newest] == [2, 1]
    assert await transcript.max_session_index(seeded, SESSION) == 2


# ── content cap and retention ────────────────────────────────────────────────


async def test_content_is_capped_at_64kb(
    seeded: Database, message_factory: MessageFactory
) -> None:
    huge = message_factory(0, session_id=SESSION, text="x" * (MAX_CONTENT_BYTES * 2))
    await transcript.advance_cursor(seeded, SESSION, (AdvanceItem(message=huge),))

    stored = await transcript.get(seeded, SESSION, huge.id)
    assert stored is not None
    assert stored.content_truncated is True
    assert stored.content_bytes > MAX_CONTENT_BYTES
    assert stored.content_json is not None
    assert len(stored.content_json.encode()) <= MAX_CONTENT_BYTES
    # The classification keys survive the cap, so the renderer still knows what
    # it was looking at.
    assert '"turnId"' in stored.content_json


async def test_small_content_is_stored_verbatim(
    seeded: Database, message_factory: MessageFactory
) -> None:
    message = message_factory(0, session_id=SESSION, text="hello")
    await transcript.advance_cursor(seeded, SESSION, (AdvanceItem(message=message),))
    stored = await transcript.get(seeded, SESSION, message.id)
    assert stored is not None
    assert stored.content_truncated is False
    assert "hello" in (stored.content_json or "")


def test_parse_received_at_ms_handles_the_api_shape() -> None:
    assert (
        transcript.parse_received_at_ms("2026-07-26 02:00:37.434+00") == 1785031237434
    )
    assert transcript.parse_received_at_ms("2026-07-26T02:00:37.434Z") == 1785031237434
    # Naive means UTC, not local.
    assert transcript.parse_received_at_ms("2026-07-26 02:00:37") == 1785031237000
    assert transcript.parse_received_at_ms("not a date") is None
    assert transcript.parse_received_at_ms(None) is None


async def test_unparseable_timestamps_are_not_instantly_prunable(
    seeded: Database, message_factory: MessageFactory
) -> None:
    broken = message_factory(0, session_id=SESSION).model_copy(
        update={"received_at": "???"}
    )
    stamp = now_ms()
    await transcript.advance_cursor(
        seeded, SESSION, (AdvanceItem(message=broken),), at=stamp
    )
    stored = await transcript.get(seeded, SESSION, broken.id)
    assert stored is not None
    assert stored.received_at_ms == stamp  # "now", not epoch 0
    assert await transcript.prune(seeded, at=stamp) == 0


async def test_prune_drops_only_content_older_than_30_days(
    seeded: Database, message_factory: MessageFactory
) -> None:
    now = datetime.now(UTC)
    old = message_factory(0, session_id=SESSION).model_copy(
        update={"received_at": (now - timedelta(days=40)).isoformat()}
    )
    fresh = message_factory(1, session_id=SESSION).model_copy(
        update={"received_at": (now - timedelta(days=2)).isoformat()}
    )
    await transcript.advance_cursor(
        seeded,
        SESSION,
        (
            AdvanceItem(message=old, deliveries=(_draft(),)),
            AdvanceItem(message=fresh, deliveries=(_draft(),)),
        ),
    )

    removed = await transcript.prune(seeded)

    assert removed == 1
    assert await transcript.get(seeded, SESSION, old.id) is None
    assert await transcript.get(seeded, SESSION, fresh.id) is not None
    # Delivery history is metadata and deliberately survives the content prune.
    assert await deliveries.pending_count(seeded, SESSION) == 2


# ── the delivery claim ───────────────────────────────────────────────────────


async def test_claim_returns_transcript_order(
    seeded: Database, message_factory: MessageFactory
) -> None:
    message = message_factory(4, session_id=SESSION)
    await transcript.advance_cursor(
        seeded,
        SESSION,
        (
            AdvanceItem(
                message=message,
                deliveries=(_draft(2), _draft(0), _draft(1)),
            ),
        ),
    )
    claimed = await deliveries.claim(seeded, claim_id="worker-a")
    assert [row.part_index for row in claimed] == [0, 1, 2]
    assert all(row.state == "sending" for row in claimed)


async def test_two_claimers_do_not_overlap(
    seeded: Database, message_factory: MessageFactory
) -> None:
    """Disjoint sets, every row claimed, no row claimed twice.

    Under SQLite the file lock made one claimer take everything; ``SKIP
    LOCKED`` lets both make progress instead. What must not change is the
    property that actually matters — no row reaches Telegram twice.
    """
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 0, 1, 2))

    first, second = await asyncio.gather(
        deliveries.claim(seeded, claim_id="a", limit=10),
        deliveries.claim(seeded, claim_id="b", limit=10),
    )

    keys = [row.key for row in first] + [row.key for row in second]
    assert len(keys) == len(set(keys)) == 3
    assert {row.claim_id for row in first} <= {"a"}
    assert {row.claim_id for row in second} <= {"b"}


async def test_two_connections_cannot_claim_the_same_row(
    seeded: Database, message_factory: MessageFactory
) -> None:
    """The redeploy-overlap case: two pools, one database, one winner.

    Two genuinely independent pools model a redeploy overlap far better than
    two handles on one SQLite file ever did — there is no shared lock to make
    the answer come out right by accident.
    """
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 0))

    other = await Database(app_dsn(), min_size=1, max_size=2).connect()
    try:
        async with tenant_scope(BOOTSTRAP_TENANT_ID):
            first, second = await asyncio.gather(
                deliveries.claim(seeded, claim_id="proc-1", limit=10),
                deliveries.claim(other, claim_id="proc-2", limit=10),
            )
    finally:
        await other.close()

    assert len(first) + len(second) == 1
    winner = (first or second)[0]
    stored = await deliveries.get(seeded, winner.key)
    assert stored is not None
    assert stored.state == "sending"
    assert stored.claim_id in ("proc-1", "proc-2")


async def test_claim_can_be_scoped_to_one_session(
    seeded: Database, message_factory: MessageFactory
) -> None:
    await sessions.upsert(seeded, "sess-2", workspace_id=WORKSPACE)
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 0))
    other = message_factory(0, session_id="sess-2")
    await transcript.advance_cursor(
        seeded, "sess-2", (AdvanceItem(message=other, deliveries=(_draft(),)),)
    )

    claimed = await deliveries.claim(seeded, claim_id="a", session_id="sess-2")

    assert [row.session_id for row in claimed] == ["sess-2"]
    assert await deliveries.pending_count(seeded, SESSION) == 1


async def test_claim_respects_the_limit(
    seeded: Database, message_factory: MessageFactory
) -> None:
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 0, 1, 2))
    claimed = await deliveries.claim(seeded, claim_id="a", limit=2)
    assert len(claimed) == 2
    assert await deliveries.pending_count(seeded, SESSION) == 1


async def test_mark_sent_failed_and_requeue(
    seeded: Database, message_factory: MessageFactory
) -> None:
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 0, 1))
    claimed = await deliveries.claim(seeded, claim_id="a")

    sent = await deliveries.mark_sent(seeded, claimed[0].key, tg_message_id=555)
    assert sent is not None
    assert sent.state == "sent"
    assert sent.tg_message_id == 555
    assert sent.claim_id is None

    retried = await deliveries.mark_failed(
        seeded, claimed[1].key, error="flood wait", retry=True
    )
    assert retried is not None
    assert retried.state == "pending"
    assert retried.attempts == 1

    dead = await deliveries.mark_failed(
        seeded, claimed[1].key, error="bot was kicked", retry=False
    )
    assert dead is not None
    assert dead.state == "failed"
    assert dead.attempts == 2

    back = await deliveries.requeue(seeded, claimed[1].key)
    assert back is not None
    assert back.state == "pending"

    skipped = await deliveries.mark_skipped(seeded, claimed[0].key, reason="too quiet")
    assert skipped is not None
    assert skipped.state == "skipped"

    assert await deliveries.counts_by_state(seeded, SESSION) == {
        "pending": 1,
        "skipped": 1,
    }


async def test_release_hands_back_unsent_rows(
    seeded: Database, message_factory: MessageFactory
) -> None:
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 0, 1))
    await deliveries.claim(seeded, claim_id="a")

    released = await deliveries.release(seeded, "a")

    assert released == 2
    assert await deliveries.pending_count(seeded, SESSION) == 2


async def test_boot_recovers_orphans_and_skips_proven_duplicates(
    seeded: Database, message_factory: MessageFactory
) -> None:
    """At-least-once by design, with the cheap duplicate guard in front."""
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 0, 1))
    claimed = await deliveries.claim(seeded, claim_id="dead-worker")
    assert len(claimed) == 2

    # Row 0's payload is already known to have landed in this chat.
    duplicate = claimed[0]
    assert duplicate.payload_json is not None
    await deliveries.enqueue(
        seeded,
        session_id=SESSION,
        message_id="synthetic-echo",
        chat_id=CHAT,
        thread_id=TOPIC,
        payload_json=duplicate.payload_json,
    )
    echo_key = (SESSION, "synthetic-echo", 0, CHAT)
    await deliveries.mark_sent(seeded, echo_key, tg_message_id=1)

    # Recovery only takes rows whose claim has gone stale; a fresh one may
    # still be in flight on a live peer. Jump past the orphan window.
    result = await deliveries.recover_orphaned(
        seeded, claim_id="new-worker", at=now_ms() + deliveries.ORPHAN_AFTER_MS + 1
    )

    assert [row.key for row in result.skipped] == [duplicate.key]
    assert [row.key for row in result.claimed] == [claimed[1].key]
    assert all(row.claim_id == "new-worker" for row in result.claimed)

    skipped = await deliveries.get(seeded, duplicate.key)
    assert skipped is not None
    assert skipped.state == "skipped"
    reclaimed = await deliveries.get(seeded, claimed[1].key)
    assert reclaimed is not None
    assert reclaimed.state == "sending"
    assert reclaimed.claim_id == "new-worker"


async def test_recover_ignores_duplicates_outside_the_window(
    seeded: Database, message_factory: MessageFactory
) -> None:
    await transcript.advance_cursor(seeded, SESSION, _items(message_factory, 0))
    claimed = await deliveries.claim(seeded, claim_id="dead-worker")
    payload = claimed[0].payload_json
    assert payload is not None
    await deliveries.enqueue(
        seeded,
        session_id=SESSION,
        message_id="ancient",
        chat_id=CHAT,
        thread_id=TOPIC,
        payload_json=payload,
    )
    stale = now_ms() - 60 * 60 * 1000
    await deliveries.mark_sent(seeded, (SESSION, "ancient", 0, CHAT), at=stale)

    result = await deliveries.recover_orphaned(
        seeded, claim_id="new-worker", at=now_ms() + deliveries.ORPHAN_AFTER_MS + 1
    )

    assert result.skipped == ()
    assert len(result.claimed) == 1


async def test_enqueue_is_idempotent(seeded: Database) -> None:
    created = await deliveries.enqueue(
        seeded,
        session_id=SESSION,
        message_id="card-1",
        chat_id=CHAT,
        payload_json='{"html":"hi"}',
    )
    again = await deliveries.enqueue(
        seeded,
        session_id=SESSION,
        message_id="card-1",
        chat_id=CHAT,
        payload_json='{"html":"hi"}',
    )
    assert created is True
    assert again is False
    row = await deliveries.get(seeded, (SESSION, "card-1", 0, CHAT))
    assert row is not None
    assert row.content_hash == deliveries.content_hash('{"html":"hi"}')


async def test_telegram_reply_lookup_finds_delivery_then_status_card(
    seeded: Database,
) -> None:
    await deliveries.enqueue(
        seeded,
        session_id=SESSION,
        message_id="agent-answer",
        chat_id=CHAT,
        payload_json='{"html":"done"}',
    )
    await deliveries.mark_sent(
        seeded,
        (SESSION, "agent-answer", 0, CHAT),
        tg_message_id=7001,
    )

    assert await deliveries.session_for_telegram_message(seeded, CHAT, 7001) == SESSION

    await sessions.set_status_card(seeded, SESSION, 7002)
    assert await deliveries.session_for_telegram_message(seeded, CHAT, 7002) == SESSION
    assert await deliveries.session_for_telegram_message(seeded, CHAT, 9999) is None


# ── the singleton lease ──────────────────────────────────────────────────────


async def test_two_acquirers_produce_exactly_one_winner(
    system_db: Database,
) -> None:
    other = await Database(worker_dsn(), min_size=1, max_size=2, system=True).connect()
    try:
        first, second = await asyncio.gather(
            lease.acquire(system_db, holder="proc-1"),
            lease.acquire(other, holder="proc-2"),
        )
    finally:
        await other.close()

    winners = [held for held in (first, second) if held is not None]
    assert len(winners) == 1
    held = await lease.get(system_db)
    assert held is not None
    assert held.holder == winners[0].holder


async def test_the_loser_cannot_take_a_live_lease(system_db: Database) -> None:
    stamp = now_ms()
    assert await lease.acquire(system_db, holder="a", at=stamp) is not None
    assert await lease.acquire(system_db, holder="b", at=stamp + 1_000) is None
    held = await lease.get(system_db)
    assert held is not None
    assert held.holder == "a"


async def test_an_expired_lease_is_stealable(system_db: Database) -> None:
    stamp = now_ms()
    first = await lease.acquire(system_db, holder="a", at=stamp, ttl_ms=lease.TTL_MS)
    assert first is not None
    stolen = await lease.acquire(system_db, holder="b", at=stamp + lease.TTL_MS + 1)
    assert stolen is not None
    assert stolen.holder == "b"


async def test_reacquiring_keeps_the_original_acquired_at(system_db: Database) -> None:
    stamp = now_ms()
    first = await lease.acquire(system_db, holder="a", at=stamp)
    assert first is not None
    renewed = await lease.acquire(system_db, holder="a", at=stamp + 4_000)
    assert renewed is not None
    assert renewed.acquired_at == first.acquired_at
    assert renewed.expires_at > first.expires_at


async def test_heartbeat_reports_a_lost_lease(system_db: Database) -> None:
    stamp = now_ms()
    assert await lease.acquire(system_db, holder="a", at=stamp) is not None
    beat = await lease.heartbeat(system_db, holder="a", at=stamp + lease.HEARTBEAT_MS)
    assert beat is not None
    assert beat.expires_at == stamp + lease.HEARTBEAT_MS + lease.TTL_MS

    # Somebody else took over while we were away.
    await lease.acquire(system_db, holder="b", at=stamp + 60_000)
    assert await lease.heartbeat(system_db, holder="a", at=stamp + 61_000) is None


async def test_release_only_works_for_the_holder(system_db: Database) -> None:
    assert await lease.acquire(system_db, holder="a") is not None
    assert await lease.release(system_db, holder="b") is False
    assert await lease.release(system_db, holder="a") is True
    assert await lease.get(system_db) is None


def test_instance_id_is_unique() -> None:
    assert lease.instance_id() != lease.instance_id()


# ── outbound prompts ─────────────────────────────────────────────────────────


async def test_prompt_ledger_lifecycle(seeded: Database) -> None:
    row = await prompts.create(
        seeded,
        session_id=SESSION,
        body="ship it",
        chat_id=CHAT,
        thread_id=TOPIC,
        index_at_post=7,
    )
    assert row.state == "pending"  # written BEFORE the HTTP call
    assert row.attempts == 0

    attempted = await prompts.record_attempt(seeded, row.message_id, error="timeout")
    assert attempted is not None
    assert attempted.attempts == 1
    assert attempted.state == "pending"  # still recoverable, same messageId

    posted = await prompts.mark_posted(seeded, row.message_id, post_state="queued")
    assert posted is not None
    assert posted.state == "posted"
    assert posted.post_state == "queued"
    assert posted.last_error is None
    assert await prompts.outstanding_count(seeded, SESSION) == 1

    witnessed = await prompts.mark_witnessed(seeded, row.message_id)
    assert witnessed is not None
    assert witnessed.state == "witnessed"
    assert witnessed.turn_id == row.message_id  # turnId == our messageId
    assert await prompts.outstanding_count(seeded, SESSION) == 0


async def test_only_unconfirmed_prompts_are_recoverable(seeded: Database) -> None:
    pending = await prompts.create(seeded, session_id=SESSION, body="a")
    posted = await prompts.create(seeded, session_id=SESSION, body="b")
    await prompts.mark_posted(seeded, posted.message_id)

    recoverable = await prompts.list_recoverable(seeded)

    assert [row.message_id for row in recoverable] == [pending.message_id]
    assert len(await prompts.list_unsettled(seeded, SESSION)) == 2


async def test_witness_many_moves_only_unsettled_prompts(seeded: Database) -> None:
    first = await prompts.create(seeded, session_id=SESSION, body="a")
    second = await prompts.create(seeded, session_id=SESSION, body="b")
    await prompts.mark_posted(seeded, first.message_id)
    await prompts.mark_posted(seeded, second.message_id)

    moved = await prompts.witness_many(
        seeded, SESSION, [first.message_id, "unknown-id", first.message_id]
    )

    assert moved == [first.message_id]
    settled = await prompts.get(seeded, first.message_id)
    assert settled is not None
    assert settled.state == "witnessed"
    assert settled.witnessed_at is not None
    assert await prompts.outstanding_count(seeded, SESSION) == 1

    assert await prompts.witness_many(seeded, SESSION, []) == []


async def test_failed_and_abandoned_prompts_stop_blocking_finalize(
    seeded: Database,
) -> None:
    failed = await prompts.create(seeded, session_id=SESSION, body="a")
    aged = await prompts.create(seeded, session_id=SESSION, body="b")

    await prompts.mark_failed(seeded, failed.message_id, error="400 bad model")
    await prompts.abandon(seeded, aged.message_id)

    assert await prompts.outstanding_count(seeded, SESSION) == 0
    rows = {
        row.message_id: row.state
        for row in await prompts.list_for_session(seeded, SESSION)
    }
    assert rows == {failed.message_id: "failed", aged.message_id: "abandoned"}


async def test_prompt_ids_are_unique_and_deleting_a_session_cascades(
    seeded: Database,
) -> None:
    assert prompts.new_message_id() != prompts.new_message_id()
    await prompts.create(seeded, session_id=SESSION, body="a")
    await sessions.delete(seeded, SESSION)
    assert await prompts.list_unsettled(seeded, SESSION) == []


# ── sessions and the turn context ────────────────────────────────────────────


async def test_turn_context_survives_a_restart(seeded: Database) -> None:
    boot_wall = now_ms()
    prompt = await prompts.create(seeded, session_id=SESSION, body="hello")
    await prompts.mark_posted(seeded, prompt.message_id, at=boot_wall - 30_000)

    context = await sessions.load_turn_context(
        seeded, SESSION, now=1_000.0, wall_ms=boot_wall
    )
    assert context is not None
    assert context.state is TurnState.IDLE
    assert [p.message_id for p in context.pending_prompts] == [prompt.message_id]
    assert context.pending_prompts[0].posted_at == pytest.approx(970.0)

    working = context.enter(
        TurnState.WORKING,
        1_010.0,
        start_witnessed=True,
        last_delta_at=1_005.0,
        turn_started_at=1_000.0,
        tool_calls=3,
        cadence_ms=6_000,
        status_card_msg_id=987,
        consecutive_idle=1,
    )
    await sessions.save_turn_context(
        seeded, SESSION, working, now=1_010.0, wall_ms=boot_wall
    )

    row = await sessions.get(seeded, SESSION)
    assert row is not None
    assert row.turn_state == "WORKING"
    assert row.start_witnessed is True
    assert row.poll_interval_ms == 6_000
    assert row.status_card_msg_id == 987
    assert row.last_delta_at == boot_wall - 5_000

    # A new process: monotonic origin moves, ages are preserved.
    restored = await sessions.load_turn_context(
        seeded, SESSION, now=50.0, wall_ms=boot_wall + 2_000
    )
    assert restored is not None
    assert restored.state is TurnState.WORKING
    assert restored.start_witnessed is True
    assert restored.tool_calls == 3
    assert restored.cadence_ms == 6_000
    assert restored.quiet_for(50.0) == pytest.approx(7.0)


async def test_load_turn_context_includes_the_workspace_status(
    seeded: Database,
) -> None:
    await workspaces.update_status(
        seeded, WORKSPACE, status="initializing", lifecycle_step="cloning"
    )
    context = await sessions.load_turn_context(seeded, SESSION, now=0.0)
    assert context is not None
    assert context.workspace_status is not None
    assert context.workspace_status.is_waking is True
    assert context.lifecycle_step == "cloning"


async def test_load_turn_context_of_an_unknown_session(db: Database) -> None:
    assert await sessions.load_turn_context(db, "nope", now=0.0) is None


async def test_save_turn_context_unbinds_a_dead_session(seeded: Database) -> None:
    context = await sessions.load_turn_context(seeded, SESSION, now=0.0)
    assert context is not None
    await sessions.save_turn_context(
        seeded, SESSION, context.enter(TurnState.DEAD, 1.0), now=1.0
    )
    row = await sessions.get(seeded, SESSION)
    assert row is not None
    assert row.is_bound is False
    assert row.dead_at is not None


async def test_record_status_keeps_error_text_and_clears_it(seeded: Database) -> None:
    errored = await sessions.record_status(
        seeded,
        SESSION,
        status=SessionStatusValue.ERROR,
        error_message="Codex ChatGPT auth not found",
    )
    assert errored is not None
    assert errored.status_value is SessionStatusValue.ERROR
    assert errored.error_text == "Codex ChatGPT auth not found"

    recovered = await sessions.record_status(
        seeded, SESSION, status=SessionStatusValue.WORKING
    )
    assert recovered is not None
    assert recovered.error_text is None


async def test_bound_sessions_drive_the_supervisor(seeded: Database) -> None:
    await sessions.upsert(seeded, "sess-2", workspace_id=WORKSPACE)
    await sessions.bind(seeded, "sess-2", chat_id=CHAT, thread_id=99)
    await sessions.update(seeded, "sess-2", turn_state="WORKING")

    bound = await sessions.list_bound(seeded)
    assert [row.id for row in bound] == ["sess-2", SESSION]  # active first

    assert await sessions.get_bound_for(seeded, CHAT, 99) is not None
    await sessions.unbind(seeded, "sess-2")
    assert await sessions.get_bound_for(seeded, CHAT, 99) is None
    assert [row.id for row in await sessions.list_bound(seeded)] == [SESSION]


async def test_mark_dead_is_terminal(seeded: Database) -> None:
    row = await sessions.mark_dead(seeded, SESSION, reason="404 from /messages")
    assert row is not None
    assert row.state is TurnState.DEAD
    assert row.is_bound is False
    assert row.last_error == "404 from /messages"
    assert await sessions.list_bound(seeded) == []


async def test_session_helpers(seeded: Database) -> None:
    assert await sessions.set_poll_interval(seeded, SESSION, 12_000) is not None
    assert await sessions.set_status_card(seeded, SESSION, 4321) is not None
    assert await sessions.clear_cursor_only(seeded, SESSION) is not None
    stamped = await sessions.touch_prompt(seeded, SESSION)
    assert stamped is not None
    assert stamped.last_prompt_at is not None
    assert stamped.poll_interval_ms == 12_000
    assert stamped.status_card_msg_id == 4321
    assert [row.id for row in await sessions.list_for_workspace(seeded, WORKSPACE)] == [
        SESSION
    ]
    assert len(await sessions.list_all(seeded)) == 1
    assert await sessions.delete(seeded, SESSION) is True
    assert await sessions.get(seeded, SESSION) is None


# ── chats ────────────────────────────────────────────────────────────────────


async def test_chat_ensure_bind_and_unbind(seeded: Database) -> None:
    created = await chats.ensure(seeded, CHAT, TOPIC)
    assert created.verbosity == "normal"
    assert created.notify == "quiet"
    assert created.is_bound is False

    bound = await chats.bind(
        seeded, CHAT, TOPIC, workspace_id=WORKSPACE, session_id=SESSION
    )
    assert bound.is_bound is True
    assert [row.key for row in await chats.list_bound(seeded)] == [(CHAT, TOPIC)]
    assert [row.key for row in await chats.for_session(seeded, SESSION)] == [
        (CHAT, TOPIC)
    ]
    assert [row.key for row in await chats.for_workspace(seeded, WORKSPACE)] == [
        (CHAT, TOPIC)
    ]

    unbound = await chats.unbind(seeded, CHAT, TOPIC)
    assert unbound is not None
    assert unbound.is_bound is False
    assert unbound.workspace_id is None


async def test_chat_defaults_are_partially_updatable(seeded: Database) -> None:
    await chats.ensure(seeded, CHAT, TOPIC)
    await chats.set_defaults(seeded, CHAT, TOPIC, project_id="proj-1", agent="claude")
    await chats.set_defaults(seeded, CHAT, TOPIC, model="opus-5-1m")

    row = await chats.get(seeded, CHAT, TOPIC)
    assert row is not None
    assert row.default_project_id == "proj-1"
    assert row.default_agent == "claude"  # not clobbered by the second call
    assert row.default_model == "opus-5-1m"

    cleared = await chats.set_defaults(seeded, CHAT, TOPIC, agent=None)
    assert cleared is not None
    assert cleared.default_agent is None  # None means NULL, UNSET means "leave it"


async def test_chat_focus_window(seeded: Database) -> None:
    stamp = now_ms()
    await chats.ensure(seeded, CHAT, TOPIC)
    row = await chats.touch_prompt(seeded, CHAT, TOPIC, focus_for_ms=60_000, at=stamp)
    assert row is not None
    assert row.last_prompt_at == stamp
    assert row.is_focused(stamp + 1_000) is True
    assert row.is_focused(stamp + 61_000) is False


async def test_chat_presentation_and_delete(seeded: Database) -> None:
    await chats.ensure(seeded, CHAT, NO_THREAD_ID, kind="general")
    await chats.set_verbosity(seeded, CHAT, NO_THREAD_ID, verbosity="verbose")
    row = await chats.set_notify(seeded, CHAT, NO_THREAD_ID, notify="loud")
    assert row is not None
    assert row.kind == "general"
    assert row.verbosity == "verbose"
    assert row.notify == "loud"
    assert len(await chats.list_all(seeded)) == 1
    assert await chats.delete(seeded, CHAT, NO_THREAD_ID) is True


async def test_update_with_nothing_is_a_read(seeded: Database) -> None:
    await chats.ensure(seeded, CHAT, TOPIC)
    assert await chats.update(seeded, CHAT, TOPIC) is not None
    assert await sessions.update(seeded, SESSION) is not None
    assert await workspaces.update(seeded, WORKSPACE) is not None


# ── workspaces ───────────────────────────────────────────────────────────────


async def test_workspace_nonce_reconciliation(db: Database) -> None:
    """POST /v0/workspaces has no idempotency key; the nonce closes the loop."""
    await workspaces.upsert(db, "ws-9", name="tg-42-abcd1234", create_nonce="abcd1234")
    found = await workspaces.get_by_nonce(db, "abcd1234")
    assert found is not None
    assert found.id == "ws-9"
    assert await workspaces.get_by_nonce(db, "nope") is None


async def test_workspace_status_stamps_lifecycle_once(db: Database) -> None:
    await workspaces.upsert(db, "ws-9", project_id="p")
    first = await workspaces.update_status(
        db, "ws-9", status="initializing", lifecycle_step="cloning", at=1_000
    )
    assert first is not None
    assert first.init_started_at == 1_000

    again = await workspaces.update_status(db, "ws-9", status="initializing", at=2_000)
    assert again is not None
    assert again.init_started_at == 1_000  # not moved
    assert again.last_status_at == 2_000
    assert again.lifecycle_step is None

    ready = await workspaces.update_status(db, "ws-9", status="ready", at=3_000)
    assert ready is not None
    assert ready.ready_at == 3_000
    assert ready.status_value.is_usable is True

    assert await workspaces.update_status(db, "missing", status="ready") is None


async def test_workspace_topic_binding_and_listing(db: Database) -> None:
    await workspaces.upsert(db, "ws-9", project_id="p")
    bound = await workspaces.bind_topic(
        db, "ws-9", chat_id=CHAT, topic_id=7, topic_name="ws-9"
    )
    assert bound is not None
    assert bound.has_topic is True
    assert await workspaces.get_by_topic(db, CHAT, 7) is not None

    marked = await workspaces.set_topic_marker(db, "ws-9", "working")
    assert marked is not None
    assert marked.topic_marker == "working"

    assert [row.id for row in await workspaces.list_for_project(db, "p")] == ["ws-9"]
    archived = await workspaces.mark_archived(db, "ws-9")
    assert archived is not None
    assert archived.status_value.is_gone is True
    assert await workspaces.list_all(db) == []
    assert len(await workspaces.list_all(db, include_archived=True)) == 1

    unbound = await workspaces.unbind_topic(db, "ws-9")
    assert unbound is not None
    assert unbound.has_topic is False
    assert await workspaces.delete(db, "ws-9") is True


async def test_deleting_a_workspace_cascades_to_sessions(seeded: Database) -> None:
    await workspaces.delete(seeded, WORKSPACE)
    assert await sessions.get(seeded, SESSION) is None


# ── wizard state ─────────────────────────────────────────────────────────────


async def test_wizard_state_round_trip_and_expiry(db: Database) -> None:
    stamp = now_ms()
    row = await wizard.set_state(
        db,
        CHAT,
        TOPIC,
        user_id=1001,
        state_key="pick_project",
        data={"project_id": "p-1"},
        tg_message_id=77,
        at=stamp,
    )
    assert row.state_key == "pick_project"
    assert row.data == {"project_id": "p-1"}
    assert row.tg_message_id == 77

    advanced = await wizard.merge_data(
        db,
        CHAT,
        TOPIC,
        user_id=1001,
        patch={"branch": "main"},
        state_key="pick_branch",
        at=stamp,
    )
    assert advanced.data == {"project_id": "p-1", "branch": "main"}
    assert advanced.tg_message_id == 77  # the edited-in-place message is kept

    assert await wizard.get(db, CHAT, TOPIC, user_id=1001) is not None
    assert await wizard.get(db, CHAT, TOPIC, user_id=9999) is None

    later = stamp + wizard.DEFAULT_TTL_MS + 1
    assert await wizard.get(db, CHAT, TOPIC, user_id=1001, at=later) is None
    assert await wizard.list_active(db, at=later) == []
    assert await wizard.prune_expired(db, at=later) == 1
    assert await wizard.prune_expired(db, at=later) == 0


async def test_wizard_clear_and_message_replacement(db: Database) -> None:
    await wizard.set_state(db, CHAT, TOPIC, user_id=1, state_key="a", tg_message_id=5)
    replaced = await wizard.set_state(
        db, CHAT, TOPIC, user_id=1, state_key="b", tg_message_id=None
    )
    assert replaced.tg_message_id is None
    assert replaced.data == {}
    assert await wizard.clear(db, CHAT, TOPIC, user_id=1) is True
    assert await wizard.clear(db, CHAT, TOPIC, user_id=1) is False


async def test_wizards_in_the_same_topic_do_not_collide(db: Database) -> None:
    await wizard.set_state(db, CHAT, TOPIC, user_id=1, state_key="a")
    await wizard.set_state(db, CHAT, TOPIC, user_id=2, state_key="b")
    mine = await wizard.get(db, CHAT, TOPIC, user_id=1)
    theirs = await wizard.get(db, CHAT, TOPIC, user_id=2)
    assert mine is not None and mine.state_key == "a"
    assert theirs is not None and theirs.state_key == "b"


# ── api events and unknown content types ─────────────────────────────────────


async def test_api_events_ring_buffer(system_db: Database) -> None:
    stamp = now_ms()
    for index in range(5):
        await events.record_api_event(
            system_db,
            method="GET",
            endpoint="/sessions/{id}/messages",
            status_code=200,
            duration_ms=100 + index,
            at=stamp + index,
        )
    await events.record_api_event(
        system_db,
        method="POST",
        endpoint="/sessions/{id}/messages",
        status_code=502,
        duration_ms=900,
        attempt=2,
        ok=False,
        error="bad gateway",
        circuit_state="half_open",
        at=stamp + 10,
    )

    recent = await events.recent_api_events(system_db, limit=2)
    assert [event.status_code for event in recent] == [502, 200]
    failures = await events.recent_api_events(system_db, only_failed=True)
    assert len(failures) == 1
    assert failures[0].attempt == 2
    assert failures[0].circuit_state == "half_open"

    window = await events.stats(system_db, since_ms=stamp)
    assert window.total == 6
    assert window.ok == 5
    assert window.failed == 1
    assert window.last_error == "bad gateway"
    assert window.max_duration_ms == 900
    assert 0.16 < window.error_rate < 0.17

    assert await events.prune_api_events(system_db, keep=2) == 4
    assert len(await events.recent_api_events(system_db, limit=50)) == 2
    assert (await events.stats(system_db, since_ms=stamp + 999_999)).total == 0


async def test_shape_signature_ignores_values(seeded: Database) -> None:
    one = {"type": "mystery", "payload": {"n": 1}, "items": [{"k": "a"}]}
    two = {"type": "mystery", "payload": {"n": 999}, "items": [{"k": "zzzz"}]}
    different = {"type": "mystery", "payload": {"n": 1, "extra": True}}

    assert events.shape_signature(one) == events.shape_signature(two)
    assert events.shape_signature(one) != events.shape_signature(different)
    assert events.shape_signature({}) != events.shape_signature([])


async def test_unknown_content_types_are_counted_not_stored(seeded: Database) -> None:
    signature = events.shape_signature({"type": "mystery", "secret": "source code"})
    first = await events.note_unknown_content_type(
        seeded,
        content_type="mystery",
        signature=signature,
        session_id=SESSION,
        message_id="m-1",
        at=1_000,
    )
    second = await events.note_unknown_content_type(
        seeded,
        content_type="mystery",
        signature=signature,
        session_id=SESSION,
        message_id="m-2",
        at=2_000,
    )

    assert (first, second) == (1, 2)
    rows = await events.list_unknown_content_types(seeded)
    assert len(rows) == 1
    row = rows[0]
    assert row.count == 2
    assert row.sample_message_id == "m-1"  # the oldest reproducible case
    assert row.first_seen_at == 1_000
    assert row.last_seen_at == 2_000
    # Only a pointer and a shape are stored — never the content.
    assert "source code" not in str(row)


async def test_deep_shapes_are_bounded(seeded: Database) -> None:
    nested: dict[str, Any] = {"leaf": 1}
    for _ in range(50):
        nested = {"child": nested}
    assert len(events.shape_signature(nested)) == 16


# ── tenancy ──────────────────────────────────────────────────────────────────


async def test_a_tenant_is_created_with_its_first_owner(system_db: Database) -> None:
    tenant = await tenancy.create(
        system_db, slug="acme", name="Acme", owner_user_id=42, status="active"
    )
    assert tenant.slug == "acme"
    assert tenant.status == "active"
    seated = await tenancy.member(system_db, tenant.id, 42)
    assert seated is not None and seated.role == "owner"
    assert await tenancy.list_owner_ids(system_db, tenant.id) == (42,)


async def test_members_are_added_promoted_and_removed(system_db: Database) -> None:
    tenant = await tenancy.create(system_db, slug="duo", name="Duo", owner_user_id=1)
    # The co-founder path: one more row, same group, same Conductor key.
    await tenancy.add_member(system_db, tenant.id, 2, role="member")
    assert [m.user_id for m in await tenancy.list_members(system_db, tenant.id)] == [
        1,
        2,
    ]

    promoted = await tenancy.add_member(system_db, tenant.id, 2, role="admin")
    assert promoted.role == "admin"
    assert promoted.is_owner is True  # admins pass the owner-only gate

    assert await tenancy.remove_member(system_db, tenant.id, 2) is True
    assert await tenancy.member(system_db, tenant.id, 2) is None


async def test_the_last_owner_cannot_be_removed(system_db: Database) -> None:
    """Otherwise a workspace can be orphaned with no way back in."""
    tenant = await tenancy.create(system_db, slug="solo", name="Solo", owner_user_id=7)
    await tenancy.add_member(system_db, tenant.id, 8, role="member")
    assert await tenancy.remove_member(system_db, tenant.id, 7) is False
    assert await tenancy.member(system_db, tenant.id, 7) is not None


async def test_a_chat_belongs_to_exactly_one_tenant(system_db: Database) -> None:
    first = await tenancy.create(system_db, slug="one", name="One", owner_user_id=1)
    second = await tenancy.create(system_db, slug="two", name="Two", owner_user_id=2)
    await tenancy.bind_chat(system_db, -500, first.id, is_primary=True)

    with pytest.raises(ValueError, match="already bound"):
        await tenancy.bind_chat(system_db, -500, second.id)

    binding = await tenancy.chat_for(system_db, -500)
    assert binding is not None and binding.tenant_id == first.id


async def test_only_one_chat_is_primary_per_tenant(system_db: Database) -> None:
    tenant = await tenancy.create(system_db, slug="p", name="P", owner_user_id=1)
    await tenancy.bind_chat(system_db, -600, tenant.id, is_primary=True)
    await tenancy.bind_chat(system_db, -601, tenant.id, is_primary=True)
    primary = await tenancy.primary_chat(system_db, tenant.id)
    assert primary is not None and primary.chat_id == -601


async def test_storing_a_key_clears_a_previous_auth_failure(
    system_db: Database,
) -> None:
    """A new key must restart polling without waiting for a redeploy."""
    tenant = await tenancy.create(system_db, slug="k", name="K", owner_user_id=1)
    await tenancy.mark_auth_failed(system_db, tenant.id, reason="401")
    failed = await tenancy.get(system_db, tenant.id)
    assert failed is not None and failed.auth_failed_at is not None

    updated = await tenancy.set_conductor_key(
        system_db, tenant.id, ciphertext=b"sealed", kid="v1", fingerprint="abc"
    )
    assert updated is not None
    assert updated.auth_failed_at is None
    assert updated.has_conductor_key is True


async def test_a_tenant_row_never_prints_its_sealed_keys(
    system_db: Database,
) -> None:
    tenant = await tenancy.create(system_db, slug="r", name="R", owner_user_id=1)
    stored = await tenancy.set_conductor_key(
        system_db, tenant.id, ciphertext=b"SEALEDBYTES", kid="v1", fingerprint="fp"
    )
    assert stored is not None
    assert "SEALED" not in repr(stored)
    assert repr(stored) == "TenantRow(slug='r', status='pending')"


async def test_an_enrollment_token_is_single_use(system_db: Database) -> None:
    tenant = await tenancy.create(system_db, slug="e", name="E", owner_user_id=1)
    await tenancy.create_enrollment_token(
        system_db,
        token_hash="hash-1",
        tenant_id=tenant.id,
        user_id=1,
        purpose="bind_chat",
        ttl_ms=60_000,
    )
    first = await tenancy.consume_enrollment_token(
        system_db, token_hash="hash-1", purpose="bind_chat"
    )
    assert first == (tenant.id, 1)
    assert (
        await tenancy.consume_enrollment_token(
            system_db, token_hash="hash-1", purpose="bind_chat"
        )
        is None
    )


async def test_an_expired_enrollment_token_is_refused(system_db: Database) -> None:
    tenant = await tenancy.create(system_db, slug="x", name="X", owner_user_id=1)
    await tenancy.create_enrollment_token(
        system_db,
        token_hash="hash-2",
        tenant_id=tenant.id,
        user_id=1,
        purpose="set_key",
        ttl_ms=1,
        at=1_000,
    )
    assert (
        await tenancy.consume_enrollment_token(
            system_db, token_hash="hash-2", purpose="set_key", at=100_000
        )
        is None
    )


async def test_issuing_a_token_invalidates_the_previous_one(
    system_db: Database,
) -> None:
    """A leaked older code must stop working the moment a new one is sent."""
    tenant = await tenancy.create(system_db, slug="i", name="I", owner_user_id=1)
    for digest in ("old", "new"):
        await tenancy.create_enrollment_token(
            system_db,
            token_hash=digest,
            tenant_id=tenant.id,
            user_id=1,
            purpose="bind_chat",
            ttl_ms=60_000,
        )
    assert (
        await tenancy.consume_enrollment_token(
            system_db, token_hash="old", purpose="bind_chat"
        )
        is None
    )
    assert (
        await tenancy.consume_enrollment_token(
            system_db, token_hash="new", purpose="bind_chat"
        )
        is not None
    )


async def test_an_admin_cannot_demote_the_owner(system_db: Database) -> None:
    """Admins pass the same command gate as owners, so this is a real path.

    Demote-then-nothing-grants-it-back is a one-way door: no Telegram command
    creates an ``owner``, so the workspace would have no administrator at all.
    """
    tenant = await tenancy.create(system_db, slug="d", name="D", owner_user_id=1)
    await tenancy.add_member(system_db, tenant.id, 2, role="admin", added_by=1)

    with pytest.raises(tenancy.RoleError, match="only an owner"):
        await tenancy.add_member(system_db, tenant.id, 1, role="member", added_by=2)

    still = await tenancy.member(system_db, tenant.id, 1)
    assert still is not None and still.role == "owner"


async def test_an_owner_can_demote_another_owner(system_db: Database) -> None:
    tenant = await tenancy.create(system_db, slug="d2", name="D", owner_user_id=1)
    await tenancy.add_member(system_db, tenant.id, 2, role="owner", added_by=1)

    demoted = await tenancy.add_member(
        system_db, tenant.id, 2, role="member", added_by=1
    )
    assert demoted.role == "member"


async def test_an_admin_cannot_remove_the_owner(system_db: Database) -> None:
    """The other route to the same one-way door."""
    tenant = await tenancy.create(system_db, slug="d3", name="D", owner_user_id=1)
    await tenancy.add_member(system_db, tenant.id, 2, role="admin", added_by=1)

    assert await tenancy.remove_member(system_db, tenant.id, 1, removed_by=2) is False
    assert await tenancy.member(system_db, tenant.id, 1) is not None


async def test_admins_do_not_count_as_owners_for_the_last_owner_guard(
    system_db: Database,
) -> None:
    """An admin is not a substitute; removing the owner would strand it."""
    tenant = await tenancy.create(system_db, slug="d4", name="D", owner_user_id=1)
    await tenancy.add_member(system_db, tenant.id, 2, role="admin", added_by=1)

    assert await tenancy.remove_member(system_db, tenant.id, 1, removed_by=1) is False


async def test_anyone_can_remove_themselves(system_db: Database) -> None:
    """``/leave``. Anyone can seat anyone, so there must be a way out."""
    tenant = await tenancy.create(system_db, slug="d5", name="D", owner_user_id=1)
    await tenancy.add_member(system_db, tenant.id, 9, role="member", added_by=1)

    assert await tenancy.remove_member(system_db, tenant.id, 9, removed_by=9) is True
    assert await tenancy.member(system_db, tenant.id, 9) is None
