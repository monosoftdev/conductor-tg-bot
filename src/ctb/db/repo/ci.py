"""Pull requests whose CI the bot is watching.

One row per pull request, not per turn. A second turn that pushes to the same
PR re-arms the existing row rather than racing a duplicate of it, and the
``notified_*`` columns are what stop a watch that is merely *re-read* from
saying the same thing twice: the notice fires when the verdict for a **new
commit** differs from the one already reported.

Claiming is the ``deliveries`` pattern without the orphan bookkeeping. A claim
pushes ``next_poll_at`` into the future before the poll runs, so a worker that
dies mid-poll costs one interval of delay and nothing else — there is no
``claimed_at`` to reconcile because a missed poll is simply the next poll.

**Every mutation here names ``tenant_id`` explicitly**, which the rest of the
repo layer deliberately never does. The reason is the same one that makes
``deliveries.claim`` take a ``tenant_id`` argument: these statements run on the
``ctb_worker`` pool, which holds ``BYPASSRLS``, so the policy that normally
supplies the filter is not evaluated. Two customers watching the same public
pull request is not a hypothetical — and without the predicate one poll would
write both rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Self

from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo._util import as_int, as_opt_str, as_str

__all__ = [
    "GIVE_UP_AFTER_MS",
    "POLL_INTERVAL_MS",
    "CiWatchRow",
    "claim_due",
    "count_watching",
    "fail",
    "finish",
    "get",
    "list_for_session",
    "prune_terminal",
    "reschedule",
    "watch",
]

#: How often a watched pull request is re-read. CI runs take minutes; polling
#: faster only spends the tenant's GitHub rate limit.
POLL_INTERVAL_MS: Final = 30_000
#: A watch that has learned nothing in this long is abandoned. Some pull
#: requests never get a CI run at all, and a row that polls forever is a slow
#: leak of somebody else's rate limit.
GIVE_UP_AFTER_MS: Final = 3 * 60 * 60 * 1000
#: A watch that keeps erroring (repo renamed, token scope wrong) is not worth
#: an unbounded number of requests.
MAX_ERRORS: Final = 10

_COLUMNS = """
    tenant_id, owner, repo, pr_number, session_id, chat_id, thread_id, state,
    head_sha, last_status, notified_sha, notified_status, attempts, last_error,
    created_at, updated_at, next_poll_at, expires_at
"""

_QUALIFIED = ", ".join(f"c.{name.strip()}" for name in _COLUMNS.split(","))


@dataclass(frozen=True, slots=True)
class CiWatchRow:
    tenant_id: uuid.UUID
    owner: str
    repo: str
    pr_number: int
    session_id: str
    chat_id: int
    thread_id: int
    state: str
    head_sha: str | None
    last_status: str | None
    notified_sha: str | None
    notified_status: str | None
    attempts: int
    last_error: str | None
    created_at: int
    updated_at: int
    next_poll_at: int
    expires_at: int

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            tenant_id=row["tenant_id"],
            owner=as_str(row["owner"]),
            repo=as_str(row["repo"]),
            pr_number=as_int(row["pr_number"]),
            session_id=as_str(row["session_id"]),
            chat_id=as_int(row["chat_id"]),
            thread_id=as_int(row["thread_id"]),
            state=as_str(row["state"], "watching"),
            head_sha=as_opt_str(row["head_sha"]),
            last_status=as_opt_str(row["last_status"]),
            notified_sha=as_opt_str(row["notified_sha"]),
            notified_status=as_opt_str(row["notified_status"]),
            attempts=as_int(row["attempts"]),
            last_error=as_opt_str(row["last_error"]),
            created_at=as_int(row["created_at"]),
            updated_at=as_int(row["updated_at"]),
            next_poll_at=as_int(row["next_poll_at"]),
            expires_at=as_int(row["expires_at"]),
        )

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.pr_number}"

    def already_said(self, sha: str, status: str) -> bool:
        """Whether this exact verdict, for this exact commit, has gone out."""
        return self.notified_sha == sha and self.notified_status == status


async def watch(
    db: Database,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    session_id: str,
    chat_id: int,
    thread_id: int,
    at: int | None = None,
    ttl_ms: int = GIVE_UP_AFTER_MS,
) -> CiWatchRow | None:
    """Start — or re-arm — the watch on one pull request.

    Re-arming deliberately keeps ``notified_sha``: the turn that just ended
    usually pushed a new commit, and it is the *commit* changing that makes the
    next verdict worth repeating, not the turn ending.
    """
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        INSERT INTO ci_watches (owner, repo, pr_number, session_id, chat_id,
                                thread_id, state, created_at, updated_at,
                                next_poll_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, 'watching', ?, ?, ?, ?)
        ON CONFLICT (tenant_id, owner, repo, pr_number) DO UPDATE
           SET session_id   = EXCLUDED.session_id,
               chat_id      = EXCLUDED.chat_id,
               thread_id    = EXCLUDED.thread_id,
               state        = 'watching',
               attempts     = 0,
               last_error   = NULL,
               updated_at   = EXCLUDED.updated_at,
               next_poll_at = EXCLUDED.next_poll_at,
               expires_at   = EXCLUDED.expires_at
        """,
        (
            owner,
            repo,
            pr_number,
            session_id,
            chat_id,
            thread_id,
            stamp,
            stamp,
            stamp,
            stamp + ttl_ms,
        ),
    )
    return await get(db, owner=owner, repo=repo, pr_number=pr_number)


