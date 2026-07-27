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
from aiogram.types import Message, ReactionTypeEmoji

from ctb.bot.handlers.topics import (
    apply_marker,
    discard_topic,
    forum_support,
    require_topic,
    resolve_client,
    resolve_db,
    send_html,
    topic_label,
)
from ctb.bot.middleware.routing import Route
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import Project, validate_pairing
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import prompts as prompts_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.delivery.render.html import escape
from ctb.settings import Settings
from ctb.turn import cursor
from ctb.turn.state import Cancel, Evidence, TopicMarker, TurnState

FOCUS_MS: Final = 30 * 60 * 1000
#: Last resort only. Precedence is chat default → ``DEFAULT_BRANCH`` env →
#: this, so the owner changes the offered branch from Railway, not from code.
DEFAULT_BRANCH: Final = "main"
#: Appended to every Telegram prompt. The delimiter matters: without it a long
#: task that carries its own style guidance averages this away instead of
#: obeying it. Rule 5's wording is load-bearing — ``cursor.quick_replies_for``
#: parses a literal ``Choices:`` line followed by ``1.``/``2.`` options into the
#: quick-reply buttons, so that block's syntax must not drift.
MOBILE_REPLY_INSTRUCTION: Final = (
    "===\n"
    "OUTPUT CONTRACT — I read this on a phone. This overrides any conflicting "
    "style guidance above.\n"
    "1. Say nothing until the work is done. No preamble, no narration between "
    "tool calls, no 'Let me...', no plan restatement. One message, at the end.\n"
    "2. Open with the outcome in one sentence: fixed / not fixed / what I "
    "found. Never restate my question and never recap the steps you took.\n"
    "3. Hard cap 6 lines and 80 words unless I explicitly ask for detail. "
    "Plain sentences or '- ' bullets. No headings, no tables, no bold labels, "
    "no closing summary.\n"
    "4. Do not list the files you changed. I already see them.\n"
    "5. Only if you need a decision from me: one line of context, then a line "
    "that is exactly 'Choices:', then 2-4 numbered one-line options "
    "('1. ...', '2. ...') with your recommendation first. Write nothing after "
    "the last option. Otherwise never write a Choices block."
)
_PROJECT_PREFIX = re.compile(r"^([^\s:]{1,80}):\s*(.+)$", re.S)
#: A pick or an ack carries no new task, and the contract is already in the
#: session from an earlier turn. Appending 140 words of formatting rules to the
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


@dataclass(frozen=True, slots=True)
class CreatedBinding:
    workspace_id: str
    session_id: str
    thread_id: int
    label: str
    deep_link: str | None = None


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


async def resolve_new_request(
    *,
    text: str,
    route: Route,
    settings: Settings,
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

    agent = (chat.default_agent if chat else None) or settings.default_agent
    model = (chat.default_model if chat else None) or settings.default_model
    effort = (chat.default_effort if chat else None) or settings.default_effort
    branch = (
        (chat.default_branch if chat else None) or settings.default_branch
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
        start_witnessed=0,
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


async def create_and_bind(
    *,
    message: Message,
    route: Route,
    request: CreateRequest,
    db: Database | None = None,
    client: ConductorClient | None = None,
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
    client: ConductorClient | None = None,
    action_id: str | None = None,
) -> CreatedBinding:
    """Create/bind from typed or voice input without manufacturing an update."""
    database = resolve_db(db)
    conductor = resolve_client(client)
    validate_pairing(request.agent, request.model, request.effort)
    label = topic_label(request.project_name, request.branch)

    operation_id = action_id or f"{chat_id}:{tg_message_id}"
    nonce = cursor.stable_nonce(operation_id)
    # A cloud workspace starts billing the instant it is created, and
    # ``POST /workspaces`` has no idempotency key — so a topic failure *after*
    # it strands a paid container that no retry can adopt. ``can_manage_topics``
    # is not proof: it has been observed ``true`` on a chat that then refused
    # ``createForumTopic``. The only proof that a topic can exist is a topic
    # that exists, so it is created first. A topic is free and deletable.
    thread_id = NO_THREAD_ID
    # Set only when *this* call created the topic — the only one we may delete.
    fresh_topic: int | None = None
    prior_label: str | None = None
    if chat_type != "private":
        if bot is None:
            raise RuntimeError("Telegram bot is not bound to the input")
        prior = await workspaces_repo.get_by_nonce(database, nonce)
        if prior is not None and prior.chat_id == chat_id and prior.topic_id:
            # A replayed update: reuse the topic this nonce already owns rather
            # than opening a sibling next to it.
            thread_id = prior.topic_id
            prior_label = prior.topic_name
        else:
            support = await forum_support(bot, chat_id)
            if support.degraded:
                raise RuntimeError(
                    f"No topic permission ({support.detail or support.reason}). "
                    "Run /setup."
                )
            thread_id = fresh_topic = await require_topic(bot, chat_id, label)
    try:
        creation = await cursor.create_workspace(
            conductor,
            database,
            chat_id=chat_id,
            project_id=request.project_id,
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
    if fresh_topic is not None:
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
    # The topic was named before the workspace existed. If what it actually
    # carries is not what the workspace ended up called, correct it through the
    # one rename path there is — never a second mechanism.
    if bot is not None and prior_label is not None and prior_label != label:
        await apply_marker(
            bot, database, workspace_id, TopicMarker.INITIALIZING, label=label
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
    kind = "dm" if chat_type == "private" else "topic"
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
        start_witnessed=0,
        consecutive_idle=0,
    )
    await chats_repo.touch_prompt(database, chat_id, thread_id, focus_for_ms=FOCUS_MS)
    return CreatedBinding(workspace_id, session_id, thread_id, label, deep_link)


async def require_session(message: Message, route: Route) -> str | None:
    if route.session_id:
        return route.session_id
    await tell(message, "No session here. Use <code>/new</code> or <code>/s</code>.")
    return None


def workspace_name(row: Any) -> str:
    return row.topic_name or row.name or row.id[:8]


def safe_title(text: str | None, fallback: str) -> str:
    value = " ".join((text or "").split())
    return value[:80] or fallback


def html_code(text: object) -> str:
    return f"<code>{escape(str(text))}</code>"


def new_session_id() -> str:
    return str(uuid.uuid4())
