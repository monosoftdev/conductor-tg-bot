"""Forum-topic lifecycle, and the small chat plumbing every handler shares.

One topic per **session** — **in a group and in a DM alike**. The address of a
prompt is the topic your thumb is in (PLAN §Chat model), so the topic is created
*instantly* — before the workspace exists — and adopted by the session id once
``POST /v0/workspaces`` returns. A workspace is a *group of rooms*: several
Conductor chats over one container, one branch and one checkout, each with its
own Telegram topic.

A session gets its room the first time it is **opened**, not when it is created.
``/new`` and ``/fork`` open one immediately because both are explicit acts;
everything else — the other sessions of an adopted workspace — materialises one
lazily through :func:`ensure_topic`, so ``/attach`` on a nine-session workspace
does not open nine rooms nobody asked for.

A bot needs no admin rights and no Premium to open a topic in a private chat;
the one precondition is @BotFather's *Threaded Mode*, which ``getMe`` reports as
``has_topics_enabled`` (:func:`dm_topic_support`). But whether DM topics work is
a **runtime fact, not a config flag** — the Bot API 10.0 rollout has been
observed refusing ``createForumTopic`` and thread-addressed ``sendMessage`` in
private chats — so every DM-topic path degrades to the linear single-seat DM
instead of dead-ending. :func:`send_html` re-sends without the thread when
Telegram says the thread is gone, exactly as the outbox reroutes a delivery.

**Renamed only on state transitions, never on a timer** (PLAN §Chat model). A
rename is a Telegram API call; a 5-second init card that renamed the topic every
tick would spend the whole flood budget on cosmetics. The last applied prefix
lives in ``sessions.topic_marker``, and :func:`apply_marker` is a no-op when
the marker has not actually changed — that check is the rule, in code.

The name is ``<marker> <task> · <project>/<branch>`` — a **state prefix** that
changes, and a **label** that never does. :func:`topic_label` builds the label
once, at creation, from the opening prompt; :func:`apply_marker` only ever
touches the prefix. Prefixes come from :mod:`ctb.signals`, so the topic list,
the status card and ``/board`` all say the same thing.

A topic has **two** state channels and both move on every transition: the name
prefix, and ``icon_custom_emoji_id`` — the round badge Telegram draws beside the
row, which is what you actually scan a long list by. (``icon_color`` is the
third and is fixed at creation, so it can only ever mean *which workspace*.)

| Conductor state          | Topic name                       | Icon |
|--------------------------|----------------------------------|------|
| workspace initializing   | ``⏳ fix login · api/main``      | ⌛ |
| ready + session idle     | ``fix login · api/main``         | 💭 |
| session working          | ``⚙️ fix login · api/main``      | ⚡ |
| turn finished, unread    | ``✅ fix login · api/main``      | ✅ |
| session error            | ``⚠️ fix login · api/main``      | ❗ |
| workspace sleeping       | ``💤 fix login · api/main``      | 💤 |
| archived / deleted       | topic deleted, else ``🗄 fix login``, closed | 🏁 |

The icons are *requests*: Telegram serves bots a fixed pack and silently keeps
the current icon for a request it cannot honour, so each state names several
acceptable emoji and :func:`topic_icon_id` takes the first the pack carries.
``scripts/probe_topic_icons.py`` prints what a live token is actually offered.

The task leads because Telegram clips a topic row from the right and a phone
shows perhaps thirty characters of it. A branch is nearly always ``main``, so
``proj/branch`` alone made every workspace on one repo look identical — same
name, same colour (it is a hash of the label), same state icon.

**There is no bot-managed "General" topic.** Every topic here belongs to one
workspace. The always-present seat is the chat root — Telegram's own General in
a forum, the plain conversation in a DM — and it is addressed as
``NO_THREAD_ID``, never created and never renamed.

This module has no router. It is imported by the handlers that do.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup

from ctb.bot.middleware.tenancy import TenantContext
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import SessionStatusValue, WorkspaceStatusValue
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, get_database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.sessions import SessionRow
from ctb.db.repo.workspaces import WorkspaceRow
from ctb.delivery.outbox import THREAD_GONE_MARKERS, is_entity_error
from ctb.delivery.render.html import escape, strip_html
from ctb.logging import get_logger
from ctb.turn.state import TopicMarker, TurnState

__all__ = [
    "ROOM_GONE_NOTICE",
    "TOPIC_NAME_LIMIT",
    "TOPIC_ICON_COLORS",
    "Claim",
    "ForumSupport",
    "TopicCreateError",
    "TopicRetirement",
    "apply_marker",
    "attach_topic",
    "room_gone",
    "session_marker",
    "claim_topic",
    "create_topic",
    "discard_topic",
    "dm_topic_support",
    "edit_html",
    "ensure_topic",
    "forum_support",
    "human_name",
    "icon_key",
    "icon_pack",
    "jump_url",
    "marker_for",
    "require_topic",
    "resolve_client",
    "room_label",
    "resolve_db",
    "retire_topic",
    "send_html",
    "telegram_reason",
    "topic_label",
    "workspace_family",
    "topic_icon_color",
    "topic_icon_id",
    "topic_title",
]

log = get_logger(__name__)

#: What a command reply may carry. Almost always inline buttons; the launcher
#: keyboard (:func:`ctb.bot.keyboards.home_keyboard`) is the one reply keyboard
#: in the bot and rides on the same send path as everything else.
type Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | None

#: Telegram's own cap on a forum topic name.
TOPIC_NAME_LIMIT: Final = 128
#: Interactive command replies retry briefly. Durable transcript output uses
#: the outbox's unbounded recovery path instead.
COMMAND_SEND_ATTEMPTS: Final = 3
COMMAND_RETRY_AFTER_CAP_S: Final = 5.0
#: Telegram's complete allowed palette for regular forum-topic icons.
TOPIC_ICON_COLORS: Final[tuple[int, ...]] = (
    0x6FB9F0,
    0xFFD67E,
    0xCB86DB,
    0x8EEE98,
    0xFF93B2,
    0xFB6F5F,
)

#: aiogram wraps every API error as "Telegram server says - Bad Request: <why>".
#: Only ``<why>`` is worth a phone line; the rest is the same on every failure.
_TELEGRAM_NOISE: Final = re.compile(
    r"^\s*(?:telegram\s+server\s+says\s*[-:]?\s*)?"
    r"(?:(?:bad\s+request|forbidden|unauthorized|conflict|not\s+found"
    r"|too\s+many\s+requests)\s*:\s*)?",
    re.IGNORECASE,
)

#: Turn states that mean "this session is busy right now".
_BUSY_STATES: Final[frozenset[TurnState]] = frozenset(
    {
        TurnState.SUBMIT_PENDING,
        TurnState.QUEUED,
        TurnState.WORKING,
        TurnState.DRAINING,
        TurnState.CANCELLING,
    }
)


# ── injection seams ──────────────────────────────────────────────────────────


def resolve_db(db: Database | None) -> Database:
    """The handler's ``db`` kwarg, or the process-wide one.

    ``create_dispatcher`` puts whatever it was given into the workflow data,
    including ``None`` when the caller relied on the global. Handlers declare
    ``db: Database | None`` and funnel through here rather than each inventing
    a fallback.
    """
    return db if db is not None else get_database()


def resolve_client(
    client: ConductorClient | None = None,
    tenant: TenantContext | None = None,
) -> ConductorClient:
    """The *tenant's* Conductor client. There is no process-wide fallback.

    Deleting the global was the point: a handler that forgets to take
    ``tenant`` now fails by name here instead of quietly reading another
    organisation's data with the wrong API key.
    """
    if tenant is not None:
        return tenant.client
    if client is not None:
        return client
    raise RuntimeError(
        "no Conductor client in scope; the handler must take `tenant: TenantContext`"
    )


# ── naming ───────────────────────────────────────────────────────────────────


#: How much of the opening prompt becomes the topic's name. Wide enough for a
#: recognisable phrase, short enough that ``proj/branch`` after it still lands
#: inside the ~30 characters a phone shows of a topic row.
TASK_HINT_CHARS: Final = 28

_WHITESPACE: Final = re.compile(r"\s+")
#: Openers that are true of every prompt and identify none of them.
_HINT_NOISE: Final = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|i\s+want\s+(?:you\s+)?to\s+"
    r"|i\s+need\s+(?:you\s+)?to\s+|let'?s\s+|help\s+me\s+|hey\s+|hi\s+|ok(?:ay)?[,\s]+)+",
    re.IGNORECASE,
)
#: Words that carry nothing on their own, so a clip ending on one reads as a
#: sentence cut off rather than a name.
_DANGLING: Final[frozenset[str]] = frozenset(
    {
        "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
        "of", "on", "or", "so", "that", "the", "then", "to", "when", "where",
        "which", "while", "with", "without",
    }
)  # fmt: skip


def task_hint(prompt: str | None) -> str:
    """A few words of the opening prompt — what makes one topic recognisable.

    Not the running prompt: this is taken **once**, from the first one, and
    never changes afterwards. A name that moved with the conversation would
    make the topic list unlearnable and cost a rename per turn.
    """
    lines = (prompt or "").strip().splitlines()
    if not lines:
        return ""
    text = _WHITESPACE.sub(" ", lines[0].strip())
    text = _HINT_NOISE.sub("", text).strip(" .,:;-—")
    if len(text) <= TASK_HINT_CHARS:
        return text
    clipped = text[:TASK_HINT_CHARS]
    if not text[TASK_HINT_CHARS].isspace():
        # The cut landed inside a word. Retreat to the boundary — but never
        # give back a stub: "implement" beats "i".
        head, sep, _ = clipped.rpartition(" ")
        if sep and len(head) >= TASK_HINT_CHARS // 2:
            clipped = head
    # A clip that lands on "the" or "where" reads like a transmission cutting
    # out. Dropping the dangling word costs a few characters and buys a phrase.
    words = clipped.split()
    while len(words) > 1 and words[-1].casefold() in _DANGLING:
        words.pop()
    return " ".join(words).rstrip(" .,")


#: ``tg-<chatid>-<nonce>``, the name we give Conductor so an ambiguous create
#: can be reconciled by listing (see :func:`ctb.turn.cursor.workspace_name`).
#: It is bookkeeping, and it must never reach a person.
_INTERNAL_NAME: Final = re.compile(r"^tg-\d+-[A-Za-z0-9_-]{4,}$")


def human_name(name: str | None) -> str:
    """``name``, unless it is the reconciliation key we invented for Conductor.

    Every workspace this bot creates is called ``tg-100200300-iszvwjeb`` on the
    Conductor side, and that string was being rendered straight into buttons
    (``+ Open tg-100200300-iszvwjeb``) and, through adoption, into topic titles.
    Empty here means "you have nothing worth showing — use a fallback".
    """
    text = (name or "").strip()
    return "" if not text or _INTERNAL_NAME.match(text) else text


def topic_label(
    project: str | None, branch: str | None, *, task: str | None = None
) -> str:
    """What tells one topic from another, marker excluded.

    **The task comes first.** Telegram clips a topic row from the right and a
    phone shows perhaps thirty characters of it, so whatever identifies the
    workspace has to be in front. It used to be ``proj/branch`` alone — and
    since a branch is almost always ``main``, three workspaces on one repo were
    three rows reading ``acme-api/main``, in the same colour, with the same
    icon. The list could not answer the one question you ask it.

    With no task — an adopted workspace, a hand rename — it falls back to
    ``proj/branch``, which is what it always was.
    """
    where = (project or "workspace").strip() or "workspace"
    branch_part = (branch or "").strip()
    if branch_part:
        where = f"{where}/{branch_part}"
    hint = task_hint(task)
    label = f"{hint} · {where}" if hint else where
    return label[:TOPIC_NAME_LIMIT]


def workspace_family(row: WorkspaceRow | None) -> str:
    """``acme-api/main`` — what every room of one workspace has in common.

    Written to ``workspaces.topic_name`` at create time and kept there after the
    room moved to the session, because it is still two things: what ``/board``
    stage 1 calls this workspace, and the key every one of its rooms hashes its
    colour from, so they read as a family in the topic list.
    """
    if row is None:
        return topic_label(None, None)
    return human_name(row.topic_name) or topic_label(
        human_name(row.name) or row.id[:8], row.branch
    )


def room_label(family: str | None, task: str | None) -> str:
    """``fix flaky login · acme-api/main`` — one room inside a known workspace.

    :func:`topic_label` composed the other way round: the workspace part is
    already rendered, so it goes in whole rather than being rebuilt from a
    project and a branch that would end up appended twice.
    """
    return topic_label(family or None, None, task=task)


def topic_title(marker: TopicMarker, label: str) -> str:
    """Prefix + label, clipped to Telegram's 128-character topic name limit."""
    prefix = marker.prefix
    room = TOPIC_NAME_LIMIT - len(prefix)
    return f"{prefix}{label[:room]}"


