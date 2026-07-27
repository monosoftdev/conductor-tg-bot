"""Fan turn-machine actions out to cards, notices, and forum topics."""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from collections.abc import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReactionTypeEmoji

from ctb.bot.handlers import topics
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, tenant_scope
from ctb.db.repo import prompts, sessions, tenancy
from ctb.delivery.outbox import Outbox, Priority
from ctb.delivery.render.html import escape
from ctb.delivery.status_card import StatusCards
from ctb.logging import get_logger
from ctb.turn.state import Action, Finalize, Notify, NotifyLevel, SetTopicMarker

__all__ = ["BotActionSink"]

log = get_logger(__name__)


class BotActionSink:
    """Execute every bot-facing action without coupling it to the poller.

    Status cards deliberately ignore actions they do not own. This composite
    keeps that isolation while ensuring notices and topic transitions are not
    silently discarded by production wiring.
    """

    def __init__(
        self,
        bot: Bot,
        db: Database,
        system_db: Database,
        outbox: Outbox,
        status_cards: StatusCards,
    ) -> None:
        self._bot = bot
        self._db = db
        self._system_db = system_db
        self._outbox = outbox
        self._cards = status_cards

    async def auth_fatal(self, session_id: str, tenant_id: uuid.UUID) -> None:
        """Tell *this tenant's* owners, once, that their key was rejected.

        Every other tenant keeps polling, so the message says "your workspace",
        not "the bot", and names the fix the person reading it can actually
        perform.
        """
        recipients = await tenancy.list_owner_ids(self._system_db, tenant_id)
        primary = await tenancy.primary_chat(self._system_db, tenant_id)
        targets: list[int] = list(recipients)
        if primary is not None and primary.chat_id not in targets:
            targets.append(primary.chat_id)
        # The notice is a *tenant's* row. The supervisor calls this from its own
        # reconcile loop, outside any scope, so enter one — otherwise the
        # tenant_id default is NULL and the one message that explains the outage
        # is the one that fails to enqueue.
        async with tenant_scope(tenant_id):
            await self._enqueue_notices(session_id, tenant_id, targets)

    async def _enqueue_notices(
        self, session_id: str, tenant_id: uuid.UUID, targets: list[int]
    ) -> None:
        for chat_id in targets:
            await self._outbox.enqueue_notice(
                "Conductor rejected this team's API key. Send "
                "<code>/key</code> in a private message to set a new one; "
                "polling is paused until you do.",
                session_id=session_id,
                key=f"conductor-auth-fatal:{tenant_id}",
                chat_id=chat_id,
                thread_id=NO_THREAD_ID,
                priority=Priority.ERROR,
                silent=False,
            )

    async def handle(
        self,
        actions: Sequence[Action],
        *,
        session_id: str,
        chat_id: int,
        thread_id: int = NO_THREAD_ID,
        deep_link: str | None = None,
    ) -> None:
        await self._cards.handle(
            actions,
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            deep_link=deep_link,
        )

        for action in actions:
            if isinstance(action, Notify):
                await self._notify(
                    action,
                    session_id=session_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                )
            elif isinstance(action, SetTopicMarker):
                await self._set_topic_marker(session_id, action)
            elif isinstance(action, Finalize):
                await self._finish_prompt_reactions(
                    session_id,
                    count=max(1, action.summary.prompts),
                    ok=action.summary.ok,
                )

    async def _notify(
        self,
        action: Notify,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int,
    ) -> None:
        digest = hashlib.sha256(action.text.encode()).hexdigest()[:16]
        key = action.once_key or f"{action.level.value}:{digest}"
        loud = action.level is NotifyLevel.LOUD
        await self._outbox.enqueue_notice(
            escape(action.text),
            session_id=session_id,
            key=key,
            chat_id=chat_id,
            thread_id=thread_id,
            priority=Priority.ERROR if loud else Priority.NORMAL,
            silent=not loud,
        )

    async def _set_topic_marker(self, session_id: str, action: SetTopicMarker) -> None:
        row = await sessions.get(self._db, session_id)
        if row is None or row.workspace_id is None:
            return
        try:
            await topics.apply_marker(
                self._bot,
                self._db,
                row.workspace_id,
                action.marker,
            )
        except Exception as exc:
            # Cosmetic state never gets to interrupt transcript delivery.
            log.warning(
                "bot.topic_marker_failed",
                session_id=session_id,
                marker=action.marker.value,
                error=repr(exc),
            )

    async def _finish_prompt_reactions(
        self,
        session_id: str,
        *,
        count: int,
        ok: bool,
    ) -> None:
        """Turn the prompt's 👀 receipt into a compact completion signal."""
        settled = 0
        for row in await prompts.list_for_session(
            self._db,
            session_id,
            limit=max(10, count * 3),
        ):
            if row.state != "witnessed":
                continue
            settled += 1
            if row.chat_id is not None and row.tg_message_id is not None:
                with contextlib.suppress(TelegramAPIError, AttributeError):
                    await self._bot.set_message_reaction(
                        chat_id=row.chat_id,
                        message_id=row.tg_message_id,
                        reaction=[ReactionTypeEmoji(emoji="👍" if ok else "😢")],
                    )
            if settled >= count:
                return
