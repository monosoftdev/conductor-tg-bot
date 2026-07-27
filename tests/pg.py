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
from contextlib import asynccontextmanager
from typing import Any, Final, LiteralString, NoReturn, cast

import psycopg
import pytest

from ctb.crypto import SecretBox
from ctb.db.bootstrap import APP_ROLE, WORKER_ROLE, bootstrap
from ctb.db.connection import (
    Database,
    reset_tenant,
    set_database,
    set_tenant,
    tenant_scope,
)
from ctb.runtime import reset_runtime, set_secret_box, set_system_database

__all__ = [
    "BOOTSTRAP_TENANT_ID",
    "as_tenant",
    "unscoped",
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

#: Deterministic so a failing test is reproducible; never used anywhere real.
TEST_MASTER_KEYS: Final = (
    "v2:VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ="
    ",v1:T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT08="
)

_APP_PASSWORD: Final = "ctb-app-test"
_WORKER_PASSWORD: Final = "ctb-worker-test"


def _no_server(exc: BaseException) -> NoReturn:
    """No database: skip on a laptop, **fail** in CI.

    Skipping locally is a kindness — the offline subset is most of the suite and
    a contributor should not need Docker to run it. Skipping in CI is a lie: it
    turns "the entire tenant-isolation, RLS, crypto-binding and claim half of
    the suite did not run" into a green tick. A DSN typo or a changed port would
    silently disarm every security test we have.
    """
    hint = f"PostgreSQL is not reachable ({exc}); run: docker compose up -d db"
    if os.environ.get("CI"):
        pytest.fail(f"{hint} — refusing to skip database tests in CI", pytrace=False)
    pytest.skip(hint)


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
        _no_server(exc)
    try:
        bootstrap(
            admin_dsn(),
            app_password=_APP_PASSWORD,
            worker_password=_WORKER_PASSWORD,
        )
    except psycopg.OperationalError as exc:  # pragma: no cover - no server
        _no_server(exc)
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
            cast(
                LiteralString,
                f"TRUNCATE {', '.join(pg_schema)} RESTART IDENTITY CASCADE",
            )
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

    The process-wide runtime handles are installed alongside it, because that
    is the shape a handler runs in: a scoped pool for its own data, a worker
    pool for tenancy lookups, and a :class:`~ctb.crypto.SecretBox`.
    """
    database = await Database(app_dsn(), min_size=1, max_size=6).connect()
    worker = await Database(worker_dsn(), min_size=1, max_size=4, system=True).connect()
    set_database(database)
    set_system_database(worker)
    set_secret_box(SecretBox.from_env_value(TEST_MASTER_KEYS))
    token = set_tenant(BOOTSTRAP_TENANT_ID)
    try:
        yield database
    finally:
        reset_tenant(token)
        set_database(None)
        reset_runtime()
        await worker.close()
        await database.close()


@pytest.fixture
async def system_db(pg_reset: tuple[str, ...]) -> AsyncIterator[Database]:
    """The **worker** pool: BYPASSRLS, cross-tenant reads, and no tenant scope.

    Production workers run exactly like this. A worker that needs to *write* a
    tenant's row must enter that tenant's scope explicitly — which is the point
    of not providing one here.
    """
    database = await Database(
        worker_dsn(), min_size=1, max_size=6, system=True
    ).connect()
    # Deliberately does NOT touch the tenant scope, in either direction: the
    # scope is a process-wide ContextVar, so a fixture that set or cleared it
    # would silently fight the `db` fixture depending on which ran last. Tests
    # that need to prove worker behaviour say so with `unscoped()`.
    try:
        yield database
    finally:
        await database.close()


@asynccontextmanager
async def unscoped() -> AsyncIterator[None]:
    """Run a block the way a background worker runs: with no tenant at all.

    This is the shape that hid three real bugs — the FSM storage read on every
    update, the voice workers, and the status-card writer all raised in
    production while every test sailed past with a scope the fixture supplied.
    """
    token = set_tenant(None)
    try:
        yield
    finally:
        reset_tenant(token)


def as_tenant(tenant_id: uuid.UUID = BOOTSTRAP_TENANT_ID) -> Any:
    """Seed a tenant's rows, on either pool.

    The worker pool bypasses row-level security but still needs a scope to
    *write*: ``tenant_id`` is filled from the GUC, so an unscoped insert is a
    NOT NULL violation. Production workers enter a scope for exactly this
    reason, and a test that seeds without one is not modelling anything real.
    """
    return tenant_scope(tenant_id)


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