def topic_icon_color(label: str) -> int:
    """Stable visual *identity*: the same project/branch keeps the same color.

    Deliberately not a state signal — ``icon_color`` cannot be changed after
    the topic is created, at any API level. State lives on the prefix and on
    :func:`topic_icon_id`.
    """
    digest = hashlib.sha256(label.casefold().encode("utf-8")).digest()
    return TOPIC_ICON_COLORS[int.from_bytes(digest[:2], "big") % len(TOPIC_ICON_COLORS)]


#: Emoji → custom-emoji id for the topic-icon pack, fetched once per process.
#: Telegram serves bots a fixed set (``getForumTopicIconStickers``) and refuses
#: anything outside it, so the ids cannot be hard-coded from documentation —
#: they have to be asked for. Keys are :func:`icon_key`-normalised.
_ICON_IDS: dict[str, str] = {}
_ICON_LOCK = asyncio.Lock()

#: U+FE0F and U+FE0E ask for the emoji or text *presentation* of a code point.
#: They carry no identity, and the two sides disagree about them constantly:
#: the state table says ``⚙`` where Telegram's pack serves ``⚙️``. An exact
#: string lookup missed every such pair, returned ``None``, and — because
#: aiogram omits an unset optional and Telegram keeps the existing value for an
#: omitted field — left the icon exactly as it was. Every rename looked like it
#: worked and the icon never moved.
_PRESENTATION_SELECTORS: Final[dict[int, int | None]] = {0xFE0E: None, 0xFE0F: None}


