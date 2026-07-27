"""``workspaces`` — local cache of the Conductor resource plus its forum topic.

``POST /v0/workspaces`` is the one create in this API with **no** idempotency
key, so it is never blind-retried. Instead a nonce is embedded in the generated
workspace name (``tg-<chatid>-<nonce>``); after an ambiguous create the caller
lists the project's workspaces, finds the name carrying the nonce, and calls
:func:`upsert` with the real id. :func:`get_by_nonce` closes that loop locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from ctb.conductor.models import WorkspaceStatusValue
from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo._util import (
    UNSET,
    Maybe,
    as_int,
    as_opt_int,
    as_opt_str,
    assign,
    update_sql,
)

__all__ = [
    "WorkspaceRow",
    "bind_topic",
    "delete",
    "get",
    "get_by_nonce",
    "get_by_topic",
    "list_all",
    "list_for_project",
    "mark_archived",
    "set_topic_marker",
    "unbind_topic",
    "update",
    "update_status",
    "upsert",
]

_COLUMNS = """
    id, project_id, name, repo_url, branch, agent, model, effort, deep_link,
    status, lifecycle_step, last_status_at, chat_id, topic_id, topic_name,
    topic_marker, create_nonce, created_at, updated_at, init_started_at,
    ready_at, archived_at
"""


@dataclass(frozen=True, slots=True)
class WorkspaceRow:
    id: str
    project_id: str | None = None
    name: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    deep_link: str | None = None
    status: str | None = None
    lifecycle_step: str | None = None
    last_status_at: int | None = None
    chat_id: int | None = None
    topic_id: int | None = None
    topic_name: str | None = None
    topic_marker: str | None = None
    create_nonce: str | None = None
    created_at: int = 0
    updated_at: int = 0
    init_started_at: int | None = None
    ready_at: int | None = None
    archived_at: int | None = None

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            id=str(row["id"]),
            project_id=as_opt_str(row["project_id"]),
            name=as_opt_str(row["name"]),
            repo_url=as_opt_str(row["repo_url"]),
            branch=as_opt_str(row["branch"]),
            agent=as_opt_str(row["agent"]),
            model=as_opt_str(row["model"]),
            effort=as_opt_str(row["effort"]),
            deep_link=as_opt_str(row["deep_link"]),
            status=as_opt_str(row["status"]),
            lifecycle_step=as_opt_str(row["lifecycle_step"]),
            last_status_at=as_opt_int(row["last_status_at"]),
            chat_id=as_opt_int(row["chat_id"]),
            topic_id=as_opt_int(row["topic_id"]),
            topic_name=as_opt_str(row["topic_name"]),
            topic_marker=as_opt_str(row["topic_marker"]),
            create_nonce=as_opt_str(row["create_nonce"]),
            created_at=as_int(row["created_at"]),
            updated_at=as_int(row["updated_at"]),
            init_started_at=as_opt_int(row["init_started_at"]),
            ready_at=as_opt_int(row["ready_at"]),
            archived_at=as_opt_int(row["archived_at"]),
        )

    @property
    def status_value(self) -> WorkspaceStatusValue:
        """The cached status as the enum. Unknown/absent coerces to ``UNKNOWN``."""
        if self.status is None:
            return WorkspaceStatusValue.UNKNOWN
        try:
            return WorkspaceStatusValue(self.status)
        except ValueError:
            return WorkspaceStatusValue.UNKNOWN

    @property
    def has_topic(self) -> bool:
        return self.chat_id is not None and self.topic_id is not None


async def get(db: Database, workspace_id: str) -> WorkspaceRow | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM workspaces WHERE id = ?", (workspace_id,)
    )
    return None if row is None else WorkspaceRow.from_row(row)


async def get_by_nonce(db: Database, create_nonce: str) -> WorkspaceRow | None:
    """Reconcile an ambiguous ``POST /v0/workspaces`` against what we already saw."""
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM workspaces WHERE create_nonce = ?", (create_nonce,)
    )
    return None if row is None else WorkspaceRow.from_row(row)


async def get_by_topic(
    db: Database, chat_id: int, topic_id: int
) -> WorkspaceRow | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM workspaces WHERE chat_id = ? AND topic_id = ?",
        (chat_id, topic_id),
    )
    return None if row is None else WorkspaceRow.from_row(row)


async def upsert(
    db: Database,
    workspace_id: str,
    *,
    project_id: Maybe[str | None] = UNSET,
    name: Maybe[str | None] = UNSET,
    repo_url: Maybe[str | None] = UNSET,
    branch: Maybe[str | None] = UNSET,
    agent: Maybe[str | None] = UNSET,
    model: Maybe[str | None] = UNSET,
    effort: Maybe[str | None] = UNSET,
    deep_link: Maybe[str | None] = UNSET,
    status: Maybe[str | None] = UNSET,
    lifecycle_step: Maybe[str | None] = UNSET,
    create_nonce: Maybe[str | None] = UNSET,
    chat_id: Maybe[int | None] = UNSET,
    topic_id: Maybe[int | None] = UNSET,
    topic_name: Maybe[str | None] = UNSET,
    at: int | None = None,
) -> WorkspaceRow:
    """Insert or refresh the cached workspace. Only named columns are touched."""
    stamp = now_ms() if at is None else at
    columns: dict[str, Any] = {}
    assign(
        columns,
        project_id=project_id,
        name=name,
        repo_url=repo_url,
        branch=branch,
        agent=agent,
        model=model,
        effort=effort,
        deep_link=deep_link,
        status=status,
        lifecycle_step=lifecycle_step,
        create_nonce=create_nonce,
        chat_id=chat_id,
        topic_id=topic_id,
        topic_name=topic_name,
    )
    async with db.transaction():
        await db.execute(
            """
            INSERT INTO workspaces (id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (workspace_id, stamp, stamp),
        )
        if columns:
            columns["updated_at"] = stamp
            await db.execute(
                update_sql("workspaces", columns, "id = ?"),
                (*columns.values(), workspace_id),
            )
        row = await get(db, workspace_id)
    if row is None:  # pragma: no cover - the insert above guarantees a row
        raise RuntimeError(f"workspaces row vanished for {workspace_id}")
    return row


