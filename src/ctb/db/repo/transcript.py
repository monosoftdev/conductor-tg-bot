"""``transcript_messages`` — the source of truth for content, and the cursor.

This module owns the single most important operation in the project:

.. code-block:: sql

    BEGIN;
      SELECT 1 FROM sessions WHERE id = ? FOR UPDATE;
      INSERT INTO transcript_messages(...) ON CONFLICT DO NOTHING;
      INSERT INTO deliveries(... state='pending') ON CONFLICT DO NOTHING;
      UPDATE sessions SET cursor_message_id=?, cursor_session_index=?;
    COMMIT;

Two invariants fall out of it, and everything else in the bot leans on them:

* **The cursor never advances past an unrecorded message.** If any insert
  raises, the whole transaction rolls back and the cursor stays where it was, so
  the next poll re-fetches the same page. No drops.
* **A replay is a no-op.** ``ON CONFLICT DO NOTHING`` against the primary keys
  makes a re-delivered page — from ``after=`` being ignored, from two pollers
  overlapping across a redeploy, from a boot-time forced drain — produce zero
  duplicate rows and therefore zero duplicate Telegram messages.

The cursor is also strictly monotonic in ``session_index``: the ``UPDATE`` is
conditional, so an out-of-order or stale page can never rewind it. Note that
``sessionIndex`` is *not* gapless (0, 2, 3, 4, … was observed live) — a gap
never means a message was missed.

Content is the user's source code. It is stored verbatim as JSON but capped at
:data:`ctb.db.MAX_CONTENT_BYTES` per message and pruned after
:data:`ctb.db.TRANSCRIPT_RETENTION_DAYS` days.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Self

from ctb.conductor.models import TranscriptMessage
from ctb.db import MAX_CONTENT_BYTES, NO_THREAD_ID, TRANSCRIPT_RETENTION_DAYS
from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo._util import (
    as_bool,
    as_int,
    as_opt_int,
    as_opt_str,
    as_str,
    content_hash,
    dumps,
)
from ctb.logging import get_logger

__all__ = [
    "AdvanceItem",
    "AdvanceResult",
    "DeliveryDraft",
    "StoredMessage",
    "advance_cursor",
    "should_shed",
    "count",
    "cursor_of",
    "encode_content",
    "get",
    "list_for_turn",
    "max_session_index",
    "parse_received_at_ms",
    "prune",
    "recent",
    "record",
]

_DAY_MS = 86_400_000

_COLUMNS = """
    session_id, message_id, session_index, type, content_json, content_truncated,
    content_bytes, turn_id, content_id, received_at, received_at_ms, inserted_at
