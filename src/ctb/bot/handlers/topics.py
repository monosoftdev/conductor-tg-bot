"""Forum-topic lifecycle, and the small chat plumbing every handler shares.

One topic per workspace. The address of a prompt is the topic your thumb is in
(PLAN §Chat model), so the topic is created *instantly* — before the workspace
exists — and adopted by the workspace id once ``POST /v0/workspaces`` returns.

**Renamed only on state transitions, never on a timer** (PLAN §Chat model). A
rename is a Telegram API call; a 5-second init card that renamed the topic every
tick would spend the whole flood budget on cosmetics. The last applied prefix
lives in ``workspaces.topic_marker``, and :func:`apply_marker` is a no-op when
the marker has not actually changed — that check is the rule, in code.

| Conductor state          | Topic name        |
|--------------------------|-------------------|
| workspace initializing   | ``~ proj/branch`` |
| ready + session idle     | ``proj/branch``   |
| session working          | ``● proj/branch`` |
| session error            | ``! proj/branch`` |
| workspace sleeping       | ``· proj/branch`` |
| archived / deleted       | ``x proj/branch``, topic closed |

This module has no router. It is imported by the handlers that do.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Final

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from ctb.conductor.client import ConductorClient, get_client
from ctb.conductor.models import SessionStatusValue, WorkspaceStatusValue
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, get_database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.sessions import SessionRow
from ctb.db.repo.workspaces import WorkspaceRow
from ctb.delivery.outbox import is_entity_error
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
    "edit_html",
    "ensure_topic",
    "forum_support",
    "jump_url",
    "marker_for",
    "require_topic",
    "resolve_client",
    "resolve_db",
    "send_html",
    "sync_marker",
    "telegram_reason",
    "topic_label",
    "topic_icon_color",
    "topic_title",
]

log = get_logger(__name__)

#: Telegram's own cap on a forum topic name.
TOPIC_NAME_LIMIT: Final = 128
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


def resolve_client(client: ConductorClient | None = None) -> ConductorClient:
    """The Conductor client. Handlers never construct one."""
    return client if client is not None else get_client()


# ── naming ───────────────────────────────────────────────────────────────────


def topic_label(project: str | None, branch: str | None) -> str:
    """``proj/branch`` — the stable half of a topic name, marker excluded."""
    left = (project or "workspace").strip() or "workspace"
    right = (branch or "").strip()
    label = f"{left}/{right}" if right else left
    return label[:TOPIC_NAME_LIMIT]


def topic_title(marker: TopicMarker, label: str) -> str:
    """Prefix + label, clipped to Telegram's 128-character topic name limit."""
    prefix = marker.prefix
    room = TOPIC_NAME_LIMIT - len(prefix)
    return f"{prefix}{label[:room]}"


def topic_icon_color(label: str) -> int:
    """Stable visual identity: the same project/branch keeps the same color."""
    digest = hashlib.sha256(label.casefold().encode("utf-8")).digest()
    return TOPIC_ICON_COLORS[int.from_bytes(digest[:2], "big") % len(TOPIC_ICON_COLORS)]


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
    jump to, and neither has a chat we have no topic for.
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
    ``deliveries`` outbox — but they honour the same rule as everything else:
    an entity-parse ``TelegramBadRequest`` is retried exactly once with
    ``parse_mode=None`` (CLAUDE.md). A reply may look ugly; it is never lost.
    """
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_thread_id": thread_id or None,
        "reply_markup": reply_markup,
        "disable_notification": silent or None,
    }
    if reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id
    try:
        return await bot.send_message(text=html, **kwargs)
    except TelegramBadRequest as exc:
        if not is_entity_error(exc):
            log.warning("topics.send_failed", chat_id=chat_id, error=str(exc))
            return None
        try:
            return await bot.send_message(
                text=strip_html(html), parse_mode=None, **kwargs
            )
        except TelegramAPIError as retry_exc:
            log.warning(
                "topics.send_retry_failed", chat_id=chat_id, error=str(retry_exc)
            )
            return None
    except TelegramAPIError as exc:
        log.warning("topics.send_failed", chat_id=chat_id, error=str(exc))
        return None


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
    #: ``unknown``.
    reason: str = "ok"
    detail: str = ""

    @property
    def degraded(self) -> bool:
        return not self.ok


async def forum_support(bot: Bot, chat_id: int) -> ForumSupport:
    """Check the two things degraded DM mode hinges on: forum + permission."""
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
    try:
        topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=topic_title(marker, label),
            icon_color=topic_icon_color(label),
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
    """Rename the topic **only if the marker actually changed**.

    Returns ``True`` when a rename was issued. Everything else — same marker,
    no topic, a Telegram failure — returns ``False`` and costs no API call
    beyond the one that failed.
    """
    row = await workspaces_repo.get(db, workspace_id)
    if row is None or row.chat_id is None or row.topic_id is None:
        return False
    name = label or row.topic_name or row.name or workspace_id
    if row.topic_marker == marker.value and label is None:
        return False
    try:
        await bot.edit_forum_topic(
            chat_id=row.chat_id,
            message_thread_id=row.topic_id,
            name=topic_title(marker, name),
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


async def sync_marker(bot: Bot, db: Database, workspace_id: str) -> bool:
    """Recompute the marker from the cached rows and apply it if it moved."""
    row = await workspaces_repo.get(db, workspace_id)
    if row is None:
        return False
    session = await _newest_session(db, row)
    marker = marker_for(
        workspace_status=row.status_value,
        turn_state=session.state if session is not None else None,
        session_status=session.status_value if session is not None else None,
    )
    return await apply_marker(bot, db, workspace_id, marker)


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
