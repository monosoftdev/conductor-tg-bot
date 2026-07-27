"""The six daily-loop commands."""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ctb.bot.app import register_router
from ctb.bot.handlers.adopt import adopt_button
from ctb.bot.handlers.common import (
    abandon_wizard,
    command_text,
    create_and_bind,
    react_received,
    request_cancel,
    require_session,
    resolve_new_request,
    short_error,
    tell,
    workspace_name,
)
from ctb.bot.handlers.topics import (
    close_topic,
    edit_html,
    jump_url,
    resolve_client,
    resolve_db,
)
from ctb.bot.keyboards import (
    CONTROL_TTL_S,
    Action,
    Cb,
    NonceError,
    NonceStore,
    button,
    confirm_keyboard,
    keyboard,
    resolve,
    url_button,
)
from ctb.bot.middleware.routing import Route
from ctb.bot.middleware.tenancy import TenantContext
from ctb.conductor.client import ConductorClient
from ctb.db.connection import Database
from ctb.db.repo import prompts as prompts_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.sessions import SessionRow
from ctb.db.repo.workspaces import WorkspaceRow
from ctb.delivery.render.html import escape
from ctb.turn.state import TurnState
from ctb.turn.supervisor import Supervisor

router = Router(name=__name__)
register_router(router, order=20)

_FIND_ALLOWED: Final = re.compile(r"[^\w\s./:@+#-]+", re.UNICODE)
BOARD_VISIBLE: Final = 10
#: Five one-line hits fit one phone screen; anything past that is scrolling
#: past the answer you already found.
FIND_VISIBLE: Final = 5
#: Un-adopted workspaces one scan considers — the same bound the view query
#: uses. The *caller* caps what it shows and says how many it hid; nothing here
#: truncates silently.
ADOPTABLE_SCAN: Final = 20
#: The confirm button already names the workspace. Say what tapping it does.
ARCHIVE_CONSEQUENCE: Final = "Closes this topic. Restorable in Conductor."
_ACTIVE_STATES: Final[frozenset[TurnState]] = frozenset(
    {
        TurnState.SUBMIT_PENDING,
        TurnState.QUEUED,
        TurnState.WAKING,
        TurnState.WORKING,
        TurnState.DRAINING,
        TurnState.CANCELLING,
    }
)
_BOARD_SQL: Final = """
SELECT session_id, workspace_id, session_title, workspace_name, workspace_state,
       model, transcript_updated_at
  FROM session_transcripts_view
 WHERE workspace_state NOT IN ('archived', 'deleted')
 ORDER BY transcript_updated_at DESC
 LIMIT 20
""".strip()


def status_icon(value: str | TurnState | None) -> str:
    state = str(value or "").casefold()
    if any(word in state for word in ("error", "dead", "failed")):
        return "⚠️"
    if any(word in state for word in ("working", "running", "draining")):
        return "⚙️"
    if any(word in state for word in ("queued", "waking", "initializ")):
        return "⏳"
    if "cancell" in state or "stopp" in state:
        return "🛑"
    if "sleep" in state:
        return "💤"
    return "✅"


def session_overview_lines(
    session: SessionRow,
    workspace: WorkspaceRow | None,
    *,
    pending: int = 0,
) -> list[str]:
    """Two lines. The workspace/branch line is dropped: the topic title says it.

    ``workspace`` stays in the signature because it still decides the Archive
    button in :func:`mode`, and the voice path passes it too.
    """
    title = safe_session_title(session)
    state = str(session.state).casefold()
    lines = [f"{status_icon(session.state)} <b>{escape(title)}</b> · {escape(state)}"]
    detail = (
        f"{escape(session.agent or '?')} · "
        f"{escape(session.model or '?')}/{escape(session.effort or '?')}"
    )
    if pending:
        detail += f" · {pending} pending"
    lines.append(detail)
    if session.error_text:
        lines.append(f"⚠️ {escape(session.error_text[:180])}")
    return lines


