"""Bare ``/new``: one edited card, state persisted in ``wizard_state``."""

from __future__ import annotations

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
    CreateRequest,
    all_projects,
    create_and_bind,
    short_error,
    tell,
)
from ctb.bot.handlers.topics import edit_html, jump_url, resolve_client, resolve_db
from ctb.bot.keyboards import (
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


class NewWorkspace(StatesGroup):
    project = State()
    branch = State()
    agent = State()
    model = State()
    effort = State()
    prompt = State()


def _rows(
    options: list[tuple[str, str]],
    *,
    nonces: NonceStore,
    message: Message,
    columns: int = 2,
) -> list[list[InlineKeyboardButton]]:
    items = [
        button(
            label,
            "wiz",
            target,
            store=nonces,
            user_id=message.from_user.id if message.from_user else None,
            chat_id=message.chat.id,
            thread_id=message.message_thread_id or NO_THREAD_ID,
            ttl=WIZARD_TTL,
            style=(
                "danger"
                if target == "cancel"
                else "success"
                if target == "defaults"
                else "primary"
            ),
        )
        for label, target in options
    ]
    return [items[index : index + columns] for index in range(0, len(items), columns)]


def _wizard_keyboard(
    options: list[tuple[str, str]],
    *,
    nonces: NonceStore,
    message: Message,
    defaults: bool = True,
    columns: int = 2,
) -> InlineKeyboardMarkup:
    rows = _rows(options, nonces=nonces, message=message, columns=columns)
    if defaults:
        rows.append(
            _rows(
                [("Go with defaults →", "defaults")],
                nonces=nonces,
                message=message,
                columns=1,
            )[0]
        )
    rows.append(
        _rows(
            [("Cancel", "cancel")],
            nonces=nonces,
            message=message,
            columns=1,
        )[0]
    )
    return keyboard(rows)


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
    await state.set_state(NewWorkspace.project)
    await state.set_data(
        {
            "projects": {item.id: item.name or item.id[:8] for item in projects},
            "project_id": chat.default_project_id,
            "branch": chat.default_branch or "main",
            "agent": chat.default_agent or settings.default_agent,
            "model": chat.default_model or settings.default_model,
            "effort": chat.default_effort or settings.default_effort,
        }
    )
    options = [
        (item.name or item.id[:8], f"project:{item.id}") for item in projects[:10]
    ]
    sent = await tell(
        message,
        "Project?",
        reply_markup=_wizard_keyboard(
            options,
            nonces=store,
            message=message,
            defaults=bool(chat.default_project_id),
            columns=1,
        ),
    )
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


async def _ask_branch(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    data = await state.get_data()
    current = str(data.get("branch") or "main")
    await state.set_state(NewWorkspace.branch)
    await _edit(
        message,
        "Branch? Type it or tap.",
        _wizard_keyboard(
            [("main", "branch:main"), (current, f"branch:{current}")],
            nonces=nonces,
            message=message,
        ),
    )


async def _ask_agent(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    await state.set_state(NewWorkspace.agent)
    await _edit(
        message,
        "Agent?",
        _wizard_keyboard(
            [(name, f"agent:{name}") for name in ("claude", "codex", "cursor")],
            nonces=nonces,
            message=message,
            columns=3,
        ),
    )


async def _ask_model(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    data = await state.get_data()
    agent = str(data.get("agent") or "claude")
    await state.set_state(NewWorkspace.model)
    await _edit(
        message,
        "Model?",
        _wizard_keyboard(
            [(name, f"model:{name}") for name in models_for(agent)[:8]],
            nonces=nonces,
            message=message,
            columns=1,
        ),
    )


async def _ask_effort(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    data = await state.get_data()
    agent = str(data.get("agent") or "claude")
    options = efforts_for(agent) or ("default",)
    await state.set_state(NewWorkspace.effort)
    await _edit(
        message,
        "Effort?",
        _wizard_keyboard(
            [(name, f"effort:{name}") for name in options],
            nonces=nonces,
            message=message,
            columns=3,
        ),
    )


async def _ask_prompt(message: Message, state: FSMContext, nonces: NonceStore) -> None:
    await state.set_state(NewWorkspace.prompt)
    await _edit(
        message,
        "Send the first prompt.",
        _wizard_keyboard(
            [],
            nonces=nonces,
            message=message,
            defaults=False,
        ),
    )


@router.callback_query(Cb.filter(F.action == "wiz"))
async def wizard_callback(
    query: CallbackQuery,
    state: FSMContext,
    nonces: NonceStore,
    settings: Settings,
) -> None:
    try:
        ticket = resolve(query, expect="wiz", store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    if not isinstance(query.message, Message):
        await query.answer("Open /new again.", show_alert=True)
        return
    value = ticket.target
    if value == "cancel":
        await state.clear()
        await query.answer("Cancelled")
        await _edit(query.message, "New workspace cancelled.", None)
        return
    data = await state.get_data()
    if value == "defaults":
        if not data.get("project_id"):
            await query.answer("Pick a project first.", show_alert=True)
            return
        await query.answer()
        await _ask_prompt(query.message, state, nonces)
        return
    kind, separator, selected = value.partition(":")
    if not separator:
        await query.answer("Expired. Run /new again.", show_alert=True)
        return
    await state.update_data({f"{kind}_id" if kind == "project" else kind: selected})
    if kind == "project":
        await query.answer()
        await _ask_branch(query.message, state, nonces)
    elif kind == "branch":
        await query.answer()
        await _ask_agent(query.message, state, nonces)
    elif kind == "agent":
        await state.update_data({"model": default_model_for(selected)})
        await query.answer()
        await _ask_model(query.message, state, nonces)
    elif kind == "model":
        await query.answer()
        await _ask_effort(query.message, state, nonces)
    elif kind == "effort":
        if selected == "default":
            await state.update_data({"effort": None})
        await query.answer()
        await _ask_prompt(query.message, state, nonces)
    else:
        await query.answer("Invalid choice.", show_alert=True)


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
    projects = data.get("projects")
    project_id = str(data.get("project_id") or "")
    if not project_id or not isinstance(projects, dict):
        await state.clear()
        await tell(
            message, "Wizard expired. Run <code>/new</code> again.", silent=False
        )
        return
    request = CreateRequest(
        project_id=project_id,
        project_name=str(projects.get(project_id) or project_id[:8]),
        branch=str(data.get("branch") or "main"),
        agent=str(data.get("agent") or settings.default_agent),
        model=str(data.get("model") or settings.default_model),
        effort=str(data.get("effort") or settings.default_effort),
        prompt=(message.text or "").strip(),
    )
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
