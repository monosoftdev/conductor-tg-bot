"""Outbox and status-card tests.

The five properties that matter, each asserted against a fake ``Bot``:

* the entity-parse retry fires **exactly once** and the reply still lands;
* the global queue paces at ~15 msg/min;
* boot re-send is at-least-once but skips a payload already sent;
* status-card edits coalesce (three edits inside the window send one);
* priority orders topics without ever reordering content inside a topic.

No network, no real waiting: the clock is a :class:`~tests.conftest.FakeClock`
and every sleep advances it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from aiogram.dispatcher.middlewares.user_context import (
    EVENT_CONTEXT_KEY,
    UserContextMiddleware,
)
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)

from ctb.bot.keyboards import (
    CONTROL_TTL_S,
    Action,
    NonceError,
    NonceStore,
    confirm_keyboard,
    parse,
    quick_reply_keyboard,
    read_stateless,
    resolve,
    stateless_payload,
    status_card_keyboard,
)
from ctb.bot.middleware.routing import RoutingMiddleware
from ctb.db.connection import Database, now_ms
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.delivery.outbox import (
    RECOVERY_RECHECK_S,
    FocusTracker,
    Outbox,
    Priority,
    delivery_payload,
    drafts_for,
    focus_tracker,
    is_entity_error,
)
from ctb.delivery.render.chunk import MessagePart, PartKind
from ctb.delivery.render.types import CodeBlock, TextBlock
from ctb.delivery.status_card import (
    CARD_MIN_EDIT_INTERVAL_S,
    CardState,
    StatusCards,
    card_buttons,
    default_keyboard,
    render_card,
)
from ctb.turn.state import (
    CardButton,
    CardKind,
    EditStatusCard,
    Finalize,
    PostStatusCard,
    SetTurnCost,
    StartTyping,
    StopTyping,
    TurnSummary,
    UpdateActivity,
)
from tests.conftest import FakeClock

SESSION = "sess-1"
#: Card payloads carry the session id, so the length that has to fit inside
#: Telegram's 64-byte callback_data is a real Conductor UUID's.
SESSION_UUID = "0f2a1c33-26bb-4dcd-8d20-c9c198d24f35"
CHAT = -100200300


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeBot:
    """Records calls; raises whatever the per-method script says to raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.script: dict[str, deque[BaseException | None]] = {}
        self._next_id = 1000
        #: Runs once, *inside* the first ``send_message``, so a test can put
        #: another task's work in the window where the real Bot API is waiting
        #: on the network and the caller has no message id yet.
        self.during_send_message: Callable[[], Awaitable[None]] | None = None

    # -- scripting --------------------------------------------------------

    def queue(self, method: str, *outcomes: BaseException | None) -> None:
        self.script.setdefault(method, deque()).extend(outcomes)

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    # -- the Bot surface --------------------------------------------------

    async def send_message(self, **kwargs: Any) -> Any:
        result = self._record("send_message", kwargs)
        hook, self.during_send_message = self.during_send_message, None
        if hook is not None:
            await hook()
        return result

    async def send_document(self, **kwargs: Any) -> Any:
        return self._record("send_document", kwargs)

    async def edit_message_text(self, **kwargs: Any) -> Any:
        return self._record("edit_message_text", kwargs)

    async def delete_message(self, **kwargs: Any) -> Any:
        return self._record("delete_message", kwargs)

    async def send_chat_action(self, **kwargs: Any) -> Any:
        return self._record("send_chat_action", kwargs)

    async def pin_chat_message(self, **kwargs: Any) -> Any:
        return self._record("pin_chat_message", kwargs)

    def _record(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, kwargs))
        queued = self.script.get(method)
        if queued:
            outcome = queued.popleft()
            if outcome is not None:
                raise outcome
        self._next_id += 1
        return _FakeMessage(self._next_id)


