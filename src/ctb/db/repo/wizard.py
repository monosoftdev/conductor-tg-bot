"""``wizard_state`` — DB-backed aiogram FSM, so a wizard survives a redeploy.

Keyed by ``(chat_id, thread_id, user_id)``: two people can run ``/new`` in the
same topic without colliding. Each wizard edits one message in place, and
``tg_message_id`` is that message.

State older than its ``expires_at`` is treated as absent by :func:`get` — a
half-finished wizard from yesterday must not hijack today's button press — and
swept by :func:`prune_expired`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

import aiosqlite

from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, now_ms
from ctb.db.repo._util import (
    UNSET,
    Maybe,
    Unset,
    as_int,
    as_opt_int,
    as_opt_str,
    dumps,
    loads,
)

__all__ = [
    "DEFAULT_TTL_MS",
    "WizardRow",
    "clear",
    "get",
    "list_active",
    "merge_data",
    "prune_expired",
    "set_state",
]

#: A wizard nobody finishes is forgotten after this long.
DEFAULT_TTL_MS = 30 * 60 * 1000

_COLUMNS = """
    chat_id, thread_id, user_id, state_key, data_json, tg_message_id,
    updated_at, expires_at
"""


@dataclass(frozen=True, slots=True)
class WizardRow:
    chat_id: int
    thread_id: int = NO_THREAD_ID
    user_id: int = 0
    state_key: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    tg_message_id: int | None = None
    updated_at: int = 0
    expires_at: int | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Self:
        return cls(
            chat_id=as_int(row["chat_id"]),
            thread_id=as_int(row["thread_id"]),
            user_id=as_int(row["user_id"]),
            state_key=as_opt_str(row["state_key"]),
            data=loads(as_opt_str(row["data_json"])),
            tg_message_id=as_opt_int(row["tg_message_id"]),
            updated_at=as_int(row["updated_at"]),
            expires_at=as_opt_int(row["expires_at"]),
        )

    def is_expired(self, at: int) -> bool:
        return self.expires_at is not None and at >= self.expires_at


async def get(
    db: Database,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    *,
    user_id: int,
    at: int | None = None,
) -> WizardRow | None:
    """The live wizard for this seat, or None if there is none (or it expired)."""
    stamp = now_ms() if at is None else at
    row = await db.fetch_one(
        f"""
        SELECT {_COLUMNS} FROM wizard_state
         WHERE chat_id = ? AND thread_id = ? AND user_id = ?
        """,
        (chat_id, thread_id, user_id),
    )
    if row is None:
        return None
    parsed = WizardRow.from_row(row)
    return None if parsed.is_expired(stamp) else parsed


async def set_state(
    db: Database,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    *,
    user_id: int,
    state_key: str | None,
    data: Mapping[str, Any] | None = None,
    tg_message_id: Maybe[int | None] = UNSET,
    ttl_ms: int = DEFAULT_TTL_MS,
    at: int | None = None,
) -> WizardRow:
    """Write the wizard's step. ``data`` replaces; use :func:`merge_data` to patch.

    ``tg_message_id`` is kept when omitted, so advancing a step does not lose
    the message the wizard is editing in place.
    """
    stamp = now_ms() if at is None else at
    expires = stamp + ttl_ms if ttl_ms > 0 else None
    payload = dumps(dict(data)) if data is not None else dumps({})
    keep_message = isinstance(tg_message_id, Unset)
    message_value = None if keep_message else tg_message_id
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO wizard_state
                (chat_id, thread_id, user_id, state_key, data_json,
                 tg_message_id, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id, user_id) DO UPDATE SET
                state_key     = excluded.state_key,
                data_json     = excluded.data_json,
                tg_message_id = CASE WHEN ? THEN wizard_state.tg_message_id
                                     ELSE excluded.tg_message_id END,
                updated_at    = excluded.updated_at,
                expires_at    = excluded.expires_at
            """,
            (
                chat_id,
                thread_id,
                user_id,
                state_key,
                payload,
                message_value,
                stamp,
                expires,
                1 if keep_message else 0,
            ),
        )
        row = await db.fetch_one(
            f"""
            SELECT {_COLUMNS} FROM wizard_state
             WHERE chat_id = ? AND thread_id = ? AND user_id = ?
            """,
            (chat_id, thread_id, user_id),
        )
    if row is None:  # pragma: no cover - the upsert above guarantees a row
        raise RuntimeError("wizard_state row vanished")
    return WizardRow.from_row(row)


async def merge_data(
    db: Database,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    *,
    user_id: int,
    patch: Mapping[str, Any],
    state_key: Maybe[str | None] = UNSET,
    tg_message_id: Maybe[int | None] = UNSET,
    ttl_ms: int = DEFAULT_TTL_MS,
    at: int | None = None,
) -> WizardRow:
    """Read-modify-write the wizard's ``data`` inside one transaction."""
    stamp = now_ms() if at is None else at
    async with db.transaction():
        current = await get(db, chat_id, thread_id, user_id=user_id, at=stamp)
        merged: dict[str, Any] = dict(current.data) if current is not None else {}
        merged.update(patch)
        if isinstance(state_key, Unset):
            key = current.state_key if current is not None else None
        else:
            key = state_key
        return await set_state(
            db,
            chat_id,
            thread_id,
            user_id=user_id,
            state_key=key,
            data=merged,
            tg_message_id=tg_message_id,
            ttl_ms=ttl_ms,
            at=stamp,
        )


async def clear(
    db: Database, chat_id: int, thread_id: int = NO_THREAD_ID, *, user_id: int
) -> bool:
    changed = await db.execute(
        """
        DELETE FROM wizard_state
         WHERE chat_id = ? AND thread_id = ? AND user_id = ?
        """,
        (chat_id, thread_id, user_id),
    )
    return changed > 0


async def list_active(db: Database, *, at: int | None = None) -> list[WizardRow]:
    stamp = now_ms() if at is None else at
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM wizard_state
         WHERE expires_at IS NULL OR expires_at > ?
         ORDER BY updated_at DESC
        """,
        (stamp,),
    )
    return [WizardRow.from_row(row) for row in rows]


async def prune_expired(db: Database, *, at: int | None = None) -> int:
    stamp = now_ms() if at is None else at
    return await db.execute(
        "DELETE FROM wizard_state WHERE expires_at IS NOT NULL AND expires_at <= ?",
        (stamp,),
    )
