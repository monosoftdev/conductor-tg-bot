"""The service that notices the bot has stopped working.

The property under test is the one the live outage needed and did not have:
**when nothing is polling, somebody is told, once, with the reason.** Everything
else here exists to keep that message from becoming noise.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ctb.db.connection import Database, now_ms
from ctb.db.repo import events as events_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import tenancy
from ctb.db.repo import workspaces as workspaces_repo
from ctb.silence import SilenceReason
from ctb.watchdog import Silence, Watchdog
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID, as_tenant

pytestmark = pytest.mark.db

HOUR_MS = 60 * 60_000


class RecordingSink:
    def __init__(self) -> None:
        self.alarms: list[tuple[Silence, str]] = []
        self.clears: list[uuid.UUID] = []

    async def silence_detected(self, silence: Silence, *, html: str) -> None:
        self.alarms.append((silence, html))

    async def silence_cleared(self, tenant_id: uuid.UUID, *, html: str) -> None:
        del html
        self.clears.append(tenant_id)


async def seed(
    db: Database,
    session_id: str,
    *,
    tenant_id: uuid.UUID = BOOTSTRAP_TENANT_ID,
    thread_id: int = 7,
    at: int,
) -> None:
    async with as_tenant(tenant_id):
        await workspaces_repo.upsert(db, f"ws-{session_id}", name=session_id)
        await sessions_repo.upsert(
            db,
            session_id,
            workspace_id=f"ws-{session_id}",
            chat_id=-100_500,
            thread_id=thread_id,
            is_bound=True,
            at=at,
        )
        await tenancy.set_conductor_key(
            db, tenant_id, ciphertext=b"sealed", kid="v1", fingerprint="fp"
        )


def make(db: Database, sink: RecordingSink, *, at: int) -> Watchdog:
    return Watchdog(db, sink=sink, interval_s=0.0, clock=lambda: at)


async def test_a_silent_workspace_reaches_its_owner_with_the_reason(
    system_db: Database,
) -> None:
    now = now_ms()
    await seed(system_db, "abandoned", at=now - HOUR_MS)
    await tenancy.mark_auth_failed(system_db, BOOTSTRAP_TENANT_ID, reason="401")
    sink = RecordingSink()

    episodes = await make(system_db, sink, at=now).check_once()

    assert [episode.slug for episode in episodes] == ["test"]
    assert episodes[0].reason is SilenceReason.AUTH_REJECTED
    assert episodes[0].sessions == ("abandoned",)
    assert len(sink.alarms) == 1
    # The message must name the fix, not just the symptom.
    assert "/key" in sink.alarms[0][1]
    assert "60 minutes" in sink.alarms[0][1]


async def test_one_message_per_episode_however_often_it_checks(
    system_db: Database,
) -> None:
    """The dedup identity is the episode, and it is stable while silence lasts.

    The key is the oldest silent session's ``updated_at`` — which by definition
    is not moving — so repeated ticks produce the same key and collide on
    ``deliveries``' primary key. Here that shows as one stable key rather than
    a growing set; the collision itself is PostgreSQL's job.
    """
    now = now_ms()
    await seed(system_db, "abandoned", at=now - HOUR_MS)
    sink = RecordingSink()
    watchdog = make(system_db, sink, at=now)

    for _ in range(4):
        await watchdog.check_once()

    assert len({silence.key for silence, _ in sink.alarms}) == 1


async def test_a_new_episode_earns_a_new_message(system_db: Database) -> None:
    """A key that never changed would silence every outage after the first."""
    now = now_ms()
    await seed(system_db, "abandoned", at=now - HOUR_MS)
    sink = RecordingSink()
    first = (await make(system_db, sink, at=now).check_once())[0]

    # It polled once — the silence ended — and then stopped again.
    await sessions_repo.update(
        system_db, "abandoned", at=now - 20 * 60_000, turn_state="IDLE"
    )
    second = (await make(system_db, sink, at=now).check_once())[0]

    assert first.key != second.key


async def test_recovery_is_reported_once_and_only_after_an_alarm(
    system_db: Database,
) -> None:
    now = now_ms()
    await seed(system_db, "abandoned", at=now - HOUR_MS)
    sink = RecordingSink()
    watchdog = make(system_db, sink, at=now)
    await watchdog.check_once()
    assert sink.clears == []

    # A poller touched it: no longer silent at this instant.
    await sessions_repo.update(system_db, "abandoned", at=now, turn_state="IDLE")
    await watchdog.check_once()
    await watchdog.check_once()

    assert sink.clears == [BOOTSTRAP_TENANT_ID]


async def test_silence_is_attributed_to_conductor_when_calls_are_failing(
    system_db: Database,
) -> None:
    """Attempts that all fail are the upstream's fault, and a restart cannot help."""
    now = now_ms()
    await seed(system_db, "abandoned", at=now - HOUR_MS)
    for index in range(3):
        await events_repo.record_api_event(
            system_db,
            method="GET",
            endpoint="/sessions/{id}/messages",
            status_code=500,
            ok=False,
            error="upstream is down",
            tenant_id=BOOTSTRAP_TENANT_ID,
            at=now - (index + 1) * 60_000,
        )
    sink = RecordingSink()

    episodes = await make(system_db, sink, at=now).check_once()

    assert episodes[0].reason is SilenceReason.CONDUCTOR_UNREACHABLE
    assert "Conductor rather than the bot" in sink.alarms[0][1]


