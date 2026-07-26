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
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum
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

from ctb.conductor.client import TokenBucket
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo.deliveries import DeliveryRow
from ctb.db.repo.transcript import DeliveryDraft
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
    "MAX_RETRY_AFTER_S",
    "NO_LINK_PREVIEW",
    "FocusTracker",
    "Outbox",
    "Priority",
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

#: "One global send queue ~15 msg/min". Telegram's own group ceiling is ~20/min;
#: this sits under it with room for the status card's edits.
SEND_RATE_PER_MINUTE: Final = 15.0
#: A three-part answer should still arrive as one burst rather than over 12s.
SEND_BURST: Final = 3.0
#: Rows claimed per pass. Small on purpose: a claimed row is unavailable to the
#: priority sort, so a big batch would let an error queue behind stale bulk.
CLAIM_BATCH: Final = 5
#: How long a topic counts as "the topic you're in" after the user acts in it.
#: (The 30-minute figure in PLAN is the *notification* focus rule, which is a
#: different knob, owned by the bot layer.)
FOCUS_WINDOW_S: Final = 300.0
#: Compatibility/configuration knob retained for health output and callers.
#: Transient failures are never abandoned merely because they hit this count.
MAX_ATTEMPTS: Final = 5
#: Cap on how long we will sit on a Telegram ``retry_after`` inline.
MAX_RETRY_AFTER_S: Final = 60.0
#: Telegram's counter is coarse; overshoot slightly rather than 429 twice.
RETRY_AFTER_PADDING_S: Final = 0.5
#: Idle poll interval for :meth:`Outbox.run` when the queue is empty.
IDLE_INTERVAL_S: Final = 1.0

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


