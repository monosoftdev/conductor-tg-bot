"""``chats`` — the routing table, keyed by ``(chat_id, thread_id)``.

The address of a prompt is the forum topic your thumb is in. ``thread_id`` is
NOT NULL and uses :data:`ctb.db.NO_THREAD_ID` (0) for "no topic" — a DM or the
supergroup's General — so the routing key never contains a NULL.

Rows are tenant-scoped by row-level security: ``tenant_id`` is filled from the
``ctb.tenant_id`` GUC and every statement here is filtered by it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo._util import (
    UNSET,
    Maybe,
    as_int,
    as_opt_int,
    as_opt_str,
    as_str,
    assign,
    update_sql,
)

__all__ = [
    "ChatKind",
    "ChatRow",
    "bind",
    "delete",
    "ensure",
    "for_session",
    "for_workspace",
    "get",
    "list_all",
    "list_bound",
    "set_defaults",
    "set_notify",
    "set_verbosity",
    "touch_prompt",
    "unbind",
    "update",
]

#: Mirrors the CHECK constraint on ``chats.kind``.
type ChatKind = str

_COLUMNS = """
    chat_id, thread_id, kind, workspace_id, session_id, default_project_id,
    default_branch, default_agent, default_model, default_effort, verbosity,
    notify, focus_until_at, last_prompt_at, created_at, updated_at
