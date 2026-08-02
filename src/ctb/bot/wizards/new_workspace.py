"""Bare ``/new``: one edited card, state persisted in ``wizard_state``.

The card's state is DB-backed and the buttons have to match it, or a redeploy
leaves a live wizard behind dead buttons — which is exactly what happened. So
the buttons are ``restartable`` (see ``ctb.bot.keyboards``), and since a
restart-proof payload has to be a bare id in a charset without ``:``, the
button does **not** carry the choice. It carries a reference to it:

``ctb:wiz:.<expiry-base36>.<wid><step><index>``

``wid`` identifies this run of the wizard, ``step`` is one letter, and ``index``
picks from the options the step persisted in the FSM. Resolution is therefore
identical before and after a restart — the store, while it lives, still makes
the button single-use, and nothing else changes.

Three things fall out of that, all of them wanted:

* **Ownership survives.** ``wizard_state`` is keyed by ``(chat_id, thread_id,
  user_id)``, so a tap from another seat reads no wizard and is refused, with
  or without the store.
* **A stale button is refused.** The step in the payload must be the step the
  FSM is on, and ``wid`` must be this run — a button from a finished wizard
  resolves to nothing.
* **Nothing destructive is reachable.** Every option only advances a form;
  ``Cancel`` abandons it.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from typing import Any, Final

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ctb.bot.app import register_router
from ctb.bot.handlers.common import (
    DEFAULT_BRANCH,
    CreateRequest,
    all_projects,
    create_and_bind,
    created_card,
    quota_error,
    short_error,
    tell,
)
from ctb.bot.handlers.topics import edit_html, resolve_client, resolve_db
from ctb.bot.keyboards import (
    Action,
    Cb,
    NonceError,
    NonceStore,
    button,
    keyboard,
    resolve,
)
from ctb.bot.middleware.routing import Route
from ctb.bot.middleware.tenancy import TenantContext, TenantSettings
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import default_model_for, efforts_for, models_for
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import wizard as wizard_repo
from ctb.delivery.render.html import escape

router = Router(name=__name__)
register_router(router, order=5)

WIZARD_TTL = 30 * 60.0

#: One letter per step. The whole payload has 64 bytes and the restart-proof
#: charset has no ``:``, so the step is a letter and the choice is an index.
_STEP_CODES: Final[dict[str, str]] = {
    "project": "p",
    "branch": "b",
    "agent": "a",
    "model": "m",
    "effort": "e",
}
_STEP_BY_CODE: Final[dict[str, str]] = {v: k for k, v in _STEP_CODES.items()}
#: Step-independent codes. None collides with a step letter.
_DEFAULTS_CODE: Final = "d"
_CANCEL_CODE: Final = "c"
#: The confirm card's two: create it now, or open the full form first.
_CREATE_CODE: Final = "g"
_OPTIONS_CODE: Final = "o"
#: ``secrets.token_urlsafe(4)`` is always six url-safe characters, so the
#: wizard id can be split off the target by position.
_WID_LEN: Final = 6
_WID_BYTES: Final = 4
#: The index is the rest of the code. No step offers ten options today, but a
#: multi-digit index costs nothing and parses the same.
_DIGITS: Final = frozenset("0123456789")

#: The wizard is over — its state expired, it finished, or this is an older
#: run's button. Nothing to resume, so say so instead of blaming the button.
GONE_MESSAGE: Final = "Wizard closed · /new to start again."
#: The wizard moved on (typed branch, another tap) before this tap landed.
STALE_MESSAGE: Final = "That step is done · use the buttons on the card."
#: The payload decodes but names nothing this wizard offered.
INVALID_MESSAGE: Final = "Not a choice on this card."
#: An older build minted the button, so it carries nothing to re-derive. The
#: wizard is still live, so the card is redrawn and the tap simply repeats.
REFRESHED_MESSAGE: Final = "Card refreshed · tap again."


class NewWorkspace(StatesGroup):
    project = State()
    branch = State()
    agent = State()
    model = State()
    effort = State()
    prompt = State()
    #: The task is already known — the line that opened this thread. One tap
    #: from a workspace, and typing again replaces the task rather than
    #: starting a second one.
    confirm = State()


#: What ``wizard_state.state_key`` holds while the card reads "Send the first
#: prompt." The voice path has no aiogram FSM filter, so it matches on this.
PROMPT_STATE_KEY: Final = NewWorkspace.prompt.state
#: The same, for the confirm card. The spoken path writes this into
#: ``wizard_state`` itself, so a tap on the card it posts resolves exactly as a
#: tap on a typed one does.
CONFIRM_STATE_KEY: Final = NewWorkspace.confirm.state


def request_from_wizard(
    data: Mapping[str, Any], text: str, *, settings: TenantSettings
) -> CreateRequest | None:
    """The wizard's answers plus this prompt, or ``None`` if it lost its project.

    Shared so a spoken prompt and a typed one cannot resolve defaults
    differently — that divergence is what sent a transcript to ``/find``.
    """
    projects = data.get("projects")
    project_urls = data.get("project_urls")
    project_id = str(data.get("project_id") or "")
    if not project_id or not isinstance(projects, Mapping):
        return None
    repository_url = (
        project_urls.get(project_id) if isinstance(project_urls, Mapping) else None
    )
    return CreateRequest(
        project_id=project_id,
        project_name=str(projects.get(project_id) or project_id[:8]),
        branch=str(data.get("branch") or settings.default_branch or DEFAULT_BRANCH),
        agent=str(data.get("agent") or settings.default_agent),
        model=str(data.get("model") or settings.default_model),
        effort=str(data.get("effort") or settings.default_effort),
        prompt=text.strip(),
        repository_url=(
            str(repository_url) if isinstance(repository_url, str) else None
        ),
    )


def new_wizard_id() -> str:
    """Identifies one run of the wizard, so an older run's button is refused."""
    return secrets.token_urlsafe(_WID_BYTES)