def icon_key(emoji: str) -> str:
    """Compare emoji by identity, not by presentation."""
    return emoji.translate(_PRESENTATION_SELECTORS)


async def icon_pack(bot: Bot) -> dict[str, str]:
    """``{emoji: custom_emoji_id}`` for every icon Telegram will accept.

    Fetched once per process and never invalidated — the pack is a Telegram
    constant, not per-chat state. An empty dict means "we could not ask", and
    the next call tries again; it is one API call, and getting the icons after
    a transient failure is worth more than remembering the failure.
    """
    if _ICON_IDS:
        return _ICON_IDS
    async with _ICON_LOCK:
        if _ICON_IDS:  # another caller may have filled it while we waited
            return _ICON_IDS
        try:
            # The parse is inside the try with the call, deliberately. Every
            # field here is optional in the schema and the whole result is
            # untyped at the wire, so a shape we did not expect is exactly as
            # survivable as a network error — and this decorates a rename that
            # has to happen either way. A stale topic title is a lie; a missing
            # icon is a missing icon.
            for sticker in await bot.get_forum_topic_icon_stickers():
                emoji = getattr(sticker, "emoji", None)
                sticker_id = getattr(sticker, "custom_emoji_id", None)
                if emoji and sticker_id:
                    _ICON_IDS.setdefault(icon_key(emoji), sticker_id)
        except Exception as exc:  # noqa: BLE001 - never propagate; see above
            log.warning("topics.icon_pack_unavailable", error=repr(exc))
            _ICON_IDS.clear()
            return {}
        # Logged because the pack is the one input to this that nobody can read
        # from the source. If a marker later resolves to nothing, this line is
        # what says whether the pack was small or the request was wrong.
        log.info("topics.icon_pack_loaded", icons=len(_ICON_IDS))
    return _ICON_IDS


