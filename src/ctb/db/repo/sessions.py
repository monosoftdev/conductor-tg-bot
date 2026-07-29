"""``sessions`` — the transcript cursor and the turn machine's persisted context.

``cursor_session_index`` is the real cursor; ``cursor_message_id`` is the
optimisation that lets the poller pass ``after=``. ``sessionIndex`` is not
gapless (the probe observed 0, 2, 3, 4, 5, 6, 7), so a gap never implies a
missed message — the cursor is only ever moved by
:func:`ctb.db.repo.transcript.advance_cursor`, which moves it in the same
transaction that records the messages.

**Clocks.** :class:`ctb.turn.state.TurnContext` timestamps are monotonic seconds
(``time.monotonic``), which are meaningless across a process restart, while
every ``*_at`` column is epoch milliseconds. :func:`save_turn_context` converts
one to the other and :func:`load_turn_context` rebases back onto the current
monotonic clock, so a restored context has *ages* that are still true even
though the origin moved. Transition 22 forces a full delta + status refresh on
boot anyway, so a rebased age is a hint, never a conclusion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Self

from ctb.conductor.models import SessionStatusValue, WorkspaceStatusValue
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo._util import (
    UNSET,
    Maybe,
    as_bool,
    as_int,
    as_opt_int,
    as_opt_str,
    as_str,
    assign,
    bit,
    update_sql,
)
from ctb.turn.state import (
    AGENTS_MARKING_TURN_END,
    CADENCE_IDLE_DECAY_MS,
    PendingPrompt,
    TurnContext,
    TurnState,
)

__all__ = [
    "SessionRow",
    "bind",
    "clear_cursor_only",
    "delete",
    "get",
    "get_bound_for",
    "list_all",
    "list_bound",
    "list_for_workspace",
    "load_turn_context",
    "mark_dead",
    "record_status",
    "save_turn_context",
    "seek_to_end",
    "set_poll_interval",
    "set_status_card",
    "touch_prompt",
    "unbind",
    "update",
    "upsert",
]

_COLUMNS = """
    tenant_id, id, workspace_id, title, agent, model, effort, fast_mode,
    chat_id, thread_id,
    is_bound, cursor_message_id, cursor_session_index, seeded, turn_state,
    start_witnessed, index_at_post, last_delta_at, entered_state_at,
    turn_started_at, consecutive_idle, consecutive_status_failures, cursor_only,
    idle_decay_step, poll_interval_ms, status_card_msg_id, waking_notified,
    warned_no_output_at, tool_calls, last_status, error_message, last_error,
    last_error_at, last_prompt_at, dead_at, created_at, updated_at
