"""Telegram command handlers.

Every module here owns exactly one ``Router`` and registers it at import time
(see :func:`ctb.bot.app.register_router`). ``discover_routers`` imports them
alphabetically, so nothing in this package may import ``ctb.bot.app`` at a
point where that would loop.

Order matters exactly once: ``text`` is the catch-all — plain text in a topic
is a prompt — so it declares ``ROUTER_ORDER = 900`` and every ``/command``
router wins ahead of it.
"""

from __future__ import annotations

__all__: list[str] = []
