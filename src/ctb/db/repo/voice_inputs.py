"""Durable, replay-safe Telegram voice-note jobs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Self

from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo._util import as_int, as_opt_int, as_opt_str, as_str

__all__ = [
    "MAX_ATTEMPTS",
    "ORPHAN_AFTER_MS",
    "Recovery",
    "VoiceInputRow",
    "claim_next",
    "complete",
    "count_pending",
    "create",
    "fail",
    "get",
    "health_stats",
    "mark_dispatching",
    "mark_transcribed",
    "prune_terminal",
    "recover_stale",
    "requeue",
    "set_ack_message",
    "wait_for_user",
]

#: A voice note that kills the process mid-transcription is replayed on the
#: next boot. Without a cap that is an unbounded, *billed* crash loop, so a job
#: that has burned this many claims is abandoned instead of requeued.
MAX_ATTEMPTS: Final = 3

#: A claimed voice job younger than this may still be in flight on a live peer
#: during a deploy overlap. Same guard, same reasoning as
#: :data:`ctb.db.repo.deliveries.ORPHAN_AFTER_MS` — but here a wrong recovery
#: costs the customer a second speech-vendor bill, not just a duplicate message.
ORPHAN_AFTER_MS: Final = 150_000

_TERMINAL_STATES: Final[tuple[str, ...]] = ("completed", "failed", "waiting_for_user")

_COLUMNS = """
    tenant_id, chat_id, tg_message_id, thread_id, user_id, file_id, file_unique_id,
    file_name, mime_type, duration_seconds, file_size, route_kind, route_session_id,
    route_workspace_id, provider, model, state, transcript, language,
    intent_json, action_id, ack_message_id, attempts, last_error, created_at,
    updated_at, completed_at
"""

#: The same list aliased for a ``RETURNING`` clause inside :func:`claim_next`.
_QUALIFIED_COLUMNS = ", ".join(f"v.{name.strip()}" for name in _COLUMNS.split(","))


@dataclass(frozen=True, slots=True)
class VoiceInputRow:
    chat_id: int
    tg_message_id: int
    thread_id: int
    user_id: int
    file_id: str
    file_unique_id: str | None
    file_name: str | None
    mime_type: str | None
    duration_seconds: int | None
    file_size: int | None
    route_kind: str
    route_session_id: str | None
    route_workspace_id: str | None
    provider: str
    model: str
    state: str
    transcript: str | None
    language: str | None
    intent_json: str | None
    action_id: str
    ack_message_id: int | None
    attempts: int
    last_error: str | None
    created_at: int
    updated_at: int
    completed_at: int | None
    #: Filled by row-level security's column default; read for fan-out.
    tenant_id: uuid.UUID | None = None

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            chat_id=as_int(row["chat_id"]),
            tenant_id=row["tenant_id"],
            tg_message_id=as_int(row["tg_message_id"]),
            thread_id=as_int(row["thread_id"]),
            user_id=as_int(row["user_id"]),
            file_id=as_str(row["file_id"]),
            file_unique_id=as_opt_str(row["file_unique_id"]),
            file_name=as_opt_str(row["file_name"]),
            mime_type=as_opt_str(row["mime_type"]),
            duration_seconds=as_opt_int(row["duration_seconds"]),
            file_size=as_opt_int(row["file_size"]),
            route_kind=as_str(row["route_kind"], "unknown"),
            route_session_id=as_opt_str(row["route_session_id"]),
            route_workspace_id=as_opt_str(row["route_workspace_id"]),
            provider=as_str(row["provider"]),
            model=as_str(row["model"]),
            state=as_str(row["state"]),
            transcript=as_opt_str(row["transcript"]),
            language=as_opt_str(row["language"]),
            intent_json=as_opt_str(row["intent_json"]),
            action_id=as_str(row["action_id"]),
            ack_message_id=as_opt_int(row["ack_message_id"]),
            attempts=as_int(row["attempts"]),
            last_error=as_opt_str(row["last_error"]),
            created_at=as_int(row["created_at"]),
            updated_at=as_int(row["updated_at"]),
            completed_at=as_opt_int(row["completed_at"]),
        )


@dataclass(frozen=True, slots=True)
class Recovery:
    """What :func:`recover_stale` did to the jobs a dead process left behind."""

    requeued: int
    abandoned: tuple[VoiceInputRow, ...]


async def get(db: Database, chat_id: int, tg_message_id: int) -> VoiceInputRow | None:
    row = await db.fetch_one(
        f"""
        SELECT {_COLUMNS}
          FROM voice_inputs
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (chat_id, tg_message_id),
    )
    return None if row is None else VoiceInputRow.from_row(row)


