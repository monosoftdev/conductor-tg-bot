"""``tenants``, ``tenant_members``, ``tenant_chats``, ``enrollment_tokens``.

These four tables *decide* scope, so they are not themselves scoped by it: they
have no row-level security and are read through the system pool. Everything
else in :mod:`ctb.db.repo` is filtered by the ``ctb.tenant_id`` GUC that this
module's lookups establish.

Three facts shape the model:

* **A Telegram chat belongs to exactly one tenant.** ``tenant_chats.chat_id`` is
  the primary key, so resolution is one point lookup with no ambiguity — and
  every other table's ``chat_id`` is therefore already tenant-unique.
* **Many Telegram users drive one tenant.** A co-founder is a ``tenant_members``
  row, not a second deployment.
* **Secrets are sealed, never stored in the clear.** The ``*_ct`` columns hold
  :class:`ctb.crypto.SecretBox` blobs. This module moves those bytes; it never
  opens them. Only :class:`ctb.conductor.pool.ClientPool` decrypts, and only
  into a client it is about to build.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final, Self

from ctb.db.connection import Database, Row, now_ms
from ctb.db.repo._util import (
    UNSET,
    Maybe,
    as_bool,
    as_int,
    as_opt_int,
    as_opt_str,
    as_str,
    assign,
    update_sql,
)

__all__ = [
    "AUTH_RETRY_AFTER_MS",
    "ROLE_ORDER",
    "RoleError",
    "TenantChat",
    "TenantMember",
    "TenantRow",
    "TenantStatus",
    "add_member",
    "bind_chat",
    "chat_for",
    "consume_enrollment_token",
    "create",
    "created_since",
    "create_enrollment_token",
    "delete_tenant",
    "demote_chat",
    "get",
    "get_by_slug",
    "list_active",
    "list_chats",
    "list_members",
    "list_owner_ids",
    "mark_auth_failed",
    "member",
    "memberships_for_user",
    "primary_chat",
    "prune_enrollment_tokens",
    "rebind_chat",
    "remove_member",
    "set_conductor_key",
    "set_elevenlabs_key",
    "set_github_key",
    "set_status",
    "unbind_chat",
    "update_defaults",
]

#: Ascending authority. ``admin`` and ``owner`` both pass ``is_owner`` checks;
#: only ``owner`` may delete the tenant or remove another owner.
ROLE_ORDER: Final[dict[str, int]] = {"member": 0, "admin": 1, "owner": 2}

#: How long ``auth_failed_at`` keeps a tenant dark before one poller is let
#: back through to ask again.
#:
#: The latch used to have no clock at all: ``sessions.list_bound`` dropped any
#: tenant with a stamp, and only ``set_conductor_key`` ever cleared it. So a
#: *single* 401 — one Conductor proxy answered 401 in the middle of an upstream
#: wobble, sixty seconds apart, for two unrelated teams — stopped every poller
#: those teams had, permanently, while the same key kept returning 200 to every
#: command the bot ran by hand. Nothing short of re-sending ``/key`` brought
#: them back, and nothing in the chat said which key to re-send or why.
#:
#: Fifteen minutes is chosen against the thing the no-retry rule exists to
#: avoid: one probe per quarter hour cannot look like a credential-stuffing
#: loop to any rate limiter, and it bounds a false latch to an outage a person
#: waits out rather than one they have to diagnose.
AUTH_RETRY_AFTER_MS: Final[int] = 15 * 60 * 1000

type TenantStatus = str  # 'pending' | 'active' | 'suspended' | 'deleted'

# GitHub/CI arrived in migration 002.  A rolling deploy can briefly run this
# code against a database that still has only 001, and CI is optional, so the
# four new fields must not make the core tenant lookup fail.  Reading through
# the row's JSON representation returns NULL for a column that is not present.
# The bytea value is represented as ``\x<hex>`` and decoded back to the exact
# sealed bytes when the column does exist.
#
# Keep this compatibility projection until migration 002 is old enough that no
# supported deployment can still be on 001.  It is deliberately read-only:
# storing a GitHub token still requires 002.
_TENANT_COLUMNS = r"""
    id, slug, name, status, conductor_api_url, conductor_key_ct,
    conductor_key_kid, conductor_key_fp, conductor_key_at, conductor_key_by,
    elevenlabs_key_ct, elevenlabs_key_kid, elevenlabs_key_fp, elevenlabs_key_at,
    CASE
        WHEN to_jsonb(tenants) ->> 'github_key_ct' IS NULL THEN NULL::bytea
        ELSE decode(substr(to_jsonb(tenants) ->> 'github_key_ct', 3), 'hex')
    END AS github_key_ct,
    to_jsonb(tenants) ->> 'github_key_kid' AS github_key_kid,
    to_jsonb(tenants) ->> 'github_key_fp' AS github_key_fp,
    (to_jsonb(tenants) ->> 'github_key_at')::bigint AS github_key_at,
    default_agent, default_model, default_effort, default_branch, voice_enabled,
    voice_mode, max_pollers, max_pending_deliveries, max_workspaces,
    auth_failed_at, auth_failed_reason, created_at, updated_at, suspended_at,
    deleted_at
