"""``/digest`` — one card that answers "what needs me?".

The topic list is the console: every room wears its state as a title prefix and
Telegram sorts them by last activity. That is the right *navigation* and it is a
poor *report*. It cannot say how long something has been running, it cannot show
why a session errored, and it cannot tell a turn that is working from one that
has been silent for forty minutes — which are exactly the three questions you
ask after being away from the phone for an hour.

So this is not a second `/board`. `/board` lists workspaces to pick one from;
this ranks **tasks by how much they want you**, worst first:

    ⚠️ errored · ⏳ stalled · ⚙️ running · ✅ finished · 💤 asleep

Everything here is read from rows the bot already writes — ``sessions`` and
``workspaces``, no Conductor call — so it answers at the same speed whether or
not the API is up, and it is the one command that still works during an outage.

:func:`digest_entries` and :func:`digest_lines` are pure, take their clock, and
are where the whole behaviour lives; the handler is assembly.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, Message

from ctb import signals
from ctb.bot.app import register_router
from ctb.bot.handlers.common import abandon_wizard, command_text, safe_title, tell
from ctb.bot.handlers.topics import human_name, jump_url, resolve_db
from ctb.bot.keyboards import keyboard, url_button
from ctb.db.connection import Database, now_ms
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.sessions import SessionRow
from ctb.db.repo.workspaces import WorkspaceRow
from ctb.delivery.render.html import escape
from ctb.turn.machine import format_duration
from ctb.turn.state import NO_OUTPUT_WARN_S, TurnState

router = Router(name=__name__)
register_router(router, order=21)

__all__ = [
    "DEFAULT_WINDOW_MS",
    "DIGEST_VISIBLE",
    "DigestEntry",
    "digest_entries",
    "digest_lines",
    "parse_window",
    "window_label",
]

#: How far back "finished" reaches by default. A day, because the question this
#: answers is "what happened since I last looked" and the honest default for
#: that is overnight.
DEFAULT_WINDOW_MS: Final = 24 * 60 * 60 * 1000
#: Nobody types a window longer than a week on a phone, and the rows behind it
#: are pruned at thirty days anyway.
MAX_WINDOW_MS: Final = 7 * 24 * 60 * 60 * 1000
#: Rows shown before the card says how many it hid. Ten one-line entries is a
#: phone screen; the rest are one `/board` away.
DIGEST_VISIBLE: Final = 10
#: Jump buttons. Fewer than the lines: a wall of buttons is not a report, and
#: only the rows that want you are worth a thumb.
DIGEST_BUTTONS: Final = 5
#: A turn is "stalled" once it has been quiet for the same interval the machine
#: uses to warn about no output. One vocabulary, one threshold — a digest that
#: called something stalled while the room said nothing would be a third opinion.
STALLED_AFTER_MS: Final = int(NO_OUTPUT_WARN_S * 1000)
#: How much of an error message survives onto a phone line.
ERROR_CHARS: Final = 90

_WINDOW_RE: Final = re.compile(r"^\s*(\d{1,3})\s*([hdm])\s*$", re.IGNORECASE)
_WINDOW_UNIT_MS: Final[dict[str, int]] = {
    "m": 60 * 1000,
    "h": 60 * 60 * 1000,
    "d": 24 * 60 * 60 * 1000,
}

#: Busy: a turn is in flight, whatever it is doing.
_RUNNING: Final[frozenset[TurnState]] = frozenset(
    {
        TurnState.SUBMIT_PENDING,
        TurnState.QUEUED,
        TurnState.WAKING,
        TurnState.WORKING,
        TurnState.DRAINING,
        TurnState.CANCELLING,
    }
)

#: Rank → (icon, plural noun). The order **is** the ranking: worst first, so the
#: thing you have to do something about is never below the thing you do not.
_BUCKETS: Final[tuple[tuple[str, str], ...]] = (
    (signals.ERROR, "errored"),
    (signals.WAITING, "stalled"),
    (signals.WORKING, "running"),
    (signals.DONE, "finished"),
    (signals.SLEEPING, "asleep"),
)
ERRORED: Final = 0
STALLED: Final = 1
RUNNING: Final = 2
FINISHED: Final = 3
ASLEEP: Final = 4


@dataclass(frozen=True, slots=True)
class DigestEntry:
    """One task, and the single most useful sentence about it right now."""

    session_id: str
    title: str
    where: str
    rank: int
    #: Milliseconds; how the rows inside one bucket are ordered.
    age_ms: int
    detail: str = ""
    chat_id: int | None = None
    thread_id: int = 0

    @property
    def icon(self) -> str:
        return _BUCKETS[self.rank][0]

    @property
    def line(self) -> str:
        """``⚠️ <b>fix login</b> · acme-api · error · 4m``, HTML-escaped."""
        parts = [escape(self.where)] if self.where else []
        if self.detail:
            parts.append(escape(self.detail))
        parts.append(format_duration(self.age_ms))
        icon = f"{self.icon} " if self.icon else ""
        return f"{icon}<b>{escape(self.title)}</b> · {' · '.join(parts)}"

    @property
    def button_label(self) -> str:
        icon = f"{self.icon} " if self.icon else ""
        return f"{icon}{self.title}"


def parse_window(text: str) -> int | None:
    """``"6h"`` → milliseconds. ``None`` when the argument is not a window.

    Deliberately strict and tiny: a bare number is ambiguous on a phone (six
    what?) and anything else is a typo the caller should hear about rather than
    silently get a day of.
    """
    match = _WINDOW_RE.match(text)
    if match is None:
        return None
    amount = int(match.group(1))
    if amount <= 0:
        return None
    return min(MAX_WINDOW_MS, amount * _WINDOW_UNIT_MS[match.group(2).lower()])


def window_label(window_ms: int) -> str:
    """``"24h"``, ``"30m"``, ``"2d"`` — how a *window* is named, not a duration.

    Deliberately not :func:`format_duration`, which is the vocabulary for how
    long something *took* and renders a day as ``24h00m``. A header reading
    "Last 24h00m" is a duration pretending to be a window.
    """
    for unit, size in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000)):
        if window_ms >= size and window_ms % size == 0:
            return f"{window_ms // size}{unit}"
    return format_duration(window_ms)


def _where(workspace: WorkspaceRow | None) -> str:
    """The workspace a task belongs to, named the way a person named it."""
    if workspace is None:
        return ""
    name = human_name(workspace.name) or human_name(workspace.topic_name)
    if name and workspace.branch and workspace.branch not in name:
        return f"{name}/{workspace.branch}"
    return name or (workspace.branch or "")


def _classify(
    session: SessionRow, workspace: WorkspaceRow | None, *, now: int, window_ms: int
) -> DigestEntry | None:
    """Which bucket this session is in, or ``None`` when it is not worth a row.

    Not worth a row: dead, archived, never bound, or quietly idle since before
    the window — the last one is the whole point of the window, because a
    workspace you have not touched in a week is not news.
    """
    state = session.state
    if state is TurnState.DEAD or not session.is_bound:
        return None
    title = safe_title(session.title, session.id[:8])
    where = _where(workspace)
    seat = (session.chat_id, session.thread_id)
    updated = session.updated_at or session.created_at

    if state is TurnState.ERROR:
        detail = (session.error_text or "error").strip().splitlines()[0][:ERROR_CHARS]
        age = max(0, now - (session.last_error_at or updated))
        return DigestEntry(session.id, title, where, ERRORED, age, detail, *seat)

    if state in _RUNNING:
        started = session.turn_started_at or session.last_prompt_at or updated
        quiet_since = session.last_delta_at or started
        if now - quiet_since >= STALLED_AFTER_MS:
            return DigestEntry(
                session.id,
                title,
                where,
                STALLED,
                max(0, now - quiet_since),
                "no output",
                *seat,
            )
        return DigestEntry(
            session.id, title, where, RUNNING, max(0, now - started), str(state), *seat
        )

    if workspace is not None and workspace.status_value.is_waking:
        return DigestEntry(
            session.id, title, where, ASLEEP, max(0, now - updated), "sleeping", *seat
        )

    age = max(0, now - updated)
    if age > window_ms:
        return None
    return DigestEntry(session.id, title, where, FINISHED, age, "", *seat)


def digest_entries(
    sessions: Sequence[SessionRow],
    workspaces: Sequence[WorkspaceRow],
    *,
    now: int,
    window_ms: int = DEFAULT_WINDOW_MS,
) -> list[DigestEntry]:
    """Rank every live task by how much it wants you. Pure; takes its clock.

    Errored and stalled tasks are **never** filtered by the window. A session
    that broke two days ago and has been sitting there since is the single most
    important row this card can carry, and hiding it behind "nothing happened
    recently" is how it stays broken.
    """
    by_id = {row.id: row for row in workspaces}
    entries: list[DigestEntry] = []
    for session in sessions:
        entry = _classify(
            session,
            by_id.get(session.workspace_id or ""),
            now=now,
            window_ms=window_ms,
        )
        if entry is not None:
            entries.append(entry)
    # Within a bucket, oldest first: the thing that has been waiting longest is
    # the thing that has been ignored longest.
    entries.sort(key=lambda item: (item.rank, -item.age_ms))
    return entries


def digest_lines(
    entries: Sequence[DigestEntry], *, window_ms: int, visible: int = DIGEST_VISIBLE
) -> list[str]:
    """The card's text: a counted header, then the ranked rows."""
    if not entries:
        return [
            f"<b>Nothing running</b> · quiet for {window_label(window_ms)}",
            "<code>/new</code> starts a task · <code>/board</code> lists workspaces.",
        ]
    counts: dict[int, int] = {}
    for entry in entries:
        counts[entry.rank] = counts.get(entry.rank, 0) + 1
    summary = " · ".join(
        f"{counts[rank]} {_BUCKETS[rank][1]}" for rank in sorted(counts) if counts[rank]
    )
    lines = [f"<b>Last {window_label(window_ms)}</b> · {summary}"]
    lines.extend(entry.line for entry in entries[:visible])
    hidden = len(entries) - min(len(entries), visible)
    if hidden:
        lines.append(f"<i>+{hidden} more · /board</i>")
    return lines


