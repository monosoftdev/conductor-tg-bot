"""The pinned per-topic status card: one message that carries the live turn.

``⏳ queued`` → ``⚙️ working 1m20s · running pytest`` → ``✅ done in 1m32s ·
3 files``.

The machine goes straight from ``queued`` to ``working``: only the kinds in
:data:`_LIVE_KINDS` are re-rendered on every tick, so anything else would leave
a turn that emits no output sitting on a frozen card with no clock.

The card exists so the *chat* can stay a conversation. A tool-heavy turn emits
dozens of activity lines; they all land on this one message instead of forty
bubbles. Two rules make that affordable:

* **At most one edit per 3 seconds.** Queued edits coalesce — three edits inside
  the window send only the last, which is what keeps a tool-heavy turn under
  Telegram's rate limit.
* **No transient messages.** Progress is an edit, never a new "🔄" that has to
  be cleaned up later.

The card is a **UX hint driven by the state machine**, not a delivery path. Every
Telegram call in here is wrapped: a card that cannot be posted, edited or pinned
logs and moves on. Nothing here can block or delay :mod:`ctb.delivery.outbox`,
which is a separate object with a separate task — that separation is the whole
point of keeping the two modules apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, current_tenant, tenant_scope
from ctb.db.repo import sessions as sessions_repo
from ctb.delivery.render.html import escape, strip_html
from ctb.logging import get_logger
from ctb.turn.machine import format_duration
from ctb.turn.state import (
    Action,
    CardButton,
    CardKind,
    EditStatusCard,
    Finalize,
    PostStatusCard,
    StartTyping,
    StopTyping,
    TurnSummary,
    UpdateActivity,
)

__all__ = [
    "BUTTON_LABELS",
    "CARD_EMOJI",
    "CARD_MIN_EDIT_INTERVAL_S",
    "CARD_STALL_AFTER_S",
    "CARD_TICK_S",
    "CardSender",
    "CardState",
    "KeyboardFactory",
    "StatusCards",
    "TERMINAL_KINDS",
    "TYPING_INTERVAL_S",
    "default_keyboard",
    "render_card",
]

_log = get_logger(__name__)

# ── policy constants (PLAN §Turn presentation) ───────────────────────────────

#: "≤1 edit/3s". The floor that makes a tool-heavy turn affordable.
CARD_MIN_EDIT_INTERVAL_S: Final = 3.0
#: ``sendChatAction("typing")`` every 4s while WORKING — Telegram expires the
#: indicator after ~5s, so this is the slowest cadence that looks continuous.
TYPING_INTERVAL_S: Final = 4.0
#: "At 10 min with no new messages the card adds `stalled?` + a Check button."
CARD_STALL_AFTER_S: Final = 600.0
#: How often :meth:`StatusCards.run` re-renders live cards. The 3s edit floor,
#: not this, decides how many API calls that costs.
CARD_TICK_S: Final = 1.0
#: An error message on the card is a hint, not a log. The full text is in the
#: session row and in ``/health``.
MAX_CARD_TEXT_CHARS: Final = 300

TYPING_ACTION: Final = "typing"

#: Kinds whose card must land immediately: there is no later tick to fix them.
TERMINAL_KINDS: Final[frozenset[CardKind]] = frozenset(
    {CardKind.DONE, CardKind.ERROR, CardKind.CANCELLED, CardKind.DEAD}
)

#: Kinds whose elapsed time ticks up, so the card is re-rendered on every tick.
_LIVE_KINDS: Final[frozenset[CardKind]] = frozenset(
    {CardKind.WORKING, CardKind.STALLED}
)

CARD_EMOJI: Final[dict[CardKind, str]] = {
    CardKind.QUEUED: "⏳",
    CardKind.STARTED: "▶️",
    CardKind.WORKING: "⚙️",
    CardKind.WAKING: "⏳",
    CardKind.STALLED: "⚙️",
    CardKind.DONE: "✅",
    CardKind.ERROR: "⚠️",
    CardKind.CANCELLING: "🛑",
    CardKind.CANCELLED: "🛑",
    CardKind.DEAD: "🚫",
}

#: Fallback copy when the state machine supplies no text for a kind.
_DEFAULT_TEXT: Final[dict[CardKind, str]] = {
    CardKind.QUEUED: "queued",
    CardKind.STARTED: "started",
    CardKind.WORKING: "working",
    CardKind.WAKING: "waking",
    CardKind.STALLED: "working",
    CardKind.DONE: "done",
    CardKind.ERROR: "error",
    CardKind.CANCELLING: "stopping",
    CardKind.CANCELLED: "stopped",
    CardKind.DEAD: "session gone",
}

BUTTON_LABELS: Final[dict[CardButton, str]] = {
    CardButton.STOP: "⏹ Stop",
    CardButton.RETRY: "↻ Retry",
    CardButton.TRANSCRIPT: "📄 Transcript",
    CardButton.OPEN: "↗ Open in Conductor",
    CardButton.CHECK: "🔄 Check",
    CardButton.CLEAR_QUEUE: "🧹 Clear queue",
    CardButton.ARCHIVE: "🗄 Archive",
}

_SEPARATOR: Final = " · "
_STALLED_SEGMENT: Final = "stalled?"
_NO_PREVIEW: Final = LinkPreviewOptions(is_disabled=True)

#: A ``TelegramBadRequest`` whose message contains one of these is benign.
_NOT_MODIFIED: Final = "message is not modified"
_GONE_MARKERS: Final[tuple[str, ...]] = (
    "message to edit not found",
    "message can't be edited",
    "message identifier is not specified",
)
_ENTITY_MARKERS: Final[tuple[str, ...]] = (
    "entit",
    "start tag",
    "end tag",
    "can't parse",
    "cant parse",
)


# ── the Bot surface we use ───────────────────────────────────────────────────


class CardSender(Protocol):
    """The slice of ``aiogram.Bot`` the card needs.

    Deliberately disjoint from ``outbox.TelegramSender``: the two modules share
    a Bot at runtime but nothing at import time.
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

    async def edit_message_text(
        self,
        *,
        text: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        parse_mode: str | None = None,
        link_preview_options: LinkPreviewOptions | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Any: ...

    async def send_chat_action(
        self,
        *,
        chat_id: int,
        action: str,
        message_thread_id: int | None = None,
    ) -> Any: ...

    async def pin_chat_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        disable_notification: bool | None = None,
    ) -> Any: ...


