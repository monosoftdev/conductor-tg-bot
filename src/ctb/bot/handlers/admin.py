"""Workspace administration: membership, health, and the data export.

"Owner" is a role on ``tenant_members``, not a position in an environment
variable, so every command here acts on *the caller's* workspace and can only
ever see that workspace's data.

``/backup`` is deliberately gone. It used to upload the whole SQLite file; the
same code against a shared database would hand one customer every other
customer's transcripts. :func:`export` replaces it with a per-tenant JSON dump
built from row-level-security-scoped reads — the database itself decides what
goes in the file.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import abandon_wizard, command_text, short_error, tell
from ctb.bot.handlers.topics import resolve_db
from ctb.bot.middleware.tenancy import TenantContext
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import deliveries, events, lease, tenancy
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.delivery.render.html import escape
from ctb.health import HealthMonitor
from ctb.runtime import system_database

router = Router(name=__name__)
register_router(router, order=10)

#: Bound on the export, so a long-running workspace cannot produce a file
#: Telegram refuses and a request cannot pull the whole database into memory.
_EXPORT_LIMIT = 5_000


def _user_id(raw: str) -> int:
    value = int(raw.strip())
    if value <= 0:
        raise ValueError
    return value


@router.message(Command("invite"))
async def invite(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    """Seat another Telegram user in this workspace.

    This is the co-founder path: same group, same Conductor organisation, same
    topics. ``/invite <id> admin`` promotes instead.
    """
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    parts = command_text(message).split()
    if not parts:
        await tell(
            message,
            "Usage: <code>/invite telegram_user_id [admin|member]</code>\n"
            "They must send the bot a message once before this works.",
        )
        return
    try:
        user_id = _user_id(parts[0])
    except ValueError:
        await tell(message, "Usage: <code>/invite telegram_user_id</code>")
        return
    role = parts[1].casefold() if len(parts) > 1 else "member"
    if role not in ("member", "admin"):
        await tell(message, "Role must be <code>member</code> or <code>admin</code>.")
        return
    await tenancy.add_member(
        system_database(),
        tenant.tenant_id,
        user_id,
        role=role,
        added_by=tenant.user_id,
    )
    await tell(message, f"Added <code>{user_id}</code> as {escape(role)}.")


@router.message(Command("remove"))
async def remove(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    """Unseat a user. The last remaining owner cannot be removed."""
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    try:
        user_id = _user_id(command_text(message))
    except ValueError:
        await tell(message, "Usage: <code>/remove telegram_user_id</code>")
        return
    removed = await tenancy.remove_member(system_database(), tenant.tenant_id, user_id)
    if not removed:
        await tell(
            message,
            f"Did not remove <code>{user_id}</code> — not a member, or the last owner.",
        )
        return
    await tell(message, f"Removed <code>{user_id}</code>.")


@router.message(Command("members"))
async def members(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    await abandon_wizard(state)
    rows = await tenancy.list_members(system_database(), tenant.tenant_id)
    lines = [f"<b>{escape(tenant.slug)}</b> · {len(rows)} member(s)"]
    for row in rows:
        handle = f" @{escape(row.username)}" if row.username else ""
        lines.append(f"<code>{row.user_id}</code>{handle} · {escape(row.role)}")
    await tell(message, "\n".join(lines))


@router.message(Command("health"))
async def health(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """This workspace's health. Platform-wide numbers live on ``/health`` HTTP."""
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    database = resolve_db(db)
    system = system_database()
    try:
        if health_monitor is not None:
            report = await health_monitor.report(force=True)
            status = str(report.status)
            uptime = f"{int(report.uptime_s)}s"
            lag = int(report.polling.get("overdue", 0) or 0)
            lease_holder = str(report.lease.get("holder") or "none")
        else:
            status = "ok"
            uptime = "n/a"
            lag = 0
            held = await lease.get(system)
            lease_holder = held.holder if held else "none"
        unknown = len(await events.list_unknown_content_types(database, limit=20))
        counts = await deliveries.counts_by_state(database)
        circuit = "no key"
        if tenant.has_client:
            api = tenant.client.health()
            circuit = str((api.get("circuit") or {}).get("state", "?"))
        recent = await events.recent_api_events(
            system, limit=20, tenant_id=tenant.tenant_id
        )
    except Exception as exc:
        await tell(message, f"Health failed: {escape(short_error(exc))}", silent=False)
        return
    failures = [event for event in recent if not event.ok]
    last = failures[0].error if failures else "none"
    pending = counts.get("pending", 0) + counts.get("sending", 0)
    await tell(
        message,
        f"<b>{escape(status)}</b> · uptime {escape(uptime)}\n"
        f"circuit {escape(circuit)} · poll lag {lag}\n"
        f"deliveries {pending} pending · unknown {unknown}\n"
        f"lease <code>{escape(lease_holder[:36])}</code>\n"
        f"last error: {escape((last or 'none')[:180])}",
    )


@router.message(Command("export"))
async def export(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
) -> None:
    """Download this workspace's rows as JSON.

    Every read runs on the tenant-scoped pool, so the file cannot contain
    another workspace's data even if this function is wrong.
    """
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    database = resolve_db(db)
    try:
        payload: dict[str, Any] = {
            "workspace": tenant.slug,
            "chats": [_asdict(row) for row in await chats_repo.list_all(database)][
                :_EXPORT_LIMIT
            ],
            "workspaces": [
                _asdict(row) for row in await workspaces_repo.list_all(database)
            ][:_EXPORT_LIMIT],
            "sessions": [
                _asdict(row) for row in await sessions_repo.list_all(database)
            ][:_EXPORT_LIMIT],
        }
        blob = json.dumps(payload, indent=2, default=str).encode("utf-8")
        if message.bot is None:
            raise RuntimeError("Telegram bot is not bound to the message")
        await message.bot.send_document(
            chat_id=message.chat.id,
            document=BufferedInputFile(blob, filename=f"{tenant.slug}-export.json"),
            message_thread_id=message.message_thread_id,
            caption="Workspace export · transcripts are not included",
        )
    except Exception as exc:
        await tell(message, f"Export failed: {escape(short_error(exc))}", silent=False)


def _asdict(row: Any) -> dict[str, Any]:
    """A repo row as plain JSON-able data. Every row here is a dataclass."""
    return asdict(row)
