"""``deliveries`` — one row per rendered chunk per destination chat.

The worker claims work with a **conditional** update:

.. code-block:: sql

    UPDATE deliveries SET state='sending', claim_id=? WHERE state='pending'

Conditional because two pollers may briefly overlap across a redeploy. Only one
``pending -> sending`` transition can win, so only one of them sends. Rows come
back in ``(session_index, part_index)`` order, which is transcript order.

The residual window is a crash between the Telegram call and the DB write. That
is resolved deliberately in favour of **at-least-once**: on boot, rows left in
``sending`` are re-sent, because a rare duplicate beats a silently lost reply.
:func:`recover_orphaned` softens the common case by skipping any orphan whose
``content_hash`` already appears on a recently ``sent`` row in the same chat.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Self

import aiosqlite

from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, now_ms
from ctb.db.repo._util import (
    as_int,
    as_opt_int,
    as_opt_str,
    as_str,
    content_hash,
    update_sql,
)

__all__ = [
    "DEDUPE_WINDOW_MS",
    "DeliveryKey",
    "DeliveryRow",
    "RecoveryResult",
    "claim",
    "content_hash",
    "counts_by_state",
    "delete_for_session",
    "enqueue",
    "get",
    "list_for_session",
    "mark_failed",
    "mark_sent",
    "mark_skipped",
    "new_claim_id",
    "pending_count",
    "recover_orphaned",
    "release",
    "requeue",
    "session_for_telegram_message",
]

#: How far back the boot-time duplicate guard looks for an identical payload.
DEDUPE_WINDOW_MS = 10 * 60 * 1000

_COLUMNS = """
    session_id, message_id, part_index, chat_id, thread_id, session_index, kind,
    state, claim_id, content_hash, payload_json, tg_message_id, attempts,
    last_error, created_at, updated_at, sent_at
"""

_KEY_WHERE = "session_id = ? AND message_id = ? AND part_index = ? AND chat_id = ?"


def new_claim_id() -> str:
    """A claim token. One per worker pass, so a crash is identifiable on boot."""
    return str(uuid.uuid4())


#: ``(session_id, message_id, part_index, chat_id)`` — the primary key.
type DeliveryKey = tuple[str, str, int, int]


@dataclass(frozen=True, slots=True)
class DeliveryRow:
    session_id: str
    message_id: str
    part_index: int = 0
    chat_id: int = 0
    thread_id: int = NO_THREAD_ID
    session_index: int = 0
    kind: str = "text"
    state: str = "pending"
    claim_id: str | None = None
    content_hash: str | None = None
    payload_json: str | None = None
    tg_message_id: int | None = None
    attempts: int = 0
    last_error: str | None = None
    created_at: int = 0
    updated_at: int = 0
    sent_at: int | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Self:
        return cls(
            session_id=str(row["session_id"]),
            message_id=str(row["message_id"]),
            part_index=as_int(row["part_index"]),
            chat_id=as_int(row["chat_id"]),
            thread_id=as_int(row["thread_id"]),
            session_index=as_int(row["session_index"]),
            kind=as_str(row["kind"], "text"),
            state=as_str(row["state"], "pending"),
            claim_id=as_opt_str(row["claim_id"]),
            content_hash=as_opt_str(row["content_hash"]),
            payload_json=as_opt_str(row["payload_json"]),
            tg_message_id=as_opt_int(row["tg_message_id"]),
            attempts=as_int(row["attempts"]),
            last_error=as_opt_str(row["last_error"]),
            created_at=as_int(row["created_at"]),
            updated_at=as_int(row["updated_at"]),
            sent_at=as_opt_int(row["sent_at"]),
        )

    @property
    def key(self) -> DeliveryKey:
        return (self.session_id, self.message_id, self.part_index, self.chat_id)


async def enqueue(
    db: Database,
    *,
    session_id: str,
    message_id: str,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    part_index: int = 0,
    session_index: int = 0,
    kind: str = "text",
    payload_json: str | None = None,
    hash_of_content: str | None = None,
    at: int | None = None,
) -> bool:
    """Queue one chunk. ``ON CONFLICT DO NOTHING``, so re-queueing is a no-op.

    Transcript deliveries are queued inside
    :func:`ctb.db.repo.transcript.advance_cursor` instead — this is for
    synthetic messages (status cards, notices) that have no transcript row.
    Returns True when the row was newly created.
    """
    stamp = now_ms() if at is None else at
    digest = hash_of_content
    if digest is None and payload_json is not None:
        digest = content_hash(payload_json)
    inserted = await db.execute(
        """
        INSERT INTO deliveries
            (session_id, message_id, part_index, chat_id, thread_id,
             session_index, kind, state, content_hash, payload_json, created_at,
             updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            session_id,
            message_id,
            part_index,
            chat_id,
            thread_id,
            session_index,
            kind,
            digest,
            payload_json,
            stamp,
            stamp,
        ),
    )
    return inserted > 0


