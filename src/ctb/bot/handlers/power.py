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
    human_name,
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
    home_keyboard,
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
from ctb.db.repo.workspaces import WorkspaceRow
from ctb.delivery.render.html import escape
from ctb.turn import cursor as turn_cursor
from ctb.turn.state import TopicMarker, TurnState

router = Router(name=__name__)
register_router(router, order=30)

#: Written for someone whose whole bot is this private chat, and still true in
#: a group: "here" is the DM root or General, "a workspace topic" is the room
#: ``/new`` opens either way. It used to open with "General ·", which named a
#: room a DM-only user does not have.
_HELP = """<b>Phone loop</b>
New thread · type a task; it becomes a workspace
In a workspace · send text, voice, or audio
Result · tap a numbered choice; ✓ is recommended
<code>/attach</code> a laptop one · <code>/board</code> · <code>/home</code> for buttons

<code>/stop</code> cancels this turn · also on the pinned card
<code>/find text</code> searches every transcript you can reach
<code>/name text</code> renames this session · <code>-w</code> renames the topic
<code>/s</code> switch · <code>/fork</code> another here · <code>/mode</code> settings
<code>/notify</code> loud·quiet·off · <code>/done</code> archives, always confirms

<b>Your team</b>
<code>/team</code> adds a group for several people · optional
<code>/invite id</code> adds someone · <code>/members</code> lists them
<code>/key</code> (privately) sets your Conductor key
<code>/use name</code> picks which team your DMs mean · <code>/leave</code> exits
<code>/export</code> downloads your data · <code>/privacy</code> explains it
<code>/voice on</code> needs your own <code>/voicekey</code> first
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


def homed_elsewhere(
    workspace: WorkspaceRow | None, seat: tuple[int | None, int] | Route
) -> tuple[int, int] | None:
    """``(chat_id, topic_id)`` of the room this workspace lives in, if not here.

    ``/s`` binds a session to the seat it is run from, which is right for the
    linear DM it was written for and wrong the moment a workspace has a topic:
    the bind re-addresses the transcript, so the topic the replies had been
    landing in goes quiet and the root starts speaking for a room it is not.
    A workspace with a room of its own is *opened*, never switched to.

    ``None`` means there is nothing to open — no topic, or the caller is
    already standing in it.
    """
    if workspace is None or not workspace.topic_id or workspace.chat_id is None:
        return None
    key = seat.key if isinstance(seat, Route) else seat
    home = (workspace.chat_id, workspace.topic_id)
    return None if home == key else home


@router.message(Command("help", "start"))
async def help_command(
    message: Message,
    state: FSMContext,
    tenant: TenantContext | None = None,
) -> None:
    await abandon_wizard(state)
    await tell(
        message,
        _HELP,
        reply_markup=home_keyboard() if _launchable(message, tenant) else None,
    )


def _launchable(message: Message, tenant: TenantContext | None) -> bool:
    """Whether handing this chat the launcher would give it working buttons.

    Two ways it would not, and both were reachable:

    * **A group.** Telegram shows a reply keyboard to everyone in it, and the
      launcher is a personal control. A group's General is a real, readable
      room, so there is nothing there for it to solve.
    * **Nobody yet.** ``/start`` and ``/help`` are in ``UNRESOLVED_COMMANDS``, so
      a stranger reaches them with no tenant at all — and their presses, being
      plain text rather than an entry command, are dropped in silence *and*
      page the owners with a stranger notice. A team that has not stored a key
      is the same shape one step later: both buttons need the Conductor client.
      ``active`` is exactly "the key is in", set by ``/key``.
    """
    return (
        message.chat.type == "private"
        and tenant is not None
        and tenant.status == "active"
    )


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
                    name=human_name(str(row.get("workspace_name") or ""))
                    or str(row.get("session_title") or workspace_id[:8]),
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
    elsewhere: list[str] = []
    for row in sessions[:12]:
        label = (
            f"{status_icon(row.state)} {safe_title(row.title, row.id[:8])} "
            f"· {row.model or '?'}"
        )
        home = homed_elsewhere(workspaces.get(row.workspace_id or ""), route)
        if home is not None:
            # This session already has a room, and it is not this one. Binding
            # it here would re-address its transcript to this seat and leave its
            # topic silent — the opposite of what "switch" promises. Offer the
            # way in instead; a DM topic has no link syntax, so it gets named.
            target = jump_url(*home)
            if target:
                rows.append([url_button(label, target)])
            else:
                elsewhere.append(label)
            continue
        current = "✓ " if row.id == route.session_id else ""
        rows.append(
            [
                button(
                    f"{current}{label}",
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
    lines = ["<b>Switch session</b>" if rows else "<b>Open a task</b>"]
    if elsewhere:
        lines.append("<i>These have their own topic — open it from the list:</i>")
        lines.extend(f"· {escape(name)}" for name in elsewhere)
    await tell(
        message,
        "\n".join(lines),
        reply_markup=keyboard(rows) if rows else None,
    )


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
    seat = (ticket.chat_id or query.from_user.id, ticket.thread_id)
    if ticket.thread_id != 0:
        chat = await chats_repo.get(database, *seat)
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
    # The button should never have been a switch button (see `/s`), but a stale
    # one minted before the workspace got its topic still resolves. Refuse
    # rather than silently move replies out of the room somebody is reading.
    home = await workspaces_repo.get(database, session.workspace_id or "")
    if homed_elsewhere(home, seat) is not None:
        await query.answer(
            "That task has its own topic. Open it there.", show_alert=True
        )
        return
    await sessions_repo.bind(
        database,
        session.id,
        chat_id=seat[0],
        thread_id=ticket.thread_id,
    )
    await chats_repo.bind(
        database,
        seat[0],
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
    # The topic now points at a session that has never run, so whatever the
    # previous one left behind — most visibly a ✅ from its last finished turn —
    # is now a claim about work that does not exist. The title stays correct
    # (a fork shares the workspace, so `project/branch` is unchanged); only the
    # state marker is stale.
    if message.bot is not None:
        await apply_marker(message.bot, database, route.workspace_id, TopicMarker.IDLE)
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
            # Rename before closing. Closing alone left whatever prefix the
            # topic last had — a swept topic could sit in the list reading
            # "⚙️ working" forever, describing a session nobody is running.
            await apply_marker(message.bot, database, row.id, TopicMarker.ARCHIVED)
            await message.bot.close_forum_topic(
                chat_id=row.chat_id, message_thread_id=row.topic_id
            )
        except Exception:
            continue
        closed += 1
    await tell(message, f"Tidy: {closed} topics closed.")
