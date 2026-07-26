"""Fan turn-machine actions out to cards, notices, and forum topics."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReactionTypeEmoji

from ctb.bot.handlers import topics
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database
from ctb.db.repo import prompts, sessions
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
        outbox: Outbox,
        status_cards: StatusCards,
        *,
        owner_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._db = db
        self._outbox = outbox
        self._cards = status_cards
        self._owner_id = owner_id

    async def auth_fatal(self, session_id: str) -> None:
        """Durably tell the owner once that the Conductor key was rejected."""
        if self._owner_id is None:
            return
        await self._outbox.enqueue_notice(
            "Conductor API key rejected. Update "
            "<code>CONDUCTOR_API_KEY</code>; session polling is paused.",
            session_id=session_id,
            key="conductor-auth-fatal",
            chat_id=self._owner_id,
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