"""

_MEMBER_COLUMNS = "tenant_id, user_id, role, username, note, added_by, added_at"

_CHAT_COLUMNS = (
    "chat_id, tenant_id, kind, is_primary, title, bound_by, bound_at, verified_at"
)


@dataclass(frozen=True, slots=True)
class TenantRow:
    """One customer. Everything a request needs, minus the plaintext keys."""

    id: uuid.UUID
    slug: str
    name: str
    status: TenantStatus = "pending"
    conductor_api_url: str = "https://api.conductor.build/v0"
    conductor_key_ct: bytes | None = None
    conductor_key_kid: str | None = None
    conductor_key_fp: str | None = None
    conductor_key_at: int | None = None
    conductor_key_by: int | None = None
    elevenlabs_key_ct: bytes | None = None
    elevenlabs_key_kid: str | None = None
    elevenlabs_key_fp: str | None = None
    elevenlabs_key_at: int | None = None
    github_key_ct: bytes | None = None
    github_key_kid: str | None = None
    github_key_fp: str | None = None
    github_key_at: int | None = None
    #: An **override** of the platform's ``DEFAULT_*`` env vars, or ``None`` to
    #: follow them. Nothing in the bot sets these; they exist so an operator can
    #: pin one tenant against the deployment-wide default (migration 004).
    default_agent: str | None = None
    default_model: str | None = None
    default_effort: str | None = None
    default_branch: str | None = None
    voice_enabled: bool = False
    voice_mode: str = "prompts"
    max_pollers: int = 20
    max_pending_deliveries: int = 5000
    max_workspaces: int = 50
    auth_failed_at: int | None = None
    auth_failed_reason: str | None = None
    created_at: int = 0
    updated_at: int = 0
    suspended_at: int | None = None
    deleted_at: int | None = None

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            id=row["id"],
            slug=as_str(row["slug"]),
            name=as_str(row["name"]),
            status=as_str(row["status"], "pending"),
            conductor_api_url=as_str(row["conductor_api_url"]),
            conductor_key_ct=_as_bytes(row["conductor_key_ct"]),
            conductor_key_kid=as_opt_str(row["conductor_key_kid"]),
            conductor_key_fp=as_opt_str(row["conductor_key_fp"]),
            conductor_key_at=as_opt_int(row["conductor_key_at"]),
            conductor_key_by=as_opt_int(row["conductor_key_by"]),
            elevenlabs_key_ct=_as_bytes(row["elevenlabs_key_ct"]),
            elevenlabs_key_kid=as_opt_str(row["elevenlabs_key_kid"]),
            elevenlabs_key_fp=as_opt_str(row["elevenlabs_key_fp"]),
            elevenlabs_key_at=as_opt_int(row["elevenlabs_key_at"]),
            github_key_ct=_as_bytes(row["github_key_ct"]),
            github_key_kid=as_opt_str(row["github_key_kid"]),
            github_key_fp=as_opt_str(row["github_key_fp"]),
            github_key_at=as_opt_int(row["github_key_at"]),
            default_agent=as_opt_str(row["default_agent"]),
            default_model=as_opt_str(row["default_model"]),
            default_effort=as_opt_str(row["default_effort"]),
            default_branch=as_opt_str(row["default_branch"]),
            voice_enabled=as_bool(row["voice_enabled"]),
            voice_mode=as_str(row["voice_mode"], "prompts"),
            max_pollers=as_int(row["max_pollers"], 20),
            max_pending_deliveries=as_int(row["max_pending_deliveries"], 5000),
            max_workspaces=as_int(row["max_workspaces"], 50),
            auth_failed_at=as_opt_int(row["auth_failed_at"]),
            auth_failed_reason=as_opt_str(row["auth_failed_reason"]),
            created_at=as_int(row["created_at"]),
            updated_at=as_int(row["updated_at"]),
            suspended_at=as_opt_int(row["suspended_at"]),
            deleted_at=as_opt_int(row["deleted_at"]),
        )

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def has_conductor_key(self) -> bool:
        return self.conductor_key_ct is not None

    def __repr__(self) -> str:
        """Never render the sealed blobs, even in a traceback."""
        return f"TenantRow(slug={self.slug!r}, status={self.status!r})"


@dataclass(frozen=True, slots=True)
class TenantMember:
    tenant_id: uuid.UUID
    user_id: int
    role: str = "member"
    username: str | None = None
    note: str | None = None
    added_by: int | None = None
    added_at: int = 0

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            tenant_id=row["tenant_id"],
            user_id=as_int(row["user_id"]),
            role=as_str(row["role"], "member"),
            username=as_opt_str(row["username"]),
            note=as_opt_str(row["note"]),
            added_by=as_opt_int(row["added_by"]),
            added_at=as_int(row["added_at"]),
        )

    @property
    def is_owner(self) -> bool:
        """Owners *and* admins pass the owner-only command gate."""
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER["admin"]

    @property
    def is_sole_owner_role(self) -> bool:
        return self.role == "owner"


@dataclass(frozen=True, slots=True)
class TenantChat:
    chat_id: int
    tenant_id: uuid.UUID
    kind: str = "group"
    is_primary: bool = False
    title: str | None = None
    bound_by: int | None = None
    bound_at: int = 0
    verified_at: int | None = None

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            chat_id=as_int(row["chat_id"]),
            tenant_id=row["tenant_id"],
            kind=as_str(row["kind"], "group"),
            is_primary=as_bool(row["is_primary"]),
            title=as_opt_str(row["title"]),
            bound_by=as_opt_int(row["bound_by"]),
            bound_at=as_int(row["bound_at"]),
            verified_at=as_opt_int(row["verified_at"]),
        )


def _as_bytes(value: Any) -> bytes | None:
    if value is None:
        return None
    return bytes(value)


# ── tenants ──────────────────────────────────────────────────────────────────


async def created_since(db: Database, *, since_ms: int) -> int:
    """How many workspaces were registered after ``since_ms``.

    The sign-up rate limit. Registration is open by default and every
    ``/register`` is an unauthenticated INSERT, so without a bound a script with
    a pool of Telegram accounts can fill the table overnight — and nothing
    prunes an abandoned `pending` tenant.
    """
    return as_int(
        await db.fetch_val(
            "SELECT COUNT(*) FROM tenants WHERE created_at >= ?",
            (since_ms,),
            default=0,
        )
    )


async def create(
    db: Database,
    *,
    slug: str,
    name: str,
    owner_user_id: int,
    status: TenantStatus = "pending",
    conductor_api_url: str | None = None,
    at: int | None = None,
) -> TenantRow:
    """Create a tenant and seat its first owner, atomically."""
    stamp = now_ms() if at is None else at
    async with db.transaction():
        row = await db.fetch_one(
            f"""
            INSERT INTO tenants (slug, name, status, conductor_api_url,
                                 created_at, updated_at)
            VALUES (?, ?, ?, COALESCE(?, 'https://api.conductor.build/v0'), ?, ?)
            RETURNING {_TENANT_COLUMNS}
            """,
            (slug, name, status, conductor_api_url, stamp, stamp),
        )
        if row is None:  # pragma: no cover - RETURNING always yields a row
            raise RuntimeError("tenant insert returned nothing")
        tenant = TenantRow.from_row(row)
        await add_member(
            db,
            tenant.id,
            owner_user_id,
            role="owner",
            added_by=owner_user_id,
            at=stamp,
        )
    return tenant


async def get(db: Database, tenant_id: uuid.UUID) -> TenantRow | None:
    row = await db.fetch_one(
        f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE id = ?", (tenant_id,)
    )
    return None if row is None else TenantRow.from_row(row)


async def get_by_slug(db: Database, slug: str) -> TenantRow | None:
    row = await db.fetch_one(
        f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE slug = ?", (slug,)
    )
    return None if row is None else TenantRow.from_row(row)


async def list_active(db: Database) -> list[TenantRow]:
    rows = await db.fetch_all(
        f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE status = 'active' "
        "ORDER BY created_at"
    )
    return [TenantRow.from_row(row) for row in rows]


async def set_status(
    db: Database, tenant_id: uuid.UUID, status: TenantStatus, *, at: int | None = None
) -> TenantRow | None:
    """Suspend, activate, or soft-delete.

    Suspension is enforced by ``sessions.list_bound``'s join, so it takes effect
    on the next reconcile pass with no code path involved.
    """
    stamp = now_ms() if at is None else at
    columns: dict[str, Any] = {"status": status, "updated_at": stamp}
    if status == "suspended":
        columns["suspended_at"] = stamp
    elif status == "deleted":
        columns["deleted_at"] = stamp
    elif status == "active":
        columns["suspended_at"] = None
    await db.execute(
        update_sql("tenants", columns, "id = ?"), (*columns.values(), tenant_id)
    )
    return await get(db, tenant_id)


async def update_defaults(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    name: Maybe[str] = UNSET,
    default_agent: Maybe[str | None] = UNSET,
    default_model: Maybe[str | None] = UNSET,
    default_effort: Maybe[str | None] = UNSET,
    default_branch: Maybe[str | None] = UNSET,
    voice_enabled: Maybe[bool] = UNSET,
    voice_mode: Maybe[str] = UNSET,
    conductor_api_url: Maybe[str] = UNSET,
    at: int | None = None,
) -> TenantRow | None:
    stamp = now_ms() if at is None else at
    columns: dict[str, Any] = {"updated_at": stamp}
    assign(
        columns,
        name=name,
        default_agent=default_agent,
        default_model=default_model,
        default_effort=default_effort,
        default_branch=default_branch,
        voice_enabled=voice_enabled,
        voice_mode=voice_mode,
        conductor_api_url=conductor_api_url,
    )
    await db.execute(
        update_sql("tenants", columns, "id = ?"), (*columns.values(), tenant_id)
    )
    return await get(db, tenant_id)


async def set_conductor_key(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    ciphertext: bytes | None,
    kid: str | None,
    fingerprint: str | None,
    by_user_id: int | None = None,
    at: int | None = None,
) -> TenantRow | None:
    """Store (or, with ``ciphertext=None``, revoke) the sealed Conductor key.

    Clears ``auth_failed_at`` so a tenant that was stopped by a rejected key
    starts polling again on the next reconcile pass.
    """
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE tenants
           SET conductor_key_ct = ?, conductor_key_kid = ?, conductor_key_fp = ?,
               conductor_key_at = ?, conductor_key_by = ?,
               auth_failed_at = NULL, auth_failed_reason = NULL, updated_at = ?
         WHERE id = ?
        """,
        (
            ciphertext,
            kid,
            fingerprint,
            None if ciphertext is None else stamp,
            by_user_id,
            stamp,
            tenant_id,
        ),
    )
    return await get(db, tenant_id)


