"""``allowed_users`` — who may drive the bot.

The env var ``ALLOWED_TELEGRAM_USER_IDS`` is the authority; this table is the
durable mirror of it plus whatever ``/allow`` added at runtime. The auth
middleware checks the union of the two, so a DB that has not been synced yet can
never lock the owner out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self

import aiosqlite

from ctb.db.connection import Database, now_ms
from ctb.db.repo._util import as_bool, as_int, as_opt_int, as_opt_str, bit

__all__ = [
    "AllowedUser",
    "count",
    "get",
    "is_allowed",
    "list_all",
    "owner",
    "remove",
    "sync",
    "upsert",
]

_COLUMNS = "user_id, is_owner, username, note, added_by, added_at"


@dataclass(frozen=True, slots=True)
class AllowedUser:
    user_id: int
    is_owner: bool = False
    username: str | None = None
    note: str | None = None
    added_by: int | None = None
    added_at: int = 0

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Self:
        return cls(
            user_id=as_int(row["user_id"]),
            is_owner=as_bool(row["is_owner"]),
            username=as_opt_str(row["username"]),
            note=as_opt_str(row["note"]),
            added_by=as_opt_int(row["added_by"]),
            added_at=as_int(row["added_at"]),
        )


async def upsert(
    db: Database,
    user_id: int,
    *,
    is_owner: bool = False,
    username: str | None = None,
    note: str | None = None,
    added_by: int | None = None,
    at: int | None = None,
) -> AllowedUser:
    """Add or update an allow-listed user. Idempotent."""
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        INSERT INTO allowed_users
            (user_id, is_owner, username, note, added_by, added_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            is_owner = excluded.is_owner,
            username = COALESCE(excluded.username, allowed_users.username),
            note     = COALESCE(excluded.note, allowed_users.note),
            added_by = COALESCE(allowed_users.added_by, excluded.added_by)
        """,
        (user_id, bit(is_owner), username, note, added_by, stamp),
    )
    stored = await get(db, user_id)
    if stored is None:  # pragma: no cover - the upsert above guarantees a row
        raise RuntimeError(f"allowed_users row vanished for {user_id}")
    return stored


async def get(db: Database, user_id: int) -> AllowedUser | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM allowed_users WHERE user_id = ?", (user_id,)
    )
    return None if row is None else AllowedUser.from_row(row)


async def is_allowed(db: Database, user_id: int | None) -> bool:
    if user_id is None:
        return False
    return (
        await db.fetch_val(
            "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,), default=0
        )
        == 1
    )


async def list_all(db: Database) -> list[AllowedUser]:
    rows = await db.fetch_all(
        f"SELECT {_COLUMNS} FROM allowed_users ORDER BY is_owner DESC, user_id"
    )
    return [AllowedUser.from_row(row) for row in rows]


async def owner(db: Database) -> AllowedUser | None:
    row = await db.fetch_one(
        f"""
        SELECT {_COLUMNS} FROM allowed_users
         WHERE is_owner = 1 ORDER BY user_id LIMIT 1
        """
    )
    return None if row is None else AllowedUser.from_row(row)


async def count(db: Database) -> int:
    return as_int(await db.fetch_val("SELECT COUNT(*) FROM allowed_users", default=0))


async def remove(db: Database, user_id: int) -> bool:
    """Delete a user. The owner is never removable."""
    changed = await db.execute(
        "DELETE FROM allowed_users WHERE user_id = ? AND is_owner = 0", (user_id,)
    )
    return changed > 0


async def sync(
    db: Database,
    user_ids: Sequence[int],
    *,
    owner_id: int | None = None,
    at: int | None = None,
) -> list[AllowedUser]:
    """Mirror the env allow-list into the table (boot path).

    Runtime additions made with ``/allow`` are left alone: this only inserts and
    re-stamps ``is_owner``, it never deletes, and it never clobbers a ``note``.
    """
    stamp = now_ms() if at is None else at
    if owner_id is not None:
        effective_owner = owner_id
    else:
        effective_owner = user_ids[0] if user_ids else None
    async with db.transaction():
        for user_id in user_ids:
            await upsert(db, user_id, is_owner=user_id == effective_owner, at=stamp)
    return await list_all(db)
