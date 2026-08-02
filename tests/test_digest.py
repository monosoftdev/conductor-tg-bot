"""``/digest`` — the ranking is the feature, so the ranking is what is tested.

The card exists to answer "what needs me?" before "what happened?", so every
test here is about *order* and about what survives the window. A digest that
buries a two-day-old error under this morning's finished work is worse than no
digest: it looks like an answer.
"""

from __future__ import annotations

from typing import Any

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

    buttons = digest_buttons(entries)

    assert [row[0].url for row in buttons] == [
        "https://t.me/c/1234567890/7",
        "https://t.me/c/1234567890/9",
    ]
    # Worst first here too — the button order is the line order.
    assert buttons[0][0].text.startswith("⚠️")


def test_a_dm_gets_no_dead_buttons() -> None:
    """Telegram publishes no link syntax for a topic in a private chat.

    A button that cannot work is worse than the thread list one swipe away.
    """
    rows = [session("only", chat_id=DM, thread_id=4)]
    entries = digest_entries(rows, [workspace()], now=NOW)

    assert digest_buttons(entries) == []


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