async def get(db: Database, key: DeliveryKey) -> DeliveryRow | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM deliveries WHERE {_KEY_WHERE}", key
    )
    return None if row is None else DeliveryRow.from_row(row)


async def claim(
    db: Database,
    *,
    claim_id: str,
    limit: int = 20,
    session_id: str | None = None,
    at: int | None = None,
) -> list[DeliveryRow]:
    """Atomically take up to ``limit`` pending rows for this worker.

    ``BEGIN IMMEDIATE`` plus the ``state = 'pending'`` predicate means two
    workers — two tasks, or two processes across a redeploy — can never claim
    the same row. The loser simply gets fewer rows.
    """
    stamp = now_ms() if at is None else at
    scope = "" if session_id is None else "AND session_id = ?"
    params: tuple[Any, ...] = (
        (claim_id, stamp, max(1, limit))
        if session_id is None
        else (claim_id, stamp, session_id, max(1, limit))
    )
    async with db.transaction():
        await db.execute(
            f"""
            UPDATE deliveries
               SET state = 'sending', claim_id = ?, updated_at = ?
             WHERE rowid IN (
                   -- The ORDER BY matches idx_deliveries_claim
                   -- (state, session_index, part_index) exactly, so this is an
                   -- index scan with no temp B-tree.
                   SELECT rowid FROM deliveries
                    WHERE state = 'pending' {scope}
                    ORDER BY session_index, part_index
                    LIMIT ?
             )
            """,
            params,
        )
        rows = await db.fetch_all(
            f"""
            SELECT {_COLUMNS} FROM deliveries
             WHERE claim_id = ? AND state = 'sending'
             ORDER BY session_index, part_index
            """,
            (claim_id,),
        )
    return [DeliveryRow.from_row(row) for row in rows]


async def _set(
    db: Database, key: DeliveryKey, columns: dict[str, Any]
) -> DeliveryRow | None:
    async with db.transaction():
        await db.execute(
            update_sql("deliveries", columns, _KEY_WHERE), (*columns.values(), *key)
        )
        return await get(db, key)


async def mark_sent(
    db: Database,
    key: DeliveryKey,
    *,
    tg_message_id: int | None = None,
    at: int | None = None,
) -> DeliveryRow | None:
    """Telegram accepted it. This is the write the crash window can lose."""
    stamp = now_ms() if at is None else at
    return await _set(
        db,
        key,
        {
            "state": "sent",
            "tg_message_id": tg_message_id,
            "claim_id": None,
            "last_error": None,
            "sent_at": stamp,
            "updated_at": stamp,
        },
    )


async def mark_failed(
    db: Database,
    key: DeliveryKey,
    *,
    error: str,
    retry: bool = True,
    at: int | None = None,
) -> DeliveryRow | None:
    """Record a send failure.

    ``retry=True`` puts the row back in ``pending`` for another attempt; pass
    False for a permanent failure (a 403 from a chat we were kicked out of).
    """
    stamp = now_ms() if at is None else at
    async with db.transaction():
        await db.execute(
            f"""
            UPDATE deliveries
               SET state = ?, claim_id = NULL, attempts = attempts + 1,
                   last_error = ?, updated_at = ?
             WHERE {_KEY_WHERE}
            """,
            ("pending" if retry else "failed", error, stamp, *key),
        )
        return await get(db, key)


async def mark_skipped(
    db: Database, key: DeliveryKey, *, reason: str, at: int | None = None
) -> DeliveryRow | None:
    """Deliberately not sent — filtered by verbosity, or a boot-time duplicate."""
    stamp = now_ms() if at is None else at
    return await _set(
        db,
        key,
        {
            "state": "skipped",
            "claim_id": None,
            "last_error": reason,
            "updated_at": stamp,
        },
    )


async def requeue(
    db: Database, key: DeliveryKey, *, at: int | None = None
) -> DeliveryRow | None:
    stamp = now_ms() if at is None else at
    return await _set(
        db, key, {"state": "pending", "claim_id": None, "updated_at": stamp}
    )


async def release(db: Database, claim_id: str, *, at: int | None = None) -> int:
    """Hand unsent rows back on a clean shutdown, so boot need not guess."""
    stamp = now_ms() if at is None else at
    return await db.execute(
        """
        UPDATE deliveries
           SET state = 'pending', claim_id = NULL, updated_at = ?
         WHERE claim_id = ? AND state = 'sending'
        """,
        (stamp, claim_id),
    )


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """What boot found in ``sending`` from a previous, crashed incarnation."""

    claimed: tuple[DeliveryRow, ...] = ()
    skipped: tuple[DeliveryRow, ...] = ()

    @property
    def total(self) -> int:
        return len(self.claimed) + len(self.skipped)


