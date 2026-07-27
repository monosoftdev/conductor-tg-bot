"""Tenant isolation, enforced by PostgreSQL rather than by anyone's diligence.

The claim this file has to make good on is strong: a repository statement with
no ``WHERE tenant_id = ?`` — which is *every* statement in
:mod:`ctb.db.repo` — cannot return another tenant's rows.

:func:`test_every_tenant_table_is_protected` is generated from the live schema
rather than a hand-written list, so a table added next year is covered without
anyone remembering to add it here.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from ctb.db.connection import (
    Database,
    current_tenant,
    reset_tenant,
    set_tenant,
    tenant_scope,
)
from ctb.db.errors import InsufficientPrivilege, TenantScopeError
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID, admin_dsn, app_dsn

pytestmark = pytest.mark.db

#: Every table carrying a ``tenant_id``. Tables added later appear here for
#: free — which is the point.
_QUERY_TENANT_TABLES = """
SELECT c.relname
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
 WHERE n.nspname = current_schema()
   AND c.relkind = 'r'
   AND a.attname = 'tenant_id'
   AND NOT a.attisdropped
   AND c.relname NOT IN ('tenants', 'tenant_members', 'tenant_chats',
                         'enrollment_tokens', 'api_events')
 ORDER BY c.relname
"""


def _tenant_tables() -> list[str]:
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        return [str(row[0]) for row in conn.execute(_QUERY_TENANT_TABLES).fetchall()]


class TestSchemaGuarantees:
    def test_the_schema_actually_has_tenant_tables(self, pg_reset: object) -> None:
        assert len(_tenant_tables()) >= 9

    def test_every_tenant_table_is_protected(self, pg_reset: object) -> None:
        """RLS enabled *and* forced, with an isolation policy, on every one."""
        missing: list[str] = []
        with psycopg.connect(admin_dsn(), autocommit=True) as conn:
            for table in _tenant_tables():
                row = conn.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = %s AND relnamespace = "
                    "(SELECT oid FROM pg_namespace WHERE nspname = current_schema())",
                    (table,),
                ).fetchone()
                policy = conn.execute(
                    "SELECT 1 FROM pg_policies WHERE tablename = %s "
                    "AND policyname = 'tenant_isolation'",
                    (table,),
                ).fetchone()
                if row is None or not row[0] or not row[1] or policy is None:
                    missing.append(table)
        assert missing == [], f"unprotected tenant tables: {missing}"

    def test_no_id_or_timestamp_column_is_a_32_bit_integer(
        self, pg_reset: object
    ) -> None:
        """Telegram ids exceed int32, and epoch-ms overflows it in 1970.

        Durations are deliberately not covered: ``duration_ms`` measures one
        HTTP request, which is not going to need 25 days of headroom.
        """
        with psycopg.connect(admin_dsn(), autocommit=True) as conn:
            rows = conn.execute(
                """
                SELECT table_name, column_name
                  FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND data_type = 'integer'
                   AND (column_name LIKE '%\\_id'
                        OR column_name LIKE '%\\_at'
                        OR column_name IN ('chat_id', 'user_id', 'thread_id'))
                """
            ).fetchall()
        assert [tuple(r) for r in rows] == []

    def test_the_app_role_cannot_bypass_row_level_security(
        self, pg_reset: object
    ) -> None:
        with psycopg.connect(admin_dsn(), autocommit=True) as conn:
            row = conn.execute(
                "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'ctb_app'"
            ).fetchone()
        assert row is not None
        assert row[0] is False and row[1] is False


class TestScopeIsRequired:
    async def test_a_query_with_no_tenant_in_scope_raises(
        self, pg_reset: object
    ) -> None:
        database = await Database(app_dsn(), min_size=1, max_size=2).connect()
        try:
            with pytest.raises(TenantScopeError):
                await database.fetch_all("SELECT 1 FROM chats")
        finally:
            await database.close()

    async def test_the_scope_is_visible_and_restorable(self, db: Database) -> None:
        assert current_tenant() == BOOTSTRAP_TENANT_ID
        async with tenant_scope(OTHER_TENANT_ID):
            assert current_tenant() == OTHER_TENANT_ID
        assert current_tenant() == BOOTSTRAP_TENANT_ID

    async def test_the_guc_reaches_the_server(self, db: Database) -> None:
        value = await db.fetch_val("SELECT current_setting('ctb.tenant_id')")
        assert uuid.UUID(str(value)) == BOOTSTRAP_TENANT_ID

    async def test_the_guc_does_not_survive_into_the_next_checkout(
        self, db: Database
    ) -> None:
        """It is transaction-local, so a pooled connection cannot leak a scope."""
        assert await db.fetch_val("SELECT current_setting('ctb.tenant_id')")
        token = set_tenant(None)
        try:
            with pytest.raises(TenantScopeError):
                await db.fetch_val("SELECT current_setting('ctb.tenant_id')")
        finally:
            reset_tenant(token)


class TestReadIsolation:
    async def test_a_repo_read_never_crosses_tenants(self, db: Database) -> None:
        await chats_repo.ensure(db, -100, 0, kind="general")
        async with tenant_scope(OTHER_TENANT_ID):
            await chats_repo.ensure(db, -200, 0, kind="general")
            assert [row.chat_id for row in await chats_repo.list_all(db)] == [-200]
        assert [row.chat_id for row in await chats_repo.list_all(db)] == [-100]

    async def test_a_point_lookup_of_another_tenants_row_returns_none(
        self, db: Database
    ) -> None:
        async with tenant_scope(OTHER_TENANT_ID):
            await workspaces_repo.upsert(db, "ws-other", name="theirs")
        assert await workspaces_repo.get(db, "ws-other") is None

    async def test_sessions_are_isolated(self, db: Database) -> None:
        async with tenant_scope(OTHER_TENANT_ID):
            await workspaces_repo.upsert(db, "ws-o", name="o")
            await sessions_repo.upsert(db, "s-other", workspace_id="ws-o")
        assert await sessions_repo.get(db, "s-other") is None
        assert await sessions_repo.list_all(db) == []


class TestWriteIsolation:
    async def test_the_tenant_column_is_filled_from_the_scope(
        self, db: Database
    ) -> None:
        await chats_repo.ensure(db, -300, 0, kind="general")
        owner = await db.fetch_val(
            "SELECT tenant_id FROM chats WHERE chat_id = ?", (-300,)
        )
        assert owner == BOOTSTRAP_TENANT_ID

    async def test_writing_a_row_for_another_tenant_is_refused(
        self, db: Database
    ) -> None:
        with pytest.raises(InsufficientPrivilege):
            await db.execute(
                "INSERT INTO chats (tenant_id, chat_id, thread_id) VALUES (?, ?, 0)",
                (OTHER_TENANT_ID, -400),
            )

    async def test_an_update_cannot_reach_another_tenants_row(
        self, db: Database
    ) -> None:
        async with tenant_scope(OTHER_TENANT_ID):
            await chats_repo.ensure(db, -500, 0, kind="general")
            await chats_repo.set_notify(db, -500, 0, notify="loud")
        changed = await db.execute(
            "UPDATE chats SET notify = 'off' WHERE chat_id = ?", (-500,)
        )
        assert changed == 0
        async with tenant_scope(OTHER_TENANT_ID):
            row = await chats_repo.get(db, -500, 0)
        assert row is not None and row.notify == "loud"

    async def test_a_delete_cannot_reach_another_tenants_row(
        self, db: Database
    ) -> None:
        async with tenant_scope(OTHER_TENANT_ID):
            await chats_repo.ensure(db, -600, 0, kind="general")
        assert await db.execute("DELETE FROM chats WHERE chat_id = ?", (-600,)) == 0


class TestSystemPool:
    async def test_the_worker_pool_sees_every_tenant(
        self, db: Database, system_db: Database
    ) -> None:
        await chats_repo.ensure(db, -700, 0, kind="general")
        async with tenant_scope(OTHER_TENANT_ID):
            await chats_repo.ensure(db, -800, 0, kind="general")
        seen = await system_db.fetch_all("SELECT chat_id FROM chats ORDER BY chat_id")
        assert [row["chat_id"] for row in seen] == [-800, -700]

    async def test_the_worker_pool_needs_no_tenant_in_scope(
        self, system_db: Database
    ) -> None:
        assert await system_db.fetch_val("SELECT COUNT(*) FROM chats") == 0