class KeyboardFactory(Protocol):
    """Builds the card's buttons.

    Injected because callback payloads carry ``(action, id, nonce)`` with
    single-use nonces, and nonce minting belongs to the bot layer. The default
    below is a plain, nonce-free fallback that is useful on its own.
    """

    def __call__(
        self,
        buttons: tuple[CardButton, ...],
        *,
        session_id: str,
        deep_link: str | None = None,
    ) -> InlineKeyboardMarkup | None: ...


def default_keyboard(
    buttons: tuple[CardButton, ...],
    *,
    session_id: str,
    deep_link: str | None = None,
) -> InlineKeyboardMarkup | None:
    """One row per button. ``OPEN`` becomes a URL button when a deepLink exists."""
    rows: list[list[InlineKeyboardButton]] = []
    for button in buttons:
        label = BUTTON_LABELS.get(button, button.value)
        if button is CardButton.OPEN:
            if not deep_link:
                continue
            rows.append([InlineKeyboardButton(text=label, url=deep_link)])
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"card:{button.value}:{session_id}"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ── the rendered card ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CardState:
    """Everything :func:`render_card` needs. Pure data, so rendering is testable."""

    kind: CardKind = CardKind.QUEUED
    #: Copy from the state machine: ``"working 1m20s · 2 pending"``.
    text: str = ""
    #: The most recent activity line. **Plain text** — the registry guarantees
    #: ``ActivityLine.text`` is not HTML, so it is escaped here.
    activity: str = ""
    buttons: tuple[CardButton, ...] = ()
    #: Monotonic seconds when the turn started, for the live elapsed counter.
    started_at: float | None = None
    stalled: bool = False
    summary: TurnSummary | None = None


def render_card(state: CardState, *, now: float) -> str:
    """Card state → Telegram HTML. Pure; never raises on odd input."""
    segments = _segments(state, now=now)
    head = f"{CARD_EMOJI.get(state.kind, '')} {segments[0]}".strip()
    body = f"<b>{escape(head)}</b>"
    tail = [escape(segment) for segment in segments[1:] if segment]
    if tail:
        body += _SEPARATOR + _SEPARATOR.join(tail)
    return body


