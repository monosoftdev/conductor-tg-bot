"""The allow-list. This is a security boundary, not a convenience.

Three rules, all from PLAN §Safety rails:

* **Every update type is checked.** The middleware is registered as an *outer*
  middleware on ``dp.update``, the one funnel every update passes through, so a
  new Bot API update kind is denied by default instead of falling through.
* **Rejection is silence.** Replying "you are not authorised" tells a stranger
  the bot exists, who it belongs to, and that the token is live. We drop the
  update and log it.
* **The owner hears about it once per unknown user per day**, with a global cap
  so a scripted flood cannot turn the owner's DM into the attacker's megaphone.

The authority is the environment (``ALLOWED_TELEGRAM_USER_IDS``); the
``allowed_users`` table is the runtime addition made by ``/allow``. The union is
checked, so a DB that has not been synced yet can never lock the owner out, and
a DB failure fails *closed* for everyone else.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from aiogram import Bot
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import (
    EVENT_CONTEXT_KEY,
    EventContext,
    UserContextMiddleware,
)
from aiogram.types import Chat, TelegramObject, Update, User

from ctb.db.connection import Database, get_database
from ctb.db.repo import allowlist
from ctb.logging import get_logger
from ctb.settings import Settings, get_settings

__all__ = [
    "MAX_OWNER_NOTICES_PER_WINDOW",
    "OWNER_NOTICE_INTERVAL_S",
    "AuthMiddleware",
    "Principal",
]

log = get_logger(__name__)

#: One DM per unknown user per day (PLAN §Safety rails).
OWNER_NOTICE_INTERVAL_S: Final = 24 * 60 * 60.0

#: ...and never more than this many in one window, however many strangers show
#: up. Without it the "once per user" rule is a spam amplifier.
MAX_OWNER_NOTICES_PER_WINDOW: Final = 20

#: Bound on the "already told the owner" table, so a flood cannot grow it
#: without limit. Eviction only risks a duplicate DM, never a missed rejection.
_MAX_TRACKED_STRANGERS: Final = 4096


@dataclass(frozen=True, slots=True)
class Principal:
    """Who sent the update, once the allow-list has had its say."""

    user_id: int | None
    is_owner: bool = False
    #: ``"env"`` (``ALLOWED_TELEGRAM_USER_IDS``) or ``"db"`` (``/allow``).
    source: str = "env"

    @property
    def is_allowed(self) -> bool:
        return self.user_id is not None


def _user_of(event: TelegramObject, data: dict[str, Any]) -> User | None:
    """The sender, from aiogram's event context with a direct fallback.

    :class:`~aiogram.dispatcher.middlewares.user_context.UserContextMiddleware`
    is registered ahead of us by ``Dispatcher.__init__`` and normally fills
    ``event_context``. The fallback keeps the check working if this middleware
    is ever used outside a Dispatcher (tests, a future webhook path).
    """
    context = data.get(EVENT_CONTEXT_KEY)
    if isinstance(context, EventContext):
        return context.user
    if isinstance(event, Update):
        return UserContextMiddleware.resolve_event_context(event).user
    return None


def _chat_of(event: TelegramObject, data: dict[str, Any]) -> Chat | None:
    context = data.get(EVENT_CONTEXT_KEY)
    if isinstance(context, EventContext):
        return context.chat
    if isinstance(event, Update):
        return UserContextMiddleware.resolve_event_context(event).chat
    return None


class AuthMiddleware(BaseMiddleware):
    """Drop every update from a user who is not on the allow-list."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        db: Database | None = None,
        notify_owner: bool = True,
        clock: Callable[[], float] = time.monotonic,
        notice_interval_s: float = OWNER_NOTICE_INTERVAL_S,
        max_notices_per_window: int = MAX_OWNER_NOTICES_PER_WINDOW,
    ) -> None:
        self._settings = settings
        self._db = db
        self._notify_owner = notify_owner
        self._clock = clock
        self._interval = notice_interval_s
        self._max_notices = max_notices_per_window
        #: user_id -> monotonic timestamp of the DM we already sent about them.
        self._notified: dict[int, float] = {}
        self._window_start: float | None = None
        self._window_count = 0
        #: Counters for ``/health``.
        self.rejected = 0
        self.notices_sent = 0

    # -- plumbing --------------------------------------------------------------

    def _resolve_settings(self) -> Settings | None:
        if self._settings is not None:
            return self._settings
        try:
            return get_settings()
        except Exception:  # pragma: no cover - misconfigured boot
            log.error("auth.settings_unavailable")
            return None

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

    async def _lookup(
        self, user_id: int, settings: Settings | None, db: Database | None
    ) -> Principal | None:
        """``None`` means "not allowed". Fails closed on a DB error."""
        if settings is not None and settings.is_allowed(user_id):
            return Principal(
                user_id=user_id,
                is_owner=user_id == settings.owner_id,
                source="env",
            )
        if db is None:
            return None
        try:
            allowed = await allowlist.is_allowed(db, user_id)
        except Exception as exc:
            # A broken DB must not open the door.
            log.error("auth.allowlist_lookup_failed", error=repr(exc))
            return None
        if not allowed:
            return None
        owner = settings is not None and user_id == settings.owner_id
        return Principal(user_id=user_id, is_owner=owner, source="db")

    # -- the boundary ----------------------------------------------------------

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings = self._resolve_settings()
        user = _user_of(event, data)
        user_id = user.id if user is not None else None

        principal = (
            None
            if user_id is None
            else await self._lookup(user_id, settings, self._resolve_db(data))
        )
        if principal is None:
            self.rejected += 1
            log.warning(
                "bot.update_rejected",
                user_id=user_id,
                username=user.username if user is not None else None,
                update_type=_update_type(event),
            )
            if user is not None and settings is not None:
                await self._maybe_notify_owner(data.get("bot"), user, settings, event)
            return None  # silence

        chat = _chat_of(event, data)
        if (
            settings is not None
            and settings.telegram_chat_id is not None
            and chat is not None
            and chat.type != "private"
            and chat.id != settings.telegram_chat_id
        ):
            self.rejected += 1
            log.warning(
                "bot.chat_rejected",
                user_id=user_id,
                chat_id=chat.id,
                configured_chat_id=settings.telegram_chat_id,
            )
            return None

        data["principal"] = principal
        data["is_owner"] = principal.is_owner
        return await handler(event, data)

    # -- owner notice ----------------------------------------------------------

    async def _maybe_notify_owner(
        self,
        bot: Any,
        user: User,
        settings: Settings,
        event: TelegramObject,
    ) -> None:
        if not self._notify_owner or not isinstance(bot, Bot):
            return
        now = self._clock()
        last = self._notified.get(user.id)
        if last is not None and now - last < self._interval:
            return
        if self._window_start is None or now - self._window_start >= self._interval:
            self._window_start = now
            self._window_count = 0
        if self._window_count >= self._max_notices:
            return

        # Record *before* awaiting: two concurrent updates from the same
        # stranger must not both decide they are the first.
        if len(self._notified) >= _MAX_TRACKED_STRANGERS:
            oldest = min(self._notified, key=lambda uid: self._notified[uid])
            del self._notified[oldest]
        self._notified[user.id] = now
        self._window_count += 1

        handle = f" @{user.username}" if user.username else ""
        text = (
            "🚫 Ignored an update from an unknown Telegram user.\n"
            f"id <code>{user.id}</code>{handle} · {_update_type(event)}\n"
            f"Allow with <code>/allow {user.id}</code>."
        )
        try:
            await bot.send_message(
                chat_id=settings.owner_id, text=text, disable_notification=True
            )
        except Exception as exc:
            # The owner may have never started the bot. Not our problem to solve
            # on the rejection path.
            log.warning("auth.owner_notice_failed", error=repr(exc))
            return
        self.notices_sent += 1


def _update_type(event: TelegramObject) -> str:
    if isinstance(event, Update):
        try:
            return event.event_type
        except Exception:  # pragma: no cover - unknown future update kind
            return "unknown"
    return type(event).__name__
