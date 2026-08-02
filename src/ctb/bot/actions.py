"""Fan turn-machine actions out to cards, notices, and forum topics."""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from collections.abc import Sequence
from typing import Final

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, ReactionTypeEmoji

from ctb import signals
from ctb.bot.handlers import topics
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, current_tenant, tenant_scope
from ctb.db.repo import (
    chats,
    ci,
    deliveries,
    prompts,
    sessions,
    tenancy,
    transcript,
)
from ctb.db.repo.sessions import SessionRow
from ctb.delivery.outbox import Outbox, Priority, notice_message_id
from ctb.delivery.render.adapters.base import one_line
from ctb.delivery.render.html import escape, escape_attr
from ctb.delivery.status_card import StatusCards
from ctb.github.links import PullRequestRef, find_pull_request
from ctb.logging import get_logger
from ctb.turn.machine import format_duration
from ctb.turn.state import (
    Action,
    CardButton,
    Finalize,
    Notify,
    NotifyLevel,
    SetTopicMarker,
    TurnSummary,
)

__all__ = ["BotActionSink", "changed_files_line", "finish_key", "finish_line"]

log = get_logger(__name__)

#: How far back a finished turn is scanned for the pull request it opened. The
#: link is normally in the last message; a long tail of tool output can push it
#: a little further, and a window that reaches into *previous* turns would
#: re-arm a watch on a PR this turn never touched.
CI_SCAN_MESSAGES: Final = 40


#: Changed files named on the receipt before it stops naming them. Five is what
#: fits a phone line-wrapped to two lines; past that the count is the answer and
#: the list is scrolling.
FINISH_FILES_SHOWN: Final = 5