def safe_session_title(session: SessionRow) -> str:
    return " ".join((session.title or "").split())[:80] or session.id[:8]


def normalize_find_term(value: str) -> str:
    """Small allow-list before SQL-literal escaping; never pass raw user text."""
    return " ".join(_FIND_ALLOWED.sub("", value).split())[:120]


def find_query(value: str) -> str:
    term = normalize_find_term(value)
    if not term:
        raise ValueError("Search needs letters or numbers.")
    literal = term.replace("'", "''")
    return (
        "SELECT session_id, workspace_id, session_title, workspace_name, "
        "transcript_updated_at, substr(transcript, 1, 240) AS preview "
        "FROM session_transcripts_view "
        f"WHERE transcript ILIKE '%{literal}%' "
        "ORDER BY transcript_updated_at DESC LIMIT 20"
    )


@router.message(Command("new"))
async def new_workspace(
    message: Message,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
) -> None:
    text = command_text(message)
    await abandon_wizard(state)
    if not text:
        from ctb.bot.wizards.new_workspace import start_wizard

        await start_wizard(
            message,
            route=route,
            settings=settings,
            state=state,
            db=db,
            client=client,
        )
        return
    try:
        database = resolve_db(db)
        conductor = tenant.client
        request = await resolve_new_request(
            text=text,
            route=route,
            defaults=tenant.settings,
            db=database,
            client=conductor,
        )
        created = await create_and_bind(
            message=message,
            route=route,
            request=request,
            db=database,
            client=conductor,
        )
    except Exception as exc:
        await tell(message, f"New failed: {escape(short_error(exc))}", silent=False)
        return
    target = (
        jump_url(message.chat.id, created.thread_id)
        if created.thread_id
        else created.deep_link
    )
    label = "Open topic" if created.thread_id else "Open in Conductor"
    markup = keyboard([[url_button(label, target)]]) if target else None
    # The topic's own card repeats "queued" two seconds later; this bubble only
    # has to carry the jump link.
    await tell(message, f"→ <b>{escape(created.label)}</b>", reply_markup=markup)


