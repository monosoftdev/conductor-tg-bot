"""Fan turn-machine actions out to cards, notices, and forum topics."""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from collections.abc import Sequence
from typing import Final

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReactionTypeEmoji

from ctb import signals
from ctb.bot.handlers import topics
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, current_tenant, tenant_scope
from ctb.db.repo import chats, ci, prompts, sessions, tenancy, transcript
from ctb.delivery.outbox import Outbox, Priority
from ctb.delivery.render.adapters.base import one_line
from ctb.delivery.render.html import escape
from ctb.delivery.status_card import StatusCards
from ctb.github.links import find_pull_request
from ctb.logging import get_logger
from ctb.turn.machine import format_duration
from ctb.turn.state import (
    Action,
    Finalize,
    Notify,
    NotifyLevel,
    SetTopicMarker,
    TurnSummary,
)

__all__ = ["BotActionSink", "finish_line"]

log = get_logger(__name__)

#: How far back a finished turn is scanned for the pull request it opened. The
#: link is normally in the last message; a long tail of tool output can push it
#: a little further, and a window that reaches into *previous* turns would
#: re-arm a watch on a PR this turn never touched.
CI_SCAN_MESSAGES: Final = 40


def finish_line(summary: TurnSummary) -> str:
    """``✅ <b>Done</b> · 1m32s · 12 tools · 5 files`` — the completion receipt.

    Deliberately the same vocabulary as the status card above it (``signals``,
    ``format_duration``): two surfaces describing one event must not word it
    differently. Failure leads with the marker and the reason, because that is
    the whole message when a turn did not work.
    """
    marker = signals.DONE if summary.ok else signals.ERROR
    head = "Done" if summary.ok else "Stopped"
    parts: list[str] = []
    if summary.duration_ms > 0:
        parts.append(format_duration(summary.duration_ms))
    if summary.tool_calls:
        parts.append(f"{summary.tool_calls} tools")
    if summary.files_changed:
        parts.append(
            f"{summary.files_changed} file{'' if summary.files_changed == 1 else 's'}"
        )
    if not summary.ok and summary.error:
        parts.append(one_line(summary.error)[:120])
    tail = "".join(f" · {escape(part)}" for part in parts)
    return f"{marker} <b>{head}</b>{tail}"


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
                await self._announce_finish(
                    action.summary,
                    session_id=session_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                )
                await self._watch_ci(
                    session_id=session_id, chat_id=chat_id, thread_id=thread_id
                )

    async def _watch_ci(self, *, session_id: str, chat_id: int, thread_id: int) -> None:
        """Start watching CI if this turn announced a pull request.

        The link *is* the announcement — the agent is asked to end on it — so
        nothing new has to be plumbed through the turn machine to find it. Read
        newest-first and stop at the first hit: a turn that mentions an older
        PR before opening its own ends on the one it opened.

        Never raises. A watch is a bonus on top of a finished turn; it does not
        get to interfere with the receipt for it.
        """
        try:
            tenant_id = current_tenant()
            if tenant_id is None:
                return
            for stored in await transcript.recent(
                self._db, session_id, limit=CI_SCAN_MESSAGES
            ):
                found = find_pull_request(stored.content_json or "")
                if found is None:
                    continue
                tenant = await tenancy.get(self._system_db, tenant_id)
                # No token, no watch: the row would only be claimed once and
                # abandoned, and this way a team that has not opted in pays
                # nothing at all.
                if tenant is None or not tenant.github_key_fp:
                    return
                await ci.watch(
                    self._db,
                    owner=found.owner,
                    repo=found.repo,
                    pr_number=found.number,
                    session_id=session_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                )
                log.info("bot.ci_watching", session_id=session_id, pr=found.slug)
                return
        except Exception as exc:  # noqa: BLE001 - never fails a finished turn
            log.warning("bot.ci_watch_failed", session_id=session_id, error=repr(exc))

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

    async def _announce_finish(
        self,
        summary: TurnSummary,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int,
    ) -> None:
        """The one notification a turn is allowed to make.

        Everything a turn emits is delivered silently under the default
        ``/notify quiet`` — the narration, the answer, the file edits — because
        a phone that buzzes eight times for one task is a phone you mute. This
        is the buzz, and there is exactly one of it: the moment the work is
        finished, which is the only moment worth interrupting somebody for.

        **It has to be a message.** The status card already says ``done`` and
        would be the natural home for this, but a card is an *edit* and Telegram
        never notifies for an edit. A card cannot ring a phone.

        It also carries what the chat no longer prints per file: how many tools
        ran and how many files changed. That trade is the point — one line at
        the end instead of twenty while you are trying to read the answer.

        Keyed on the cursor the session finished at, so a ``Finalize``
        re-derived after a redeploy lands on the same primary key instead of
        announcing one turn twice. Never raises: a missing receipt must not
        take down the poller that produced the work it is describing.
        """
        try:
            row = await sessions.get(self._db, session_id)
            chat = await chats.get(self._db, chat_id, thread_id)
            if chat is not None and chat.notify == "off":
                return
            await self._outbox.enqueue_notice(
                finish_line(summary),
                session_id=session_id,
                key=f"turn-done:{session_id}:{row.cursor_session_index if row else 0}",
                chat_id=chat_id,
                thread_id=thread_id,
                priority=Priority.NORMAL,
                silent=False,
            )
        except Exception as exc:  # noqa: BLE001 - a receipt never stops a turn
            log.warning(
                "bot.finish_notice_failed", session_id=session_id, error=repr(exc)
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
