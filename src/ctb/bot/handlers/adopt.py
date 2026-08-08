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
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import note_linear_seat, short_error
from ctb.bot.handlers.topics import (
    Claim,
    TopicCreateError,
    attach_topic,
    claim_topic,
    discard_topic,
    dm_topic_support,
    human_name,
    jump_url,
    marker_for,
    require_topic,
    resolve_client,
    resolve_db,
    room_label,
    send_html,
    topic_label,
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
from ctb.bot.middleware.routing import Route
from ctb.bot.middleware.tenancy import TenantContext
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import Session, TranscriptMessage, Workspace
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.sessions import SessionRow
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
    #: The snapshot card actually landed in :attr:`thread_id`. ``send_html``
    #: never raises, so without this the caller can only *assume* the room has
    #: something in it — and an assumption is not allowed to suppress a reply.
    carded: bool = False


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
    asked = _gist(prompt) if prompt is not None else ""
    replied = _gist(answer) if answer is not None else ""
    if asked:
        lines.append(f"👤 {escape(asked)}")
    if replied:
        lines.append(f"🤖 {escape(replied)}")
    # An empty gist means the tail held nothing sayable — a lifecycle event, a
    # shape we do not know. Say that, rather than printing a bare "🤖".
    lines.append(
        "<i>Snapshot · live from here</i>"
        if asked or replied
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
    claim_thread: int = NO_THREAD_ID,
) -> AdoptResult:
    """Bind an existing remote workspace to a topic in this chat.

    Idempotent: a workspace that already owns a live topic here is jumped to,
    never duplicated. A deleted topic is replaced; a binding in another chat
    is refused so opening it here cannot silently break the original route.

    ``claim_thread`` is an empty thread this adoption may move into instead of
    opening one — see :attr:`ctb.bot.middleware.routing.Route.claimable_thread`.
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
                claim_thread=claim_thread,
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
    claim_thread: int = NO_THREAD_ID,
) -> AdoptResult:
    local = await workspaces_repo.get(db, workspace_id)
    known_rooms = [
        row
        for row in await sessions_repo.list_for_workspace(db, workspace_id)
        if row.has_room
    ]
    if known_rooms and all(row.chat_id != chat_id for row in known_rooms):
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
    # `workspace.name` is `tg-<chat>-<nonce>` for anything this bot created.
    # The *family* label names the workspace and keys every one of its rooms'
    # colours, so they read as one group in the topic list.
    family = topic_label(
        human_name(workspace.name) or workspace_id[:8], workspace.branch
    )
    marker = marker_for(workspace_status=workspace.status)

    # Which session, first: the room belongs to one now, so "does this already
    # have a room here?" cannot be asked before "which one are we opening?".
    remote_sessions = await _all_sessions(client, workspace_id)
    if not remote_sessions:
        raise AdoptError("No sessions in this workspace yet.")
    chosen = _pick_session(remote_sessions, session_hint)
    label = room_label(family, _session_title(chosen))

    # A private chat used to be excluded here, back when a DM had exactly one
    # linear seat and no topic to remember. It can have both now, and skipping
    # the check meant a second `/attach` of the same workspace opened a second
    # room beside the first instead of jumping to it.
    remembered = _remembered_topic(
        next((row for row in known_rooms if row.id == chosen.id), None), chat_id
    )
    # The same probe ``/attach`` uses on the thread it was typed in, and for the
    # same reason: a rename is the only call that proves a room is still there,
    # and it is one we owe anyway. It never raises — a refusal we cannot read
    # keeps the room unnamed rather than failing the whole open, because the
    # room the user is trying to reach is the one thing this must not lose.
    claim = (
        await claim_topic(bot, chat_id, remembered, label, marker=marker)
        if remembered is not None
        else Claim(False)
    )
    existing_thread: int | None = None
    if remembered is not None and claim.alive:
        # The existence probe also reconciled the visible title to the current
        # remote status — when Telegram let it. Persist the marker only then:
        # recording a title the topic list is not showing makes the next
        # transition skip the rename that would have fixed it.
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
                topic_name=family,
            )
            await sessions_repo.bind_topic(
                db,
                chosen.id,
                chat_id=chat_id,
                topic_id=remembered,
                topic_name=label,
            )
            if claim.named:
                await sessions_repo.set_topic_marker(db, chosen.id, marker.value)
            # Repair a route that a partial write lost. The room is this
            # session's, so the only thing the `chats` row can be repaired *to*
            # is this session — which is what keying the memory on the session
            # rather than the workspace bought.
            route = await chats_repo.get(db, chat_id, remembered)
            if route is None or route.session_id != chosen.id:
                await chats_repo.bind(
                    db,
                    chat_id,
                    remembered,
                    workspace_id=workspace.id,
                    session_id=chosen.id,
                    kind="topic",
                )
        return AdoptResult(
            workspace_id=workspace_id,
            session_id=chosen.id,
            thread_id=remembered,
            name=label,
            deep_link=workspace.deep_link or (local.deep_link if local else None),
            already=True,
            sessions=len(remote_sessions),
            sleeping=workspace.status.is_waking,
        )

    # The topic is created before any state is written, for the same reason
    # ``common.create_and_bind_input`` does it: a half-written binding pointing
    # at a topic that does not exist is worse than a command that failed.
    thread_id = NO_THREAD_ID
    fresh = False
    claimed = False
    named = False
    if existing_thread is not None:
        thread_id = existing_thread
    elif (
        claim_thread
        and (
            claim := await claim_topic(bot, chat_id, claim_thread, label, marker=marker)
        ).alive
    ):
        # The `/attach` that produced this button was itself typed into an empty
        # thread. Adopt into that one: opening a sibling left the request and the
        # workspace in different rooms, and in a threaded DM the room this used
        # to fall back to — thread 0 — is not one a person can read at all.
        # `claim_topic` renames it *and* proves it is still there, which is the
        # same order `require_topic` imposes on a room we open ourselves.
        thread_id = claim_thread
        claimed = True
        named = claim.named
    elif chat_type != "private":
        thread_id = await require_topic(
            bot, chat_id, label, marker=marker, color_key=family
        )
        fresh = True
    else:
        # A DM with no thread to claim: open one if this bot may, and fall back
        # to the linear seat if it may not. Never fail the adoption over it.
        support = await dm_topic_support(bot)
        refusal = support.detail or support.reason if support.degraded else None
        if refusal is None:
            try:
                thread_id = await require_topic(
                    bot, chat_id, label, marker=marker, color_key=family
                )
                fresh = True
            except TopicCreateError as exc:
                refusal = exc.reason
        if refusal is not None:
            # The same one line `/new` says, through the same once-per-chat
            # gate. Dropping a workspace into the linear seat *silently* is how
            # a DM that cannot have topics looks like a bot that lost the reply.
            await note_linear_seat(bot, chat_id, refusal)

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
            family=family,
            marker=marker,
            fresh=fresh,
            record_marker=fresh or named or not claimed,
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

    posted = await send_html(bot, chat_id, card, thread_id=thread_id, silent=True)

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
        carded=posted is not None,
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


def _remembered_topic(row: SessionRow | None, chat_id: int) -> int | None:
    """The topic this chat already owns for *this session*, if any.

    Keyed on the session rather than the workspace, so the repair path can only
    ever restore a room to the session that room belongs to — a different
    session in the same room is exactly what this change exists to prevent.
    """
    if row is None or row.chat_id != chat_id or not row.has_room:
        return None
    return row.thread_id


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
    family: str,
    marker: TopicMarker,
    fresh: bool,
    record_marker: bool = True,
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
        # Still the workspace's: which Telegram chat it lives in, which is what
        # refuses opening it in a second one. The *room* is the session's.
        chat_id=chat_id,
        topic_name=family,
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
                session_id=session.id,
                chat_id=chat_id,
                topic_id=thread_id,
                label=label,
                marker=marker,
            )
        elif thread_id:
            await sessions_repo.bind_topic(
                db,
                session.id,
                chat_id=chat_id,
                topic_id=thread_id,
                topic_name=label,
            )
            if record_marker:
                # ``False`` only when a claimed thread refused the rename, so
                # the topic list is not showing this marker and recording it
                # would make the next transition skip the rename that fixes it.
                await sessions_repo.set_topic_marker(db, session.id, marker.value)
            await chats_repo.ensure(db, chat_id, thread_id, kind="topic")
        else:
            await workspaces_repo.upsert(db, workspace.id, chat_id=chat_id)
            await chats_repo.ensure(db, chat_id, NO_THREAD_ID, kind="dm")
        # Bound last, and through `bind`, which releases whatever session was on
        # this seat before. Nothing else unbinds it, and two bound sessions on
        # one seat means two pollers writing into one room.
        await sessions_repo.bind(db, session.id, chat_id=chat_id, thread_id=thread_id)
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
    route: Route,
    nonces: NonceStore,
    tenant: TenantContext,
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
    chat_type = _chat_type(query, chat_id)
    # Opening costs several API calls; answer first so Telegram stops spinning.
    await query.answer("Opening…")
    try:
        result = await adopt_workspace(
            bot=query.bot,
            db=resolve_db(db),
            client=resolve_client(client, tenant),
            chat_id=chat_id,
            chat_type=chat_type,
            workspace_id=workspace_id,
            session_hint=session_hint or None,
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
        # Adopted into the very thread the button was tapped in, and the
        # snapshot card is on screen there. A button pointing at this room
        # would not be. Gated on the card having *landed*, never on the seats
        # matching: `send_html` returns None rather than raising, and a
        # suppressed reply on top of a failed one is a tap with no output at
        # all (CLAUDE.md — never gate a sendMessage on a conclusion).
        return
    target = jump_url(chat_id, result.thread_id)
    label = "Open topic" if target else "Open in Conductor"
    link = target or result.deep_link
    await send_html(
        query.bot,
        chat_id,
        ack_line(result, linkable=target is not None),
        thread_id=ticket.thread_id,
        reply_markup=keyboard([[url_button(label, link)]]) if link else None,
        silent=True,
    )


def ack_line(result: AdoptResult, *, linkable: bool = True) -> str:
    """Two words and a name. The card in the topic carries the detail.

    ``linkable`` is false in a private chat, where Telegram publishes no link
    syntax for a thread — so the ack is read *outside* the room it names and
    the only button on offer opens a browser. Name the room instead, exactly as
    :func:`~ctb.bot.handlers.common.created_card` does for the same reason: the
    thread list is the whole navigation, and "already open" without saying
    *where* is what left somebody stranded in a thread called "/attach".
    """
    if not result.already:
        return f"→ <b>{escape(result.name)}</b>"
    where = (
        ""
        if linkable
        else "\n<i>It is in this chat's thread list, under that name.</i>"
    )
    return f"→ <b>{escape(result.name)}</b> · already open{where}"


def _chat_type(query: CallbackQuery, chat_id: int) -> str:
    message = query.message
    if isinstance(message, Message):
        return message.chat.type
    return "private" if chat_id > 0 else "supergroup"
