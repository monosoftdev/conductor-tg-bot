"""PostgreSQL fixtures: migrate once per session, truncate between tests.

Per-test migration against PostgreSQL costs 150-400ms, which would add minutes
to the suite. Migrating once and truncating instead costs 1-3ms per test.

The session fixture is deliberately **synchronous**. The asyncio fixture loop
scope is ``function``, so a session-scoped *async* fixture would build its pool on an
event loop that is torn down after the first test — a class of failure that is
extremely confusing to debug. A blocking ``psycopg.connect`` sidesteps it, and
the same sync runner is what ``python -m ctb.db.bootstrap`` uses in production,
where migrations must also finish before any pool opens.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Final

import psycopg
import pytest

from ctb.db.bootstrap import APP_ROLE, WORKER_ROLE, bootstrap
from ctb.db.connection import Database, reset_tenant, set_database, set_tenant

__all__ = [
    "BOOTSTRAP_TENANT_ID",
    "OTHER_TENANT_ID",
    "admin_dsn",
    "app_dsn",
    "make_tenant",
    "worker_dsn",
]

#: The tenant every legacy single-tenant test runs as.
BOOTSTRAP_TENANT_ID: Final = uuid.UUID("00000000-0000-4000-8000-000000000001")
#: A second tenant, seeded in every test, so isolation is always falsifiable.
OTHER_TENANT_ID: Final = uuid.UUID("00000000-0000-4000-8000-000000000002")

_APP_PASSWORD: Final = "ctb-app-test"
_WORKER_PASSWORD: Final = "ctb-worker-test"


def _base_dsn() -> str:
    """Where the disposable PostgreSQL lives. Matches ``compose.yaml``."""
    return os.environ.get(
        "CTB_TEST_DSN",
        "host=127.0.0.1 port=5433 user=postgres password=postgres dbname=ctb_test",
    )


def admin_dsn() -> str:
    return _base_dsn()


def _role_dsn(user: str, password: str) -> str:
    parts = [
        piece
        for piece in _base_dsn().split()
        if not piece.startswith(("user=", "password="))
    ]
    parts.extend([f"user={user}", f"password={password}"])
    return " ".join(parts)


def app_dsn() -> str:
    """The ``ctb_app`` role: row-level security applies."""
    return _role_dsn(APP_ROLE, _APP_PASSWORD)


def worker_dsn() -> str:
    """The ``ctb_worker`` role: BYPASSRLS, for the cross-tenant workers."""
    return _role_dsn(WORKER_ROLE, _WORKER_PASSWORD)


@pytest.fixture(scope="session")
def pg_schema() -> tuple[str, ...]:
    """Roles created, migrations applied, once. Returns the truncatable tables.

    The schema is dropped first. The test database is disposable by
    construction, and rebuilding it removes an entire class of confusing
    failure: an edited migration that the ``schema_version`` table believes has
    already been applied.
    """
    try:
        with psycopg.connect(admin_dsn(), autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
    except psycopg.OperationalError as exc:  # pragma: no cover - no server
        pytest.skip(f"PostgreSQL is not reachable ({exc}); run: docker compose up -d db")
    try:
        bootstrap(
            admin_dsn(),
            app_password=_APP_PASSWORD,
            worker_password=_WORKER_PASSWORD,
        )
    except psycopg.OperationalError as exc:  # pragma: no cover - no server
        pytest.skip(
            f"PostgreSQL is not reachable ({exc}); run: docker compose up -d db"
        )
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = current_schema() AND tablename <> 'schema_version'"
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _seed_tenants(conn: psycopg.Connection[object]) -> None:
    for tenant_id, slug in (
        (BOOTSTRAP_TENANT_ID, "test"),
        (OTHER_TENANT_ID, "other"),
    ):
        conn.execute(
            # A placeholder sealed key, so the seeded tenants look configured.
            # Nothing decrypts it: the pools under test are fakes, and the real
            # SecretBox is exercised directly in tests/test_crypto.py.
            "INSERT INTO tenants (id, slug, name, status, conductor_key_ct, "
            "                     conductor_key_kid, conductor_key_fp) "
            "VALUES (%s, %s, %s, 'active', %s, 'v2', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tenant_id, slug, slug.title(), b"sealed-placeholder", f"fp-{slug}"),
        )
        conn.execute(
            "INSERT INTO tenant_members (tenant_id, user_id, role) "
            "VALUES (%s, %s, 'owner') ON CONFLICT DO NOTHING",
            (tenant_id, 1001 if tenant_id == BOOTSTRAP_TENANT_ID else 2001),
        )


@pytest.fixture
def pg_reset(pg_schema: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    """An empty schema with both test tenants seeded, before every test."""
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        conn.execute(
            f"TRUNCATE {', '.join(pg_schema)} RESTART IDENTITY CASCADE"  # noqa: S608
        )
        _seed_tenants(conn)
    yield pg_schema


@pytest.fixture
async def db(pg_reset: tuple[str, ...]) -> AsyncIterator[Database]:
    """The **app** pool, scoped to the bootstrap tenant.

    Deliberately the RLS-enforcing role rather than the worker one: every
    database-touching test in the suite then exercises the isolation policy for
    free, and a repo statement that quietly stopped being tenant-scoped shows up
    as a failure here rather than as a leak in production.
    """
    database = await Database(app_dsn(), min_size=1, max_size=6).connect()
    set_database(database)
    token = set_tenant(BOOTSTRAP_TENANT_ID)
    try:
        yield database
    finally:
        reset_tenant(token)
        set_database(None)
        await database.close()


@pytest.fixture
async def system_db(pg_reset: tuple[str, ...]) -> AsyncIterator[Database]:
    """The **worker** pool: BYPASSRLS, cross-tenant reads allowed.

    The bootstrap tenant is still in scope, mirroring production — a worker
    acts *on behalf of* a tenant even though it may read across all of them —
    so inserts pick up the right ``tenant_id`` default.
    """
    database = await Database(
        worker_dsn(), min_size=1, max_size=6, system=True
    ).connect()
    token = set_tenant(BOOTSTRAP_TENANT_ID)
    try:
        yield database
    finally:
        reset_tenant(token)
        await database.close()


async def make_tenant(
    system: Database,
    *,
    slug: str,
    status: str = "active",
    owner_id: int = 5000,
) -> uuid.UUID:
    """Create an extra tenant for isolation tests."""
    row = await system.fetch_one(
        "INSERT INTO tenants (slug, name, status) VALUES (?, ?, ?) RETURNING id",
        (slug, slug.title(), status),
    )
    assert row is not None
    tenant_id: uuid.UUID = row["id"]
    await system.execute(
        "INSERT INTO tenant_members (tenant_id, user_id, role) VALUES (?, ?, 'owner')",
        (tenant_id, owner_id),
    )
    return tenant_id
