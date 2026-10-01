"""``/digest`` — the ranking is the feature, so the ranking is what is tested.

The card exists to answer "what needs me?" before "what happened?", so every
test here is about *order* and about what survives the window. A digest that
buries a two-day-old error under this morning's finished work is worse than no
digest: it looks like an answer.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from aiogram.types import InlineKeyboardButton

from ctb.bot.handlers.digest import (
    ASLEEP,
    DEFAULT_WINDOW_MS,
    ERRORED,
    FINISHED,
    RUNNING,
    STALLED,
    STALLED_AFTER_MS,
    digest_buttons,
    digest_entries,
    digest_lines,
    parse_window,
    window_label,
)
from ctb.bot.keyboards import (
    CONTROL_TTL_S,
    READ_TTL_S,
    Action,
    NonceError,
    NonceStore,
    parse,
    read_stateless,
)
from ctb.db.repo.sessions import SessionRow
from ctb.db.repo.workspaces import WorkspaceRow

NOW = 1_800_000_000_000
MINUTE = 60_000
HOUR = 60 * MINUTE

CHAT = -1001234567890
DM = 5551234


def session(session_id: str, **overrides: Any) -> SessionRow:
    base: dict[str, Any] = {
        "id": session_id,
        "workspace_id": "ws-1",
        "title": session_id,
        "chat_id": CHAT,
        "thread_id": 7,
        "is_bound": True,
        "turn_state": "IDLE",
        "updated_at": NOW - MINUTE,
        "created_at": NOW - HOUR,
    }
    base |= overrides
    return SessionRow(**base)


def _buttons(entries: Any) -> list[list[InlineKeyboardButton]]:
    """``digest_buttons`` with the ticket plumbing a handler supplies."""
    return digest_buttons(
        entries, store=NonceStore(), user_id=1001, chat_id=CHAT, thread_id=0
    )


def workspace(workspace_id: str = "ws-1", **overrides: Any) -> WorkspaceRow:
    base: dict[str, Any] = {
        "id": workspace_id,
        "name": "acme-api",
        "branch": "main",
        "status": "ready",
    }
    base |= overrides
    return WorkspaceRow(**base)


def ranks(entries: Any) -> list[int]:
    return [entry.rank for entry in entries]


# ── ranking ──────────────────────────────────────────────────────────────────


def test_the_worst_thing_is_first() -> None:
    """Worst first, always. This is the whole contract of the card."""
    rows = [
        session("finished"),
        session(
            "asleep-one",
            workspace_id="ws-sleep",
        ),
        session(
            "running",
            turn_state="WORKING",
            turn_started_at=NOW - 2 * MINUTE,
            last_delta_at=NOW - MINUTE,
        ),
        session(
            "stalled",
            turn_state="WORKING",
            turn_started_at=NOW - 2 * HOUR,
            last_delta_at=NOW - STALLED_AFTER_MS - MINUTE,
        ),
        session("broken", turn_state="ERROR", error_message="boom", last_error_at=NOW),
    ]
    entries = digest_entries(
        rows,
        [workspace(), workspace("ws-sleep", status="sleeping")],
        now=NOW,
    )

    assert [entry.session_id for entry in entries] == [
        "broken",
        "stalled",
        "running",
        "finished",
        "asleep-one",
    ]
    assert ranks(entries) == [ERRORED, STALLED, RUNNING, FINISHED, ASLEEP]


def test_the_longest_ignored_row_leads_its_bucket() -> None:
    """Inside a bucket the oldest is first: it has been waiting longest."""
    rows = [
        session("recent", turn_state="ERROR", last_error_at=NOW - MINUTE),
        session("ancient", turn_state="ERROR", last_error_at=NOW - 6 * HOUR),
    ]

    entries = digest_entries(rows, [workspace()], now=NOW)

    assert [entry.session_id for entry in entries] == ["ancient", "recent"]


def test_a_quiet_turn_is_stalled_and_says_so() -> None:
    """Working and silent for twenty minutes is the state nothing else shows.

    The topic list cannot distinguish it from a healthy turn — both wear ⚙️ —
    and it is the single most common thing somebody opens the phone to check.
    """
    rows = [
        session(
            "quiet",
            turn_state="WORKING",
            turn_started_at=NOW - 3 * HOUR,
            last_delta_at=NOW - STALLED_AFTER_MS - 1,
        )
    ]

    entries = digest_entries(rows, [workspace()], now=NOW)

    assert entries[0].rank == STALLED
    assert entries[0].detail == "no output"
    # Aged from the last output, not from the start: "silent for 21m", not "3h".
    assert entries[0].age_ms == STALLED_AFTER_MS + 1


def test_a_working_turn_just_under_the_threshold_is_not_stalled() -> None:
    rows = [
        session(
            "busy",
            turn_state="WORKING",
            turn_started_at=NOW - HOUR,
            last_delta_at=NOW - STALLED_AFTER_MS + 1,
        )
    ]

    assert digest_entries(rows, [workspace()], now=NOW)[0].rank == RUNNING


# ── what the window may and may not hide ─────────────────────────────────────


def test_the_window_hides_old_finished_work() -> None:
    rows = [session("old", updated_at=NOW - DEFAULT_WINDOW_MS - MINUTE)]

    assert digest_entries(rows, [workspace()], now=NOW) == []


def test_the_window_never_hides_something_broken() -> None:
    """A session that broke two days ago is exactly what this card is for.

    Filtering it out under "nothing happened recently" is how it stays broken.
    """
    rows = [
        session(
            "broken",
            turn_state="ERROR",
            error_message="boom",
            last_error_at=NOW - 3 * DEFAULT_WINDOW_MS,
            updated_at=NOW - 3 * DEFAULT_WINDOW_MS,
        ),
        session(
            "stuck",
            turn_state="WORKING",
            turn_started_at=NOW - 3 * DEFAULT_WINDOW_MS,
            last_delta_at=NOW - 3 * DEFAULT_WINDOW_MS,
            updated_at=NOW - 3 * DEFAULT_WINDOW_MS,
        ),
    ]

    assert ranks(digest_entries(rows, [workspace()], now=NOW)) == [ERRORED, STALLED]


def test_dead_and_unbound_tasks_are_not_news() -> None:
    rows = [
        session("dead", turn_state="DEAD"),
        session("unbound", is_bound=False),
    ]

    assert digest_entries(rows, [workspace()], now=NOW) == []


# ── the card itself ──────────────────────────────────────────────────────────


def test_the_header_counts_every_bucket_it_shows() -> None:
    rows = [
        session("broken", turn_state="ERROR", error_message="boom", last_error_at=NOW),
        session("a"),
        session("b"),
    ]
    entries = digest_entries(rows, [workspace()], now=NOW)

    header = digest_lines(entries, window_ms=DEFAULT_WINDOW_MS)[0]

    assert header == "<b>Last 1d</b> · 1 errored · 2 finished"


def test_an_error_reaches_the_line_that_reports_it() -> None:
    """The error text is the reason to open the room. It has to be on the card."""
    rows = [
        session(
            "broken",
            turn_state="ERROR",
            error_message="model overloaded, retry later",
            last_error_at=NOW - MINUTE,
        )
    ]

    line = digest_entries(rows, [workspace()], now=NOW)[0].line

    assert line == (
        "⚠️ <b>broken</b> · acme-api/main · model overloaded, retry later · 1m00s"
    )


def test_a_hostile_title_cannot_inject_markup() -> None:
    rows = [session("x", title="<b>pwn</b>", turn_state="ERROR", last_error_at=NOW)]

    line = digest_entries(rows, [workspace()], now=NOW)[0].line

    assert "<b>pwn</b>" not in line.removeprefix("⚠️ <b>").removesuffix("</b>")
    assert "&lt;b&gt;pwn&lt;/b&gt;" in line


def test_an_empty_digest_says_what_to_do_instead() -> None:
    lines = digest_lines([], window_ms=DEFAULT_WINDOW_MS)

    assert lines[0].startswith("<b>Nothing running</b>")
    assert "/new" in lines[1]


def test_a_long_digest_says_how_many_it_hid() -> None:
    rows = [session(f"s{index}") for index in range(14)]
    entries = digest_entries(rows, [workspace()], now=NOW)

    lines = digest_lines(entries, window_ms=DEFAULT_WINDOW_MS, visible=10)

    assert len(lines) == 12  # header + 10 + the tail
    assert lines[-1] == "<i>+4 more · /board</i>"


# ── buttons ──────────────────────────────────────────────────────────────────


def test_buttons_jump_to_the_rooms_that_have_one() -> None:
    rows = [
        session("broken", turn_state="ERROR", last_error_at=NOW),
        session("fine", thread_id=9),
    ]
    entries = digest_entries(rows, [workspace()], now=NOW)

    buttons = _buttons(entries)

    assert [row[0].url for row in buttons] == [
        "https://t.me/c/1234567890/7",
        "https://t.me/c/1234567890/9",
    ]
    # Worst first here too — the button order is the line order.
    assert buttons[0][0].text.startswith("↗ \u26a0\ufe0f")


def test_a_dm_gets_a_verb_because_it_can_never_get_a_destination() -> None:
    """``jump_url`` answers ``None`` for *every* private chat.

    This used to assert no buttons at all, reasoning that "a button that cannot
    work is worse than the thread list one swipe away". The premise holds — a DM
    publishes no link syntax for a topic — and the conclusion did not, because a
    DM is the **default** flow: ``/start``, ``/key``, ``/new`` all happen there,
    so the card that ranks what needs you offered no way to act on any of it.

    Nor is it one swipe. On the live database one owner's DM holds 43 threads of
    which exactly 1 routes to a usable session. Ranking the task that wants
    attention and then returning the reader to that list is most of the way to
    not having answered, so the fallback is a verb — read it here — rather than
    nothing.
    """
    rows = [session("only", chat_id=DM, thread_id=4)]
    entries = digest_entries(rows, [workspace()], now=NOW)

    buttons = _buttons(entries)

    assert len(buttons) == 1
    item = buttons[0][0]
    assert item.url is None, "a DM link would be a dead button"
    assert item.callback_data is not None
    assert item.text.startswith("📄")


def test_the_two_verbs_never_wear_the_same_face() -> None:
    """One moves you, one brings it to you. ``board_stage1`` learned this."""
    group = digest_entries(
        [session("a", chat_id=CHAT, thread_id=7)], [workspace()], now=NOW
    )
    dm = digest_entries([session("b", chat_id=DM, thread_id=7)], [workspace()], now=NOW)

    assert _buttons(group)[0][0].text.startswith("↗")
    assert _buttons(dm)[0][0].text.startswith("📄")


# ── the window argument ──────────────────────────────────────────────────────


def test_a_window_is_parsed_or_refused_and_never_guessed() -> None:
    assert parse_window("30m") == 30 * MINUTE
    assert parse_window("6h") == 6 * HOUR
    assert parse_window(" 2D ") == 2 * 24 * HOUR
    # A bare number is ambiguous on a phone: six what?
    assert parse_window("6") is None
    assert parse_window("last week") is None
    assert parse_window("0h") is None


def test_a_window_is_capped_rather_than_refused() -> None:
    """Asking for a year is a reasonable thing to type and a fine thing to cap."""
    assert parse_window("365d") == 7 * 24 * HOUR


def test_a_window_is_named_as_a_window_and_not_as_a_duration() -> None:
    """``format_duration`` renders a day as ``24h00m`` — a duration, not a window."""
    assert window_label(DEFAULT_WINDOW_MS) == "1d"
    assert window_label(6 * HOUR) == "6h"
    assert window_label(30 * MINUTE) == "30m"
    # Not a whole unit of anything: fall back rather than lie about it.
    assert window_label(90 * 1000) == "1m30s"


def test_a_read_button_outlives_a_control_and_a_redeploy() -> None:
    """Two properties the ranked card is useless without.

    *Stateless*, because ``NonceStore`` is in-memory: a digest whose only verb
    answered "expired" after every deploy would be a card that works until the
    next release. ``Action.TRANSCRIPT`` is already in ``RESTARTABLE_ACTIONS`` —
    "Stop, Retry, Transcript and Check are all safe to repeat" — so the payload
    is self-describing and signed.

    *Long-lived*, because 15 minutes is sized for ``Stop``, whose target may not
    be the same turn when a stale tap lands. A transcript has no such hazard, and
    a ranked card is exactly what somebody scrolls back to after a coffee.
    """
    entries = digest_entries(
        [session("only", chat_id=DM, thread_id=4)], [workspace()], now=NOW
    )

    item = _buttons(entries)[0][0]
    data = item.callback_data
    assert data is not None

    # Readable by a process that never minted it — no store consulted.
    ticket = read_stateless(parse(data).nonce, Action.TRANSCRIPT.value)
    assert ticket.target == "only"

    # And still readable well past the window a control gets.
    later = read_stateless(
        parse(data).nonce,
        Action.TRANSCRIPT.value,
        now=time.time() + CONTROL_TTL_S + 60,
    )
    assert later.target == "only"
    with pytest.raises(NonceError):
        read_stateless(
            parse(data).nonce,
            Action.TRANSCRIPT.value,
            now=time.time() + READ_TTL_S + 3600,
        )
