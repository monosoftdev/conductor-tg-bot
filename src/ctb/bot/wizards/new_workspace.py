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
    short_error,
    tell,
)
from ctb.bot.handlers.topics import edit_html, jump_url, resolve_client, resolve_db
from ctb.bot.keyboards import (
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
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import default_model_for, efforts_for, models_for
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import wizard as wizard_repo
from ctb.delivery.render.html import escape
from ctb.settings import Settings

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
#: Step-independent codes. Neither collides with a step letter.
_DEFAULTS_CODE: Final = "d"
_CANCEL_CODE: Final = "c"
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


#: What ``wizard_state.state_key`` holds while the card reads "Send the first
#: prompt." The voice path has no aiogram FSM filter, so it matches on this.
PROMPT_STATE_KEY: Final = NewWorkspace.prompt.state


def request_from_wizard(
    data: Mapping[str, Any], text: str, *, settings: Settings
) -> CreateRequest | None:
    """The wizard's answers plus this prompt, or ``None`` if it lost its project.

    Shared so a spoken prompt and a typed one cannot resolve defaults
    differently — that divergence is what sent a transcript to ``/find``.
    """
    projects = data.get("projects")
    project_id = str(data.get("project_id") or "")
    if not project_id or not isinstance(projects, Mapping):
        return None
    return CreateRequest(
        project_id=project_id,
        project_name=str(projects.get(project_id) or project_id[:8]),
        branch=str(data.get("branch") or settings.default_branch or DEFAULT_BRANCH),
        agent=str(data.get("agent") or settings.default_agent),
        model=str(data.get("model") or settings.default_model),
        effort=str(data.get("effort") or settings.default_effort),
        prompt=text.strip(),
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
    return button(
        label,
        Action.WIZARD,
        f"{wid}{code}",
        store=nonces,
        user_id=message.from_user.id if message.from_user else None,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id or NO_THREAD_ID,
        ttl=WIZARD_TTL,
        style=(
            "danger"
            if code == _CANCEL_CODE
            else "success"
            if code == _DEFAULTS_CODE
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
    settings: Settings,
    state: FSMContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
    nonces: NonceStore | None = None,
) -> None:
    from ctb.bot.keyboards import get_nonce_store

    store = nonces or get_nonce_store()
    database = resolve_db(db)
    try:
        projects = await all_projects(resolve_client(client))
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
            "project_id": chat.default_project_id,
            "branch": chat.default_branch or settings.default_branch,
            "agent": chat.default_agent or settings.default_agent,
            "model": chat.default_model or settings.default_model,
            "effort": chat.default_effort or settings.default_effort,
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


async def _edit(
    message: Message,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    if message.bot is None:
        raise RuntimeError("Telegram bot is not bound to the message")
    await edit_html(
        message.bot,
        message.chat.id,
        message.message_id,
        text,
        reply_markup=markup,
    )


async def _ask_branch(
    message: Message,
    state: FSMContext,
    nonces: NonceStore,
    settings: Settings,
) -> None:
    """Offer the configured default first, then the last-used branch.

    ``DEFAULT_BRANCH=dev`` is the whole point: the branch you almost always
    want is the first button, and the remembered one only earns a second button
    when it is genuinely different.
    """
    data = await state.get_data()
    configured = settings.default_branch or DEFAULT_BRANCH
    current = str(data.get("branch") or configured)
    markup = await _offer(
        message,
        state,
        nonces,
        step="branch",
        fsm_state=NewWorkspace.branch,
        options=[(configured, configured), (current, current)],
    )
    await _edit(message, "Branch? Type it or tap.", markup)


async def _ask_agent(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    markup = await _offer(
        message,
        state,
        nonces,
        step="agent",
        fsm_state=NewWorkspace.agent,
        options=[(name, name) for name in ("claude", "codex", "cursor")],
        columns=3,
    )
    await _edit(message, "Agent?", markup)


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
    await _edit(message, "Model?", markup)


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
    await _edit(message, "Effort?", markup)


async def _ask_prompt(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    markup = await _offer(
        message,
        state,
        nonces,
        step="prompt",
        fsm_state=NewWorkspace.prompt,
        options=(),
        defaults=False,
    )
    await _edit(message, "Send the first prompt.", markup)


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
    settings: Settings,
) -> bool:
    """Redraw the step the wizard is on, with buttons this process minted.

    A payload minted by an older build carries nothing to re-derive — the
    restart-proof format did not exist when it was made — so the only honest
    recovery is to hand back a card that works. Costs one tap instead of a
    whole ``/new``.
    """
    match step:
        case "branch":
            await _ask_branch(message, state, nonces, settings)
        case "agent":
            await _ask_agent(message, state, nonces)
        case "model":
            await _ask_model(message, state, nonces)
        case "effort":
            await _ask_effort(message, state, nonces)
        case "prompt":
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
    nonces: NonceStore,
    settings: Settings,
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
            and await _reask(step, _card(query), state, nonces, settings)
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
    picked = _pick(code, data)
    if isinstance(picked, str):
        await query.answer(picked, show_alert=True)
        return
    step, selected = picked
    await state.update_data({"project_id" if step == "project" else step: selected})
    await query.answer()
    if step == "project":
        await _ask_branch(_card(query), state, nonces, settings)
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


@router.message(NewWorkspace.branch, F.text & ~F.text.startswith("/"))
async def typed_branch(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    branch = (message.text or "").strip()[:160]
    if not branch:
        return
    await state.update_data({"branch": branch})
    row = await wizard_repo.get(
        resolve_db(None),
        message.chat.id,
        message.message_thread_id or NO_THREAD_ID,
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
    settings: Settings,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    data = await state.get_data()
    wizard_row = await wizard_repo.get(
        resolve_db(db),
        message.chat.id,
        message.message_thread_id or NO_THREAD_ID,
        user_id=message.from_user.id if message.from_user else 0,
    )
    request = request_from_wizard(data, message.text or "", settings=settings)
    if request is None:
        await state.clear()
        await tell(
            message, "Wizard expired. Run <code>/new</code> again.", silent=False
        )
        return
    try:
        created = await create_and_bind(
            message=message,
            route=route,
            request=request,
            db=db,
            client=client,
        )
    except Exception as exc:
        await tell(message, f"New failed: {escape(short_error(exc))}", silent=False)
        return
    await state.clear()
    target = (
        jump_url(message.chat.id, created.thread_id)
        if created.thread_id
        else created.deep_link
    )
    label = "Open topic" if created.thread_id else "Open in Conductor"
    markup = keyboard([[url_button(label, target)]]) if target else None
    text = f"✓ <b>{escape(created.label)}</b>"
    if wizard_row is not None and wizard_row.tg_message_id is not None:
        card = message.model_copy(update={"message_id": wizard_row.tg_message_id})
        await _edit(card, text, markup)
    else:
        await tell(message, text, reply_markup=markup)