def digest_buttons(
    entries: Sequence[DigestEntry], *, limit: int = DIGEST_BUTTONS
) -> list[list[InlineKeyboardButton]]:
    """Jump buttons for the rows that have a room to jump to.

    A private chat publishes no link syntax for a topic, so ``jump_url`` returns
    ``None`` there and those rows are text only — which is correct rather than
    degraded: in a DM the thread list is one swipe away and a dead button would
    be worse than no button.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries:
        if len(rows) >= limit:
            break
        if entry.chat_id is None:
            continue
        target = jump_url(entry.chat_id, entry.thread_id)
        if target is None:
            continue
        rows.append([url_button(entry.button_label, target)])
    return rows


@router.message(Command("digest"))
async def digest(
    message: Message,
    state: FSMContext,
    db: Database | None = None,
) -> None:
    await abandon_wizard(state)
    argument = command_text(message).strip()
    window_ms = DEFAULT_WINDOW_MS
    if argument:
        parsed = parse_window(argument)
        if parsed is None:
            await tell(message, "Usage: <code>/digest [30m|6h|2d]</code>")
            return
        window_ms = parsed
    database = resolve_db(db)
    entries = digest_entries(
        await sessions_repo.list_all(database),
        await workspaces_repo.list_all(database),
        now=now_ms(),
        window_ms=window_ms,
    )
    buttons = digest_buttons(entries)
    await tell(
        message,
        "\n".join(digest_lines(entries, window_ms=window_ms)),
        reply_markup=keyboard(buttons) if buttons else None,
    )
