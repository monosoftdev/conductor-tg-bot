"""The cursor layer, against the real client and the scripted fake API.

These tests drive ``ctb.turn.cursor`` through ``ConductorClient`` over
``httpx.MockTransport``, so the URL building, the ``after``/``offset`` mutual
exclusion, the 404 handling and the retry policy are the production ones.
Nothing here touches the network and nothing sleeps.

The properties being pinned, in the order PLAN §Verification lists them:

* **seek to end** binds a session without replaying a byte of history, in
  ``~2·log₂N`` requests;
* **the replay attack** — ``after=`` ignored and the whole transcript returned —
  is dropped entirely by the ``sessionIndex`` filter;
* **the id disappeared** — a 404 on ``after`` is repaired by offset paging, not
  mistaken for a dead session;
* **``hasMore`` paging** drains within a tick, bounded, and resumes next tick.

Every scenario test asserts delivery correctness with no state machine present
at all, which is the design claim: *the transcript cursor is the source of truth
for content; ``GET /status`` never gates delivery.*
"""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from ctb.conductor.client import ConductorClient
from ctb.conductor.errors import ApiError, AuthFatal, NotFound, PairingError
from ctb.db.connection import Database
from ctb.db.repo import chats, deliveries, prompts, sessions, transcript, workspaces
from ctb.delivery.render.types import Verbosity
from ctb.settings import Settings
from ctb.turn import cursor
from ctb.turn.state import PostOk
from tests.fakes.fake_conductor import (
    SCENARIOS,
    Advance,
    FakeConductor,
    FakeSession,
    PostFailure,
    Tick,
    assistant,
    error_mid_turn,
    error_result,
    fast_turn,
    queued_idle_trap,
    replay_attack,
    result,
    state_changed,
    system_init,
    tool_use,
    user_message,
)

CHAT_ID = -1002000000000
THREAD_ID = 77

type ClientFactory = Callable[[FakeConductor], ConductorClient]


async def _no_sleep(_seconds: float) -> None:
    """The client's backoff, minus the waiting."""
    return None


@pytest.fixture
def fake() -> FakeConductor:
    return FakeConductor()


@pytest.fixture
async def clients(settings: Settings) -> AsyncIterator[ClientFactory]:
    """Build real clients pointed at any fake, and close them afterwards."""
    made: list[ConductorClient] = []

    def make(target: FakeConductor) -> ConductorClient:
        instance = ConductorClient(
            settings,
            transport=target.transport(),
            sleep=_no_sleep,
            rng=random.Random(20260726),
            max_attempts=2,
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
    db: Database,
    session: FakeSession,
    *,
    chat_id: int = CHAT_ID,
    thread_id: int = THREAD_ID,
    verbosity: str = "normal",
    bound: bool = True,
) -> None:
    """Create the workspace/session/chat rows a drain needs."""
    await workspaces.upsert(db, session.workspace_id, name=session.workspace.name)
    await sessions.upsert(
        db,
        session.session_id,
        workspace_id=session.workspace_id,
        chat_id=chat_id if bound else None,
        thread_id=thread_id,
        is_bound=bound,
    )
    await chats.ensure(db, chat_id, thread_id)
    await chats.set_verbosity(db, chat_id, thread_id, verbosity=verbosity)


def message_calls(fake: FakeConductor) -> int:
    return len(fake.calls_to("/messages", method="GET"))


async def delivered_html(db: Database, session_id: str) -> str:
    """Every queued delivery's HTML, concatenated.

    Only the ``html`` field: a payload also carries the ``parse_mode=None``
    plain-text twin, and counting both would double every match.
    """
    rows = await deliveries.list_for_session(db, session_id, limit=1000)
    parts: list[str] = []
    for row in rows:
        payload = json.loads(row.payload_json or "{}")
        parts.append(str(payload.get("html", "")))
        parts.append(str(payload.get("content", "")))
    return "\n".join(parts)


def assistant_texts(session: FakeSession) -> list[str]:
    """Every prose block the agent produced, in transcript order."""
    out: list[str] = []
    for message in session.messages_model():
        for block in message.blocks:
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    out.append(text)
    return out


async def assert_each_answer_delivered_once(db: Database, session: FakeSession) -> None:
    html = await delivered_html(db, session.session_id)
    for text in assistant_texts(session):
        assert html.count(text) == 1, f"{text!r} delivered {html.count(text)}x"
    rows = await deliveries.list_for_session(db, session.session_id, limit=1000)
    assert len({row.key for row in rows}) == len(rows)