@router.message(Command("board"))
async def board(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    await abandon_wizard(state)
    database = resolve_db(db)
    rows = await board_rows(database, resolve_client(client, tenant))
    if not rows:
        await tell(message, "No live workspaces.")
        return
    # Every row is a button. One that already has a topic jumps to it; one made
    # on the laptop gets "+ Open …", which opens a topic for it here.
    buttons = []
    for row in rows[:BOARD_VISIBLE]:
        wid = str(row.get("workspace_id") or "")
        local = await workspaces_repo.get(database, wid) if wid else None
        name = str(row.get("workspace_name") or row.get("session_title") or wid[:8])
        icon = status_icon(
            str(row.get("display_state") or row.get("workspace_state") or "")
        )
        model = str(row.get("model") or "")
        target = (
            jump_url(local.chat_id, local.topic_id)
            if local and local.chat_id and local.topic_id
            else None
        )
        if target:
            suffix = f" · {model}" if model else ""
            buttons.append([url_button(f"{icon} {name}{suffix}", target)])
        elif wid:
            buttons.append(
                [
                    adopt_button(
                        workspace_id=wid,
                        name=name,
                        session_id=str(row.get("session_id") or "") or None,
                        store=nonces,
                        user_id=message.from_user.id if message.from_user else None,
                        chat_id=message.chat.id,
                        thread_id=message.message_thread_id or 0,
                    )
                ]
            )
    lines = [f"<b>{len(rows)} live</b>"]
    if len(rows) > BOARD_VISIBLE:
        lines.append(f"<i>+{len(rows) - BOARD_VISIBLE} more · /s</i>")
    await tell(
        message, "\n".join(lines), reply_markup=keyboard(buttons) if buttons else None
    )


async def board_rows(
    database: Database, client: ConductorClient
) -> list[dict[str, object]]:
    local_sessions = await sessions_repo.list_all(database)
    sessions_by_id = {row.id: row for row in local_sessions}
    sessions_by_workspace: dict[str, SessionRow] = {}
    for session in local_sessions:
        if session.workspace_id and session.workspace_id not in sessions_by_workspace:
            sessions_by_workspace[session.workspace_id] = session
    rows: list[dict[str, object]]
    try:
        result = await client.sql(_BOARD_SQL)
        rows = []
        for raw in result.rows:
            item = dict(raw)
            session = sessions_by_id.get(str(item.get("session_id") or ""))
            item["display_state"] = (
                str(session.state)
                if session is not None
                else str(item.get("workspace_state") or "")
            )
            rows.append(item)
    except Exception:
        local = await workspaces_repo.list_all(database)
        rows = [
            {
                "workspace_id": row.id,
                "workspace_name": workspace_name(row),
                "workspace_state": row.status or "unknown",
                "display_state": str(sessions_by_workspace[row.id].state)
                if row.id in sessions_by_workspace
                else row.status or "unknown",
                "model": row.model or "",
            }
            for row in local[:20]
        ]
    return rows


async def adoptable_rows(
    database: Database,
    client: ConductorClient,
    *,
    query: str = "",
    exclude: Collection[str] = (),
    limit: int = ADOPTABLE_SCAN,
) -> list[dict[str, object]]:
    """Org-wide workspaces that have no topic in this bot yet.

    Never raises: ``/s`` still lists the topics it knows when ``POST /v0/sql``
    is unavailable — :func:`board_rows` already falls back to the local cache,
    and anything past that degrades to "no suggestions".
    """
    try:
        rows = await board_rows(database, client)
    except Exception:
        return []
    needle = query.strip().casefold()
    seen = set(exclude)
    out: list[dict[str, object]] = []
    for row in rows:
        wid = str(row.get("workspace_id") or "")
        if not wid or wid in seen:
            continue
        local = await workspaces_repo.get(database, wid)
        if local is not None and local.chat_id is not None and local.topic_id:
            continue
        name = str(row.get("workspace_name") or row.get("session_title") or wid[:8])
        if needle and needle not in name.casefold() and needle not in wid.casefold():
            continue
        seen.add(wid)
        out.append(row)
        if len(out) >= limit:
            break
    return out


@router.message(Command("attach"))
async def attach_workspace(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    """Open a cloud workspace created outside Telegram."""
    await abandon_wizard(state)
    database = resolve_db(db)
    rows = await adoptable_rows(
        database,
        tenant.client,
        query=command_text(message),
        limit=BOARD_VISIBLE,
    )
    if not rows:
        await tell(message, "No unattached cloud workspace matches.")
        return
    buttons = []
    for row in rows:
        workspace_id = str(row.get("workspace_id") or "")
        name = str(
            row.get("workspace_name") or row.get("session_title") or workspace_id[:8]
        )
        buttons.append(
            [
                adopt_button(
                    workspace_id=workspace_id,
                    name=name,
                    session_id=str(row.get("session_id") or "") or None,
                    store=nonces,
                    user_id=message.from_user.id if message.from_user else None,
                    chat_id=message.chat.id,
                    thread_id=message.message_thread_id or 0,
                )
            ]
        )
    await tell(
        message,
        "<b>Open laptop workspace</b> · continues from now",
        reply_markup=keyboard(buttons),
    )


def board_lines(rows: list[dict[str, object]]) -> list[str]:
    """Text-only board. ``/board`` uses buttons; this is the voice path's copy."""
    lines = [f"<b>Board · {len(rows)} recent</b>"]
    for row in rows[:BOARD_VISIBLE]:
        wid = str(row.get("workspace_id") or "")
        name = str(row.get("workspace_name") or row.get("session_title") or wid[:8])
        status = str(row.get("display_state") or row.get("workspace_state") or "?")
        model = str(row.get("model") or "")
        lines.append(
            f"{status_icon(status)} <b>{escape(name)}</b>"
            + (f" · {escape(model)}" if model else "")
        )
    if len(rows) > BOARD_VISIBLE:
        lines.append(f"<i>+{len(rows) - BOARD_VISIBLE} more · use /s to switch</i>")
    return lines


@router.message(Command("stop"))
async def stop(
    message: Message,
    route: Route,
    state: FSMContext,
    supervisor: Supervisor | None = None,
) -> None:
    await abandon_wizard(state)
    session_id = await require_session(message, route)
    if not session_id:
        return
    try:
        accepted = await request_cancel(
            supervisor,
            session_id,
            requested_by=message.from_user.id if message.from_user else None,
        )
    except Exception as exc:
        await tell(message, f"Stop failed: {escape(short_error(exc))}", silent=False)
        return
    if not accepted:
        await tell(message, "Stop unavailable — retry.", silent=False)
        return
    # The card flips to 🛑 stopping… a second later; a bubble saying the same
    # thing is permanent noise. Fall back to it only if reactions are refused.
    if not await react_received(message):
        await tell(message, "Stopping…")


async def run_find(
    message: Message,
    text: str,
    *,
    client: ConductorClient,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        rendered = await find_text(client, text)
    except Exception as exc:
        await tell(message, f"Find failed: {escape(short_error(exc))}", silent=False)
        return
    await tell(message, rendered, reply_markup=reply_markup)


async def find_text(client: ConductorClient, text: str) -> str:
    """One line per hit. Ten titled 180-char previews are three screens."""
    result = await client.sql(find_query(text))
    if not result.rows:
        return "No matches."
    shown = result.rows[:FIND_VISIBLE]
    total = result.row_count or len(result.rows)
    lines: list[str] = []
    if total > len(shown) or result.truncated:
        lines.append(
            f"<b>{total} matches</b>" + (" · truncated" if result.truncated else "")
        )
    for row in shown:
        title = str(row.get("workspace_name") or row.get("session_title") or "session")
        preview = " ".join(str(row.get("preview") or "").split())
        lines.append(f"<b>{escape(title)}</b> · {escape(preview[:90])}")
    return "\n".join(lines)


@router.message(Command("find"))
async def find(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
) -> None:
    await abandon_wizard(state)
    text = command_text(message)
    if not text:
        await tell(message, "Usage: <code>/find text</code>")
        return
    await run_find(message, text, client=tenant.client)


@router.message(Command("mode", "here"))
async def mode(
    message: Message,
    route: Route,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    if route.session is None:
        await require_session(message, route)
        return
    session = route.session
    database = resolve_db(db)
    workspace = (
        await workspaces_repo.get(database, route.workspace_id)
        if route.workspace_id
        else None
    )
    pending = await prompts_repo.outstanding_count(database, session.id)
    controls = []
    if session.state in _ACTIVE_STATES:
        controls.append(
            button(
                "⏹ Stop",
                Action.STOP,
                session.id,
                store=nonces,
                user_id=message.from_user.id if message.from_user else None,
                chat_id=message.chat.id,
                thread_id=route.thread_id,
                ttl=CONTROL_TTL_S,
            )
        )
    controls.append(
        button(
            "📄 Transcript",
            Action.TRANSCRIPT,
            session.id,
            store=nonces,
            user_id=message.from_user.id if message.from_user else None,
            chat_id=message.chat.id,
            thread_id=route.thread_id,
            ttl=CONTROL_TTL_S,
        )
    )
    rows = [controls[index : index + 2] for index in range(0, len(controls), 2)]
    workspace_controls = []
    if workspace is not None and workspace.deep_link:
        workspace_controls.append(url_button("↗ Open", workspace.deep_link))
    if workspace is not None:
        workspace_controls.append(
            button(
                "🗄 Archive…",
                Action.ARCHIVE_REQUEST,
                workspace.id,
                store=nonces,
                user_id=message.from_user.id if message.from_user else None,
                chat_id=message.chat.id,
                thread_id=route.thread_id,
                ttl=CONTROL_TTL_S,
            )
        )
    if workspace_controls:
        rows.append(workspace_controls)
    await tell(
        message,
        "\n".join(session_overview_lines(session, workspace, pending=pending)),
        reply_markup=keyboard(rows),
    )


@router.message(Command("done"))
async def done(
    message: Message,
    route: Route,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    if not route.workspace_id:
        await tell(message, "No workspace here.")
        return
    row = await workspaces_repo.get(resolve_db(db), route.workspace_id)
    name = workspace_name(row) if row else route.workspace_id[:8]
    markup = confirm_keyboard(
        Action.ARCHIVE,
        route.workspace_id,
        name,
        verb="Archive",
        store=nonces,
        user_id=message.from_user.id if message.from_user else None,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id or 0,
    )
    # The button below already reads "Archive <name>", and the topic title bar
    # reads it a third time. Spend the line on the consequence instead.
    await tell(message, ARCHIVE_CONSEQUENCE, reply_markup=markup)


@router.callback_query(Cb.filter(F.action == Action.ARCHIVE.value))
async def confirm_archive(
    query: CallbackQuery,
    nonces: NonceStore,
    tenant: TenantContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.ARCHIVE, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    await query.answer("Archiving…")
    database = resolve_db(db)
    try:
        if query.bot is None:
            raise RuntimeError("Telegram bot is not bound to callback")
        workspace_id = ticket.target
        workspace = await workspaces_repo.get(database, workspace_id)
        if workspace is None:
            session = await sessions_repo.get(database, ticket.target)
            if session is None or session.workspace_id is None:
                raise RuntimeError("Workspace is no longer available.")
            workspace_id = session.workspace_id
        await resolve_client(client, tenant).archive_workspace(workspace_id)
        await workspaces_repo.mark_archived(database, workspace_id)
        await close_topic(query.bot, database, workspace_id)
    except Exception as exc:
        if isinstance(query.message, Message) and query.bot is not None:
            changed = await edit_html(
                query.bot,
                query.message.chat.id,
                query.message.message_id,
                f"⚠️ Archive failed · {escape(short_error(exc))}",
                reply_markup=None,
            )
            if not changed:
                await query.message.answer(
                    f"Archive failed: {escape(short_error(exc))}"
                )
        return
    if isinstance(query.message, Message) and query.bot is not None:
        changed = await edit_html(
            query.bot,
            query.message.chat.id,
            query.message.message_id,
            f"✓ Archived <b>{escape(ticket.label)}</b>.",
            reply_markup=None,
        )
        if not changed:
            await query.message.answer(f"Archived <b>{escape(ticket.label)}</b>.")


@router.callback_query(Cb.filter(F.action == Action.ARCHIVE_REQUEST.value))
async def request_archive(
    query: CallbackQuery,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    """Turn status-card Archive into the same named two-tap flow as /done."""
    try:
        ticket = resolve(query, expect=Action.ARCHIVE_REQUEST, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    database = resolve_db(db)
    workspace = await workspaces_repo.get(database, ticket.target)
    if workspace is None:
        session = await sessions_repo.get(database, ticket.target)
        if session is not None and session.workspace_id is not None:
            workspace = await workspaces_repo.get(database, session.workspace_id)
    if workspace is None:
        await query.answer("Workspace is gone.", show_alert=True)
        return
    name = workspace_name(workspace)
    markup = confirm_keyboard(
        Action.ARCHIVE,
        workspace.id,
        name,
        verb="Archive",
        store=nonces,
        user_id=query.from_user.id,
        chat_id=ticket.chat_id,
        thread_id=ticket.thread_id,
    )
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer(ARCHIVE_CONSEQUENCE, reply_markup=markup)


@router.callback_query(Cb.filter(F.action == Action.CANCEL.value))
async def cancel_callback(query: CallbackQuery, nonces: NonceStore) -> None:
    try:
        resolve(query, expect=Action.CANCEL, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    await query.answer("Cancelled")
    if isinstance(query.message, Message) and query.bot is not None:
        await edit_html(
            query.bot,
            query.message.chat.id,
            query.message.message_id,
            "Cancelled.",
            reply_markup=None,
        )
