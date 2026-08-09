"""The six daily-loop commands."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ctb import signals
from ctb.bot.app import register_router
from ctb.bot.handlers.adopt import (
    SESSION_SCAN,
    ack_line,
    adopt_button,
    adopt_workspace,
)
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
    TopicRetirement,
    edit_html,
    human_name,
    jump_url,
    resolve_client,
    resolve_db,
    retire_topic,
    send_html,
)
from ctb.bot.keyboards import (
    CONTROL_TTL_S,
    PLAIN_STYLE,
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
from ctb.logging import get_logger
from ctb.turn.state import TurnState
from ctb.turn.supervisor import Supervisor

log = get_logger(__name__)

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
#: Sessions ``/board`` stage 2 lists before it says how many it hid.
BOARD_SESSIONS_VISIBLE: Final = 10
#: The confirm button already names the task. Say what tapping it does — and
#: the two cards must not read alike, because one of them takes the whole
#: workspace with it.
ARCHIVE_CONSEQUENCE: Final = (
    "Deletes this topic and everything said in it. "
    "The workspace stays restorable in Conductor."
)
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
    """Stage 1 of a two-stage picker: which workspace, then which session.

    A session is one Conductor chat, and a topic is now one per session — so
    "open a workspace" stopped being a single tap and became a question with two
    halves. The wording carries the whole feature: stage 1 says *see its
    sessions* and never says "open"; stage 2 says *open it in this chat* and
    never says "workspace" as the thing being chosen.
    """
    await abandon_wizard(state)
    database = resolve_db(db)
    rows = await board_rows(database, resolve_client(client, tenant))
    query = command_text(message)
    if query:
        needle = query.casefold()
        rows = [row for row in rows if needle in row_name(row).casefold()]
    if not rows:
        await tell(message, "No live workspaces." if not query else "No match.")
        return
    text, markup = board_stage1(
        rows,
        store=nonces,
        user_id=message.from_user.id if message.from_user else None,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id or NO_THREAD_ID,
    )
    await tell(message, text, reply_markup=markup)


def board_stage1(
    rows: list[dict[str, object]],
    *,
    store: NonceStore,
    user_id: int | None,
    chat_id: int,
    thread_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The workspace picker. **Every row behaves identically.**

    ``/board`` used to special-case a workspace with no room here into a
    ``+ Open …`` adopt button, so two rows of the same list did two different
    things and only one of them told you which. A laptop workspace and a local
    one both drill down now; the difference shows up in stage 2, as jump versus
    open, where there is room to say so. ``adopt_button`` stays in ``/attach``.
    """
    buttons = []
    for row in rows[:BOARD_VISIBLE]:
        workspace_id = str(row.get("workspace_id") or "")
        if not workspace_id:
            continue
        icon = status_icon(
            str(row.get("display_state") or row.get("workspace_state") or "")
        )
        count = _session_count(row)
        buttons.append(
            [
                button(
                    f"{icon} {row_name(row)} · {_sessions_suffix(count)}",
                    Action.BOARD_WS,
                    workspace_id,
                    store=store,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    ttl=CONTROL_TTL_S,
                )
            ]
        )
    # "live" said nothing about what was being counted, which is how a board of
    # two workspaces came to announce "4 live" and look plausible.
    lines = [f"<b>Workspaces · {len(rows)}</b>", "Tap one to see its sessions."]
    if len(rows) > BOARD_VISIBLE:
        lines.append(f"<i>+{len(rows) - BOARD_VISIBLE} more · /board name</i>")
    return "\n".join(lines), keyboard(buttons) if buttons else None


def _session_count(row: dict[str, object]) -> int:
    """``session_count`` off a view row, which is untyped at the wire."""
    value = row.get("session_count")
    return value if isinstance(value, int) else 0


def _sessions_suffix(count: int) -> str:
    if count <= 0:
        return "no sessions yet"
    return f"{count} session{'' if count == 1 else 's'}"