async def topic_icon_id(bot: Bot, marker: TopicMarker) -> str | None:
    """The custom-emoji id for this state's icon, or ``None`` to leave it alone.

    Walks :attr:`TopicMarker.icons` in order and takes the first the pack
    carries, so one absent emoji costs a fallback rather than the whole icon.

    Never raises and never blocks a rename: with no pack and no match the
    caller renames without touching the icon. A missing icon is cosmetic; a
    failed rename is a topic that lies.
    """
    wanted = marker.icons
    if not wanted:
        return None
    pack = await icon_pack(bot)
    if not pack:
        return None
    for emoji in wanted:
        found = pack.get(icon_key(emoji))
        if found is not None:
            return found
    # Not a failure worth a rename, but worth saying out loud: this state will
    # keep whatever icon the topic already had, for every workspace, forever.
    log.warning(
        "topics.icon_unavailable",
        marker=marker.value,
        wanted=list(wanted),
        pack=len(pack),
    )
    return None


def marker_for(
    *,
    workspace_status: WorkspaceStatusValue | None = None,
    turn_state: TurnState | None = None,
    session_status: SessionStatusValue | None = None,
) -> TopicMarker:
    """The one place the naming table above is encoded.

    Workspace lifecycle wins over session state: a sleeping workspace cannot be
    working, whatever a stale turn row says.
    """
    if workspace_status is not None:
        if workspace_status.is_gone:
            return TopicMarker.ARCHIVED
        if workspace_status in (
            WorkspaceStatusValue.INITIALIZING,
            WorkspaceStatusValue.UPDATING,
        ):
            return TopicMarker.INITIALIZING
        if workspace_status is WorkspaceStatusValue.SLEEPING:
            return TopicMarker.SLEEPING
    if turn_state is TurnState.DEAD:
        return TopicMarker.ARCHIVED
    if turn_state is TurnState.ERROR or session_status is SessionStatusValue.ERROR:
        return TopicMarker.ERROR
    if turn_state in _BUSY_STATES or turn_state is TurnState.WAKING:
        return TopicMarker.WORKING
    return TopicMarker.IDLE


def jump_url(chat_id: int, topic_id: int | None) -> str | None:
    """``https://t.me/c/<internal>/<topic>`` — /board's "tap to jump".

    Only supergroups (``-100…``) have the ``/c/`` form; a DM has nothing to
    jump to — not even a DM *topic*, which Telegram publishes no link syntax
    for — and neither has a chat we have no topic for.
    """
    text = str(chat_id)
    if not text.startswith("-100"):
        return None
    internal = text[4:]
    if not internal.isdigit():
        return None
    if topic_id is None or topic_id == NO_THREAD_ID:
        return f"https://t.me/c/{internal}"
    return f"https://t.me/c/{internal}/{topic_id}"


# ── sending, with the mandatory HTML fallback ────────────────────────────────


def thread_is_gone(exc: BaseException) -> bool:
    """Telegram refused the thread itself, not the message in it.

    The same markers the outbox reroutes on. A DM thread can be refused by a
    client that predates threaded mode, by the Bot API 10.0 regression, or
    because the topic was deleted — all of which mean *send it linearly*, never
    *drop it*.
    """
    text = str(exc).casefold()
    return any(marker in text for marker in THREAD_GONE_MARKERS)


async def send_html(
    bot: Bot,
    chat_id: int,
    html: str,
    *,
    thread_id: int = NO_THREAD_ID,
    reply_markup: Markup = None,
    silent: bool = False,
    reply_to_message_id: int | None = None,
) -> Message | None:
    """Send one interactive reply. Never raises; returns ``None`` on failure.

    Command replies are small, immediate and few, so they do not go through the
    ``deliveries`` outbox — but they honour the same rules as everything else:

    * an entity-parse ``TelegramBadRequest`` is retried exactly once with
      ``parse_mode=None`` (CLAUDE.md) — a reply may look ugly, never be lost;
    * a *thread* Telegram will not accept is retried once without it. A topic
      that stops working must leave a working chat behind, so the reply lands
      in the chat root rather than nowhere.
    """
    sent, thread_gone = await _send_attempt(
        bot,
        chat_id,
        html,
        thread_id=thread_id,
        reply_markup=reply_markup,
        silent=silent,
        reply_to_message_id=reply_to_message_id,
    )
    if sent is not None or not thread_gone or thread_id == NO_THREAD_ID:
        return sent
    log.warning("topics.thread_gone", chat_id=chat_id, thread_id=thread_id)
    # The reply-to target lived in that thread too; asking for it again is a
    # second way to fail at the one thing left to get right.
    sent, _ = await _send_attempt(
        bot,
        chat_id,
        html,
        thread_id=NO_THREAD_ID,
        reply_markup=reply_markup,
        silent=silent,
        reply_to_message_id=None,
    )
    return sent


