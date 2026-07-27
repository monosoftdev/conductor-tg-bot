"""Operator commands, for whoever runs the deployment.

``PLATFORM_ADMIN_IDS`` is the gate, and it is deliberately *not* an allow-list
for using the bot — membership of a workspace decides that. An operator is
someone who can suspend a workspace or read the whole instance's health, not
someone who can read a customer's transcripts. Nothing here reads tenant data:
the strongest thing available is a slug, a status and a count.

Without these an operator's only recourse is ``psql``, which is a bad answer
when the thing to do is "stop that workspace, now".
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import abandon_wizard, command_text, tell
from ctb.bot.handlers.registration import deauthorize
from ctb.bot.middleware.tenancy import TenantContext, forget_cached
from ctb.db.repo import tenancy
from ctb.delivery.render.html import escape
from ctb.logging import get_logger
from ctb.runtime import system_database
from ctb.settings import Settings

router = Router(name=__name__)
register_router(router, order=1)

log = get_logger(__name__)

#: Rows one screen can hold on a phone.
_LIST_LIMIT = 30

_USAGE = (
    "<code>/platform list</code> · every workspace and its state\n"
    "<code>/platform suspend &lt;slug&gt;</code> · stop its polling now\n"
    "<code>/platform resume &lt;slug&gt;</code> · let it run again"
)


@router.message(Command("platform"))
async def platform(
    message: Message,
    tenant: TenantContext,
    settings: Settings,
    state: FSMContext,
) -> None:
    await abandon_wizard(state)
    if not settings.is_platform_admin(tenant.user_id):
        # Silence would be wrong here: the caller is a legitimate customer who
        # simply is not an operator, and they deserve to know the command
        # exists but is not theirs.
        await tell(message, "That command belongs to whoever runs this instance.")
        return

    parts = command_text(message).split()
    action = parts[0].casefold() if parts else ""
    slug = parts[1].casefold() if len(parts) > 1 else ""
    system = system_database()

    if action == "list":
        rows = await tenancy.list_active(system)
        lines = [f"<b>{len(rows)}</b> active workspace(s)"]
        for row in rows[:_LIST_LIMIT]:
            key = "🔑" if row.has_conductor_key else "—"
            stopped = " · auth failed" if row.auth_failed_at else ""
            lines.append(f"{key} <code>{escape(row.slug)}</code>{stopped}")
        if len(rows) > _LIST_LIMIT:
            lines.append(f"…and {len(rows) - _LIST_LIMIT} more")
        await tell(message, "\n".join(lines))
        return

    if action in {"suspend", "resume"} and slug:
        target = await tenancy.get_by_slug(system, slug)
        if target is None:
            await tell(message, "No workspace by that name.")
            return
        status = "suspended" if action == "suspend" else "active"
        await tenancy.set_status(system, target.id, status)
        forget_cached(target.id)
        if action == "suspend":
            # Drop the cached keys too: a suspended workspace should not have
            # a live, keyed connection sitting in memory. Best effort — the row
            # is what stops the polling, and that has already happened.
            await deauthorize(target.id)
        log.warning(
            "platform.status_changed",
            tenant=target.slug,
            status=status,
            by=tenant.user_id,
        )
        await tell(message, f"<b>{escape(target.slug)}</b> is now {status}.")
        return

    await tell(message, _USAGE)
