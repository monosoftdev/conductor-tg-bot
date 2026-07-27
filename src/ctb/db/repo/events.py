"""``api_events`` and ``unknown_content_types`` — the observability tables.

``api_events`` is a ring buffer behind ``/health``: every Conductor request
lands here with its status, duration, attempt number and circuit state, and the
oldest rows are dropped by row id once the buffer is full.

``unknown_content_types`` counts what the renderer could not classify. It
deliberately stores a *pointer* to a sample message — session id and message id
— and a structural signature, never the content itself: transcript content is
the user's source code. :func:`shape_signature` digests key *paths* only, with
no values anywhere near it.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Self

from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo._util import as_int, as_opt_int, as_opt_str, as_str

__all__ = [
    "ApiEvent",
    "ApiEventStats",
    "MAX_API_EVENTS",
    "UnknownContentType",
    "list_unknown_content_types",
    "note_unknown_content_type",
    "prune_api_events",
    "record_api_event",
    "recent_api_events",
    "shape_signature",
    "stats",
]

#: How many API events the ring buffer keeps.
MAX_API_EVENTS: Final = 2_000
#: Prune is amortised: it runs on one insert in this many.
_PRUNE_EVERY: Final = 256
#: Depth and breadth caps so a pathological payload cannot make a huge signature.
_SIGNATURE_MAX_DEPTH: Final = 6
_SIGNATURE_MAX_KEYS: Final = 200

_EVENT_COLUMNS = """
    id, tenant_id, at, method, endpoint, status_code, duration_ms, attempt, ok,
    error, circuit_state, request_id, session_id
"""

_UNKNOWN_COLUMNS = """
    type, shape_signature, count, sample_session_id, sample_message_id,
    first_seen_at, last_seen_at
"""


@dataclass(frozen=True, slots=True)
class ApiEvent:
    id: int
    tenant_id: uuid.UUID | None = None
    at: int = 0
    method: str | None = None
    endpoint: str | None = None
    status_code: int | None = None
    duration_ms: int | None = None
    attempt: int = 1
    ok: bool = True
    error: str | None = None
    circuit_state: str | None = None
    request_id: str | None = None
    session_id: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            id=as_int(row["id"]),
            tenant_id=row["tenant_id"],
            at=as_int(row["at"]),
            method=as_opt_str(row["method"]),
            endpoint=as_opt_str(row["endpoint"]),
            status_code=as_opt_int(row["status_code"]),
            duration_ms=as_opt_int(row["duration_ms"]),
            attempt=as_int(row["attempt"], 1),
            ok=bool(row["ok"]),
            error=as_opt_str(row["error"]),
            circuit_state=as_opt_str(row["circuit_state"]),
            request_id=as_opt_str(row["request_id"]),
            session_id=as_opt_str(row["session_id"]),
        )


@dataclass(frozen=True, slots=True)
class ApiEventStats:
    """What ``/health`` prints for a window."""

    total: int = 0
    ok: int = 0
    failed: int = 0
    avg_duration_ms: int = 0
    max_duration_ms: int = 0
    last_error: str | None = None
    last_error_at: int | None = None

    @property
    def error_rate(self) -> float:
        return 0.0 if self.total == 0 else self.failed / self.total


async def record_api_event(
    db: Database,
    *,
    method: str,
    endpoint: str,
    status_code: int | None = None,
    duration_ms: int | None = None,
    attempt: int = 1,
    ok: bool = True,
    error: str | None = None,
    circuit_state: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    tenant_id: uuid.UUID | None = None,
    at: int | None = None,
) -> int:
    """Append one request to the ring buffer. Returns its row id.

    ``tenant_id`` attributes the call, so one tenant's failing key shows up as
    that tenant's error rate rather than everyone's.

    ``endpoint`` must already be templated (``/sessions/{id}/messages``) so the
    buffer groups by route rather than by id, and never records a secret.
    """
    stamp = now_ms() if at is None else at
    event_id = await db.fetch_val(
        """
        INSERT INTO api_events
            (tenant_id, at, method, endpoint, status_code, duration_ms, attempt,
             ok, error, circuit_state, request_id, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            tenant_id,
            stamp,
            method,
            endpoint,
            status_code,
            duration_ms,
            max(1, attempt),
            ok,
            error,
            circuit_state,
            request_id,
            session_id,
        ),
    )
    row_id = 0 if event_id is None else int(event_id)
    if row_id and row_id % _PRUNE_EVERY == 0:
        await prune_api_events(db)
    return row_id