"""

#: Sessions in these states are polled first after a restart. A literal, not a
#: bind parameter, because it is only ever this module-level constant.
_ACTIVE_STATES_SQL = "('QUEUED', 'WAKING', 'WORKING', 'DRAINING', 'CANCELLING')"


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: str
    #: Filled by row-level security's column default; read for fan-out.
    tenant_id: uuid.UUID | None = None
    workspace_id: str | None = None
    title: str | None = None
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    fast_mode: bool = False
    chat_id: int | None = None
    thread_id: int = NO_THREAD_ID
    is_bound: bool = True
    cursor_message_id: str | None = None
    cursor_session_index: int = -1
    seeded: bool = False
    turn_state: str = "IDLE"
    start_witnessed: bool = False
    index_at_post: int | None = None
    last_delta_at: int | None = None
    entered_state_at: int | None = None
    turn_started_at: int | None = None
    consecutive_idle: int = 0
    consecutive_status_failures: int = 0
    cursor_only: bool = False
    idle_decay_step: int = 0
    poll_interval_ms: int = CADENCE_IDLE_DECAY_MS[0]
    status_card_msg_id: int | None = None
    waking_notified: bool = False
    warned_no_output_at: int | None = None
    tool_calls: int = 0
    last_status: str | None = None
    error_message: str | None = None
    last_error: str | None = None
    last_error_at: int | None = None
    last_prompt_at: int | None = None
    dead_at: int | None = None
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            id=str(row["id"]),
            tenant_id=row["tenant_id"],
            workspace_id=as_opt_str(row["workspace_id"]),
            title=as_opt_str(row["title"]),
            agent=as_opt_str(row["agent"]),
            model=as_opt_str(row["model"]),
            effort=as_opt_str(row["effort"]),
            fast_mode=as_bool(row["fast_mode"]),
            chat_id=as_opt_int(row["chat_id"]),
            thread_id=as_int(row["thread_id"]),
            is_bound=as_bool(row["is_bound"]),
            cursor_message_id=as_opt_str(row["cursor_message_id"]),
            cursor_session_index=as_int(row["cursor_session_index"], -1),
            seeded=as_bool(row["seeded"]),
            turn_state=as_str(row["turn_state"], "IDLE"),
            start_witnessed=as_bool(row["start_witnessed"]),
            index_at_post=as_opt_int(row["index_at_post"]),
            last_delta_at=as_opt_int(row["last_delta_at"]),
            entered_state_at=as_opt_int(row["entered_state_at"]),
            turn_started_at=as_opt_int(row["turn_started_at"]),
            consecutive_idle=as_int(row["consecutive_idle"]),
            consecutive_status_failures=as_int(row["consecutive_status_failures"]),
            cursor_only=as_bool(row["cursor_only"]),
            idle_decay_step=as_int(row["idle_decay_step"]),
            poll_interval_ms=as_int(row["poll_interval_ms"], CADENCE_IDLE_DECAY_MS[0]),
            status_card_msg_id=as_opt_int(row["status_card_msg_id"]),
            waking_notified=as_bool(row["waking_notified"]),
            warned_no_output_at=as_opt_int(row["warned_no_output_at"]),
            tool_calls=as_int(row["tool_calls"]),
            last_status=as_opt_str(row["last_status"]),
            error_message=as_opt_str(row["error_message"]),
            last_error=as_opt_str(row["last_error"]),
            last_error_at=as_opt_int(row["last_error_at"]),
            last_prompt_at=as_opt_int(row["last_prompt_at"]),
            dead_at=as_opt_int(row["dead_at"]),
            created_at=as_int(row["created_at"]),
            updated_at=as_int(row["updated_at"]),
        )

    @property
    def state(self) -> TurnState:
        try:
            return TurnState(self.turn_state)
        except ValueError:  # pragma: no cover - a CHECK constraint prevents this
            return TurnState.IDLE

    @property
    def status_value(self) -> SessionStatusValue:
        if self.last_status is None:
            return SessionStatusValue.UNKNOWN
        try:
            return SessionStatusValue(self.last_status)
        except ValueError:
            return SessionStatusValue.UNKNOWN

    @property
    def error_text(self) -> str | None:
        """``errorMessage ?? lastError`` — what the ERROR card shows."""
        return self.error_message or self.last_error


# ── clock conversion ─────────────────────────────────────────────────────────


def _to_wall_ms(value: float | None, *, now: float, wall_ms: int) -> int | None:
    """Monotonic seconds → epoch ms, preserving the age of the observation."""
    if value is None:
        return None
    return wall_ms - int((now - value) * 1000)


def _to_monotonic(value: int | None, *, now: float, wall_ms: int) -> float | None:
    """Epoch ms → monotonic seconds on *this* process's clock."""
    if value is None:
        return None
    return now - max(0.0, (wall_ms - value) / 1000.0)


# ── reads ────────────────────────────────────────────────────────────────────


async def get(db: Database, session_id: str) -> SessionRow | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM sessions WHERE id = ?", (session_id,)
    )
    return None if row is None else SessionRow.from_row(row)


