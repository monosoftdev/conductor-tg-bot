"""The six daily-loop commands."""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ctb import signals
from ctb.bot.app import register_router
from ctb.bot.handlers.adopt import adopt_button
from ctb.bot.handlers.common import (
    abandon_wizard,
    command_text,
    create_and_bind,
    created_card,
    react_received,
    request_cancel,
    require_session,
    resolve_new_request,
    safe_title,
    short_error,
    tell,
    workspace_name,
)
from ctb.bot.handlers.topics import (
    close_topic,
    edit_html,
    human_name,
    jump_url,
    resolve_client,
    resolve_db,
    send_html,
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
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
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
#: One row per **session**, so several rows can share a workspace. The limit is
#: deliberately larger than :data:`BOARD_VISIBLE`: it is spent on sessions and
#: :func:`board_rows` then collapses them to workspaces, so a chatty workspace
#: with a dozen sessions must not push every other workspace off the board.
_BOARD_SQL: Final = """
SELECT session_id, workspace_id, session_title, workspace_name, workspace_state,
       model, transcript_updated_at
  FROM session_transcripts_view
 WHERE workspace_state NOT IN ('archived', 'deleted')
 ORDER BY transcript_updated_at DESC
 LIMIT 60
""".strip()


def status_icon(value: str | TurnState | None) -> str:
    """The glyph for a session state, from the one shared vocabulary.

    ``/board``, the pinned card and the topic prefix all answer "what is this
    session doing" and used to answer it with different glyphs. They now read
    from :mod:`ctb.signals`.
    """
    state = str(value or "").casefold()
    if any(word in state for word in ("error", "dead", "failed")):
        return signals.ERROR
    if any(word in state for word in ("working", "running", "draining")):
        return signals.WORKING
    if any(word in state for word in ("queued", "waking", "initializ")):
        return signals.WAITING
    if "cancell" in state or "stopp" in state:
        return signals.CANCELLED
    if "sleep" in state:
        return signals.SLEEPING
    return signals.DONE


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
            tenant=tenant,
            state=state,
            db=db,
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
    # The topic's own card repeats "queued" two seconds later; this bubble only
    # has to say where that topic is — and nothing at all when it is right here.
    # `route.thread_id`, not the raw `message_thread_id` — the router folds a
    # forum's General back to 0 and fills in the thread Telegram omits for a DM
    # topic, and the sibling callers in the wizard already read it from there.
    text, markup = created_card(message.chat.id, created, from_thread=route.thread_id)
    await tell(message, text, reply_markup=markup)


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
        name = human_name(str(row.get("workspace_name") or "")) or str(
            row.get("session_title") or wid[:8]
        )
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
    # "live" said nothing about what was being counted, which is how a board of
    # two workspaces came to announce "4 live" and look plausible.
    lines = [f"<b>{len(rows)} workspace{'' if len(rows) == 1 else 's'}</b>"]
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
        seen: set[str] = set()
        for raw in result.rows:
            item = dict(raw)
            # The view has one row per *session*. A workspace with three
            # sessions is still one workspace, one topic and one button — left
            # alone this counted rows and reported "4 live" over two
            # workspaces, under three identical buttons that all jumped to the
            # same topic. Rows arrive newest-first, so the first one seen for a
            # workspace is the one worth showing. A row with no workspace id
            # cannot be collapsed and is kept as-is.
            workspace_id = str(item.get("workspace_id") or "")
            if workspace_id:
                if workspace_id in seen:
                    continue
                seen.add(workspace_id)
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


def row_name(row: dict[str, object]) -> str:
    workspace_id = str(row.get("workspace_id") or "")
    return human_name(str(row.get("workspace_name") or "")) or str(
        row.get("session_title") or workspace_id[:8]
    )


def adoptable(
    board: list[dict[str, object]],
    local: Collection[WorkspaceRow],
    *,
    query: str = "",
    exclude: Collection[str] = (),
    limit: int = ADOPTABLE_SCAN,
) -> list[dict[str, object]]:
    """Workspaces with no room in this chat yet, from data already fetched.

    Pure, and given both sources on purpose. It used to re-read every candidate
    with ``workspaces_repo.get`` inside the loop — one query per row to answer a
    question one query answers for all of them.

    The two sources are a **union**, not a filter of one by the other.
    ``session_transcripts_view`` has a row only once a session has said
    something, so a workspace opened on the laptop a minute ago is missing from
    it — and it is exactly the one somebody reaches for ``/attach`` to find.
    """
    homed = {row.id for row in local if row.chat_id is not None and row.topic_id}
    listed = {str(row.get("workspace_id") or "") for row in board}
    candidates = [*board]
    candidates.extend(
        {
            "workspace_id": row.id,
            "workspace_name": row.name or "",
            "workspace_state": row.status or "unknown",
            "display_state": row.status or "unknown",
            "model": row.model or "",
        }
        for row in local
        if row.id not in listed and row.id not in homed
    )
    needle = query.strip().casefold()
    seen = set(exclude)
    out: list[dict[str, object]] = []
    for row in candidates:
        workspace_id = str(row.get("workspace_id") or "")
        if not workspace_id or workspace_id in seen or workspace_id in homed:
            continue
        name = row_name(row)
        if (
            needle
            and needle not in name.casefold()
            and needle not in workspace_id.casefold()
        ):
            continue
        seen.add(workspace_id)
        out.append(row)
        if len(out) >= limit:
            break
    return out


async def adoptable_rows(
    database: Database,
    client: ConductorClient,
    *,
    query: str = "",
    exclude: Collection[str] = (),
    limit: int = ADOPTABLE_SCAN,
) -> list[dict[str, object]]:
    """:func:`adoptable`, fetching its own inputs. Never raises.

    ``/s`` still lists the topics it knows when ``POST /v0/sql`` is unavailable
    — :func:`board_rows` already falls back to the local cache, and anything
    past that degrades to "no suggestions".
    """
    try:
        board = await board_rows(database, client)
        local = await workspaces_repo.list_all(database)
    except Exception:
        return []
    return adoptable(board, local, query=query, exclude=exclude, limit=limit)


@router.message(Command("attach"))
async def attach_workspace(
    message: Message,
    tenant: TenantContext,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
    query: str | None = None,
) -> None:
    """Open a cloud workspace created outside Telegram.

    ``query`` is explicit because the launcher button calls this directly, and
    a reply keyboard sends its *label* as an ordinary message: ``command_text``
    would read "📎 Attach existing" as ``Attach existing`` and filter every
    workspace out, so the one button that exists to find them found none.
    """
    await abandon_wizard(state)
    database = resolve_db(db)
    if query is None:
        query = command_text(message)
    try:
        client = tenant.client
    except Exception as exc:
        # `tenant.client` raises for a team with no key stored. Evaluated here,
        # inside the guard, because the launcher button puts this one tap away
        # from somebody who has not run `/key` yet — and an unguarded raise
        # answers "⚠️ Request failed" instead of naming the missing step.
        await tell(message, f"Cannot look: {escape(short_error(exc))}", silent=False)
        return
    # One fetch of each source, shared by the list and by the empty-state line.
    # `nothing_to_attach` used to re-run `board_rows` — a second `POST /v0/sql`
    # and a second full scan, on the path that already found nothing.
    board = await board_rows(database, client)
    local = await workspaces_repo.list_all(database)
    rows = adoptable(board, local, query=query, limit=BOARD_VISIBLE)
    if not rows:
        await tell(message, nothing_to_attach(local, query))
        return
    buttons = []
    for row in rows:
        workspace_id = str(row.get("workspace_id") or "")
        name = row_name(row)
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


def nothing_to_attach(local: Collection[WorkspaceRow], query: str) -> str:
    """Why ``/attach`` has nothing to offer — there are three reasons.

    "No unattached cloud workspace matches" was every one of them at once, and
    the most common by far is the *least* alarming: everything you have is
    already open here, one thread each. Reading that as "the bot cannot see my
    workspaces" is what makes ``/attach`` look broken when it is working.

    Counted over rooms this chat actually holds, never over "workspaces that
    exist" — a row with no topic is one :func:`adoptable` would have offered, so
    reaching this line means it is not there, and calling it "already open"
    would point somebody at a thread that does not exist.
    """
    if query:
        return f"Nothing unattached matches <b>{escape(query)}</b>."
    total = sum(1 for row in local if row.chat_id is not None and row.topic_id)
    if not total:
        return "No workspaces in Conductor yet · describe a task here to start one."
    plural = "" if total == 1 else "s"
    return (
        f"All {total} workspace{plural} already open here · "
        "pick one from the thread list, or <code>/board</code>."
    )


def board_lines(rows: list[dict[str, object]]) -> list[str]:
    """Text-only board. ``/board`` uses buttons; this is the voice path's copy."""
    lines = [f"<b>Board · {len(rows)} workspace{'' if len(rows) == 1 else 's'}</b>"]
    for row in rows[:BOARD_VISIBLE]:
        wid = str(row.get("workspace_id") or "")
        name = human_name(str(row.get("workspace_name") or "")) or str(
            row.get("session_title") or wid[:8]
        )
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


#: What the DM root says when a line of work arrives in it. The root is not a
#: seat once `/new` has moved the work into topics — it is the cockpit, and a
#: cockpit never prompts on its own (PLAN §Safety rails). Saying so beats the
#: "No session here" it used to answer, which was true and useless: the session
#: existed, it just lived one topic away.
DM_COCKPIT_HINT: Final = (
    "This is the main chat · it doesn't run prompts.\n"
    "Send it to a task below, or <code>/new</code> to start one."
)


async def cockpit_target(db: Database) -> tuple[str, str] | None:
    """``(session_id, label)`` for the seat a cockpit's one button points at.

    The most recently prompted bound seat — the thing "send it there" means
    when the person did not say where. ``None`` when nothing has ever run, in
    which case there is no cockpit to be and the caller says so plainly.
    """
    rows = sorted(
        (row for row in await chats_repo.list_bound(db) if row.last_prompt_at),
        key=lambda row: row.last_prompt_at or 0,
        reverse=True,
    )
    for row in rows:
        if not row.session_id:
            continue
        session = await sessions_repo.get(db, row.session_id)
        label = safe_title(session.title if session else None, row.session_id[:8])
        return row.session_id, label
    return None


async def cockpit_markup(
    db: Database,
    text: str,
    *,
    nonces: NonceStore,
    user_id: int | None,
    chat_id: int,
    thread_id: int,
) -> InlineKeyboardMarkup | None:
    """The single-use "Send to …" button a cockpit offers instead of prompting.

    Shared by the typed and the spoken path on purpose: the two surfaces
    resolving "where would this have gone?" differently is the divergence that
    once sent a dictated prompt to ``/find``.

    :data:`CONTROL_TTL_S`, not the 60-second default — this is a phone control
    on a line the owner may well come back to after reading the reply, not a
    destructive confirm.
    """
    target = await cockpit_target(db)
    if target is None:
        return None
    session_id, label = target
    return keyboard(
        [
            [
                button(
                    f"Send to {label}",
                    Action.SEND,
                    f"{session_id}\n{text}",
                    store=nonces,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    ttl=CONTROL_TTL_S,
                )
            ]
        ]
    )


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
        title = human_name(str(row.get("workspace_name") or "")) or str(
            row.get("session_title") or "session"
        )
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


async def _reply_beside(
    message: Message, html: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Answer a card *in the room the card is in*.

    ``Message.answer`` addresses the thread only when Telegram set
    ``is_topic_message``, which it does for a forum and — as
    :func:`ctb.bot.middleware.routing._thread_id` already documents — not for a
    topic in a private chat. So a reply to a card sitting in a workspace's DM
    topic lands in the DM root, which is the *New Chat* composer: the one seat
    that is not a room. Address the thread the card is in, explicitly.
    """
    if message.bot is None:  # pragma: no cover - a card always carries its bot
        return
    await send_html(
        message.bot,
        message.chat.id,
        html,
        thread_id=message.message_thread_id or NO_THREAD_ID,
        reply_markup=reply_markup,
    )


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
                await _reply_beside(
                    query.message, f"Archive failed: {escape(short_error(exc))}"
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
            await _reply_beside(
                query.message, f"Archived <b>{escape(ticket.label)}</b>."
            )


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
        await _reply_beside(query.message, ARCHIVE_CONSEQUENCE, reply_markup=markup)


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