def _unique(options: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Two buttons that do the same thing are one button.

    The branch step offers the configured default *and* the remembered one,
    which is usually the same branch — a bare ``/new`` rendered "main" twice.
    Options that genuinely differ (``dev`` and ``main``) are both kept, in
    order.
    """
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, value in options:
        if value in seen:
            continue
        seen.add(value)
        unique.append((label, value))
    return unique


def _button(
    label: str,
    code: str,
    *,
    nonces: NonceStore,
    message: Message,
    wid: str,
) -> InlineKeyboardButton:
    return _seat_button(
        label,
        code,
        nonces=nonces,
        wid=wid,
        user_id=message.from_user.id if message.from_user else None,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id or NO_THREAD_ID,
    )


def _seat_button(
    label: str,
    code: str,
    *,
    nonces: NonceStore,
    wid: str,
    user_id: int | None,
    chat_id: int,
    thread_id: int,
) -> InlineKeyboardButton:
    """The same ticket without a ``Message`` to read it off.

    The spoken path has a seat and a user id but never an update, and the two
    surfaces minting *differently scoped* tickets for the same card is the kind
    of divergence that ends with a button that answers "expired" to its owner.
    """
    return button(
        label,
        Action.WIZARD,
        f"{wid}{code}",
        store=nonces,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        ttl=WIZARD_TTL,
        style=(
            "danger"
            if code == _CANCEL_CODE
            else "success"
            if code in {_DEFAULTS_CODE, _CREATE_CODE}
            else "primary"
        ),
        # The owner redeploys constantly, and the FSM behind this card already
        # survives that. The buttons have to as well.
        restartable=True,
    )


def _wizard_keyboard(
    options: Sequence[tuple[str, str]],
    *,
    step: str,
    wid: str,
    nonces: NonceStore,
    message: Message,
    defaults: bool = True,
    columns: int = 2,
) -> InlineKeyboardMarkup:
    """Render the deduped ``options`` — the caller persists the same list."""
    letter = _STEP_CODES.get(step, "")
    items = [
        _button(label, f"{letter}{index}", nonces=nonces, message=message, wid=wid)
        for index, (label, _value) in enumerate(options)
    ]
    rows = [items[index : index + columns] for index in range(0, len(items), columns)]
    if defaults:
        rows.append(
            [
                _button(
                    "Go with defaults →",
                    _DEFAULTS_CODE,
                    nonces=nonces,
                    message=message,
                    wid=wid,
                )
            ]
        )
    rows.append(
        [_button("Cancel", _CANCEL_CODE, nonces=nonces, message=message, wid=wid)]
    )
    return keyboard(rows)


async def _offer(
    message: Message,
    state: FSMContext,
    nonces: NonceStore,
    *,
    step: str,
    fsm_state: State,
    options: Sequence[tuple[str, str]],
    defaults: bool = True,
    columns: int = 2,
) -> InlineKeyboardMarkup:
    """Persist the offered options, then draw exactly those buttons.

    The list written here is the one the payload's index refers to; writing it
    anywhere else would let the two drift.
    """
    data = await state.get_data()
    wid = str(data.get("wid") or "") or new_wizard_id()
    offered = _unique(options)
    await state.set_state(fsm_state)
    await state.update_data(
        {"wid": wid, "step": step, "options": [value for _label, value in offered]}
    )
    return _wizard_keyboard(
        offered,
        step=step,
        wid=wid,
        nonces=nonces,
        message=message,
        defaults=defaults,
        columns=columns,
    )


async def start_wizard(
    message: Message,
    *,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
    nonces: NonceStore | None = None,
) -> None:
    from ctb.bot.keyboards import get_nonce_store

    store = nonces or get_nonce_store()
    database = resolve_db(db)
    defaults = tenant.settings
    try:
        projects = await all_projects(resolve_client(client, tenant))
    except Exception as exc:
        await tell(
            message, f"Projects failed: {escape(short_error(exc))}", silent=False
        )
        return
    if not projects:
        await tell(message, "No Conductor projects found.")
        return
    chat = route.chat or await chats_repo.ensure(
        database, message.chat.id, route.thread_id, kind=route.kind
    )
    await state.set_data(
        {
            "wid": new_wizard_id(),
            "projects": {item.id: item.name or item.id[:8] for item in projects},
            "project_urls": {
                item.id: item.git_remote for item in projects if item.git_remote
            },
            "project_id": chat.default_project_id,
            "branch": chat.default_branch or defaults.default_branch,
            "agent": chat.default_agent or defaults.default_agent,
            "model": chat.default_model or defaults.default_model,
            "effort": chat.default_effort or defaults.default_effort,
        }
    )
    markup = await _offer(
        message,
        state,
        store,
        step="project",
        fsm_state=NewWorkspace.project,
        options=[(item.name or item.id[:8], item.id) for item in projects[:10]],
        defaults=bool(chat.default_project_id),
        columns=1,
    )
    sent = await tell(message, "Project?", reply_markup=markup)
    if sent and message.from_user:
        await wizard_repo.merge_data(
            database,
            message.chat.id,
            route.thread_id,
            user_id=message.from_user.id,
            patch={},
            tg_message_id=sent.message_id,
        )


#: How much of the task the confirm card echoes back. Long enough to recognise
#: a dictation that came out wrong, short enough to leave the buttons on screen.
TASK_ECHO_CHARS: Final = 220


async def start_task(
    message: Message,
    text: str,
    *,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
    nonces: NonceStore | None = None,
) -> None:
    """A task arrived in an empty thread. Offer to make it a workspace.

    This is the whole shape of a threaded DM: *New Chat* is a composer, so a
    line typed there becomes a thread, and the natural reading of a thread with
    nothing in it is "a task I have not started yet". Answering "No session
    here" was true and useless — the person had just said what they wanted.

    It stops one step short of doing it. A workspace bills from the moment it
    exists, so the last thing between a typo and a container is a card that
    says what is about to be created and a button that says create it.
    """
    from ctb.bot.keyboards import get_nonce_store

    store = nonces or get_nonce_store()
    database = resolve_db(db)
    defaults = tenant.settings
    try:
        projects = await all_projects(resolve_client(client, tenant))
    except Exception as exc:
        await tell(
            message, f"Projects failed: {escape(short_error(exc))}", silent=False
        )
        return
    if not projects:
        await tell(message, "No Conductor projects found.")
        return
    chat = route.chat or await chats_repo.ensure(
        database, message.chat.id, route.thread_id, kind=route.kind
    )
    await state.set_data(
        await task_data(
            database, chat=chat, defaults=defaults, projects=projects, text=text
        )
    )
    body, markup = await _confirm_card(message, state, store)
    sent = await tell(message, body, reply_markup=markup)
    if sent and message.from_user:
        await wizard_repo.merge_data(
            database,
            message.chat.id,
            route.thread_id,
            user_id=message.from_user.id,
            patch={},
            tg_message_id=sent.message_id,
        )


async def task_data(
    database: Database,
    *,
    chat: Any,
    defaults: TenantSettings,
    projects: Sequence[Any],
    text: str,
) -> dict[str, Any]:
    """The wizard data a task-first run starts from.

    Shared with the spoken path, which has no aiogram FSM and writes the same
    shape straight into ``wizard_state``. One builder, so a dictated task and a
    typed one cannot resolve a different project or a different model.
    """
    known = {item.id: item.name or item.id[:8] for item in projects}
    project_id = chat.default_project_id if chat is not None else None
    if project_id not in known:
        project_id = await _last_used_project(database, known) or projects[0].id
    return {
        "wid": new_wizard_id(),
        "projects": known,
        "project_urls": {
            item.id: item.git_remote for item in projects if item.git_remote
        },
        "project_id": project_id,
        "step": "confirm",
        "options": [],
        "branch": (chat.default_branch if chat else None) or defaults.default_branch,
        "agent": (chat.default_agent if chat else None) or defaults.default_agent,
        "model": (chat.default_model if chat else None) or defaults.default_model,
        "effort": (chat.default_effort if chat else None) or defaults.default_effort,
        "prompt": text.strip(),
    }


async def _last_used_project(
    database: Database, known: Mapping[str, str]
) -> str | None:
    """The project this team most recently pointed a chat at, if it still exists."""
    recent = sorted(await chats_repo.list_all(database), key=lambda row: row.updated_at)
    for row in reversed(recent):
        if row.default_project_id in known:
            return row.default_project_id
    return None


def task_echo(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= TASK_ECHO_CHARS:
        return collapsed
    return collapsed[: TASK_ECHO_CHARS - 1].rstrip() + "…"


def confirm_card(
    data: Mapping[str, Any],
    *,
    nonces: NonceStore,
    user_id: int | None,
    chat_id: int,
    thread_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """The one card that stands between a typed line and a paid container.

    Pure: it reads the wizard data and mints buttons. Persisting the step is
    the caller's job, because the two surfaces persist it differently — aiogram
    FSM for a typed line, ``wizard_state`` directly for a dictated one.
    """
    wid = str(data.get("wid") or "")
    chosen = chosen_line(data, upto="")
    lines = [f"<b>{escape(task_echo(str(data.get('prompt') or '')))}</b>"]
    if chosen:
        lines.append(f"<i>{escape(chosen)}</i>")

    def control(label: str, code: str) -> InlineKeyboardButton:
        return _seat_button(
            label,
            code,
            nonces=nonces,
            wid=wid,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )

    return "\n".join(lines), keyboard(
        [
            [control("▶️ Start workspace", _CREATE_CODE)],
            [control("⚙️ Change", _OPTIONS_CODE), control("Cancel", _CANCEL_CODE)],
        ]
    )


async def _confirm_card(
    message: Message,
    state: FSMContext,
    nonces: NonceStore,
) -> tuple[str, InlineKeyboardMarkup]:
    """:func:`confirm_card` for the typed path, with the FSM step persisted."""
    data = await state.get_data()
    wid = str(data.get("wid") or "") or new_wizard_id()
    await state.set_state(NewWorkspace.confirm)
    patch = {"wid": wid, "step": "confirm", "options": []}
    await state.update_data(patch)
    return confirm_card(
        {**data, **patch},
        nonces=nonces,
        user_id=message.from_user.id if message.from_user else None,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id or NO_THREAD_ID,
    )


#: The order the wizard asks in, which is the order the breadcrumb reads in.
_CHOSEN_ORDER: Final = ("project", "branch", "agent", "model", "effort")


def chosen_line(data: Mapping[str, Any], upto: str) -> str:
    """What has been picked so far — ``api/main · claude``.

    Five taps used to leave no trace: each step replaced the last and the final
    card said only "Send the first prompt", so a mis-tapped agent was invisible
    until a workspace had been paid for. This costs nothing — the card is one
    message being edited either way.
    """
    projects = data.get("projects") or {}
    parts: list[str] = []
    for step in _CHOSEN_ORDER:
        if step == upto:
            break
        value = data.get("project_id") if step == "project" else data.get(step)
        if not value:
            continue
        text = str(projects.get(value) or value) if step == "project" else str(value)
        if step == "branch" and parts:
            parts[-1] = f"{parts[-1]}/{text}"
            continue
        parts.append(text)
    return " · ".join(parts)


def _ask(data: Mapping[str, Any], step: str, question: str) -> str:
    """The question, under the breadcrumb of everything answered before it."""
    chosen = chosen_line(data, step)
    return f"<i>{escape(chosen)}</i>\n{question}" if chosen else question


async def _edit(
    message: Message,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> bool:
    """Rewrite the one wizard card. ``False`` when the card is no longer there."""
    if message.bot is None:
        raise RuntimeError("Telegram bot is not bound to the message")
    return await edit_html(
        message.bot,
        message.chat.id,
        message.message_id,
        text,
        reply_markup=markup,
    )


async def _ask_project(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    """Re-offer the projects this run already fetched.

    ``start_wizard`` asks this first from a live listing; the confirm card's
    "⚙️ Change" asks it again from ``data["projects"]``, because the project is
    the one field the card *guessed* and therefore the one most worth being
    able to correct. Sending it to the branch step instead left a mis-guessed
    repo unreachable from the card — Cancel and start over, or pay for a
    container against the wrong repository.
    """
    data = await state.get_data()
    projects = data.get("projects")
    known = projects if isinstance(projects, Mapping) else {}
    markup = await _offer(
        message,
        state,
        nonces,
        step="project",
        fsm_state=NewWorkspace.project,
        options=[(str(name), str(pid)) for pid, name in list(known.items())[:10]],
        defaults=bool(data.get("project_id")),
        columns=1,
    )
    await _edit(message, "Project?", markup)


async def _ask_branch(
    message: Message,
    state: FSMContext,
    nonces: NonceStore,
    defaults: TenantSettings,
) -> None:
    """The configured default first, then ``main``, then the pinned one.

    ``DEFAULT_BRANCH=dev`` is the whole point: the branch you almost always want
    is the first button and the one "go with defaults" takes. **``main`` is
    always the second**, because it is the branch every repository has and the
    other answer whatever the default is — offering only the default made the
    step a one-button formality you had to type your way out of.

    :func:`_unique` collapses them when they are the same string, so
    ``DEFAULT_BRANCH=main`` renders one button rather than two identical ones,
    and a chat pinned to something else with ``/defaults branch`` earns a third.
    Anything else is still typed.
    """
    data = await state.get_data()
    configured = defaults.default_branch or DEFAULT_BRANCH
    current = str(data.get("branch") or configured)
    markup = await _offer(
        message,
        state,
        nonces,
        step="branch",
        fsm_state=NewWorkspace.branch,
        options=[
            (configured, configured),
            (DEFAULT_BRANCH, DEFAULT_BRANCH),
            (current, current),
        ],
        columns=3,
    )
    await _edit(message, _ask(data, "branch", "Branch? Type it or tap."), markup)


async def _ask_agent(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    data = await state.get_data()
    markup = await _offer(
        message,
        state,
        nonces,
        step="agent",
        fsm_state=NewWorkspace.agent,
        options=[(name, name) for name in ("claude", "codex", "cursor")],
        columns=3,
    )
    await _edit(message, _ask(data, "agent", "Agent?"), markup)


async def _ask_model(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    data = await state.get_data()
    agent = str(data.get("agent") or "claude")
    markup = await _offer(
        message,
        state,
        nonces,
        step="model",
        fsm_state=NewWorkspace.model,
        options=[(name, name) for name in models_for(agent)[:8]],
        columns=1,
    )
    await _edit(message, _ask(data, "model", "Model?"), markup)


async def _ask_effort(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    data = await state.get_data()
    agent = str(data.get("agent") or "claude")
    markup = await _offer(
        message,
        state,
        nonces,
        step="effort",
        fsm_state=NewWorkspace.effort,
        options=[(name, name) for name in efforts_for(agent) or ("default",)],
        columns=3,
    )
    await _edit(message, _ask(data, "effort", "Effort?"), markup)


async def _ask_prompt(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    data = await state.get_data()
    if data.get("prompt"):
        # The task was known before the form was: this run started from a line
        # typed into an empty thread, and "⚙️ Change" only came back here to fix
        # the model. Asking for the prompt again would throw that line away.
        body, markup = await _confirm_card(message, state, nonces)
        await _edit(message, body, markup)
        return
    markup = await _offer(
        message,
        state,
        nonces,
        step="prompt",
        fsm_state=NewWorkspace.prompt,
        options=(),
        defaults=False,
    )
    # The last card before money is spent shows *everything* chosen, not a
    # prefix of it — this is the only chance to notice a mis-tap for free.
    chosen = chosen_line(data, upto="")
    await _edit(
        message,
        f"<b>{escape(chosen)}</b>\nSend the first prompt."
        if chosen
        else "Send the first prompt.",
        markup,
    )


def _card(query: CallbackQuery) -> Message:
    """The wizard card, re-attributed to the person who tapped it.

    ``_button`` mints each ticket for ``message.from_user``. On a tap that
    message is the card *the bot sent*, so ``from_user`` is the bot — every
    step drawn in response to a tap was bound to the bot's id and then refused
    the owner, as ``wrong_user``, which reads as "This button has expired".
    Only the first card, drawn from the owner's own ``/new``, ever worked.
    """
    card = query.message
    assert isinstance(card, Message)  # guarded by the caller
    return card.model_copy(update={"from_user": query.from_user})


async def _reask(
    step: str,
    message: Message,
    state: FSMContext,
    nonces: NonceStore,
    settings: TenantSettings,
) -> bool:
    """Redraw the step the wizard is on, with buttons this process minted.

    A payload minted by an older build carries nothing to re-derive — the
    restart-proof format did not exist when it was made — so the only honest
    recovery is to hand back a card that works. Costs one tap instead of a
    whole ``/new``.
    """
    match step:
        case "project":
            await _ask_project(message, state, nonces)
        case "branch":
            await _ask_branch(message, state, nonces, settings)
        case "agent":
            await _ask_agent(message, state, nonces)
        case "model":
            await _ask_model(message, state, nonces)
        case "effort":
            await _ask_effort(message, state, nonces)
        case "prompt" | "confirm":
            await _ask_prompt(message, state, nonces)
        case _:
            return False
    return True


def _pick(code: str, data: Mapping[str, Any]) -> tuple[str, str] | str:
    """``(step, value)`` for a tapped option, or the line to answer with.

    Everything the payload claims is checked against the FSM: the run, the
    step, and the index. A payload that survives all three named an option this
    wizard is offering right now.
    """
    step = _STEP_BY_CODE.get(code[:1], "")
    index = code[1:]
    if not step or not index or not set(index) <= _DIGITS:
        return INVALID_MESSAGE
    if data.get("step") != step:
        return STALE_MESSAGE
    options = data.get("options")
    position = int(index)
    if not isinstance(options, list) or position >= len(options):
        return INVALID_MESSAGE
    return step, str(options[position])


@router.callback_query(Cb.filter(F.action == Action.WIZARD.value))
async def wizard_callback(
    query: CallbackQuery,
    state: FSMContext,
    route: Route,
    nonces: NonceStore,
    tenant: TenantContext,
    db: Database | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.WIZARD, store=nonces)
    except NonceError as exc:
        # A payload this build cannot read is usually one an older build minted
        # — the restart-proof format did not exist yet, so there is nothing in
        # it to re-derive. The wizard itself is fine (its state is in SQLite),
        # so redraw the step rather than dead-end on the word "expired".
        step = str((await state.get_data()).get("step") or "")
        if (
            exc.reason == "unknown"
            and step
            and isinstance(query.message, Message)
            and await _reask(step, _card(query), state, nonces, tenant.settings)
        ):
            await query.answer(REFRESHED_MESSAGE, show_alert=True)
            return
        await query.answer(exc.user_message, show_alert=True)
        return
    if not isinstance(query.message, Message):
        await query.answer("Open /new again.", show_alert=True)
        return
    # ``state`` is keyed by (chat, thread, user), so this read is also the
    # ownership check the in-memory store used to make — and unlike the store
    # it is still here after a redeploy.
    data = await state.get_data()
    wid = str(data.get("wid") or "")
    if not wid or ticket.target[:_WID_LEN] != wid:
        await query.answer(GONE_MESSAGE, show_alert=True)
        return
    code = ticket.target[_WID_LEN:]
    if code == _CANCEL_CODE:
        await state.clear()
        await query.answer("Cancelled")
        await _edit(query.message, "New workspace cancelled.", None)
        return
    if code == _DEFAULTS_CODE:
        if not data.get("project_id"):
            await query.answer("Pick a project first.", show_alert=True)
            return
        await query.answer()
        await _ask_prompt(_card(query), state, nonces)
        return
    if code == _OPTIONS_CODE:
        await query.answer()
        await _ask_project(_card(query), state, nonces)
        return
    if code == _CREATE_CODE:
        await query.answer("Starting…")
        await _create_from_card(
            _card(query), state, route=route, tenant=tenant, db=db, data=data
        )
        return
    picked = _pick(code, data)
    if isinstance(picked, str):
        await query.answer(picked, show_alert=True)
        return
    step, selected = picked
    await state.update_data({"project_id" if step == "project" else step: selected})
    await query.answer()
    if step == "project":
        await _ask_branch(_card(query), state, nonces, tenant.settings)
    elif step == "branch":
        await _ask_agent(_card(query), state, nonces)
    elif step == "agent":
        await state.update_data({"model": default_model_for(selected)})
        await _ask_model(_card(query), state, nonces)
    elif step == "model":
        await _ask_effort(_card(query), state, nonces)
    else:
        if selected == "default":
            await state.update_data({"effort": None})
        await _ask_prompt(_card(query), state, nonces)


async def _create_from_card(
    card: Message,
    state: FSMContext,
    *,
    route: Route,
    tenant: TenantContext,
    db: Database | None,
    data: Mapping[str, Any],
) -> None:
    """Spend the money the confirm card was holding back, and report on it.

    The card is both the button that was tapped and the surface the answer is
    written on, so one event still has one face — and the idempotency key is
    the card's own message id, which does not change when a tap is retried.
    """
    request = request_from_wizard(
        data, str(data.get("prompt") or ""), settings=tenant.settings
    )
    if request is None or not request.prompt:
        await state.clear()
        await _edit(card, "Wizard closed · /new to start again.", None)
        return
    refusal = await quota_error(resolve_db(db), tenant.settings)
    if refusal is not None:
        await state.clear()
        await _edit(card, escape(refusal), None)
        return
    try:
        created = await create_and_bind(
            message=card, route=route, request=request, db=db, client=tenant.client
        )
    except Exception as exc:
        # Clear first, for the same reason `typed_prompt` does: the next line
        # typed here is meant for a session, not for a second attempt at this.
        await state.clear()
        await _edit(
            card,
            f"New failed: {escape(short_error(exc))}\n"
            "Nothing was created. Run <code>/new</code> to try again.",
            None,
        )
        return
    await state.clear()
    text, markup = created_card(card.chat.id, created, from_thread=route.thread_id)
    await _edit(card, text, markup)


@router.message(NewWorkspace.confirm, F.text & ~F.text.startswith("/"))
async def typed_confirm(
    message: Message,
    route: Route,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    """A second line while the confirm card is up **replaces** the task.

    Never a second workspace, and never a prompt: nothing exists to prompt yet.
    Dictation gets a word wrong often enough that "say it again" has to be the
    cheapest possible repair.
    """
    text = (message.text or "").strip()
    if not text:
        return
    database = resolve_db(db)
    user_id = message.from_user.id if message.from_user else 0
    await state.update_data({"prompt": text})
    row = await wizard_repo.get(
        database, message.chat.id, route.thread_id, user_id=user_id
    )
    card = (
        message.model_copy(update={"message_id": row.tg_message_id})
        if row is not None and row.tg_message_id
        else None
    )
    body, markup = await _confirm_card(card or message, state, nonces)
    if card is not None and await _edit(card, body, markup):
        return
    # The card was deleted, or there never was one. Post a new one and remember
    # it, or the next correction posts a third.
    sent = await tell(message, body, reply_markup=markup)
    if sent is not None and user_id:
        await wizard_repo.merge_data(
            database,
            message.chat.id,
            route.thread_id,
            user_id=user_id,
            patch={},
            tg_message_id=sent.message_id,
        )


@router.message(NewWorkspace.branch, F.text & ~F.text.startswith("/"))
async def typed_branch(
    message: Message, route: Route, state: FSMContext, nonces: NonceStore
) -> None:
    branch = (message.text or "").strip()[:160]
    if not branch:
        return
    await state.update_data({"branch": branch})
    # `route.thread_id`, never the raw `message_thread_id`: `start_wizard` wrote
    # this row under the seat the router resolved, which folds a forum's
    # General back to 0 and reads a DM topic Telegram does not tag. Reading it
    # back by a second rule is how the card is "not found" and the wizard
    # answers by posting a fresh message instead of editing the one on screen.
    row = await wizard_repo.get(
        resolve_db(None),
        message.chat.id,
        route.thread_id,
        user_id=message.from_user.id if message.from_user else 0,
    )
    # Continue by editing the one wizard card, not the user's branch message.
    target = message
    if row and row.tg_message_id:
        target = message.model_copy(update={"message_id": row.tg_message_id})
    await _ask_agent(target, state, nonces)


@router.message(NewWorkspace.prompt, F.text & ~F.text.startswith("/"))
async def typed_prompt(
    message: Message,
    state: FSMContext,
    route: Route,
    tenant: TenantContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    data = await state.get_data()
    wizard_row = await wizard_repo.get(
        resolve_db(db),
        message.chat.id,
        route.thread_id,
        user_id=message.from_user.id if message.from_user else 0,
    )
    request = request_from_wizard(data, message.text or "", settings=tenant.settings)
    if request is None:
        await state.clear()
        await tell(
            message, "Wizard expired. Run <code>/new</code> again.", silent=False
        )
        return
    # `resolve_new_request` guards the one-line `/new text` path, and the wizard
    # has never gone through it — so a team at its limit could spend past it by
    # taking the form instead. That was always wrong; it matters now because the
    # form is the *usual* way in rather than the long way round.
    refusal = await quota_error(resolve_db(db), tenant.settings)
    if refusal is not None:
        await state.clear()
        await tell(message, escape(refusal), silent=False)
        return
    try:
        created = await create_and_bind(
            message=message,
            route=route,
            request=request,
            db=db,
            client=tenant.client,
        )
    except Exception as exc:
        # Clear first. The message reads terminal ("at its limit of 50
        # workspaces"), so the next thing typed is meant for a session — but a
        # live wizard would swallow it and try to create another workspace,
        # silently, for the next thirty minutes. The sibling failure branch
        # above already clears; this one forgetting to was an oversight.
        await state.clear()
        await tell(
            message,
            f"New failed: {escape(short_error(exc))}\n"
            "Nothing was created. Run <code>/new</code> to try again.",
            silent=False,
        )
        return
    await state.clear()
    # Same shape as `/new` and adopt: one event should not have three faces.
    text, markup = created_card(message.chat.id, created, from_thread=route.thread_id)
    if wizard_row is not None and wizard_row.tg_message_id is not None:
        card = message.model_copy(update={"message_id": wizard_row.tg_message_id})
        await _edit(card, text, markup)
    else:
        await tell(message, text, reply_markup=markup)