class FocusTracker:
    """The ``(chat_id, thread_id)`` the user last acted in.

    PLAN §Notifications ranks the topic you are *in* ahead of everything else,
    which means the queue has to learn something only the bot layer knows. This
    is that seam, and it is deliberately the whole seam: two attribute writes,
    no lock, no I/O — it runs on every incoming update.

    The routing middleware writes it, the outbox reads it, and in production
    both take the process-wide :func:`focus_tracker` — there is no wiring to
    forget. Both sides accept an explicit instance so a test can hand them the
    same tracker on a fake clock instead of racing a global.
    """

    __slots__ = ("_at", "_clock", "_key")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._key: tuple[int, int] | None = None
        self._at = float("-inf")

    def note(self, chat_id: int, thread_id: int = NO_THREAD_ID) -> None:
        self._key = (chat_id, thread_id)
        self._at = self._clock()

    @property
    def current(self) -> tuple[int, int] | None:
        return self._key

    def is_focused(self, chat_id: int, thread_id: int, window: float) -> bool:
        return (
            self._key == (chat_id, thread_id) and (self._clock() - self._at) <= window
        )

    def clear(self) -> None:
        self._key = None
        self._at = float("-inf")


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
    """Claims delivery rows and sends them, one global paced queue."""

    def __init__(
        self,
        bot: TelegramSender,
        db: Database,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rate_per_minute: float = SEND_RATE_PER_MINUTE,
        burst: float = SEND_BURST,
        batch_size: int = CLAIM_BATCH,
        idle_interval: float = IDLE_INTERVAL_S,
        focus_window: float = FOCUS_WINDOW_S,
        max_attempts: int = MAX_ATTEMPTS,
        claim_id: str | None = None,
        quick_replies: QuickReplyFactory = _default_quick_replies,
        focus: FocusTracker | None = None,
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
        self._pacer = TokenBucket(
            rate=max(rate_per_minute, 1.0) / 60.0,
            burst=max(burst, 1.0),
            clock=clock,
            sleep=sleep,
        )
        self._focus = focus if focus is not None else focus_tracker()
        self._pause_until = float("-inf")
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
        }

    # -- introspection ----------------------------------------------------

    @property
    def claim_id(self) -> str:
        return self._claim_id

    @property
    def paused_for(self) -> float:
        """Seconds left on a Telegram-imposed global pause. 0 when running."""
        return max(0.0, self._pause_until - self._clock())

    def health(self) -> dict[str, Any]:
        """Counters for ``/health``. No content, ever."""
        return {
            **self._counters,
            "claim_id": self._claim_id,
            "paused_for": round(self.paused_for, 3),
            "focus": self._focus.current,
            "running": self._task is not None and not self._task.done(),
        }

    async def pending_count(self, session_id: str | None = None) -> int:
        return await deliveries_repo.pending_count(self._db, session_id)

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
        session_index: int = 0,
        priority: Priority = Priority.NORMAL,
        silent: bool = False,
    ) -> int:
        """Queue a durable non-transcript message (a ``Notify`` action).

        ``key`` becomes the row's ``message_id``, so a repeated notice with the
        same key — the ``once_key`` the machine emits — is deduped by the
        primary key rather than by bookkeeping in the caller.
        """
        parts = [
            MessagePart(
                kind=PartKind.TEXT,
                index=index,
                html=chunk,
                plain=strip_html(chunk),
                silent=silent,
            )
            for index, chunk in enumerate(chunk_html(html))
        ]
        return await self.enqueue_parts(
            parts,
            session_id=session_id,
            message_id=f"notice:{key}",
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
            await self._pacer.acquire()
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
        """Recover, then claim-and-send forever."""
        await self.recover()
        while True:
            sent = await self.run_once()
            if sent == 0:
                await self._idle_wait()

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
        """
        if self._recovered:
            return 0
        self._recovered = True
        result = await deliveries_repo.recover_orphaned(
            self._db, claim_id=self._claim_id
        )
        self._counters["recovery_skipped"] += len(result.skipped)
        if result.total:
            _log.info(
                "outbox.recovered",
                claimed=len(result.claimed),
                skipped=len(result.skipped),
            )
        sent = 0
        for job in self._prioritize(self._prepare(result.claimed)):
            await self._pacer.acquire()
            if await self._deliver(job):
                sent += 1
                self._counters["recovered"] += 1
        return sent

    async def run_once(self) -> int:
        """Claim one batch and send it. Returns the number of messages sent."""
        if self.paused_for > 0:
            return 0
        rows = await deliveries_repo.claim(
            self._db, claim_id=self._claim_id, limit=self._batch_size
        )
        if not rows:
            return 0
        jobs = self._prioritize(self._prepare(rows))
        sent = 0
        for index, job in enumerate(jobs):
            if self.paused_for > 0:
                # Telegram told us to back off. Hand the rest of the batch back
                # so a restart (or the next pass) re-orders it with fresh
                # priorities instead of replaying a stale ordering.
                await self._requeue_all(jobs[index:])
                break
            await self._pacer.acquire()
            if await self._deliver(job):
                sent += 1
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

    async def _deliver(self, job: _Job) -> bool:
        """Send one row and record the outcome. Never raises."""
        row = job.row
        if job.part is None:
            await self._skip(row, "unreadable payload")
            return False
        if row.kind == "activity":
            # An activity line belongs on the status card. If one ever reaches
            # the queue, drop it rather than narrating tool calls into the chat.
            await self._skip(row, "activity belongs to the status card")
            return False
        try:
            tg_message_id = await self._send_part(row, job.part)
        except (TelegramForbiddenError, TelegramNotFound, TelegramEntityTooLarge) as e:
            await self._fail(row, exc=e, retry=False)
            return False
        except TelegramBadRequest as exc:
            # The entity-parse retry already happened inside _send_html. A 400
            # that survives it is a request-shape problem; retrying is a loop.
            await self._fail(row, exc=exc, retry=False)
            return False
        except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as exc:
            await self._fail(row, exc=exc, retry=True)
            return False
        except Exception as exc:  # never let one row kill the worker
            await self._fail(row, exc=exc, retry=True)
            return False
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
        return True

    async def _send_part(self, row: DeliveryRow, part: MessagePart) -> int | None:
        """Dispatch by part kind, absorbing at most one Telegram 429."""
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
                        self._quick_replies(
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
                if attempt == 0:
                    # Sends are sequential, so sleeping here pauses the whole
                    # queue, which is exactly what a 429 asks for.
                    await self._sleep(delay + RETRY_AFTER_PADDING_S)
                    continue
                self._pause_until = self._clock() + delay
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

    async def _fail(self, row: DeliveryRow, *, exc: BaseException, retry: bool) -> None:
        # A prolonged Telegram outage must not silently turn a reply into a
        # terminal failure. Only definite permanent errors selected by
        # _deliver use retry=False.
        will_retry = retry
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