async def get(
    db: Database,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    tenant_id: uuid.UUID | None = None,
) -> CiWatchRow | None:
    """Read one watch. ``tenant_id`` is required on the BYPASSRLS pool."""
    if tenant_id is None:
        row = await db.fetch_one(
            f"SELECT {_COLUMNS} FROM ci_watches "
            "WHERE owner = ? AND repo = ? AND pr_number = ?",
            (owner, repo, pr_number),
        )
    else:
        row = await db.fetch_one(
            f"SELECT {_COLUMNS} FROM ci_watches "
            "WHERE tenant_id = ? AND owner = ? AND repo = ? AND pr_number = ?",
            (tenant_id, owner, repo, pr_number),
        )
    return None if row is None else CiWatchRow.from_row(row)


async def list_for_session(db: Database, session_id: str) -> tuple[CiWatchRow, ...]:
    rows = await db.fetch_all(
        f"SELECT {_COLUMNS} FROM ci_watches WHERE session_id = ? "
        "ORDER BY created_at DESC",
        (session_id,),
    )
    return tuple(CiWatchRow.from_row(row) for row in rows)


async def claim_due(
    db: Database, *, limit: int = 16, at: int | None = None
) -> tuple[CiWatchRow, ...]:
    """Take up to ``limit`` watches whose next poll is due, cross-tenant.

    Runs on the worker pool: the claim is the one query that must see every
    tenant at once. ``FOR UPDATE SKIP LOCKED`` gives overlapping deployments
    disjoint sets, and the pushed-forward ``next_poll_at`` is the lease.
    """
    stamp = now_ms() if at is None else at
    rows = await db.fetch_all(
        f"""
        WITH candidate AS (
            SELECT tenant_id, owner, repo, pr_number
              FROM ci_watches
             WHERE state = 'watching'
               AND next_poll_at <= ?
             ORDER BY next_poll_at
             LIMIT ?
               FOR UPDATE SKIP LOCKED
        ),
        claimed AS (
            UPDATE ci_watches c
               SET next_poll_at = ?, updated_at = ?
              FROM candidate d
             WHERE c.tenant_id = d.tenant_id
               AND c.owner = d.owner
               AND c.repo = d.repo
               AND c.pr_number = d.pr_number
            RETURNING {_QUALIFIED}
        )
        SELECT {_COLUMNS} FROM claimed
        """,
        (stamp, limit, stamp + POLL_INTERVAL_MS, stamp),
    )
    return tuple(CiWatchRow.from_row(row) for row in rows)


async def reschedule(
    db: Database,
    row: CiWatchRow,
    *,
    head_sha: str | None,
    status: str,
    delay_ms: int = POLL_INTERVAL_MS,
    at: int | None = None,
) -> None:
    """Record what this poll saw and keep watching."""
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE ci_watches
           SET head_sha = ?, last_status = ?, attempts = 0, last_error = NULL,
               updated_at = ?, next_poll_at = ?
         WHERE tenant_id = ? AND owner = ? AND repo = ? AND pr_number = ?
        """,
        (
            head_sha,
            status,
            stamp,
            stamp + delay_ms,
            row.tenant_id,
            row.owner,
            row.repo,
            row.pr_number,
        ),
    )


async def finish(
    db: Database,
    row: CiWatchRow,
    *,
    head_sha: str | None,
    status: str,
    notified: bool,
    state: str = "done",
    at: int | None = None,
) -> None:
    """Stop watching. ``notified`` records that the owner has been told."""
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE ci_watches
           SET state = ?, head_sha = ?, last_status = ?,
               notified_sha = CASE WHEN ? THEN ? ELSE notified_sha END,
               notified_status = CASE WHEN ? THEN ? ELSE notified_status END,
               last_error = NULL, updated_at = ?
         WHERE tenant_id = ? AND owner = ? AND repo = ? AND pr_number = ?
        """,
        (
            state,
            head_sha,
            status,
            notified,
            head_sha,
            notified,
            status,
            stamp,
            row.tenant_id,
            row.owner,
            row.repo,
            row.pr_number,
        ),
    )


async def fail(
    db: Database,
    row: CiWatchRow,
    *,
    error: str,
    fatal: bool = False,
    delay_ms: int = POLL_INTERVAL_MS,
    at: int | None = None,
) -> None:
    """Record a failed poll; give up after :data:`MAX_ERRORS`, or at once."""
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE ci_watches
           SET attempts = attempts + 1,
               last_error = ?,
               state = CASE WHEN ? OR attempts + 1 >= ? THEN 'gave_up'
                            ELSE state END,
               updated_at = ?,
               next_poll_at = ?
         WHERE tenant_id = ? AND owner = ? AND repo = ? AND pr_number = ?
        """,
        (
            error[:200],
            fatal,
            MAX_ERRORS,
            stamp,
            stamp + delay_ms,
            row.tenant_id,
            row.owner,
            row.repo,
            row.pr_number,
        ),
    )


async def count_watching(db: Database) -> int:
    value = await db.fetch_val(
        "SELECT COUNT(*) FROM ci_watches WHERE state = 'watching'"
    )
    return int(value or 0)


async def prune_terminal(db: Database, *, older_than_ms: int) -> int:
    """Drop finished watches. Cross-tenant, so it runs on the worker pool."""
    cutoff = now_ms() - older_than_ms
    return await db.execute(
        "DELETE FROM ci_watches WHERE state <> 'watching' AND updated_at < ?",
        (cutoff,),
    )