def _segments(state: CardState, *, now: float) -> list[str]:
    raw = state.text.strip() or _DEFAULT_TEXT.get(state.kind, state.kind.value)
    segments = [part.strip() for part in raw.split(_SEPARATOR) if part.strip()]
    if not segments:
        segments = [state.kind.value]
    if state.kind in _LIVE_KINDS and state.started_at is not None:
        # The machine froze a duration into the copy when it emitted the action;
        # re-derive it so the card ticks up between transitions.
        ms = max(0, int((now - state.started_at) * 1000))
        elapsed = f"working {format_duration(ms)}"
        if segments[0].startswith("working"):
            segments[0] = elapsed
        else:
            segments.insert(0, elapsed)
    if state.activity:
        segments.append(state.activity)
    if state.stalled or state.kind is CardKind.STALLED:
        segments.append(_STALLED_SEGMENT)
    summary = state.summary
    if summary is not None and state.kind is CardKind.DONE and summary.files_changed:
        segments.append(f"{summary.files_changed} files")
    return [segment[:MAX_CARD_TEXT_CHARS] for segment in segments]


# ── the live card ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Card:
    """One topic's card: rendering state plus the transport bookkeeping."""

    session_id: str
    chat_id: int
    thread_id: int
    state: CardState
    #: Whose card this is. The tick loop is process-wide, so persisting the
    #: card's message id needs the owning tenant's scope explicitly.
    tenant_id: uuid.UUID | None = None
    deep_link: str | None = None
    message_id: int | None = None
    typing: bool = False
    dirty: bool = True
    rendered: str = ""
    rendered_buttons: tuple[CardButton, ...] = ()
    last_edit_at: float = float("-inf")
    last_typing_at: float = float("-inf")
    last_change_at: float = 0.0
    suppressed_until: float = float("-inf")
    #: The turn is over: drop this card once its final text has landed, so the
    #: next turn posts a fresh one and the finished card stays as history.
    retire_when_clean: bool = False

    @property
    def key(self) -> tuple[int, int]:
        return (self.chat_id, self.thread_id)


