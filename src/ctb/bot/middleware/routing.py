"""``(chat_id, message_thread_id)`` → the bound session.

The address of a prompt is the forum topic your thumb is in (PLAN §Chat model).
That makes routing a pure lookup with no ambient "current session" variable to
lose track of, and DM mode falls out for free: a private chat has no thread, so
it is ``thread_id = 0`` and gets exactly one bound session.

Resolution order, cheapest first:

1. ``chats(chat_id, thread_id)`` — the routing table proper.
2. ``sessions`` filtered on the same key with ``is_bound = 1`` — the fallback
   when a session was bound without a chat row (or the chat row lost its
   pointer).

The middleware is **read-only by default**. Creating a ``chats`` row on every
incoming update would write to SQLite for every keystroke in a group the bot
merely watches; the handlers that actually need a row call ``chats.ensure``.

It is also the one place that knows, for every update, which topic the thumb is
in — so it marks that topic as the send queue's focus (PLAN §Notifications tier
1). Two attribute writes, no I/O, and it happens before the DB lookup so a
routing hiccup cannot cost the ordering.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import EVENT_CONTEXT_KEY, EventContext
from aiogram.types import Message, TelegramObject
from aiogram.types import Update as TgUpdate

from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, get_database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo.chats import ChatRow
from ctb.db.repo.sessions import SessionRow
from ctb.delivery.outbox import FocusTracker, focus_tracker
from ctb.logging import bind_log_context, get_logger

__all__ = [
    "ROUTE_KEY",
    "ReplyResolver",
    "Route",
    "RoutingMiddleware",
    "default_reply_resolver",
]

log = get_logger(__name__)

#: Handler data key. ``async def handler(message: Message, route: Route)`` works
#: because aiogram injects handler kwargs by name.
ROUTE_KEY: Final = "route"

#: ``(db, chat_id, tg_message_id) -> session_id``. The reply-to override in
#: PLAN §Safety rails ("replying to any bot message routes to that message's
#: session") needs a lookup from a Telegram message id back to the session that
#: produced it. Tests may replace the repository-backed default.
type ReplyResolver = Callable[[Database, int, int], Awaitable[str | None]]


async def default_reply_resolver(
    db: Database, chat_id: int, tg_message_id: int
) -> str | None:
    return await deliveries_repo.session_for_telegram_message(
        db, chat_id, tg_message_id
    )


@dataclass(frozen=True, slots=True)
class Route:
    """Where this update came from and which session it drives."""

    #: Which workspace owns this update. Stamped by
    #: :class:`~ctb.bot.middleware.tenancy.TenantMiddleware`, and used for
    #: logging and fan-out — never for filtering, which row-level security
    #: already does.
    tenant_id: uuid.UUID | None = None
    #: Whether *this tenant* has voice on. The platform switch is separate.
    tenant_voice_enabled: bool = False
    chat_id: int | None = None
    thread_id: int = NO_THREAD_ID
    #: ``"dm"`` · ``"general"`` · ``"topic"`` · ``"unknown"``.
    kind: str = "unknown"
    chat: ChatRow | None = None
    session: SessionRow | None = None
    #: Set when the update is a reply to another message — the escape hatch for
    #: routing a prompt at a session other than the topic's.
    reply_to_message_id: int | None = None
    #: True when :attr:`session` came from the reply-to override rather than
    #: from the topic.
    via_reply: bool = False

    @property
    def key(self) -> tuple[int | None, int]:
        return (self.chat_id, self.thread_id)

    @property
    def session_id(self) -> str | None:
        if self.session is not None:
            return self.session.id
        return self.chat.session_id if self.chat is not None else None

    @property
    def workspace_id(self) -> str | None:
        if self.chat is not None and self.chat.workspace_id:
            return self.chat.workspace_id
        return self.session.workspace_id if self.session is not None else None

    @property
    def is_bound(self) -> bool:
        return self.session_id is not None

    @property
    def is_dm(self) -> bool:
        return self.kind == "dm"

    @property
    def is_general(self) -> bool:
        """The cockpit. PLAN §Safety rails: General never prompts."""
        return self.kind == "general"

    @property
    def is_topic(self) -> bool:
        return self.kind == "topic"


class RoutingMiddleware(BaseMiddleware):
    """Put a :class:`Route` in the handler data for every update."""

    def __init__(
        self,
        *,
        db: Database | None = None,
        reply_resolver: ReplyResolver | None = None,
        focus: FocusTracker | None = None,
    ) -> None:
        self._db = db
        self._reply_resolver = reply_resolver or default_reply_resolver
        self._focus = focus if focus is not None else focus_tracker()

    def _resolve_db(self, data: dict[str, Any]) -> Database | None:
        if self._db is not None:
            return self._db
        candidate = data.get("db")
        if isinstance(candidate, Database):
            return candidate
        try:
            return get_database()
        except Exception:
            return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        context = data.get(EVENT_CONTEXT_KEY)
        if not isinstance(context, EventContext):
            context = EventContext()
        chat_id = context.chat_id
        thread_id = context.thread_id or NO_THREAD_ID
        kind = _chat_kind(context, thread_id)
        reply_to = _reply_to_message_id(event)

        tenant = data.get("tenant")
        route = Route(
            tenant_id=getattr(tenant, "tenant_id", None),
            tenant_voice_enabled=bool(
                getattr(getattr(tenant, "settings", None), "voice_enabled", False)
            ),
            chat_id=chat_id,
            thread_id=thread_id,
            kind=kind,
            reply_to_message_id=reply_to,
        )
        if chat_id is not None:
            self._focus.note(chat_id, thread_id)
        db = self._resolve_db(data)
        # No tenant means TenantMiddleware let this through unresolved (the
        # registration path). There is no scope, so there is nothing to read.
        if db is not None and chat_id is not None and route.tenant_id is not None:
            route = await self._resolve(db, route)

        data[ROUTE_KEY] = route
        bind_log_context(session_id=route.session_id, workspace_id=route.workspace_id)
        return await handler(event, data)

    async def _resolve(self, db: Database, route: Route) -> Route:
        chat_id = route.chat_id
        assert chat_id is not None
        try:
            chat = await chats_repo.get(db, chat_id, route.thread_id)
            session = await self._session_for(db, chat, chat_id, route.thread_id)
            via_reply = False
            override = await self._reply_override(db, chat_id, route)
            if override is not None:
                session, via_reply = override, True
        except Exception as exc:
            # Routing is a lookup, not a gate. A DB hiccup must not swallow the
            # update — the handler sees an unbound route and says so.
            log.error("routing.lookup_failed", error=repr(exc), chat_id=chat_id)
            return route
        # `chats.kind` is authoritative once a row exists (a DM row is written
        # as kind='dm'), but the live Telegram chat type wins for General vs
        # topic because a topic can be deleted out from under us.
        return Route(
            chat_id=chat_id,
            thread_id=route.thread_id,
            kind=route.kind,
            chat=chat,
            session=session,
            reply_to_message_id=route.reply_to_message_id,
            via_reply=via_reply,
        )

    async def _session_for(
        self, db: Database, chat: ChatRow | None, chat_id: int, thread_id: int
    ) -> SessionRow | None:
        if chat is not None and chat.session_id:
            session = await sessions_repo.get(db, chat.session_id)
            if session is not None:
                return session
        return await sessions_repo.get_bound_for(db, chat_id, thread_id)

    async def _reply_override(
        self, db: Database, chat_id: int, route: Route
    ) -> SessionRow | None:
        if self._reply_resolver is None or route.reply_to_message_id is None:
            return None
        try:
            session_id = await self._reply_resolver(
                db, chat_id, route.reply_to_message_id
            )
        except Exception as exc:
            log.warning("routing.reply_resolver_failed", error=repr(exc))
            return None
        if not session_id:
            return None
        return await sessions_repo.get(db, session_id)


def _chat_kind(context: EventContext, thread_id: int) -> str:
    chat = context.chat
    if chat is None:
        return "unknown"
    if chat.type == "private":
        return "dm"
    if chat.type in {"group", "supergroup"}:
        return "topic" if thread_id != NO_THREAD_ID else "general"
    return "unknown"


def _reply_to_message_id(event: TelegramObject) -> int | None:
    """Only a *user* message can carry the reply-to override.

    A callback query's ``message`` is the bot's own card, and the button's
    target is already pinned by the nonce ticket, so it is deliberately not
    treated as a reply.
    """
    message: Message | None = None
    if isinstance(event, TgUpdate):
        message = event.message or event.edited_message
    elif isinstance(event, Message):
        message = event
    if message is None:
        return None
    reply = message.reply_to_message
    return reply.message_id if reply is not None else None
