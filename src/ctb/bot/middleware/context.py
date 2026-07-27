"""Bind the PLAN §Logging context keys for the lifetime of one update.

Every log line emitted while handling an update — including the rejection line
from :class:`~ctb.bot.middleware.tenancy.TenantMiddleware` and every Conductor API
call made downstream — carries ``request_id``, ``chat_id``, ``thread_id`` and
``user_id``. That is the whole debugging surface: one grep on ``request_id``
gives the complete story of one thumb press.

Registered *before* the allow-list so a rejected update is still traceable, and
it unbinds exactly the keys it bound, so it composes with whatever the poller
tasks bind in their own contexts.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import EVENT_CONTEXT_KEY, EventContext
from aiogram.types import TelegramObject, Update

from ctb.db import NO_THREAD_ID
from ctb.logging import bind_log_context, unbind_log_context

__all__ = ["LogContextMiddleware", "new_request_id"]

_BOUND_KEYS = (
    "request_id",
    "chat_id",
    "thread_id",
    "user_id",
    "update_id",
    "update_type",
)


def new_request_id() -> str:
    """Short, collision-free enough for one process's lifetime of updates."""
    return uuid.uuid4().hex[:12]


class LogContextMiddleware(BaseMiddleware):
    """Bind per-update structlog context; always unbind it again."""

    def __init__(
        self, *, request_id_factory: Callable[[], str] = new_request_id
    ) -> None:
        self._request_id = request_id_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        context = data.get(EVENT_CONTEXT_KEY)
        if not isinstance(context, EventContext):
            context = EventContext()
        request_id = self._request_id()
        data["request_id"] = request_id

        bind_log_context(
            request_id=request_id,
            chat_id=context.chat_id,
            thread_id=context.thread_id if context.thread_id else NO_THREAD_ID,
            user_id=context.user_id,
            update_id=event.update_id if isinstance(event, Update) else None,
            update_type=_update_type(event),
        )
        try:
            return await handler(event, data)
        finally:
            unbind_log_context(*_BOUND_KEYS)


def _update_type(event: TelegramObject) -> str | None:
    if not isinstance(event, Update):
        return type(event).__name__
    try:
        return event.event_type
    except Exception:  # pragma: no cover - unknown future update kind
        return "unknown"