#: Which state a workspace wears in stage 1 when its sessions disagree. The
#: *most active* one, not the newest: a workspace with one working session reads
#: ⚙️ even when the session that spoke last has gone quiet.
_STATE_PRIORITY: Final[tuple[frozenset[TurnState], ...]] = (
    frozenset({TurnState.ERROR, TurnState.DEAD}),
    _ACTIVE_STATES,
)


def _busiest(states: list[TurnState]) -> TurnState | None:
    for tier in _STATE_PRIORITY:
        for state in states:
            if state in tier:
                return state
    return states[0] if states else None


def _local_board_rows(
    local: Collection[WorkspaceRow],
    sessions: dict[str, list[SessionRow]],
    *,
    exclude: Collection[str],
) -> list[dict[str, object]]:
    """Cached workspaces the transcript view has not heard of yet."""
    skip = frozenset(exclude)
    out: list[dict[str, object]] = []
    for row in local:
        if row.id in skip:
            continue
        known = sessions.get(row.id, [])
        busiest = _busiest([item.state for item in known])
        out.append(
            {
                "workspace_id": row.id,
                "workspace_name": workspace_name(row),
                "workspace_state": row.status or "unknown",
                "display_state": str(busiest)
                if busiest is not None
                else row.status or "unknown",
                "model": row.model or "",
                "session_count": len(known),
            }
        )
    return out


async def board_rows(
    database: Database, client: ConductorClient
) -> list[dict[str, object]]:
    """One row per **workspace**, with the session count stage 1 shows.

    The view has one row per session, and collapsing them used to throw the
    count away — the same fetch that answers "which workspaces" already answers
    "how many sessions each", for free.

    The local cache is **unioned in**, not used only as a fallback:
    ``session_transcripts_view`` has a row only once a session has said
    something, so a workspace opened a minute ago is missing from it — and
    ``/board`` is now the only way to reach one, so leaving it out would make it
    unreachable rather than merely late.
    """
    local_sessions = await sessions_repo.list_all(database)
    sessions_by_id = {row.id: row for row in local_sessions}
    local_by_workspace: dict[str, list[SessionRow]] = {}
    for session in local_sessions:
        if session.workspace_id:
            local_by_workspace.setdefault(session.workspace_id, []).append(session)
    rows: list[dict[str, object]]
    try:
        result = await client.sql(_BOARD_SQL)
        raw_rows = [dict(item) for item in result.rows]
        counts: dict[str, int] = {}
        states: dict[str, list[TurnState]] = {}
        for item in raw_rows:
            workspace_id = str(item.get("workspace_id") or "")
            if not workspace_id:
                continue
            counts[workspace_id] = counts.get(workspace_id, 0) + 1
            session = sessions_by_id.get(str(item.get("session_id") or ""))
            if session is not None:
                states.setdefault(workspace_id, []).append(session.state)
        rows = []
        seen: set[str] = set()
        for item in raw_rows:
            # A workspace with three sessions is still one workspace and one
            # button — left alone this counted rows and reported "4 live" over
            # two workspaces. Rows arrive newest-first, so the first one seen
            # for a workspace is the one worth showing. A row with no workspace
            # id cannot be collapsed and is kept as-is.
            workspace_id = str(item.get("workspace_id") or "")
            if workspace_id:
                if workspace_id in seen:
                    continue
                seen.add(workspace_id)
            busiest = _busiest(states.get(workspace_id, []))
            item["display_state"] = (
                str(busiest)
                if busiest is not None
                else str(item.get("workspace_state") or "")
            )
            item["session_count"] = max(
                counts.get(workspace_id, 1),
                len(local_by_workspace.get(workspace_id, ())),
            )
            rows.append(item)
        rows.extend(
            _local_board_rows(
                await workspaces_repo.list_all(database),
                local_by_workspace,
                exclude=seen,
            )
        )
    except Exception:
        local = await workspaces_repo.list_all(database)
        rows = []
        for row in local[:20]:
            known = local_by_workspace.get(row.id, [])
            busiest = _busiest([item.state for item in known])
            rows.append(
                {
                    "workspace_id": row.id,
                    "workspace_name": workspace_name(row),
                    "workspace_state": row.status or "unknown",
                    "display_state": str(busiest)
                    if busiest is not None
                    else row.status or "unknown",
                    "model": row.model or "",
                    "session_count": len(known),
                }
            )
    return rows


