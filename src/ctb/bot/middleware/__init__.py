"""Outer middleware for every Telegram update.

Three middlewares, registered on ``dp.update`` in this exact order by
:func:`ctb.bot.app.install_middleware`:

1. :class:`~ctb.bot.middleware.context.LogContextMiddleware` — binds
   ``request_id``/``chat_id``/``thread_id`` so even a rejection has a log line
   you can grep.
2. :class:`~ctb.bot.middleware.auth.AuthMiddleware` — **the security
   boundary**. Runs on every update type. A non-allow-listed user gets silence.
3. :class:`~ctb.bot.middleware.routing.RoutingMiddleware` — resolves
   ``(chat_id, message_thread_id)`` to the bound session and puts a
   :class:`~ctb.bot.middleware.routing.Route` in the handler data.

They are *outer* middlewares on the ``update`` observer, which is the single
funnel every update passes through — messages, edits, callbacks, inline
queries, chat-member changes and anything Telegram adds later. Registering per
event type would leave a hole the day Bot API 9.x ships a new update kind.
"""

from __future__ import annotations

from ctb.bot.middleware.auth import (
    OWNER_NOTICE_INTERVAL_S,
    AuthMiddleware,
    Principal,
)
from ctb.bot.middleware.context import LogContextMiddleware
from ctb.bot.middleware.routing import (
    ROUTE_KEY,
    ReplyResolver,
    Route,
    RoutingMiddleware,
)

__all__ = [
    "OWNER_NOTICE_INTERVAL_S",
    "ROUTE_KEY",
    "AuthMiddleware",
    "LogContextMiddleware",
    "Principal",
    "ReplyResolver",
    "Route",
    "RoutingMiddleware",
]