async def _send_attempt(
    bot: Bot,
    chat_id: int,
    html: str,
    *,
    thread_id: int,
    reply_markup: Markup,
    silent: bool,
    reply_to_message_id: int | None,
) -> tuple[Message | None, bool]:
    """One addressed send. ``(message, thread_is_gone)`` — never raises."""
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_thread_id": thread_id or None,
        "reply_markup": reply_markup,
        "disable_notification": silent or None,
    }
    if reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id

    async def send(text: str, *, plain: bool = False) -> Message:
        for attempt in range(COMMAND_SEND_ATTEMPTS):
            try:
                if plain:
                    return await bot.send_message(text=text, parse_mode=None, **kwargs)
                return await bot.send_message(text=text, **kwargs)
            except TelegramRetryAfter as exc:
                if attempt + 1 >= COMMAND_SEND_ATTEMPTS:
                    raise
                await asyncio.sleep(
                    min(max(float(exc.retry_after), 0.0), COMMAND_RETRY_AFTER_CAP_S)
                    + 0.1
                )
            except (TelegramNetworkError, TelegramServerError):
                if attempt + 1 >= COMMAND_SEND_ATTEMPTS:
                    raise
                await asyncio.sleep(0.2 * (2**attempt))
        raise RuntimeError("unreachable command-send retry state")

    try:
        return await send(html), False
    except TelegramBadRequest as exc:
        if not is_entity_error(exc):
            log.warning("topics.send_failed", chat_id=chat_id, error=str(exc))
            return None, thread_is_gone(exc)
        try:
            return await send(strip_html(html), plain=True), False
        except TelegramAPIError as retry_exc:
            log.warning(
                "topics.send_retry_failed", chat_id=chat_id, error=str(retry_exc)
            )
            return None, thread_is_gone(retry_exc)
    except TelegramAPIError as exc:
        log.warning("topics.send_failed", chat_id=chat_id, error=str(exc))
        return None, thread_is_gone(exc)


async def edit_html(
    bot: Bot,
    chat_id: int,
    message_id: int,
    html: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit a card in place. ``False`` means "the card is gone, post a new one".

    "message is not modified" is a success: the card already says this.
    """
    try:
        await bot.edit_message_text(
            text=html,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if "not modified" in message:
            return True
        if is_entity_error(exc):
            try:
                await bot.edit_message_text(
                    text=strip_html(html),
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode=None,
                    reply_markup=reply_markup,
                )
                return True
            except TelegramAPIError:
                return False
        log.debug("topics.edit_failed", chat_id=chat_id, error=str(exc))
        return False
    except TelegramAPIError as exc:
        log.debug("topics.edit_failed", chat_id=chat_id, error=str(exc))
        return False


# ── topic lifecycle ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ForumSupport:
    """Can this chat host one topic per workspace?"""

    ok: bool
    #: Machine-readable: ``ok`` · ``dm`` · ``not_forum`` · ``no_permission`` ·
    #: ``threads_off`` · ``unknown``.
    reason: str = "ok"
    detail: str = ""

    @property
    def degraded(self) -> bool:
        return not self.ok


async def forum_support(bot: Bot, chat_id: int) -> ForumSupport:
    """Check the two things a *group* topic hinges on: forum + permission.

    A private chat is not this question — a bot needs no rights there. DMs go
    through :func:`dm_topic_support` instead, and are reported degraded here so
    ``/setup``'s group probe keeps refusing to run in one.
    """
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramAPIError as exc:
        return ForumSupport(False, "unknown", str(exc))
    if chat.type == "private":
        return ForumSupport(False, "dm", "private chat")
    if not chat.is_forum:
        return ForumSupport(False, "not_forum", "forum topics are off")
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError as exc:
        return ForumSupport(False, "unknown", str(exc))
    if not bool(getattr(member, "can_manage_topics", False)):
        return ForumSupport(False, "no_permission", "bot cannot manage topics")
    return ForumSupport(True)


async def dm_topic_support(bot: Bot) -> ForumSupport:
    """Can this bot open a topic *inside a private chat*?

    One precondition, and it is about the bot rather than the chat: @BotFather's
    *Threaded Mode*, reported by ``getMe`` as ``has_topics_enabled``. No admin
    rights, no Premium, no per-chat state — which is why this takes no
    ``chat_id``. (The sibling toggle, "disallow users to create new threads",
    governs the *user*; ``BOT_FORUM_CREATE_FORBIDDEN`` is never about us.)

    Three answers, two of which are "go ahead":

    * ``True``  — threaded mode is on.
    * ``None``  — absent. Telegram omits false optionals, so this is *usually*
      "off" — and it is deliberately still tried. An unknown is not a refusal:
      ``createForumTopic`` is the only real proof, the caller degrades on it
      anyway, and the price of the ambiguity is one refused call per ``/new``
      rather than a feature that can never turn itself on.
    * ``False`` — explicitly off. Skip the doomed create.

    Deliberately not cached. It is one cheap call on a path that already makes
    several, and caching would make flipping the @BotFather toggle need a
    redeploy. Every failure — including a bot object that has no ``getMe`` —
    answers "no", because the fallback is a chat that still works.
    """
    try:
        me = await bot.get_me()
    except Exception as exc:  # noqa: BLE001 - any failure means "degrade"
        return ForumSupport(False, "unknown", telegram_reason(exc))
    if getattr(me, "has_topics_enabled", None) is False:
        return ForumSupport(False, "threads_off", "threaded mode is off")
    return ForumSupport(True)


def telegram_reason(exc: BaseException) -> str:
    """Telegram's own words, stripped of the boilerplate around them.

    ``"Telegram server says - Bad Request: not enough rights to create a
    topic"`` becomes ``"not enough rights to create a topic"`` — the only part
    that tells the owner what to fix, and short enough for one phone line.
    """
    text = str(exc).strip()
    line = text.splitlines()[0] if text else type(exc).__name__
    cleaned = _TELEGRAM_NOISE.sub("", line).strip()
    return (cleaned or type(exc).__name__)[:120]


class TopicCreateError(RuntimeError):
    """Telegram refused to create the forum topic, and said why.

    The old hardcoded "run /setup and grant Manage Topics" sent the owner in a
    circle when ``/setup`` reported everything was fine — so the reason travels
    with the exception all the way to the chat.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"Topic creation failed · {reason}")
        self.reason = reason


