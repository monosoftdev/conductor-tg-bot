"""The outbox: the single path by which anything reaches Telegram.

Nothing else in the bot calls ``sendMessage``. Everything — transcript content,
notices, command replies — comes through here, so the rate limit, the
entity-parse retry and the ``link_preview_options.is_disabled`` flag are applied
exactly once, in one place.

Three properties matter, in this order:

**Never lose a reply.** Rows are claimed with the repo's conditional
``pending -> sending`` update, so two overlapping pollers cannot both send one.
The residual window is a crash between the Telegram call and the DB write, and
that is resolved deliberately in favour of at-least-once: on boot, rows stranded
in ``sending`` are re-sent, guarded by the ``content_hash`` check in
:func:`ctb.db.repo.deliveries.recover_orphaned` so the common case is skipped. A
rare duplicate beats a silently lost reply.

**Never let formatting drop a reply.** Any ``TelegramBadRequest`` that mentions
entity parsing gets **exactly one** retry with ``parse_mode=None`` and the
pre-computed plain text. The message may look ugly; it goes out.

**Stay under Telegram's limits.** One global queue paced at ~15 messages/minute,
ordering topics by (1) the topic you are in, (2) errors, (3) everything else.
Ordering is computed per ``(chat_id, thread_id)`` group, never per row, so a
late-arriving error can never overtake earlier content *inside* a topic — the
transcript order a reader depends on is preserved.

The status card is a separate object with a separate task. A card bug cannot
block a send here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Final, Protocol

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, LinkPreviewOptions

from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, now_ms
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo.deliveries import DeliveryRow
from ctb.db.repo.transcript import DeliveryDraft
from ctb.delivery.pacing import (
    CHAT_BURST,
    CHAT_RATE_PER_MINUTE,
    DestinationRotor,
    TelegramPacer,
)
from ctb.delivery.render.chunk import (
    MessagePart,
    PartKind,
    chunk_blocks,
    chunk_html,
)
from ctb.delivery.render.html import strip_html
from ctb.delivery.render.types import TELEGRAM_TEXT_LIMIT, Block
from ctb.logging import get_logger

__all__ = [
    "CLAIM_BATCH",
    "ENTITY_ERROR_MARKERS",
    "FOCUS_WINDOW_S",
    "MAX_ATTEMPTS",
    "INLINE_RETRY_CEILING_S",
    "MAX_RETRY_AFTER_S",
    "NO_LINK_PREVIEW",
    "FocusTracker",
    "ControlFactory",
    "Outbox",
    "Priority",
    "notice_message_id",
    "QuickReplyFactory",
    "SEND_BURST",
    "SEND_RATE_PER_MINUTE",
    "TelegramSender",
    "blocks_to_parts",
    "delivery_payload",
    "drafts_for",
    "focus_tracker",
    "is_entity_error",
]

_log = get_logger(__name__)

# ── policy constants (PLAN §Notifications) ───────────────────────────────────

#: Retained as the historical names for the per-chat budget, which now lives
#: in :mod:`ctb.delivery.pacing` alongside the separate global one.
SEND_RATE_PER_MINUTE: Final = CHAT_RATE_PER_MINUTE
SEND_BURST: Final = CHAT_BURST
#: Rows claimed per pass. Small on purpose: a claimed row is unavailable to the
#: priority sort, so a big batch would let an error queue behind stale bulk.
CLAIM_BATCH: Final = 5
#: How long a topic counts as "the topic you're in" after the user acts in it.
#: (The 30-minute figure in PLAN is the *notification* focus rule, which is a
#: different knob, owned by the bot layer.)
FOCUS_WINDOW_S: Final = 300.0
#: Retries before a *retryable* failure is given up on. Permanence is normally
#: decided by the error, not by a counter — a Telegram outage must not abandon a
#: reply because it happened five times. This is the backstop for the other
#: case: a row that fails reproducibly (a payload the renderer chokes on) is
#: re-claimed first on every single pass, because `claim` orders by
#: ``(session_index, part_index)``. Without a cap that one row owns a claim slot
#: and a per-chat token forever, and inflates the pending count that the
#: shedding threshold reads.
MAX_ATTEMPTS: Final = 5
#: Cap on how long we will sit on a Telegram ``retry_after`` inline.
MAX_RETRY_AFTER_S: Final = 60.0
#: Past this, a 429 is not waited out inline at all. One outbox task serves
#: every tenant, so an inline sleep is a process-wide stall; beyond a couple of
#: seconds it is strictly better to pause that one chat and move on.
INLINE_RETRY_CEILING_S: Final = 2.0
#: Telegram's counter is coarse; overshoot slightly rather than 429 twice.
RETRY_AFTER_PADDING_S: Final = 0.5
#: Idle poll interval for :meth:`Outbox.run` when the queue is empty.
IDLE_INTERVAL_S: Final = 1.0
#: How stale a mid-turn session's ``updated_at`` may be before the hold lapses.
#: A live poller touches it every tick, so anything older means nobody is coming
#: back for that turn and its output must go out now.
HOLD_FRESH_MS: Final = 300_000
#: The longest one destination's queue may be held, however healthy the turn
#: looks. A turn that runs longer than this is real, so the batch goes out —
#: silently, which is the point: the cap bounds *latency*, never the single
#: buzz, and a state machine wedged in ``WORKING`` costs half an hour, not a
#: reply.
MAX_HOLD_MS: Final = 1_800_000

#: PLAN: link previews are disabled everywhere. Agent output is full of URLs and
#: a preview card under every reply is noise.
NO_LINK_PREVIEW: Final = LinkPreviewOptions(is_disabled=True)

#: Substrings that identify a ``TelegramBadRequest`` caused by our own markup.
#: Deliberately broad — a false positive costs one unformatted retry, a false
#: negative costs the reply.
ENTITY_ERROR_MARKERS: Final[tuple[str, ...]] = (
    "entit",
    "start tag",
    "end tag",
    "can't parse",
    "cant parse",
    "unsupported",
    "parse_mode",
)


#: Substrings that identify a vanished forum topic. Telegram phrases it as a
#: 400 *or* a 404 depending on the method, and neither is a dead chat: the group
#: is fine and the answer can still be delivered to its General.
#:
#: ``TOPIC_ID_INVALID`` is the raw MTProto refusal, and it leaks through
#: untranslated on the *private-chat* topic methods — where it is the only
#: signal there is. A DM ignores ``message_thread_id`` for a deleted topic and
#: silently sends to the root (tdlib/telegram-bot-api#854), so a rename is the
#: one call that still answers "is this room there?", and this is how it says
#: no. Treating it as anything else is what made ``/board`` refuse to reopen a
#: workspace whose DM topic had been deleted from the phone.
THREAD_GONE_MARKERS: Final[tuple[str, ...]] = (
    "message thread not found",
    "topic_deleted",
    "topic deleted",
    "thread not found",
    "topic not found",
    "topic_id_invalid",
    "topic id invalid",
)


def _thread_is_gone(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in THREAD_GONE_MARKERS)


class _Outcome(Enum):
    """What one row's send means for the rows queued behind it.

    ``DROPPED`` is terminal — the row will never send, so nothing waits on it.
    ``DEFERRED`` means it is pending again, and everything behind it in the same
    topic must wait or the reader sees the parts out of order.
    """

    SENT = "sent"
    DROPPED = "dropped"
    DEFERRED = "deferred"


class Priority(IntEnum):
    """Send order. Lower goes first. Persisted in ``deliveries.payload_json``.

    :data:`FOCUS` is never persisted — it is computed at send time from the
    topic the user last acted in.
    """

    FOCUS = 0
    ERROR = 10
    NORMAL = 20
    BULK = 30


# ── focus: the topic the thumb is in ─────────────────────────────────────────


def notice_message_id(key: str) -> str:
    """A notice's delivery id, namespaced away from transcript message ids.

    Public because a caller that wants to *revise* the notice it queued has to
    find the row again, and guessing the prefix at the other end is how the two
    drift apart.
    """
    return f"notice:{key}"


class FocusTracker:
    """The ``(chat_id, thread_id)`` pairs users last acted in, and when.

    PLAN §Notifications ranks the topic you are *in* ahead of everything else,
    which means the queue has to learn something only the bot layer knows. This
    is that seam, and it is deliberately the whole seam: one dict write, no
    lock, no I/O — it runs on every incoming update.

    It holds *many* entries rather than one. With a single shared bot, one
    slot would mean every customer's thumb overwriting every other customer's;
    ``chat_id`` is already globally tenant-unique (see ``tenant_chats``), so no
    tenant plumbing is needed here.

    The routing middleware writes it, the outbox reads it, and in production
    both take the process-wide :func:`focus_tracker` — there is no wiring to
    forget. Both sides accept an explicit instance so a test can hand them the
    same tracker on a fake clock instead of racing a global.
    """

    __slots__ = ("_clock", "_entries", "_max")

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, max_entries: int = 256
    ) -> None:
        self._clock = clock
        self._max = max(1, max_entries)
        self._entries: OrderedDict[tuple[int, int], float] = OrderedDict()

    def note(self, chat_id: int, thread_id: int = NO_THREAD_ID) -> None:
        key = (chat_id, thread_id)
        self._entries[key] = self._clock()
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    @property
    def current(self) -> tuple[int, int] | None:
        """The most recently touched topic. Diagnostics only."""
        if not self._entries:
            return None
        return next(reversed(self._entries))

    def is_focused(self, chat_id: int, thread_id: int, window: float) -> bool:
        at = self._entries.get((chat_id, thread_id))
        return at is not None and (self._clock() - at) <= window

    def clear(self) -> None:
        self._entries.clear()


_focus = FocusTracker()


def focus_tracker() -> FocusTracker:
    """The process-wide tracker, used by both sides unless told otherwise."""
    return _focus


# ── the Bot surface we use ───────────────────────────────────────────────────


class TelegramSender(Protocol):
    """The slice of ``aiogram.Bot`` the outbox needs.

    A Protocol rather than ``Bot`` so tests can supply a recorder, and so this
    module never grows a dependency on the bot layer's construction order.
    """

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        link_preview_options: LinkPreviewOptions | None = None,
        disable_notification: bool | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Any: ...

    async def send_document(
        self,
        *,
        chat_id: int,
        document: BufferedInputFile,
        message_thread_id: int | None = None,
        caption: str | None = None,
        parse_mode: str | None = None,
        disable_notification: bool | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Any: ...


class QuickReplyFactory(Protocol):
    """Build a keyboard for agent decision options at send time."""

    def __call__(
        self,
        choices: Sequence[str],
        session_id: str,
        *,
        chat_id: int,
        thread_id: int,
    ) -> InlineKeyboardMarkup | None: ...


class ControlFactory(Protocol):
    """Build a keyboard for bot-owned durable notice controls."""

    def __call__(
        self,
        buttons: Sequence[str],
        session_id: str,
        *,
        chat_id: int,
        thread_id: int,
    ) -> InlineKeyboardMarkup | None: ...


def _default_quick_replies(
    choices: Sequence[str],
    session_id: str,
    *,
    chat_id: int,
    thread_id: int,
) -> InlineKeyboardMarkup | None:
    # Late import preserves the delivery layer's boot independence.
    from ctb.bot.keyboards import quick_reply_keyboard

    return quick_reply_keyboard(
        choices,
        session_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )


def _default_control_buttons(
    buttons: Sequence[str],
    session_id: str,
    *,
    chat_id: int,
    thread_id: int,
) -> InlineKeyboardMarkup | None:
    # Late import preserves the delivery layer's boot independence.
    from ctb.bot.keyboards import status_card_keyboard
    from ctb.turn.state import CardButton

    known: list[CardButton] = []
    for value in buttons:
        try:
            known.append(CardButton(value))
        except ValueError:
            continue
    return status_card_keyboard(
        known,
        session_id,
        columns=1,
        chat_id=chat_id,
        thread_id=thread_id,
    )


# ── payload helpers, shared with the poller ──────────────────────────────────


def delivery_payload(part: MessagePart, *, priority: Priority = Priority.NORMAL) -> str:
    """Serialise a part for ``deliveries.payload_json``.

    The priority rides along in the payload because it is a property of the
    content (an error outranks a tool summary) rather than of the queue.
    """
    data = part.payload()
    if priority is not Priority.NORMAL:
        data["priority"] = int(priority)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def drafts_for(
    parts: Sequence[MessagePart],
    *,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    priority: Priority = Priority.NORMAL,
) -> tuple[DeliveryDraft, ...]:
    """Rendered parts → ``DeliveryDraft``s for ``transcript.advance_cursor``.

    The poller queues deliveries in the *same* transaction that advances the
    cursor, so this returns drafts rather than writing anything: the cursor must
    never move past content that failed to enqueue.
    """
    return tuple(
        DeliveryDraft(
            chat_id=chat_id,
            thread_id=thread_id,
            part_index=part.index,
            kind=part.kind.value,
            priority=int(priority),
            payload_json=delivery_payload(part, priority=priority),
            content_hash=part.content_hash,
        )
        for part in parts
    )


def blocks_to_parts(
    blocks: Sequence[Block], *, turn_id: str = "", limit: int = TELEGRAM_TEXT_LIMIT
) -> list[MessagePart]:
    """Renderer output → outbox rows, so the poller has one import for the seam."""
    return chunk_blocks(blocks, turn_id=turn_id, limit=limit)


def is_entity_error(exc: TelegramBadRequest) -> bool:
    """Whether a 400 was caused by our HTML rather than by the request shape."""
    message = str(exc.message if exc.message else exc).lower()
    return any(marker in message for marker in ENTITY_ERROR_MARKERS)


# ── internals ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Job:
    """A claimed row with its payload already decoded, so the sort is cheap."""

    row: DeliveryRow
    part: MessagePart | None
    priority: int


def _message_id_of(result: Any) -> int | None:
    value = getattr(result, "message_id", None)
    return value if isinstance(value, int) else None


def _thread_arg(thread_id: int) -> int | None:
    """``0`` means "no topic" in the DB and ``None`` on the wire."""
    return thread_id or None


def _plain_of(part: MessagePart) -> str:
    return part.plain or strip_html(part.html)


class Outbox:
    """Claims delivery rows and sends them, fairly, under two rate budgets."""

    def __init__(
        self,
        bot: TelegramSender,
        db: Database,
        *,
        pacer: TelegramPacer | None = None,
        rate_per_minute: float = CHAT_RATE_PER_MINUTE,
        burst: float = CHAT_BURST,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        batch_size: int = CLAIM_BATCH,
        orphan_after_ms: int = deliveries_repo.ORPHAN_AFTER_MS,
        idle_interval: float = IDLE_INTERVAL_S,
        focus_window: float = FOCUS_WINDOW_S,
        max_attempts: int = MAX_ATTEMPTS,
        claim_id: str | None = None,
        quick_replies: QuickReplyFactory = _default_quick_replies,
        control_buttons: ControlFactory = _default_control_buttons,
        focus: FocusTracker | None = None,
        hold_fresh_ms: int = HOLD_FRESH_MS,
        max_hold_ms: int = MAX_HOLD_MS,
    ) -> None:
        self._bot = bot
        self._db = db
        self._clock = clock
        self._sleep = sleep
        self._batch_size = max(1, batch_size)
        self._idle_interval = max(0.0, idle_interval)
        self._focus_window = max(0.0, focus_window)
        self._max_attempts = max(1, max_attempts)
        self._claim_id = claim_id or deliveries_repo.new_claim_id()
        self._quick_replies = quick_replies
        self._control_buttons = control_buttons
        # ``pacer`` is the production path: one instance shared with the
        # status cards, so both streams spend the same per-token budget. The
        # rate/burst arguments build a private one, which is what a test that
        # only cares about a single chat's pacing wants.
        self._pacer = (
            pacer
            if pacer is not None
            else TelegramPacer(
                chat_rate_per_minute=max(rate_per_minute, 1.0),
                chat_burst=max(burst, 1.0),
                clock=clock,
                sleep=sleep,
            )
        )
        self._rotor = DestinationRotor(clock=clock)
        self._focus = focus if focus is not None else focus_tracker()
        self._orphan_after_ms = orphan_after_ms
        # Zero disables the hold outright, which is what a test that wants the
        # old send-as-it-arrives behaviour asks for.
        self._hold_fresh_ms = max(0, hold_fresh_ms)
        self._max_hold_ms = max(0, max_hold_ms)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._recovered = False
        self._counters: dict[str, int] = {
            "sent": 0,
            "skipped": 0,
            "retried": 0,
            "entity_retries": 0,
            "rate_limited": 0,
            "failed": 0,
            "recovered": 0,
            "recovery_skipped": 0,
            "rerouted": 0,
            "held": 0,
            "loop_errors": 0,
        }

    # -- introspection ----------------------------------------------------

    @property
    def claim_id(self) -> str:
        return self._claim_id

    def paused_for_chat(self, chat_id: int) -> float:
        """Seconds left on a 429 pause for one chat. 0 when it may send."""
        return self._pacer.paused_for(chat_id)

    @property
    def paused_for(self) -> float:
        """Seconds left on a Telegram-imposed *global* pause. 0 when running.

        A single group's 429 pauses only that group; see
        :meth:`ctb.delivery.pacing.TelegramPacer.pause_chat`.
        """
        return self._pacer.global_paused_for

    def health(self) -> dict[str, Any]:
        """Counters for ``/health``. No content, ever."""
        return {
            **self._counters,
            "claim_id": self._claim_id,
            "paused_for": round(self.paused_for, 3),
            "focus": self._focus.current,
            **self._pacer.health(),
            "running": self._task is not None and not self._task.done(),
        }

    async def pending_count(self, session_id: str | None = None) -> int:
        return await deliveries_repo.pending_count(self._db, session_id)

    async def _has_stranded_rows(self) -> bool:
        """Anything in ``sending`` that is not ours, and so still to recover."""
        return (
            await deliveries_repo.count_unclaimed_sending(
                self._db, claim_id=self._claim_id
            )
            > 0
        )

    # -- focus ------------------------------------------------------------

    def note_activity(self, chat_id: int, thread_id: int = NO_THREAD_ID) -> None:
        """The user just did something here — prioritise this topic's sends."""
        self._focus.note(chat_id, thread_id)

    def _is_focused(self, chat_id: int, thread_id: int) -> bool:
        return self._focus.is_focused(chat_id, thread_id, self._focus_window)

    # -- enqueueing -------------------------------------------------------

    async def enqueue_parts(
        self,
        parts: Sequence[MessagePart],
        *,
        session_id: str,
        message_id: str,
        chat_id: int,
        thread_id: int = NO_THREAD_ID,
        session_index: int = 0,
        priority: Priority = Priority.NORMAL,
    ) -> int:
        """Queue rendered parts. Returns how many rows were newly created.

        Re-queueing the same ``(session_id, message_id, part_index, chat_id)``
        is a no-op, so a caller that crashed mid-loop can simply repeat.
        """
        created = 0
        for part in parts:
            if await deliveries_repo.enqueue(
                self._db,
                session_id=session_id,
                message_id=message_id,
                chat_id=chat_id,
                thread_id=thread_id,
                part_index=part.index,
                session_index=session_index,
                kind=part.kind.value,
                payload_json=delivery_payload(part, priority=priority),
                priority=int(priority),
                hash_of_content=part.content_hash,
            ):
                created += 1
        if created:
            self._wake.set()
        return created

    async def enqueue_notice(
        self,
        html: str,
        *,
        session_id: str,
        key: str,
        chat_id: int,
        thread_id: int = NO_THREAD_ID,
        session_index: int | None = None,
        priority: Priority = Priority.NORMAL,
        silent: bool = False,
        control_buttons: Sequence[str] = (),
    ) -> int:
        """Queue a durable non-transcript message (a ``Notify`` action).

        ``key`` becomes the row's ``message_id``, so a repeated notice with the
        same key — the ``once_key`` the machine emits — is deduped by the
        primary key rather than by bookkeeping in the caller.

        ``session_index`` defaults to *behind everything already queued* for
        this destination, not to zero. Claims come back in ``session_index``
        order, so a notice minted now with index 0 sorts ahead of transcript
        chunks queued minutes ago — the reader would see "the session stalled"
        printed above the answer it refers to.
        """
        if session_index is None:
            session_index = await deliveries_repo.max_pending_index(
                self._db, chat_id=chat_id, thread_id=thread_id
            )
        chunks = chunk_html(html)
        parts = [
            MessagePart(
                kind=PartKind.TEXT,
                index=index,
                html=chunk,
                plain=strip_html(chunk),
                silent=silent,
                control_buttons=(
                    tuple(control_buttons) if index == len(chunks) - 1 else ()
                ),
            )
            for index, chunk in enumerate(chunks)
        ]
        return await self.enqueue_parts(
            parts,
            session_id=session_id,
            message_id=notice_message_id(key),
            chat_id=chat_id,
            thread_id=thread_id,
            session_index=session_index,
            priority=priority,
        )

    # -- direct sends (command replies; nothing durable to resume) ---------

    async def send_text(
        self,
        html: str,
        *,
        chat_id: int,
        thread_id: int = NO_THREAD_ID,
        silent: bool = False,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> list[int]:
        """Send now, paced and entity-retried, without persisting anything.

        For interactive replies, where waiting on the queue would feel broken
        and where a crash mid-send should not resurrect the message later.
        Returns the Telegram message ids that landed; ``[]`` on failure.
        """
        sent: list[int] = []
        chunks = chunk_html(html)
        for index, chunk in enumerate(chunks):
            await self._pacer.acquire_global()
            try:
                message_id = await self._send_html(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    html=chunk,
                    plain=strip_html(chunk),
                    silent=silent,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                )
            except Exception as exc:
                _log.warning(
                    "outbox.send_text_failed",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    error=repr(exc),
                )
                break
            if message_id is not None:
                sent.append(message_id)
        return sent

    # -- the worker -------------------------------------------------------

    async def start(self) -> None:
        """Spawn the worker task. Recovery runs first, inside the task."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="ctb-outbox")

    async def stop(self) -> None:
        """Cancel the worker and hand unsent claims back as ``pending``.

        A clean shutdown leaves nothing in ``sending``, so the next boot's
        at-least-once recovery has nothing to second-guess.
        """
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await deliveries_repo.release(self._db, self._claim_id)

    async def run(self) -> None:
        """Recover, then claim-and-send forever.

        A database hiccup must not take the process down. This is a *critical*
        service, so an exception escaping here cancels Telegram polling, the
        status cards, the supervisor and the voice workers — a `PoolTimeout`
        from a moment of connection pressure would turn a slow minute into a
        full restart. The supervisor's own loop already survives per pass; this
        one now does too, backing off so a genuinely broken database is not
        hammered.
        """
        with contextlib.suppress(asyncio.CancelledError):
            await self._run_forever()

    async def _run_forever(self) -> None:
        await self._guarded(self.recover)
        failures = 0
        while True:
            sent = await self._guarded(self.run_once)
            if sent is None:
                failures += 1
                await self._sleep(
                    min(self._idle_interval * 2**failures, MAX_RETRY_AFTER_S)
                )
                continue
            failures = 0
            if sent == 0:
                if not self._recovered:
                    # Rows were still inside the orphan window last time.
                    sent += await self._guarded(self.recover) or 0
                await self._idle_wait()

    async def _guarded(self, step: Callable[[], Awaitable[int]]) -> int | None:
        """Run one pass. ``None`` means it failed and the caller should back off."""
        try:
            return await step()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._counters["loop_errors"] += 1
            _log.error("outbox.pass_failed", error=repr(exc)[:500])
            return None

    async def _idle_wait(self) -> None:
        pause = self.paused_for
        timeout = max(self._idle_interval, pause) if pause > 0 else self._idle_interval
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def recover(self) -> int:
        """Re-send rows a previous incarnation left in ``sending``.

        At-least-once by design. :func:`~ctb.db.repo.deliveries.recover_orphaned`
        has already skipped any orphan whose payload matches a recently sent row
        in the same chat, so in the common case this does nothing at all.

        Recovery only takes rows whose claim has gone stale, because a fresh
        one may still be in flight on an overlapping deployment. That means a
        *fast* restart can find rows too young to touch — so this stays armed
        until a pass finds none left, and :meth:`run` calls it again on each
        idle tick. Latching after the first pass would strand them forever.
        """
        if self._recovered:
            return 0
        result = await deliveries_repo.recover_orphaned(
            self._db, claim_id=self._claim_id, orphan_after_ms=self._orphan_after_ms
        )
        self._recovered = not await self._has_stranded_rows()
        self._counters["recovery_skipped"] += len(result.skipped)
        if result.total:
            _log.info(
                "outbox.recovered",
                claimed=len(result.claimed),
                skipped=len(result.skipped),
            )
        sent = 0
        for job in self._prioritize(self._prepare(result.claimed)):
            await self._pacer.acquire_global()
            if await self._deliver(job):
                sent += 1
                self._counters["recovered"] += 1
        return sent

    async def _sendable(
        self, destinations: Sequence[deliveries_repo.Destination]
    ) -> list[deliveries_repo.Destination]:
        """Drop the destinations whose turn is still running.

        One task is one notification. An agent narrates as it works, and every
        one of those messages lands in the tray even when it is sent silently —
        so unless the topic is ``/notify loud``, the batch waits for the turn to
        finish and arrives with the finish line, which is the buzz.

        Nothing here decides *whether* a row sends, only *when*: a destination
        whose head has waited :data:`MAX_HOLD_MS` is released regardless, and
        :func:`~ctb.db.repo.deliveries.held_destinations` already ignores a
        session whose poller has stopped writing. Both valves fail open.
        """
        if not destinations or not self._hold_fresh_ms:
            return list(destinations)
        held = await deliveries_repo.held_destinations(
            self._db, fresh_within_ms=self._hold_fresh_ms
        )
        if not held:
            return list(destinations)
        stamp = now_ms()
        ready = [
            destination
            for destination in destinations
            if destination.key not in held
            or stamp - destination.head_at >= self._max_hold_ms
        ]
        self._counters["held"] += len(destinations) - len(ready)
        return ready

    async def run_once(self) -> int:
        """Serve each destination in turn. Returns the number of messages sent.

        Claiming is scoped to one ``(chat, topic)`` at a time, so ordering
        within a topic is exact while several topics drain in parallel — and
        one tenant's ten-thousand-row backlog cannot monopolise the sender.
        A destination that has spent its per-chat budget is *skipped*, not
        waited on, and one whose turn is still running is held — see
        :meth:`_sendable`.
        """
        if self.paused_for > 0:
            return 0
        pending = await deliveries_repo.pending_destinations(self._db, limit=64)
        destinations = self._rotor.order(
            await self._sendable(pending),
            key=lambda item: (item.chat_id, item.thread_id),
            urgency=lambda item: (
                int(Priority.FOCUS)
                if self._focus.is_focused(
                    item.chat_id, item.thread_id, self._focus_window
                )
                else item.priority
            ),
        )
        sent = 0
        served = 0
        for destination in destinations:
            if self.paused_for > 0:
                break
            if not self._pacer.chat_ready(destination.chat_id):
                continue
            served += 1
            rows = await deliveries_repo.claim(
                self._db,
                claim_id=self._claim_id,
                limit=self._batch_size,
                tenant_id=destination.tenant_id,
                chat_id=destination.chat_id,
                thread_id=destination.thread_id,
            )
            self._rotor.served((destination.chat_id, destination.thread_id))
            if not rows:
                continue
            sent += await self._drain(jobs=self._prioritize(self._prepare(rows)))

        if sent == 0 and destinations and served == 0:
            # Every destination is saturated and there is nobody else to serve.
            # Waiting beats spinning: one busy topic should still drain at its
            # own rate. ``_drain`` blocks on the chat budget per message.
            head = destinations[0]
            if self._pacer.paused_for(head.chat_id) <= 0:
                rows = await deliveries_repo.claim(
                    self._db,
                    claim_id=self._claim_id,
                    limit=self._batch_size,
                    tenant_id=head.tenant_id,
                    chat_id=head.chat_id,
                    thread_id=head.thread_id,
                )
                self._rotor.served((head.chat_id, head.thread_id))
                if rows:
                    sent += await self._drain(
                        jobs=self._prioritize(self._prepare(rows))
                    )
        return sent

    async def _drain(self, *, jobs: Sequence[_Job]) -> int:
        """Send one destination's claimed batch, in order, until told to stop.

        **Stopping on a deferral is what preserves order.** These rows share a
        ``(chat_id, thread_id)``, so they are the transcript a reader scrolls
        through. If part 2 goes back to ``pending`` after a network blip and we
        carry on with parts 3 and 4, the next pass re-claims part 2 — and the
        reader sees ``3, 4, 2``. A row that is *terminally* gone blocks nothing
        and the batch continues past it.
        """
        sent = 0
        for index, job in enumerate(jobs):
            if self.paused_for > 0 or self._pacer.paused_for(job.row.chat_id) > 0:
                # Telegram told us to back off. Hand the rest of the batch back
                # so the next pass re-orders it with fresh priorities instead of
                # replaying a stale ordering.
                await self._requeue_all(jobs[index:])
                break
            # Both budgets, per message: Telegram limits a token *and* a chat,
            # and spending one token per destination visit would let a single
            # chat send a whole batch on a single token.
            await self._pacer.acquire_chat(job.row.chat_id)
            await self._pacer.acquire_global()
            outcome = await self._deliver(job)
            if outcome is _Outcome.SENT:
                sent += 1
            elif outcome is _Outcome.DEFERRED:
                await self._requeue_all(jobs[index + 1 :])
                break
        return sent

    async def flush(self, *, max_passes: int = 100) -> int:
        """Drain the queue. For tests, shutdown, and ``/health`` diagnostics."""
        total = 0
        for _ in range(max_passes):
            sent = await self.run_once()
            if sent == 0:
                break
            total += sent
        return total

    # -- ordering ---------------------------------------------------------

    def _prepare(self, rows: Iterable[DeliveryRow]) -> list[_Job]:
        return [_decode(row) for row in rows]

    def _prioritize(self, jobs: Sequence[_Job]) -> list[_Job]:
        """Order topics, never rows.

        The rank of a ``(chat_id, thread_id)`` group is the best rank any of its
        rows carries; rows keep their claim order inside the group. That is what
        stops an error from jumping ahead of the answer that explains it.
        """
        best: dict[tuple[int, int], int] = {}
        for job in jobs:
            key = (job.row.chat_id, job.row.thread_id)
            rank = (
                int(Priority.FOCUS)
                if self._is_focused(job.row.chat_id, job.row.thread_id)
                else job.priority
            )
            best[key] = min(best.get(key, int(Priority.BULK)), rank)
        # sorted() is stable: within a group the claim order survives untouched.
        return sorted(jobs, key=lambda job: best[(job.row.chat_id, job.row.thread_id)])

    async def _requeue_all(self, jobs: Sequence[_Job]) -> None:
        for job in jobs:
            await deliveries_repo.requeue(self._db, job.row.key)

    # -- sending ----------------------------------------------------------

    async def _deliver(self, job: _Job) -> _Outcome:
        """Send one row and record the outcome. Never raises.

        The distinction the caller needs is not "did it work" but "may the rest
        of this topic go now". A row that will *never* send blocks nothing; a
        row put back to ``pending`` blocks everything behind it, because
        sending part 3 while part 2 waits is a reordering the reader sees.
        """
        row = job.row
        if job.part is None:
            await self._skip(row, "unreadable payload")
            return _Outcome.DROPPED
        if row.kind == "activity":
            # An activity line belongs on the status card. If one ever reaches
            # the queue, drop it rather than narrating tool calls into the chat.
            await self._skip(row, "activity belongs to the status card")
            return _Outcome.DROPPED
        try:
            tg_message_id = await self._send_part(row, job.part)
        except (TelegramForbiddenError, TelegramEntityTooLarge) as exc:
            await self._fail(row, exc=exc, retry=False)
            return _Outcome.DROPPED
        except (TelegramNotFound, TelegramBadRequest) as exc:
            # A deleted forum topic is not a dead chat. Failing it terminally
            # loses that turn's output for good — the transcript cursor advanced
            # in the same transaction that queued these rows, so nothing can
            # re-derive them. Send it to the group's General instead, where the
            # owner can still read the answer.
            if _thread_is_gone(exc) and row.thread_id != NO_THREAD_ID:
                return await self._reroute_to_general(row, exc)
            # The entity-parse retry already happened inside _send_html. A 400
            # that survives it is a request-shape problem; retrying is a loop.
            await self._fail(row, exc=exc, retry=False)
            return _Outcome.DROPPED
        except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as exc:
            return await self._fail(row, exc=exc, retry=True)
        except Exception as exc:  # never let one row kill the worker
            return await self._fail(row, exc=exc, retry=True)
        await deliveries_repo.mark_sent(self._db, row.key, tg_message_id=tg_message_id)
        self._counters["sent"] += 1
        _log.debug(
            "outbox.sent",
            session_id=row.session_id,
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            part_index=row.part_index,
            kind=row.kind,
        )
        return _Outcome.SENT

    async def _reroute_to_general(
        self, row: DeliveryRow, exc: BaseException
    ) -> _Outcome:
        """Re-home one row from a vanished topic to the group's General.

        And free the room itself. Moving only the *row* left the session still
        pointing at the dead thread, so the next turn queued there and paid this
        reroute again, row by row, forever — while ``/board`` kept offering a
        jump to a topic that does not exist. ``room_gone`` is idempotent, so
        the rest of this batch discovering the same thing costs nothing.
        """
        moved = await deliveries_repo.move_to_thread(
            self._db, row.key, thread_id=NO_THREAD_ID
        )
        self._counters["rerouted"] += 1
        _log.warning(
            "outbox.topic_gone",
            session_id=row.session_id,
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            error=repr(exc)[:200],
        )
        await self._release_room(row)
        if moved is None:  # pragma: no cover - the row was just claimed
            await self._fail(row, exc=exc, retry=False)
            return _Outcome.DROPPED
        # Deferred, not dropped: the row is pending again at a new destination,
        # and the rest of this batch belongs to the destination that is gone.
        return _Outcome.DEFERRED

    async def _release_room(self, row: DeliveryRow) -> None:
        """Tell the one seam that a topic is gone. Never fails a delivery.

        Imported here rather than at module scope: ``handlers.topics`` imports
        ``THREAD_GONE_MARKERS`` from this module, and delivery must not grow a
        construction-order dependency on the bot layer.
        """
        try:
            from ctb.bot.handlers.topics import room_gone

            await room_gone(self._bot, self._db, row.session_id)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - cosmetics never fail delivery
            _log.warning(
                "outbox.room_release_failed",
                session_id=row.session_id,
                error=repr(exc)[:200],
            )

    async def _send_part(self, row: DeliveryRow, part: MessagePart) -> int | None:
        """Dispatch by part kind, absorbing one *short* Telegram 429.

        A 429 is never waited out inline beyond :data:`INLINE_RETRY_CEILING_S`.
        There is one outbox task in the process, so sleeping here stops every
        other tenant too — a single group hitting its per-group flood limit with
        ``retry_after=60`` would freeze the whole instance for a minute. Past
        the ceiling the chat is paused in the pacer and the row is handed back:
        the pause is per chat, so everyone else keeps sending, and this row is
        re-claimed once the pause expires.
        """
        for attempt in (0, 1):
            try:
                if part.kind is PartKind.DOCUMENT:
                    return await self._send_document(row, part)
                return await self._send_html(
                    chat_id=row.chat_id,
                    thread_id=row.thread_id,
                    html=part.html,
                    plain=_plain_of(part),
                    silent=part.silent,
                    reply_markup=(
                        self._control_buttons(
                            part.control_buttons,
                            row.session_id,
                            chat_id=row.chat_id,
                            thread_id=row.thread_id,
                        )
                        if part.control_buttons
                        else self._quick_replies(
                            part.quick_replies,
                            row.session_id,
                            chat_id=row.chat_id,
                            thread_id=row.thread_id,
                        )
                        if part.quick_replies
                        else None
                    ),
                )
            except TelegramRetryAfter as exc:
                delay = min(max(float(exc.retry_after), 0.0), MAX_RETRY_AFTER_S)
                self._counters["rate_limited"] += 1
                _log.warning(
                    "outbox.rate_limited",
                    chat_id=row.chat_id,
                    retry_after=delay,
                    attempt=attempt,
                )
                if attempt == 0 and delay <= INLINE_RETRY_CEILING_S:
                    await self._sleep(delay + RETRY_AFTER_PADDING_S)
                    continue
                # Pause *this chat*, not the queue. One customer's group
                # hitting a limit is not everyone else's outage; the pacer
                # escalates to a global pause only when several distinct chats
                # 429 at once, which is the signature of a token-level limit.
                #
                # `exc.retry_after`, not the clamped `delay`: honouring a
                # 300-second cooldown as 60 guarantees another 429.
                self._pacer.pause_chat(row.chat_id, max(float(exc.retry_after), 0.0))
                raise
        return None

    async def _send_html(
        self,
        *,
        chat_id: int,
        thread_id: int,
        html: str,
        plain: str,
        silent: bool,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> int | None:
        """Send HTML; on an entity-parse 400 retry **once**, unformatted.

        The retry is not itself retried. If plain text also fails, the failure
        is real and belongs in ``last_error`` where ``/health`` can show it.
        """
        try:
            result = await self._bot.send_message(
                chat_id=chat_id,
                text=html,
                message_thread_id=_thread_arg(thread_id),
                parse_mode="HTML",
                link_preview_options=NO_LINK_PREVIEW,
                disable_notification=silent or None,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            if not is_entity_error(exc):
                raise
            self._counters["entity_retries"] += 1
            _log.warning("outbox.entity_retry", chat_id=chat_id, error=str(exc.message))
            result = await self._bot.send_message(
                chat_id=chat_id,
                text=plain or " ",
                message_thread_id=_thread_arg(thread_id),
                parse_mode=None,
                link_preview_options=NO_LINK_PREVIEW,
                disable_notification=silent or None,
                reply_markup=reply_markup,
            )
        return _message_id_of(result)

    async def _send_document(self, row: DeliveryRow, part: MessagePart) -> int | None:
        """The overflow path: ``turn-<id>.md`` and ``.diff`` attachments.

        The caption is sent unparsed — captions are plain text (``src/x.py +12
        −3``), and a caption is not worth a 400.
        """
        document = BufferedInputFile(
            (part.content or "").encode("utf-8"),
            filename=part.filename or "output.md",
        )
        result = await self._bot.send_document(
            chat_id=row.chat_id,
            document=document,
            message_thread_id=_thread_arg(row.thread_id),
            caption=part.caption,
            parse_mode=None,
            disable_notification=part.silent or None,
        )
        return _message_id_of(result)

    # -- bookkeeping ------------------------------------------------------

    async def _skip(self, row: DeliveryRow, reason: str) -> None:
        await deliveries_repo.mark_skipped(self._db, row.key, reason=reason)
        self._counters["skipped"] += 1
        _log.info(
            "outbox.skipped",
            session_id=row.session_id,
            chat_id=row.chat_id,
            part_index=row.part_index,
            reason=reason,
        )

    async def _say_a_reply_was_dropped(self, row: DeliveryRow) -> None:
        """Tell the topic that part of an answer will not arrive.

        Best effort by construction: this runs *because* sending to this chat
        keeps failing, so it may well fail too. It is enqueued rather than sent
        so it inherits the same retry and ordering as everything else, and its
        `once_key` keeps a burst of failed parts to one line.
        """
        try:
            await self._enqueue_dropped_notice(row)
        except Exception as exc:  # never let the notice break the claim loop
            _log.warning(
                "outbox.drop_notice_failed",
                session_id=row.session_id,
                error=repr(exc)[:200],
            )

    async def _enqueue_dropped_notice(self, row: DeliveryRow) -> None:
        await self.enqueue_notice(
            "⚠️ Part of a reply could not be delivered after "
            f"{self._max_attempts} attempts. Open 📄 Transcript on the card, "
            "or read it in Conductor — nothing was lost there.",
            session_id=row.session_id,
            key=f"delivery-dropped:{row.session_id}:{row.session_index}",
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            priority=Priority.ERROR,
            silent=False,
        )

    async def _fail(
        self, row: DeliveryRow, *, exc: BaseException, retry: bool
    ) -> _Outcome:
        # A prolonged Telegram outage must not silently turn a reply into a
        # terminal failure. Only definite permanent errors selected by
        # _deliver use retry=False.
        #
        # But a row that fails *reproducibly* — a malformed payload, a quick
        # reply the renderer chokes on — would otherwise retry forever, and
        # because `claim` orders by (session_index, part_index) it is re-claimed
        # first on every pass: one poison row permanently owning a claim slot, a
        # per-chat token and a database write per second, for the life of the
        # deployment. `MAX_ATTEMPTS` is what stops that.
        will_retry = retry and (row.attempts + 1) < self._max_attempts
        if retry and not will_retry:
            _log.error(
                "outbox.giving_up",
                session_id=row.session_id,
                chat_id=row.chat_id,
                part_index=row.part_index,
                attempts=row.attempts + 1,
                error=repr(exc)[:500],
            )
            # This module's whole claim is that a reply is never lost. When one
            # is, the person waiting for it is the last who should find out
            # from a log line — so say it in the chat, with the one control
            # that can still retrieve the text.
            await self._say_a_reply_was_dropped(row)
        await deliveries_repo.mark_failed(
            self._db, row.key, error=repr(exc)[:500], retry=will_retry
        )
        self._counters["retried" if will_retry else "failed"] += 1
        _log.warning(
            "outbox.send_failed",
            session_id=row.session_id,
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            part_index=row.part_index,
            attempts=row.attempts + 1,
            will_retry=will_retry,
            error=repr(exc)[:500],
        )
        return _Outcome.DEFERRED if will_retry else _Outcome.DROPPED


def _decode(row: DeliveryRow) -> _Job:
    """``payload_json`` → job. A corrupt payload is skipped, never crashed on."""
    data: object = None
    if row.payload_json:
        try:
            data = json.loads(row.payload_json)
        except (ValueError, TypeError):
            data = None
    if not isinstance(data, dict):
        return _Job(row=row, part=None, priority=int(Priority.NORMAL))
    raw_priority = data.get("priority")
    priority = raw_priority if isinstance(raw_priority, int) else int(Priority.NORMAL)
    try:
        part = MessagePart.from_payload(data)
    except (ValueError, TypeError):
        return _Job(row=row, part=None, priority=priority)
    if part.kind is PartKind.DOCUMENT:
        usable = part.content is not None
    else:
        usable = bool(part.html.strip())
    return _Job(row=row, part=part if usable else None, priority=priority)