def finish_line(summary: TurnSummary) -> str:
    """``✅ <b>Done</b> · 1m32s · 12 tools · 5 files`` — the completion receipt.

    Deliberately the same vocabulary as the status card above it (``signals``,
    ``format_duration``): two surfaces describing one event must not word it
    differently. Failure leads with the marker and the reason, because that is
    the whole message when a turn did not work.

    A successful turn that changed files names them on a second line. "5 files"
    alone is the question, not the answer — the paths are what tells you whether
    the agent went where you expected, and they are the one thing the chat no
    longer prints per edit under the default verbosity.
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
    line = f"{marker} <b>{head}</b>{tail}"
    files = changed_files_line(summary)
    return f"{line}\n{files}" if files else line


def changed_files_line(summary: TurnSummary) -> str:
    """``<code>src/a.py</code> · <code>src/b.py</code> · +3 more``, or ``""``.

    Empty for a turn that changed nothing, and for a failed one: a stopped turn
    already leads with why it stopped, and a half-finished edit list underneath
    reads like a claim that the work landed.
    """
    if not summary.ok or not summary.files:
        return ""
    shown = summary.files[:FINISH_FILES_SHOWN]
    rendered = " · ".join(f"<code>{escape(path)}</code>" for path in shown)
    hidden = max(0, summary.files_changed - len(shown))
    return f"{rendered} · +{hidden} more" if hidden else rendered


def finish_key(session_id: str, row: SessionRow | None) -> str:
    """The delivery id of a session's receipt — one per prompt, not per finalize.

    The prompt is the only identity that survives the machine changing its
    mind. A turn's own identity does not: ``_finalize`` clears
    ``turn_started_at`` and ``turn_ids``, so the next premature finish looks
    like a brand-new turn and would earn a brand-new notification.

    With no prompt of ours behind it — an adopted session driven from a Mac —
    there is nothing to key on but the cursor, which is the old behaviour and
    the right one there: those finishes really are separate turns.
    """
    if row is not None and row.last_prompt_at:
        return f"turn-done:{session_id}:p{row.last_prompt_at}"
    return f"turn-done:{session_id}:{row.cursor_session_index if row else 0}"


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
                pr = await self._finished_pr(session_id)
                if pr is not None:
                    await self._share_review_pr(
                        pr,
                        session_id=session_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                    )
                    await self._watch_ci(
                        pr,
                        session_id=session_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                    )

    async def _finished_pr(self, session_id: str) -> PullRequestRef | None:
        for stored in await transcript.recent(
            self._db, session_id, limit=CI_SCAN_MESSAGES
        ):
            found = find_pull_request(stored.content_json or "")
            if found is not None:
                return found
        return None

    async def _share_review_pr(
        self,
        pr: PullRequestRef,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int,
    ) -> None:
        """Send and pin the pull request link as the topic's review handle."""
        try:
            key = f"pr-review:{session_id}:{pr.owner}:{pr.repo}:{pr.number}"
            message_id = notice_message_id(key)
            row = await deliveries.get(self._db, (session_id, message_id, 0, chat_id))
            if row is not None and row.tg_message_id is not None:
                await self._pin_message(chat_id, thread_id, row.tg_message_id)
                return
            if row is not None:
                return

            html = (
                f'PR ready for review: <a href="{escape_attr(pr.url)}">'
                f"{escape(pr.slug)}</a>"
            )
            sent = await self._outbox.send_text(
                html, chat_id=chat_id, thread_id=thread_id, silent=True
            )
            if not sent:
                return
            created = await self._outbox.enqueue_notice(
                html,
                session_id=session_id,
                key=key,
                chat_id=chat_id,
                thread_id=thread_id,
                priority=Priority.NORMAL,
                silent=True,
            )
            if created:
                await deliveries.mark_sent(
                    self._db,
                    (session_id, message_id, 0, chat_id),
                    tg_message_id=sent[0],
                )
            await self._pin_message(chat_id, thread_id, sent[0])
        except Exception as exc:  # noqa: BLE001 - review link is a bonus
            log.warning(
                "bot.pr_review_notice_failed",
                session_id=session_id,
                pr=pr.slug,
                error=repr(exc),
            )

    async def _pin_message(self, chat_id: int, thread_id: int, message_id: int) -> None:
        if thread_id == NO_THREAD_ID:
            return
        with contextlib.suppress(TelegramAPIError, AttributeError):
            await self._bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message_id,
                disable_notification=True,
            )

    async def _watch_ci(
        self,
        pr: PullRequestRef | None = None,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int,
    ) -> None:
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
            found = pr or await self._finished_pr(session_id)
            if found is None:
                return
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

        **Posted once, then edited.** The machine can conclude a turn is over
        more than once — an ``idle`` in the quiet of a long tool call is
        byte-for-byte the ``idle`` after the last one — and this used to buzz
        every time, because the key carried the cursor and the cursor moves.
        Ten receipts for one task is what that looks like on a phone. The key
        now carries the *prompt*, so a re-finalize collides with the row that
        is already there and revises that message instead: one notification,
        then silent corrections as the real numbers arrive.

        Never raises: a missing receipt must not take down the poller that
        produced the work it is describing.
        """
        try:
            row = await sessions.get(self._db, session_id)
            chat = await chats.get(self._db, chat_id, thread_id)
            if chat is not None and chat.notify == "off":
                return
            key = finish_key(session_id, row)
            text = finish_line(summary)
            controls: tuple[str, ...] = (
                (CardButton.ARCHIVE.value,)
                if summary.ok and row is not None and row.workspace_id is not None
                else ()
            )
            created = await self._outbox.enqueue_notice(
                text,
                session_id=session_id,
                key=key,
                chat_id=chat_id,
                thread_id=thread_id,
                priority=Priority.NORMAL,
                silent=False,
                control_buttons=controls,
            )
            if not created:
                await self._revise_finish(
                    key,
                    text,
                    session_id=session_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    control_buttons=controls,
                )
        except Exception as exc:  # noqa: BLE001 - a receipt never stops a turn
            log.warning(
                "bot.finish_notice_failed", session_id=session_id, error=repr(exc)
            )

    async def _revise_finish(
        self,
        key: str,
        text: str,
        *,
        session_id: str,
        chat_id: int,
        thread_id: int,
        control_buttons: tuple[str, ...],
    ) -> None:
        """Update a receipt that has already been sent. Telegram edits are silent.

        A receipt still sitting in the queue is left alone: nobody has seen it,
        and it will go out carrying whatever the last enqueue wrote. Only a
        message that has actually landed is worth correcting.
        """
        row = await deliveries.get(
            self._db, (session_id, notice_message_id(key), 0, chat_id)
        )
        if row is None or row.tg_message_id is None:
            return
        await topics.edit_html(
            self._bot,
            chat_id,
            row.tg_message_id,
            text,
            reply_markup=self._control_markup(
                control_buttons,
                session_id=session_id,
                chat_id=chat_id,
                thread_id=thread_id,
            ),
        )

    def _control_markup(
        self,
        buttons: tuple[str, ...],
        *,
        session_id: str,
        chat_id: int,
        thread_id: int,
    ) -> InlineKeyboardMarkup | None:
        if not buttons:
            return None
        try:
            from ctb.bot.keyboards import status_card_keyboard

            return status_card_keyboard(
                [CardButton(value) for value in buttons],
                session_id,
                columns=1,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        except Exception:
            return None

    async def _set_topic_marker(self, session_id: str, action: SetTopicMarker) -> None:
        # Straight to the session: the marker is the *session's* now, not its
        # workspace's. Two sessions of one workspace used to fight over a single
        # `topic_marker` — whichever ticked last won, so a room could read
        # "⚙️ working" for a session you were not looking at.
        try:
            await topics.apply_marker(
                self._bot,
                self._db,
                session_id,
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
