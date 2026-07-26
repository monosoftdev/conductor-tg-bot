"""``outbound_prompts`` — the idempotency ledger for ``POST /messages``.

The order is load-bearing and verified against the live API:

1. :func:`create` writes the row with a locally generated ``message_id``
   (uuid4) **before** any HTTP call.
2. The client POSTs with that id as ``messageId``.
3. :func:`mark_posted` records the outcome.

Re-POSTing the same ``messageId`` dedupes server-side (probe assumption #7,
passed twice: one user echo, not two), so an ambiguous outcome — timeout, reset,
5xx — is retried **forever with the same id**. A crash between step 1 and step 3
leaves the row in ``pending``; boot picks it up via :func:`list_recoverable` and
re-POSTs (transition 3).

A prompt is *witnessed* when the transcript shows a ``userMessage`` whose
``content.id`` equals our ``message_id``; from then on every message of that
turn carries it as ``content.turnId``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Self

import aiosqlite

from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, now_ms
from ctb.db.repo._util import as_int, as_opt_int, as_opt_str, as_str, update_sql

__all__ = [
    "PromptRow",
    "abandon",
    "create",
    "delete",
    "get",
    "list_for_session",
    "list_recoverable",
    "list_unsettled",
    "mark_failed",
    "mark_posted",
    "mark_witnessed",
    "new_message_id",
    "outstanding_count",
    "record_attempt",
    "witness_many",
]

#: States that still owe us an outcome — the poller must not finalize a turn
#: while one of these is younger than ``PROMPT_AGE_OUT_S``.
UNSETTLED_STATES = ("pending", "posted")

_COLUMNS = """
    message_id, session_id, chat_id, thread_id, tg_message_id, body,
    index_at_post, state, post_state, turn_id, attempts, last_error, created_at,
    posted_at, witnessed_at
