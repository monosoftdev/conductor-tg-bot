"""Plain-text routing.

General is structurally search-only. Topics and the one DM seat submit through
the durable prompt ledger, then echo the *original* text's destination.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import (
    react_received,
    request_cancel,
    safe_title,
    short_error,
    submit_prompt,
    tell,
)
from ctb.bot.handlers.core import run_find
from ctb.bot.handlers.topics import resolve_client, resolve_db
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
from ctb.bot.middleware.tenancy import TenantContext
from ctb.conductor.client import ConductorClient
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import prompts as prompts_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import transcript as transcript_repo
from ctb.delivery.render.html import escape
from ctb.turn.supervisor import Supervisor

ROUTER_ORDER = 900
router = Router(name=__name__)
register_router(router, order=ROUTER_ORDER)

_SERVICE_CONTENT: frozenset[ContentType] = frozenset(
    {
        ContentType.NEW_CHAT_MEMBERS,
        ContentType.LEFT_CHAT_MEMBER,
        ContentType.CHAT_OWNER_LEFT,
        ContentType.CHAT_OWNER_CHANGED,
        ContentType.NEW_CHAT_TITLE,
        ContentType.NEW_CHAT_PHOTO,
        ContentType.DELETE_CHAT_PHOTO,
        ContentType.GROUP_CHAT_CREATED,
        ContentType.SUPERGROUP_CHAT_CREATED,
        ContentType.CHANNEL_CHAT_CREATED,
        ContentType.MESSAGE_AUTO_DELETE_TIMER_CHANGED,
        ContentType.MIGRATE_TO_CHAT_ID,
        ContentType.MIGRATE_FROM_CHAT_ID,
        ContentType.PINNED_MESSAGE,
        ContentType.FORUM_TOPIC_CREATED,
        ContentType.FORUM_TOPIC_EDITED,
        ContentType.FORUM_TOPIC_CLOSED,
        ContentType.FORUM_TOPIC_REOPENED,
        ContentType.GENERAL_FORUM_TOPIC_HIDDEN,
        ContentType.GENERAL_FORUM_TOPIC_UNHIDDEN,
        ContentType.VIDEO_CHAT_SCHEDULED,
        ContentType.VIDEO_CHAT_STARTED,
        ContentType.VIDEO_CHAT_ENDED,
        ContentType.VIDEO_CHAT_PARTICIPANTS_INVITED,
    }
)


def is_general_cockpit(message: Message, route: Route) -> bool:
    """Telegram may identify the built-in General topic as thread ``1``."""
    return not route.via_reply and (
        route.is_general
        or (
            message.chat.type in {"group", "supergroup"}
            and (message.message_thread_id or 0) in {0, 1}
        )
    )


@router.message(
    F.photo
    | F.document
    | F.video
    | F.animation
    | F.video_note
    | F.sticker
    | F.location
    | F.contact
    | F.poll
)
async def unsupported_attachment(message: Message) -> None:
    """Never silently imply that Conductor saw a binary attachment."""
    await tell(message, "📎 Not forwarded — text or voice only.", silent=False)


@router.message(F.text & ~F.text.startswith("/"))
async def plain_text(
    message: Message,
    route: Route,
    tenant: TenantContext,
    nonces: NonceStore,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await tell(message, "Empty message · send the instruction again.")
        return
    if is_general_cockpit(message, route):
        # No send path exists here. Search first; an explicit single-use button
        # is required to turn this text into a prompt. The button rides on the
        # search results — one typed line must never cost two notifications.
        database = resolve_db(db)
        rows = sorted(
            (
                row
                for row in await chats_repo.list_bound(database)
                if row.last_prompt_at
            ),
            key=lambda row: row.last_prompt_at or 0,
            reverse=True,
        )
        markup = None
        if rows and rows[0].session_id:
            session = await sessions_repo.get(database, rows[0].session_id)
            destination = safe_title(
                session.title if session else None, rows[0].session_id[:8]
            )
            markup = keyboard(
                [
                    [
                        button(
                            f"Send to {destination}",
                            Action.SEND,
                            f"{rows[0].session_id}\n{text}",
                            store=nonces,
                            user_id=message.from_user.id if message.from_user else None,
                            chat_id=message.chat.id,
                            thread_id=message.message_thread_id or 0,
                        )
                    ]
                ]
            )
        await run_find(message, text, client=tenant.client, reply_markup=markup)
        return

    if not route.session_id:
        await tell(
            message, "No session here. Use <code>/new</code> or <code>/s</code>."
        )
        return
    database = resolve_db(db)
    try:
        _, state = await submit_prompt(
            db=database,
            client=resolve_client(client, tenant),
            session_id=route.session_id,
            text=text,
            chat_id=message.chat.id,
            thread_id=message.message_thread_id or 0,
            tg_message_id=message.message_id,
        )
    except Exception as exc:
        await tell(message, f"Prompt failed: {escape(short_error(exc))}", silent=False)
        return
    if await react_received(message):
        return
    pending = await prompts_repo.outstanding_count(database, route.session_id)
    destination = (
        route.session.title
        if route.session is not None and route.session.title
        else route.session_id[:8]
    )
    wording = f"queued ({pending} pending)" if pending > 1 else state
    markup = keyboard(
        [
            [
                button(
                    "Stop",
                    Action.STOP,
                    route.session_id,
                    store=nonces,
                    user_id=message.from_user.id if message.from_user else None,
                    chat_id=message.chat.id,
                    thread_id=message.message_thread_id or 0,
                )
            ]
        ]
    )
    await tell(
        message,
        f"→ <b>{escape(destination)}</b> · {escape(wording)}",
        reply_markup=markup,
    )


@router.callback_query(Cb.filter(F.action == Action.STOP.value))
async def stop_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    supervisor: Supervisor | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.STOP, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    try:
        accepted = await request_cancel(
            supervisor, ticket.target, requested_by=query.from_user.id
        )
    except Exception as exc:
        await query.answer(f"Stop failed: {short_error(exc)}", show_alert=True)
        return
    if not accepted:
        await query.answer("Stop unavailable — retry.", show_alert=True)
        return
    await query.answer("Stopping…")


@router.callback_query(Cb.filter(F.action == Action.RETRY.value))
async def retry_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    tenant: TenantContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.RETRY, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    database = resolve_db(db)
    recent = await prompts_repo.list_for_session(database, ticket.target, limit=1)
    if not recent:
        await query.answer("Nothing to retry.", show_alert=True)
        return
    source = recent[0]
    try:
        await submit_prompt(
            db=database,
            client=resolve_client(client, tenant),
            session_id=ticket.target,
            text=source.body,
            chat_id=ticket.chat_id
            or (query.message.chat.id if query.message else query.from_user.id),
            thread_id=ticket.thread_id
            or (
                (query.message.message_thread_id or 0)
                if isinstance(query.message, Message)
                else 0
            ),
        )
    except Exception as exc:
        await query.answer(f"Retry failed: {short_error(exc)}", show_alert=True)
        return
    await query.answer("Queued again")


@router.callback_query(Cb.filter(F.action == Action.CLEAR_QUEUE.value))
async def clear_queue_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    supervisor: Supervisor | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.CLEAR_QUEUE, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    try:
        accepted = await request_cancel(
            supervisor,
            ticket.target,
            requested_by=query.from_user.id,
        )
    except Exception as exc:
        await query.answer(f"Clear failed: {short_error(exc)}", show_alert=True)
        return
    if not accepted:
        await query.answer("Clear unavailable. Retry shortly.", show_alert=True)
        return
    await query.answer("Clearing queued prompts…")


@router.callback_query(Cb.filter(F.action == Action.CHECK.value))
async def check_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    tenant: TenantContext,
    client: ConductorClient | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.CHECK, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    try:
        status = await resolve_client(client, tenant).get_session_status(ticket.target)
    except Exception as exc:
        await query.answer(f"Check failed: {short_error(exc)}", show_alert=True)
        return
    detail = f" · {status.error_text}" if status.error_text else ""
    await query.answer(f"{status.status.value}{detail}"[:200], show_alert=True)


@router.callback_query(Cb.filter(F.action == Action.TRANSCRIPT.value))
async def transcript_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.TRANSCRIPT, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    rows = reversed(
        await transcript_repo.recent(resolve_db(db), ticket.target, limit=200)
    )
    body = "\n\n".join(
        f"## {row.session_index} · {row.type}\n\n```json\n"
        f"{row.content_json or '{}'}\n```"
        for row in rows
    )
    await query.answer("Sending transcript…")
    if query.bot is None or query.message is None:
        return
    await query.bot.send_document(
        chat_id=query.message.chat.id,
        document=BufferedInputFile(
            (body or "No cached messages.").encode(),
            filename=f"session-{ticket.target[:8]}.md",
        ),
        message_thread_id=(
            query.message.message_thread_id
            if isinstance(query.message, Message)
            else None
        ),
    )


@router.callback_query(Cb.filter(F.action == Action.SEND.value))
async def send_callback(
    query: CallbackQuery,
    nonces: NonceStore,
    tenant: TenantContext,
    db: Database | None = None,
    client: ConductorClient | None = None,
) -> None:
    try:
        ticket = resolve(query, expect=Action.SEND, store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    session_id, separator, text = ticket.target.partition("\n")
    if not separator or not session_id or not text:
        await query.answer("Expired. Search again.", show_alert=True)
        return
    try:
        await submit_prompt(
            db=resolve_db(db),
            client=resolve_client(client, tenant),
            session_id=session_id,
            text=text,
            chat_id=ticket.chat_id or query.from_user.id,
            thread_id=ticket.thread_id,
        )
    except Exception as exc:
        await query.answer(f"Prompt failed: {short_error(exc)}", show_alert=True)
        return
    await query.answer("Queued")
    if isinstance(query.message, Message):
        with contextlib.suppress(Exception):
            await query.message.edit_reply_markup(reply_markup=None)


@router.edited_message(F.text)
async def edited_text(message: Message) -> None:
    """Telegram edits cannot mutate a prompt already accepted by Conductor."""
    await tell(
        message,
        "Edit not resent · send the correction as a new message.",
        silent=False,
    )


@router.edited_message()
async def edited_payload(message: Message) -> None:
    """Captions and future editable payloads follow the same explicit rule."""
    if message.content_type in _SERVICE_CONTENT:
        return
    await tell(
        message,
        "Edit not resent · send the correction as a new message.",
        silent=False,
    )


@router.callback_query()
async def unknown_callback(query: CallbackQuery) -> None:
    """Never leave an old or malformed inline button spinning forever."""
    await query.answer(
        "Expired control · run /mode for fresh buttons.", show_alert=True
    )


@router.message()
async def unsupported_message(message: Message) -> None:
    """Final message catch-all: user payloads get an answer, service events do not."""
    if message.content_type in _SERVICE_CONTENT:
        return
    if message.content_type == ContentType.TEXT:
        await tell(message, "Unknown command · use /help.", silent=False)
        return
    await tell(message, "📎 Not forwarded — text or voice only.", silent=False)
