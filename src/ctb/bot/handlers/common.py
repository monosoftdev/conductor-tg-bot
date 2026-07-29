"""Shared command operations.

The handlers stay deliberately thin: this module owns the crash-safe prompt
ledger and the workspace/session binding sequence so every entry point follows
the same rules.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message, ReactionTypeEmoji

from ctb.bot.handlers.topics import (
    TopicCreateError,
    apply_marker,
    claim_topic,
    discard_topic,
    dm_topic_support,
    forum_support,
    human_name,
    jump_url,
    require_topic,
    resolve_db,
    send_html,
    topic_label,
)
from ctb.bot.keyboards import keyboard, url_button
from ctb.bot.middleware.routing import Route
from ctb.bot.middleware.tenancy import TenantSettings
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import Project, validate_pairing
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import prompts as prompts_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.delivery.render.html import escape
from ctb.logging import get_logger
from ctb.turn import cursor
from ctb.turn.state import Cancel, Evidence, TopicMarker, TurnState

log = get_logger(__name__)

FOCUS_MS: Final = 30 * 60 * 1000
#: Last resort only. Precedence is chat default → ``DEFAULT_BRANCH`` env →
#: this, so the owner changes the offered branch from Railway, not from code.
DEFAULT_BRANCH: Final = "main"
#: Appended to every Telegram prompt. The delimiter matters: without it a long
#: task that carries its own style guidance averages this away instead of
#: obeying it.
#:
#: Every clause here is a fact about this bot's own surface, not a preference,
#: which is why the rules can be checked rather than argued with:
#:
#: * rule 1 — the status card already carries the tool line and the ``path +12
#:   −3`` diff line, so narration is a second copy of what is on screen;
#: * rule 4 — the phone bubble is ~40 characters wide, a markdown table wraps
#:   into rubble there, ``markdown_to_html`` flattens a heading to bold, and
#:   ``chunk_blocks`` turns code past :data:`MAX_INLINE_CODE_LINES` into a
#:   ``.md`` attachment the reader must leave the chat to open;
#: * rule 5 — the reader is on a phone with no shell, so "run this to check" is
#:   an unfinished turn, not a handover;
#: * rule 7 — ``cursor.quick_replies_for`` parses a literal ``Choices:`` line
#:   followed by consecutive ``1.``/``2.`` options and gives up on any other
#:   text after them, and ``keyboards.quick_replies_fit`` drops back to plain
#:   text unless each option fits :data:`MAX_BUTTON_TEXT` once numbered. Under
#:   40 characters is that budget with room to spare. The block's syntax must
#:   not drift.
MOBILE_REPLY_INSTRUCTION: Final = (
    "===\n"
    "OUTPUT CONTRACT — Telegram, phone screen. This overrides any conflicting "
    "style guidance above.\n"
    "1. Say nothing until the work is done, then one message. No preamble, "
    "progress notes or plan restatement — I already see your tool calls and "
    "file diffs.\n"
    "2. Line 1 is the outcome: fixed / not fixed / blocked / what I found. "
    "Anything broken or guessed belongs there, never at the bottom.\n"
    "3. 6 lines and 80 words max unless I ask for detail. Never restate my "
    "question, recap your steps, or list the files you changed.\n"
    "4. Write for a 40-character screen: plain sentences or '- ' bullets, "
    "`code` for paths, ```lang fences for code (a long one arrives as a file "
    "attachment, so paste only what I must read). No tables — one bullet per "
    "row. No headings, bold labels, emoji or closing summary.\n"
    "5. I cannot run anything myself: verify before you claim it, and name "
    "the evidence on the same line.\n"
    "6. Decide anything reversible yourself and name the default you took. "
    "Ask only when the repo cannot answer it and the answer changes the "
    "work.\n"
    "7. To ask: one line of context, then a line that is exactly 'Choices:', "
    "then 2-4 options as '1. ...', '2. ...', recommendation first, each under "
    "40 characters and readable alone (tapping one sends it back as my next "
    "message). Nothing after the last option. Otherwise never write a Choices "
    "block."
)
#: Said once when a chat that should have had a topic per workspace could not
#: get one. What Telegram said is for the log — the tenant cannot flip another
#: account's @BotFather toggle — so the line carries only what changes for them.
LINEAR_DM_NOTICE: Final = (
    "Topics unavailable here · one workspace at a time. <code>/s</code> switches."
)
#: Chats already told. In-process on purpose: this is a nudge, not a fact worth
#: a column, and repeating it once after a redeploy is cheaper than a migration.
_LINEAR_TOLD: Final[set[int]] = set()
_LINEAR_TOLD_MAX: Final = 4096

_PROJECT_PREFIX = re.compile(r"^([^\s:]{1,80}):\s*(.+)$", re.S)
#: A pick or an ack carries no new task, and the contract is already in the
#: session from an earlier turn. Appending 240 words of formatting rules to the
#: word "yes" only teaches the model that the reply is the important part.
_TERSE_FOLLOW_UP: Final = re.compile(
    r"^(?:choose option\s+[1-4]\b.*"
    r"|[1-4][.)]?"
    r"|y|n|yes|no|yep|nope|ok|okay|sure|go|go ahead|do it|continue|proceed"
    r"|approved|lgtm"
    r")[.!]*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class CreateRequest:
    project_id: str
    project_name: str
    branch: str
    agent: str
    model: str
    effort: str
    prompt: str
    #: The API accepts this independently of Conductor's optional GitHub
    #: connection. Kept beside the project id so `/new` can route around that
    #: one capability refusal without changing the selected repository.
    repository_url: str | None = None


@dataclass(frozen=True, slots=True)
class CreatedBinding:
    workspace_id: str
    session_id: str
    thread_id: int
    label: str
    deep_link: str | None = None
    #: Set when this chat wanted a topic and Telegram would not give one, so
    #: the workspace took the chat's single linear seat instead. Telegram's own
    #: words, for the caller that wants to show them; the chat only ever gets
    #: :data:`LINEAR_DM_NOTICE`.
    linear_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Seat:
    """Where a new workspace will live, decided before anything is paid for."""

    thread_id: int
    #: Set only when *this* call created the topic — the only one we may delete.
    fresh_topic: int | None = None
    #: Set when the request arrived in an empty thread Telegram had already
    #: opened, and that thread became this workspace's room. We did not create
    #: it, so it is never discarded — but it still wears whatever Telegram
    #: named it, so it must be renamed once the workspace exists.
    claimed_topic: int | None = None
    #: The claimed room actually took the workspace's name. ``False`` means
    #: Telegram refused the rename and the topic list still shows whatever it
    #: opened the thread as — so the marker is left unrecorded and the next
    #: state transition tries again.
    claimed_named: bool = False
    #: The name a replayed nonce's topic already carries, if it drifted.
    prior_label: str | None = None
    #: Why there is no topic, when there should have been one.
    refusal: str | None = None

    @property
    def owns_topic(self) -> bool:
        """This call put the workspace in that room, however it got there."""
        return self.fresh_topic is not None or self.claimed_topic is not None


class CancelDispatcher(Protocol):
    def dispatch(
        self, session_id: str, evidence: Evidence
    ) -> Awaitable[object | None]: ...


def command_text(message: Message) -> str:
    text = message.text or ""
    return text.partition(" ")[2].strip()


async def _react(message: Message, emoji: str) -> bool:
    bot = getattr(message, "bot", None)
    if bot is None:
        return False
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except (TelegramAPIError, AttributeError):
        return False
    return True


async def react_received(message: Message) -> bool:
    """Use a reaction as the zero-noise prompt acknowledgement."""
    return await _react(message, "👀")


async def react_ok(message: Message) -> bool:
    """The command did exactly what it says. A 👍 beats a bubble that echoes it."""
    return await _react(message, "👍")


async def abandon_wizard(state: FSMContext | None) -> None:
    if state is not None and await state.get_state() is not None:
        await state.clear()


async def request_cancel(
    supervisor: CancelDispatcher | None,
    session_id: str,
    *,
    requested_by: int | None,
) -> bool:
    """Route cancel through the session machine; never bypass its drain."""
    if supervisor is None:
        return False
    result = await supervisor.dispatch(session_id, Cancel(requested_by=requested_by))
    return result is not None and result is not False


async def tell(
    message: Message,
    text: str,
    *,
    reply_markup: Any = None,
    silent: bool = True,
) -> Message | None:
    """Reply to a command. Silent by default — the phone is already in hand.

    Only something the owner has to act on (a failure) is worth a push, so
    those callers pass ``silent=False`` explicitly.
    """
    if message.bot is None:
        raise RuntimeError("Telegram bot is not bound to the message")
    return await send_html(
        message.bot,
        message.chat.id,
        text,
        thread_id=message.message_thread_id or NO_THREAD_ID,
        reply_markup=reply_markup,
        silent=silent,
    )


def short_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:160]


async def all_projects(client: ConductorClient) -> list[Project]:
    projects: list[Project] = []
    offset = 0
    for _ in range(20):
        page = await client.list_projects(limit=100, offset=offset)
        projects.extend(page.data)
        if not page.has_more:
            break
        offset += len(page.data)
        if not page.data:
            break
    return projects


def match_project(projects: list[Project], query: str) -> Project | None:
    needle = query.strip().casefold()
    exact = [
        item
        for item in projects
        if item.id.casefold() == needle or (item.name or "").casefold() == needle
    ]
    if exact:
        return exact[0]
    prefix = [
        item
        for item in projects
        if item.id.casefold().startswith(needle)
        or (item.name or "").casefold().startswith(needle)
    ]
    return prefix[0] if len(prefix) == 1 else None


async def quota_error(db: Database, defaults: TenantSettings) -> str | None:
    """The workspace quota, as one named check. ``None`` means go ahead.

    Extracted so the protection is a thing that can be called and tested. As an
    inline branch it shipped unexercised, and an adversarial review deleted it
    without turning the suite red — a quota nothing verifies is a number in a
    column, not a limit.

    Archived workspaces do not count: this bounds what a tenant runs *at once*,
    not what it has ever run.
    """
    live = await workspaces_repo.count_live(db)
    if live < defaults.max_workspaces:
        return None
    return (
        f"Your team is at its limit of {defaults.max_workspaces} "
        "Conductor workspaces. Finish one with /done first."
    )


async def resolve_new_request(
    *,
    text: str,
    route: Route,
    defaults: TenantSettings,
    db: Database,
    client: ConductorClient,
) -> CreateRequest:
    projects = await all_projects(client)
    if not projects:
        raise ValueError("No Conductor projects found.")

    explicit: str | None = None
    prompt = text.strip()
    matched = _PROJECT_PREFIX.match(prompt)
    if matched:
        explicit, prompt = matched.group(1), matched.group(2).strip()
    if not prompt:
        raise ValueError("Add a prompt after /new.")

    chat = route.chat
    project = match_project(projects, explicit) if explicit else None
    if project is None and explicit:
        raise ValueError(f"Project not found: {explicit}")
    if project is None and chat is not None and chat.default_project_id:
        project = match_project(projects, chat.default_project_id)
    if project is None:
        recent = sorted(await chats_repo.list_all(db), key=lambda row: row.updated_at)
        for row in reversed(recent):
            if row.default_project_id:
                project = match_project(projects, row.default_project_id)
                if project is not None:
                    break
    if project is None:
        project = projects[0]

    refusal = await quota_error(db, defaults)
    if refusal is not None:
        raise ValueError(refusal)

    agent = (chat.default_agent if chat else None) or defaults.default_agent
    model = (chat.default_model if chat else None) or defaults.default_model
    effort = (chat.default_effort if chat else None) or defaults.default_effort
    branch = (
        (chat.default_branch if chat else None) or defaults.default_branch
    ) or DEFAULT_BRANCH
    validate_pairing(agent, model, effort)
    return CreateRequest(
        project_id=project.id,
        project_name=project.name or project.id[:8],
        branch=branch,
        agent=agent,
        model=model,
        effort=effort,
        prompt=prompt,
        repository_url=project.git_remote,
    )


async def submit_prompt(
    *,
    db: Database,
    client: ConductorClient,
    session_id: str,
    text: str,
    chat_id: int,
    thread_id: int,
    tg_message_id: int | None = None,
    message_id: str | None = None,
) -> tuple[str, str]:
    """Persist first, then POST with the same id forever.

    An ambiguous result intentionally remains ``pending`` for the supervisor to
    recover; a definite failure is marked and surfaced.
    """
    body = augment_prompt(text)
    result = await cursor.send_prompt(
        client,
        db,
        session_id=session_id,
        text=body,
        chat_id=chat_id,
        thread_id=thread_id,
        tg_message_id=tg_message_id,
        message_id=message_id,
        # Keep Telegram interactive. Ambiguous/unreachable outcomes remain
        # pending and the supervisor retries the identical id automatically.
        max_attempts=1,
    )
    state = result.prompt.post_state or ("saved" if result.ambiguous else "queued")

    if result.reason == "already settled":
        return result.message_id, "already sent"

    await sessions_repo.touch_prompt(db, session_id)
    await sessions_repo.update(
        db,
        session_id,
        turn_state=str(TurnState.QUEUED),
        start_witnessed=False,
        consecutive_idle=0,
    )
    await chats_repo.touch_prompt(db, chat_id, thread_id, focus_for_ms=FOCUS_MS)
    return result.message_id, state


def augment_prompt(text: str) -> str:
    """Attach one stable presentation instruction to every Telegram prompt.

    Skipped for a bare pick or ack ("yes", "2", a quick-reply button): the
    contract is already in the session, and the boilerplate would otherwise be
    most of the user turn.
    """
    cleaned = text.strip()
    if MOBILE_REPLY_INSTRUCTION in cleaned:
        return cleaned
    if _TERSE_FOLLOW_UP.match(cleaned):
        return cleaned
    return f"{cleaned}\n\n{MOBILE_REPLY_INSTRUCTION}"


async def _seat_for(
    *,
    bot: Bot | None,
    database: Database,
    chat_id: int,
    chat_type: str,
    label: str,
    nonce: str,
    claimable_thread: int = NO_THREAD_ID,
) -> _Seat:
    """Reserve the room this workspace will live in — **before** it is paid for.

    A cloud workspace starts billing the instant it is created, and
    ``POST /workspaces`` has no idempotency key, so a topic failure *after* it
    strands a paid container that no retry can adopt. ``can_manage_topics`` is
    not proof: it has been observed ``true`` on a chat that then refused
    ``createForumTopic``. The only proof that a topic can exist is a topic that
    exists, so it is created first. A topic is free and deletable.

    The two chat kinds differ in what a refusal *means*, not in what is tried:

    * **Group** — a group without topics has no working shape to fall back to
      (General never prompts), so a refusal fails the command and says why.
    * **DM** — a refusal degrades to the linear single-seat DM, which is what
      the bot did here until today and still works. Whether Telegram allows DM
      topics is a runtime fact, not a config flag; a feature that is optional
      must never take the bot down with it.

    ``claimable_thread`` is the one thing that keeps a threaded DM from growing
    two rooms per workspace. Telegram's *New Chat* seat is a composer, not a
    chat: anything typed there — ``/new`` included — makes the client open a
    thread named after that first line, and the bot's update arrives already
    inside it. Opening a second topic next to that one is how ``/new`` came to
    leave a stray ``/new`` thread behind every time. So an empty thread the
    request already sits in *is* the room; only a bound thread (or none at all)
    makes a new one. The caller decides emptiness, because only it can see the
    route.

    A claimed thread is held to the same standard as a created one: the rename
    that gives it the workspace's name is also the call that proves it still
    exists. "An update once arrived from it" is not proof — the confirm card
    puts a human-length pause in front of the create, and a thread can be
    deleted in that window.
    """
    prior = await workspaces_repo.get_by_nonce(database, nonce)
    if prior is not None and prior.chat_id == chat_id and prior.topic_id:
        # A replayed update: reuse the topic this nonce already owns rather
        # than opening a sibling next to it.
        return _Seat(prior.topic_id, prior_label=prior.topic_name)
    if chat_type == "private":
        if bot is None:
            return _Seat(NO_THREAD_ID, refusal="no bot bound")
        if claimable_thread:
            # No `dm_topic_support` probe: this update *arrived* in a thread, so
            # the question it answers ("can this bot have topics here?") is
            # already answered, and answered by the only thing that counts.
            claim = await claim_topic(bot, chat_id, claimable_thread, label)
            if claim.alive:
                return _Seat(
                    claimable_thread,
                    claimed_topic=claimable_thread,
                    claimed_named=claim.named,
                )
            # Deleted between the card and the tap. Fall through and open one.
        support = await dm_topic_support(bot)
        if support.degraded:
            return _Seat(NO_THREAD_ID, refusal=support.detail or support.reason)
        try:
            topic_id = await require_topic(bot, chat_id, label)
        except TopicCreateError as exc:
            return _Seat(NO_THREAD_ID, refusal=exc.reason)
        return _Seat(topic_id, fresh_topic=topic_id)
    if bot is None:
        raise RuntimeError("Telegram bot is not bound to the input")
    support = await forum_support(bot, chat_id)
    if support.degraded:
        raise RuntimeError(
            f"No topic permission ({support.detail or support.reason}). Run /setup."
        )
    topic_id = await require_topic(bot, chat_id, label)
    return _Seat(topic_id, fresh_topic=topic_id)


async def note_linear_seat(bot: Bot | None, chat_id: int, reason: str) -> bool:
    """Tell this chat once that it is linear, and why that is visible to them.

    Never raises and never blocks the create it follows: the workspace exists,
    the prompt is queued, and a chat that missed one nudge is still a working
    chat. Returns whether a line was actually sent.
    """
    log.info("common.topics_unavailable", chat_id=chat_id, reason=reason)
    if bot is None or chat_id in _LINEAR_TOLD:
        return False
    if len(_LINEAR_TOLD) >= _LINEAR_TOLD_MAX:
        _LINEAR_TOLD.clear()
    _LINEAR_TOLD.add(chat_id)
    try:
        await send_html(bot, chat_id, LINEAR_DM_NOTICE, thread_id=NO_THREAD_ID)
    except Exception as exc:  # noqa: BLE001 - cosmetics never fail a create
        log.warning("common.linear_notice_failed", chat_id=chat_id, error=repr(exc))
        return False
    return True


async def create_and_bind(
    *,
    message: Message,
    route: Route,
    request: CreateRequest,
    client: ConductorClient,
    db: Database | None = None,
) -> CreatedBinding:
    bot = getattr(message, "bot", None)
    if bot is None and message.chat.type != "private":
        raise RuntimeError("Telegram bot is not bound to the message")
    return await create_and_bind_input(
        bot=bot,
        chat_id=message.chat.id,
        chat_type=message.chat.type,
        tg_message_id=message.message_id,
        route=route,
        request=request,
        db=db,
        client=client,
    )


async def create_and_bind_input(
    *,
    bot: Bot | None,
    chat_id: int,
    chat_type: str,
    tg_message_id: int,
    route: Route,
    request: CreateRequest,
    db: Database | None = None,
    client: ConductorClient,
    action_id: str | None = None,
) -> CreatedBinding:
    """Create/bind from typed or voice input without manufacturing an update.

    ``client`` is the *tenant's* client, passed explicitly: there is no
    process-wide fallback to reach for, which is what makes a cross-organisation
    create impossible to write by accident.
    """
    database = resolve_db(db)
    conductor = client
    validate_pairing(request.agent, request.model, request.effort)
    # The opening prompt names the topic. Taken once, here, and never revisited:
    # `apply_marker` only ever changes the prefix, so the name a workspace is
    # born with is the name it keeps.
    label = topic_label(request.project_name, request.branch, task=request.prompt)

    operation_id = action_id or f"{chat_id}:{tg_message_id}"
    nonce = cursor.stable_nonce(operation_id)
    seat = await _seat_for(
        bot=bot,
        database=database,
        chat_id=chat_id,
        chat_type=chat_type,
        label=label,
        nonce=nonce,
        claimable_thread=route.claimable_thread,
    )
    thread_id = seat.thread_id
    fresh_topic = seat.fresh_topic
    prior_label = seat.prior_label
    try:
        creation = await cursor.create_workspace(
            conductor,
            database,
            chat_id=chat_id,
            project_id=request.project_id,
            repository_url=request.repository_url,
            branch=request.branch,
            session_name=request.prompt[:80],
            agent=request.agent,
            model=request.model,
            effort=request.effort,
            nonce=nonce,
        )
        if not creation.ok or creation.workspace_id is None:
            raise RuntimeError(
                "Workspace create is uncertain. Check Conductor; it was not retried."
            )
    except BaseException:
        # Nothing will ever live in this room. Free and deletable, unlike the
        # container it was meant to host — so a retry finds no empty siblings.
        if fresh_topic is not None and bot is not None:
            await discard_topic(bot, chat_id, fresh_topic)
        raise
    workspace_id = creation.workspace_id
    workspace_name = creation.name
    deep_link = creation.deep_link
    # Persist the mapping immediately: from here on a replay reuses this topic.
    if seat.owns_topic:
        await workspaces_repo.upsert(
            database,
            workspace_id,
            create_nonce=nonce,
            chat_id=chat_id,
            topic_id=thread_id,
            topic_name=label,
        )
    session_id = creation.session_id or ""
    if not session_id:
        page = await conductor.list_workspace_sessions(workspace_id, limit=1)
        if not page.data:
            raise RuntimeError("Workspace created, but no session was returned.")
        session_id = page.data[0].id

    # The topic was named before the workspace existed. If what it actually
    # carries is not what the workspace ended up called, correct it through the
    # one rename path there is — never a second mechanism.
    #
    # **Before** the upsert below, not after. `apply_marker` decides whether a
    # rename is needed by comparing against the title the row says was last
    # applied; writing `topic_name=label` first destroys exactly that evidence
    # and makes the correction skip itself.
    if bot is not None and prior_label is not None and prior_label != label:
        await apply_marker(
            bot, database, workspace_id, TopicMarker.INITIALIZING, label=label
        )
    await workspaces_repo.upsert(
        database,
        workspace_id,
        project_id=request.project_id,
        name=workspace_name,
        branch=request.branch,
        agent=request.agent,
        model=request.model,
        effort=request.effort,
        deep_link=deep_link,
        status="initializing",
        create_nonce=nonce,
        chat_id=chat_id,
        topic_id=thread_id if thread_id else None,
        topic_name=label,
    )
    if thread_id and (seat.fresh_topic is not None or seat.claimed_named):
        # The room is wearing ⏳ right now — `require_topic` created it that way,
        # or `claim_topic` renamed it before any of this was paid for. Record
        # that, or `apply_marker`'s "has anything changed?" test cannot parse
        # the stored marker and every early rename spends a Telegram call to
        # set a title that is already correct.
        #
        # Deliberately *not* recorded when a claimed thread refused the rename:
        # the row would then assert a title the list has never shown, and the
        # next transition would skip the rename that would have fixed it.
        await workspaces_repo.set_topic_marker(
            database, workspace_id, TopicMarker.INITIALIZING.value
        )
    await sessions_repo.upsert(
        database,
        session_id,
        workspace_id=workspace_id,
        title=request.prompt[:80],
        agent=request.agent,
        model=request.model,
        effort=request.effort,
        chat_id=chat_id,
        thread_id=thread_id,
        is_bound=True,
    )
    # This session was created by us and is empty before the first POST. Mark
    # that known boundary explicitly. Letting the supervisor "seek to end"
    # later could skip a fast first reply that completed before its first pass.
    await sessions_repo.seek_to_end(
        database,
        session_id,
        message_id=None,
        session_index=-1,
    )
    # A seat with a thread is a topic seat, whatever chat it is in. Only the
    # linear DM root is still ``dm``.
    kind = "dm" if chat_type == "private" and not thread_id else "topic"
    await chats_repo.bind(
        database,
        chat_id,
        thread_id,
        workspace_id=workspace_id,
        session_id=session_id,
        kind=kind,
    )
    await chats_repo.set_defaults(
        database,
        chat_id,
        route.thread_id,
        project_id=request.project_id,
        branch=request.branch,
        agent=request.agent,
        model=request.model,
        effort=request.effort,
    )
    await chats_repo.set_defaults(
        database,
        chat_id,
        thread_id,
        project_id=request.project_id,
        branch=request.branch,
        agent=request.agent,
        model=request.model,
        effort=request.effort,
    )
    # New workspaces start in `initializing`. Capture the prompt durably now,
    # but do not POST it until the poller observes `ready`; rule 10 then emits a
    # RePost with this exact id.
    prompt_body = augment_prompt(request.prompt)
    existing_prompt = (
        await prompts_repo.get(database, action_id) if action_id is not None else None
    )
    if existing_prompt is None:
        await prompts_repo.create(
            database,
            session_id=session_id,
            body=prompt_body,
            chat_id=chat_id,
            thread_id=thread_id,
            tg_message_id=tg_message_id,
            index_at_post=-1,
            message_id=action_id,
        )
    elif (
        existing_prompt.session_id != session_id or existing_prompt.body != prompt_body
    ):
        raise RuntimeError("Voice operation id belongs to a different prompt.")
    await sessions_repo.touch_prompt(database, session_id)
    await sessions_repo.update(
        database,
        session_id,
        turn_state=str(TurnState.WAKING),
        start_witnessed=False,
        consecutive_idle=0,
    )
    await chats_repo.touch_prompt(database, chat_id, thread_id, focus_for_ms=FOCUS_MS)
    # Last, and only once everything above worked: a refusal is worth one line,
    # a failed create is worth none — the caller is already saying that.
    if seat.refusal is not None:
        await note_linear_seat(bot, chat_id, seat.refusal)
    return CreatedBinding(
        workspace_id,
        session_id,
        thread_id,
        label,
        deep_link,
        linear_reason=seat.refusal,
    )


def created_card(
    chat_id: int,
    created: CreatedBinding,
    *,
    from_thread: int = NO_THREAD_ID,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """One face for "the workspace exists now", wherever it was asked for.

    Telegram publishes no link syntax for a topic inside a *private* chat, so a
    DM's brand-new topic gets no button. With nothing but ``→ label`` left in
    the root, ``/new`` read as though it had done nothing — while the work had
    in fact moved one room over, and the root had stopped being a seat. Say
    which room, since we cannot link to it.

    ``from_thread`` is where this card is about to be *shown*. When that is the
    workspace's own room there is nowhere to point: no button, and no line
    telling somebody to go and find a topic they are already reading.
    """
    text = f"→ <b>{escape(created.label)}</b>"
    if created.thread_id and created.thread_id == from_thread:
        return f"{text}\n<i>This thread is the workspace · type to send.</i>", None
    target = (
        jump_url(chat_id, created.thread_id) if created.thread_id else created.deep_link
    )
    if target:
        label = "Open topic" if created.thread_id else "Open in Conductor"
        return text, keyboard([[url_button(label, target)]])
    if created.thread_id:
        text += "\n<i>Opened as its own topic · it is in this chat's topic list.</i>"
    return text, None


async def require_session(message: Message, route: Route) -> str | None:
    if route.session_id:
        return route.session_id
    await tell(message, "No session here. Use <code>/new</code> or <code>/s</code>.")
    return None


def workspace_name(row: Any) -> str:
    """What to call this workspace to a person.

    ``row.name`` is Conductor's, which for anything the bot created is the
    ``tg-<chat>-<nonce>`` reconciliation key — bookkeeping, never a name.
    """
    return row.topic_name or human_name(row.name) or row.id[:8]


def safe_title(text: str | None, fallback: str) -> str:
    value = " ".join((text or "").split())
    return value[:80] or fallback


def html_code(text: object) -> str:
    return f"<code>{escape(str(text))}</code>"


def new_session_id() -> str:
    return str(uuid.uuid4())