"""


def new_message_id() -> str:
    """A fresh Conductor idempotency key. Never reused across prompts."""
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class PromptRow:
    message_id: str
    session_id: str
    body: str = ""
    chat_id: int | None = None
    thread_id: int = NO_THREAD_ID
    tg_message_id: int | None = None
    index_at_post: int | None = None
    state: str = "pending"
    post_state: str | None = None
    turn_id: str | None = None
    attempts: int = 0
    last_error: str | None = None
    created_at: int = 0
    posted_at: int | None = None
    witnessed_at: int | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Self:
        return cls(
            message_id=str(row["message_id"]),
            session_id=str(row["session_id"]),
            body=as_str(row["body"]),
            chat_id=as_opt_int(row["chat_id"]),
            thread_id=as_int(row["thread_id"]),
            tg_message_id=as_opt_int(row["tg_message_id"]),
            index_at_post=as_opt_int(row["index_at_post"]),
            state=as_str(row["state"], "pending"),
            post_state=as_opt_str(row["post_state"]),
            turn_id=as_opt_str(row["turn_id"]),
            attempts=as_int(row["attempts"]),
            last_error=as_opt_str(row["last_error"]),
            created_at=as_int(row["created_at"]),
            posted_at=as_opt_int(row["posted_at"]),
            witnessed_at=as_opt_int(row["witnessed_at"]),
        )

    @property
    def is_settled(self) -> bool:
        return self.state not in UNSETTLED_STATES


async def create(
    db: Database,
    *,
    session_id: str,
    body: str,
    chat_id: int | None = None,
    thread_id: int = NO_THREAD_ID,
    tg_message_id: int | None = None,
    index_at_post: int | None = None,
    message_id: str | None = None,
    at: int | None = None,
) -> PromptRow:
    """Write the ledger row. **Call this before the HTTP request, never after.**"""
    stamp = now_ms() if at is None else at
    key = message_id or new_message_id()
    await db.execute(
        """
        INSERT INTO outbound_prompts
            (message_id, session_id, chat_id, thread_id, tg_message_id, body,
             index_at_post, state, attempts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
        """,
        (
            key,
            session_id,
            chat_id,
            thread_id,
            tg_message_id,
            body,
            index_at_post,
            stamp,
        ),
    )
    row = await get(db, key)
    if row is None:  # pragma: no cover - the insert above guarantees a row
        raise RuntimeError(f"outbound_prompts row vanished for {key}")
    return row


async def get(db: Database, message_id: str) -> PromptRow | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM outbound_prompts WHERE message_id = ?", (message_id,)
    )
    return None if row is None else PromptRow.from_row(row)


async def _set(
    db: Database, message_id: str, columns: dict[str, Any]
) -> PromptRow | None:
    async with db.transaction():
        await db.execute(
            update_sql("outbound_prompts", columns, "message_id = ?"),
            (*columns.values(), message_id),
        )
        return await get(db, message_id)


async def record_attempt(
    db: Database, message_id: str, *, error: str | None = None
) -> PromptRow | None:
    """Count a POST attempt (including the ambiguous ones we will retry)."""
    async with db.transaction():
        await db.execute(
            """
            UPDATE outbound_prompts
               SET attempts = attempts + 1, last_error = ?
             WHERE message_id = ?
            """,
            (error, message_id),
        )
        return await get(db, message_id)


async def mark_posted(
    db: Database,
    message_id: str,
    *,
    post_state: str | None = None,
    index_at_post: int | None = None,
    at: int | None = None,
) -> PromptRow | None:
    """The POST returned 2xx. ``post_state`` is the API's ``queued``/``sent``."""
    stamp = now_ms() if at is None else at
    columns: dict[str, Any] = {
        "state": "posted",
        "post_state": post_state,
        "posted_at": stamp,
        "last_error": None,
    }
    if index_at_post is not None:
        columns["index_at_post"] = index_at_post
    return await _set(db, message_id, columns)


async def mark_witnessed(
    db: Database,
    message_id: str,
    *,
    turn_id: str | None = None,
    at: int | None = None,
) -> PromptRow | None:
    """The transcript echoed this prompt back. The turn is definitely running."""
    stamp = now_ms() if at is None else at
    return await _set(
        db,
        message_id,
        {
            "state": "witnessed",
            "turn_id": turn_id or message_id,
            "witnessed_at": stamp,
        },
    )


async def mark_failed(db: Database, message_id: str, *, error: str) -> PromptRow | None:
    """Terminal, non-retryable failure (a 4xx that is not ambiguous)."""
    return await _set(db, message_id, {"state": "failed", "last_error": error})


async def abandon(
    db: Database, message_id: str, *, reason: str = "aged out"
) -> PromptRow | None:
    """Stop waiting: the prompt aged out or the user cleared the queue."""
    return await _set(db, message_id, {"state": "abandoned", "last_error": reason})


async def witness_many(
    db: Database,
    session_id: str,
    content_ids: Iterable[str],
    *,
    at: int | None = None,
) -> list[str]:
    """Witness every prompt whose id appeared as ``content.id`` in this delta.

    Returns the ids that actually moved, so the caller can emit exactly one
    ``AbandonPrompt``/card update per prompt.
    """
    ids = [value for value in dict.fromkeys(content_ids) if value]
    if not ids:
        return []
    stamp = now_ms() if at is None else at
    placeholders = ", ".join("?" for _ in ids)
    async with db.transaction():
        rows = await db.fetch_all(
            f"""
            SELECT message_id FROM outbound_prompts
             WHERE session_id = ?
               AND message_id IN ({placeholders})
               AND state IN ('pending', 'posted')
            """,
            (session_id, *ids),
        )
        moved = [str(row["message_id"]) for row in rows]
        if moved:
            await db.execute(
                f"""
                UPDATE outbound_prompts
                   SET state = 'witnessed',
                       witnessed_at = ?,
                       turn_id = COALESCE(turn_id, message_id)
                 WHERE session_id = ?
                   AND message_id IN ({", ".join("?" for _ in moved)})
                """,
                (stamp, session_id, *moved),
            )
    return moved


async def list_unsettled(db: Database, session_id: str) -> list[PromptRow]:
    """Prompts POSTed (or about to be) that the transcript has not echoed yet."""
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM outbound_prompts
         WHERE session_id = ? AND state IN ('pending', 'posted')
         ORDER BY created_at
        """,
        (session_id,),
    )
    return [PromptRow.from_row(row) for row in rows]


async def list_recoverable(
    db: Database, *, session_id: str | None = None
) -> list[PromptRow]:
    """Boot recovery (transition 3): rows written but never confirmed posted.

    These are re-POSTed with the **identical** ``message_id``.
    """
    if session_id is None:
        rows = await db.fetch_all(
            f"""
            SELECT {_COLUMNS} FROM outbound_prompts
             WHERE state = 'pending'
             ORDER BY created_at
            """
        )
    else:
        rows = await db.fetch_all(
            f"""
            SELECT {_COLUMNS} FROM outbound_prompts
             WHERE state = 'pending' AND session_id = ?
             ORDER BY created_at
            """,
            (session_id,),
        )
    return [PromptRow.from_row(row) for row in rows]


async def list_for_session(
    db: Database, session_id: str, *, limit: int = 50
) -> list[PromptRow]:
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM outbound_prompts
         WHERE session_id = ?
         ORDER BY created_at DESC
         LIMIT ?
        """,
        (session_id, max(1, limit)),
    )
    return [PromptRow.from_row(row) for row in rows]


async def outstanding_count(db: Database, session_id: str) -> int:
    return as_int(
        await db.fetch_val(
            """
            SELECT COUNT(*) FROM outbound_prompts
             WHERE session_id = ? AND state IN ('pending', 'posted')
            """,
            (session_id,),
            default=0,
        )
    )


async def delete(db: Database, message_id: str) -> bool:
    changed = await db.execute(
        "DELETE FROM outbound_prompts WHERE message_id = ?", (message_id,)
    )
    return changed > 0