async def set_elevenlabs_key(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    ciphertext: bytes | None,
    kid: str | None,
    fingerprint: str | None,
    at: int | None = None,
) -> TenantRow | None:
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE tenants
           SET elevenlabs_key_ct = ?, elevenlabs_key_kid = ?,
               elevenlabs_key_fp = ?, elevenlabs_key_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            ciphertext,
            kid,
            fingerprint,
            None if ciphertext is None else stamp,
            stamp,
            tenant_id,
        ),
    )
    return await get(db, tenant_id)


async def set_github_key(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    ciphertext: bytes | None,
    kid: str | None,
    fingerprint: str | None,
    at: int | None = None,
) -> TenantRow | None:
    """Store (or, with ``ciphertext=None``, revoke) this team's GitHub token."""
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE tenants
           SET github_key_ct = ?, github_key_kid = ?,
               github_key_fp = ?, github_key_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            ciphertext,
            kid,
            fingerprint,
            None if ciphertext is None else stamp,
            stamp,
            tenant_id,
        ),
    )
    return await get(db, tenant_id)


async def mark_auth_failed(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    reason: str | None = None,
    at: int | None = None,
) -> None:
    """Pause this tenant's pollers for :data:`AUTH_RETRY_AFTER_MS`.

    Cleared early by :func:`set_conductor_key`; otherwise it expires on its own
    and ``sessions.list_bound`` lets one poller back through to ask again.

    The stamp is **refreshed**, not kept. It used to carry ``AND auth_failed_at
    IS NULL`` so the first rejection's time survived — which was free while the
    latch was permanent and is a bug now that the same column is the clock: a
    stamp that never moves forward is a window that never closes, so a genuinely
    revoked key would be re-probed on every pass instead of every fifteen
    minutes. Each fresh rejection restarts the wait.
    """
    stamp = now_ms() if at is None else at
    await db.execute(
        """
        UPDATE tenants
           SET auth_failed_at = ?, auth_failed_reason = ?, updated_at = ?
         WHERE id = ?
        """,
        (stamp, reason, stamp, tenant_id),
    )


