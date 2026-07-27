"""Concurrent claiming, now that there is no global write lock.

Under SQLite a file lock serialised every writer, so ``asyncio.gather(claim,
claim)`` proved much less than it looked like it proved. PostgreSQL runs the
claimers genuinely in parallel, which is what these tests exercise.

The claim's correctness rests on two things, and both are checked here by
observation rather than by reading the SQL:

* ``FOR UPDATE SKIP LOCKED`` — concurrent claimers take *disjoint* sets.
* Per-destination ordering — a claim scoped to one ``(chat, topic)`` still
  returns transcript order, which is what keeps a reply's chunks in sequence
  while other topics drain in parallel.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from ctb.db.connection import Database, tenant_scope
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import voice_inputs as voice_repo
from ctb.db.repo import workspaces as workspaces_repo
from tests.pg import BOOTSTRAP_TENANT_ID, app_dsn

pytestmark = pytest.mark.db

CHAT = -100_1234567890
WORKSPACE = "ws-claim"
SESSION = "sess-claim"


async def _seed(db: Database, *, destinations: int, per_destination: int) -> int:
    await workspaces_repo.upsert(db, WORKSPACE, name="claims")
    await sessions_repo.upsert(db, SESSION, workspace_id=WORKSPACE, chat_id=CHAT)
    total = 0
    for topic in range(destinations):
        for index in range(per_destination):
            await deliveries_repo.enqueue(
                db,
                session_id=SESSION,
                message_id=f"m-{topic}-{index}",
                chat_id=CHAT,
                thread_id=topic,
                session_index=index,
                payload_json=f'{{"text":"t{topic}-{index}"}}',
            )
            total += 1
    return total


class TestExactlyOnce:
    async def test_two_claimers_never_take_the_same_row(self, db: Database) -> None:
        await _seed(db, destinations=1, per_destination=40)
        first, second = await asyncio.gather(
            deliveries_repo.claim(db, claim_id="a", limit=40),
            deliveries_repo.claim(db, claim_id="b", limit=40),
        )
        keys = [row.key for row in (*first, *second)]
        assert len(keys) == len(set(keys)) == 40

    async def test_eight_pools_partition_the_queue_and_keep_order(
        self, db: Database
    ) -> None:
        """Disjointness and ordering under genuine parallelism.

        Eight independent connection pools — a closer model of a redeploy
        overlap than two handles on one file ever was — drain 400 rows across
        20 topics. Every row must be claimed exactly once, and each topic's rows
        must come back in transcript order.
        """
        expected = await _seed(db, destinations=20, per_destination=20)
        pools = [
            await Database(app_dsn(), min_size=1, max_size=2).connect()
            for _ in range(8)
        ]
        try:

            async def drain(pool: Database, worker: int) -> list[tuple[int, int, int]]:
                taken: list[tuple[int, int, int]] = []
                async with tenant_scope(BOOTSTRAP_TENANT_ID):
                    while True:
                        rows = await deliveries_repo.claim(
                            pool, claim_id=f"w{worker}", limit=5
                        )
                        if not rows:
                            return taken
                        for row in rows:
                            taken.append((row.thread_id, row.session_index, worker))
                        await asyncio.sleep(0)

            results = await asyncio.gather(
                *(drain(pool, i) for i, pool in enumerate(pools))
            )
        finally:
            for pool in pools:
                await pool.close()

        claimed = [item for batch in results for item in batch]
        assert len(claimed) == expected, "every row is claimed"
        assert len({(t, i) for t, i, _ in claimed}) == expected, "…exactly once"

        by_topic: dict[int, list[int]] = defaultdict(list)
        for topic, index, worker in claimed:
            by_topic[(topic, worker)].append(index)  # type: ignore[index]
        for sequence in by_topic.values():
            assert sequence == sorted(sequence), "order within a topic is preserved"

    async def test_a_claim_marks_when_it_was_taken(self, db: Database) -> None:
        await _seed(db, destinations=1, per_destination=1)
        [row] = await deliveries_repo.claim(db, claim_id="a", limit=1, at=5_000)
        assert row.state == "sending"
        assert row.claimed_at == 5_000


class TestScoping:
    async def test_a_claim_can_be_limited_to_one_destination(
        self, db: Database
    ) -> None:
        await _seed(db, destinations=3, per_destination=4)
        rows = await deliveries_repo.claim(
            db, claim_id="a", limit=99, chat_id=CHAT, thread_id=1
        )
        assert {row.thread_id for row in rows} == {1}
        assert len(rows) == 4

    async def test_pending_destinations_groups_the_queue(self, db: Database) -> None:
        await _seed(db, destinations=3, per_destination=4)
        destinations = await deliveries_repo.pending_destinations(db)
        assert {d.thread_id for d in destinations} == {0, 1, 2}
        assert all(d.pending == 4 for d in destinations)
        assert all(d.tenant_id == BOOTSTRAP_TENANT_ID for d in destinations)

    async def test_pending_destinations_are_oldest_first(self, db: Database) -> None:
        await workspaces_repo.upsert(db, WORKSPACE, name="claims")
        await sessions_repo.upsert(db, SESSION, workspace_id=WORKSPACE, chat_id=CHAT)
        for topic, created in ((7, 3_000), (8, 1_000), (9, 2_000)):
            await deliveries_repo.enqueue(
                db,
                session_id=SESSION,
                message_id=f"m{topic}",
                chat_id=CHAT,
                thread_id=topic,
                at=created,
            )
        order = [d.thread_id for d in await deliveries_repo.pending_destinations(db)]
        assert order == [8, 9, 7]


class TestOrphanRecovery:
    async def test_a_fresh_claim_is_not_treated_as_an_orphan(
        self, db: Database
    ) -> None:
        """A live peer's in-flight row must never be stolen and re-sent."""
        await _seed(db, destinations=1, per_destination=2)
        await deliveries_repo.claim(db, claim_id="live", limit=2, at=10_000)
        result = await deliveries_repo.recover_orphaned(
            db, claim_id="boot", at=10_500, orphan_after_ms=60_000
        )
        assert result.total == 0

    async def test_a_stale_claim_is_recovered(self, db: Database) -> None:
        await _seed(db, destinations=1, per_destination=2)
        await deliveries_repo.claim(db, claim_id="dead", limit=2, at=10_000)
        result = await deliveries_repo.recover_orphaned(
            db, claim_id="boot", at=10_000 + 61_000, orphan_after_ms=60_000
        )
        assert len(result.claimed) == 2
        assert all(row.claim_id == "boot" for row in result.claimed)

    async def test_an_already_sent_payload_is_skipped_not_repeated(
        self, db: Database
    ) -> None:
        await workspaces_repo.upsert(db, WORKSPACE, name="claims")
        await sessions_repo.upsert(db, SESSION, workspace_id=WORKSPACE, chat_id=CHAT)
        payload = '{"text":"identical"}'
        for message_id in ("sent-one", "orphan"):
            await deliveries_repo.enqueue(
                db,
                session_id=SESSION,
                message_id=message_id,
                chat_id=CHAT,
                payload_json=payload,
            )
        await deliveries_repo.claim(db, claim_id="old", limit=2, at=1_000)
        await deliveries_repo.mark_sent(
            db, (SESSION, "sent-one", 0, CHAT), tg_message_id=42, at=1_000
        )
        result = await deliveries_repo.recover_orphaned(
            db, claim_id="boot", at=100_000, orphan_after_ms=1_000
        )
        assert [row.message_id for row in result.skipped] == ["orphan"]
        assert result.claimed == ()


