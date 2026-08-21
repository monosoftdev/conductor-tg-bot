"""``(chat_id, message_thread_id)`` → the bound session.

The address of a prompt is the forum topic your thumb is in (PLAN §Chat model).
That makes routing a pure lookup with no ambient "current session" variable to
lose track of, in every chat there is.

A private chat can host topics too, and whether it does is a **runtime fact**:
the same DM is a column of topics when Telegram allows it and one linear seat
when it does not, sometimes on consecutive days. So the two coexist, told apart
by the thread on the update rather than by a flag:

* ``thread_id == 0`` in a DM — the linear seat, and the DM's cockpit. Exactly
  one bound session, and ``/s`` may switch it to *any* workspace, which is what
  makes a topicless DM usable at all.
* ``thread_id != 0`` in a DM — a workspace's topic, addressed exactly as a
  group topic is: ``/s`` stays inside that workspace, because the way to reach
  another one is to tap its topic.

:attr:`Route.is_dm` therefore means "the one-seat DM", not "a private chat".

Resolution order, cheapest first:

1. ``chats(chat_id, thread_id)`` — the routing table proper.
2. ``sessions`` filtered on the same key with ``is_bound = 1`` — the fallback
   when a session was bound without a chat row (or the chat row lost its
   pointer).

The middleware is **read-only by default**. Creating a ``chats`` row on every
incoming update would write to the database for every keystroke in a group
merely watches; the handlers that actually need a row call ``chats.ensure``.

It is also the one place that knows, for every update, which topic the thumb is
in — so it marks that topic as the send queue's focus (PLAN §Notifications tier
1). Two attribute writes, no I/O, and it happens before the DB lookup so a
routing hiccup cannot cost the ordering.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Final

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import (
    EVENT_CONTEXT_KEY,
    EVENT_THREAD_ID_KEY,
    EventContext,
)
from aiogram.types import (
    CallbackQuery,
    MaybeInaccessibleMessage,
    Message,
    TelegramObject,
)
from aiogram.types import Update as TgUpdate

from ctb.db import GENERAL_TOPIC_ID, NO_THREAD_ID
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
    def is_private(self) -> bool:
        """A private chat, topics or not."""
        return self.kind == "dm"

    @property
    def is_dm(self) -> bool:
        """The **one-seat** DM: a private chat with no topic in play.

        Deliberately narrower than :attr:`is_private`. Everything that reads
        this reads it to mean "one session at a time, switchable to anything"
        — true of the DM root, and false of a DM topic, which is addressed like
        any other topic.
        """
        return self.kind == "dm" and self.thread_id == NO_THREAD_ID

    @property
    def is_general(self) -> bool:
        """The cockpit. PLAN §Safety rails: General never prompts."""
        return self.kind == "general"

    @property
    def is_topic(self) -> bool:
        """A workspace's own room — in a group or in a DM."""
        return self.thread_id != NO_THREAD_ID and self.kind in {"topic", "dm"}

    @property
    def claimable_thread(self) -> int:
        """A thread this update may *move into*, or :data:`NO_THREAD_ID`.

        Telegram's *New Chat* seat in a threaded DM is a composer, not a room:
        anything sent there — a command included — makes the client open a new
        thread named after that first line, and the update arrives already
        inside it. Opening a second topic beside that one is how one ``/new``
        came to leave a stray ``/new`` thread behind every time. So an empty
        thread the request already sits in *is* the room.

        Empty means: a private chat's thread with no session and no workspace
        bound to it. A group is deliberately excluded — its General is thread 0
        anyway, and a topic in a group is somebody's room, not scratch space.

        It lives here, beside :attr:`is_dm` and :attr:`is_topic`, because the
        seat is the router's own conclusion: it folds a forum's General back to
        0 and supplies the thread Telegram omits for a DM topic. Anything that
        answers this question from a raw ``Message`` — or, as the voice worker
        first did, from a stored row — is a second rule, and the two drift.
        """
        if not self.is_private or not self.thread_id:
            return NO_THREAD_ID
        if self.session_id or (self.chat is not None and self.chat.workspace_id):
            return NO_THREAD_ID
        return self.thread_id

    def reclaimable_thread(self, workspace_id: str) -> int:
        """This thread when it is *this workspace's* room and lost its session.

        :attr:`claimable_thread`'s counterpart for a room that has a history.
        The two are deliberately exclusive: that one refuses any thread whose
        ``chats`` row names a workspace, precisely so a detached room is never
        spent on a *new* one — and without this, "reopen" then had nowhere to
        land but a sibling topic beside the room the owner was typing in.

        Unlike :attr:`claimable_thread` a group is not excluded. The exclusion
        there is that a group topic is somebody's room rather than scratch
        space; here the ``chats`` row says whose, and it is this workspace's.

        Reclaiming is still only a *proposal*: ``adopt`` renames the thread
        before it binds anything, and a thread that is really gone answers that
        rename and gets a fresh room instead.
        """
        if not self.thread_id or self.session_id:
            return NO_THREAD_ID
        if self.chat is None or self.chat.workspace_id != workspace_id:
            return NO_THREAD_ID
        return self.thread_id


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
        thread_id = _thread_id(context, event)
        kind = _chat_kind(context, thread_id)
        reply_to = _reply_to_message_id(event)
        context = self._publish_seat(context, thread_id, data)

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

    @staticmethod
    def _publish_seat(
        context: EventContext, thread_id: int, data: dict[str, Any]
    ) -> EventContext:
        """Hand aiogram's FSM the same seat this middleware resolved.

        ``FSMStrategy.USER_IN_TOPIC`` keys wizard state on
        ``EventContext.thread_id``, and Telegram only fills that in when it also
        sets ``is_topic_message`` — documented for forums, never promised for a
        topic in a private chat. Left alone, every seat in a DM shares one
        wizard: an unfinished ``/new`` in the root then swallows the next line
        typed in *any* topic and spends it on a second workspace instead of
        prompting the session that topic is bound to.

        :func:`_thread_id` has already answered this exact question — including
        folding a forum's General back to :data:`NO_THREAD_ID` — so the answer
        is published rather than recomputed by a second, differing rule.
        ``install_middleware`` re-registers the FSM middleware behind us so it
        reads what this writes.
        """
        seat = thread_id or None
        if context.thread_id == seat:
            return context
        updated = replace(context, thread_id=seat)
        data[EVENT_CONTEXT_KEY] = updated
        # aiogram's own backward-compatible mirror of the same value. Nothing
        # here reads it, but leaving it disagreeing with the context beside it
        # is how the next reader gets the old answer.
        if seat is None:
            data.pop(EVENT_THREAD_ID_KEY, None)
        else:
            data[EVENT_THREAD_ID_KEY] = seat
        return updated

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
        return replace(route, chat=chat, session=session, via_reply=via_reply)

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