async def require_topic(
    bot: Bot,
    chat_id: int,
    label: str,
    *,
    marker: TopicMarker = TopicMarker.INITIALIZING,
    color_key: str | None = None,
) -> int:
    """Create the topic **now**, before the workspace exists — or raise.

    This call is the *proof* that a topic can exist here. ``can_manage_topics``
    is not: it has been observed ``true`` on a chat that then refused
    ``createForumTopic``. Anything that costs money runs after this returns.

    ``color_key`` is what the colour is hashed from, and it is the *workspace*
    label rather than this room's: one workspace now owns several rooms, and
    they read as a family in the list only if they share a colour. Defaults to
    ``label``, which is right for the first room of a new workspace.
    """
    icon = await topic_icon_id(bot, marker)
    try:
        topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=topic_title(marker, label),
            # Colour is identity and is fixed from here on; the emoji is state
            # and changes with every later rename. Telegram ignores the colour
            # entirely once a custom emoji is set, which is the right trade —
            # state is what you scan the list for.
            icon_color=topic_icon_color(color_key or label),
            icon_custom_emoji_id=icon,
        )
    except TelegramAPIError as exc:
        reason = telegram_reason(exc)
        log.warning(
            "topics.create_failed", chat_id=chat_id, reason=reason, error=str(exc)
        )
        raise TopicCreateError(reason) from exc
    return topic.message_thread_id


@dataclass(frozen=True, slots=True)
class Claim:
    """What a :func:`claim_topic` probe learned about an existing thread."""

    #: Telegram still knows this thread. ``False`` means open a new one.
    alive: bool
    #: The rename landed, so the stored title is what the list is showing.
    named: bool = False


async def claim_topic(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    label: str,
    *,
    marker: TopicMarker = TopicMarker.INITIALIZING,
) -> Claim:
    """Take over a thread Telegram opened, by renaming it — and prove it exists.

    This is :func:`require_topic`'s counterpart for the seat a request arrived
    in, and it carries the same contract, for the same reason: *the only proof
    that a room is usable is an API call that used it*. A claimed thread is not
    free of that question merely because an update once came from it — the
    confirm card puts a human-length pause between "Telegram opened a thread"
    and "we spend money on it", and a thread can be deleted from the phone in
    that window. Binding a paid workspace to a room that is gone is exactly the
    failure ``require_topic`` exists to prevent.

    The rename is the probe because it is a call we owe anyway: Telegram named
    this room after whatever opened it — ``/new``, or the first sentence of a
    dictated task — and the thread list is the only navigation a DM has.

    Three outcomes, and only one of them is a refusal:

    * ``Claim(True, True)`` — renamed, or already carrying that title.
    * ``Claim(True, False)`` — Telegram would not rename it, but the thread is
      there. Whether a bot may rename a thread a *user* created in a DM is
      undocumented; being unable to retitle a room is not being unable to use
      it, so the workspace still moves in and the caller leaves the stored
      marker NULL, which makes the next state transition retry the rename.
    * ``Claim(False)`` — the thread is gone. The caller opens one instead.
    """
    icon = await topic_icon_id(bot, marker)
    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=topic_title(marker, label),
            icon_custom_emoji_id=icon,
        )
    except TelegramBadRequest as exc:
        detail = str(exc).casefold()
        if "not modified" in detail:
            return Claim(True, True)
        if thread_is_gone(exc):
            log.info("topics.claim_gone", chat_id=chat_id, thread_id=thread_id)
            return Claim(False)
        log.info(
            "topics.claim_unnamed",
            chat_id=chat_id,
            thread_id=thread_id,
            reason=telegram_reason(exc),
        )
        return Claim(True)
    except TelegramAPIError as exc:
        # Ambiguous — a network blip, a 429. Treating that as "gone" would open
        # a duplicate room for a thread that is fine, which is unrecoverable;
        # treating it as "unnamed" costs a title until the next transition.
        log.warning(
            "topics.claim_unavailable", chat_id=chat_id, error=telegram_reason(exc)
        )
        return Claim(True)
    return Claim(True, True)


async def create_topic(
    bot: Bot,
    chat_id: int,
    label: str,
    *,
    marker: TopicMarker = TopicMarker.INITIALIZING,
    color_key: str | None = None,
) -> int | None:
    """:func:`require_topic` for callers that degrade instead of failing.

    ``None`` means this chat cannot host topics (a DM, forum mode off, missing
    permission) — the caller falls back to degraded single-session mode rather
    than failing the command.
    """
    try:
        return await require_topic(
            bot, chat_id, label, marker=marker, color_key=color_key
        )
    except TopicCreateError:
        return None


