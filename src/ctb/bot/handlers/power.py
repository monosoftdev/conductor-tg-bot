"""Power commands kept out of BotFather's daily menu."""

from __future__ import annotations

import json
import time
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    Message,
)

from ctb.bot.app import register_router
from ctb.bot.handlers.adopt import adopt_button
from ctb.bot.handlers.common import (
    abandon_wizard,
    command_text,
    new_session_id,
    react_ok,
    require_session,
    safe_title,
    short_error,
    tell,
    workspace_name,
)
from ctb.bot.handlers.core import adoptable_rows, status_icon
from ctb.bot.handlers.topics import (
    apply_marker,
    edit_html,
    jump_url,
    marker_for,
    resolve_client,
    resolve_db,
    topic_label,
)
from ctb.bot.keyboards import (
    CONTROL_TTL_S,
    Cb,
    NonceError,
    NonceStore,
    button,
    keyboard,
    resolve,
    url_button,
)
from ctb.bot.middleware.routing import Route
from ctb.bot.middleware.tenancy import TenantContext
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import validate_pairing
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import transcript as transcript_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.sessions import SessionRow
from ctb.delivery.render.html import escape
from ctb.turn import cursor as turn_cursor
from ctb.turn.state import TurnState

router = Router(name=__name__)
register_router(router, order=30)

_HELP = """<b>Phone loop</b>
General · <code>/new</code>, <code>/attach</code>, <code>/board</code>, or search
Topic · send text, voice, or audio
Result · tap a numbered choice; ✓ is recommended

<code>/mode</code> controls this session
<code>/s</code> switches only inside this workspace
<code>/fork</code> starts another session here
<code>/notify</code> sets topic alerts

<b>Your team</b>
<code>/invite id</code> adds someone · <code>/members</code> lists them
<code>/key</code> (privately) sets your Conductor key
<code>/export</code> downloads your data · <code>/privacy</code> explains it
<code>/use name</code> picks which team your DMs mean · <code>/leave</code> exits one
<code>/voice on</code> needs your own <code>/voicekey</code> first

Stop from the pinned card. /done always confirms.
Voice commands need “command” or “команда”."""

#: Buttons ``/s`` shows in General before it says how many it hid.
GENERAL_VISIBLE: Final = 12

#: One line, both forms — the second is the only way to set a branch by hand.
_DEFAULTS_USAGE: Final = (
    "<code>/defaults agent model effort</code> · <code>/defaults branch name</code>"
)

#: Name of the throwaway topic ``/setup`` creates to prove it can. Deleted
#: immediately; only ever visible if the delete itself is refused.
_SETUP_PROBE_LABEL: Final = "setup check"

_SWITCH_ACTIVE = frozenset(
    {
        TurnState.SUBMIT_PENDING,
        TurnState.QUEUED,
        TurnState.WAKING,
        TurnState.WORKING,
        TurnState.DRAINING,
        TurnState.CANCELLING,
    }
)


def switchable_sessions(
    sessions: list[SessionRow],
    route: Route,
) -> list[SessionRow]:
    """A topic may switch sessions, never workspaces. DMs remain one seat."""
    if route.is_dm:
        return sessions
    if route.workspace_id is None:
        return []
    return [row for row in sessions if row.workspace_id == route.workspace_id]


@router.message(Command("help", "start"))
async def help_command(message: Message, state: FSMContext) -> None:
    await abandon_wizard(state)
    await tell(message, _HELP)


