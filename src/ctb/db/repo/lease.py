"""``singleton_lease`` — only one supervisor may poll.

Layer 2 of the redeploy-overlap defence (layer 1 is the Railway volume's
single-attach plus ``numReplicas=1``). A 15s TTL heartbeated every 5s: the
supervisor refuses to spawn a poller without the lease and cancels every task
the moment it loses it.

The race is decided inside the database, not in Python. :func:`acquire` is a
single ``INSERT … ON CONFLICT DO UPDATE … WHERE``: the update only fires when
the incumbent lease has expired or is already ours,
so of two processes starting together exactly one gets a lease and the loser
gets ``None``.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from typing import Final, Self

from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo._util import as_int, as_str

__all__ = [
    "HEARTBEAT_MS",
    "SUPERVISOR",
    "TTL_MS",
    "Lease",
    "acquire",
    "get",
    "heartbeat",
    "instance_id",
    "release",
]

#: The one lease name this project uses.
SUPERVISOR: Final = "supervisor"
#: A lease this old may be stolen by whoever asks next.
TTL_MS: Final = 15_000
#: Renew this often. Three renewals fit inside one TTL.
HEARTBEAT_MS: Final = 5_000

_COLUMNS = "name, holder, acquired_at, heartbeat_at, expires_at"


def instance_id() -> str:
    """A holder token unique to this process. Logged, never a secret."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class Lease:
    name: str
    holder: str
    acquired_at: int = 0
    heartbeat_at: int = 0
    expires_at: int = 0

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            name=as_str(row["name"]),
            holder=as_str(row["holder"]),
            acquired_at=as_int(row["acquired_at"]),
            heartbeat_at=as_int(row["heartbeat_at"]),
            expires_at=as_int(row["expires_at"]),
        )

    def is_expired(self, at: int) -> bool:
        return at >= self.expires_at

    def held_by(self, holder: str, at: int) -> bool:
        return self.holder == holder and not self.is_expired(at)


async def get(db: Database, name: str = SUPERVISOR) -> Lease | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM singleton_lease WHERE name = ?", (name,)
    )
    return None if row is None else Lease.from_row(row)


async def acquire(
    db: Database,
    name: str = SUPERVISOR,
    *,
    holder: str,
    ttl_ms: int = TTL_MS,
    at: int | None = None,
) -> Lease | None:
    """Take the lease, or return ``None`` if someone else holds a live one.

    Re-acquiring a lease we already hold is a renewal and keeps the original
    ``acquired_at``, so "how long have we been the supervisor" stays true.
    """
    stamp = now_ms() if at is None else at
    expires = stamp + max(1, ttl_ms)
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO singleton_lease
                (name, holder, acquired_at, heartbeat_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                holder       = excluded.holder,
                acquired_at  = CASE WHEN singleton_lease.holder = excluded.holder
                                    THEN singleton_lease.acquired_at
                                    ELSE excluded.acquired_at END,
                heartbeat_at = excluded.heartbeat_at,
                expires_at   = excluded.expires_at
              WHERE singleton_lease.holder = excluded.holder
                 OR singleton_lease.expires_at <= excluded.heartbeat_at
            """,
            (name, holder, stamp, stamp, expires),
        )
        current = await get(db, name)
    if current is None:  # pragma: no cover - the insert above guarantees a row
        return None
    return current if current.holder == holder else None


async def heartbeat(
    db: Database,
    name: str = SUPERVISOR,
    *,
    holder: str,
    ttl_ms: int = TTL_MS,
    at: int | None = None,
) -> Lease | None:
    """Extend our lease. ``None`` means we lost it — cancel every poller now."""
    stamp = now_ms() if at is None else at
    expires = stamp + max(1, ttl_ms)
    async with db.transaction():
        changed = await db.execute(
            """
            UPDATE singleton_lease
               SET heartbeat_at = ?, expires_at = ?
             WHERE name = ? AND holder = ?
            """,
            (stamp, expires, name, holder),
        )
        if changed == 0:
            return None
        return await get(db, name)


async def release(db: Database, name: str = SUPERVISOR, *, holder: str) -> bool:
    """Give the lease back on a clean shutdown so the next boot starts instantly."""
    changed = await db.execute(
        "DELETE FROM singleton_lease WHERE name = ? AND holder = ?", (name, holder)
    )
    return changed > 0