async def recent_api_events(
    db: Database,
    *,
    limit: int = 50,
    only_failed: bool = False,
    tenant_id: uuid.UUID | None = None,
) -> list[ApiEvent]:
    """The tail of the ring buffer.

    ``api_events`` is a system table with no row-level security — a request can
    fail before a tenant is resolved — so a caller answering *one tenant's*
    ``/health`` must pass ``tenant_id`` explicitly.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if only_failed:
        clauses.append("NOT ok")
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, limit))
    rows = await db.fetch_all(
        f"""
        SELECT {_EVENT_COLUMNS} FROM api_events
        {where}
         ORDER BY id DESC
         LIMIT ?
        """,
        tuple(params),
    )
    return [ApiEvent.from_row(row) for row in rows]


async def stats(
    db: Database, *, since_ms: int, tenant_id: uuid.UUID | None = None
) -> ApiEventStats:
    scope = "" if tenant_id is None else "AND tenant_id = ?"
    scope_params: tuple[Any, ...] = () if tenant_id is None else (tenant_id,)
    row = await db.fetch_one(
        f"""
        SELECT COUNT(*)                            AS total,
               COUNT(*) FILTER (WHERE ok)          AS ok,
               COALESCE(AVG(duration_ms), 0)       AS avg_ms,
               COALESCE(MAX(duration_ms), 0)       AS max_ms
          FROM api_events
         WHERE at >= ? {scope}
        """,
        (since_ms, *scope_params),
    )
    if row is None:  # pragma: no cover - an aggregate always returns one row
        return ApiEventStats()
    total = as_int(row["total"])
    ok = as_int(row["ok"])
    failure = await db.fetch_one(
        f"""
        SELECT error, at FROM api_events
         WHERE at >= ? AND NOT ok AND error IS NOT NULL {scope}
         ORDER BY id DESC
         LIMIT 1
        """,
        (since_ms, *scope_params),
    )
    return ApiEventStats(
        total=total,
        ok=ok,
        failed=total - ok,
        avg_duration_ms=int(as_int(row["avg_ms"])),
        max_duration_ms=as_int(row["max_ms"]),
        last_error=None if failure is None else as_opt_str(failure["error"]),
        last_error_at=None if failure is None else as_int(failure["at"]),
    )


async def prune_api_events(db: Database, *, keep: int = MAX_API_EVENTS) -> int:
    """Drop everything older than the newest ``keep`` rows."""
    return await db.execute(
        """
        DELETE FROM api_events
         WHERE id <= (SELECT MAX(id) FROM api_events) - ?
        """,
        (max(1, keep),),
    )


# ── unknown content types ────────────────────────────────────────────────────


def shape_signature(content: Any) -> str:
    """A digest of the key *paths* in ``content``. Values never enter it.

    ``{"a": {"b": 1}, "c": [{"d": 2}]}`` and ``{"a": {"b": 9}, "c": [{"d": 8}]}``
    produce the same signature, so a recurring unknown shape is counted once
    rather than once per message.
    """
    paths: list[str] = []
    _walk(content, "", paths, 0)
    paths.sort()
    joined = "\n".join(paths[:_SIGNATURE_MAX_KEYS])
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    return digest if joined else f"empty:{digest}"


def _walk(value: Any, prefix: str, out: list[str], depth: int) -> None:
    if len(out) >= _SIGNATURE_MAX_KEYS:
        return
    if depth >= _SIGNATURE_MAX_DEPTH:
        out.append(f"{prefix}:…")
        return
    if isinstance(value, dict):
        items: Iterable[Any] = sorted(str(key) for key in value)  # pyright: ignore[reportUnknownArgumentType]
        for key in items:
            child = value[key]  # pyright: ignore[reportUnknownVariableType]
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(child, dict | list):
                _walk(child, path, out, depth + 1)
            else:
                out.append(f"{path}:{type(child).__name__}")
    elif isinstance(value, list):
        path = f"{prefix}[]"
        if not value:
            out.append(f"{path}:empty")
        else:
            _walk(value[0], path, out, depth + 1)  # pyright: ignore[reportUnknownArgumentType]
    else:
        out.append(f"{prefix}:{type(value).__name__}")


@dataclass(frozen=True, slots=True)
class UnknownContentType:
    type: str
    shape_signature: str
    count: int = 0
    sample_session_id: str | None = None
    sample_message_id: str | None = None
    first_seen_at: int = 0
    last_seen_at: int = 0

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            type=as_str(row["type"]),
            shape_signature=as_str(row["shape_signature"]),
            count=as_int(row["count"]),
            sample_session_id=as_opt_str(row["sample_session_id"]),
            sample_message_id=as_opt_str(row["sample_message_id"]),
            first_seen_at=as_int(row["first_seen_at"]),
            last_seen_at=as_int(row["last_seen_at"]),
        )


async def note_unknown_content_type(
    db: Database,
    *,
    content_type: str,
    signature: str,
    session_id: str | None = None,
    message_id: str | None = None,
    at: int | None = None,
) -> int:
    """Count one unclassifiable message. Returns the new running count.

    The first sighting keeps the sample pointer; later ones only bump the
    counter, so the sample stays the oldest reproducible case.
    """
    stamp = now_ms() if at is None else at
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO unknown_content_types
                (type, shape_signature, count, sample_session_id,
                 sample_message_id, first_seen_at, last_seen_at)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, type, shape_signature) DO UPDATE SET
                count        = unknown_content_types.count + 1,
                last_seen_at = excluded.last_seen_at
            """,
            (content_type, signature, session_id, message_id, stamp, stamp),
        )
        return as_int(
            await db.fetch_val(
                """
                SELECT count FROM unknown_content_types
                 WHERE type = ? AND shape_signature = ?
                """,
                (content_type, signature),
                default=0,
            )
        )


async def list_unknown_content_types(
    db: Database, *, limit: int = 50
) -> list[UnknownContentType]:
    rows = await db.fetch_all(
        f"""
        SELECT {_UNKNOWN_COLUMNS} FROM unknown_content_types
         ORDER BY last_seen_at DESC
         LIMIT ?
        """,
        (max(1, limit),),
    )
    return [UnknownContentType.from_row(row) for row in rows]