# ── seek to end ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("count", [0, 1, 2, 3, 7, 40])
async def test_seek_to_end_never_replays(
    db: Database, fake: FakeConductor, client: ConductorClient, count: int
) -> None:
    session = fake.add_session(seed=[assistant(f"old {i}") for i in range(count)])
    await bind(db, session)

    seek = await cursor.seek_to_end(client, db, session.session_id)

    assert seek.skipped is False
    assert seek.message_count == count
    if count == 0:
        assert seek.cursor_message_id is None
        assert seek.cursor_session_index == -1
    else:
        last = session.messages_model()[-1]
        assert seek.cursor_message_id == last.id
        assert seek.cursor_session_index == last.session_index

    # The whole point: not one historical message was recorded or queued.
    assert await transcript.count(db, session.session_id) == 0
    assert await deliveries.pending_count(db, session.session_id) == 0

    # And a following drain sees nothing: the cursor is already at the end.
    drained = await cursor.drain(client, db, session.session_id)
    assert drained.n == 0
    assert drained.delta is None


async def test_seek_to_end_is_logarithmic(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """~2·log₂N probes, not N. A linear scan here would page a whole transcript."""
    count = 500
    session = fake.add_session(seed=[assistant(f"old {i}") for i in range(count)])
    await bind(db, session)

    seek = await cursor.seek_to_end(client, db, session.session_id)

    budget = 2 * count.bit_length() + 6
    assert seek.requests <= budget
    assert message_calls(fake) <= budget
    assert seek.cursor_session_index == session.messages_model()[-1].session_index


async def test_seek_to_end_previews_the_last_agent_message(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(
        seed=(
            user_message("an older prompt"),
            system_init(),
            assistant("the newest answer"),
            state_changed("idle"),
        )
    )
    await bind(db, session)

    seek = await cursor.seek_to_end(client, db, session.session_id)

    assert seek.preview is not None
    assert seek.preview.is_assistant_text
    assert seek.preview_text == "the newest answer"


async def test_seek_to_end_refuses_a_seeded_session(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """Re-seeking a live session would jump the cursor over undelivered work."""
    session = fake.add_session(seed=[assistant("first")])
    await bind(db, session)
    await cursor.drain(client, db, session.session_id)
    fake.reset_calls()

    seek = await cursor.seek_to_end(client, db, session.session_id)

    assert seek.skipped is True
    assert seek.requests == 0
    assert message_calls(fake) == 0
    row = await sessions.get(db, session.session_id)
    assert row is not None
    assert row.cursor_session_index == session.messages_model()[-1].session_index


async def test_seek_to_end_forced_moves_a_seeded_cursor(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(
        seed=[assistant("first")],
        script=[Tick(emit=(assistant("second"),))],
        advance=Advance.MANUAL,
    )
    await bind(db, session)
    await cursor.drain(client, db, session.session_id)

    session.tick()  # a second message lands while we are not looking
    seek = await cursor.seek_to_end(client, db, session.session_id, force=True)

    assert seek.skipped is False
    assert seek.cursor_session_index == session.messages_model()[-1].session_index


async def test_seek_to_end_requires_a_session_row(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session()
    with pytest.raises(ValueError, match="no sessions row"):
        await cursor.seek_to_end(client, db, session.session_id)


async def test_find_last_message_survives_a_lying_has_more(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """If ``hasMore`` never goes false, the empty page is the fallback signal."""
    session = fake.add_session(seed=[assistant(f"m{i}") for i in range(9)])
    original = session._messages_json

    def always_more(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        status, payload = original(**kwargs)
        if status == 200 and payload["data"]:
            payload = {**payload, "hasMore": True}
        return status, payload

    session._messages_json = always_more  # type: ignore[method-assign]

    message, offset, _requests = await cursor.find_last_message(
        client, session.session_id
    )

    assert message is not None
    assert message.id == session.messages_model()[-1].id
    assert offset == 8


# ── the drain ────────────────────────────────────────────────────────────────


async def test_drain_records_queues_and_advances(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(
        script=[Tick(emit=(state_changed(), assistant("the answer"), result("done")))],
        advance=Advance.MANUAL,
    )
    await bind(db, session)
    session.tick()

    drained = await cursor.drain(client, db, session.session_id)

    assert drained.n == 3
    assert drained.recorded == 3
    assert drained.delta is not None
    assert drained.delta.n == 3
    assert drained.delta.has_agent_content is True
    assert drained.exhausted is True
    assert await transcript.count(db, session.session_id) == 3

    rows = await deliveries.list_for_session(db, session.session_id)
    assert rows, "the assistant answer must be queued for delivery"
    assert {row.chat_id for row in rows} == {CHAT_ID}
    assert {row.thread_id for row in rows} == {THREAD_ID}
    assert all(row.state == "pending" for row in rows)
    assert all(row.content_hash for row in rows)
    assert "the answer" in await delivered_html(db, session.session_id)

    row = await sessions.get(db, session.session_id)
    assert row is not None
    assert row.cursor_session_index == drained.cursor_session_index
    assert row.cursor_message_id == drained.cursor_message_id
    assert row.seeded is True


async def test_drain_is_a_no_op_when_nothing_is_new(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(seed=[assistant("one")])
    await bind(db, session)

    first = await cursor.drain(client, db, session.session_id)
    second = await cursor.drain(client, db, session.session_id)

    assert first.n == 1
    assert second.n == 0
    assert second.delta is None
    assert second.dropped == 0
    assert await transcript.count(db, session.session_id) == 1


async def test_drain_without_a_binding_records_but_delivers_nothing(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(seed=[assistant("out of band")])
    await bind(db, session, bound=False)

    drained = await cursor.drain(client, db, session.session_id)

    assert drained.n == 1
    assert drained.deliveries_created == 0
    assert await transcript.count(db, session.session_id) == 1
    assert await deliveries.pending_count(db, session.session_id) == 0


async def test_drain_with_deliver_false_only_records(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """Recording without mirroring: catch a session up before binding a topic."""
    session = fake.add_session(seed=[assistant("silent")])
    await bind(db, session)

    drained = await cursor.drain(client, db, session.session_id, deliver=False)

    assert drained.n == 1
    assert drained.deliveries_created == 0
    assert await deliveries.pending_count(db, session.session_id) == 0


async def test_drain_honours_chat_verbosity(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """QUIET hides tool calls, VERBOSE shows them — same transcript either way."""
    quiet = fake.add_session(seed=(tool_use("Bash", tool_input={"command": "pytest"}),))
    loud = fake.add_session(seed=(tool_use("Bash", tool_input={"command": "pytest"}),))
    await bind(db, quiet, thread_id=1, verbosity="quiet")
    await bind(db, loud, thread_id=2, verbosity="verbose")

    quiet_result = await cursor.drain(client, db, quiet.session_id)
    loud_result = await cursor.drain(client, db, loud.session_id)

    assert await deliveries.pending_count(db, quiet.session_id) == 0
    assert await deliveries.pending_count(db, loud.session_id) >= 1
    assert "Bash" in (quiet_result.latest_activity or "")
    assert "Bash" in (loud_result.latest_activity or "")


async def test_drain_reports_the_turn_cost_separately_from_activity(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """The money is the one fact the state machine cannot derive for itself.

    It rides its own field so the card can hold it back until the turn is
    actually finished — as an activity line it landed next to ``working 20s``.
    """
    session = fake.add_session(
        seed=(tool_use("Bash", tool_input={"command": "pytest"}), result("done"))
    )
    await bind(db, session)

    drained = await cursor.drain(client, db, session.session_id)

    assert drained.latest_cost_usd == pytest.approx(0.0123)
    assert drained.latest_activity == "Bash · pytest"
    assert "done" not in (drained.latest_activity or "")


async def test_drain_requires_a_session_row(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session()
    with pytest.raises(ValueError, match="no sessions row"):
        await cursor.drain(client, db, session.session_id)


async def test_destination_for_defaults_to_normal_without_a_chat_row(
    db: Database, fake: FakeConductor
) -> None:
    session = fake.add_session()
    await workspaces.upsert(db, session.workspace_id)
    await sessions.upsert(
        db,
        session.session_id,
        workspace_id=session.workspace_id,
        chat_id=CHAT_ID,
        thread_id=0,
        is_bound=True,
    )

    destination = await cursor.destination_for(db, session.session_id)

    assert destination == cursor.Destination(chat_id=CHAT_ID, thread_id=0)
    assert destination is not None
    assert destination.verbosity is Verbosity.NORMAL


async def test_destination_for_an_unbound_session_is_none(
    db: Database, fake: FakeConductor
) -> None:
    session = fake.add_session()
    await bind(db, session, bound=False)
    assert await cursor.destination_for(db, session.session_id) is None


async def test_quiet_destination_is_silent_until_topic_is_focused(
    db: Database, fake: FakeConductor
) -> None:
    session = fake.add_session()
    await bind(db, session)
    await chats.set_notify(db, CHAT_ID, THREAD_ID, notify="quiet")

    quiet = await cursor.destination_for(db, session.session_id)
    assert quiet is not None
    assert quiet.silent is True

    await chats.touch_prompt(db, CHAT_ID, THREAD_ID, focus_for_ms=60_000)
    focused = await cursor.destination_for(db, session.session_id)
    assert focused is not None
    assert focused.silent is False


async def test_quiet_delivery_sets_telegram_silent_flag(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(seed=[assistant("done")])
    await bind(db, session)
    await chats.set_notify(db, CHAT_ID, THREAD_ID, notify="quiet")

    await cursor.drain(client, db, session.session_id)

    rows = await deliveries.list_for_session(db, session.session_id)
    assert rows
    assert all(json.loads(row.payload_json or "{}")["silent"] for row in rows)


# ── the replay attack ────────────────────────────────────────────────────────


async def test_replay_attack_is_dropped_by_the_session_index_filter(
    db: Database, clients: ClientFactory
) -> None:
    """``after=`` ignored, whole transcript returned: every old message dropped."""
    scenario = replay_attack(advance=Advance.MANUAL)
    client = clients(scenario.fake)
    session = scenario.session
    await bind(db, session)

    seek = await cursor.seek_to_end(client, db, session.session_id)
    assert seek.cursor_session_index >= 0
    seeded = len(session.transcript)

    session.tick()  # the working tick emits two genuinely new messages
    drained = await cursor.drain(client, db, session.session_id)

    assert drained.dropped == seeded, "every replayed message must be filtered"
    assert drained.n == len(session.transcript) - seeded
    assert await transcript.count(db, session.session_id) == drained.n
    html = await delivered_html(db, session.session_id)
    assert "an older answer" not in html
    assert "new answer" in html


async def test_repeated_replay_never_duplicates_a_delivery(
    db: Database, clients: ClientFactory
) -> None:
    scenario = replay_attack(advance=Advance.MANUAL)
    client = clients(scenario.fake)
    session = scenario.session
    await bind(db, session)
    await cursor.seek_to_end(client, db, session.session_id)
    session.tick()

    first = await cursor.drain(client, db, session.session_id)
    before = await deliveries.list_for_session(db, session.session_id, limit=1000)
    second = await cursor.drain(client, db, session.session_id)
    after = await deliveries.list_for_session(db, session.session_id, limit=1000)

    assert first.n > 0
    assert second.n == 0
    assert second.dropped == len(session.transcript)
    assert [row.key for row in before] == [row.key for row in after]


async def test_a_server_that_ignores_after_cannot_spin_the_pager(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """No forward progress ends the tick instead of burning the page budget."""
    session = fake.add_session(
        seed=[assistant(f"m{i}") for i in range(30)], replay_after=True
    )
    await bind(db, session)
    await cursor.seek_to_end(client, db, session.session_id)
    fake.reset_calls()

    drained = await cursor.drain(client, db, session.session_id, page_limit=5)

    assert drained.n == 0
    assert drained.pages == 1
    assert message_calls(fake) == 1


# ── the id that disappeared ──────────────────────────────────────────────────


async def test_unknown_after_id_is_repaired_by_offset_paging(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(
        seed=[assistant(f"old {i}") for i in range(6)],
        script=[Tick(emit=(assistant("brand new"),))],
        advance=Advance.MANUAL,
    )
    await bind(db, session)
    await cursor.seek_to_end(client, db, session.session_id)
    stored = await sessions.get(db, session.session_id)
    assert stored is not None
    kept_index = stored.cursor_session_index

    # The id vanishes (compaction, a session rebuild, a bad write). The *index*
    # is the real cursor, which is what makes the repair possible at all.
    await sessions.update(db, session.session_id, cursor_message_id="gone:9999:0")
    session.tick()

    drained = await cursor.drain(client, db, session.session_id)

    assert drained.repaired is True
    assert drained.n == 1
    assert drained.messages[0].session_index > kept_index
    assert "brand new" in await delivered_html(db, session.session_id)

    repaired = await sessions.get(db, session.session_id)
    assert repaired is not None
    assert repaired.cursor_message_id == session.messages_model()[-1].id

    # And the next tick is back on the cheap `after=` path.
    fake.reset_calls()
    again = await cursor.drain(client, db, session.session_id)
    assert again.repaired is False
    assert all(
        call.params.get("after") for call in fake.calls_to("/messages", method="GET")
    )


async def test_repair_finds_nothing_when_the_cursor_is_already_at_the_end(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(seed=[assistant(f"old {i}") for i in range(5)])
    await bind(db, session)
    await cursor.seek_to_end(client, db, session.session_id)
    await sessions.update(db, session.session_id, cursor_message_id="gone:1:0")

    drained = await cursor.drain(client, db, session.session_id)

    assert drained.repaired is True
    assert drained.n == 0
    assert drained.delta is None


async def test_a_missing_session_still_raises_not_found(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """A 404 without a cursor is the session itself: the poller must see E404."""
    session = fake.add_session(seed=[assistant("hi")])
    await bind(db, session)
    del fake.sessions[session.session_id]

    with pytest.raises(NotFound):
        await cursor.drain(client, db, session.session_id)


async def test_a_missing_session_raises_even_after_a_repair_attempt(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(seed=[assistant("hi")])
    await bind(db, session)
    await cursor.drain(client, db, session.session_id)
    del fake.sessions[session.session_id]

    with pytest.raises(NotFound):
        await cursor.drain(client, db, session.session_id)


# ── hasMore paging ───────────────────────────────────────────────────────────


async def test_paging_drains_within_one_tick(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(seed=[assistant(f"m{i}") for i in range(25)])
    await bind(db, session)

    drained = await cursor.drain(client, db, session.session_id, page_limit=10)

    assert drained.n == 25
    assert drained.pages == 3
    assert drained.exhausted is True
    assert drained.truncated is False
    assert await transcript.count(db, session.session_id) == 25


async def test_paging_is_bounded_and_resumes_next_tick(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """A tick must end. The rest is not lost — the cursor simply resumes."""
    session = fake.add_session(seed=[assistant(f"m{i}") for i in range(120)])
    await bind(db, session)

    first = await cursor.drain(
        client, db, session.session_id, page_limit=10, max_pages=5
    )
    assert first.n == 50
    assert first.pages == 5
    assert first.truncated is True
    assert first.exhausted is False

    second = await cursor.drain(client, db, session.session_id, page_limit=10)
    assert second.n == 70
    assert second.exhausted is True
    assert await transcript.count(db, session.session_id) == 120


async def test_session_index_gaps_are_not_treated_as_loss(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """Live indices went 0, 2, 3, 4, … — a gap never means a missed message."""
    session = fake.add_session(seed=[assistant(f"m{i}") for i in range(12)])
    await bind(db, session)

    drained = await cursor.drain(client, db, session.session_id)

    indices = [m.session_index for m in drained.messages]
    assert indices != list(range(len(indices))), "the fake must produce gaps"
    assert drained.n == 12
    assert await transcript.count(db, session.session_id) == 12


# ── evidence ─────────────────────────────────────────────────────────────────


async def test_build_delta_reports_what_the_machine_needs(
    fake: FakeConductor,
) -> None:
    session = fake.add_session(
        seed=(
            user_message("go"),
            tool_use("Bash", tool_input={"command": "pytest"}),
            assistant("done"),
            error_result("it broke"),
        )
    )
    messages = session.messages_model()

    delta = cursor.build_delta(messages)

    assert delta is not None
    assert delta.n == 4
    assert delta.max_index == max(m.session_index for m in messages)
    assert delta.has_agent_content is True
    assert delta.tool_calls == 1
    assert delta.has_error_result is True
    assert delta.witnessed_prompt_ids == frozenset({session.prompt_ids[0]})
    assert delta.turn_ids == frozenset({session.prompt_ids[0]})


def test_build_delta_of_an_empty_page_is_none() -> None:
    assert cursor.build_delta([]) is None


# ── rendering never stalls the cursor ────────────────────────────────────────


async def test_a_raising_chunker_degrades_instead_of_wedging_the_cursor(
    db: Database,
    fake: FakeConductor,
    client: ConductorClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(cursor, "chunk_blocks", explode)
    session = fake.add_session(seed=(assistant("still worth reading"),))
    await bind(db, session)

    drained = await cursor.drain(client, db, session.session_id)

    assert drained.n == 1, "the cursor must still advance"
    assert "still worth reading" in await delivered_html(db, session.session_id)


async def test_a_totally_broken_renderer_still_advances_the_cursor(
    db: Database,
    fake: FakeConductor,
    client: ConductorClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unrendered message beats a session wedged forever on the same page."""

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(cursor, "chunk_blocks", explode)
    monkeypatch.setattr(cursor, "chunk_html", explode)
    session = fake.add_session(seed=(assistant("lost, but not stuck"),))
    await bind(db, session)

    drained = await cursor.drain(client, db, session.session_id)

    assert drained.n == 1
    assert drained.deliveries_created == 0
    row = await sessions.get(db, session.session_id)
    assert row is not None
    assert row.cursor_session_index == drained.cursor_session_index


# ── the Choices block is buttons, not prose ──────────────────────────────────


def _choices_message(fake: FakeConductor, text: str) -> Any:
    session = fake.add_session(seed=(assistant(text),))
    return session.messages_model()[-1]


def _one_payload(message: Any) -> dict[str, Any]:
    drafts = cursor.plan_deliveries(message, cursor.Destination(chat_id=CHAT_ID))
    assert len(drafts) == 1
    payload: dict[str, Any] = json.loads(drafts[0].payload_json or "{}")
    return payload


DECISION = (
    "Fixed the flaky fixture; suite is green.\n\n"
    "Function scope was the bug. What next?\n\n"
    "Choices:\n"
    "1. Ship the fix now\n"
    "2. Refactor the helpers\n"
    "3. Do nothing"
)


def test_the_choices_block_is_not_also_printed_as_text(fake: FakeConductor) -> None:
    """It was rendered twice — as prose and as the buttons under it."""
    payload = _one_payload(_choices_message(fake, DECISION))

    assert payload["quick_replies"] == [
        "Ship the fix now",
        "Refactor the helpers",
        "Do nothing",
    ]
    assert "Choices:" not in payload["html"]
    assert "Ship the fix now" not in payload["html"]
    assert payload["html"].endswith("What next?")
    # The line of context above the marker is content, and stays.
    assert payload["html"].startswith("Fixed the flaky fixture")


def test_options_too_long_for_a_button_keep_their_text(fake: FakeConductor) -> None:
    """A truncated option nobody can read is worse than a duplicate."""
    long_option = "Rewrite the whole async helper module and " + "x" * 40
    text = f"Context.\n\nChoices:\n1. {long_option}\n2. Do nothing"
    payload = _one_payload(_choices_message(fake, text))

    assert payload["quick_replies"] == [long_option, "Do nothing"]
    assert "Choices:" in payload["html"]
    assert long_option in payload["html"]


def test_a_message_that_is_only_a_choices_block_keeps_its_text(
    fake: FakeConductor,
) -> None:
    """Cutting it would leave nothing to hang the buttons on. Never do that."""
    payload = _one_payload(_choices_message(fake, "Choices:\n1. Yes\n2. No"))

    assert payload["quick_replies"] == ["Yes", "No"]
    assert "1. Yes" in payload["html"]


def test_numbered_steps_without_the_marker_are_untouched(fake: FakeConductor) -> None:
    text = "Ran the steps:\n1. installed deps\n2. ran the suite\n3. it passed"
    payload = _one_payload(_choices_message(fake, text))

    assert "quick_replies" not in payload
    assert "installed deps" in payload["html"]


def test_preview_text_collapses_and_truncates(fake: FakeConductor) -> None:
    session = fake.add_session(seed=(assistant("a" * 400),))
    message = session.messages_model()[-1]

    text = cursor.preview_text(message, limit=40)

    assert len(text) <= 40
    assert text.endswith("…")


# ── prompts ──────────────────────────────────────────────────────────────────


async def test_send_prompt_writes_the_ledger_row_before_the_http_call(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session()
    await bind(db, session)
    seen: list[Any] = []
    original = session._post_json

    def spy(body: Any) -> Any:
        seen.append(body.get("messageId"))
        return original(body)

    session._post_json = spy  # type: ignore[method-assign]

    sent = await cursor.send_prompt(
        client, db, session_id=session.session_id, text="do the thing"
    )

    assert sent.posted is True
    assert seen == [sent.message_id], "our id, chosen before the request"
    row = await prompts.get(db, sent.message_id)
    assert row is not None
    assert row.state == "posted"
    assert row.body == "do the thing"
    assert session.echo_count(sent.message_id) == 1


async def test_send_prompt_records_the_cursor_index_at_post(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session(seed=[assistant("history")])
    await bind(db, session)
    await cursor.drain(client, db, session.session_id)
    row = await sessions.get(db, session.session_id)
    assert row is not None

    sent = await cursor.send_prompt(
        client, db, session_id=session.session_id, text="next"
    )

    assert sent.prompt.index_at_post == row.cursor_session_index
    assert isinstance(sent.evidence, PostOk)
    assert sent.evidence.message_id == sent.message_id


async def test_a_posted_prompt_is_witnessed_by_its_echo(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session()
    await bind(db, session)
    sent = await cursor.send_prompt(
        client, db, session_id=session.session_id, text="go"
    )

    drained = await cursor.drain(client, db, session.session_id)

    assert drained.witnessed == (sent.message_id,)
    assert drained.delta is not None
    assert sent.message_id in drained.delta.witnessed_prompt_ids
    row = await prompts.get(db, sent.message_id)
    assert row is not None
    assert row.state == "witnessed"
    assert row.turn_id == sent.message_id


async def test_a_fatal_api_error_marks_the_prompt_failed(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session()
    await bind(db, session)
    session.fail_next_post(PostFailure(status=400))

    with pytest.raises(ApiError):
        await cursor.send_prompt(
            client, db, session_id=session.session_id, text="nope", max_attempts=1
        )

    rows = await prompts.list_for_session(db, session.session_id)
    assert len(rows) == 1
    assert rows[0].state == "failed"


async def test_repost_of_a_settled_prompt_is_a_no_op(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    session = fake.add_session()
    await bind(db, session)
    sent = await cursor.send_prompt(
        client, db, session_id=session.session_id, text="go"
    )
    await cursor.drain(client, db, session.session_id)
    fake.reset_calls()

    again = await cursor.repost_prompt(client, db, sent.message_id)

    assert again.posted is True
    assert again.reason == "already settled"
    assert fake.calls_to("/messages", method="POST") == []


async def test_repost_of_an_unknown_prompt_is_a_programming_error(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    with pytest.raises(ValueError, match="no outbound_prompts row"):
        await cursor.repost_prompt(client, db, "never-created")


# ── workspace / session creation ─────────────────────────────────────────────


def test_workspace_names_carry_a_parseable_nonce() -> None:
    nonce = cursor.new_nonce()
    name = cursor.workspace_name(CHAT_ID, nonce)

    assert len(nonce) == cursor.NONCE_LENGTH
    assert cursor.nonce_of(name) == nonce
    assert cursor.nonce_of("api/fix-flaky") is None
    assert cursor.nonce_of(None) is None
    assert len({cursor.new_nonce() for _ in range(200)}) > 190


def test_stable_workspace_nonce_is_repeatable_and_parseable() -> None:
    first = cursor.stable_nonce("-1001:42")

    assert first == cursor.stable_nonce("-1001:42")
    assert first != cursor.stable_nonce("-1001:43")
    assert cursor.nonce_of(cursor.workspace_name(CHAT_ID, first)) == first


async def test_create_workspace_persists_the_nonce(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    project_id = fake.add_project("example")

    created = await cursor.create_workspace(
        client, db, chat_id=CHAT_ID, agent="claude", project_id=project_id
    )

    assert created.ok
    assert created.created is True
    assert created.reconciled is False
    assert fake.created_workspace_names == [created.name]
    row = await workspaces.get_by_nonce(db, created.nonce)
    assert row is not None
    assert row.id == created.workspace_id
    assert row.name == created.name


async def test_replayed_workspace_create_intent_does_not_post_twice(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    project_id = fake.add_project("example")
    nonce = cursor.stable_nonce(f"{CHAT_ID}:42")

    first = await cursor.create_workspace(
        client,
        db,
        chat_id=CHAT_ID,
        agent="claude",
        project_id=project_id,
        nonce=nonce,
    )
    second = await cursor.create_workspace(
        client,
        db,
        chat_id=CHAT_ID,
        agent="claude",
        project_id=project_id,
        nonce=nonce,
    )

    assert first.workspace_id == second.workspace_id
    assert second.reconciled
    assert len(fake.calls_to("/workspaces", method="POST")) == 1


async def test_an_ambiguous_create_reconciles_instead_of_retrying(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """The write landed and the response was lost.

    ``POST /v0/workspaces`` has no idempotency key, so a blind retry would
    strand a second cloud workspace. The nonce in the name settles it.
    """
    project_id = fake.add_project("example")
    fake.fail_next_workspace_create(PostFailure(status=500, landed=True))

    created = await cursor.create_workspace(
        client, db, chat_id=CHAT_ID, agent="claude", project_id=project_id
    )

    assert created.ok
    assert created.reconciled is True
    assert created.created is False
    assert len(fake.created_workspace_names) == 1, "exactly one workspace exists"
    assert len(fake.calls_to("/workspaces", method="POST")) == 1
    row = await workspaces.get_by_nonce(db, created.nonce)
    assert row is not None
    assert row.id == created.workspace_id


async def test_an_ambiguous_create_that_did_not_land_reports_honestly(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    project_id = fake.add_project("example")
    fake.fail_next_workspace_create(PostFailure(status=502, landed=False))

    created = await cursor.create_workspace(
        client, db, chat_id=CHAT_ID, agent="claude", project_id=project_id
    )

    assert created.ok is False
    assert created.unresolved is True
    assert created.workspace_id is None
    assert fake.created_workspace_names == []


async def test_a_rejected_create_is_not_reconciled(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """A rejection is not ambiguous: nothing was created, so nothing is sought."""
    project_id = fake.add_project("example")
    fake.fail_next_workspace_create(PostFailure(status=403))

    with pytest.raises(AuthFatal):
        await cursor.create_workspace(
            client, db, chat_id=CHAT_ID, agent="claude", project_id=project_id
        )

    assert fake.created_workspace_names == []
    assert fake.calls_to("/projects", method="GET") == []


async def test_a_pairing_error_never_reaches_the_api(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    project_id = fake.add_project("example")

    with pytest.raises(PairingError):
        await cursor.create_workspace(
            client,
            db,
            chat_id=CHAT_ID,
            agent="claude",
            model="gpt-5.5",
            project_id=project_id,
        )

    assert fake.calls == []


async def test_reconcile_prefers_the_local_cache(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    project_id = fake.add_project("example")
    created = await cursor.create_workspace(
        client, db, chat_id=CHAT_ID, agent="claude", project_id=project_id
    )
    fake.reset_calls()

    found = await cursor.reconcile_workspace(client, db, project_id, created.nonce)

    assert found is not None
    assert found.id == created.workspace_id
    assert fake.calls == [], "a cached nonce costs no request"


async def test_reconcile_without_a_project_id_cannot_answer(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    """Only a per-project listing exists, so a repo-url create is unresolvable."""
    assert await cursor.find_workspace_by_nonce(client, None, "abcdefgh") is None


async def test_create_session_supplies_the_idempotency_key(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    workspace = fake.add_workspace("api/fix-flaky")

    created = await cursor.create_session(
        client, db, workspace_id=workspace.id, agent="claude", model="sonnet"
    )

    body = fake.calls_to("/sessions", method="POST")[0].body
    assert body is not None
    assert body["sessionId"] == created.id
    row = await sessions.get(db, created.id)
    assert row is not None
    assert row.workspace_id == workspace.id
    assert row.is_bound is False


async def test_create_session_is_replayable_with_the_same_id(
    db: Database, fake: FakeConductor, client: ConductorClient
) -> None:
    workspace = fake.add_workspace("api/fix-flaky")
    first = await cursor.create_session(client, db, workspace_id=workspace.id)

    second = await cursor.create_session(
        client, db, workspace_id=workspace.id, session_id=first.id
    )

    assert second.id == first.id
    assert len(fake.sessions) == 1


# ── the named scenarios, end to end ──────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_every_scenario_delivers_every_answer_exactly_once(
    db: Database, clients: ClientFactory, name: str
) -> None:
    """The headline property, swept over all nine scripted sequences.

    No ``TurnState`` appears anywhere in this test: the cursor alone loses
    nothing and repeats nothing, including through the queued-idle trap, a turn
    that starts and finishes between polls, a persistent ``error`` status and a
    ``/status`` endpoint that is 500ing throughout.
    """
    scenario = SCENARIOS[name]()
    client = clients(scenario.fake)
    session = scenario.session
    await bind(db, session)
    scenario.start()

    for _ in range(len(session.script) + 3):
        await cursor.drain(client, db, session.session_id)

    assert await transcript.count(db, session.session_id) == len(session.transcript)
    await assert_each_answer_delivered_once(db, session)


async def test_the_queued_idle_trap_delivers_the_answer_the_status_hides(
    db: Database, clients: ClientFactory
) -> None:
    """``idle, idle, idle, working, idle×3`` — the answer lands after ``working``.

    A finalize-on-first-idle design loses this reply. The cursor never reads the
    status at all, which is exactly why it cannot.
    """
    scenario = queued_idle_trap(advance=Advance.MANUAL)
    client = clients(scenario.fake)
    session = scenario.session
    await bind(db, session)
    scenario.start()

    for _ in range(len(session.script) + 2):
        session.tick()
        drained = await cursor.drain(client, db, session.session_id)
        assert drained.dropped == 0

    assert "the answer is 42" in await delivered_html(db, session.session_id)
    await assert_each_answer_delivered_once(db, session)


async def test_a_fast_turn_is_delivered_even_though_working_is_never_seen(
    db: Database, clients: ClientFactory
) -> None:
    scenario = fast_turn(advance=Advance.MANUAL)
    client = clients(scenario.fake)
    session = scenario.session
    await bind(db, session)
    scenario.start()

    for _ in range(len(session.script) + 2):
        session.tick()
        await cursor.drain(client, db, session.session_id)

    assert "done: 7" in await delivered_html(db, session.session_id)
    await assert_each_answer_delivered_once(db, session)


async def test_error_mid_turn_still_delivers_the_partial_output(
    db: Database, clients: ClientFactory
) -> None:
    """The drain is unconditional, so partial output is durable before the error
    is ever surfaced — transition 17's ``ForceDrain`` is belt and braces."""
    scenario = error_mid_turn(advance=Advance.MANUAL)
    client = clients(scenario.fake)
    session = scenario.session
    await bind(db, session)
    scenario.start()

    for _ in range(len(session.script) + 2):
        session.tick()
        await cursor.drain(client, db, session.session_id)

    html = await delivered_html(db, session.session_id)
    assert "partial output 3" in html
    assert "trailing after the error" in html
    await assert_each_answer_delivered_once(db, session)
    assert await transcript.count(db, session.session_id) == len(session.transcript)