def _thread_id(context: EventContext, event: TelegramObject) -> int:
    """Which seat this update arrived in.

    aiogram only publishes ``EventContext.thread_id`` when Telegram also set
    ``is_topic_message``, which is documented for forums and *not* promised for
    a topic in a private chat. Getting that wrong would silently address every
    prompt typed in a DM topic to the DM root — a wrong session, not a missing
    one — so a private chat falls back to the raw ``message_thread_id``. In a
    private chat there is nothing else it could mean.
    """
    chat = context.chat
    if context.thread_id:
        if context.thread_id == GENERAL_TOPIC_ID and (
            chat is not None and chat.type in {"group", "supergroup"}
        ):
            # A forum's built-in General topic is thread 1, and Telegram tags
            # its messages inconsistently. Left alone it produces a *second*
            # identity for the one seat that is always there: a `chats` row, a
            # focus, wizard state and deliveries all keyed on 1, while `/setup`
            # wrote 0. Normalise once, here, instead of patching `{0, 1}` into
            # each of the many places that ask.
            return NO_THREAD_ID
        return context.thread_id
    if chat is None or chat.type != "private":
        return NO_THREAD_ID
    message = _message_of(event)
    if message is None:
        return NO_THREAD_ID
    return getattr(message, "message_thread_id", None) or NO_THREAD_ID


def _message_of(event: TelegramObject) -> Message | MaybeInaccessibleMessage | None:
    """The message an update hangs off, whoever sent it."""
    if isinstance(event, TgUpdate):
        query = event.callback_query
        return (
            event.message
            or event.edited_message
            or (query.message if query is not None else None)
        )
    if isinstance(event, Message):
        return event
    if isinstance(event, CallbackQuery):
        return event.message
    return None


def _chat_kind(context: EventContext, thread_id: int) -> str:
    """Which *chat* this is. Which *seat* is :attr:`Route.thread_id`'s job.

    A private chat stays ``"dm"`` whether or not the update arrived in a topic:
    everything downstream that branches on the kind — the voice queue's replay,
    ``chats.ensure`` — needs to know it is talking to a person, and would go
    looking for group topic rights if told otherwise.
    """
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
