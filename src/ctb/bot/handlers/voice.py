"""Telegram voice-note entry point and retry callback."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import tell
from ctb.bot.keyboards import Cb, NonceError, NonceStore, resolve
from ctb.bot.middleware.routing import Route
from ctb.voice.service import VoiceEnqueueStatus, VoiceService

ROUTER_ORDER = 850
router = Router(name=__name__)
register_router(router, order=ROUTER_ORDER)


@router.message(F.voice | F.audio)
async def audio_message(
    message: Message,
    route: Route,
    voice_service: VoiceService | None = None,
) -> None:
    if voice_service is None:
        await tell(message, "Voice is unavailable.", silent=False)
        return
    status = await voice_service.enqueue(message, route)
    if status is VoiceEnqueueStatus.ACCEPTED:
        ack = await tell(message, "🎙 Transcribing…", silent=True)
        if ack is not None:
            await voice_service.attach_ack(
                message.chat.id,
                message.message_id,
                ack.message_id,
            )
    elif status is VoiceEnqueueStatus.DUPLICATE:
        await tell(message, "🎙 Already processing.")
    elif status in {VoiceEnqueueStatus.DISABLED, VoiceEnqueueStatus.UNCONFIGURED}:
        # A fat-fingered mic tap does not deserve a deployment lecture. The
        # owner configured this bot; one line is the whole message.
        await tell(message, "Voice is off.")
    elif status is VoiceEnqueueStatus.TOO_LONG:
        await tell(
            message,
            f"Voice note is too long · max "
            f"{voice_service.settings.voice_max_duration_seconds}s.",
            silent=False,
        )
    elif status is VoiceEnqueueStatus.TOO_LARGE:
        await tell(message, "Voice note is too large · max 20 MB.", silent=False)


@router.callback_query(Cb.filter(F.action == "voice_retry"))
async def retry_voice(
    query: CallbackQuery,
    nonces: NonceStore,
    voice_service: VoiceService | None = None,
) -> None:
    try:
        ticket = resolve(query, expect="voice_retry", store=nonces)
    except NonceError as exc:
        await query.answer(exc.user_message, show_alert=True)
        return
    if voice_service is None:
        await query.answer("Voice is unavailable.", show_alert=True)
        return
    try:
        chat_text, message_text = ticket.target.split(":", 1)
        queued = await voice_service.retry(int(chat_text), int(message_text))
    except (ValueError, TypeError):
        queued = False
    await query.answer("Retrying…" if queued else "Nothing to retry.", show_alert=True)