async def delete_tenant(db: Database, tenant_id: uuid.UUID) -> bool:
    """Hard delete. Every tenant-scoped table cascades from here."""
    return await db.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,)) > 0


# ── members ──────────────────────────────────────────────────────────────────


class RoleError(RuntimeError):
    """A role change that would leave the workspace unadministerable."""


async def add_member(
    db: Database,
    tenant_id: uuid.UUID,
    user_id: int,
    *,
    role: str = "member",
    username: str | None = None,
    note: str | None = None,
    added_by: int | None = None,
    at: int | None = None,
) -> TenantMember:
    """Seat a user, or update their role. Idempotent.

    **Only an owner may change an owner's role.** Admins pass the same
    ``is_owner`` command gate as owners, so without this an admin could demote
    the owner to ``member`` — and nothing in Telegram grants ``owner`` back.

    **The last owner may not be demoted, not even by themselves.** A workspace
    with zero owners is unadministrable *and* undeletable: every command that
    could fix it is gated on ``is_owner``, and ``/forget`` needs the role
    exactly. One mistyped ``/invite <my own id> member`` would strand the
    tenant, its sealed key and its transcripts forever.
    """
    stamp = now_ms() if at is None else at
    if role != "owner":
        existing = await member(db, tenant_id, user_id)
        if existing is not None and existing.role == "owner":
            actor = None if added_by is None else await member(db, tenant_id, added_by)
            if actor is None or actor.role != "owner":
                raise RoleError("only an owner can change an owner's role")
            if len(await list_owner_ids(db, tenant_id)) <= 1:
                raise RoleError(
                    "that is the only owner; promote someone else to owner first"
                )
    row = await db.fetch_one(
        f"""
        INSERT INTO tenant_members
            (tenant_id, user_id, role, username, note, added_by, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tenant_id, user_id) DO UPDATE SET
            role     = excluded.role,
            username = COALESCE(excluded.username, tenant_members.username),
            note     = COALESCE(excluded.note, tenant_members.note)
        RETURNING {_MEMBER_COLUMNS}
        """,
        (tenant_id, user_id, role, username, note, added_by, stamp),
    )
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise RuntimeError("tenant_members upsert returned nothing")
    return TenantMember.from_row(row)