async def recover_orphaned(
    db: Database,
    *,
    claim_id: str,
    dedupe_window_ms: int = DEDUPE_WINDOW_MS,
    at: int | None = None,
) -> RecoveryResult:
    """Take over rows stranded in ``sending`` by a crash. At-least-once.

    Each orphan is compared against recently ``sent`` rows in the same chat: an
    identical ``content_hash`` means the Telegram call almost certainly landed
    and only the bookkeeping write was lost, so the row is skipped. Everything
    else is re-claimed and re-sent — losing a reply is worse than repeating one.
    """
    stamp = now_ms() if at is None else at
    cutoff = stamp - max(0, dedupe_window_ms)
    claimed: list[DeliveryRow] = []
    skipped: list[DeliveryRow] = []
    async with db.transaction():
        rows = await db.fetch_all(
            f"""
            SELECT {_COLUMNS} FROM deliveries
             WHERE state = 'sending'
             ORDER BY session_index, part_index
            """
        )
        for raw in rows:
            row = DeliveryRow.from_row(raw)
            duplicate = False
            if row.content_hash is not None:
                duplicate = (
                    await db.fetch_val(
                        """
                        SELECT 1 FROM deliveries
                         WHERE chat_id = ? AND thread_id = ? AND content_hash = ?
                           AND state = 'sent' AND COALESCE(sent_at, 0) >= ?
                         LIMIT 1
                        """,
                        (row.chat_id, row.thread_id, row.content_hash, cutoff),
                        default=0,
                    )
                    == 1
                )
            if duplicate:
                await db.execute(
                    f"""
                    UPDATE deliveries
                       SET state = 'skipped', claim_id = NULL,
                           last_error = 'boot: identical payload already sent',
                           updated_at = ?
                     WHERE {_KEY_WHERE}
                    """,
                    (stamp, *row.key),
                )
                skipped.append(row)
            else:
                await db.execute(
                    f"""
                    UPDATE deliveries
                       SET claim_id = ?, updated_at = ?
                     WHERE {_KEY_WHERE}
                    """,
                    (claim_id, stamp, *row.key),
                )
                claimed.append(replace(row, claim_id=claim_id))
    return RecoveryResult(claimed=tuple(claimed), skipped=tuple(skipped))


async def pending_count(db: Database, session_id: str | None = None) -> int:
    if session_id is None:
        return as_int(
            await db.fetch_val(
                "SELECT COUNT(*) FROM deliveries WHERE state = 'pending'", default=0
            )
        )
    return as_int(
        await db.fetch_val(
            """
            SELECT COUNT(*) FROM deliveries
             WHERE state = 'pending' AND session_id = ?
            """,
            (session_id,),
            default=0,
        )
    )


async def session_for_telegram_message(
    db: Database,
    chat_id: int,
    tg_message_id: int,
) -> str | None:
    """Resolve an agent reply or status card back to its session."""
    row = await db.fetch_one(
        """
        SELECT session_id
          FROM deliveries
         WHERE chat_id = ? AND tg_message_id = ? AND state = 'sent'
         ORDER BY sent_at DESC
         LIMIT 1
        """,
        (chat_id, tg_message_id),
    )
    if row is not None:
        return str(row["session_id"])
    row = await db.fetch_one(
        """
        SELECT id
          FROM sessions
         WHERE chat_id = ? AND status_card_msg_id = ?
         LIMIT 1
        """,
        (chat_id, tg_message_id),
    )
    return None if row is None else str(row["id"])


async def counts_by_state(
    db: Database, session_id: str | None = None
) -> dict[str, int]:
    """State histogram for ``/health``."""
    if session_id is None:
        rows = await db.fetch_all(
            "SELECT state, COUNT(*) AS n FROM deliveries GROUP BY state"
        )
    else:
        rows = await db.fetch_all(
            """
            SELECT state, COUNT(*) AS n FROM deliveries
             WHERE session_id = ? GROUP BY state
            """,
            (session_id,),
        )
    return {as_str(row["state"]): as_int(row["n"]) for row in rows}


async def list_for_session(
    db: Database, session_id: str, *, state: str | None = None, limit: int = 100
) -> list[DeliveryRow]:
    if state is None:
        rows = await db.fetch_all(
            f"""
            SELECT {_COLUMNS} FROM deliveries
             WHERE session_id = ?
             ORDER BY session_index, part_index
             LIMIT ?
            """,
            (session_id, max(1, limit)),
        )
    else:
        rows = await db.fetch_all(
            f"""
            SELECT {_COLUMNS} FROM deliveries
             WHERE session_id = ? AND state = ?
             ORDER BY session_index, part_index
             LIMIT ?
            """,
            (session_id, state, max(1, limit)),
        )
    return [DeliveryRow.from_row(row) for row in rows]


async def delete_for_session(db: Database, session_id: str) -> int:
    return await db.execute(
        "DELETE FROM deliveries WHERE session_id = ?", (session_id,)
    )
