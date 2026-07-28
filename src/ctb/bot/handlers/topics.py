"""Forum-topic lifecycle, and the small chat plumbing every handler shares.

One topic per workspace — **in a group and in a DM alike**. The address of a
prompt is the topic your thumb is in (PLAN §Chat model), so the topic is created
*instantly* — before the workspace exists — and adopted by the workspace id once
``POST /v0/workspaces`` returns.

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
lives in ``workspaces.topic_marker``, and :func:`apply_marker` is a no-op when
the marker has not actually changed — that check is the rule, in code.

The name is ``<marker> <task> · <project>/<branch>`` — a **state prefix** that
changes, and a **label** that never does. :func:`topic_label` builds the label
once, at creation, from the opening prompt; :func:`apply_marker` only ever
touches the prefix. Prefixes come from :mod:`ctb.signals`, so the topic list,
the status card and ``/board`` all say the same thing:

| Conductor state          | Topic name                       |
|--------------------------|----------------------------------|
| workspace initializing   | ``⏳ fix login · api/main``      |
| ready + session idle     | ``fix login · api/main``         |
| session working          | ``⚙️ fix login · api/main``      |
| turn finished, unread    | ``✅ fix login · api/main``      |
| session error            | ``⚠️ fix login · api/main``      |
| workspace sleeping       | ``💤 fix login · api/main``      |
| archived / deleted       | ``🗄 fix login · api/main``, topic closed |

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
from typing import Any, Final

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, Message

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
from ctb.delivery.render.html import strip_html
from ctb.logging import get_logger
from ctb.turn.state import TopicMarker, TurnState

__all__ = [
    "TOPIC_NAME_LIMIT",
    "TOPIC_ICON_COLORS",
    "ForumSupport",
    "TopicCreateError",
    "apply_marker",
    "attach_topic",
    "close_topic",
    "create_topic",
    "discard_topic",
    "dm_topic_support",
    "edit_html",
    "ensure_topic",
    "forum_support",
    "human_name",
    "jump_url",
    "marker_for",
    "require_topic",
    "resolve_client",
    "resolve_db",
    "send_html",
    "telegram_reason",
    "topic_label",
    "topic_icon_color",
    "topic_title",
]

log = get_logger(__name__)

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

    Every workspace this bot creates is called ``tg-1132334-iszvwjeb`` on the
    Conductor side, and that string was being rendered straight into buttons
    (``+ Open tg-1132334-iszvwjeb``) and, through adoption, into topic titles.
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
    three rows reading ``reclaimly-be/main``, in the same colour, with the same
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
#: they have to be asked for.
_ICON_IDS: dict[str, str] = {}
_ICON_LOCK = asyncio.Lock()


async def topic_icon_id(bot: Bot, marker: TopicMarker) -> str | None:
    """The custom-emoji id for this state's icon, or ``None`` to leave it alone.

    Never raises and never blocks a rename: if the pack cannot be fetched, or
    does not contain the wanted emoji, the caller renames without touching the
    icon. A missing icon is cosmetic; a failed rename is a topic that lies.
    """
    wanted = marker.icon
    if not wanted:
        return None
    if not _ICON_IDS:
        async with _ICON_LOCK:
            if not _ICON_IDS:  # another caller may have filled it while we waited
                try:
                    for sticker in await bot.get_forum_topic_icon_stickers():
                        emoji = getattr(sticker, "emoji", None)
                        sticker_id = getattr(sticker, "custom_emoji_id", None)
                        if emoji and sticker_id and emoji not in _ICON_IDS:
                            _ICON_IDS[emoji] = sticker_id
                except Exception as exc:
                    # Deliberately every exception, not just TelegramAPIError.
                    # This decorates a rename that has to happen either way, so
                    # there is no failure here worth propagating — a stale
                    # topic title is a lie, a missing icon is a missing icon.
                    log.warning("topics.icon_pack_unavailable", error=repr(exc))
                    return None
    return _ICON_IDS.get(wanted)


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
    reply_markup: InlineKeyboardMarkup | None = None,
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
    reply_markup: InlineKeyboardMarkup | None,
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
) -> int:
    """Create the topic **now**, before the workspace exists — or raise.

    This call is the *proof* that a topic can exist here. ``can_manage_topics``
    is not: it has been observed ``true`` on a chat that then refused
    ``createForumTopic``. Anything that costs money runs after this returns.
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
            icon_color=topic_icon_color(label),
            icon_custom_emoji_id=icon,
        )
    except TelegramAPIError as exc:
        reason = telegram_reason(exc)
        log.warning(
            "topics.create_failed", chat_id=chat_id, reason=reason, error=str(exc)
        )
        raise TopicCreateError(reason) from exc
    return topic.message_thread_id