"""


@dataclass(frozen=True, slots=True)
class ChatRow:
    chat_id: int
    thread_id: int = NO_THREAD_ID
    kind: str = "topic"
    workspace_id: str | None = None
    session_id: str | None = None
    default_project_id: str | None = None
    default_branch: str | None = None
    default_agent: str | None = None
    default_model: str | None = None
    default_effort: str | None = None
    verbosity: str = "normal"
    notify: str = "quiet"
    focus_until_at: int | None = None
    last_prompt_at: int | None = None
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            chat_id=as_int(row["chat_id"]),
            thread_id=as_int(row["thread_id"]),
            kind=as_str(row["kind"], "topic"),
            workspace_id=as_opt_str(row["workspace_id"]),
            session_id=as_opt_str(row["session_id"]),
            default_project_id=as_opt_str(row["default_project_id"]),
            default_branch=as_opt_str(row["default_branch"]),
            default_agent=as_opt_str(row["default_agent"]),
            default_model=as_opt_str(row["default_model"]),
            default_effort=as_opt_str(row["default_effort"]),
            verbosity=as_str(row["verbosity"], "normal"),
            notify=as_str(row["notify"], "quiet"),
            focus_until_at=as_opt_int(row["focus_until_at"]),
            last_prompt_at=as_opt_int(row["last_prompt_at"]),
            created_at=as_int(row["created_at"]),
            updated_at=as_int(row["updated_at"]),
        )

    @property
    def key(self) -> tuple[int, int]:
        return (self.chat_id, self.thread_id)

    @property
    def is_bound(self) -> bool:
        return self.session_id is not None

    def is_focused(self, at: int) -> bool:
        """True while this chat is the one the user last prompted (loud window)."""
        return self.focus_until_at is not None and at < self.focus_until_at


async def get(
    db: Database, chat_id: int, thread_id: int = NO_THREAD_ID
) -> ChatRow | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM chats WHERE chat_id = ? AND thread_id = ?",
        (chat_id, thread_id),
    )
    return None if row is None else ChatRow.from_row(row)


async def ensure(
    db: Database,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    *,
    kind: ChatKind = "topic",
    at: int | None = None,
) -> ChatRow:
    """Get the row, creating it with defaults if this chat is new."""
    stamp = now_ms() if at is None else at
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO chats
                (chat_id, thread_id, kind, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (chat_id, thread_id, kind, stamp, stamp),
        )
        row = await get(db, chat_id, thread_id)
    if row is None:  # pragma: no cover - the insert above guarantees a row
        raise RuntimeError(f"chats row vanished for ({chat_id}, {thread_id})")
    return row


async def update(
    db: Database,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    *,
    kind: Maybe[str] = UNSET,
    workspace_id: Maybe[str | None] = UNSET,
    session_id: Maybe[str | None] = UNSET,
    default_project_id: Maybe[str | None] = UNSET,
    default_branch: Maybe[str | None] = UNSET,
    default_agent: Maybe[str | None] = UNSET,
    default_model: Maybe[str | None] = UNSET,
    default_effort: Maybe[str | None] = UNSET,
    verbosity: Maybe[str] = UNSET,
    notify: Maybe[str] = UNSET,
    focus_until_at: Maybe[int | None] = UNSET,
    last_prompt_at: Maybe[int | None] = UNSET,
    at: int | None = None,
) -> ChatRow | None:
    """Partial update. Omitted arguments are left alone; ``None`` writes NULL."""
    columns: dict[str, Any] = {}
    assign(
        columns,
        kind=kind,
        workspace_id=workspace_id,
        session_id=session_id,
        default_project_id=default_project_id,
        default_branch=default_branch,
        default_agent=default_agent,
        default_model=default_model,
        default_effort=default_effort,
        verbosity=verbosity,
        notify=notify,
        focus_until_at=focus_until_at,
        last_prompt_at=last_prompt_at,
    )
    if not columns:
        return await get(db, chat_id, thread_id)
    columns["updated_at"] = now_ms() if at is None else at
    sql = update_sql("chats", columns, "chat_id = ? AND thread_id = ?")
    async with db.transaction():
        await db.execute(sql, (*columns.values(), chat_id, thread_id))
        return await get(db, chat_id, thread_id)


async def bind(
    db: Database,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    *,
    workspace_id: str | None,
    session_id: str | None,
    kind: ChatKind = "topic",
    at: int | None = None,
) -> ChatRow:
    """Point a chat/topic at a workspace + session."""
    await ensure(db, chat_id, thread_id, kind=kind, at=at)
    row = await update(
        db,
        chat_id,
        thread_id,
        workspace_id=workspace_id,
        session_id=session_id,
        at=at,
    )
    if row is None:  # pragma: no cover - ensure() guarantees the row
        raise RuntimeError(f"chats row vanished for ({chat_id}, {thread_id})")
    return row


async def unbind(
    db: Database, chat_id: int, thread_id: int = NO_THREAD_ID, *, at: int | None = None
) -> ChatRow | None:
    """Detach the session (and workspace) without deleting remembered defaults."""
    return await update(
        db, chat_id, thread_id, workspace_id=None, session_id=None, at=at
    )


async def set_defaults(
    db: Database,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    *,
    project_id: Maybe[str | None] = UNSET,
    branch: Maybe[str | None] = UNSET,
    agent: Maybe[str | None] = UNSET,
    model: Maybe[str | None] = UNSET,
    effort: Maybe[str | None] = UNSET,
    at: int | None = None,
) -> ChatRow | None:
    """Remember what ``/new`` should pre-fill next time."""
    return await update(
        db,
        chat_id,
        thread_id,
        default_project_id=project_id,
        default_branch=branch,
        default_agent=agent,
        default_model=model,
        default_effort=effort,
        at=at,
    )


async def set_verbosity(
    db: Database, chat_id: int, thread_id: int = NO_THREAD_ID, *, verbosity: str
) -> ChatRow | None:
    return await update(db, chat_id, thread_id, verbosity=verbosity)


async def set_notify(
    db: Database, chat_id: int, thread_id: int = NO_THREAD_ID, *, notify: str
) -> ChatRow | None:
    return await update(db, chat_id, thread_id, notify=notify)


async def touch_prompt(
    db: Database,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    *,
    focus_for_ms: int = 0,
    at: int | None = None,
) -> ChatRow | None:
    """Stamp ``last_prompt_at`` and extend the focus window (the loud rule)."""
    stamp = now_ms() if at is None else at
    focus = UNSET if focus_for_ms <= 0 else stamp + focus_for_ms
    return await update(
        db, chat_id, thread_id, last_prompt_at=stamp, focus_until_at=focus, at=stamp
    )


async def list_all(db: Database) -> list[ChatRow]:
    rows = await db.fetch_all(
        f"SELECT {_COLUMNS} FROM chats ORDER BY chat_id, thread_id"
    )
    return [ChatRow.from_row(row) for row in rows]


async def list_bound(db: Database) -> list[ChatRow]:
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM chats
         WHERE session_id IS NOT NULL
         ORDER BY chat_id, thread_id
        """
    )
    return [ChatRow.from_row(row) for row in rows]


async def for_session(db: Database, session_id: str) -> list[ChatRow]:
    """Every chat/topic that mirrors this session. Usually exactly one."""
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM chats
         WHERE session_id = ? ORDER BY chat_id, thread_id
        """,
        (session_id,),
    )
    return [ChatRow.from_row(row) for row in rows]


async def for_workspace(db: Database, workspace_id: str) -> list[ChatRow]:
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM chats
         WHERE workspace_id = ?
         ORDER BY chat_id, thread_id
        """,
        (workspace_id,),
    )
    return [ChatRow.from_row(row) for row in rows]


async def delete(db: Database, chat_id: int, thread_id: int = NO_THREAD_ID) -> bool:
    changed = await db.execute(
        "DELETE FROM chats WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id)
    )
    return changed > 0
