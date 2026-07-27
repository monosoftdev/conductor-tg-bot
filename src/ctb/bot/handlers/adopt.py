"""Open a workspace that already exists in Conductor as a topic here.

``/new`` creates a workspace *and* its topic in one motion. A workspace created
on the laptop has no topic, so ``/board`` could only print its name — visible,
unreachable. This module is the missing link: it opens a topic for an existing
remote workspace, binds its most recently active session to that topic, and
points the transcript cursor at the end so the phone mirrors what happens
**next**.

Two rules here are load-bearing.

**The cursor is seeded by :func:`ctb.turn.cursor.seek_to_end` and nothing else.**
Adoption never backfills history through the delivery path. ``seek_to_end``
refuses to move the cursor of an already-seeded session, which is exactly what
makes a second adoption safe.

**The "last exchange" card is a read-only snapshot.** It is rendered here and
sent with :func:`ctb.bot.handlers.topics.send_html` — *outside* the
``deliveries`` outbox. It writes no ``deliveries`` row, moves no cursor and
marks nothing as seen. Reusing :func:`ctb.turn.cursor.plan_deliveries` for it
would look like a simplification and would be a correctness bug: those rows are
the delivery ledger, and a message that is both "already delivered" and "behind
the cursor" can never be delivered again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import short_error
from ctb.bot.handlers.topics import (
    attach_topic,
    discard_topic,
    jump_url,
    marker_for,
    require_topic,
    resolve_client,
    resolve_db,
    send_html,
    topic_label,
    topic_title,
)
from ctb.bot.keyboards import (
    CONTROL_TTL_S,
    Action,
    Cb,
    NonceError,
    NonceStore,
    button,
    keyboard,
    resolve,
    url_button,
)
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import Session, TranscriptMessage, Workspace
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.workspaces import WorkspaceRow
from ctb.delivery.render.html import escape
from ctb.logging import get_logger
from ctb.turn import cursor
from ctb.turn.state import TopicMarker

__all__ = [
    "ADOPT_PREFIX",
    "EXCHANGE_CHARS",
    "SESSION_SCAN",
    "TAIL_WINDOW",
    "AdoptError",
    "AdoptResult",
    "adopt_button",
    "adopt_workspace",
    "snapshot_card",
]

log = get_logger(__name__)
router = Router(name=__name__)
register_router(router, order=25)

#: The label that turns a listed workspace into a tappable one.
ADOPT_PREFIX: Final = "+ Open "
#: Trailing messages read for the snapshot card. One request, bounded, and wide
#: enough that a tool-heavy turn does not push the prompt out of the window.
TAIL_WINDOW: Final = 24
#: Characters per side of the last exchange. Two phone paragraphs, no more.
EXCHANGE_CHARS: Final = 220
#: Enough of ``preview_text`` to survive cutting the mobile output contract off
#: a Telegram-sent prompt before clipping to :data:`EXCHANGE_CHARS`.
_SOURCE_CHARS: Final = 4_000
#: The contract ``common.augment_prompt`` appends to every Telegram prompt. It
#: is instruction, not content, so the snapshot cuts it. Pinned by a test
#: against ``MOBILE_REPLY_INSTRUCTION``.
_CONTRACT_MARK: Final = "OUTPUT CONTRACT"
#: Sessions listed to pick the newest one and to report how many there are.
SESSION_SCAN: Final = 20
SESSION_SCAN_PAGES: Final = 5

@dataclass(slots=True)
class _WorkspaceLock:
    lock: asyncio.Lock
    users: int = 0


_locks: dict[str, _WorkspaceLock] = {}
_TOPIC_GONE: Final = ("topic not found", "message thread not found", "thread not found")


class AdoptError(RuntimeError):
    """Adoption refused, with one phone line saying why."""


@dataclass(frozen=True, slots=True)
class AdoptResult:
    """What :func:`adopt_workspace` did."""

    workspace_id: str
    session_id: str
    thread_id: int
    name: str
    deep_link: str | None = None
    #: The workspace already had a live topic in this chat; nothing was created.
    already: bool = False
    #: Sessions the workspace has, so the reply can say which one was taken.
    sessions: int = 1
    #: The workspace was asleep when it was adopted.
    sleeping: bool = False


# ── the button ───────────────────────────────────────────────────────────────


def adopt_button(
    *,
    workspace_id: str,
    name: str,
    session_id: str | None = None,
    store: NonceStore | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    thread_id: int = NO_THREAD_ID,
) -> InlineKeyboardButton:
    """``+ Open api/fix-flaky`` — the one affordance a laptop workspace needs.

    Non-destructive, so no named two-tap confirm; single-use and time-boxed
    like every other safe control.
    """
    return button(
        f"{ADOPT_PREFIX}{name}",
        Action.ADOPT,
        f"{workspace_id}\n{session_id or ''}",
        store=store,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        ttl=CONTROL_TTL_S,
    )


# ── the snapshot card ────────────────────────────────────────────────────────


def snapshot_card(
    messages: Sequence[TranscriptMessage],
    *,
    session_title: str = "",
    session_count: int = 1,
    sleeping: bool = False,
) -> str:
    """The last exchange, as one card. **Read-only — see the module docstring.**

    Bounded by construction: two clipped lines plus at most three short ones,
    so it can never approach Telegram's 4096 limit and never needs chunking.
    """
    lines: list[str] = []
    if session_count > 1:
        lines.append(f"{session_count} sessions · <b>{escape(session_title)}</b>")
    if sleeping:
        lines.append("💤 Sleeping. A prompt may wake it — unverified.")
    prompt = next((m for m in reversed(messages) if m.is_user_echo), None)
    answer = cursor.pick_preview([m for m in messages if not m.is_user_echo])
    if prompt is not None:
        lines.append(f"👤 {escape(_gist(prompt))}")
    if answer is not None:
        lines.append(f"🤖 {escape(_gist(answer))}")
    lines.append(
        "<i>Snapshot · live from here</i>"
        if prompt is not None or answer is not None
        else "<i>Nothing yet · live from here</i>"
    )
    return "\n".join(lines)


def _gist(message: TranscriptMessage) -> str:
    return _clip(_without_contract(cursor.preview_text(message, limit=_SOURCE_CHARS)))


def _without_contract(text: str) -> str:
    """Drop the appended mobile output contract from an echoed prompt."""
    index = text.find(_CONTRACT_MARK)
    if index < 0:
        return text
    return text[:index].rstrip(" =\n\t").strip() or text


def _clip(text: str, limit: int = EXCHANGE_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + "…"


async def _tail_read(
    client: ConductorClient, session_id: str
) -> tuple[TranscriptMessage, ...]:
    """A bounded look at the tail for an already-seeded session.

    Read-only, like everything else behind the snapshot card: no cursor write,
    no ``deliveries`` row, no ``transcript_messages`` row.
    """
    last, offset, _ = await cursor.find_last_message(client, session_id)
    if last is None:
        return ()
    start = max(0, offset + 1 - TAIL_WINDOW)
    page = await client.get_messages(session_id, offset=start, limit=TAIL_WINDOW)
    return tuple(page.data)


# ── adoption ─────────────────────────────────────────────────────────────────


async def adopt_workspace(
    *,
    bot: Bot,
    db: Database,
    client: ConductorClient,
    chat_id: int,
    chat_type: str,
    workspace_id: str,
    session_hint: str | None = None,
) -> AdoptResult:
    """Bind an existing remote workspace to a topic in this chat.

    Idempotent: a workspace that already owns a live topic here is jumped to,
    never duplicated. A deleted topic is replaced; a binding in another chat
    is refused so opening it here cannot silently break the original route.
    """
    entry = _locks.setdefault(workspace_id, _WorkspaceLock(asyncio.Lock()))
    # Increment before the first await. A waiter therefore keeps the entry in
    # the registry, closing the release/pop/new-lock race with a third tap.
    entry.users += 1
    try:
        async with entry.lock:
            return await _adopt_workspace(
                bot=bot,
                db=db,
                client=client,
                chat_id=chat_id,
                chat_type=chat_type,
                workspace_id=workspace_id,
                session_hint=session_hint,
            )
    finally:
        entry.users -= 1
        if entry.users == 0 and _locks.get(workspace_id) is entry:
            _locks.pop(workspace_id, None)


async def _adopt_workspace(
    *,
    bot: Bot,
    db: Database,
    client: ConductorClient,
    chat_id: int,
    chat_type: str,
    workspace_id: str,
    session_hint: str | None,
) -> AdoptResult:
    local = await workspaces_repo.get(db, workspace_id)
    if (
        local is not None
        and local.topic_id
        and local.chat_id is not None
        and local.chat_id != chat_id
    ):
        raise AdoptError("Already connected in another Telegram chat.")
    workspace = await _describe(client, workspace_id)
    if workspace.status.value == "unknown":
        try:
            status = await client.get_workspace_status(workspace_id)
        except Exception as exc:
            raise AdoptError(f"Status unavailable · {short_error(exc)}") from exc
        workspace.status = status.status
        workspace.lifecycle_step = status.lifecycle_step
    if workspace.status.is_gone:
        raise AdoptError("Archived in Conductor. Restore it there first.")
    label = topic_label(workspace.name or workspace_id[:8], workspace.branch)
    marker = marker_for(workspace_status=workspace.status)

    remembered = _remembered_topic(local, chat_id) if chat_type != "private" else None
    existing_thread: int | None = None
    if remembered is not None and await _topic_exists(
        bot, chat_id, remembered, label, marker
    ):
        route = await chats_repo.get(db, chat_id, remembered)
        known = await sessions_repo.list_for_workspace(db, workspace_id)
        session_id = (
            route.session_id
            if route is not None and route.session_id
            else (known[0].id if known else "")
        )
        if session_id:
            # The existence probe also reconciled the visible title to the
            # current remote status. Persist the same marker, otherwise a
            # later transition can incorrectly skip the rename.
            async with db.transaction():
                await workspaces_repo.upsert(
                    db,
                    workspace.id,
                    project_id=workspace.project_id,
                    name=workspace.name,
                    repo_url=workspace.repository_url,
                    branch=workspace.branch,
                    agent=workspace.agent,
                    model=workspace.model,
                    effort=workspace.effort,
                    deep_link=workspace.deep_link,
                    status=workspace.status.value,
                    lifecycle_step=workspace.lifecycle_step,
                    chat_id=chat_id,
                    topic_id=remembered,
                    topic_name=label,
                )
                await workspaces_repo.set_topic_marker(db, workspace.id, marker.value)
            return AdoptResult(
                workspace_id=workspace_id,
                session_id=session_id,
                thread_id=remembered,
                name=label,
                deep_link=workspace.deep_link or (local.deep_link if local else None),
                already=True,
                sessions=max(1, len(known)),
                sleeping=workspace.status.is_waking,
            )
        # A workspace row can survive a partial write even when its chat/session
        # route did not. Repair that exact topic instead of creating a sibling.
        existing_thread = remembered

    remote_sessions = await _all_sessions(client, workspace_id)
    if not remote_sessions:
        raise AdoptError("No sessions in this workspace yet.")
    chosen = _pick_session(remote_sessions, session_hint)

    # The topic is created before any state is written, for the same reason
    # ``common.create_and_bind_input`` does it: a half-written binding pointing
    # at a topic that does not exist is worse than a command that failed.
    thread_id = NO_THREAD_ID
    fresh = False
    if chat_type != "private":
        if existing_thread is not None:
            thread_id = existing_thread
        else:
            thread_id = await require_topic(bot, chat_id, label, marker=marker)
            fresh = True

    prior_session = await sessions_repo.get(db, chosen.id)
    try:
        seek = await _bind(
            db,
            client=client,
            workspace=workspace,
            session=chosen,
            all_sessions=remote_sessions,
            chat_id=chat_id,
            thread_id=thread_id,
            label=label,
            marker=marker,
            fresh=fresh,
        )
    except BaseException:
        if fresh:
            await discard_topic(bot, chat_id, thread_id)
        if prior_session is None:
            await sessions_repo.delete(db, chosen.id)
        if local is None:
            await workspaces_repo.delete(db, workspace_id)
        raise
    tail = await _tail_read(client, chosen.id) if seek.skipped else seek.tail
    card = snapshot_card(
        tail,
        session_title=_session_title(chosen),
        session_count=len(remote_sessions),
        sleeping=workspace.status.is_waking,
    )

    await send_html(bot, chat_id, card, thread_id=thread_id, silent=True)

    log.info(
        "adopt.bound",
        workspace_id=workspace_id,
        session_id=chosen.id,
        thread_id=thread_id,
        created_topic=fresh,
    )
    return AdoptResult(
        workspace_id=workspace_id,
        session_id=chosen.id,
        thread_id=thread_id,
        name=label,
        deep_link=workspace.deep_link,
        already=False,
        sessions=len(remote_sessions),
        sleeping=workspace.status.is_waking,
    )


async def _describe(client: ConductorClient, workspace_id: str) -> Workspace:
    try:
        return await client.get_workspace(workspace_id)
    except Exception as exc:
        raise AdoptError(f"Workspace unavailable · {short_error(exc)}") from exc


async def _all_sessions(client: ConductorClient, workspace_id: str) -> list[Session]:
    sessions: list[Session] = []
    offset = 0
    for _ in range(SESSION_SCAN_PAGES):
        page = await client.list_workspace_sessions(
            workspace_id, limit=SESSION_SCAN, offset=offset
        )
        sessions.extend(page.data)
        if not page.has_more or not page.data:
            break
        offset += len(page.data)
    return sessions


async def _topic_exists(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    label: str,
    marker: TopicMarker,
) -> bool:
    """Only an explicit not-found response permits a replacement topic."""
    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=topic_title(marker, label),
        )
        return True
    except TelegramBadRequest as exc:
        detail = str(exc).casefold()
        if "not modified" in detail:
            return True
        if any(value in detail for value in _TOPIC_GONE):
            return False
        raise AdoptError(f"Topic check failed · {short_error(exc)}") from exc
    except TelegramAPIError as exc:
        # A network/server failure is ambiguous. Creating a sibling topic here
        # would turn a brief Telegram outage into permanent duplicate rooms.
        raise AdoptError(f"Topic check unavailable · {short_error(exc)}") from exc


def _pick_session(sessions: Sequence[Session], hint: str | None) -> Session:
    """The most recently active session — the view's answer, or the API's first.

    ``session_transcripts_view`` is ordered by ``transcript_updated_at``, so the
    id ``/board`` and ``/s`` carry is already "the newest". Fall back to the
    listing when there is no hint, or when the hint is not in this workspace.
    """
    if hint:
        for session in sessions:
            if session.id == hint:
                return session
    return sessions[0]


def _remembered_topic(row: WorkspaceRow | None, chat_id: int) -> int | None:
    """The topic this chat already owns for the workspace, if any."""
    if row is None or row.chat_id != chat_id or not row.topic_id:
        return None
    return row.topic_id


def _session_title(session: Session) -> str:
    return (
        " ".join((session.title or session.name or "").split())[:80] or session.id[:8]
    )


async def _bind(
    db: Database,
    *,
    client: ConductorClient,
    workspace: Workspace,
    session: Session,
    all_sessions: Sequence[Session],
    chat_id: int,
    thread_id: int,
    label: str,
    marker: TopicMarker,
    fresh: bool,
) -> cursor.SeekResult:
    """Write the binding, seeding the cursor between "known" and "polled".

    The session row is created **unbound**, seeded, and only then bound: the
    supervisor starts a poller for anything bound, and a poller that wins that
    race would seed the cursor itself and post its own first-bind preview.
    """
    await workspaces_repo.upsert(
        db,
        workspace.id,
        project_id=workspace.project_id,
        name=workspace.name,
        repo_url=workspace.repository_url,
        branch=workspace.branch,
        agent=workspace.agent,
        model=workspace.model,
        effort=workspace.effort,
        deep_link=workspace.deep_link,
        status=workspace.status.value,
    )
    await sessions_repo.upsert(
        db,
        session.id,
        workspace_id=workspace.id,
        title=_session_title(session),
        agent=session.agent or workspace.agent,
        model=session.model or workspace.model,
        effort=session.effort or workspace.effort,
        is_bound=False,
    )
    seek = await cursor.seek_to_end(client, db, session.id, preview_window=TAIL_WINDOW)
    # Everything after the remote seek is one local commit. A DB fault cannot
    # leave a topic pointing at a half-bound session.
    async with db.transaction():
        for other in all_sessions:
            if other.id == session.id:
                continue
            await sessions_repo.upsert(
                db,
                other.id,
                workspace_id=workspace.id,
                title=_session_title(other),
                agent=other.agent or workspace.agent,
                model=other.model or workspace.model,
                effort=other.effort or workspace.effort,
                is_bound=False,
            )
        if thread_id and fresh:
            await attach_topic(
                db,
                workspace_id=workspace.id,
                chat_id=chat_id,
                topic_id=thread_id,
                label=label,
                marker=marker,
            )
        elif thread_id:
            await workspaces_repo.bind_topic(
                db,
                workspace.id,
                chat_id=chat_id,
                topic_id=thread_id,
                topic_name=label,
            )
            await workspaces_repo.set_topic_marker(db, workspace.id, marker.value)
            await chats_repo.ensure(db, chat_id, thread_id, kind="topic")
        else:
            await workspaces_repo.upsert(db, workspace.id, chat_id=chat_id)
            await chats_repo.ensure(db, chat_id, NO_THREAD_ID, kind="dm")
        await sessions_repo.upsert(
            db,
            session.id,
            chat_id=chat_id,
            thread_id=thread_id,
            is_bound=True,
        )
        await chats_repo.bind(
            db,
            chat_id,
            thread_id,
            workspace_id=workspace.id,
            session_id=session.id,
            kind="topic" if thread_id else "dm",
        )
        await chats_repo.set_defaults(
            db,
            chat_id,
            thread_id,
            project_id=workspace.project_id,
            branch=workspace.branch,
            agent=session.agent or workspace.agent,
            model=session.model or workspace.model,
            effort=session.effort or workspace.effort,
        )
    return seek


# ── the callback ─────────────────────────────────────────────────────────────


@router.callback_query(Cb.filter(F.action == Action.ADOPT.value))
async def adopt_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.ADOPT, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    workspace_id, _, session_hint = ticket.target.partition("\n")
    if query.bot is None or not workspace_id:
        await query.answer("Expired. Run /board again.", show_alert=True)
        return
    chat_id = ticket.chat_id or query.from_user.id
    # Opening costs several API calls; answer first so Telegram stops spinning.
    await query.answer("Opening…")
    try:
        result = await adopt_workspace(
            bot=query.bot,
            db=resolve_db(db),
            client=resolve_client(client),
            chat_id=chat_id,
            chat_type=_chat_type(query, chat_id),
            workspace_id=workspace_id,
            session_hint=session_hint or None,
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
    target = jump_url(chat_id, result.thread_id)
    label = "Open topic" if target else "Open in Conductor"
    link = target or result.deep_link
    await send_html(
        query.bot,
        chat_id,
        ack_line(result),
        thread_id=ticket.thread_id,
        reply_markup=keyboard([[url_button(label, link)]]) if link else None,
        silent=True,
    )


def ack_line(result: AdoptResult) -> str:
    """Two words and a name. The card in the topic carries the detail."""
    suffix = " · already open" if result.already else ""
    return f"→ <b>{escape(result.name)}</b>{suffix}"


def _chat_type(query: CallbackQuery, chat_id: int) -> str:
    message = query.message
    if isinstance(message, Message):
        return message.chat.type
    return "private" if chat_id > 0 else "supergroup"