async def create_topic(
    bot: Bot,
    chat_id: int,
    label: str,
    *,
    marker: TopicMarker = TopicMarker.INITIALIZING,
) -> int | None:
    """:func:`require_topic` for callers that degrade instead of failing.

    ``None`` means this chat cannot host topics (a DM, forum mode off, missing
    permission) — the caller falls back to degraded single-session mode rather
    than failing the command.
    """
    try:
        return await require_topic(bot, chat_id, label, marker=marker)
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
    workspace_id: str,
    chat_id: int,
    topic_id: int,
    label: str,
    marker: TopicMarker = TopicMarker.INITIALIZING,
) -> None:
    """Adopt an already-created topic once the workspace id is known."""
    await workspaces_repo.bind_topic(
        db,
        workspace_id,
        chat_id=chat_id,
        topic_id=topic_id,
        topic_name=label,
    )
    await workspaces_repo.set_topic_marker(db, workspace_id, marker.value)
    await chats_repo.ensure(db, chat_id, topic_id, kind="topic")


async def ensure_topic(
    bot: Bot,
    db: Database,
    *,
    chat_id: int,
    workspace_id: str,
    label: str,
    marker: TopicMarker = TopicMarker.INITIALIZING,
) -> int | None:
    """Idempotent create-and-adopt for a workspace that already exists."""
    existing = await workspaces_repo.get(db, workspace_id)
    if existing is not None and existing.has_topic and existing.topic_id is not None:
        return existing.topic_id
    topic_id = await create_topic(bot, chat_id, label, marker=marker)
    if topic_id is None:
        return None
    await attach_topic(
        db,
        workspace_id=workspace_id,
        chat_id=chat_id,
        topic_id=topic_id,
        label=label,
        marker=marker,
    )
    return topic_id


async def apply_marker(
    bot: Bot,
    db: Database,
    workspace_id: str,
    marker: TopicMarker,
    *,
    label: str | None = None,
) -> bool:
    """Rename the topic **only if the title would actually change**.

    Returns ``True`` when a rename was issued. Everything else — same title,
    no topic, a Telegram failure — returns ``False`` and costs no API call
    beyond the one that failed.

    The state icon rides along: ``icon_custom_emoji_id`` is the one visual
    channel Telegram lets a bot change after creation (``icon_color`` is fixed
    for the topic's life), and the rename call is already being made.
    """
    row = await workspaces_repo.get(db, workspace_id)
    if row is None or row.chat_id is None or row.topic_id is None:
        return False
    # Never `row.name` (the *Conductor* name, `tg-<chat>-<nonce>`) and never
    # `row.project_id` (an id, not a project). Retitling somebody's topic to an
    # internal identifier because one column came back NULL is worse than
    # leaving the generic word a nameless workspace would have been given.
    name = label or row.topic_name or topic_label(None, row.branch)
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
    # ``None`` here means "keep whatever icon the topic has". aiogram omits an
    # unset optional from the payload, and Telegram keeps the existing value
    # for an omitted field — so a pack we could not fetch costs the icon
    # update, never the rename.
    icon = await topic_icon_id(bot, marker)
    try:
        await bot.edit_forum_topic(
            chat_id=row.chat_id,
            message_thread_id=row.topic_id,
            name=title,
            icon_custom_emoji_id=icon,
        )
    except TelegramAPIError as exc:
        log.warning(
            "topics.rename_failed",
            workspace_id=workspace_id,
            marker=marker.value,
            error=str(exc),
        )
        return False
    await workspaces_repo.set_topic_marker(db, workspace_id, marker.value)
    if label is not None and label != row.topic_name:
        await workspaces_repo.update(db, workspace_id, topic_name=label)
    return True


async def close_topic(bot: Bot, db: Database, workspace_id: str) -> bool:
    """Archive: rename to ``x proj/branch`` and close the topic."""
    row = await workspaces_repo.get(db, workspace_id)
    if row is None or row.chat_id is None or row.topic_id is None:
        return False
    await apply_marker(bot, db, workspace_id, TopicMarker.ARCHIVED)
    try:
        await bot.close_forum_topic(chat_id=row.chat_id, message_thread_id=row.topic_id)
    except TelegramAPIError as exc:
        log.warning("topics.close_failed", workspace_id=workspace_id, error=str(exc))
        return False
    return True


async def _newest_session(db: Database, row: WorkspaceRow) -> SessionRow | None:
    sessions = await sessions_repo.list_for_workspace(db, row.id)
    if not sessions:
        return None
    bound = [s for s in sessions if s.is_bound]
    pool = bound or sessions
    return max(pool, key=lambda s: s.updated_at)