async def get_bound_for(
    db: Database, chat_id: int, thread_id: int = NO_THREAD_ID
) -> SessionRow | None:
    """The session a prompt typed in this topic goes to."""
    row = await db.fetch_one(
        f"""
        SELECT {_COLUMNS} FROM sessions
         WHERE chat_id = ? AND thread_id = ? AND is_bound
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (chat_id, thread_id),
    )
    return None if row is None else SessionRow.from_row(row)


async def list_bound(db: Database) -> list[SessionRow]:
    """What the supervisor reconciles its task set against, every 5s.

    Cross-tenant by design, so it runs on the **system** pool. The join is the
    whole suspension mechanism: setting ``tenants.status`` to anything but
    ``active``, or stamping ``auth_failed_at``, stops that tenant's polling on
    the next pass with no code path involved and nobody else affected.

    Ordered by tenant so the supervisor can round-robin, and by turn state
    within each tenant so a starved tenant still gets its *busy* sessions
    polled.
    """
    qualified = ", ".join(f"s.{name.strip()}" for name in _COLUMNS.split(","))
    rows = await db.fetch_all(
        f"""
        SELECT {qualified} FROM sessions s
          JOIN tenants t ON t.id = s.tenant_id
         WHERE s.is_bound
           AND s.turn_state != 'DEAD'
           AND t.status = 'active'
           AND t.auth_failed_at IS NULL
         ORDER BY
            s.tenant_id,
            CASE WHEN s.turn_state IN {_ACTIVE_STATES_SQL} THEN 0 ELSE 1 END,
            s.updated_at DESC
        """
    )
    return [SessionRow.from_row(row) for row in rows]


async def list_all(db: Database) -> list[SessionRow]:
    rows = await db.fetch_all(
        f"SELECT {_COLUMNS} FROM sessions ORDER BY created_at DESC"
    )
    return [SessionRow.from_row(row) for row in rows]


async def list_for_workspace(db: Database, workspace_id: str) -> list[SessionRow]:
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM sessions
         WHERE workspace_id = ?
         ORDER BY created_at DESC
        """,
        (workspace_id,),
    )
    return [SessionRow.from_row(row) for row in rows]


# ── writes ───────────────────────────────────────────────────────────────────