async def test_silence_with_nothing_to_blame_is_the_bots_own_fault(
    system_db: Database,
) -> None:
    """No rejection and no attempts at all — the shape of the four-day outage."""
    now = now_ms()
    await seed(system_db, "abandoned", at=now - HOUR_MS)
    sink = RecordingSink()

    episodes = await make(system_db, sink, at=now).check_once()

    assert episodes[0].reason is SilenceReason.UNEXPLAINED
    assert not episodes[0].reason.is_explained


async def test_one_tenants_outage_is_not_reported_to_another(
    system_db: Database,
) -> None:
    """Isolation holds here too: an episode belongs to exactly one team."""
    now = now_ms()
    await seed(system_db, "acme-quiet", at=now - HOUR_MS)
    await seed(system_db, "rival-busy", tenant_id=OTHER_TENANT_ID, thread_id=8, at=now)
    sink = RecordingSink()

    episodes = await make(system_db, sink, at=now).check_once()

    assert [episode.tenant_id for episode in episodes] == [BOOTSTRAP_TENANT_ID]
    assert episodes[0].sessions == ("acme-quiet",)


async def test_a_read_failure_does_not_kill_the_watchdog(
    system_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watchdog that dies on a transient error is the failure it exists to catch."""
    now = now_ms()
    await seed(system_db, "abandoned", at=now - HOUR_MS)
    sink = RecordingSink()
    watchdog = make(system_db, sink, at=now)

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr(sessions_repo, "list_silent", boom)
    await watchdog.stop()
    await watchdog.run()  # returns rather than raising

    monkeypatch.undo()
    assert (await watchdog.check_once())[0].sessions == ("abandoned",)


async def test_a_failing_sink_leaves_the_episode_to_be_retried(
    system_db: Database,
) -> None:
    """The enqueue is the durable part; a failed send must not consume the alarm."""
    now = now_ms()
    await seed(system_db, "abandoned", at=now - HOUR_MS)

    class BrokenSink(RecordingSink):
        async def silence_detected(self, silence: Silence, *, html: str) -> None:
            await super().silence_detected(silence, html=html)
            raise RuntimeError("Telegram is down")

    sink = BrokenSink()
    watchdog = make(system_db, sink, at=now)
    await watchdog.check_once()
    await watchdog.check_once()

    # Tried both times, same key both times — nothing was swallowed.
    assert len(sink.alarms) == 2
    assert len({silence.key for silence, _ in sink.alarms}) == 1