async def member(
    db: Database, tenant_id: uuid.UUID, user_id: int
) -> TenantMember | None:
    row = await db.fetch_one(
        f"SELECT {_MEMBER_COLUMNS} FROM tenant_members "
        "WHERE tenant_id = ? AND user_id = ?",
        (tenant_id, user_id),
    )
    return None if row is None else TenantMember.from_row(row)


async def memberships_for_user(db: Database, user_id: int) -> list[TenantMember]:
    """Every tenant this user may drive, oldest seat first."""
    rows = await db.fetch_all(
        f"SELECT {_MEMBER_COLUMNS} FROM tenant_members "
        "WHERE user_id = ? ORDER BY added_at",
        (user_id,),
    )
    return [TenantMember.from_row(row) for row in rows]


async def list_members(db: Database, tenant_id: uuid.UUID) -> list[TenantMember]:
    rows = await db.fetch_all(
        f"SELECT {_MEMBER_COLUMNS} FROM tenant_members "
        "WHERE tenant_id = ? ORDER BY added_at",
        (tenant_id,),
    )
    return [TenantMember.from_row(row) for row in rows]


async def list_owner_ids(db: Database, tenant_id: uuid.UUID) -> tuple[int, ...]:
    """Who gets the alerts. Owners first, then admins, then oldest seat."""
    rows = await db.fetch_all(
        """
        SELECT user_id FROM tenant_members
         WHERE tenant_id = ? AND role IN ('owner', 'admin')
         ORDER BY CASE WHEN role = 'owner' THEN 0 ELSE 1 END, added_at
        """,
        (tenant_id,),
    )
    return tuple(as_int(row["user_id"]) for row in rows)