class StatusCards:
    """Owns every topic's card. One background task drives them all."""

    def __init__(
        self,
        bot: CardSender,
        db: Database,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        keyboard: KeyboardFactory = default_keyboard,
        min_edit_interval: float = CARD_MIN_EDIT_INTERVAL_S,
        typing_interval: float = TYPING_INTERVAL_S,
        stall_after: float = CARD_STALL_AFTER_S,
        tick_interval: float = CARD_TICK_S,
        pin: bool = True,
    ) -> None:
        self._bot = bot
        self._db = db
        self._clock = clock
        self._sleep = sleep
        self._keyboard = keyboard
        self._min_edit_interval = max(0.0, min_edit_interval)
        self._typing_interval = max(0.1, typing_interval)
        self._stall_after = max(0.0, stall_after)
        self._tick_interval = max(0.01, tick_interval)
        self._pin = pin
        self._cards: dict[tuple[int, int], _Card] = {}
        #: Topics whose card has already been pinned once. Every turn posts a
        #: fresh card, and pinning each one makes Telegram inject a "pinned a
        #: message" service bubble per turn — a push-shaped notification that
        #: says nothing. Pin the first card in a topic and leave the pin alone.
        self._pinned: set[tuple[int, int]] = set()
        self._task: asyncio.Task[None] | None = None
        self._counters: dict[str, int] = {
            "posted": 0,
            "edits": 0,
            "coalesced": 0,
            "typing": 0,
            "errors": 0,
        }

    # -- introspection ----------------------------------------------------

    def state_for(
        self, chat_id: int, thread_id: int = NO_THREAD_ID
    ) -> CardState | None:
        card = self._cards.get((chat_id, thread_id))
        return None if card is None else card.state

    def message_id_for(self, chat_id: int, thread_id: int = NO_THREAD_ID) -> int | None:
        card = self._cards.get((chat_id, thread_id))
        return None if card is None else card.message_id

    def rendered_for(self, chat_id: int, thread_id: int = NO_THREAD_ID) -> str:
        card = self._cards.get((chat_id, thread_id))
        return "" if card is None else card.rendered

    def health(self) -> dict[str, Any]:
        return {
            **self._counters,
            "cards": len(self._cards),
            "running": self._task is not None and not self._task.done(),
        }

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="ctb-status-cards")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._sleep(self._tick_interval)

    def restore(
        self,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int = NO_THREAD_ID,
        message_id: int | None,
        state: CardState | None = None,
        deep_link: str | None = None,
    ) -> None:
        """Adopt a card that a previous incarnation posted.

        Boot re-derives the turn state from the DB; this hands the card's
        message id back so the first edit lands on it instead of posting a
        second one above it.
        """
        card = self._card(
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            deep_link=deep_link,
        )
        card.message_id = message_id
        if message_id is not None:
            # A previous incarnation already posted and pinned this topic's
            # card; adopting it must not re-pin after the next repost.
            self._pinned.add(card.key)
        if state is not None:
            card.state = state
        card.dirty = True

    # -- the machine's actions --------------------------------------------

    async def handle(
        self,
        actions: Sequence[Action],
        *,
        session_id: str,
        chat_id: int,
        thread_id: int = NO_THREAD_ID,
        deep_link: str | None = None,
    ) -> None:
        """Apply a whole batch of actions, then flush **once**.

        Flushing per batch rather than per action is what makes the card cheap:
        the machine routinely emits an ``EditStatusCard`` and a ``Finalize``
        together, and that must cost one API call, not two.
        """
        touched = False
        finalized = False
        for action in actions:
            touched |= await self._apply(
                action,
                session_id=session_id,
                chat_id=chat_id,
                thread_id=thread_id,
                deep_link=deep_link,
            )
            finalized |= isinstance(action, Finalize)
        if not touched:
            return
        card = self._cards.get((chat_id, thread_id))
        if card is None:
            return
        # DEAD is terminal for the whole session; an ERROR card is not — the
        # machine keeps editing it, and retiring would make the next edit post a
        # duplicate card above it.
        card.retire_when_clean |= finalized or card.state.kind is CardKind.DEAD
        await self._flush(card, force=card.state.kind in TERMINAL_KINDS)
        await self._maybe_retire(card)

    async def apply(
        self,
        action: Action,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int = NO_THREAD_ID,
        deep_link: str | None = None,
    ) -> None:
        """Single-action convenience. Same semantics as :meth:`handle`."""
        await self.handle(
            (action,),
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            deep_link=deep_link,
        )

    async def _apply(
        self,
        action: Action,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int,
        deep_link: str | None,
    ) -> bool:
        """Mutate card state. Returns whether the action was ours."""
        now = self._clock()
        match action:
            case PostStatusCard(kind=kind, text=text, buttons=buttons):
                card = self._card(
                    session_id=session_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    deep_link=deep_link,
                )
                # A PostStatusCard means a new turn: drop the previous card's
                # id so the finished one stays in the chat as history.
                previous = card.state.kind
                card.message_id = None
                card.retire_when_clean = False
                card.state = CardState(kind=kind, text=text, buttons=buttons)
                self._on_kind(card, kind, now, previous=previous)
                card.dirty = True
                return True
            case EditStatusCard(
                kind=kind, text=text, buttons=buttons, activity=activity
            ):
                card = self._card(
                    session_id=session_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    deep_link=deep_link,
                )
                previous = card.state.kind
                card.state = replace(
                    card.state,
                    kind=kind,
                    text=text,
                    buttons=buttons,
                    activity=activity if activity is not None else card.state.activity,
                )
                self._on_kind(card, kind, now, previous=previous)
                if activity:
                    card.last_change_at = now
                card.dirty = True
                return True
            case UpdateActivity(activity=activity):
                card = self._cards.get((chat_id, thread_id))
                if card is None:
                    return False
                card.state = replace(card.state, activity=activity)
                card.last_change_at = now
                card.dirty = True
                return True
            case StartTyping():
                card = self._card(
                    session_id=session_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    deep_link=deep_link,
                )
                card.typing = True
                await self._send_typing(card, now)
                return True
            case StopTyping():
                card = self._cards.get((chat_id, thread_id))
                if card is not None:
                    card.typing = False
                return True
            case Finalize(summary=summary):
                card = self._card(
                    session_id=session_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    deep_link=deep_link,
                )
                card.state = replace(
                    card.state,
                    summary=summary,
                    stalled=False,
                    activity="",
                )
                card.typing = False
                card.dirty = True
                return True
            case _:
                return False

    def _on_kind(
        self, card: _Card, kind: CardKind, now: float, *, previous: CardKind
    ) -> None:
        """Keep the elapsed-time anchor and the stall clock honest."""
        if kind is CardKind.QUEUED:
            card.state = replace(card.state, started_at=None, stalled=False)
        elif kind in (CardKind.STARTED, CardKind.WORKING) and (
            card.state.started_at is None
        ):
            card.state = replace(card.state, started_at=now)
        if kind is not CardKind.STALLED and kind is not previous:
            card.state = replace(card.state, stalled=False)
        if kind is not previous:
            card.last_change_at = now

    async def _maybe_retire(self, card: _Card) -> None:
        """Drop a finished card once its final text has actually landed.

        While it is still dirty the flush was suppressed (a 429), so the card
        stays live and the next tick tries again — a "done" card that never
        arrived is worse than one that arrives three seconds late.
        """
        if not card.retire_when_clean or card.dirty:
            return
        self._cards.pop(card.key, None)
        card.typing = False
        await self._persist_card_id(card, None)

    # -- the periodic pass ------------------------------------------------

    async def tick(self) -> None:
        """One pass over every live card. Never raises."""
        now = self._clock()
        for card in list(self._cards.values()):
            try:
                if card.typing and (now - card.last_typing_at) >= (
                    self._typing_interval
                ):
                    await self._send_typing(card, now)
                if (
                    card.state.kind is CardKind.WORKING
                    and not card.state.stalled
                    and self._stall_after > 0
                    and (now - card.last_change_at) >= self._stall_after
                ):
                    card.state = replace(
                        card.state,
                        stalled=True,
                        buttons=_with_check(card.state.buttons),
                    )
                    card.dirty = True
                if card.state.kind in _LIVE_KINDS:
                    # Cheap: _flush suppresses the API call when the rendered
                    # text is unchanged, and the 3s floor caps it regardless.
                    card.dirty = True
                await self._flush(card, force=card.retire_when_clean)
                await self._maybe_retire(card)
            except Exception as exc:  # a card must never take the task down
                self._counters["errors"] += 1
                _log.warning(
                    "status_card.tick_failed",
                    chat_id=card.chat_id,
                    thread_id=card.thread_id,
                    error=repr(exc),
                )

    # -- transport --------------------------------------------------------

    def _card(
        self,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int,
        deep_link: str | None,
    ) -> _Card:
        key = (chat_id, thread_id)
        card = self._cards.get(key)
        if card is None:
            card = _Card(
                session_id=session_id,
                chat_id=chat_id,
                thread_id=thread_id,
                state=CardState(),
                # Captured here, where a scope exists: `handle` runs inside the
                # poller's tenant, `tick` does not.
                tenant_id=current_tenant(),
                deep_link=deep_link,
                last_change_at=self._clock(),
            )
            self._cards[key] = card
        else:
            card.session_id = session_id
            card.tenant_id = card.tenant_id or current_tenant()
            if deep_link is not None:
                card.deep_link = deep_link
        return card

    async def _flush(self, card: _Card, *, force: bool = False) -> None:
        """Post or edit if something actually changed and the floor allows it."""
        if not card.dirty:
            return
        now = self._clock()
        if now < card.suppressed_until:
            # A Telegram 429 is never overridden, not even for a final card.
            # It stays dirty and the next tick tries again.
            return
        rendered = render_card(card.state, now=now)
        if (
            card.message_id is not None
            and rendered == card.rendered
            and card.state.buttons == card.rendered_buttons
        ):
            card.dirty = False
            return
        if card.message_id is None:
            await self._post(card, rendered, now)
            return
        if not force and (now - card.last_edit_at) < self._min_edit_interval:
            # Coalesce: leave it dirty, the next tick sends the *latest* text.
            self._counters["coalesced"] += 1
            return
        await self._edit(card, rendered, now)

    async def _post(self, card: _Card, rendered: str, now: float) -> None:
        markup = self._markup(card)
        try:
            result = await self._bot.send_message(
                chat_id=card.chat_id,
                text=rendered,
                message_thread_id=card.thread_id or None,
                parse_mode="HTML",
                link_preview_options=_NO_PREVIEW,
                disable_notification=True,
                reply_markup=markup,
            )
        except TelegramRetryAfter as exc:
            self._back_off(card, exc)
            return
        except Exception as exc:
            self._fail(card, "post", exc)
            return
        message_id = getattr(result, "message_id", None)
        card.message_id = message_id if isinstance(message_id, int) else None
        card.rendered = rendered
        card.rendered_buttons = card.state.buttons
        card.last_edit_at = now
        card.dirty = False
        self._counters["posted"] += 1
        if card.message_id is not None:
            await self._pin_card(card)
            await self._persist_card_id(card, card.message_id)

    async def _edit(self, card: _Card, rendered: str, now: float) -> None:
        markup = self._markup(card)
        try:
            await self._bot.edit_message_text(
                text=rendered,
                chat_id=card.chat_id,
                message_id=card.message_id,
                parse_mode="HTML",
                link_preview_options=_NO_PREVIEW,
                reply_markup=markup,
            )
        except TelegramRetryAfter as exc:
            self._back_off(card, exc)
            return
        except TelegramBadRequest as exc:
            message = str(exc.message or exc).lower()
            if _NOT_MODIFIED in message:
                card.rendered = rendered
                card.rendered_buttons = card.state.buttons
                card.last_edit_at = now
                card.dirty = False
                return
            if any(marker in message for marker in _GONE_MARKERS):
                # Someone deleted the card. Post a new one on the next flush.
                card.message_id = None
                card.dirty = True
                return
            if any(marker in message for marker in _ENTITY_MARKERS):
                # Same belt and braces as the outbox: one unformatted retry.
                await self._edit_plain(card, rendered, now)
                return
            self._fail(card, "edit", exc)
            return
        except Exception as exc:
            self._fail(card, "edit", exc)
            return
        card.rendered = rendered
        card.rendered_buttons = card.state.buttons
        card.last_edit_at = now
        card.dirty = False
        self._counters["edits"] += 1

    async def _edit_plain(self, card: _Card, rendered: str, now: float) -> None:
        """The one unformatted retry. Not itself retried."""
        try:
            await self._bot.edit_message_text(
                text=strip_html(rendered),
                chat_id=card.chat_id,
                message_id=card.message_id,
                parse_mode=None,
                link_preview_options=_NO_PREVIEW,
                reply_markup=self._markup(card),
            )
        except Exception as exc:
            self._fail(card, "edit_plain", exc)
            return
        card.rendered = rendered
        card.rendered_buttons = card.state.buttons
        card.last_edit_at = now
        card.dirty = False
        self._counters["edits"] += 1

    async def _send_typing(self, card: _Card, now: float) -> None:
        card.last_typing_at = now
        try:
            await self._bot.send_chat_action(
                chat_id=card.chat_id,
                action=TYPING_ACTION,
                message_thread_id=card.thread_id or None,
            )
        except Exception as exc:
            self._fail(card, "typing", exc)
            return
        self._counters["typing"] += 1

    async def _pin_card(self, card: _Card) -> None:
        if not self._pin or card.message_id is None:
            return
        if card.key in self._pinned:
            return
        # Marked before the call, not after: a chat where we lack the pin
        # permission must not re-ask on every turn.
        self._pinned.add(card.key)
        try:
            await self._bot.pin_chat_message(
                chat_id=card.chat_id,
                message_id=card.message_id,
                disable_notification=True,
            )
        except Exception as exc:
            # Pinning needs a permission we may not have. The card still works.
            _log.info("status_card.pin_failed", chat_id=card.chat_id, error=repr(exc))

    async def _persist_card_id(self, card: _Card, message_id: int | None) -> None:
        """Remember (or forget) which message is this session's card.

        Runs from the process-wide tick loop, so it enters the card's own
        tenant scope. Without that the write is rejected and the id goes stale
        across a redeploy — the card is then edited into a message that is no
        longer the newest thing in the topic.
        """
        tenant_id = card.tenant_id or current_tenant()
        if tenant_id is None:
            _log.warning("status_card.unscoped", session_id=card.session_id)
            return
        try:
            async with tenant_scope(tenant_id):
                await sessions_repo.set_status_card(
                    self._db, card.session_id, message_id
                )
        except Exception as exc:
            _log.warning(
                "status_card.persist_failed",
                session_id=card.session_id,
                error=repr(exc),
            )

    def _markup(self, card: _Card) -> InlineKeyboardMarkup | None:
        try:
            return self._keyboard(
                card.state.buttons,
                session_id=card.session_id,
                deep_link=card.deep_link,
            )
        except Exception as exc:
            _log.warning("status_card.keyboard_failed", error=repr(exc))
            return None

    def _back_off(self, card: _Card, exc: TelegramRetryAfter) -> None:
        card.suppressed_until = self._clock() + max(float(exc.retry_after), 0.0)
        card.dirty = True
        self._counters["errors"] += 1
        _log.warning(
            "status_card.rate_limited",
            chat_id=card.chat_id,
            retry_after=exc.retry_after,
        )

    def _fail(self, card: _Card, what: str, exc: BaseException) -> None:
        card.dirty = False
        self._counters["errors"] += 1
        _log.warning(
            "status_card.failed",
            op=what,
            chat_id=card.chat_id,
            thread_id=card.thread_id,
            error=repr(exc)[:500],
        )


def _with_check(buttons: tuple[CardButton, ...]) -> tuple[CardButton, ...]:
    return buttons if CardButton.CHECK in buttons else (*buttons, CardButton.CHECK)