class _FakeMessage:
    __slots__ = ("message_id",)

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class Sleeper:
    """A sleep that records and moves the fake clock, so nothing really waits."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.clock.advance(delay)

    @property
    def total(self) -> float:
        return sum(self.delays)


def bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(
        method=SendMessage(chat_id=CHAT, text="x"), message=message
    )


def entity_error() -> TelegramBadRequest:
    return bad_request("Bad Request: can't parse entities: unclosed start tag")


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(
        method=SendMessage(chat_id=CHAT, text="x"), message="connection reset"
    )


def retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=CHAT, text="x"),
        message="Too Many Requests: retry after",
        retry_after=seconds,
    )


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def session(db: Database) -> str:
    await sessions_repo.upsert(db, SESSION)
    return SESSION


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def sleeper(clock: FakeClock) -> Sleeper:
    return Sleeper(clock)


@pytest.fixture
def outbox_factory(
    bot: FakeBot, db: Database, clock: FakeClock, sleeper: Sleeper
) -> Callable[..., Outbox]:
    def make(**overrides: Any) -> Outbox:
        options: dict[str, Any] = {
            "clock": clock,
            "sleep": sleeper,
            "batch_size": 20,
            "burst": 1000.0,  # pacing off unless a test asks for it
            # Recovery normally leaves a fresh claim alone in case an
            # overlapping deployment is still sending it. These tests set the
            # scene and recover immediately, so there is no peer to protect.
            "orphan_after_ms": 0,
            # Its own tracker, on the fake clock: focus is shared process-wide
            # in production, and a test must not inherit another test's thumb.
            "focus": FocusTracker(clock=clock),
        }
        options.update(overrides)
        return Outbox(bot, db, **options)

    return make


@pytest.fixture
def outbox(outbox_factory: Callable[..., Outbox]) -> Outbox:
    return outbox_factory()


def part(index: int = 0, html: str = "<b>hi</b>", plain: str = "hi") -> MessagePart:
    return MessagePart(kind=PartKind.TEXT, index=index, html=html, plain=plain)


async def enqueue(
    outbox: Outbox,
    *,
    message_id: str = "msg-1",
    chat_id: int = CHAT,
    thread_id: int = 0,
    session_index: int = 0,
    priority: Priority = Priority.NORMAL,
    parts: list[MessagePart] | None = None,
    session_id: str = SESSION,
) -> int:
    return await outbox.enqueue_parts(
        parts if parts is not None else [part()],
        session_id=session_id,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
        session_index=session_index,
        priority=priority,
    )


# ── payload helpers ──────────────────────────────────────────────────────────


def test_delivery_payload_round_trips() -> None:
    original = MessagePart(
        kind=PartKind.CODE,
        index=2,
        html="<pre>x</pre>",
        plain="x",
        silent=True,
        quick_replies=("Ship now", "Review first"),
        control_buttons=(CardButton.ARCHIVE.value,),
    )
    data = json.loads(delivery_payload(original, priority=Priority.ERROR))
    assert data["priority"] == int(Priority.ERROR)
    assert MessagePart.from_payload(data) == original


def test_normal_priority_is_not_persisted() -> None:
    data = json.loads(delivery_payload(part()))
    assert "priority" not in data


def test_drafts_for_maps_one_draft_per_part() -> None:
    parts = [part(0), part(1, html="<i>two</i>", plain="two")]
    drafts = drafts_for(parts, chat_id=CHAT, thread_id=7, priority=Priority.ERROR)
    assert [d.part_index for d in drafts] == [0, 1]
    assert {d.chat_id for d in drafts} == {CHAT}
    assert {d.thread_id for d in drafts} == {7}
    assert drafts[0].content_hash == parts[0].content_hash
    assert json.loads(drafts[1].payload_json or "{}")["html"] == "<i>two</i>"


def test_blocks_to_parts_uses_the_chunker() -> None:
    from ctb.delivery.outbox import blocks_to_parts

    parts = blocks_to_parts(
        [TextBlock(html="hello"), CodeBlock(text="print(1)", language="python")],
        turn_id="t1",
    )
    assert parts
    assert parts[0].index == 0
    assert "print(1)" in "".join(p.html for p in parts)


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: can't parse entities: unclosed start tag",
        'Bad Request: unsupported start tag "script"',
        "Bad Request: can't find end tag corresponding to start tag",
        "Bad Request: entity begins after the end of the message",
    ],
)
def test_entity_errors_are_recognised(message: str) -> None:
    assert is_entity_error(bad_request(message))


def test_non_entity_bad_request_is_not_an_entity_error() -> None:
    assert not is_entity_error(bad_request("Bad Request: chat not found"))


# ── the happy path ───────────────────────────────────────────────────────────


async def test_sends_a_queued_part_and_marks_it_sent(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    assert await enqueue(outbox) == 1
    assert await outbox.run_once() == 1

    call = bot.calls_to("send_message")[0]
    assert call["text"] == "<b>hi</b>"
    assert call["parse_mode"] == "HTML"
    assert call["chat_id"] == CHAT
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None
    assert row.state == "sent"
    assert row.tg_message_id is not None


async def test_decision_result_carries_one_tap_replies(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    decision = MessagePart(
        kind=PartKind.TEXT,
        html="<b>Choose</b>",
        plain="Choose",
        quick_replies=("Use SQLite", "Use Postgres"),
    )
    await enqueue(outbox, parts=[decision])

    assert await outbox.run_once() == 1
    markup = bot.calls_to("send_message")[0]["reply_markup"]
    assert markup is not None
    assert [row[0].text for row in markup.inline_keyboard] == [
        "✓ 1 · Use SQLite",
        "2 · Use Postgres",
    ]
    assert markup.inline_keyboard[0][0].style == "success"


def test_options_too_long_to_spell_out_become_bare_numbers() -> None:
    """The old label was a tail-truncated copy of a list already on screen.

    A 48-character budget cannot spell out an option and stay readable, wherever
    the cut falls. The prose stays (the cursor only strips it when the options
    fit), so the numbers are enough.
    """
    long_options = (
        "Delete the duplicate placeholder ## Testing section at line 89.",
        "Tell me which file you meant — there are 9 other root-level .md files.",
        "Leave it as-is.",
    )

    markup = quick_reply_keyboard(long_options, SESSION)

    assert markup is not None
    assert len(markup.inline_keyboard) == 1, "bare numbers belong on one row"
    assert [b.text for b in markup.inline_keyboard[0]] == ["1", "2", "3"]


def test_every_option_in_a_block_gets_its_own_colour() -> None:
    """Two buttons that look the same are two buttons you have to read twice."""
    markup = quick_reply_keyboard(("a" * 60, "b" * 60, "c" * 60, "d" * 60), SESSION)

    assert markup is not None
    styles = [button.style for button in markup.inline_keyboard[0]]
    assert len(set(styles)) == len(styles) == 4
    # `None` is the client's own chrome, not a fallback to Action.SEND's blue.
    assert styles == ["success", "primary", None, "danger"]


def test_options_that_fit_are_still_spelled_out_one_per_row() -> None:
    markup = quick_reply_keyboard(("Use SQLite", "Use Postgres"), SESSION)

    assert markup is not None
    assert [row[0].text for row in markup.inline_keyboard] == [
        "✓ 1 · Use SQLite",
        "2 · Use Postgres",
    ]


async def test_link_previews_are_disabled_everywhere(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    await enqueue(outbox)
    await outbox.run_once()
    await outbox.send_text("<b>direct</b>", chat_id=CHAT)

    calls = bot.calls_to("send_message")
    assert len(calls) == 2
    for call in calls:
        options = call["link_preview_options"]
        assert options is not None and options.is_disabled is True


async def test_thread_zero_becomes_none_on_the_wire(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    await enqueue(outbox, thread_id=0, message_id="a")
    await enqueue(outbox, thread_id=42, message_id="b")
    await outbox.flush()

    threads = [c["message_thread_id"] for c in bot.calls_to("send_message")]
    assert set(threads) == {None, 42}


async def test_document_parts_go_out_as_documents(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    document = MessagePart(
        kind=PartKind.DOCUMENT,
        index=0,
        filename="turn-1.md",
        content="# full output",
        caption="Full output for turn 1",
    )
    await enqueue(outbox, parts=[document])
    assert await outbox.run_once() == 1

    call = bot.calls_to("send_document")[0]
    assert call["document"].filename == "turn-1.md"
    assert call["caption"] == "Full output for turn 1"
    # Captions are plain text; a caption is not worth a 400.
    assert call["parse_mode"] is None


async def test_activity_rows_never_reach_the_chat(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    await deliveries_repo.enqueue(
        db,
        session_id=SESSION,
        message_id="act-1",
        chat_id=CHAT,
        kind="activity",
        payload_json=delivery_payload(part(html="<b>Bash · pytest</b>")),
    )
    assert await outbox.run_once() == 0
    assert bot.calls_to("send_message") == []
    row = await deliveries_repo.get(db, (SESSION, "act-1", 0, CHAT))
    assert row is not None and row.state == "skipped"


async def test_unreadable_payload_is_skipped_not_crashed_on(
    outbox: Outbox, db: Database, session: str
) -> None:
    await deliveries_repo.enqueue(
        db,
        session_id=SESSION,
        message_id="broken",
        chat_id=CHAT,
        payload_json="{not json",
    )
    assert await outbox.run_once() == 0
    row = await deliveries_repo.get(db, (SESSION, "broken", 0, CHAT))
    assert row is not None and row.state == "skipped"


# ── belt and braces: the entity-parse retry ──────────────────────────────────


async def test_entity_error_retries_exactly_once_and_still_delivers(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    bot.queue("send_message", entity_error())
    await enqueue(outbox, parts=[part(html="<b>bold <x>", plain="bold <x>")])

    assert await outbox.run_once() == 1

    calls = bot.calls_to("send_message")
    assert len(calls) == 2, "exactly one retry, no more"
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[1]["parse_mode"] is None
    assert calls[1]["text"] == "bold <x>"
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "sent"
    assert outbox.health()["entity_retries"] == 1


async def test_the_entity_retry_is_not_itself_retried(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    bot.queue("send_message", entity_error(), entity_error())
    await enqueue(outbox)

    assert await outbox.run_once() == 0
    assert len(bot.calls_to("send_message")) == 2
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "failed"


async def test_a_non_entity_bad_request_is_permanent(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    bot.queue("send_message", bad_request("Bad Request: chat not found"))
    await enqueue(outbox)

    assert await outbox.run_once() == 0
    assert len(bot.calls_to("send_message")) == 1
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "failed"


async def test_being_kicked_out_is_permanent(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    bot.queue(
        "send_message",
        TelegramForbiddenError(
            method=SendMessage(chat_id=CHAT, text="x"), message="bot was kicked"
        ),
    )
    await enqueue(outbox)
    await outbox.run_once()

    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "failed"


async def test_a_network_blip_goes_back_to_pending(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    bot.queue("send_message", network_error())
    await enqueue(outbox)

    assert await outbox.run_once() == 0
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "pending" and row.attempts == 1

    assert await outbox.run_once() == 1
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "sent"


async def test_a_transient_failure_retries_until_the_attempt_cap(
    outbox_factory: Callable[..., Outbox], bot: FakeBot, db: Database, session: str
) -> None:
    """Retry a blip; give up on a row that fails every single time.

    Both halves matter. A Telegram outage must not abandon a reply after two
    tries — but a row that fails *reproducibly* is re-claimed first on every
    pass, because claims are ordered by ``(session_index, part_index)``. With no
    cap it owns a claim slot and a per-chat token for the life of the process.
    """
    outbox = outbox_factory(max_attempts=3)
    for _ in range(3):
        bot.queue("send_message", network_error())
    await enqueue(outbox)

    for _ in range(2):
        await outbox.run_once()
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "pending" and row.attempts == 2

    await outbox.run_once()
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "failed" and row.attempts == 3


async def test_a_poison_row_stops_blocking_the_topic_behind_it(
    outbox_factory: Callable[..., Outbox], bot: FakeBot, db: Database, session: str
) -> None:
    """Head-of-line blocking is the reason the cap exists."""
    outbox = outbox_factory(max_attempts=2)
    for _ in range(2):
        bot.queue("send_message", network_error())
    await enqueue(outbox, message_id="poison", session_index=1)
    await enqueue(outbox, message_id="good", session_index=2)

    for _ in range(3):
        await outbox.run_once()

    poison = await deliveries_repo.get(db, (SESSION, "poison", 0, CHAT))
    good = await deliveries_repo.get(db, (SESSION, "good", 0, CHAT))
    assert poison is not None and poison.state == "failed"
    assert good is not None and good.state == "sent"


# ── Telegram 429 ─────────────────────────────────────────────────────────────


async def test_a_short_retry_after_is_waited_out_inline(
    outbox: Outbox, bot: FakeBot, sleeper: Sleeper, db: Database, session: str
) -> None:
    bot.queue("send_message", retry_after(1))
    await enqueue(outbox)

    assert await outbox.run_once() == 1
    assert 1.0 <= sleeper.total <= 2.0
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "sent"


async def test_a_long_retry_after_pauses_that_chat_instead_of_stalling_everyone(
    outbox: Outbox, bot: FakeBot, sleeper: Sleeper, db: Database, session: str
) -> None:
    """One outbox task serves every tenant, so an inline sleep is global.

    A group hitting its flood limit with ``retry_after=60`` must not freeze
    every other workspace's deliveries for a minute.
    """
    bot.queue("send_message", retry_after(60))
    await enqueue(outbox)

    assert await outbox.run_once() == 0
    assert sleeper.total == 0.0  # nobody waited
    assert outbox.paused_for_chat(CHAT) >= 60.0  # this chat did
    assert outbox.paused_for == 0
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "pending"


async def test_a_second_429_pauses_that_chat_and_requeues_the_batch(
    outbox: Outbox, bot: FakeBot, clock: FakeClock, db: Database, session: str
) -> None:
    """One group's flood limit is that group's problem, not everyone's.

    With a shared bot serving every workspace, a global pause here would let
    one customer's busy topic stop the whole service.
    """
    bot.queue("send_message", retry_after(5))
    await enqueue(outbox, message_id="a", session_index=1)
    await enqueue(outbox, message_id="b", session_index=2)

    assert await outbox.run_once() == 0
    assert outbox.paused_for_chat(CHAT) > 0
    assert outbox.paused_for == 0  # …and nobody else is affected
    # The untouched row went back to pending rather than sitting claimed.
    second = await deliveries_repo.get(db, (SESSION, "b", 0, CHAT))
    assert second is not None and second.state == "pending"
    # And that chat really is paused.
    assert await outbox.run_once() == 0

    clock.advance(10.0)
    assert await outbox.run_once() >= 1


# ── pacing ───────────────────────────────────────────────────────────────────


async def test_the_global_queue_paces_at_fifteen_a_minute(
    outbox_factory: Callable[..., Outbox], sleeper: Sleeper, session: str
) -> None:
    outbox = outbox_factory(rate_per_minute=15.0, burst=3.0)
    for index in range(5):
        await enqueue(outbox, message_id=f"m{index}", session_index=index)

    assert await outbox.flush() == 5
    # Three ride the burst; the last two wait 60/15 = 4s each.
    assert len(sleeper.delays) == 2
    assert sleeper.total == pytest.approx(8.0, abs=0.5)


async def test_a_burst_sized_turn_is_not_paced(
    outbox_factory: Callable[..., Outbox], sleeper: Sleeper, session: str
) -> None:
    outbox = outbox_factory(rate_per_minute=15.0, burst=3.0)
    await enqueue(outbox, parts=[part(0), part(1), part(2)])

    assert await outbox.flush() == 3
    assert sleeper.delays == []


# ── priority ─────────────────────────────────────────────────────────────────


async def test_priority_orders_topics_focus_then_errors_then_the_rest(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    await enqueue(outbox, message_id="normal", thread_id=1, session_index=1)
    await enqueue(
        outbox,
        message_id="error",
        thread_id=2,
        session_index=2,
        priority=Priority.ERROR,
    )
    await enqueue(outbox, message_id="focused", thread_id=3, session_index=3)
    outbox.note_activity(CHAT, 3)

    assert await outbox.run_once() == 3
    assert [c["message_thread_id"] for c in bot.calls_to("send_message")] == [3, 2, 1]


async def test_priority_never_reorders_content_inside_a_topic(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    await enqueue(
        outbox,
        message_id="answer",
        thread_id=1,
        session_index=1,
        parts=[part(html="<b>answer</b>", plain="answer")],
    )
    await enqueue(
        outbox,
        message_id="boom",
        thread_id=1,
        session_index=2,
        priority=Priority.ERROR,
        parts=[part(html="<b>boom</b>", plain="boom")],
    )

    await outbox.run_once()
    assert [c["text"] for c in bot.calls_to("send_message")] == [
        "<b>answer</b>",
        "<b>boom</b>",
    ]


async def test_focus_expires(
    outbox_factory: Callable[..., Outbox], bot: FakeBot, clock: FakeClock, session: str
) -> None:
    outbox = outbox_factory(focus_window=60.0)
    await enqueue(
        outbox, message_id="err", thread_id=2, session_index=1, priority=Priority.ERROR
    )
    await enqueue(outbox, message_id="plain", thread_id=3, session_index=2)
    outbox.note_activity(CHAT, 3)
    clock.advance(61.0)

    await outbox.run_once()
    assert [c["message_thread_id"] for c in bot.calls_to("send_message")] == [2, 3]


async def test_an_incoming_update_focuses_the_topic_it_arrived_in(
    outbox_factory: Callable[..., Outbox],
    bot: FakeBot,
    db: Database,
    session: str,
) -> None:
    """PLAN tier 1, wired exactly as production wires it: no injection.

    ``Priority.FOCUS`` existed but nothing ever set the focus, so the queue was
    really errors-then-everything-else.
    """
    outbox = outbox_factory(focus=None)  # -> the process-wide tracker
    await enqueue(
        outbox, message_id="err", thread_id=2, session_index=1, priority=Priority.ERROR
    )
    await enqueue(outbox, message_id="plain", thread_id=3, session_index=2)

    update = Update(
        update_id=1,
        message=Message(
            message_id=10,
            date=dt.datetime(2026, 7, 26, 12, 0, tzinfo=dt.UTC),
            chat=Chat(id=CHAT, type="supergroup"),
            from_user=User(id=7, is_bot=False, first_name="O"),
            text="go",
            message_thread_id=3,
            is_topic_message=True,
        ),
    )

    async def handler(event: Any, payload: dict[str, Any]) -> None:
        return None

    try:
        await RoutingMiddleware(db=db)(
            handler,
            update,
            {
                EVENT_CONTEXT_KEY: UserContextMiddleware.resolve_event_context(update),
                "db": db,
            },
        )
        assert focus_tracker().current == (CHAT, 3)

        await outbox.run_once()
        assert [c["message_thread_id"] for c in bot.calls_to("send_message")] == [3, 2]
    finally:
        focus_tracker().clear()


async def test_parts_of_one_message_stay_in_order(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    parts = [
        part(index, html=f"<b>{index}</b>", plain=str(index)) for index in range(4)
    ]
    await enqueue(outbox, parts=parts)

    await outbox.run_once()
    assert [c["text"] for c in bot.calls_to("send_message")] == [
        "<b>0</b>",
        "<b>1</b>",
        "<b>2</b>",
        "<b>3</b>",
    ]


# ── claiming and boot recovery ───────────────────────────────────────────────


async def test_two_workers_never_send_the_same_row(
    outbox_factory: Callable[..., Outbox], bot: FakeBot, session: str
) -> None:
    first = outbox_factory()
    second = outbox_factory()
    await enqueue(first)

    assert await first.run_once() == 1
    assert await second.run_once() == 0
    assert len(bot.calls_to("send_message")) == 1


async def test_boot_resends_rows_stranded_in_sending(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    await enqueue(outbox)
    # A previous incarnation claimed it and died before Telegram answered.
    await deliveries_repo.claim(db, claim_id="dead-worker")

    assert await outbox.recover() == 1
    assert len(bot.calls_to("send_message")) == 1
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "sent"


async def test_boot_does_not_duplicate_when_the_content_hash_matches(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    payload = part()
    # The identical payload already went out moments ago…
    await deliveries_repo.enqueue(
        db,
        session_id=SESSION,
        message_id="already",
        chat_id=CHAT,
        payload_json=delivery_payload(payload),
        hash_of_content=payload.content_hash,
    )
    await deliveries_repo.mark_sent(db, (SESSION, "already", 0, CHAT), tg_message_id=1)
    # …and this row was stranded mid-send.
    await enqueue(outbox, message_id="orphan", parts=[payload])
    await deliveries_repo.claim(db, claim_id="dead-worker")

    assert await outbox.recover() == 0
    assert bot.calls_to("send_message") == []
    row = await deliveries_repo.get(db, (SESSION, "orphan", 0, CHAT))
    assert row is not None and row.state == "skipped"


async def test_boot_resends_when_the_hash_differs(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    sent = part(html="<b>old</b>", plain="old")
    await deliveries_repo.enqueue(
        db,
        session_id=SESSION,
        message_id="already",
        chat_id=CHAT,
        payload_json=delivery_payload(sent),
        hash_of_content=sent.content_hash,
    )
    await deliveries_repo.mark_sent(db, (SESSION, "already", 0, CHAT), tg_message_id=1)
    await enqueue(
        outbox, message_id="orphan", parts=[part(html="<b>new</b>", plain="new")]
    )
    await deliveries_repo.claim(db, claim_id="dead-worker")

    assert await outbox.recover() == 1
    assert bot.calls_to("send_message")[0]["text"] == "<b>new</b>"


async def test_recovery_does_not_repeat_itself_inside_the_interval(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    await enqueue(outbox)
    await deliveries_repo.claim(db, claim_id="dead-worker")

    assert await outbox.recover() == 1
    assert await outbox.recover() == 0
    assert len(bot.calls_to("send_message")) == 1


async def test_a_claim_stranded_hours_into_an_uptime_is_still_recovered(
    outbox: Outbox, bot: FakeBot, db: Database, session: str, clock: FakeClock
) -> None:
    """Recovery is a heartbeat, not a boot step.

    It used to latch: the first pass that found nothing stranded disarmed it
    for the life of the process. But a claim strands on *any* crash between the
    Telegram call and the database write — a blip inside ``mark_sent``, a
    cancelled task, a pod evicted mid-send — and every one of those happens
    long after boot. Latched, that row waited for the next deploy to be
    noticed, which on a healthy service is the definition of a stuck message.
    """
    assert await outbox.recover() == 0  # a clean boot: nothing to do
    assert await outbox.recover() == 0  # and it stays quiet inside the window

    # Hours later, a send strands a claim.
    await enqueue(outbox)
    await deliveries_repo.claim(db, claim_id="dead-worker")
    assert await outbox.recover() == 0, "still inside the recheck interval"

    clock.advance(RECOVERY_RECHECK_S + 1)

    assert await outbox.recover() == 1
    assert len(bot.calls_to("send_message")) == 1


async def test_stop_hands_unsent_claims_back(
    outbox: Outbox, db: Database, session: str
) -> None:
    await enqueue(outbox)
    claimed = await deliveries_repo.claim(db, claim_id=outbox.claim_id)
    assert len(claimed) == 1

    await outbox.stop()

    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "pending"


# ── enqueueing ───────────────────────────────────────────────────────────────


async def test_enqueue_is_idempotent(outbox: Outbox, session: str) -> None:
    assert await enqueue(outbox) == 1
    assert await enqueue(outbox) == 0
    assert await outbox.pending_count() == 1


async def test_a_notice_dedupes_on_its_key(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    for _ in range(3):
        await outbox.enqueue_notice(
            "<b>Conductor is back</b>",
            session_id=SESSION,
            key="conductor-up",
            chat_id=CHAT,
        )

    assert await outbox.flush() == 1
    assert bot.calls_to("send_message")[0]["text"] == "<b>Conductor is back</b>"


async def test_send_text_is_chunked_and_carries_the_markup_on_the_last_part(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    markup = InlineKeyboardMarkup(inline_keyboard=[])
    long_html = "\n\n".join("word " * 200 for _ in range(6))

    ids = await outbox.send_text(long_html, chat_id=CHAT, reply_markup=markup)

    calls = bot.calls_to("send_message")
    assert len(calls) == len(ids) > 1
    assert all(call["reply_markup"] is None for call in calls[:-1])
    assert calls[-1]["reply_markup"] is markup


async def test_send_text_survives_a_failure(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    bot.queue("send_message", bad_request("Bad Request: chat not found"))
    assert await outbox.send_text("<b>hi</b>", chat_id=CHAT) == []


async def test_health_reports_counters(
    outbox: Outbox, bot: FakeBot, session: str
) -> None:
    bot.queue("send_message", entity_error())
    await enqueue(outbox)
    await outbox.run_once()

    health = outbox.health()
    assert health["sent"] == 1
    assert health["entity_retries"] == 1
    assert health["claim_id"] == outbox.claim_id


# ── one task, one notification ───────────────────────────────────────────────
#
# `disable_notification` takes the sound away, not the line in the tray, so
# muting a narrating agent still left eight of them per task. Anything but
# `/notify loud` therefore holds the topic's queue until the turn ends. Every
# test below is really the same question asked twice: does it hold while the
# turn is live, and does it fail *open* every other time.


async def working(db: Database, *, at: int | None = None) -> None:
    """Bind the session to the chat and put it mid-turn."""
    await sessions_repo.bind(db, SESSION, chat_id=CHAT, thread_id=0)
    await sessions_repo.update(db, SESSION, at=at, turn_state="WORKING")


async def test_a_running_turn_holds_its_replies(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    await working(db)
    await enqueue(outbox)

    assert await outbox.run_once() == 0
    assert bot.calls_to("send_message") == []
    assert outbox.health()["held"] == 1
    assert await outbox.pending_count() == 1


async def test_the_batch_lands_when_the_turn_finishes(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    await working(db)
    await enqueue(outbox, message_id="a")
    await enqueue(outbox, message_id="b", session_index=1)
    assert await outbox.run_once() == 0

    await sessions_repo.update(db, SESSION, turn_state="IDLE")

    assert await outbox.flush() == 2
    assert len(bot.calls_to("send_message")) == 2


async def test_the_finish_line_lands_last(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    """The buzz is the *last* thing, not the first — it describes the batch."""
    await working(db)
    await enqueue(outbox, message_id="answer")
    await outbox.enqueue_notice(
        "✅ Done", session_id=SESSION, key="turn-done:1", chat_id=CHAT
    )
    assert await outbox.run_once() == 0

    await sessions_repo.update(db, SESSION, turn_state="IDLE")
    await outbox.flush()

    assert [call["text"] for call in bot.calls_to("send_message")][-1] == "✅ Done"


async def test_loud_still_sends_every_reply_as_it_arrives(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    await working(db)
    await chats_repo.ensure(db, CHAT, 0)
    await chats_repo.set_notify(db, CHAT, 0, notify="loud")
    await enqueue(outbox)

    assert await outbox.run_once() == 1
    assert outbox.health()["held"] == 0


async def test_off_holds_too(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    """`off` means no buzz, not "stream it at me" — only `loud` opts back in."""
    await working(db)
    await chats_repo.ensure(db, CHAT, 0)
    await chats_repo.set_notify(db, CHAT, 0, notify="off")
    await enqueue(outbox)

    assert await outbox.run_once() == 0


async def test_a_dead_poller_does_not_hold_the_output_hostage(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    """`WORKING` written an hour ago means nobody is coming back for it."""
    await working(db, at=now_ms() - 3_600_000)
    await enqueue(outbox)

    assert await outbox.run_once() == 1


async def test_a_turn_that_never_ends_releases_the_queue_anyway(
    outbox_factory: Callable[..., Outbox],
    bot: FakeBot,
    db: Database,
    session: str,
) -> None:
    """The cap is on latency. A machine wedged in `WORKING` costs a wait."""
    outbox = outbox_factory(max_hold_ms=0)
    await working(db)
    await enqueue(outbox)

    assert await outbox.run_once() == 1


async def test_an_unbound_session_is_never_held(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    """Nobody is polling it, so no more output is coming to wait for."""
    await sessions_repo.update(db, SESSION, turn_state="WORKING", is_bound=False)
    await enqueue(outbox)

    assert await outbox.run_once() == 1


async def test_another_topic_drains_while_this_one_holds(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    await working(db)
    await sessions_repo.upsert(db, "sess-2", chat_id=CHAT + 1, thread_id=0)
    await enqueue(outbox, message_id="held")
    await enqueue(outbox, message_id="free", chat_id=CHAT + 1, session_id="sess-2")

    assert await outbox.run_once() == 1
    assert bot.calls_to("send_message")[0]["chat_id"] == CHAT + 1


async def test_one_stuck_turn_cannot_hold_another_session_s_reply(
    outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    """The clog, in one test.

    A hold used to key on the *room*: any session addressed at
    ``(chat, thread)`` being mid-turn held everything queued there. One room
    per session makes that the same question — **except at ``thread_id = 0``**,
    which the linear DM seat, a group's General and every session ``room_gone``
    has parked all share. So one container that never came up, its poller
    dutifully refreshing ``updated_at`` every tick, held the whole chat root:
    another session's finished answer, and every notice queued behind it, for
    as long as the cap allowed.
    """
    await working(db)  # sess-1: stuck mid-turn, addressed at the chat root
    await sessions_repo.upsert(db, "sess-2", chat_id=CHAT, thread_id=0)
    await sessions_repo.update(db, "sess-2", turn_state="IDLE")
    await enqueue(outbox, message_id="answer", session_id="sess-2")

    assert await outbox.run_once() == 1
    assert bot.calls_to("send_message")[0]["chat_id"] == CHAT


# ── the status card ──────────────────────────────────────────────────────────


@pytest.fixture
def cards(
    bot: FakeBot, db: Database, clock: FakeClock, sleeper: Sleeper
) -> StatusCards:
    return StatusCards(bot, db, clock=clock, sleep=sleeper, pin=False)


def test_render_card_walks_the_documented_states() -> None:
    now = 100.0
    queued = render_card(CardState(kind=CardKind.QUEUED, text="queued"), now=now)
    started = render_card(CardState(kind=CardKind.STARTED, text="started"), now=now)
    working = render_card(
        CardState(
            kind=CardKind.WORKING,
            text="working 0s",
            activity="running pytest",
            started_at=20.0,
        ),
        now=now,
    )
    done = render_card(
        CardState(
            kind=CardKind.DONE,
            text="done in 1m32s",
            summary=TurnSummary(files_changed=3),
        ),
        now=now,
    )

    assert queued == "<b>⏳ queued</b>"
    assert started == "<b>▶️ started</b>"
    assert working == "<b>⚙️ working 1m20s</b> · running pytest"
    assert done == "<b>✅ done in 1m32s</b> · 3 files"


def test_a_finished_card_shows_exactly_one_state() -> None:
    """Exhibit B: ``⚙️ working 20s · ✅ done · 45.8s · 12 turns · $0.2887``.

    Three claims in one line — working *and* done, two clocks, four decimals.
    A terminal card drops the activity line whatever the renderer sent, and
    rounds the money to something readable on a phone.
    """
    card = CardState(
        kind=CardKind.DONE,
        text="done in 46s · 12 tools",
        activity="✅ done · 45.8s · 12 turns",
        started_at=20.0,
        cost_usd=0.2887,
        buttons=(CardButton.TRANSCRIPT, CardButton.RETRY, CardButton.OPEN),
    )
    rendered = render_card(card, now=100.0)

    assert rendered == "<b>✅ done in 46s</b> · 12 tools · $0.29"
    assert "working" not in rendered
    assert "45.8s" not in rendered
    assert "0.2887" not in rendered


def test_a_finished_card_never_offers_stop() -> None:
    running = CardState(kind=CardKind.WORKING, buttons=(CardButton.STOP,))
    assert card_buttons(running) == (CardButton.STOP,)

    for kind in (CardKind.DONE, CardKind.ERROR, CardKind.CANCELLED, CardKind.DEAD):
        finished = CardState(
            kind=kind,
            buttons=(CardButton.STOP, CardButton.RETRY, CardButton.TRANSCRIPT),
        )
        assert CardButton.STOP not in card_buttons(finished)
        assert CardButton.RETRY in card_buttons(finished)


def test_cost_is_never_shown_beside_a_live_clock() -> None:
    live = render_card(
        CardState(kind=CardKind.WORKING, text="working 4s", cost_usd=0.2887),
        now=0.0,
    )
    assert "$" not in live


def test_render_card_escapes_the_activity_line() -> None:
    text = render_card(
        CardState(kind=CardKind.WORKING, text="working 1s", activity="grep <a> & b"),
        now=0.0,
    )
    assert "&lt;a&gt; &amp; b" in text
    assert "<a>" not in text


def test_default_keyboard_uses_a_url_button_for_the_deep_link() -> None:
    markup = default_keyboard(
        (CardButton.STOP, CardButton.OPEN),
        session_id="s1",
        deep_link="https://conductor.build/w/1",
    )
    assert markup is not None
    buttons = [row[0] for row in markup.inline_keyboard]
    assert buttons[0].callback_data == "card:stop:s1"
    assert buttons[1].url == "https://conductor.build/w/1"


def test_default_keyboard_drops_open_without_a_deep_link() -> None:
    assert default_keyboard((CardButton.OPEN,), session_id="s1") is None


async def test_the_card_is_posted_once_then_edited(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, db: Database, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
        thread_id=5,
    )
    assert len(bot.calls_to("send_message")) == 1
    assert bot.calls_to("send_message")[0]["message_thread_id"] == 5
    row = await sessions_repo.get(db, SESSION)
    assert row is not None and row.status_card_msg_id is not None

    clock.advance(4.0)
    await cards.apply(
        EditStatusCard(CardKind.STARTED, "started", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
        thread_id=5,
    )
    assert len(bot.calls_to("send_message")) == 1
    assert len(bot.calls_to("edit_message_text")) == 1


async def test_a_new_card_deletes_the_unfinished_one_it_replaces(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    """Two Stops in one topic is two answers to "is this still going?".

    A workspace that wakes and *then* takes a prompt walks this path: rule 9
    posts ``⏳ waking``, and the prompt that follows starts a turn with no card
    id, so the machine posts a second one. The waking card kept a working Stop.
    """
    await cards.apply(
        PostStatusCard(CardKind.WAKING, "waking · preparing", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )
    clock.advance(4.0)
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )

    deleted = bot.calls_to("delete_message")
    assert [call["message_id"] for call in deleted] == [1001]
    assert len(bot.calls_to("send_message")) == 2
    assert cards.health()["superseded"] == 1


async def test_the_tick_never_posts_a_second_copy_of_a_card_in_flight(
    bot: FakeBot, db: Database, clock: FakeClock, session: str
) -> None:
    """The other road to two Stops, and the one `_supersede` cannot close.

    ``_post`` learns the message id only when ``sendMessage`` returns, so for
    the length of that round trip the card is dirty with ``message_id is None``
    — and the tick loop is a *separate task* reading the same ``_Card``. It saw
    exactly what the batch saw and posted the card again. Both copies carried
    Stop; the loser kept the text it was posted with (``⏳ waking · preparing``)
    while the winner ticked on to ``⚙️ working``, which is a live control on a
    message that is no longer describing anything.
    """
    cards = StatusCards(bot, db, clock=clock, pin=False)
    ticks: list[asyncio.Task[None]] = []

    async def fire_the_tick_loop() -> None:
        # A real task, as `run()` uses: the point is that it is *not* this one.
        ticks.append(asyncio.create_task(cards.tick()))
        await asyncio.sleep(0)

    bot.during_send_message = fire_the_tick_loop
    await cards.apply(
        PostStatusCard(CardKind.WAKING, "waking · preparing", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
        thread_id=5,
    )
    await asyncio.gather(*ticks)

    assert len(bot.calls_to("send_message")) == 1
    row = await sessions_repo.get(db, SESSION)
    assert row is not None and row.status_card_msg_id == 1001


async def test_a_refused_delete_still_takes_the_stale_stop_away(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    """A stale *line* is cosmetic. A stale control is a status claim."""
    await cards.apply(
        PostStatusCard(CardKind.WAKING, "waking", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )
    bot.queue("delete_message", bad_request("Bad Request: message can't be deleted"))

    clock.advance(4.0)
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )

    stripped = bot.calls_to("edit_message_text")
    assert len(stripped) == 1
    assert stripped[0]["message_id"] == 1001
    assert stripped[0]["reply_markup"] is None
    assert "waking" in stripped[0]["text"]


async def test_a_finished_card_is_left_in_the_chat_as_history(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    """``done`` already has no Stop, and the turn it summarises really happened."""
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )
    clock.advance(4.0)
    await cards.apply(
        EditStatusCard(CardKind.DONE, "done in 3s", (CardButton.RETRY,)),
        session_id=SESSION,
        chat_id=CHAT,
    )

    clock.advance(4.0)
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )

    assert bot.calls_to("delete_message") == []
    assert len(bot.calls_to("send_message")) == 2


async def test_queued_edits_coalesce_to_the_last_one(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"),
        session_id=SESSION,
        chat_id=CHAT,
    )
    for index, activity in enumerate(("reading files", "running pytest", "writing")):
        clock.advance(0.5)
        await cards.apply(
            EditStatusCard(
                CardKind.WORKING, f"working {index}s", (CardButton.STOP,), activity
            ),
            session_id=SESSION,
            chat_id=CHAT,
        )

    # Three edits inside the 3s window: none of them went out yet.
    assert bot.calls_to("edit_message_text") == []

    clock.advance(3.0)
    await cards.tick()

    edits = bot.calls_to("edit_message_text")
    assert len(edits) == 1, "three queued edits send only the last"
    assert "writing" in edits[0]["text"]
    assert "running pytest" not in edits[0]["text"]


async def test_an_unchanged_card_costs_no_api_call(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    for _ in range(5):
        clock.advance(5.0)
        await cards.apply(
            EditStatusCard(CardKind.QUEUED, "queued"),
            session_id=SESSION,
            chat_id=CHAT,
        )
    assert bot.calls_to("edit_message_text") == []


async def test_the_working_card_ticks_up_on_its_own(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    await cards.apply(
        EditStatusCard(CardKind.WORKING, "working 0s"),
        session_id=SESSION,
        chat_id=CHAT,
    )
    clock.advance(92.0)
    await cards.tick()

    edits = bot.calls_to("edit_message_text")
    assert edits and "1m32s" in edits[-1]["text"]


async def test_a_finished_card_lands_immediately_and_is_retired(
    cards: StatusCards, bot: FakeBot, db: Database, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    # Same batch, same tick as the post: the 3s floor must not delay "done".
    await cards.handle(
        (
            EditStatusCard(
                CardKind.DONE,
                "done in 1m32s",
                (CardButton.TRANSCRIPT, CardButton.OPEN),
            ),
            Finalize(TurnSummary(duration_ms=92_000, files_changed=3, tool_calls=12)),
        ),
        session_id=SESSION,
        chat_id=CHAT,
        deep_link="https://conductor.build/w/1",
    )

    edits = bot.calls_to("edit_message_text")
    assert len(edits) == 1, "one edit for the whole finish batch"
    assert "done in 1m32s" in edits[0]["text"]
    assert "3 files" in edits[0]["text"]
    assert cards.state_for(CHAT) is None, "the card is retired for the next turn"
    row = await sessions_repo.get(db, SESSION)
    assert row is not None and row.status_card_msg_id is None


async def test_the_turn_price_waits_for_the_turn_to_finish(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    """Money is a finished-turn fact, so it never sits beside a live clock."""
    await cards.apply(
        PostStatusCard(CardKind.WORKING, "working 0s", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )
    await cards.handle(
        (UpdateActivity("Bash · pytest -q"), SetTurnCost(0.2887)),
        session_id=SESSION,
        chat_id=CHAT,
    )
    clock.advance(5.0)
    await cards.tick()

    live = bot.calls_to("edit_message_text")[-1]["text"]
    assert "Bash · pytest -q" in live
    assert "$" not in live

    await cards.handle(
        (
            EditStatusCard(
                CardKind.DONE,
                "done in 46s · 12 tools",
                (CardButton.STOP, CardButton.TRANSCRIPT, CardButton.RETRY),
            ),
            Finalize(TurnSummary(duration_ms=46_000, tool_calls=12)),
        ),
        session_id=SESSION,
        chat_id=CHAT,
    )

    finished = bot.calls_to("edit_message_text")[-1]
    assert finished["text"] == "<b>✅ done in 46s</b> · 12 tools · $0.29"
    labels = [
        cell.text for row in finished["reply_markup"].inline_keyboard for cell in row
    ]
    assert not any("Stop" in label for label in labels)


async def test_a_new_turn_posts_a_fresh_card(
    cards: StatusCards, bot: FakeBot, session: str
) -> None:
    for _ in range(2):
        await cards.apply(
            PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
        )
        await cards.handle(
            (
                EditStatusCard(CardKind.DONE, "done in 1s"),
                Finalize(TurnSummary(duration_ms=1000)),
            ),
            session_id=SESSION,
            chat_id=CHAT,
        )
    assert len(bot.calls_to("send_message")) == 2


async def test_typing_is_sent_every_four_seconds_while_working(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    await cards.apply(StartTyping(), session_id=SESSION, chat_id=CHAT)
    assert len(bot.calls_to("send_chat_action")) == 1

    clock.advance(2.0)
    await cards.tick()
    assert len(bot.calls_to("send_chat_action")) == 1

    clock.advance(2.5)
    await cards.tick()
    assert len(bot.calls_to("send_chat_action")) == 2
    assert bot.calls_to("send_chat_action")[0]["action"] == "typing"

    await cards.apply(StopTyping(), session_id=SESSION, chat_id=CHAT)
    clock.advance(10.0)
    await cards.tick()
    assert len(bot.calls_to("send_chat_action")) == 2


async def test_the_card_runs_no_stall_detector_of_its_own(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    """ "Is it stuck?" is the turn machine's question, and only its question.

    The card used to answer it too, from `last_change_at` — which moves only on
    a kind change or a tool-activity line. A turn streaming nothing but text
    therefore got `stalled?` at ten minutes *while it was writing*, and the
    clear was gated on a kind change, so it kept saying it. The machine
    measures real transcript deltas and sends `CardKind.STALLED` when it means
    it.
    """
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    await cards.apply(
        EditStatusCard(CardKind.WORKING, "working 0s", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )

    clock.advance(3601.0)
    await cards.tick()

    assert "stalled?" not in bot.calls_to("edit_message_text")[-1]["text"]
    state = cards.state_for(CHAT)
    assert state is not None and CardButton.CHECK not in state.buttons


async def test_the_machine_saying_stalled_is_what_shows_it(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    clock.advance(CARD_MIN_EDIT_INTERVAL_S + 1)
    await cards.apply(
        EditStatusCard(
            CardKind.STALLED,
            "working 20m01s · stalled?",
            (CardButton.CHECK, CardButton.STOP),
        ),
        session_id=SESSION,
        chat_id=CHAT,
    )

    text = bot.calls_to("edit_message_text")[-1]["text"]
    assert "stalled?" in text
    state = cards.state_for(CHAT)
    assert state is not None and CardButton.CHECK in state.buttons


async def test_a_deleted_card_is_reposted(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    bot.queue(
        "edit_message_text", bad_request("Bad Request: message to edit not found")
    )
    clock.advance(5.0)
    await cards.apply(
        EditStatusCard(CardKind.WORKING, "working 5s"),
        session_id=SESSION,
        chat_id=CHAT,
    )
    await cards.tick()

    assert len(bot.calls_to("send_message")) == 2


async def test_a_card_edit_retries_once_without_markup(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    bot.queue("edit_message_text", entity_error())
    clock.advance(5.0)
    await cards.apply(
        EditStatusCard(CardKind.WORKING, "working 5s", (), "grep <x>"),
        session_id=SESSION,
        chat_id=CHAT,
    )

    edits = bot.calls_to("edit_message_text")
    assert len(edits) == 2
    assert edits[1]["parse_mode"] is None
    assert "<b>" not in edits[1]["text"]


async def test_message_not_modified_is_benign(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    bot.queue(
        "edit_message_text",
        bad_request("Bad Request: message is not modified"),
    )
    clock.advance(5.0)
    await cards.apply(
        EditStatusCard(CardKind.WORKING, "working 5s"),
        session_id=SESSION,
        chat_id=CHAT,
    )
    assert cards.health()["errors"] == 0


async def test_a_card_429_is_honoured_and_the_card_still_lands(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    bot.queue("send_message", retry_after(30))
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    assert cards.message_id_for(CHAT) is None

    clock.advance(5.0)
    await cards.tick()
    assert cards.message_id_for(CHAT) is None, "still inside the backoff"

    clock.advance(30.0)
    await cards.tick()
    assert cards.message_id_for(CHAT) is not None


async def test_a_broken_bot_cannot_stall_the_card_task(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    bot.queue("send_message", RuntimeError("boom"))
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    clock.advance(10.0)
    await cards.tick()  # must not raise
    assert cards.health()["errors"] >= 1


async def test_a_broken_card_cannot_block_the_outbox(
    cards: StatusCards, outbox: Outbox, bot: FakeBot, db: Database, session: str
) -> None:
    bot.queue("send_message", RuntimeError("card is broken"))
    await cards.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )

    await enqueue(outbox)
    assert await outbox.run_once() == 1
    row = await deliveries_repo.get(db, (SESSION, "msg-1", 0, CHAT))
    assert row is not None and row.state == "sent"


async def test_restore_adopts_a_card_from_a_previous_process(
    cards: StatusCards, bot: FakeBot, clock: FakeClock, session: str
) -> None:
    cards.restore(session_id=SESSION, chat_id=CHAT, message_id=4242)
    clock.advance(5.0)
    await cards.apply(
        EditStatusCard(CardKind.WORKING, "working 5s"),
        session_id=SESSION,
        chat_id=CHAT,
    )

    assert bot.calls_to("send_message") == []
    assert bot.calls_to("edit_message_text")[0]["message_id"] == 4242


async def test_a_redeploy_mid_turn_leaves_one_card_and_one_stop(
    bot: FakeBot, db: Database, clock: FakeClock, sleeper: Sleeper, session: str
) -> None:
    """The screenshot bug: two cards, two Stops, after a deploy mid-turn.

    The old process posts the card and persists its id. The new process starts
    with an empty ``_cards`` but a restored machine context that still says the
    turn has one, so it emits ``EditStatusCard`` — and the first thing the boot
    path actually emits is an ``UpdateActivity``, which used to be dropped.
    """
    before = StatusCards(bot, db, clock=clock, sleep=sleeper, pin=False)
    await before.apply(
        PostStatusCard(CardKind.QUEUED, "waking · preparing", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )
    posted = bot.calls_to("send_message")
    assert len(posted) == 1
    card_id = before.message_id_for(CHAT)
    assert card_id is not None

    # -- the deploy lands here: same DB, a brand-new StatusCards ------------
    after = StatusCards(bot, db, clock=clock, sleep=sleeper, pin=False)
    await after.apply(
        UpdateActivity("Bash · gh pr view 1351"), session_id=SESSION, chat_id=CHAT
    )
    # An activity line is not a state: it must not render "queued" over the
    # true "waking · preparing", and it must not post anything.
    assert bot.calls_to("send_message") == posted
    assert bot.calls_to("edit_message_text") == []

    clock.advance(36.0)
    await after.apply(
        EditStatusCard(CardKind.WORKING, "working 36s", (CardButton.STOP,)),
        session_id=SESSION,
        chat_id=CHAT,
    )

    assert bot.calls_to("send_message") == posted, "a second card was posted"
    edits = bot.calls_to("edit_message_text")
    assert [edit["message_id"] for edit in edits] == [card_id]
    # The elapsed counter re-anchors to this process, exactly as the machine's
    # own `_rebase_clock` does — a deploy costs the duration, not the card.
    assert "working" in edits[-1]["text"]
    assert "Bash · gh pr view 1351" in edits[-1]["text"]


async def test_pinning_failure_does_not_lose_the_card(
    bot: FakeBot, db: Database, clock: FakeClock, sleeper: Sleeper, session: str
) -> None:
    pinning = StatusCards(bot, db, clock=clock, sleep=sleeper, pin=True)
    bot.queue("pin_chat_message", bad_request("Bad Request: not enough rights"))

    await pinning.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )
    assert pinning.message_id_for(CHAT) is not None


async def test_the_main_chat_is_never_pinned(
    bot: FakeBot, clock: FakeClock, sleeper: Sleeper
) -> None:
    pinning = StatusCards(
        bot, cast(Database, object()), clock=clock, sleep=sleeper, pin=True
    )

    await pinning.apply(
        PostStatusCard(CardKind.QUEUED, "queued"), session_id=SESSION, chat_id=CHAT
    )

    assert bot.calls_to("pin_chat_message") == []


async def test_topic_cards_are_still_pinned(
    bot: FakeBot, clock: FakeClock, sleeper: Sleeper
) -> None:
    pinning = StatusCards(
        bot, cast(Database, object()), clock=clock, sleep=sleeper, pin=True
    )

    await pinning.apply(
        PostStatusCard(CardKind.QUEUED, "queued"),
        session_id=SESSION,
        chat_id=CHAT,
        thread_id=5,
    )

    pins = bot.calls_to("pin_chat_message")
    assert len(pins) == 1
    assert pins[0]["message_id"] == pinning.message_id_for(CHAT, 5)


async def test_deleting_the_pinned_card_hands_the_pin_to_its_replacement(
    bot: FakeBot, db: Database, clock: FakeClock, sleeper: Sleeper, session: str
) -> None:
    """A topic pins its first card once. Deleting that card unpins the topic.

    Without handing the pin on, superseding the very first card would leave the
    topic pinless for the rest of the session — the card is pinned exactly so
    it stays reachable under a long transcript.

    Only topics pin at all, so this runs in one: in the main chat there is no
    pin to hand on. See :func:`test_the_main_chat_is_never_pinned`.
    """
    pinning = StatusCards(bot, db, clock=clock, sleep=sleeper, pin=True)
    await pinning.apply(
        PostStatusCard(CardKind.WAKING, "waking"),
        session_id=SESSION,
        chat_id=CHAT,
        thread_id=5,
    )
    clock.advance(4.0)

    await pinning.apply(
        PostStatusCard(CardKind.QUEUED, "queued"),
        session_id=SESSION,
        chat_id=CHAT,
        thread_id=5,
    )

    pins = bot.calls_to("pin_chat_message")
    assert len(pins) == 2, "the replacement claims the pin the delete released"
    assert pins[0]["message_id"] != pins[1]["message_id"]
    assert pins[1]["message_id"] == pinning.message_id_for(CHAT, 5)


async def test_unknown_actions_are_left_alone(
    cards: StatusCards, bot: FakeBot, session: str
) -> None:
    from ctb.turn.state import SetCadence

    await cards.apply(SetCadence(6000), session_id=SESSION, chat_id=CHAT)
    assert bot.calls == []


def test_now_ms_is_used_for_wall_clock_columns() -> None:
    # Guard against a future refactor reintroducing seconds in *_at columns.
    assert now_ms() > 1_700_000_000_000


# ── the card after a redeploy ────────────────────────────────────────────────
#
# PLAN's headline live test is a redeploy *while a turn is running*. The reply
# still lands (the cursor never cared), but until now the pinned card's Stop
# answered "This button has expired." — the one control a thumb reaches for.


def _tap(data: str | None, user_id: int = 7) -> CallbackQuery:
    return CallbackQuery(
        id="cb-1",
        from_user=User(id=user_id, is_bot=False, first_name="O"),
        chat_instance="ci-1",
        data=data,
    )


def _card_payload(kind: CardButton, store: NonceStore) -> str:
    markup = status_card_keyboard([kind], SESSION_UUID, store=store)
    assert markup is not None
    data = markup.inline_keyboard[0][0].callback_data
    assert data is not None
    assert len(data.encode()) <= 64  # Telegram's hard cap on callback_data
    return data


def test_stop_minted_before_a_redeploy_still_works_after_it() -> None:
    before = NonceStore()
    data = _card_payload(CardButton.STOP, before)

    after = NonceStore()  # the redeploy: a brand-new process, empty registry
    ticket = resolve(_tap(data), expect=Action.STOP, store=after)
    assert ticket.target == SESSION_UUID


def test_a_destructive_confirm_does_not_survive_a_redeploy() -> None:
    before = NonceStore()
    markup = confirm_keyboard(
        Action.ARCHIVE, SESSION_UUID, "api/fix-flaky", verb="Archive", store=before
    )
    data = markup.inline_keyboard[0][0].callback_data
    assert data is not None

    after = NonceStore()
    with pytest.raises(NonceError) as caught:
        resolve(_tap(data), expect=Action.ARCHIVE, store=after)
    assert caught.value.reason == "unknown"


def test_clear_queue_does_not_survive_a_redeploy_either() -> None:
    """It deletes prompts you typed. A scrolled-back mis-tap must not land."""
    before = NonceStore()
    data = _card_payload(CardButton.CLEAR_QUEUE, before)

    after = NonceStore()
    with pytest.raises(NonceError):
        resolve(_tap(data), expect=Action.CLEAR_QUEUE, store=after)


def test_archive_from_the_card_survives_but_only_opens_the_named_confirm() -> None:
    before = NonceStore()
    data = _card_payload(CardButton.ARCHIVE, before)

    after = NonceStore()
    ticket = resolve(_tap(data), expect=Action.ARCHIVE_REQUEST, store=after)
    assert ticket.action == Action.ARCHIVE_REQUEST.value
    # …and the confirm it opens is a fresh single-use nonce, not a stateless one.
    confirm = confirm_keyboard(
        Action.ARCHIVE, ticket.target, "api/fix-flaky", verb="Archive", store=after
    )
    packed = confirm.inline_keyboard[0][0].callback_data
    assert packed is not None
    assert not parse(packed).nonce.startswith(".")


def test_a_restart_proof_button_is_still_single_use_inside_one_process() -> None:
    store = NonceStore()
    data = _card_payload(CardButton.STOP, store)

    assert resolve(_tap(data), expect=Action.STOP, store=store).target == SESSION_UUID
    with pytest.raises(NonceError) as caught:
        resolve(_tap(data), expect=Action.STOP, store=store)
    assert caught.value.reason == "used"


def test_a_restart_proof_button_spent_after_a_redeploy_is_not_replayable() -> None:
    before = NonceStore()
    data = _card_payload(CardButton.STOP, before)

    after = NonceStore()
    resolve(_tap(data), expect=Action.STOP, store=after)
    with pytest.raises(NonceError) as caught:
        resolve(_tap(data), expect=Action.STOP, store=after)
    assert caught.value.reason == "used"


def test_an_expired_restart_proof_payload_is_refused() -> None:
    payload = stateless_payload(Action.STOP, SESSION_UUID, 0.0)
    assert payload is not None
    with pytest.raises(NonceError) as caught:
        read_stateless(payload, Action.STOP.value)
    assert caught.value.reason == "expired"


def test_a_restart_proof_payload_cannot_be_repointed_at_a_destructive_action() -> None:
    payload = stateless_payload(Action.STOP, SESSION_UUID, CONTROL_TTL_S)
    assert payload is not None
    with pytest.raises(NonceError) as caught:
        read_stateless(payload, Action.ARCHIVE.value)
    assert caught.value.reason == "mismatch"


def test_only_restartable_actions_get_a_stateless_payload() -> None:
    assert stateless_payload(Action.ARCHIVE, SESSION_UUID, CONTROL_TTL_S) is None
    assert stateless_payload(Action.CLEAR_QUEUE, SESSION_UUID, CONTROL_TTL_S) is None
    # A target that would not round-trip (the quick-reply prompt body) is
    # refused rather than silently truncated.
    assert stateless_payload(Action.STOP, f"{SESSION_UUID}\nrun it", 60.0) is None


def test_a_revoked_restart_proof_button_does_not_come_back_to_life() -> None:
    """Revocation has to beat the payload, or archiving would not disarm it."""
    store = NonceStore()
    data = _card_payload(CardButton.STOP, store)
    assert store.revoke_target(SESSION_UUID) == 1

    with pytest.raises(NonceError) as caught:
        resolve(_tap(data), expect=Action.STOP, store=store)
    assert caught.value.reason == "used"
