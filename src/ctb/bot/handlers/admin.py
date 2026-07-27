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
from ctb.bot.middleware.tenancy import TenantContext, forget_cached
from ctb.db.connection import Database, now_ms
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import deliveries, events, lease, tenancy
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
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
from ctb.runtime import system_database
from ctb.settings import Settings

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
    try:
        await tenancy.add_member(
            system_database(),
            tenant.tenant_id,
            user_id,
            role=role,
            added_by=tenant.user_id,
        )
    except tenancy.RoleError as exc:
        await tell(message, escape(str(exc)))
        return
    forget_cached(tenant.tenant_id)
    await tell(message, f"Added <code>{user_id}</code> as {escape(role)}.")


@router.message(Command("remove"))
async def remove(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    """Unseat a user. Owners can only be removed by owners, never the last."""
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    try:
        user_id = _user_id(command_text(message))
    except ValueError:
        await tell(message, "Usage: <code>/remove telegram_user_id</code>")
        return
    removed = await tenancy.remove_member(
        system_database(), tenant.tenant_id, user_id, removed_by=tenant.user_id
    )
    if not removed:
        await tell(
            message,
            f"Did not remove <code>{user_id}</code> — not a member, or the last owner.",
        )
        return
    forget_cached(tenant.tenant_id)
    await tell(message, f"Removed <code>{user_id}</code>.")


@router.message(Command("leave"))
async def leave(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    """Remove *yourself* from this workspace.

    Anyone can seat anyone with ``/invite``, so there has to be a way out that
    does not depend on the person who seated you. An owner cannot leave while
    they are the last one — that would strand the workspace.
    """
    await abandon_wizard(state)
    left = await tenancy.remove_member(
        system_database(), tenant.tenant_id, tenant.user_id
    )
    if not left:
        await tell(
            message,
            "You are the last owner of <b>"
            + escape(tenant.slug)
            + "</b>. Make someone else an owner first, or use <code>/forget</code>.",
        )
        return
    forget_cached(tenant.tenant_id)
    await tell(message, f"You have left <b>{escape(tenant.slug)}</b>.")


@router.message(Command("members"))
async def members(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    """Who is in this workspace. Owners only — it lists Telegram user ids."""
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    rows = await tenancy.list_members(system_database(), tenant.tenant_id)
    lines = [f"<b>{escape(tenant.slug)}</b> · {len(rows)} member(s)"]
    for row in rows:
        handle = f" @{escape(row.username)}" if row.username else ""
        lines.append(f"<code>{row.user_id}</code>{handle} · {escape(row.role)}")
    await tell(message, "\n".join(lines))


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
    tenant: TenantContext,
    state: FSMContext,
    settings: Settings,
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
        # This workspace's client and this workspace's calls. `api_events` has
        # no row-level security — a request can fail before a tenant resolves —
        # so the filter here is explicit and the pool is the worker's.
        api = tenant.client.health() if tenant.has_client else {}
        recent = await events.recent_api_events(
            system, limit=20, tenant_id=tenant.tenant_id
        )
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
            # The lease is a platform row, not a tenant's: the worker pool.
            held = await lease.get(system)
            holder = "held" if held else "<i>unheld</i>"
            unknown = len(await events.list_unknown_content_types(database, limit=20))
        delivery = _delivery_line(digest, now_ms_=stamp)
        if delivery:
            problems.append(delivery)
    except Exception as exc:
        await tell(message, f"Health failed: {escape(short_error(exc))}", silent=False)
        return
    circuit = str((api.get("circuit") or {}).get("state", "?")) if api else "no key"
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