async def upsert(
    db: Database,
    session_id: str,
    *,
    workspace_id: Maybe[str | None] = UNSET,
    title: Maybe[str | None] = UNSET,
    agent: Maybe[str | None] = UNSET,
    model: Maybe[str | None] = UNSET,
    effort: Maybe[str | None] = UNSET,
    fast_mode: Maybe[bool] = UNSET,
    chat_id: Maybe[int | None] = UNSET,
    thread_id: Maybe[int] = UNSET,
    is_bound: Maybe[bool] = UNSET,
    at: int | None = None,
) -> SessionRow:
    """Insert or refresh a session row. Cursor and turn fields are untouched."""
    stamp = now_ms() if at is None else at
    columns: dict[str, Any] = {}
    assign(
        columns,
        workspace_id=workspace_id,
        title=title,
        agent=agent,
        model=model,
        effort=effort,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    if isinstance(fast_mode, bool):
        columns["fast_mode"] = bit(fast_mode)
    if isinstance(is_bound, bool):
        columns["is_bound"] = bit(is_bound)
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO sessions (id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (session_id, stamp, stamp),
        )
        if columns:
            columns["updated_at"] = stamp
            await db.execute(
                update_sql("sessions", columns, "id = ?"),
                (*columns.values(), session_id),
            )
        row = await get(db, session_id)
    if row is None:  # pragma: no cover - the insert above guarantees a row
        raise RuntimeError(f"sessions row vanished for {session_id}")
    return row


async def update(
    db: Database, session_id: str, *, at: int | None = None, **columns: Any
) -> SessionRow | None:
    """Escape hatch for a raw column update. Prefer the named helpers."""
    if not columns:
        return await get(db, session_id)
    columns["updated_at"] = now_ms() if at is None else at
    async with db.transaction():
        await db.execute(
            update_sql("sessions", columns, "id = ?"), (*columns.values(), session_id)
        )
        return await get(db, session_id)


async def bind(
    db: Database,
    session_id: str,
    *,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    at: int | None = None,
) -> SessionRow | None:
    """Route this session's transcript into a chat/topic and start polling it."""
    return await update(
        db,
        session_id,
        at=at,
        chat_id=chat_id,
        thread_id=thread_id,
        is_bound=True,
    )


async def unbind(
    db: Database, session_id: str, *, at: int | None = None
) -> SessionRow | None:
    """Stop polling. The cursor is kept so a re-bind resumes where it left off."""
    return await update(db, session_id, at=at, is_bound=False)


async def mark_dead(
    db: Database, session_id: str, *, reason: str | None = None, at: int | None = None
) -> SessionRow | None:
    """Transition 20: the session 404s. Terminal — unbind and stop the task."""
    stamp = now_ms() if at is None else at
    columns: dict[str, Any] = {
        "turn_state": str(TurnState.DEAD),
        "is_bound": False,
        "dead_at": stamp,
        "entered_state_at": stamp,
    }
    if reason is not None:
        columns["last_error"] = reason
        columns["last_error_at"] = stamp
    return await update(db, session_id, at=stamp, **columns)


async def record_status(
    db: Database,
    session_id: str,
    *,
    status: str | SessionStatusValue,
    error_message: str | None = None,
    last_error: str | None = None,
    at: int | None = None,
) -> SessionRow | None:
    """Cache a ``GET /v0/sessions/{id}/status`` observation.

    ``error`` is a first-class, indefinitely persistent status: the error text is
    kept so the ERROR card can show it, and cleared as soon as a non-error status
    is observed.
    """
    stamp = now_ms() if at is None else at
    value = str(status)
    is_error = value == SessionStatusValue.ERROR
    return await update(
        db,
        session_id,
        at=stamp,
        last_status=value,
        error_message=error_message if is_error else None,
        last_error=last_error if is_error else None,
        last_error_at=stamp if is_error else None,
    )


async def set_poll_interval(
    db: Database, session_id: str, interval_ms: int, *, at: int | None = None
) -> SessionRow | None:
    return await update(db, session_id, at=at, poll_interval_ms=max(0, interval_ms))


async def set_status_card(
    db: Database, session_id: str, tg_message_id: int | None, *, at: int | None = None
) -> SessionRow | None:
    return await update(db, session_id, at=at, status_card_msg_id=tg_message_id)


async def clear_cursor_only(
    db: Database, session_id: str, *, at: int | None = None
) -> SessionRow | None:
    """``/status`` works again: leave the degraded fixed-cadence mode."""
    return await update(
        db, session_id, at=at, cursor_only=False, consecutive_status_failures=0
    )


async def touch_prompt(
    db: Database, session_id: str, *, at: int | None = None
) -> SessionRow | None:
    stamp = now_ms() if at is None else at
    return await update(db, session_id, at=stamp, last_prompt_at=stamp)


async def seek_to_end(
    db: Database,
    session_id: str,
    *,
    message_id: str | None,
    session_index: int,
    at: int | None = None,
) -> SessionRow | None:
    """First bind: jump the cursor to the end instead of replaying history.

    Deliberately unconditional — unlike :func:`transcript.advance_cursor` this
    *is* allowed to move the cursor without recording anything, because there is
    nothing to deliver. It is the only such path.
    """
    return await update(
        db,
        session_id,
        at=at,
        cursor_message_id=message_id,
        cursor_session_index=session_index,
        seeded=True,
    )


async def delete(db: Database, session_id: str) -> bool:
    changed = await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return changed > 0


# ── turn context ─────────────────────────────────────────────────────────────


async def load_turn_context(
    db: Database,
    session_id: str,
    *,
    now: float,
    wall_ms: int | None = None,
) -> TurnContext | None:
    """Rebuild the machine's context after a restart.

    ``pending_prompts`` comes from ``outbound_prompts`` (the durable ledger),
    the workspace status from the cached workspace row. The purely within-turn
    fields — ``delivered``, ``turn_ids``, ``cancel_requested_at`` — are not
    persisted: transition 22 re-derives them from a forced drain on boot.
    """
    wall = now_ms() if wall_ms is None else wall_ms
    row = await get(db, session_id)
    if row is None:
        return None
    ws_status: WorkspaceStatusValue | None = None
    lifecycle_step: str | None = None
    if row.workspace_id is not None:
        ws_row = await db.fetch_one(
            "SELECT status, lifecycle_step FROM workspaces WHERE id = ?",
            (row.workspace_id,),
        )
        if ws_row is not None:
            raw = as_opt_str(ws_row["status"])
            lifecycle_step = as_opt_str(ws_row["lifecycle_step"])
            if raw is not None:
                try:
                    ws_status = WorkspaceStatusValue(raw)
                except ValueError:
                    ws_status = WorkspaceStatusValue.UNKNOWN
    prompt_rows = await db.fetch_all(
        """
        SELECT message_id, index_at_post, created_at, posted_at
          FROM outbound_prompts
         WHERE session_id = ? AND state IN ('pending', 'posted')
         ORDER BY created_at
        """,
        (session_id,),
    )
    pending = tuple(
        PendingPrompt(
            message_id=str(p["message_id"]),
            posted_at=_to_monotonic(
                as_opt_int(p["posted_at"]) or as_int(p["created_at"]),
                now=now,
                wall_ms=wall,
            )
            or now,
            index_at_post=as_opt_int(p["index_at_post"]),
        )
        for p in prompt_rows
    )
    entered = _to_monotonic(row.entered_state_at, now=now, wall_ms=wall)
    return TurnContext(
        state=row.state,
        start_witnessed=row.start_witnessed,
        pending_prompts=pending,
        index_at_post=row.index_at_post,
        last_delta_at=_to_monotonic(row.last_delta_at, now=now, wall_ms=wall),
        entered_state_at=now if entered is None else entered,
        turn_started_at=_to_monotonic(row.turn_started_at, now=now, wall_ms=wall),
        consecutive_idle=row.consecutive_idle,
        consecutive_status_failures=row.consecutive_status_failures,
        cursor_only=row.cursor_only,
        cadence_ms=row.poll_interval_ms,
        idle_decay_step=row.idle_decay_step,
        last_status=row.status_value if row.last_status else None,
        workspace_status=ws_status,
        lifecycle_step=lifecycle_step,
        waking_notified=row.waking_notified,
        error_message=row.error_text,
        warned_no_output_at=_to_monotonic(
            row.warned_no_output_at, now=now, wall_ms=wall
        ),
        status_card_msg_id=row.status_card_msg_id,
        tool_calls=row.tool_calls,
        marks_turn_end=(row.agent or "") in AGENTS_MARKING_TURN_END,
    )


async def save_turn_context(
    db: Database,
    session_id: str,
    context: TurnContext,
    *,
    now: float,
    wall_ms: int | None = None,
) -> None:
    """Persist the durable half of the machine's context.

    The cursor columns are **not** written here — only
    :func:`transcript.advance_cursor` and :func:`seek_to_end` move the cursor.
    """
    wall = now_ms() if wall_ms is None else wall_ms
    columns: dict[str, Any] = {
        "turn_state": str(context.state),
        "start_witnessed": bit(context.start_witnessed),
        "index_at_post": context.index_at_post,
        "last_delta_at": _to_wall_ms(context.last_delta_at, now=now, wall_ms=wall),
        "entered_state_at": _to_wall_ms(
            context.entered_state_at, now=now, wall_ms=wall
        ),
        "turn_started_at": _to_wall_ms(context.turn_started_at, now=now, wall_ms=wall),
        "consecutive_idle": context.consecutive_idle,
        "consecutive_status_failures": context.consecutive_status_failures,
        "cursor_only": bit(context.cursor_only),
        "idle_decay_step": context.idle_decay_step,
        "poll_interval_ms": context.cadence_ms,
        "status_card_msg_id": context.status_card_msg_id,
        "waking_notified": bit(context.waking_notified),
        "warned_no_output_at": _to_wall_ms(
            context.warned_no_output_at, now=now, wall_ms=wall
        ),
        "tool_calls": context.tool_calls,
        "error_message": context.error_message,
        "updated_at": wall,
    }
    if context.last_status is not None:
        columns["last_status"] = str(context.last_status)
    if context.state is TurnState.DEAD:
        columns["is_bound"] = False
        columns["dead_at"] = wall
    await db.execute(
        update_sql("sessions", columns, "id = ?"), (*columns.values(), session_id)
    )
