"""Bounded, durable Telegram voice-note worker."""

from __future__ import annotations

import asyncio
import io
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message

from ctb.bot.handlers.common import (
    CancelDispatcher,
    create_and_bind_input,
    request_cancel,
    resolve_new_request,
    short_error,
    submit_prompt,
    workspace_name,
)
from ctb.bot.handlers.core import (
    board_lines,
    board_rows,
    find_text,
    session_overview_lines,
)
from ctb.bot.handlers.topics import edit_html, send_html
from ctb.bot.keyboards import (
    Action,
    NonceStore,
    button,
    confirm_keyboard,
    keyboard,
)
from ctb.bot.middleware.routing import Route
from ctb.bot.middleware.tenancy import TenantSettings
from ctb.conductor.client import ConductorClient
from ctb.conductor.pool import ClientPool
from ctb.db.connection import Database, now_ms, tenant_scope
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import prompts as prompts_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import tenancy
from ctb.db.repo import voice_inputs as voice_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.voice_inputs import VoiceInputRow
from ctb.delivery.render.html import escape
from ctb.logging import get_logger
from ctb.settings import Settings
from ctb.voice.intent import (
    VoiceCommand,
    VoiceIntent,
    VoiceIntentKind,
    parse_intent,
)
from ctb.voice.pool import ProviderPool
from ctb.voice.provider import SpeechProvider, TranscriptionError

__all__ = ["VoiceEnqueueStatus", "VoiceService"]

log = get_logger(__name__)

#: Said whenever a voice action needs the Conductor API and no key is stored.
_NO_KEY = (
    "This workspace has no Conductor API key yet. An owner can set one with "
    "<code>/key</code> in a private message."
)

_OPERATION_NAMESPACE: Final = uuid.UUID("ab10e43e-134f-4559-84a4-ad0374ed6606")
_TRANSCRIPT_LIMIT: Final = 16_000
# Long enough to check the transcription was right, short enough to stay under
# two phone lines. The receipt is a verification aid, not a re-read of the note.
_AUDIT_LIMIT: Final = 140
_POLL_SECONDS: Final = 0.5
_ERROR_BACKOFF_SECONDS: Final = 5.0
_MAINTENANCE_SECONDS: Final = 3_600.0
_STATIC_KEYTERMS: Final[tuple[str, ...]] = (
    "Conductor",
    "Telegram",
    "aiogram",
    "Railway",
    "SQLite",
    "sessionIndex",
    "messageId",
    "Pyright",
    "Ruff",
    "pytest",
)


class VoiceEnqueueStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"
    TOO_LONG = "too_long"
    TOO_LARGE = "too_large"