async def discard_topic(bot: Bot, chat_id: int, topic_id: int) -> bool:
    """Undo a topic *this same call* just created. Never a pre-existing one.

    A topic is free and deletable; the cloud workspace it was meant to host is
    neither. When the create half of the pair fails, the topic goes away so a
    retry does not leave a column of empty rooms behind. Delete first, close as
    the fallback — an old topic with messages in it cannot be deleted.
    """
    try:
        await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=topic_id)
        return True
    except TelegramAPIError as exc:
        log.warning(
            "topics.discard_failed",
            chat_id=chat_id,
            topic_id=topic_id,
            error=str(exc),
        )
    try:
        await bot.close_forum_topic(chat_id=chat_id, message_thread_id=topic_id)
    except TelegramAPIError as exc:
        log.warning(
            "topics.discard_close_failed",
            chat_id=chat_id,
            topic_id=topic_id,
            error=str(exc),
        )
        return False
    return True


async def attach_topic(
    db: Database,
    *,
    session_id: str,
    chat_id: int,
    topic_id: int,
    label: str,
    marker: TopicMarker = TopicMarker.INITIALIZING,
) -> None:
    """Adopt an already-created topic once the session id is known."""
    await sessions_repo.bind_topic(
        db,
        session_id,
        chat_id=chat_id,
        topic_id=topic_id,
        topic_name=label,
    )
    await sessions_repo.set_topic_marker(db, session_id, marker.value)
    await chats_repo.ensure(db, chat_id, topic_id, kind="topic")


async def ensure_topic(
    bot: Bot,
    db: Database,
    *,
    chat_id: int,
    session_id: str,
    label: str,
    marker: TopicMarker = TopicMarker.INITIALIZING,
    color_key: str | None = None,
) -> int | None:
    """Idempotent create-and-adopt for a session that already exists.

    This is the *lazy* half of the model: a session gets its room the first
    time it is opened, not when it is created. Without that, ``/attach`` on a
    laptop workspace with nine sessions would open nine rooms nobody asked for.
    """
    existing = await sessions_repo.get(db, session_id)
    if existing is not None and existing.has_room and existing.chat_id == chat_id:
        return existing.thread_id
    topic_id = await create_topic(
        bot, chat_id, label, marker=marker, color_key=color_key
    )
    if topic_id is None:
        return None
    await attach_topic(
        db,
        session_id=session_id,
        chat_id=chat_id,
        topic_id=topic_id,
        label=label,
        marker=marker,
    )
    return topic_id


async def session_marker(db: Database, session: SessionRow) -> TopicMarker:
    """:func:`marker_for` for one session, workspace lifecycle included.

    The workspace still wins — a sleeping container cannot be working — it is
    just resolved per session now, which is the whole point: two sessions of one
    workspace in different states used to fight over a single ``topic_marker``
    and whichever ticked last won.
    """
    workspace = (
        await workspaces_repo.get(db, session.workspace_id)
        if session.workspace_id
        else None
    )
    return marker_for(
        workspace_status=workspace.status_value if workspace is not None else None,
        turn_state=session.state,
        session_status=session.status_value,
    )


async def apply_marker(
    bot: Bot,
    db: Database,
    session_id: str,
    marker: TopicMarker,
    *,
    label: str | None = None,
) -> bool:
    """Rename the session's topic **only if the title would actually change**.

    Returns ``True`` when a rename was issued. Everything else — same title,
    no topic, a Telegram failure — returns ``False`` and costs no API call
    beyond the one that failed.

    A topic the bot did not create is renamed by :func:`claim_topic` instead,
    *before* it is bound to anything — so there is no case here where the stored
    marker is a claim about a title nobody ever applied, and no reason for a
    caller to want this comparison skipped.

    The state icon rides along: ``icon_custom_emoji_id`` is the one visual
    channel Telegram lets a bot change after creation (``icon_color`` is fixed
    for the topic's life), and the rename call is already being made.
    """
    row = await sessions_repo.get(db, session_id)
    if row is None or not row.has_room or row.chat_id is None:
        return False
    # Never the *Conductor* workspace name (`tg-<chat>-<nonce>`) and never a
    # project id. Retitling somebody's topic to an internal identifier because
    # one column came back NULL is worse than leaving the generic word a
    # nameless workspace would have been given.
    fallback = topic_label(None, None)
    if row.workspace_id:
        workspace = await workspaces_repo.get(db, row.workspace_id)
        if workspace is not None:
            fallback = topic_label(None, workspace.branch)
    name = label or row.topic_name or fallback
    title = topic_title(marker, name)
    # Compare the *rendered title*, not just the marker. Comparing markers
    # meant `/name -w` with an unchanged name always spent an API call, while a
    # renamed-but-same-state topic silently kept its old title.
    #
    # ``topic_marker`` is NULL until the first rename, and an older row can
    # carry a marker this version no longer has — neither is a reason to skip,
    # so an unreadable marker falls through to renaming.
    try:
        current = topic_title(TopicMarker(row.topic_marker), row.topic_name or name)
    except ValueError:
        current = None
    if row.topic_marker == marker.value and title == current:
        return False
    thread_id = row.thread_id
    # ``None`` here means "keep whatever icon the topic has". aiogram omits an
    # unset optional from the payload, and Telegram keeps the existing value
    # for an omitted field — so a pack we could not fetch costs the icon
    # update, never the rename.
    icon = await topic_icon_id(bot, marker)
    try:
        await bot.edit_forum_topic(
            chat_id=row.chat_id,
            message_thread_id=thread_id,
            name=title,
            icon_custom_emoji_id=icon,
        )
    except TelegramAPIError as exc:
        log.warning(
            "topics.rename_failed",
            session_id=session_id,
            marker=marker.value,
            error=str(exc),
        )
        # A rename refused because the *thread* is gone is the only signal
        # Telegram ever gives that a topic was deleted — there is no service
        # message for it (see :func:`room_gone`). Say it once, here.
        if thread_is_gone(exc):
            await room_gone(bot, db, session_id)
        return False
    await sessions_repo.set_topic_marker(db, session_id, marker.value)
    if label is not None and label != row.topic_name:
        await sessions_repo.update(db, session_id, topic_name=label)
    return True