"""

# ``ON CONFLICT DO NOTHING`` rather than ``INSERT OR IGNORE``: the two look
# interchangeable and are not. ``OR IGNORE`` swallows *every* constraint
# violation — a CHECK, a NOT NULL, a bad foreign key — which would silently drop
# a message or a delivery while the cursor moved on regardless. That is the one
# failure this design must not have. ``DO NOTHING`` ignores only a uniqueness
# conflict, which is exactly the replay case, and still raises on everything
# else so the transaction rolls back and the next poll re-fetches the page.
_INSERT_MESSAGE = """
    INSERT INTO transcript_messages
        (session_id, message_id, session_index, type, content_json,
         content_truncated, content_bytes, turn_id, content_id, received_at,
         received_at_ms, inserted_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT DO NOTHING
"""

_INSERT_DELIVERY = """
    INSERT INTO deliveries
        (session_id, message_id, part_index, chat_id, thread_id, session_index,
         kind, priority, state, content_hash, payload_json, created_at,
         updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
    ON CONFLICT DO NOTHING
"""

#: Strictly monotonic in ``session_index`` — a stale page cannot rewind us.
#: ``seeded`` is set too: once real content has been recorded, a later
#: seek-to-end must not be allowed to jump the cursor over undelivered messages.
_log = get_logger(__name__)

#: Anything above this is shed under backpressure. Mirrors
#: ``ctb.delivery.outbox.Priority.ERROR`` without importing upwards.
_ERROR_PRIORITY: Final = 10

_ADVANCE_CURSOR = """
    UPDATE sessions
       SET cursor_message_id = ?, cursor_session_index = ?, seeded = true,
           updated_at = ?
     WHERE id = ? AND cursor_session_index < ?
"""


# ── encoding ─────────────────────────────────────────────────────────────────


def parse_received_at_ms(value: str | None) -> int | None:
    """Parse the API's timestamp (``"2026-07-26 02:00:37.434+00"``) to epoch ms.

    It is not strict ISO-8601, which is why the raw string is also kept. A naive
    timestamp is read as UTC. Returns ``None`` when it cannot be parsed at all,
    and the caller substitutes "now" — a message must never sort as epoch 0 and
    be pruned the moment it arrives.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("z", "Z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def encode_content(content: Any) -> tuple[str, bool, int]:
    """JSON-encode ``content``, capped at :data:`ctb.db.MAX_CONTENT_BYTES`.

    Returns ``(json_text, truncated, byte_length_before_the_cap)``. When the cap
    bites, the stored value is a small envelope that keeps the classification
    keys (``type``, ``turnId``, …) and drops the payload, so the row stays valid
    JSON and the renderer can still tell what it was looking at.
    """
    text = dumps(content)
    size = len(text.encode("utf-8"))
    if size <= MAX_CONTENT_BYTES:
        return text, False, size
    summary: dict[str, Any] = {"_truncated": True, "_bytes": size}
    if isinstance(content, dict):
        for key in ("type", "turnId", "userMessageId", "id", "eventId"):
            value = content.get(key)
            if isinstance(value, str):
                summary[key] = value
        raw = content.get("rawPayload")
        if isinstance(raw, dict):
            raw_type = raw.get("type")
            if isinstance(raw_type, str):
                summary["rawPayloadType"] = raw_type
    return dumps(summary), True, size


# ── rows ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StoredMessage:
    session_id: str
    message_id: str
    session_index: int
    type: str = ""
    content_json: str | None = None
    content_truncated: bool = False
    content_bytes: int = 0
    turn_id: str | None = None
    content_id: str | None = None
    received_at: str | None = None
    received_at_ms: int = 0
    inserted_at: int = 0

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            session_id=str(row["session_id"]),
            message_id=str(row["message_id"]),
            session_index=as_int(row["session_index"], -1),
            type=as_str(row["type"]),
            content_json=as_opt_str(row["content_json"]),
            content_truncated=as_bool(row["content_truncated"]),
            content_bytes=as_int(row["content_bytes"]),
            turn_id=as_opt_str(row["turn_id"]),
            content_id=as_opt_str(row["content_id"]),
            received_at=as_opt_str(row["received_at"]),
            received_at_ms=as_int(row["received_at_ms"]),
            inserted_at=as_int(row["inserted_at"]),
        )


@dataclass(frozen=True, slots=True)
class DeliveryDraft:
    """One rendered chunk bound for one chat, queued in the same transaction.

    ``content_hash`` is filled in from ``payload_json`` when omitted; it is what
    the boot-time re-send guard compares against already-sent rows.
    """

    chat_id: int
    thread_id: int = NO_THREAD_ID
    part_index: int = 0
    kind: str = "text"
    #: Send order; see ``ctb.delivery.outbox.Priority``. Lower goes first.
    priority: int = 20
    payload_json: str | None = None
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class AdvanceItem:
    """A fetched message plus whatever the renderer wants delivered for it."""

    message: TranscriptMessage
    deliveries: tuple[DeliveryDraft, ...] = ()


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    cursor_message_id: str | None = None
    cursor_session_index: int = -1
    recorded: int = 0
    duplicates: int = 0
    deliveries_created: int = 0
    deliveries_duplicate: int = 0
    advanced: bool = False
    turn_ids: frozenset[str] = field(default_factory=frozenset)
    content_ids: frozenset[str] = field(default_factory=frozenset)


async def cursor_of(db: Database, session_id: str) -> tuple[str | None, int] | None:
    row = await db.fetch_one(
        "SELECT cursor_message_id, cursor_session_index FROM sessions WHERE id = ?",
        (session_id,),
    )
    if row is None:
        return None
    return as_opt_str(row["cursor_message_id"]), as_int(row["cursor_session_index"], -1)


async def should_shed(
    db: Database, *, max_pending: int | None, session_id: str = ""
) -> bool:
    """Is this tenant's delivery backlog over its ceiling?

    ``None`` disables the check. Extracted from ``advance_cursor`` so the
    protection is one callable thing: as an inline branch it had no test, and
    disabling it did not turn the suite red.

    The count is tenant-scoped by row-level security and served by the partial
    claim index, so it is an index-only scan of one workspace's pending rows.
    """
    if max_pending is None:
        return False
    pending = await deliveries_repo.pending_count(db)
    if pending < max_pending:
        return False
    _log.warning(
        "transcript.deliveries_shed",
        session_id=session_id,
        pending=pending,
        limit=max_pending,
    )
    return True


async def advance_cursor(
    db: Database,
    session_id: str,
    items: Sequence[AdvanceItem],
    *,
    max_pending: int | None = None,
    at: int | None = None,
) -> AdvanceResult:
    """Record a page of transcript messages and move the cursor, atomically.

    Ordering is by ``session_index`` ascending regardless of how the caller
    passed them, so the cursor lands on the highest index actually recorded.

    Raises ``ValueError`` if an item belongs to a different session — that is a
    routing bug, and silently storing it under the wrong session would poison
    the cursor. Any database error rolls the whole page back and leaves the
    cursor untouched; the next poll re-fetches it.
    """
    stamp = now_ms() if at is None else at
    for item in items:
        owner = item.message.session_id
        if owner and owner != session_id:
            raise ValueError(
                f"message {item.message.id!r} belongs to session {owner!r}, "
                f"not {session_id!r}"
            )

    if not items:
        cursor = await cursor_of(db, session_id)
        if cursor is None:
            return AdvanceResult()
        return AdvanceResult(
            cursor_message_id=cursor[0], cursor_session_index=cursor[1]
        )

    ordered = sorted(items, key=lambda item: item.message.session_index)
    # Backpressure. A workspace whose chat the bot can no longer post to would
    # otherwise grow `deliveries` without bound, and the destination scan every
    # other workspace depends on degrades with it. The *cursor still advances*:
    # dropping a delivery is recoverable from the transcript, losing the cursor
    # is not.
    shed = await should_shed(db, max_pending=max_pending, session_id=session_id)
    recorded = 0
    duplicates = 0
    created = 0
    dup_deliveries = 0
    turn_ids: set[str] = set()
    content_ids: set[str] = set()

    async with db.transaction():
        # Serialise per session. Recording messages, queueing their deliveries
        # and moving the cursor must be one indivisible step; two pollers on the
        # same session would otherwise interleave and leave a cursor that has
        # advanced past deliveries the other transaction had not committed yet.
        # Under SQLite this came free from BEGIN IMMEDIATE.
        await db.execute(
            "SELECT 1 FROM sessions WHERE id = ? FOR UPDATE", (session_id,)
        )
        for item in ordered:
            message = item.message
            content_json, truncated, size = encode_content(message.content)
            received_ms = parse_received_at_ms(message.received_at)
            inserted = await db.execute(
                _INSERT_MESSAGE,
                (
                    session_id,
                    message.id,
                    message.session_index,
                    message.type,
                    content_json,
                    truncated,
                    size,
                    message.turn_id,
                    message.content_id,
                    message.received_at or None,
                    stamp if received_ms is None else received_ms,
                    stamp,
                ),
            )
            if inserted > 0:
                recorded += 1
            else:
                duplicates += 1
            if message.turn_id:
                turn_ids.add(message.turn_id)
            if message.is_user_echo and message.content_id:
                content_ids.add(message.content_id)

            for draft in item.deliveries:
                if shed and draft.priority > int(_ERROR_PRIORITY):
                    # Errors still get through; they are how the owner finds out.
                    continue
                payload = draft.payload_json
                digest = draft.content_hash
                if digest is None and payload is not None:
                    digest = content_hash(payload)
                queued = await db.execute(
                    _INSERT_DELIVERY,
                    (
                        session_id,
                        message.id,
                        draft.part_index,
                        draft.chat_id,
                        draft.thread_id,
                        message.session_index,
                        draft.kind,
                        draft.priority,
                        digest,
                        payload,
                        stamp,
                        stamp,
                    ),
                )
                if queued > 0:
                    created += 1
                else:
                    dup_deliveries += 1

        top = ordered[-1].message
        moved = await db.execute(
            _ADVANCE_CURSOR,
            (top.id, top.session_index, stamp, session_id, top.session_index),
        )
        cursor = await cursor_of(db, session_id)

    cursor_id, cursor_index = cursor if cursor is not None else (None, -1)
    return AdvanceResult(
        cursor_message_id=cursor_id,
        cursor_session_index=cursor_index,
        recorded=recorded,
        duplicates=duplicates,
        deliveries_created=created,
        deliveries_duplicate=dup_deliveries,
        advanced=moved > 0,
        turn_ids=frozenset(turn_ids),
        content_ids=frozenset(content_ids),
    )


async def record(
    db: Database,
    session_id: str,
    message: TranscriptMessage,
    *,
    deliveries: Sequence[DeliveryDraft] = (),
    at: int | None = None,
) -> AdvanceResult:
    """Single-message convenience wrapper around :func:`advance_cursor`."""
    return await advance_cursor(
        db,
        session_id,
        (AdvanceItem(message=message, deliveries=tuple(deliveries)),),
        at=at,
    )


# ── reads ────────────────────────────────────────────────────────────────────


async def get(db: Database, session_id: str, message_id: str) -> StoredMessage | None:
    row = await db.fetch_one(
        f"""
        SELECT {_COLUMNS} FROM transcript_messages
         WHERE session_id = ? AND message_id = ?
        """,
        (session_id, message_id),
    )
    return None if row is None else StoredMessage.from_row(row)


async def list_for_turn(
    db: Database, session_id: str, turn_id: str, *, limit: int = 500
) -> list[StoredMessage]:
    """Every message of one turn, in transcript order. Correlated on ``turnId``."""
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM transcript_messages
         WHERE session_id = ? AND turn_id = ?
         ORDER BY session_index
         LIMIT ?
        """,
        (session_id, turn_id, max(1, limit)),
    )
    return [StoredMessage.from_row(row) for row in rows]


async def recent(
    db: Database, session_id: str, *, limit: int = 20
) -> list[StoredMessage]:
    """The newest messages first — what ``/transcript`` renders."""
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM transcript_messages
         WHERE session_id = ?
         ORDER BY session_index DESC
         LIMIT ?
        """,
        (session_id, max(1, limit)),
    )
    return [StoredMessage.from_row(row) for row in rows]


async def count(db: Database, session_id: str | None = None) -> int:
    if session_id is None:
        return as_int(
            await db.fetch_val("SELECT COUNT(*) FROM transcript_messages", default=0)
        )
    return as_int(
        await db.fetch_val(
            "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
            (session_id,),
            default=0,
        )
    )


async def max_session_index(db: Database, session_id: str) -> int | None:
    """The highest index we have stored. Not necessarily contiguous."""
    return as_opt_int(
        await db.fetch_val(
            "SELECT MAX(session_index) FROM transcript_messages WHERE session_id = ?",
            (session_id,),
        )
    )


async def prune(
    db: Database,
    *,
    older_than_days: int = TRANSCRIPT_RETENTION_DAYS,
    at: int | None = None,
) -> int:
    """Delete transcript content older than the retention window.

    Deliveries are deliberately **not** cascaded (there is no FK): delivery
    history is metadata, transcript content is the user's source code.
    """
    stamp = now_ms() if at is None else at
    cutoff = stamp - max(0, older_than_days) * _DAY_MS
    return await db.execute(
        "DELETE FROM transcript_messages WHERE received_at_ms < ?", (cutoff,)
    )