async def update(
    db: Database, workspace_id: str, *, at: int | None = None, **columns: Any
) -> WorkspaceRow | None:
    """Escape hatch for a raw column update. Prefer the named helpers."""
    if not columns:
        return await get(db, workspace_id)
    columns["updated_at"] = now_ms() if at is None else at
    async with db.transaction():
        await db.execute(
            update_sql("workspaces", columns, "id = ?"),
            (*columns.values(), workspace_id),
        )
        return await get(db, workspace_id)


async def update_status(
    db: Database,
    workspace_id: str,
    *,
    status: str | WorkspaceStatusValue,
    lifecycle_step: str | None = None,
    at: int | None = None,
) -> WorkspaceRow | None:
    """Record a ``GET /v0/workspaces/{id}/status`` observation.

    ``ready_at`` / ``init_started_at`` / ``archived_at`` are stamped the first
    time the corresponding status is seen, so a later re-observation does not
    keep moving them.
    """
    stamp = now_ms() if at is None else at
    value = str(status)
    columns: dict[str, Any] = {
        "status": value,
        "lifecycle_step": lifecycle_step,
        "last_status_at": stamp,
        "updated_at": stamp,
    }
    async with db.transaction():
        current = await get(db, workspace_id)
        if current is None:
            return None
        if (
            value == WorkspaceStatusValue.INITIALIZING
            and current.init_started_at is None
        ):
            columns["init_started_at"] = stamp
        if value == WorkspaceStatusValue.READY and current.ready_at is None:
            columns["ready_at"] = stamp
        if (
            value in (WorkspaceStatusValue.ARCHIVED, WorkspaceStatusValue.DELETED)
            and current.archived_at is None
        ):
            columns["archived_at"] = stamp
        await db.execute(
            update_sql("workspaces", columns, "id = ?"),
            (*columns.values(), workspace_id),
        )
        return await get(db, workspace_id)


async def bind_topic(
    db: Database,
    workspace_id: str,
    *,
    chat_id: int,
    topic_id: int,
    topic_name: str | None = None,
    at: int | None = None,
) -> WorkspaceRow | None:
    return await update(
        db,
        workspace_id,
        at=at,
        chat_id=chat_id,
        topic_id=topic_id,
        topic_name=topic_name,
    )


async def unbind_topic(
    db: Database, workspace_id: str, *, at: int | None = None
) -> WorkspaceRow | None:
    return await update(db, workspace_id, at=at, topic_id=None, topic_marker=None)


async def set_topic_marker(
    db: Database, workspace_id: str, marker: str, *, at: int | None = None
) -> WorkspaceRow | None:
    """Remember the last applied topic-name prefix so renames stay idempotent."""
    return await update(db, workspace_id, at=at, topic_marker=marker)


async def mark_archived(
    db: Database, workspace_id: str, *, at: int | None = None
) -> WorkspaceRow | None:
    stamp = now_ms() if at is None else at
    return await update(
        db,
        workspace_id,
        at=stamp,
        status=str(WorkspaceStatusValue.ARCHIVED),
        archived_at=stamp,
    )


async def list_all(
    db: Database, *, include_archived: bool = False
) -> list[WorkspaceRow]:
    where = "" if include_archived else "WHERE archived_at IS NULL"
    rows = await db.fetch_all(
        f"SELECT {_COLUMNS} FROM workspaces {where} ORDER BY created_at DESC"
    )
    return [WorkspaceRow.from_row(row) for row in rows]


async def list_for_project(db: Database, project_id: str) -> list[WorkspaceRow]:
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM workspaces
         WHERE project_id = ?
         ORDER BY created_at DESC
        """,
        (project_id,),
    )
    return [WorkspaceRow.from_row(row) for row in rows]


async def delete(db: Database, workspace_id: str) -> bool:
    """Drop the cache row. Sessions cascade; chats fall back to unbound."""
    changed = await db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    return changed > 0