class TopicRetirement(StrEnum):
    """What actually became of the room when its workspace was archived."""

    #: The topic is gone from the chat, and so is the card the tap came from.
    DELETED = "deleted"
    #: Telegram would not delete it, so it is renamed ``🗄 …`` and closed.
    CLOSED = "closed"
    #: Neither worked. The room is still live and still lying about it.
    FAILED = "failed"
    #: There was no topic to retire — a linear DM, or one already unbound.
    NONE = "none"


async def retire_topic(bot: Bot, db: Database, session_id: str) -> TopicRetirement:
    """Archive: take the room away, or close it if Telegram will not.

    Deleting is what archiving *means* on a phone. A closed topic is still a row
    in the list, at the same size and in the same reach of the thumb as a live
    one — so ``/done`` on the ten tasks of a busy week left ten rooms behind,
    and the list stopped being scannable, which is the one job it has.

    Deletion needs ``can_delete_messages``, which a bot has in its own DM and
    may not have in somebody's group, so the old rename-and-close stays as the
    fallback: a room that plainly says it is finished beats one that lies. The
    binding is dropped only on the delete path — a closed topic still exists,
    and forgetting where it is would strand every later state change.
    """
    row = await sessions_repo.get(db, session_id)
    if row is None or not row.has_room or row.chat_id is None:
        return TopicRetirement.NONE
    chat_id, topic_id = row.chat_id, row.thread_id
    try:
        await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=topic_id)
    except TelegramAPIError as exc:
        # Not a warning: in a group without the right, this is the expected
        # answer and the fallback below is a perfectly good outcome.
        log.info(
            "topics.delete_refused",
            session_id=session_id,
            topic_id=topic_id,
            error=str(exc),
        )
    else:
        await sessions_repo.unbind_topic(db, session_id)
        await chats_repo.unbind(db, chat_id, topic_id)
        return TopicRetirement.DELETED
    await apply_marker(bot, db, session_id, TopicMarker.ARCHIVED)
    try:
        await bot.close_forum_topic(chat_id=chat_id, message_thread_id=topic_id)
    except TelegramAPIError as exc:
        log.warning("topics.close_failed", session_id=session_id, error=str(exc))
        return TopicRetirement.FAILED
    return TopicRetirement.CLOSED


#: Said in the chat root when a topic turns out to have been deleted. It names
#: both ways out, because the session behind it is still live and still costing
#: money — this seam deliberately does *not* archive anything.
ROOM_GONE_NOTICE: Final = (
    "«{name}» lost its topic · <code>/board</code> to open it again, "
    "<code>/done</code> to archive it."
)


async def room_gone(bot: Bot, db: Database, session_id: str) -> bool:
    """A topic was deleted from Telegram. Free the session and say so once.

    **Telegram sends no update for a deleted topic.** ``prompts._SERVICE_CONTENT``
    enumerates created/edited/closed/reopened and hidden/unhidden — there is no
    deleted member. The bot finds out only by trying to use the room, and three
    places discover it independently: :func:`send_html`'s thread-gone resend,
    the outbox reroute, and :func:`apply_marker`'s refused rename. Before this
    seam none of them told the others, so the session stayed bound to a dead
    thread, the poller kept running, ``/board`` kept offering a jump to a room
    that was gone, and every delivery paid the reroute again row by row.

    **A deleted topic is a detach, not an archive.** The gesture is reachable by
    accident from a phone, it has no confirm of its own, and the thing on the
    other side costs money and holds uncommitted work. Unbinding is free to
    undo; archiving on a Telegram gesture is not.

    Idempotent — three racing discoveries cost one line, not three — and it
    returns whether this call was the one that did the work.
    """
    row = await sessions_repo.get(db, session_id)
    if row is None or not row.has_room or row.chat_id is None:
        return False
    chat_id, thread_id = row.chat_id, row.thread_id
    name = (row.topic_name or row.title or session_id[:8]).strip() or session_id[:8]
    await sessions_repo.unbind_topic(db, session_id)
    await chats_repo.unbind(db, chat_id, thread_id)
    log.info("topics.room_gone", session_id=session_id, thread_id=thread_id)
    await send_html(
        bot,
        chat_id,
        ROOM_GONE_NOTICE.format(name=escape(name)),
        thread_id=NO_THREAD_ID,
        silent=True,
    )
    return True