@dataclass(frozen=True, slots=True)
class BoardSession:
    """One stage-2 row: a Conductor chat, and whether it has a room here."""

    session_id: str
    title: str
    model: str
    state: str
    #: The thread its transcript is already being read in, in *this* chat.
    thread_id: int | None = None


async def board_sessions(
    database: Database, client: ConductorClient, workspace_id: str, *, chat_id: int
) -> list[BoardSession]:
    """The workspace's sessions, remote ∪ local, newest first.

    A **union**, not one filtered by the other: a session created on the laptop
    thirty seconds ago is in neither the transcript view nor the local cache
    reliably, and it is exactly the one somebody opened ``/board`` to reach.
    Degrades to the local rows alone when the API is unavailable — a stage that
    cannot list anything is worse than one listing what it already knows.
    """
    local = {
        row.id: row
        for row in await sessions_repo.list_for_workspace(database, workspace_id)
    }
    ordered: list[str] = []
    titles: dict[str, str] = {}
    models: dict[str, str] = {}
    try:
        page = await client.list_workspace_sessions(workspace_id, limit=SESSION_SCAN)
        for item in page.data:
            ordered.append(item.id)
            titles[item.id] = safe_title(item.title or item.name, item.id[:8])
            models[item.id] = item.model or ""
    except Exception:
        log.info("board.sessions_remote_unavailable", workspace_id=workspace_id)
    for session_id, row in local.items():
        if session_id not in titles:
            ordered.append(session_id)
        titles.setdefault(session_id, safe_session_title(row))
        models.setdefault(session_id, row.model or "")
    out: list[BoardSession] = []
    seen: set[str] = set()
    for session_id in ordered:
        if session_id in seen:
            continue
        seen.add(session_id)
        row = local.get(session_id)
        # `live`, not just `archived_at`: a session Conductor has 404ed is DEAD
        # locally and cannot be opened at all, so offering it means a room is
        # created and the open then fails on `seek_to_end`. The remote listing
        # drops a dead session; the local union is what puts it back.
        if row is not None and not row.live:
            continue
        out.append(
            BoardSession(
                session_id=session_id,
                title=titles.get(session_id) or session_id[:8],
                model=models.get(session_id, ""),
                state=str(row.state) if row is not None else "",
                thread_id=(
                    row.thread_id
                    if row is not None and row.has_room and row.chat_id == chat_id
                    else None
                ),
            )
        )
    return out