@dataclass(slots=True)
class VoiceService:
    """Transcribes voice notes, per tenant, with per-tenant credentials.

    Voice is the one feature whose data genuinely leaves the perimeter, so
    there is no platform-wide speech key: a tenant that has not stored one has
    no provider, and its notes are refused rather than billed to somebody else.
    """

    settings: Settings
    db: Database
    system_db: Database
    bot: Bot
    clients: ClientPool
    supervisor: CancelDispatcher
    nonces: NonceStore
    #: ``None`` means the platform has no speech vendor configured at all.
    providers: ProviderPool | None = None
    _stop: asyncio.Event = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._stop = asyncio.Event()

    @property
    def available(self) -> bool:
        """Whether the *platform* serves voice at all.

        Whether a given tenant can use it is decided per job, by whether they
        have stored a speech key.
        """
        return self.settings.voice_enabled and self.providers is not None

    async def enqueue(self, message: Message, route: Route) -> VoiceEnqueueStatus:
        media = message.voice or getattr(message, "audio", None)
        if media is None:
            raise ValueError("message has no voice or audio")
        if not self.settings.voice_enabled or not route.tenant_voice_enabled:
            return VoiceEnqueueStatus.DISABLED
        if self.providers is None:
            return VoiceEnqueueStatus.UNCONFIGURED
        if media.duration > self.settings.voice_max_duration_seconds:
            return VoiceEnqueueStatus.TOO_LONG
        if (
            media.file_size is not None
            and media.file_size > self.settings.voice_max_file_bytes
        ):
            return VoiceEnqueueStatus.TOO_LARGE
        user_id = message.from_user.id if message.from_user is not None else 0
        action_id = str(
            uuid.uuid5(
                _OPERATION_NAMESPACE,
                f"{message.chat.id}:{message.message_id}",
            )
        )
        route_kind = route.kind
        if (
            not route.via_reply
            and message.chat.type in {"group", "supergroup"}
            and (message.message_thread_id or 0) in {0, 1}
        ):
            route_kind = "general"
        elif route.via_reply and route.session_id:
            route_kind = "topic"
        _row, inserted = await voice_repo.create(
            self.db,
            chat_id=message.chat.id,
            tg_message_id=message.message_id,
            thread_id=message.message_thread_id or 0,
            user_id=user_id,
            file_id=media.file_id,
            file_unique_id=media.file_unique_id,
            file_name=getattr(media, "file_name", None),
            mime_type=getattr(media, "mime_type", None),
            duration_seconds=media.duration,
            file_size=media.file_size,
            route_kind=route_kind,
            route_session_id=route.session_id,
            route_workspace_id=route.workspace_id,
            provider=self.settings.voice_stt_provider,
            model=self.settings.voice_stt_model,
            action_id=action_id,
        )
        return VoiceEnqueueStatus.ACCEPTED if inserted else VoiceEnqueueStatus.DUPLICATE

    async def retry(self, chat_id: int, tg_message_id: int) -> bool:
        return await voice_repo.requeue(self.db, chat_id, tg_message_id)

    async def attach_ack(
        self, chat_id: int, tg_message_id: int, ack_message_id: int
    ) -> None:
        await voice_repo.set_ack_message(
            self.db,
            chat_id,
            tg_message_id,
            ack_message_id,
        )

    async def run(self) -> None:
        """Serve voice jobs, or nothing at all when the feature is off.

        Voice is optional and unproven. Nothing in here may take the process
        down with it: a disabled install spawns no tasks, and every loop body
        catches, logs and carries on. See ``ctb.__main__.OPTIONAL_SERVICES``.
        """
        if not self.available:
            log.info(
                "voice.disabled",
                enabled=self.settings.voice_enabled,
                configured=self.providers is not None,
            )
            await self._stop.wait()
            return
        await self._recover()
        async with asyncio.TaskGroup() as tasks:
            for index in range(self.settings.voice_max_concurrent):
                tasks.create_task(self._worker(index), name=f"ctb-voice-{index}")
            tasks.create_task(self._maintenance(), name="ctb-voice-maintenance")

    async def stop(self) -> None:
        self._stop.set()
        if self.providers is not None:
            await self.providers.aclose()

    async def _recover(self) -> None:
        """Requeue what a dead process left behind; report what we gave up on."""
        try:
            # Cross-tenant maintenance, so the worker pool.
            recovery = await voice_repo.recover_stale(self.system_db)
            pruned = await self._prune()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("voice.recovery_failed", error=short_error(exc))
            return
        log.info(
            "voice.worker_started",
            recovered=recovery.requeued,
            abandoned=len(recovery.abandoned),
            pruned=pruned,
        )
        for row in recovery.abandoned:
            with suppress(Exception):
                await self._send_failure(row, "Transcription kept failing.")

    async def _prune(self) -> int:
        cutoff = now_ms() - self.settings.voice_completed_retention_days * 86_400_000
        return await voice_repo.prune_terminal(self.system_db, before=cutoff)

    async def _worker(self, index: int) -> None:
        while not self._stop.is_set():
            try:
                await self._tick(index)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The bot's job is answering "did my agent finish?". A voice
                # worker that cannot reach SQLite or Telegram backs off; it
                # never escapes into the TaskGroup.
                log.warning("voice.worker_error", worker=index, error=short_error(exc))
                await self._pause(_ERROR_BACKOFF_SECONDS)

    async def _tick(self, index: int) -> None:
        # Claiming is cross-tenant, so it runs on the worker pool. Everything
        # after it acts on *one* tenant's rows and runs in that tenant's scope.
        row = await voice_repo.claim_next(self.system_db)
        if row is None:
            await self._pause(_POLL_SECONDS)
            return
        if row.tenant_id is None:  # pragma: no cover - the column is NOT NULL
            return
        # The failure path needs the scope as much as the happy path does.
        # Written as one block rather than try/except *around* the scope,
        # because `async with` exits before an outer `except` body runs — and
        # `voice_repo.fail` on the tenant-scoped pool with no tenant set raises
        # `TenantScopeError`, replacing the real error and leaving the job stuck
        # in `transcribing` until the next redeploy. The user never hears why
        # their voice note went nowhere.
        async with tenant_scope(row.tenant_id):
            try:
                await self._process(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = short_error(exc)
                log.warning(
                    "voice.job_failed",
                    chat_id=row.chat_id,
                    tg_message_id=row.tg_message_id,
                    worker=index,
                    error=error,
                )
                with suppress(Exception):
                    await voice_repo.fail(self.db, row, error=error)
                with suppress(Exception):
                    await self._send_failure(row, error)

    async def _maintenance(self) -> None:
        while not self._stop.is_set():
            await self._pause(_MAINTENANCE_SECONDS)
            if self._stop.is_set():
                return
            try:
                pruned = await self._prune()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("voice.prune_failed", error=short_error(exc))
                continue
            if pruned:
                log.info("voice.jobs_pruned", count=pruned)

    async def _defaults(self, row: VoiceInputRow) -> TenantSettings:
        """This job's tenant's agent/model/effort defaults."""
        tenant = (
            None
            if row.tenant_id is None
            else await tenancy.get(self.system_db, row.tenant_id)
        )
        if tenant is None:
            return TenantSettings()
        return TenantSettings.of(tenant, self.settings)

    async def _provider_for(self, row: VoiceInputRow) -> SpeechProvider | None:
        """This job's tenant's speech provider, or ``None`` if unset."""
        if self.providers is None or row.tenant_id is None:
            return None
        tenant = await tenancy.get(self.system_db, row.tenant_id)
        if tenant is None:  # pragma: no cover - FK makes this unreachable
            return None
        return await self.providers.get(tenant)

    async def _client_for(self, row: VoiceInputRow) -> ConductorClient | None:
        if row.tenant_id is None:
            return None
        tenant = await tenancy.get(self.system_db, row.tenant_id)
        if tenant is None or not tenant.has_conductor_key:
            return None
        return await self.clients.get(tenant)

    async def _pause(self, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _process(self, row: VoiceInputRow) -> None:
        current = row
        provider = await self._provider_for(current)
        if current.transcript is None:
            if provider is None:
                raise TranscriptionError(
                    "This workspace has no speech key. An owner can set one "
                    "with /voicekey in a private message."
                )
            audio = await self._download(current)
            result = await provider.transcribe(
                audio,
                filename=current.file_name or f"voice-{current.tg_message_id}.ogg",
                mime_type=current.mime_type or "audio/ogg",
                keyterms=await self._keyterms(current),
            )
            transcript = " ".join(result.text.split())[:_TRANSCRIPT_LIMIT]
            if sum(char.isalnum() for char in transcript) < 2:
                raise TranscriptionError("No clear speech detected.")
            intent = parse_intent(transcript, self.settings.voice_wake_phrases)
            refreshed = await voice_repo.mark_transcribed(
                self.db,
                current,
                transcript=transcript,
                language=result.language,
                intent_json=intent.to_json(),
            )
            if refreshed is None:  # pragma: no cover - row cannot vanish normally
                raise RuntimeError("Voice job disappeared.")
            current = refreshed
        else:
            intent = (
                VoiceIntent.from_json(current.intent_json)
                if current.intent_json
                else parse_intent(current.transcript, self.settings.voice_wake_phrases)
            )

        await voice_repo.mark_dispatching(self.db, current)
        terminal = await self._dispatch(current, intent)
        if terminal:
            await voice_repo.complete(self.db, current)

    async def _download(self, row: VoiceInputRow) -> bytes:
        tg_file = await self.bot.get_file(row.file_id)
        effective_size = tg_file.file_size or row.file_size
        if (
            effective_size is not None
            and effective_size > self.settings.voice_max_file_bytes
        ):
            raise TranscriptionError("Voice note is over the 20 MB limit.")
        if not tg_file.file_path:
            raise TranscriptionError("Telegram did not return a download path.")
        destination = io.BytesIO()
        await self.bot.download_file(tg_file.file_path, destination=destination)
        audio = destination.getvalue()
        if len(audio) > self.settings.voice_max_file_bytes:
            raise TranscriptionError("Voice note is over the 20 MB limit.")
        if not audio:
            raise TranscriptionError("Voice note is empty.")
        return audio

    async def _keyterms(self, row: VoiceInputRow) -> list[str]:
        terms: list[str] = list(_STATIC_KEYTERMS)
        if row.route_session_id:
            session = await sessions_repo.get(self.db, row.route_session_id)
            if session is not None:
                terms.extend(
                    item
                    for item in (
                        session.title,
                        session.agent,
                        session.model,
                        session.effort,
                    )
                    if item
                )
        if row.route_workspace_id:
            workspace = await workspaces_repo.get(self.db, row.route_workspace_id)
            if workspace is not None:
                terms.extend(
                    item
                    for item in (workspace.name, workspace.branch, workspace.topic_name)
                    if item
                )
        return terms

    async def _dispatch(self, row: VoiceInputRow, intent: VoiceIntent) -> bool:
        if intent.kind is VoiceIntentKind.EMPTY:
            await voice_repo.wait_for_user(self.db, row, reason="empty transcript")
            await self._send(row, "🎙 No clear speech detected.")
            return False
        if intent.kind is VoiceIntentKind.AMBIGUOUS:
            await voice_repo.wait_for_user(self.db, row, reason=intent.reason)
            await self._send(
                row,
                f"🎙 <b>Needs clarification</b>\n{escape(self._audit(intent.text))}",
            )
            return False
        mode = (await self._defaults(row)).voice_mode
        if mode == "shadow":
            await self._send(
                row,
                f"🎙 <b>Heard</b>\n{escape(self._audit(intent.text))}\n"
                "<i>Shadow mode · nothing sent.</i>",
            )
            return True
        if intent.kind is VoiceIntentKind.COMMAND:
            if mode != "commands":
                await voice_repo.wait_for_user(
                    self.db, row, reason="voice commands are in preview mode"
                )
                await self._send(
                    row,
                    f"🎙 Command detected: <b>{escape(str(intent.command))}</b>",
                )
                return False
            return await self._command(row, intent)
        return await self._prompt(row, intent.text)

    async def _prompt(self, row: VoiceInputRow, text: str) -> bool:
        conductor = await self._client_for(row)
        if conductor is None:
            await self._send(row, _NO_KEY)
            return True
        if row.route_kind == "general":
            rendered = await find_text(conductor, text)
            await self._send(
                row,
                f"🎙 <b>Search</b> · {escape(self._audit(text))}\n{rendered}",
            )
            return True
        if not row.route_session_id:
            await self._send(row, "No session here. Use /new or /s.")
            return True
        _message_id, state = await submit_prompt(
            db=self.db,
            client=conductor,
            session_id=row.route_session_id,
            text=text,
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            tg_message_id=row.tg_message_id,
            message_id=row.action_id,
        )
        pending = await prompts_repo.outstanding_count(self.db, row.route_session_id)
        suffix = f" · {pending} pending" if pending > 1 else ""
        # Transcript first: that is the part the owner has to check. The state
        # word is a footnote, not a headline.
        await self._send(
            row,
            f"🎙 {escape(self._audit(text))}\n<i>{escape(state)}{suffix}</i>",
        )
        return True

    async def _command(self, row: VoiceInputRow, intent: VoiceIntent) -> bool:
        command = intent.command
        conductor = await self._client_for(row)
        if command is VoiceCommand.STOP:
            if not row.route_session_id:
                await self._send(row, "No session here.")
                return True
            accepted = await request_cancel(
                self.supervisor,
                row.route_session_id,
                requested_by=row.user_id,
            )
            await self._send(row, "Stopping…" if accepted else "Stop unavailable.")
            return True
        if command is VoiceCommand.FIND:
            if not intent.argument:
                await self._send(row, "Say what to find after the command.")
                return True
            if conductor is None:
                await self._send(row, _NO_KEY)
                return True
            await self._send(row, await find_text(conductor, intent.argument))
            return True
        if command is VoiceCommand.BOARD:
            if conductor is None:
                await self._send(row, _NO_KEY)
                return True
            rows = await board_rows(self.db, conductor)
            await self._send(
                row, "\n".join(board_lines(rows)) if rows else "No live workspaces."
            )
            return True
        if command is VoiceCommand.MODE:
            session = (
                await sessions_repo.get(self.db, row.route_session_id)
                if row.route_session_id
                else None
            )
            if session is None:
                await self._send(row, "No session here.")
            else:
                workspace = (
                    await workspaces_repo.get(self.db, session.workspace_id)
                    if session.workspace_id
                    else None
                )
                pending = await prompts_repo.outstanding_count(self.db, session.id)
                await self._send(
                    row,
                    "\n".join(
                        session_overview_lines(
                            session,
                            workspace,
                            pending=pending,
                        )
                    ),
                )
            return True
        if command is VoiceCommand.DONE:
            if not row.route_workspace_id:
                await self._send(row, "No workspace here.")
                return True
            workspace = await workspaces_repo.get(self.db, row.route_workspace_id)
            name = (
                workspace_name(workspace) if workspace else row.route_workspace_id[:8]
            )
            markup = confirm_keyboard(
                Action.ARCHIVE,
                row.route_workspace_id,
                name,
                verb="Archive",
                store=self.nonces,
                user_id=row.user_id,
                chat_id=row.chat_id,
                thread_id=row.thread_id,
            )
            await self._send(
                row,
                f"Archive <b>{escape(name)}</b>?",
                reply_markup=markup,
            )
            return True
        if command is VoiceCommand.NEW:
            if not intent.argument:
                await self._send(row, "Say the new workspace prompt after the command.")
                return True
            chat = await chats_repo.get(self.db, row.chat_id, row.thread_id)
            session = (
                await sessions_repo.get(self.db, row.route_session_id)
                if row.route_session_id
                else None
            )
            route = Route(
                chat_id=row.chat_id,
                thread_id=row.thread_id,
                kind=row.route_kind,
                chat=chat,
                session=session,
            )
            if conductor is None:
                await self._send(row, _NO_KEY)
                return True
            request = await resolve_new_request(
                text=intent.argument,
                route=route,
                defaults=await self._defaults(row),
                db=self.db,
                client=conductor,
            )
            created = await create_and_bind_input(
                bot=self.bot,
                chat_id=row.chat_id,
                chat_type="private" if row.route_kind == "dm" else "supergroup",
                tg_message_id=row.tg_message_id,
                route=route,
                request=request,
                db=self.db,
                client=conductor,
                action_id=row.action_id,
            )
            await self._send(
                row, f"Created <b>{escape(created.label)}</b> · prompt queued."
            )
            return True
        await self._send(row, "Unknown voice command.")
        return True

    async def _send_failure(self, row: VoiceInputRow, error: str) -> None:
        target = f"{row.chat_id}:{row.tg_message_id}"
        markup = keyboard(
            [
                [
                    button(
                        "Retry",
                        "voice_retry",
                        target,
                        store=self.nonces,
                        user_id=row.user_id,
                        chat_id=row.chat_id,
                        thread_id=row.thread_id,
                    )
                ]
            ]
        )
        await self._send(
            row,
            f"🎙 Failed: {escape(error)}",
            reply_markup=markup,
        )

    async def _send(
        self,
        row: VoiceInputRow,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        latest = await voice_repo.get(self.db, row.chat_id, row.tg_message_id)
        ack_message_id = latest.ack_message_id if latest is not None else None
        if ack_message_id is not None:
            edited = await edit_html(
                self.bot,
                row.chat_id,
                ack_message_id,
                text,
                reply_markup=reply_markup,
            )
            if edited:
                return
        await send_html(
            self.bot,
            row.chat_id,
            text,
            thread_id=row.thread_id,
            reply_markup=reply_markup,
            reply_to_message_id=row.tg_message_id,
        )

    @staticmethod
    def _audit(text: str) -> str:
        clipped = text[:_AUDIT_LIMIT]
        return clipped + ("…" if len(text) > _AUDIT_LIMIT else "")