@router.message(Command("s"))
async def switch_session(
    message: Message,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    database = resolve_db(db)
    query = command_text(message).casefold()
    sessions = [
        row
        for row in await sessions_repo.list_all(database)
        if row.state is not TurnState.DEAD
    ]
    workspaces = {
        row.id: row
        for row in await workspaces_repo.list_all(database, include_archived=False)
    }
    if query:
        sessions = [
            row
            for row in sessions
            if query in row.id.casefold()
            or query in (row.title or "").casefold()
            or (
                row.workspace_id in workspaces
                and (
                    query in workspace_name(workspaces[row.workspace_id]).casefold()
                    or query in (workspaces[row.workspace_id].branch or "").casefold()
                )
            )
        ]
    sessions.sort(
        key=lambda row: (
            row.state in _SWITCH_ACTIVE,
            row.last_prompt_at or row.updated_at or row.created_at,
        ),
        reverse=True,
    )
    in_general = message.chat.type in {"group", "supergroup"} and (
        message.message_thread_id or 0
    ) in {0, 1}
    if in_general:
        entries: list[InlineKeyboardButton] = []
        seen: set[str] = set()
        for session in sessions:
            workspace_id = session.workspace_id or ""
            workspace = workspaces.get(workspace_id)
            if workspace is None or workspace_id in seen:
                continue
            seen.add(workspace_id)
            target = (
                jump_url(workspace.chat_id, workspace.topic_id)
                if workspace.chat_id is not None
                else None
            )
            if target:
                entries.append(
                    url_button(
                        f"{status_icon(session.state)} {workspace_name(workspace)}",
                        target,
                    )
                )
        # "I know its name" also has to reach a workspace made on the laptop,
        # which has no topic and therefore no local session to switch to.
        for row in await adoptable_rows(
            database, tenant.client, query=query, exclude=seen
        ):
            workspace_id = str(row.get("workspace_id") or "")
            entries.append(
                adopt_button(
                    workspace_id=workspace_id,
                    name=str(
                        row.get("workspace_name")
                        or row.get("session_title")
                        or workspace_id[:8]
                    ),
                    session_id=str(row.get("session_id") or "") or None,
                    store=nonces,
                    user_id=message.from_user.id if message.from_user else None,
                    chat_id=message.chat.id,
                    thread_id=route.thread_id,
                )
            )
        if not entries:
            await tell(message, "No workspace matches.")
            return
        shown = entries[:GENERAL_VISIBLE]
        lines = ["<b>Open workspace</b> · + opens a laptop one here"]
        if len(entries) > len(shown):
            lines.append(f"<i>+{len(entries) - len(shown)} more · /s name</i>")
        await tell(
            message,
            "\n".join(lines),
            reply_markup=keyboard([[item] for item in shown]),
        )
        return
    sessions = switchable_sessions(sessions, route)
    if not sessions:
        await tell(
            message,
            "No session in this workspace. Use /fork or /board.",
        )
        return
    rows = []
    for row in sessions[:12]:
        label = safe_title(row.title, row.id[:8])
        current = "✓ " if row.id == route.session_id else ""
        model = row.model or "?"
        rows.append(
            [
                button(
                    f"{current}{status_icon(row.state)} {label} · {model}",
                    "switch",
                    row.id,
                    store=nonces,
                    user_id=message.from_user.id if message.from_user else None,
                    chat_id=message.chat.id,
                    thread_id=route.thread_id,
                    ttl=CONTROL_TTL_S,
                )
            ]
        )
    await tell(message, "<b>Switch session</b>", reply_markup=keyboard(rows))


@router.callback_query(Cb.filter(F.action == "switch"))
async def switch_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    try:
        ticket = resolve(query, expect="switch", store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    database = resolve_db(db)
    session = await sessions_repo.get(database, ticket.target)
    if session is None:
        await query.answer("Session is gone.", show_alert=True)
        return
    if ticket.thread_id != 0:
        chat = await chats_repo.get(
            database,
            ticket.chat_id or query.from_user.id,
            ticket.thread_id,
        )
        if (
            chat is None
            or chat.workspace_id is None
            or chat.workspace_id != session.workspace_id
        ):
            await query.answer(
                "That session belongs to another workspace. Open its topic.",
                show_alert=True,
            )
            return
    await sessions_repo.bind(
        database,
        session.id,
        chat_id=ticket.chat_id or query.from_user.id,
        thread_id=ticket.thread_id,
    )
    await chats_repo.bind(
        database,
        ticket.chat_id or query.from_user.id,
        ticket.thread_id,
        workspace_id=session.workspace_id,
        session_id=session.id,
        kind="dm" if ticket.thread_id == 0 else "topic",
    )
    await query.answer("Switched")
    if isinstance(query.message, Message) and query.bot is not None:
        await edit_html(
            query.bot,
            query.message.chat.id,
            query.message.message_id,
            f"✓ Current · <b>{escape(safe_title(session.title, session.id[:8]))}</b>",
            reply_markup=None,
        )


@router.message(Command("fork"))
async def fork(
    message: Message,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    await abandon_wizard(state)
    if not route.workspace_id:
        await tell(message, "No workspace here.")
        return
    database = resolve_db(db)
    current = route.session
    chat = route.chat
    agent = (
        (chat.default_agent if chat else None)
        or (current.agent if current else None)
        or tenant.settings.default_agent
    )
    model = (
        (chat.default_model if chat else None)
        or (current.model if current else None)
        or tenant.settings.default_model
    )
    effort = (
        (chat.default_effort if chat else None)
        or (current.effort if current else None)
        or tenant.settings.default_effort
    )
    title = command_text(message) or "Telegram fork"
    session_id = new_session_id()
    try:
        session = await turn_cursor.create_session(
            resolve_client(client, tenant),
            database,
            workspace_id=route.workspace_id,
            session_id=session_id,
            title=title,
            agent=agent,
            model=model,
            effort=effort,
        )
        await sessions_repo.upsert(
            database,
            session.id,
            workspace_id=route.workspace_id,
            title=title,
            agent=agent,
            model=model,
            effort=effort,
            chat_id=message.chat.id,
            thread_id=route.thread_id,
            is_bound=True,
        )
        await chats_repo.bind(
            database,
            message.chat.id,
            route.thread_id,
            workspace_id=route.workspace_id,
            session_id=session.id,
            kind=route.kind if route.kind in {"dm", "topic"} else "topic",
        )
    except Exception as exc:
        await tell(message, f"Fork failed: {escape(short_error(exc))}", silent=False)
        return
    # The argument the user just typed is the whole message. React instead.
    if not await react_ok(message):
        await tell(message, f"Forked <b>{escape(title[:80])}</b>.")


@router.message(Command("name"))
async def rename(
    message: Message,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    await abandon_wizard(state)
    raw = command_text(message)
    workspace_mode = raw.startswith("-w ")
    name = raw[3:].strip() if workspace_mode else raw
    if not name:
        await tell(
            message, "Usage: <code>/name text</code> or <code>/name -w text</code>"
        )
        return
    conductor = resolve_client(client, tenant)
    database = resolve_db(db)
    try:
        if workspace_mode:
            if not route.workspace_id:
                raise ValueError("No workspace here.")
            await conductor.rename_workspace(route.workspace_id, name)
            await workspaces_repo.update(database, route.workspace_id, name=name)
            workspace = await workspaces_repo.get(database, route.workspace_id)
            if workspace is not None and message.bot is not None:
                await apply_marker(
                    message.bot,
                    database,
                    route.workspace_id,
                    marker_for(
                        workspace_status=workspace.status_value,
                        turn_state=route.session.state if route.session else None,
                    ),
                    label=topic_label(name, workspace.branch),
                )
        else:
            session_id = await require_session(message, route)
            if not session_id:
                return
            await conductor.rename_session(session_id, name)
            await sessions_repo.update(database, session_id, title=name)
    except Exception as exc:
        await tell(message, f"Rename failed: {escape(short_error(exc))}", silent=False)
        return
    # The new name is already visible in the topic title (or the /s list).
    if not await react_ok(message):
        await tell(message, f"Renamed to <b>{escape(name)}</b>.")


@router.message(Command("open"))
async def open_workspace(
    message: Message,
    route: Route,
    state: FSMContext,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    if not route.workspace_id:
        await tell(message, "No workspace here.")
        return
    row = await workspaces_repo.get(resolve_db(db), route.workspace_id)
    if row is None or not row.deep_link:
        await tell(message, "No Conductor link cached yet.")
        return
    await tell(
        message,
        f"↗ <b>{escape(workspace_name(row))}</b>",
        reply_markup=keyboard([[url_button("Open in Conductor", row.deep_link)]]),
    )


@router.message(Command("desk"))
async def desk(
    message: Message,
    route: Route,
    state: FSMContext,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    if not route.workspace_id:
        await tell(message, "No workspace here.")
        return
    row = await workspaces_repo.get(resolve_db(db), route.workspace_id)
    if row is None:
        await tell(message, "Workspace cache missing.")
        return
    model = f"{route.session.model}/{route.session.effort}" if route.session else "?"
    lines = [
        f"<b>{escape(workspace_name(row))}</b>",
        f"{escape(row.branch or '?')} · {escape(model)}",
    ]
    markup = (
        keyboard([[url_button("Open in Conductor", row.deep_link)]])
        if row.deep_link
        else None
    )
    await tell(message, "\n".join(lines), reply_markup=markup)


@router.message(Command("log"))
async def log_command(
    message: Message,
    route: Route,
    state: FSMContext,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    session_id = await require_session(message, route)
    if not session_id:
        return
    try:
        limit = min(200, max(1, int(command_text(message) or "40")))
    except ValueError:
        await tell(message, "Usage: <code>/log [1-200]</code>")
        return
    rows = reversed(
        await transcript_repo.recent(resolve_db(db), session_id, limit=limit)
    )
    parts = [
        (
            f"## {row.session_index} · {row.type}\n\n"
            f"```json\n{row.content_json or '{}'}\n```"
        )
        for row in rows
    ]
    data = ("\n\n".join(parts) or "No cached messages.").encode()
    if message.bot is None:
        raise RuntimeError("Telegram bot is not bound to the message")
    await message.bot.send_document(
        message.chat.id,
        BufferedInputFile(data, filename=f"session-{session_id[:8]}.md"),
        message_thread_id=message.message_thread_id,
    )


@router.message(Command("notify"))
async def notify(
    message: Message,
    route: Route,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    value = command_text(message).lower()
    if value not in {"loud", "quiet", "off"}:
        current = route.chat.notify if route.chat else "quiet"
        choices = []
        for option, label in (
            ("loud", "🔔 Loud"),
            ("quiet", "🔕 Quiet"),
            ("off", "◯ Off"),
        ):
            choices.append(
                [
                    button(
                        f"{'✓ ' if option == current else ''}{label}",
                        "notify",
                        option,
                        store=nonces,
                        user_id=message.from_user.id if message.from_user else None,
                        chat_id=message.chat.id,
                        thread_id=route.thread_id,
                        ttl=CONTROL_TTL_S,
                        style="success" if option == current else "primary",
                    )
                ]
            )
        await tell(
            message,
            f"<b>Topic alerts</b> · {escape(current)}",
            reply_markup=keyboard(choices),
        )
        return
    database = resolve_db(db)
    await chats_repo.ensure(database, message.chat.id, route.thread_id, kind=route.kind)
    await chats_repo.set_notify(
        database, message.chat.id, route.thread_id, notify=value
    )
    if not await react_ok(message):
        await tell(message, f"Notify: <b>{escape(value)}</b>.")


@router.callback_query(Cb.filter(F.action == "notify"))
async def notify_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    try:
        ticket = resolve(query, expect="notify", store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    if ticket.target not in {"loud", "quiet", "off"}:
        await query.answer("Invalid alert mode.", show_alert=True)
        return
    database = resolve_db(db)
    chat_id = ticket.chat_id or query.from_user.id
    await chats_repo.ensure(
        database,
        chat_id,
        ticket.thread_id,
        kind="dm" if ticket.thread_id == 0 else "topic",
    )
    await chats_repo.set_notify(
        database,
        chat_id,
        ticket.thread_id,
        notify=ticket.target,
    )
    await query.answer(f"Alerts: {ticket.target}")
    if isinstance(query.message, Message) and query.bot is not None:
        await edit_html(
            query.bot,
            query.message.chat.id,
            query.message.message_id,
            f"✓ Topic alerts · <b>{escape(ticket.target)}</b>",
            reply_markup=None,
        )


@router.message(Command("defaults"))
async def defaults(
    message: Message,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    database = resolve_db(db)
    raw = command_text(message)
    chat = route.chat or await chats_repo.ensure(
        database, message.chat.id, route.thread_id, kind=route.kind
    )
    if raw:
        fields = raw.split()
        # The branch is remembered from the last create, but it has to be
        # settable without creating anything — that is the whole cold start.
        if len(fields) == 2 and fields[0].casefold() == "branch":
            branch = fields[1][:160]
            await chats_repo.set_defaults(
                database, message.chat.id, route.thread_id, branch=branch
            )
            await tell(message, f"Default branch: <b>{escape(branch)}</b>.")
            return
        if len(fields) != 3:
            await tell(message, _DEFAULTS_USAGE)
            return
        try:
            agent, model, effort = validate_pairing(*fields)
        except Exception as exc:
            await tell(
                message, f"Invalid defaults: {escape(short_error(exc))}", silent=False
            )
            return
        await chats_repo.set_defaults(
            database,
            message.chat.id,
            route.thread_id,
            agent=agent.value,
            model=model,
            effort=effort,
        )
        # `effort` is not always allow-listed: an agent with no declared
        # efforts (cursor) passes any string through validate_pairing.
        await tell(
            message,
            f"Defaults: <b>{escape(agent.value)}</b> · "
            f"{escape(model or '')}/{escape(effort or '')}.",
        )
        return
    agent = chat.default_agent or tenant.settings.default_agent
    model = chat.default_model or tenant.settings.default_model
    effort = chat.default_effort or tenant.settings.default_effort
    branch = chat.default_branch or tenant.settings.default_branch
    await tell(
        message,
        f"Defaults: <b>{escape(agent)}</b> · {escape(model)}/{escape(effort)} · "
        f"<b>{escape(branch)}</b>\n" + _DEFAULTS_USAGE,
    )


@router.message(Command("sql"))
async def sql_command(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
    is_owner: bool,
    client: ConductorClient | None = None,
) -> None:
    await abandon_wizard(state)
    if not is_owner:
        await tell(message, "Owner only.")
        return
    query = command_text(message).strip()
    folded = query.casefold()
    if (
        not folded.startswith("select ")
        or ";" in query.rstrip(";")
        or "set_config" in folded
    ):
        await tell(message, "Read-only single <code>SELECT</code> required.")
        return
    try:
        result = await resolve_client(client, tenant).sql(query)
    except Exception as exc:
        await tell(message, f"SQL failed: {escape(short_error(exc))}", silent=False)
        return
    payload = json.dumps(result.rows[:20], ensure_ascii=False, indent=2, default=str)
    suffix = " · truncated" if result.truncated else ""
    await tell(
        message,
        f"<b>{result.row_count or len(result.rows)} rows{suffix}</b>\n"
        f"<pre>{escape(payload[:3500])}</pre>",
    )


@router.message(Command("tidy"))
async def tidy(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
) -> None:
    """Close stale and archived topics. Owners only — it closes other people's."""
    await abandon_wizard(state)
    if not tenant.is_owner:
        await tell(message, "Owners only.")
        return
    database = resolve_db(db)
    closed = 0
    cutoff = int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000
    for row in await workspaces_repo.list_all(database, include_archived=True):
        stale = row.updated_at < cutoff and row.topic_id is not None
        archived = row.archived_at is not None
        if not (stale or archived) or row.chat_id is None or row.topic_id is None:
            continue
        try:
            if message.bot is None:
                raise RuntimeError("Telegram bot is not bound to the message")
            await message.bot.close_forum_topic(
                chat_id=row.chat_id, message_thread_id=row.topic_id
            )
        except Exception:
            continue
        closed += 1
    await tell(message, f"Tidy: {closed} topics closed.")
