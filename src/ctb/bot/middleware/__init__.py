"""Outer middleware for every Telegram update.

Three middlewares, registered on ``dp.update`` in this exact order by
:func:`ctb.bot.app.install_middleware`:

1. :class:`~ctb.bot.middleware.context.LogContextMiddleware` — binds
   ``request_id``/``chat_id``/``thread_id`` so even a rejection has a log line
   you can grep.
2. :class:`~ctb.bot.middleware.tenancy.TenantMiddleware` — **the security
   boundary**. Runs on every update type. It resolves the chat to a tenant,
   checks membership, publishes the database scope, and gives everyone else
   silence.
3. :class:`~ctb.bot.middleware.routing.RoutingMiddleware` — resolves
   ``(chat_id, message_thread_id)`` to the bound session and puts a
   :class:`~ctb.bot.middleware.routing.Route` in the handler data.

The order is not cosmetic. Routing reads the database, and a tenant-scoped
query with no tenant in scope raises. Tenancy must therefore run first, and
aiogram's own FSM middleware — which also reads storage — is moved behind it.

They are *outer* middlewares on the ``update`` observer, which is the single
funnel every update passes through — messages, edits, callbacks, inline
queries, chat-member changes and anything Telegram adds later. Registering per
event type would leave a hole the day Bot API 9.x ships a new update kind.
"""

from __future__ import annotations

from ctb.bot.middleware.context import LogContextMiddleware
from ctb.bot.middleware.routing import (
    ROUTE_KEY,
    ReplyResolver,
    Route,
    RoutingMiddleware,
)
from ctb.bot.middleware.tenancy import (
    STRANGER_NOTICE_INTERVAL_S,
    Principal,
    StrangerNotifier,
    TenantContext,
    TenantMiddleware,
    TenantSettings,
)

__all__ = [
    "ROUTE_KEY",
    "STRANGER_NOTICE_INTERVAL_S",
    "LogContextMiddleware",
    "Principal",
    "ReplyResolver",
    "Route",
    "RoutingMiddleware",
    "StrangerNotifier",
    "TenantContext",
    "TenantMiddleware",
    "TenantSettings",
]