async def remove_member(
    db: Database,
    tenant_id: uuid.UUID,
    user_id: int,
    *,
    removed_by: int | None = None,
) -> bool:
    """Unseat a user.

    Two guards, and the second is the one that matters: the last **owner**
    cannot be removed, where owner means the ``owner`` role specifically and
    not the admins who merely pass the same command gate. Counting admins would
    let an admin remove the owner and leave a workspace nobody can administer.
    """
    async with db.transaction():
        current = await member(db, tenant_id, user_id)
        if current is None:
            return False
        if current.role == "owner":
            actor = (
                None if removed_by is None else await member(db, tenant_id, removed_by)
            )
            if removed_by is not None and (actor is None or actor.role != "owner"):
                return False
            remaining = [
                row
                for row in await list_members(db, tenant_id)
                if row.role == "owner" and row.user_id != user_id
            ]
            if not remaining:
                return False
        changed = await db.execute(
            "DELETE FROM tenant_members WHERE tenant_id = ? AND user_id = ?",
            (tenant_id, user_id),
        )
    return changed > 0


# ── chats ────────────────────────────────────────────────────────────────────


async def chat_for(db: Database, chat_id: int) -> TenantChat | None:
    """The tenancy of one Telegram chat. This is the resolution hot path."""
    row = await db.fetch_one(
        f"SELECT {_CHAT_COLUMNS} FROM tenant_chats WHERE chat_id = ?", (chat_id,)
    )
    return None if row is None else TenantChat.from_row(row)


async def bind_chat(
    db: Database,
    chat_id: int,
    tenant_id: uuid.UUID,
    *,
    kind: str = "group",
    is_primary: bool = False,
    title: str | None = None,
    bound_by: int | None = None,
    verified: bool = False,
    at: int | None = None,
) -> TenantChat:
    """Attach a chat to a tenant.

    Re-binding a chat that already belongs to a *different* tenant is refused:
    a shared bot means anyone can add it to a group, and silently re-homing a
    chat would move one tenant's topics under another's key.
    """
    stamp = now_ms() if at is None else at
    async with db.transaction():
        existing = await chat_for(db, chat_id)
        if existing is not None and existing.tenant_id != tenant_id:
            raise ValueError(
                f"chat {chat_id} is already bound to another tenant; "
                "unbind it there first"
            )
        if is_primary:
            await db.execute(
                "UPDATE tenant_chats SET is_primary = false "
                "WHERE tenant_id = ? AND chat_id <> ?",
                (tenant_id, chat_id),
            )
        row = await db.fetch_one(
            f"""
            INSERT INTO tenant_chats
                (chat_id, tenant_id, kind, is_primary, title, bound_by, bound_at,
                 verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id) DO UPDATE SET
                kind        = excluded.kind,
                is_primary  = excluded.is_primary,
                title       = COALESCE(excluded.title, tenant_chats.title),
                verified_at = COALESCE(excluded.verified_at,
                                       tenant_chats.verified_at)
            RETURNING {_CHAT_COLUMNS}
            """,
            (
                chat_id,
                tenant_id,
                kind,
                is_primary,
                title,
                bound_by,
                stamp,
                stamp if verified else None,
            ),
        )
    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise RuntimeError("tenant_chats upsert returned nothing")
    return TenantChat.from_row(row)


async def unbind_chat(db: Database, chat_id: int) -> bool:
    return (
        await db.execute("DELETE FROM tenant_chats WHERE chat_id = ?", (chat_id,)) > 0
    )


async def demote_chat(db: Database, chat_id: int) -> bool:
    """Stop treating this chat as the team's, without forgetting it exists.

    ``is_primary`` is read in exactly one place — :func:`primary_chat`, which
    :meth:`~ctb.bot.actions.BotActionSink._notice_targets` adds to every alarm —
    so a group the bot can no longer reach makes each of those a permanent
    ``failed`` delivery. Nothing is lost (the owners' DMs are separate targets)
    and nothing self-corrects: the same alarm fails again next episode, and
    ``/health``'s failure digest reports a fault that no longer has a cure.

    Deliberately not :func:`unbind_chat`. The tenancy of a chat is what
    ``/setup`` established, and deleting it on a Telegram error would re-open
    the group to being claimed by another team. This only withdraws the
    *nomination*; ``/setup`` restores it in one command.
    """
    return (
        await db.execute(
            "UPDATE tenant_chats SET is_primary = false WHERE chat_id = ? "
            "AND is_primary",
            (chat_id,),
        )
        > 0
    )