class TestVoiceClaim:
    async def _seed_voice(self, db: Database, count: int) -> None:
        for index in range(count):
            await voice_repo.create(
                db,
                chat_id=CHAT,
                tg_message_id=1_000 + index,
                thread_id=0,
                user_id=7,
                file_id=f"f{index}",
                file_unique_id=None,
                file_name=None,
                mime_type="audio/ogg",
                duration_seconds=3,
                file_size=1024,
                route_kind="topic",
                route_session_id=None,
                route_workspace_id=None,
                provider="elevenlabs",
                model="scribe_v2",
                action_id=f"a{index}",
                at=1_000 + index,
            )

    async def test_concurrent_workers_get_different_jobs(self, db: Database) -> None:
        await self._seed_voice(db, 4)
        claimed = await asyncio.gather(*(voice_repo.claim_next(db) for _ in range(4)))
        got = [row.tg_message_id for row in claimed if row is not None]
        assert len(got) == len(set(got)) == 4

    async def test_a_claim_advances_the_state_and_counts_the_attempt(
        self, db: Database
    ) -> None:
        await self._seed_voice(db, 1)
        row = await voice_repo.claim_next(db)
        assert row is not None
        assert row.state == "transcribing"
        assert row.attempts == 1

    async def test_an_empty_queue_returns_none(self, db: Database) -> None:
        assert await voice_repo.claim_next(db) is None