def board_stage2(
    workspace: WorkspaceRow | None,
    workspace_id: str,
    sessions: list[BoardSession],
    *,
    store: NonceStore,
    user_id: int | None,
    chat_id: int,
    thread_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The session picker, and the one card that says *open … in this chat*.

    A session that already has a room here is a plain link — Telegram just
    jumps, no ticket and no work. One without gets a single-use ticket, minted
    **here**, at stage-2 render: fanning them out at stage 1 would mint forty
    tickets for one tap.
    """
    name = workspace_name(workspace) if workspace is not None else workspace_id[:8]
    buttons: list[list[InlineKeyboardButton]] = []
    for item in sessions[:BOARD_SESSIONS_VISIBLE]:
        icon = status_icon(item.state or (workspace.status if workspace else None))
        label = f"{icon} {item.title}" + (f" · {item.model}" if item.model else "")
        target = jump_url(chat_id, item.thread_id) if item.thread_id else None
        if target:
            buttons.append([url_button(label, target)])
            continue
        buttons.append(
            [
                button(
                    label,
                    Action.BOARD_SESSION,
                    f"{workspace_id}\n{item.session_id}",
                    store=store,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    ttl=CONTROL_TTL_S,
                )
            ]
        )
    # Back is always last and names its destination rather than its direction,
    # so it is readable alone on a 40-character screen.
    buttons.append(
        [
            button(
                "« All workspaces",
                Action.BOARD_BACK,
                "",
                store=store,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                ttl=CONTROL_TTL_S,
                style=PLAIN_STYLE,
            )
        ]
    )
    if not sessions:
        lines = [f"<b>{escape(name)}</b>", f"No sessions in {escape(name)} yet."]
        return "\n".join(lines), keyboard(buttons)
    lines = [
        f"<b>{escape(name)} · {_sessions_suffix(len(sessions))}</b>",
        "Tap one to open it in this chat.",
    ]
    detail = " · ".join(
        part
        for part in (
            escape(workspace.branch or "") if workspace else "",
            f"{escape(workspace.agent or '?')}/{escape(workspace.model or '?')}"
            if workspace and (workspace.agent or workspace.model)
            else "",
        )
        if part
    )
    if detail:
        lines.append(detail)
    if len(sessions) > BOARD_SESSIONS_VISIBLE:
        hidden = len(sessions) - BOARD_SESSIONS_VISIBLE
        lines.append(f"<i>+{hidden} more · open one in Conductor</i>")
    return "\n".join(lines), keyboard(buttons)


async def _redraw(query: CallbackQuery, text: str, markup: Any) -> None:
    """Edit the card in place, or post a fresh one if it is gone.

    Never drop the tap: an edit fails when the card was deleted, and answering
    a tap with nothing at all is the one outcome a picker may not have.
    """
    if not isinstance(query.message, Message) or query.bot is None:
        return
    changed = await edit_html(
        query.bot,
        query.message.chat.id,
        query.message.message_id,
        text,
        reply_markup=markup,
    )
    if not changed:
        await _reply_beside(query.message, text, reply_markup=markup)


@router.callback_query(Cb.filter(F.action == Action.BOARD_WS.value))
async def board_workspace_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    tenant: TenantContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.BOARD_WS, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    await query.answer()
    database = resolve_db(db)
    conductor = resolve_client(client, tenant)
    chat_id = ticket.chat_id or query.from_user.id
    workspace = await workspaces_repo.get(database, ticket.target)
    sessions = await board_sessions(database, conductor, ticket.target, chat_id=chat_id)
    text, markup = board_stage2(
        workspace,
        ticket.target,
        sessions,
        store=nonces,
        user_id=query.from_user.id,
        chat_id=chat_id,
        thread_id=ticket.thread_id,
    )
    await _redraw(query, text, markup)


@router.callback_query(Cb.filter(F.action == Action.BOARD_BACK.value))
async def board_back_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    tenant: TenantContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.BOARD_BACK, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    await query.answer()
    database = resolve_db(db)
    rows = await board_rows(database, resolve_client(client, tenant))
    text, markup = board_stage1(
        rows,
        store=nonces,
        user_id=query.from_user.id,
        chat_id=ticket.chat_id or query.from_user.id,
        thread_id=ticket.thread_id,
    )
    await _redraw(query, text, markup)


@router.callback_query(Cb.filter(F.action == Action.BOARD_SESSION.value))
async def board_session_callback(
    query: CallbackQuery,
    route: Route,
    nonces: NonceStore,
    tenant: TenantContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    """Connect one session: open its room, or bind this seat if there is none.

    Both branches are :func:`adopt_workspace` scoped by ``session_hint``. With
    per-session rooms the hint stopped meaning "which session gets the
    workspace's room" and started meaning "which session to open", so tapping
    two sessions of one workspace opens two rooms. In a seat that cannot host
    topics it degrades to binding *that* seat — which is the one job ``/s``
    used to have and the reason it could be retired.
    """
    try:
        ticket = resolve(query, expect=Action.BOARD_SESSION, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    workspace_id, _, session_id = ticket.target.partition("\n")
    if query.bot is None or not workspace_id:
        await query.answer("Expired. Run /board again.", show_alert=True)
        return
    chat_id = ticket.chat_id or query.from_user.id
    await query.answer("Opening…")
    try:
        result = await adopt_workspace(
            bot=query.bot,
            db=resolve_db(db),
            client=resolve_client(client, tenant),
            chat_id=chat_id,
            chat_type=_callback_chat_type(query, chat_id),
            workspace_id=workspace_id,
            session_hint=session_id or None,
            claim_thread=route.claimable_thread,
        )
    except Exception as exc:
        await send_html(
            query.bot,
            chat_id,
            f"Open failed · {escape(short_error(exc))}",
            thread_id=ticket.thread_id,
            silent=False,
        )
        return
    if result.carded and result.thread_id == ticket.thread_id:
        # Opened into the very thread the button was tapped in, and the snapshot
        # card is on screen there. A button pointing at this room would not be.
        return
    target = jump_url(chat_id, result.thread_id)
    link = target or result.deep_link
    await send_html(
        query.bot,
        chat_id,
        ack_line(result, linkable=target is not None),
        thread_id=ticket.thread_id,
        reply_markup=keyboard(
            [[url_button("Open topic" if target else "Open in Conductor", link)]]
        )
        if link
        else None,
        silent=True,
    )


def _callback_chat_type(query: CallbackQuery, chat_id: int) -> str:
    message = query.message
    if isinstance(message, Message):
        return message.chat.type
    return "private" if chat_id > 0 else "supergroup"


def row_name(row: dict[str, object]) -> str:
    workspace_id = str(row.get("workspace_id") or "")
    return human_name(str(row.get("workspace_name") or "")) or str(
        row.get("session_title") or workspace_id[:8]
    )


def adoptable(
    board: list[dict[str, object]],
    local: Collection[WorkspaceRow],
    *,
    homed: Collection[str] = (),
    query: str = "",
    exclude: Collection[str] = (),
    limit: int = ADOPTABLE_SCAN,
) -> list[dict[str, object]]:
    """Workspaces with no room in this chat yet, from data already fetched.

    Pure, and given all three sources on purpose. It used to re-read every
    candidate with ``workspaces_repo.get`` inside the loop — one query per row
    to answer a question one query answers for all of them.

    The board and the local cache are a **union**, not a filter of one by the
    other. ``session_transcripts_view`` has a row only once a session has said
    something, so a workspace opened on the laptop a minute ago is missing from
    it — and it is exactly the one somebody reaches for ``/attach`` to find.

    ``homed`` is the workspace ids that have a *room* here. It is passed in
    rather than derived, because a room now belongs to a session and this
    function is given workspaces (:func:`homed_workspaces` computes it).
    """
    homed = frozenset(homed)
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


async def homed_workspaces(database: Database, chat_id: int | None = None) -> set[str]:
    """Workspace ids that already have at least one room, optionally in one chat.

    One query for the whole census. It used to be a column on ``workspaces``;
    with a room per session it is a fact about the session rows.
    """
    return {
        row.workspace_id
        for row in await sessions_repo.list_with_room(database)
        if row.workspace_id and (chat_id is None or row.chat_id == chat_id)
    }


async def adoptable_rows(
    database: Database,
    client: ConductorClient,
    *,
    query: str = "",
    exclude: Collection[str] = (),
    limit: int = ADOPTABLE_SCAN,
) -> list[dict[str, object]]:
    """:func:`adoptable`, fetching its own inputs. Never raises.

    Still lists the topics it knows when ``POST /v0/sql`` is unavailable —
    :func:`board_rows` already falls back to the local cache, and anything past
    that degrades to "no suggestions".
    """
    try:
        board = await board_rows(database, client)
        local = await workspaces_repo.list_all(database)
        homed = await homed_workspaces(database)
    except Exception:
        return []
    return adoptable(
        board, local, homed=homed, query=query, exclude=exclude, limit=limit
    )


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
    homed = await homed_workspaces(database, message.chat.id)
    rows = adoptable(board, local, homed=homed, query=query, limit=BOARD_VISIBLE)
    if not rows:
        await tell(message, nothing_to_attach(homed, query))
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


def nothing_to_attach(homed: Collection[str], query: str) -> str:
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
    total = len(set(homed))
    if not total:
        return "No workspaces in Conductor yet · describe a task here to start one."
    plural = "" if total == 1 else "s"
    return (
        f"All {total} workspace{plural} already open here · "
        "pick one from the thread list, or <code>/board</code>."
    )


def board_lines(rows: list[dict[str, object]]) -> list[str]:
    """Text-only board. ``/board`` uses buttons; this is the voice path's copy.

    It says nothing about tapping, because voice cannot tap: the way on from
    here is to name the workspace out loud.
    """
    lines = [f"<b>Workspaces · {len(rows)}</b>"]
    for row in rows[:BOARD_VISIBLE]:
        status = str(row.get("display_state") or row.get("workspace_state") or "?")
        count = _session_count(row)
        lines.append(
            f"{status_icon(status)} <b>{escape(row_name(row))}</b>"
            f" · {_sessions_suffix(count)}"
        )
    if len(rows) > BOARD_VISIBLE:
        hidden = len(rows) - BOARD_VISIBLE
        lines.append(f'<i>+{hidden} more · say "open &lt;name&gt;"</i>')
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

#: How many recent tasks the cockpit offers to send a stray line to. Three fits
#: under the message without pushing it off screen, and the fourth answer is
#: ``/board``.
COCKPIT_TARGETS: Final = 3


async def cockpit_targets(
    db: Database, *, limit: int = COCKPIT_TARGETS
) -> list[tuple[str, str]]:
    """``[(session_id, label), …]`` — the seats a cockpit can send to.

    The most recently prompted bound seats, newest first. It used to return
    exactly one, which was right when a chat held one or two tasks and a coin
    flip once it held ten: "send it there" meant "send it to whatever I touched
    last", and being wrong costs a prompt delivered to the wrong agent.

    Empty when nothing has ever run, in which case there is no cockpit to be and
    the caller says so plainly.
    """
    rows = sorted(
        (row for row in await chats_repo.list_bound(db) if row.last_prompt_at),
        key=lambda row: row.last_prompt_at or 0,
        reverse=True,
    )
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not row.session_id or row.session_id in seen:
            continue
        seen.add(row.session_id)
        session = await sessions_repo.get(db, row.session_id)
        label = safe_title(session.title if session else None, row.session_id[:8])
        out.append((row.session_id, label))
        if len(out) >= max(1, limit):
            break
    return out


async def cockpit_target(db: Database) -> tuple[str, str] | None:
    """The single newest target. Kept for callers that only want the head."""
    targets = await cockpit_targets(db, limit=1)
    return targets[0] if targets else None


async def cockpit_markup(
    db: Database,
    text: str,
    *,
    nonces: NonceStore,
    user_id: int | None,
    chat_id: int,
    thread_id: int,
) -> InlineKeyboardMarkup | None:
    """The single-use "Send to …" buttons a cockpit offers instead of prompting.

    Shared by the typed and the spoken path on purpose: the two surfaces
    resolving "where would this have gone?" differently is the divergence that
    once sent a dictated prompt to ``/find``.

    Up to :data:`COCKPIT_TARGETS` rows, newest first. One button was a guess
    dressed as a decision; three is the shortlist a person can actually
    recognise, and the first is still the one that used to be offered alone.

    :data:`CONTROL_TTL_S`, not the 60-second default — this is a phone control
    on a line the owner may well come back to after reading the reply, not a
    destructive confirm.
    """
    targets = await cockpit_targets(db)
    if not targets:
        return None
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
            for session_id, label in targets
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
                # The session, not the workspace: `/done` archives *this task*
                # and takes the workspace only when it was the last one.
                session.id,
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


async def live_siblings(
    database: Database,
    client: ConductorClient,
    session: SessionRow,
) -> int | None:
    """How many *other* live sessions this workspace has. ``None`` = unknown.

    Counted against the API, not the local cache: a chat somebody opened on the
    laptop is invisible to it, and the whole point of the number is deciding
    whether archiving the workspace throws away work nobody in this chat can
    see. When the count cannot be established the caller archives the session
    and leaves the workspace alone — leaving a container running is recoverable,
    archiving somebody's live laptop session from a phone is not.
    """
    if not session.workspace_id:
        return None
    try:
        page = await client.list_workspace_sessions(
            session.workspace_id, limit=SESSION_SCAN
        )
    except Exception:
        log.info("done.session_count_unavailable", session_id=session.id)
        return None
    archived_locally = {
        row.id
        for row in await sessions_repo.list_for_workspace(
            database, session.workspace_id
        )
        if row.archived_at is not None
    }
    return sum(
        1
        for item in page.data
        if item.id != session.id and item.id not in archived_locally
    )


def archive_consequence(name: str, others: int | None) -> str:
    """The two cards, and they must not read alike.

    The count is fetched *before* the card is drawn, so the second sentence is a
    fact rather than a guess — and the tap that follows re-reads it, because
    "last one" can stop being true while a phone is in a pocket.
    """
    if others is None:
        return (
            "Deletes this topic and everything said in it. "
            "The workspace stays — I could not check its other tasks."
        )
    if others <= 0:
        return (
            f"Last task in <b>{escape(name)}</b> — this archives the whole "
            "workspace too. Both stay restorable in Conductor."
        )
    plural = "" if others == 1 else "s"
    return (
        "Deletes this topic and everything said in it. "
        f"The workspace and its {others} other task{plural} stay."
    )


@router.message(Command("done"))
async def done(
    message: Message,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    """Archive *this task*, and the workspace only when it is the last one.

    It used to archive the workspace from inside a room, which under one room
    per session would throw away every sibling task to finish one.
    """
    await abandon_wizard(state)
    session = route.session
    if session is None:
        await tell(
            message,
            "Run this inside a task, or <code>/board</code> to pick one.",
        )
        return
    database = resolve_db(db)
    workspace = (
        await workspaces_repo.get(database, session.workspace_id)
        if session.workspace_id
        else None
    )
    others = await live_siblings(database, resolve_client(client, tenant), session)
    markup = confirm_keyboard(
        Action.ARCHIVE,
        session.id,
        safe_session_title(session),
        verb="Archive",
        store=nonces,
        user_id=message.from_user.id if message.from_user else None,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id or 0,
    )
    # The button below already reads "Archive <name>", and the topic title bar
    # reads it a third time. Spend the line on the consequence instead.
    await tell(
        message,
        archive_consequence(
            workspace_name(workspace) if workspace else session.id[:8], others
        ),
        reply_markup=markup,
    )


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
    # The button's label is ``Archive <name>``; saying it back whole would read
    # "Archived Archive fix flaky". The row is the name's only honest source.
    name = ticket.label
    took_workspace = False
    try:
        if query.bot is None:
            raise RuntimeError("Telegram bot is not bound to callback")
        session = await _archive_target(database, ticket.target)
        if session is None:
            raise RuntimeError("Task is no longer available.")
        name = safe_session_title(session)
        conductor = resolve_client(client, tenant)
        # Remote archive first, room deletion last. A deleted room with a live
        # session behind it is a task you can no longer reach; a live room with
        # an archived session behind it says so on its next tick.
        await conductor.archive_session(session.id)
        others = await live_siblings(database, conductor, session)
        if others is not None and others <= 0 and session.workspace_id:
            await conductor.archive_workspace(session.workspace_id)
            await workspaces_repo.mark_archived(database, session.workspace_id)
            took_workspace = True
        retirement = await retire_topic(query.bot, database, session.id)
        await sessions_repo.mark_archived(database, session.id)
        # **The archived session's own room**, not the seat the button was
        # minted in. They are the same seat for a `/done` typed in the room —
        # and a different one whenever the target came from somewhere else: a
        # `/done` sent as a reply routes to the replied-to session
        # (`Route.via_reply`), and the status card's Archive can be tapped from
        # a card that outlived its room. Unbinding the ticket's seat then
        # detached the room the tap was made in and left the archived session
        # still routing in its own.
        # And only while it is *this* task's: a room ``room_gone`` already
        # detached leaves the session addressed at the chat's seat, which the
        # linear DM legitimately shares. Unbinding what the row actually routes
        # to is the only version that cannot take a bystander with it.
        if session.chat_id is not None:
            room = await chats_repo.get(database, session.chat_id, session.thread_id)
            if room is not None and room.session_id == session.id:
                await chats_repo.unbind(database, session.chat_id, session.thread_id)
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
    if retirement is TopicRetirement.DELETED:
        # The card lived in the topic, so it went with it. Editing a message in
        # a deleted thread fails, and the fallback would post the receipt into
        # the chat root — a line about a room nobody can look at any more. The
        # room disappearing off the list *is* the confirmation.
        #
        # Unless the *workspace* went too: that is news beyond this room, and
        # there is no room left that could carry it.
        if took_workspace and query.bot is not None:
            await send_html(
                query.bot,
                ticket.chat_id or query.from_user.id,
                f"🗄 Archived <b>{escape(name)}</b> · the workspace was its last task.",
                thread_id=NO_THREAD_ID,
                silent=True,
            )
        return
    if isinstance(query.message, Message) and query.bot is not None:
        note = " Topic left open." if retirement is TopicRetirement.FAILED else ""
        if took_workspace:
            note += " Workspace archived too."
        changed = await edit_html(
            query.bot,
            query.message.chat.id,
            query.message.message_id,
            f"✓ Archived <b>{escape(name)}</b>.{escape(note)}",
            reply_markup=None,
        )
        if not changed:
            await _reply_beside(
                query.message, f"Archived <b>{escape(name)}</b>.{escape(note)}"
            )


async def _archive_target(database: Database, target: str) -> SessionRow | None:
    """The session an Archive ticket means.

    Tickets minted before this release carry a *workspace* id — the status card
    and ``/mode`` both targeted one — so a button that outlived the deploy
    resolves to that workspace's bound session rather than answering "gone".
    """
    session = await sessions_repo.get(database, target)
    if session is not None:
        return session
    candidates = await sessions_repo.list_for_workspace(database, target)
    bound = [row for row in candidates if row.is_bound]
    pool = bound or candidates
    return max(pool, key=lambda row: row.updated_at) if pool else None


@router.callback_query(Cb.filter(F.action == Action.ARCHIVE_REQUEST.value))
async def request_archive(
    query: CallbackQuery,
    nonces: NonceStore,
    tenant: TenantContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    """Turn status-card Archive into the same named two-tap flow as /done."""
    try:
        ticket = resolve(query, expect=Action.ARCHIVE_REQUEST, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    database = resolve_db(db)
    session = await _archive_target(database, ticket.target)
    if session is None:
        await query.answer("Task is gone.", show_alert=True)
        return
    workspace = (
        await workspaces_repo.get(database, session.workspace_id)
        if session.workspace_id
        else None
    )
    others = await live_siblings(database, resolve_client(client, tenant), session)
    markup = confirm_keyboard(
        Action.ARCHIVE,
        session.id,
        safe_session_title(session),
        verb="Archive",
        store=nonces,
        user_id=query.from_user.id,
        chat_id=ticket.chat_id,
        thread_id=ticket.thread_id,
    )
    await query.answer()
    if isinstance(query.message, Message):
        await _reply_beside(
            query.message,
            archive_consequence(
                workspace_name(workspace) if workspace else session.id[:8], others
            ),
            reply_markup=markup,
        )


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