async def set_ack_message(
    db: Database,
    chat_id: int,
    tg_message_id: int,
    ack_message_id: int,
    *,
    at: int | None = None,
) -> VoiceInputRow | None:
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE voice_inputs
           SET ack_message_id = ?, updated_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (ack_message_id, stamp, chat_id, tg_message_id),
    )
    return await get(db, chat_id, tg_message_id)


async def create(
    db: Database,
    *,
    chat_id: int,
    tg_message_id: int,
    thread_id: int,
    user_id: int,
    file_id: str,
    file_unique_id: str | None,
    file_name: str | None,
    mime_type: str | None,
    duration_seconds: int | None,
    file_size: int | None,
    route_kind: str,
    route_session_id: str | None,
    route_workspace_id: str | None,
    provider: str,
    model: str,
    action_id: str,
    at: int | None = None,
) -> tuple[VoiceInputRow, bool]:
    """Insert once; a replay returns the original route and operation id."""
    stamp = now_ms() if at is None else at
    changed = await db.execute(
        """
        INSERT INTO voice_inputs
            (chat_id, tg_message_id, thread_id, user_id, file_id,
             file_unique_id, file_name, mime_type, duration_seconds, file_size,
             route_kind,
             route_session_id, route_workspace_id, provider, model, state,
             action_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            chat_id,
            tg_message_id,
            thread_id,
            user_id,
            file_id,
            file_unique_id,
            file_name,
            mime_type,
            duration_seconds,
            file_size,
            route_kind,
            route_session_id,
            route_workspace_id,
            provider,
            model,
            action_id,
            stamp,
            stamp,
        ),
    )
    row = await get(db, chat_id, tg_message_id)
    if row is None:  # pragma: no cover - insert or existing row guarantees this
        raise RuntimeError("voice_inputs row vanished")
    return row, changed == 1


async def claim_next(db: Database, *, at: int | None = None) -> VoiceInputRow | None:
    """Claim the oldest transcription or recovered dispatch job.

    One statement. ``FOR UPDATE SKIP LOCKED`` means concurrent workers pick
    *different* rows instead of all converging on the head of the queue and all
    but one losing a compare-and-swap.
    """
    stamp = now_ms() if at is None else at
    row = await db.fetch_one(
        f"""
        WITH candidate AS (
            SELECT chat_id, tg_message_id, state
              FROM voice_inputs
             WHERE state IN ('received', 'transcribed')
             ORDER BY created_at
             LIMIT 1
               FOR UPDATE SKIP LOCKED
        ),
        claimed AS (
            UPDATE voice_inputs v
               SET state = CASE WHEN c.state = 'received'
                                THEN 'transcribing' ELSE 'dispatching' END,
                   attempts = v.attempts
                              + CASE WHEN c.state = 'received' THEN 1 ELSE 0 END,
                   updated_at = ?,
                   last_error = NULL
              FROM candidate c
             WHERE v.chat_id = c.chat_id
               AND v.tg_message_id = c.tg_message_id
               AND v.state = c.state
            RETURNING {_QUALIFIED_COLUMNS}
        )
        SELECT {_COLUMNS} FROM claimed
        """,
        (stamp,),
    )
    return None if row is None else VoiceInputRow.from_row(row)


async def mark_transcribed(
    db: Database,
    row: VoiceInputRow,
    *,
    transcript: str,
    language: str | None,
    intent_json: str,
    at: int | None = None,
) -> VoiceInputRow | None:
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE voice_inputs
           SET state = 'transcribed', transcript = ?, language = ?,
               intent_json = ?, last_error = NULL, updated_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (transcript, language, intent_json, stamp, row.chat_id, row.tg_message_id),
    )
    return await get(db, row.chat_id, row.tg_message_id)


async def mark_dispatching(
    db: Database, row: VoiceInputRow, *, at: int | None = None
) -> VoiceInputRow | None:
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE voice_inputs
           SET state = 'dispatching', updated_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (stamp, row.chat_id, row.tg_message_id),
    )
    return await get(db, row.chat_id, row.tg_message_id)


async def complete(
    db: Database, row: VoiceInputRow, *, at: int | None = None
) -> VoiceInputRow | None:
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE voice_inputs
           SET state = 'completed', last_error = NULL, updated_at = ?,
               completed_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (stamp, stamp, row.chat_id, row.tg_message_id),
    )
    return await get(db, row.chat_id, row.tg_message_id)


async def wait_for_user(
    db: Database,
    row: VoiceInputRow,
    *,
    reason: str | None = None,
    at: int | None = None,
) -> VoiceInputRow | None:
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE voice_inputs
           SET state = 'waiting_for_user', last_error = ?, updated_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (reason, stamp, row.chat_id, row.tg_message_id),
    )
    return await get(db, row.chat_id, row.tg_message_id)


async def fail(
    db: Database, row: VoiceInputRow, *, error: str, at: int | None = None
) -> VoiceInputRow | None:
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE voice_inputs
           SET state = 'failed', last_error = ?, updated_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (error[:500], stamp, row.chat_id, row.tg_message_id),
    )
    return await get(db, row.chat_id, row.tg_message_id)


async def requeue(
    db: Database, chat_id: int, tg_message_id: int, *, at: int | None = None
) -> bool:
    stamp = now_ms() if at is None else at
    changed = await db.execute(
        """
        UPDATE voice_inputs
           SET state = CASE WHEN transcript IS NULL THEN 'received'
                            ELSE 'transcribed' END,
               last_error = NULL, updated_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
           AND state IN ('failed', 'waiting_for_user')
        """,
        (stamp, chat_id, tg_message_id),
    )
    return changed == 1


async def recover_stale(
    db: Database,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    orphan_after_ms: int = ORPHAN_AFTER_MS,
    at: int | None = None,
) -> Recovery:
    """Recover jobs whose process died after a conditional claim.

    A job is requeued only while it still has attempts left. One that has
    already burned ``max_attempts`` is marked ``failed`` instead: the note is
    reproducibly killing the process, and every replay costs the owner money.

    **Only jobs older than ``orphan_after_ms`` are touched.** This runs at
    boot, and a Railway deploy overlaps the old container with the new one: a
    row still marked ``transcribing`` may have a live speech-vendor request in
    flight on the peer. Requeueing it transcribes the same note twice, bills
    the customer twice and dispatches the prompt twice. ``deliveries`` has the
    same guard for the same reason — and voice is the path that costs real
    money per call, so it needs it more, not less.
    """
    stamp = now_ms() if at is None else at
    cutoff = stamp - max(0, orphan_after_ms)
    exhausted = await db.fetch_all(
        f"""
        SELECT {_COLUMNS}
          FROM voice_inputs
         WHERE state IN ('transcribing', 'dispatching')
           AND attempts >= ?
           AND updated_at <= ?
        """,
        (max_attempts, cutoff),
    )
    abandoned: list[VoiceInputRow] = []
    for raw in exhausted:
        row = VoiceInputRow.from_row(raw)
        failed = await fail(
            db,
            row,
            error=f"Gave up after {max_attempts} attempts.",
            at=stamp,
        )
        abandoned.append(failed or row)
    requeued = await db.execute(
        """
        UPDATE voice_inputs
           SET state = CASE WHEN transcript IS NULL THEN 'received'
                            ELSE 'transcribed' END,
               updated_at = ?
         WHERE state IN ('transcribing', 'dispatching')
           AND updated_at <= ?
        """,
        (stamp, cutoff),
    )
    return Recovery(requeued=requeued, abandoned=tuple(abandoned))


async def count_pending(db: Database) -> int:
    value = await db.fetch_val(
        """
        SELECT COUNT(*)
          FROM voice_inputs
         WHERE state NOT IN ('completed', 'failed', 'waiting_for_user')
        """,
        default=0,
    )
    return int(value or 0)


async def health_stats(db: Database) -> dict[str, object]:
    rows = await db.fetch_all(
        "SELECT state, COUNT(*) AS count FROM voice_inputs GROUP BY state"
    )
    by_state = {as_str(row["state"]): as_int(row["count"]) for row in rows}
    average = await db.fetch_val(
        """
        SELECT AVG(completed_at - created_at)
          FROM voice_inputs
         WHERE completed_at IS NOT NULL
        """,
        default=0,
    )
    pending = sum(
        by_state.get(state, 0)
        for state in ("received", "transcribing", "transcribed", "dispatching")
    )
    return {
        "pending": pending,
        # Broken out of ``pending`` on purpose: a note parked here is the shape
        # a hang takes, and "1 pending" cannot be told apart from a note that
        # arrived a second ago.
        "transcribing": by_state.get("transcribing", 0),
        "failed": by_state.get("failed", 0),
        "waiting_for_user": by_state.get("waiting_for_user", 0),
        "completed": by_state.get("completed", 0),
        "avg_latency_ms": int(float(average or 0)),
        "by_state": by_state,
    }


async def prune_terminal(db: Database, *, before: int) -> int:
    """Drop finished jobs — including ``failed`` and ``waiting_for_user``.

    Every one of those rows still holds a transcript, so retention has to cover
    each terminal state, not just the happy path. ``completed_at`` is only set
    by :func:`complete`, hence the fallback to ``updated_at``.
    """
    placeholders = ", ".join("?" for _ in _TERMINAL_STATES)
    return await db.execute(
        f"""
        DELETE FROM voice_inputs
         WHERE state IN ({placeholders})
           AND COALESCE(completed_at, updated_at) < ?
        """,
        (*_TERMINAL_STATES, before),
    )
