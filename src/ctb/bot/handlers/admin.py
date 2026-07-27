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
from ctb.db.connection import Database, now_ms
from ctb.db.repo import allowlist, deliveries, events, lease
from ctb.db.repo.deliveries import (
    TERMINAL_RETENTION_DAYS as DELIVERY_RETENTION_DAYS,
)
from ctb.delivery.render.html import escape
from ctb.health import (
    DEGRADATION_AUTH_FATAL,
    DEGRADATION_DELIVERY_FAILED,
    DEGRADATION_LEASE_LOST,
    HealthMonitor,
    lease_line,
)
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
        await tell(message, "Owner only.")
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
        await tell(message, "Owner only.")
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


def _count(value: object) -> int:
    """A health section is untyped JSON; a missing or odd key reads as zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _ago(stamp_ms: int, *, now_ms_: int) -> str:
    """``4m ago`` — when it happened, which is half of "does this matter"."""
    if stamp_ms <= 0:
        return "unknown"
    seconds = max(0, (now_ms_ - stamp_ms) // 1000)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"


def _voice_line(voice: dict[str, object], *, retention_days: int) -> str:
    """Voice counters, but only once there is something to say.

    ``transcribing`` is called out separately: a note parked mid-flight is what
    a hang looks like, and folded into ``pending`` it reads the same as a note
    that arrived a second ago.
    """
    pending = _count(voice.get("pending"))
    failed = _count(voice.get("failed"))
    transcribing = _count(voice.get("transcribing"))
    if not (pending or failed):
        return ""
    parts = [f"{pending} waiting"]
    if transcribing:
        parts.append(f"{transcribing} transcribing")
    if failed:
        parts.append(f"{failed} failed · Retry on the note")
    line = "🎙 voice · " + " · ".join(parts)
    if failed:
        line += f"\n   clears itself after {retention_days}d"
    return line


def _delivery_line(digest: dict[str, object], *, now_ms_: int) -> str:
    """What failed to reach Telegram, when, why, and when it goes away.

    "2 deliveries exhausted their retries" answered none of those, so it read
    as a live fault needing attention when it is a record of a past one.
    """
    count = _count(digest.get("count"))
    if not count:
        return ""
    reason = str(digest.get("reason") or "").strip() or "Telegram refused it"
    when = _ago(_count(digest.get("newest_ms")), now_ms_=now_ms_)
    noun = "reply" if count == 1 else "replies"
    return (
        f"📬 {count} {noun} never sent · newest {escape(when)}\n"
        f"   {escape(reason[:80])}\n"
        f"   already retried to the limit · clears itself after "
        f"{DELIVERY_RETENTION_DAYS}d"
    )


def _verdict(status: str, degraded: bool, needs_owner: bool) -> str:
    """One line saying what this means for the person reading it.

    "degraded" is a word about the process. The owner is asking whether the bot
    still works and whether they have to do something.
    """
    if status == "down":
        return "🚫 <b>Down</b> · prompts are saved, nothing is running"
    if not degraded:
        return "✅ <b>Working</b> · nothing needs you"
    if needs_owner:
        return "⚠️ <b>Working</b> · one thing needs you"
    return "⚠️ <b>Working</b> · past failures, clearing on their own"


@router.message(Command("health"))
async def health(
    message: Message,
    state: FSMContext,
    is_owner: bool,
    settings: Settings,
    db: Database | None = None,
    client: ConductorClient | None = None,
    health_monitor: HealthMonitor | None = None,
) -> None:
    await abandon_wizard(state)
    if not is_owner:
        await tell(message, "Owner only.")
        return
    database = resolve_db(db)
    stamp = now_ms()
    problems: list[str] = []
    facts: list[str] = []
    needs_owner = False
    try:
        report = (
            await health_monitor.report(force=True)
            if health_monitor is not None
            else None
        )
        digest = await deliveries.failure_digest(database)
        counts = await deliveries.counts_by_state(database)
        api = resolve_client(client).health()
        recent = await events.recent_api_events(database, limit=20)
        if report is not None:
            status = str(report.status)
            # Anything the owner cannot fix themselves is noise on a phone; say
            # which ones actually want a human.
            needs_owner = report.has(DEGRADATION_AUTH_FATAL) or report.has(
                DEGRADATION_LEASE_LOST
            )
            for item in report.degradations:
                if item.code in {DEGRADATION_DELIVERY_FAILED}:
                    continue  # said better, with when and why, below
                problems.append(
                    f"⚠️ {escape(item.detail or item.code.replace('_', ' '))}"
                )
            facts.append(
                "📡 {} {} polling · {} behind".format(
                    _count(report.polling.get("bound_sessions")),
                    "session"
                    if _count(report.polling.get("bound_sessions")) == 1
                    else "sessions",
                    _count(report.polling.get("overdue")),
                )
            )
            voice = _voice_line(
                report.voice,
                retention_days=settings.voice_completed_retention_days,
            )
            if voice:
                problems.append(voice)
            uptime = f"{int(report.uptime_s)}s"
            holder = lease_line(report.lease)
            unknown = len(report.unknown_content_types)
        else:
            status = "ok"
            uptime = "n/a"
            held = await lease.get(database)
            holder = "held" if held else "<i>unheld</i>"
            unknown = len(await events.list_unknown_content_types(database, limit=20))
        delivery = _delivery_line(digest, now_ms_=stamp)
        if delivery:
            problems.append(delivery)
    except Exception as exc:
        await tell(message, f"Health failed: {escape(short_error(exc))}", silent=False)
        return
    circuit = str((api.get("circuit") or {}).get("state", "?"))
    failures = [event for event in recent if not event.ok]
    queued = counts.get("pending", 0) + counts.get("sending", 0)

    lines = [_verdict(status, bool(problems), needs_owner), ""]
    lines += problems
    if problems:
        lines.append("")
    # Plain English: "circuit closed" reads like a fault and is the healthy one.
    lines.append(
        "🔌 Conductor {} · {} {} in the last 20 calls".format(
            "reachable" if circuit == "closed" else f"<b>{escape(circuit)}</b>",
            len(failures) or "no",
            "failure" if len(failures) == 1 else "failures",
        )
    )
    lines += facts
    if queued:
        lines.append(f"📬 {queued} replies on the way out")
    if unknown:
        lines.append(f"❓ {unknown} unrecognised message shapes (nothing lost)")
    lines.append(f"⏱ up {escape(uptime)} · {holder}")
    if failures:
        lines.append(
            f"<i>last API error · {escape((failures[0].error or '')[:120])}</i>"
        )
    await tell(message, "\n".join(lines))


@router.message(Command("backup"))
async def backup(
    message: Message,
    state: FSMContext,
    is_owner: bool,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    if not is_owner:
        await tell(message, "Owner only.")
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