async def rebind_chat(
    db: Database,
    chat_id: int,
    tenant_id: uuid.UUID,
    *,
    kind: str = "dm",
    title: str | None = None,
    bound_by: int | None = None,
    at: int | None = None,
) -> TenantChat:
    """Move a private chat to another tenant, on purpose.

    :func:`bind_chat` refuses this, and rightly: a shared bot means anyone can
    add it to a group, so silently re-homing one would move a customer's topics
    under somebody else's key. ``/use`` is the opposite situation — one person
    choosing, in their own private chat, which of the workspaces *they are
    already in* their DMs mean. Without a way to say that, ``/use`` binds once
    and then raises forever.

    The caller must have verified membership of ``tenant_id``. Deliberately
    refuses a group: re-homing a shared chat is never one person's decision.
    """
    if kind != "dm":
        raise ValueError("rebind_chat is for private chats only")
    stamp = now_ms() if at is None else at
    async with db.transaction():
        await unbind_chat(db, chat_id)
        return await bind_chat(
            db,
            chat_id,
            tenant_id,
            kind=kind,
            title=title,
            bound_by=bound_by,
            at=stamp,
        )


async def list_chats(db: Database, tenant_id: uuid.UUID) -> list[TenantChat]:
    rows = await db.fetch_all(
        f"SELECT {_CHAT_COLUMNS} FROM tenant_chats WHERE tenant_id = ? "
        "ORDER BY is_primary DESC, bound_at",
        (tenant_id,),
    )
    return [TenantChat.from_row(row) for row in rows]


async def primary_chat(db: Database, tenant_id: uuid.UUID) -> TenantChat | None:
    row = await db.fetch_one(
        f"SELECT {_CHAT_COLUMNS} FROM tenant_chats "
        "WHERE tenant_id = ? AND is_primary LIMIT 1",
        (tenant_id,),
    )
    return None if row is None else TenantChat.from_row(row)


# ── enrollment tokens ────────────────────────────────────────────────────────


async def create_enrollment_token(
    db: Database,
    *,
    token_hash: str,
    tenant_id: uuid.UUID,
    user_id: int,
    purpose: str,
    ttl_ms: int,
    at: int | None = None,
) -> None:
    """Record a single-use token. Only its hash is stored, never the token."""
    stamp = now_ms() if at is None else at
    async with db.transaction():
        # One live token per (tenant, user, purpose): issuing a new one must
        # invalidate the last, or a leaked older code stays usable.
        await db.execute(
            "DELETE FROM enrollment_tokens "
            "WHERE tenant_id = ? AND user_id = ? AND purpose = ?",
            (tenant_id, user_id, purpose),
        )
        await db.execute(
            """
            INSERT INTO enrollment_tokens
                (token_hash, tenant_id, user_id, purpose, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token_hash, tenant_id, user_id, purpose, stamp, stamp + max(1, ttl_ms)),
        )


async def consume_enrollment_token(
    db: Database, *, token_hash: str, purpose: str, at: int | None = None
) -> tuple[uuid.UUID, int] | None:
    """Redeem a token exactly once. Returns ``(tenant_id, user_id)``.

    The ``used_at IS NULL`` predicate in the ``UPDATE`` is what makes it
    single-use under concurrency; two redemptions race and one gets nothing.
    """
    stamp = now_ms() if at is None else at
    row = await db.fetch_one(
        """
        UPDATE enrollment_tokens
           SET used_at = ?
         WHERE token_hash = ? AND purpose = ? AND used_at IS NULL
           AND expires_at > ?
        RETURNING tenant_id, user_id
        """,
        (stamp, token_hash, purpose, stamp),
    )
    if row is None:
        return None
    return row["tenant_id"], as_int(row["user_id"])


async def prune_enrollment_tokens(db: Database, *, at: int | None = None) -> int:
    stamp = now_ms() if at is None else at
    return await db.execute(
        "DELETE FROM enrollment_tokens WHERE expires_at < ?", (stamp,)
    )
