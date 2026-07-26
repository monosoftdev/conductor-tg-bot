"""Owner-only administration."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import abandon_wizard, command_text, short_error, tell
from ctb.bot.handlers.topics import resolve_client, resolve_db
from ctb.conductor.client import ConductorClient
from ctb.db.backup import create_backup
from ctb.db.connection import Database
from ctb.db.repo import allowlist, deliveries, events, lease
from ctb.delivery.render.html import escape
from ctb.health import HealthMonitor
from ctb.settings import Settings

router = Router(name=__name__)
register_router(router, order=10)


def _user_id(raw: str) -> int:
    value = int(raw.strip())
    if value <= 0:
        raise ValueError
    return value


@router.message(Command("allow"))
async def allow(
    message: Message,
    state: FSMContext,
    is_owner: bool,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    if not is_owner:
        return
    try:
        user_id = _user_id(command_text(message))
    except ValueError:
        await tell(message, "Usage: <code>/allow telegram_user_id</code>")
        return
    await allowlist.upsert(
        resolve_db(db),
        user_id,
        added_by=message.from_user.id if message.from_user else None,
    )
    await tell(message, f"Allowed <code>{user_id}</code>.")


@router.message(Command("deny"))
async def deny(
    message: Message,
    state: FSMContext,
    is_owner: bool,
    settings: Settings,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    if not is_owner:
        return
    try:
        user_id = _user_id(command_text(message))
    except ValueError:
        await tell(message, "Usage: <code>/deny telegram_user_id</code>")
        return
    if user_id in settings.allowed_telegram_user_ids:
        await tell(message, "That user is allowed by environment; update Railway.")
        return
    removed = await allowlist.remove(resolve_db(db), user_id)
    await tell(
        message, f"{'Denied' if removed else 'Not found'} <code>{user_id}</code>."
    )


@router.message(Command("health"))
async def health(
    message: Message,
    state: FSMContext,
    is_owner: bool,
    db: Database | None = None,
    client: ConductorClient | None = None,
    health_monitor: HealthMonitor | None = None,
) -> None:
    await abandon_wizard(state)
    if not is_owner:
        return
    database = resolve_db(db)
    try:
        if health_monitor is not None:
            report = await health_monitor.report(force=True)
            status = str(report.status)
            uptime = f"{int(report.uptime_s)}s"
            lag = int(report.polling.get("overdue", 0) or 0)
            lease_holder = str(report.lease.get("holder") or "none")
            unknown = len(report.unknown_content_types)
        else:
            status = "ok"
            uptime = "n/a"
            lag = 0
            held = await lease.get(database)
            lease_holder = held.holder if held else "none"
            unknown = len(await events.list_unknown_content_types(database, limit=20))
        counts = await deliveries.counts_by_state(database)
        api = resolve_client(client).health()
        circuit = str((api.get("circuit") or {}).get("state", "?"))
        recent = await events.recent_api_events(database, limit=20)
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


@router.message(Command("backup"))
async def backup(
    message: Message,
    state: FSMContext,
    is_owner: bool,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    if not is_owner:
        return
    try:
        snapshot = await create_backup(resolve_db(db))
        if message.bot is None:
            raise RuntimeError("Telegram bot is not bound to the message")
        await message.bot.send_document(
            chat_id=message.chat.id,
            document=FSInputFile(snapshot, filename="ctb-backup.db"),
            message_thread_id=message.message_thread_id,
            caption="SQLite backup",
        )
    except Exception as exc:
        await tell(message, f"Backup failed: {escape(short_error(exc))}", silent=False)
