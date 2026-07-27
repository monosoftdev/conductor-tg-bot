"""PostgreSQL connection pooling, transactions, and the tenant scope.

Three things live here, and the third is the reason the other two look the way
they do.

**A pool, not a connection.** psycopg's :class:`AsyncConnectionPool` hands out
connections per statement. Every ``*_at`` column is epoch milliseconds; every
statement is written with ``?`` placeholders and translated on the way out (see
:mod:`ctb.db.sqlparam`), so the 137 call sites in :mod:`ctb.db.repo` never
mention psycopg.

**Re-entrant transactions.** :meth:`Database.transaction` binds a connection to
the current task in a :class:`ContextVar`; a nested ``transaction()`` or a bare
``execute()`` inside the block joins it rather than deadlocking on a second
checkout. The binding records the task id because ``asyncio.create_task``
*copies* the context — without that check a child task would issue statements on
its parent's connection concurrently, which psycopg connections do not allow.

**The tenant scope.** A tenant-scoped pool sets ``ctb.tenant_id`` on every
checkout, and row-level security policies filter on it. That is why repo SQL has
no ``WHERE tenant_id = ?``: forgetting a filter returns zero rows instead of
another tenant's data. Connections run with ``autocommit=False`` so the GUC is
transaction-local and physically cannot survive into the next checkout.

A ``system=True`` pool connects as the worker role, which bypasses RLS. It is
for the supervisor, the delivery and voice claim loops, migrations, prune, and
the tenancy lookups themselves — the tables that *decide* scope cannot be
filtered by it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Any, Final, LiteralString, Self, cast

from psycopg import AsyncConnection
from psycopg.rows import dict_row, tuple_row
from psycopg_pool import AsyncConnectionPool

from ctb.db.errors import TenantScopeError
from ctb.db.sqlparam import to_pyformat

__all__ = [
    "CONNECT_OPTIONS",
    "Database",
    "Params",
    "Row",
    "current_tenant",
    "get_database",
    "now_ms",
    "open_database",
    "reset_tenant",
    "set_database",
    "set_tenant",
    "tenant_scope",
]

#: The GUC row-level security policies read. Set per checkout, transaction-local.
_TENANT_GUC: Final = "ctb.tenant_id"

#: Applied by the server at connection start, so nothing can reset them later.
#: ``statement_timeout`` bounds a runaway query; the idle-in-transaction timeout
#: kills a leaked transaction instead of letting it wedge a pool slot forever.
CONNECT_OPTIONS: Final = (
    "-c statement_timeout=30000 "
    "-c idle_in_transaction_session_timeout=30000 "
    "-c timezone=UTC"
)

#: A fetched row. ``dict_row`` supports ``row["col"]`` throughout the repo layer.
type Row = dict[str, Any]

#: Bind parameters, positional. Named parameters are deliberately unsupported.
type Params = Sequence[Any]

_EMPTY: Final[tuple[Any, ...]] = ()

#: Open transactions, keyed by ``(pool id, task id)``.
#:
#: Both halves matter. The task id is required because ``asyncio.create_task``
#: *copies* the context — without it a child task would issue statements on its
#: parent's connection, which psycopg does not allow. The pool id is required
#: because two pools exist: a system-pool call made inside an app-pool
#: transaction would otherwise run on the app connection, under a role with no
#: grants on the system tables.
#: The default is an immutable mapping: a shared mutable default would be one
#: dict for the whole process, and every ``_bound.set`` here replaces rather
#: than mutates, so nothing ever writes through it.
_NO_TRANSACTIONS: Final[Mapping[tuple[int, int], AsyncConnection[Row]]] = (
    MappingProxyType({})
)
_bound: ContextVar[Mapping[tuple[int, int], AsyncConnection[Row]]] = ContextVar(
    "ctb_db_bound", default=_NO_TRANSACTIONS
)

#: The tenant every scoped statement is filtered by.
_tenant: ContextVar[uuid.UUID | None] = ContextVar("ctb_db_tenant", default=None)


def now_ms() -> int:
    """Wall-clock epoch milliseconds, UTC. Every ``*_at`` column stores this."""
    return int(time.time() * 1000)


# ── tenant scope ─────────────────────────────────────────────────────────────


def set_tenant(tenant_id: uuid.UUID | None) -> Token[uuid.UUID | None]:
    """Enter a tenant scope. Pass the token to :func:`reset_tenant`."""
    return _tenant.set(tenant_id)


def reset_tenant(token: Token[uuid.UUID | None]) -> None:
    _tenant.reset(token)


def current_tenant() -> uuid.UUID | None:
    """The tenant in scope, or ``None`` outside any scope."""
    return _tenant.get()


@asynccontextmanager
async def tenant_scope(tenant_id: uuid.UUID) -> AsyncIterator[uuid.UUID]:
    """Run a block scoped to one tenant."""
    token = set_tenant(tenant_id)
    try:
        yield tenant_id
    finally:
        reset_tenant(token)


# ── the database ─────────────────────────────────────────────────────────────


class Database:
    """A pooled PostgreSQL handle with this project's statement dialect."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        system: bool = False,
        timeout: float = 10.0,
        open_timeout: float = 30.0,
    ) -> None:
        self.dsn = dsn
        #: ``True`` for the worker pool: no tenant GUC, RLS bypassed by role.
        self.system = system
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout
        self._open_timeout = open_timeout
        self._pool: AsyncConnectionPool[AsyncConnection[Row]] | None = None

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> Self:
        if self._pool is not None:
            return self
        pool: AsyncConnectionPool[AsyncConnection[Row]] = AsyncConnectionPool(
            self.dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=self._timeout,
            open=False,
            kwargs={
                "row_factory": dict_row,
                "autocommit": False,
                "options": CONNECT_OPTIONS,
            },
            reset=_reset_connection,
        )
        await pool.open(wait=True, timeout=self._open_timeout)
        self._pool = pool
        return self

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    @property
    def pool(self) -> AsyncConnectionPool[AsyncConnection[Row]]:
        if self._pool is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self._pool

    def stats(self) -> dict[str, int]:
        """Pool counters for ``/health``. Exhaustion is visible before it hurts."""
        if self._pool is None:
            return {}
        return dict(self._pool.get_stats())

    # -- the seam every statement goes through -------------------------------

    def _binding_key(self) -> tuple[int, int]:
        """Which open transaction, if any, this call belongs to."""
        return (id(self), _task_id())

    async def _apply_scope(self, conn: AsyncConnection[Row]) -> None:
        """Publish the tenant, transaction-locally.

        The GUC answers "who is this acting for"; the *role* answers "may this
        connection see everything". They are separate on purpose: a system pool
        with a tenant in scope still bypasses row-level security, but its
        inserts pick up the right ``tenant_id`` default — which is what lets a
        worker write on a tenant's behalf without naming the column.
        """
        tenant = _tenant.get()
        if tenant is None:
            if self.system:
                return
            raise TenantScopeError(
                "no tenant in scope for a tenant-scoped query; wrap the call in "
                "ctb.db.connection.tenant_scope() or use the system database"
            )
        await conn.execute(
            "SELECT set_config(%s, %s, true)", (_TENANT_GUC, str(tenant))
        )

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[AsyncConnection[Row]]:
        """The open transaction's connection, or a fresh one that self-commits."""
        existing = _bound.get().get(self._binding_key())
        if existing is not None:
            yield existing
            return
        async with self.pool.connection() as conn:
            await self._apply_scope(conn)
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            await conn.commit()

    @asynccontextmanager
    async def transaction(
        self, *, immediate: bool = True
    ) -> AsyncIterator[AsyncConnection[Row]]:
        """Run a block atomically. Nested calls join the outer transaction.

        ``immediate`` is accepted and ignored: it named SQLite's
        ``BEGIN IMMEDIATE``, and PostgreSQL takes row locks as it goes. The
        argument stays so the call sites that pass it keep reading the same.
        """
        del immediate
        key = self._binding_key()
        open_now = _bound.get()
        existing = open_now.get(key)
        if existing is not None:
            yield existing
            return
        async with self.pool.connection() as conn:
            await self._apply_scope(conn)
            token = _bound.set({**open_now, key: conn})
            try:
                try:
                    yield conn
                except BaseException:
                    await conn.rollback()
                    raise
                await conn.commit()
            finally:
                _bound.reset(token)

    # -- statements ----------------------------------------------------------

    async def execute(self, sql: str, params: Params = _EMPTY) -> int:
        """Run one statement. Returns ``rowcount`` (-1 when not applicable)."""
        async with self._acquire() as conn:
            cursor = await conn.execute(_query(sql), tuple(params))
            return cursor.rowcount

    async def execute_script(self, script: str) -> None:
        """Run several ``;``-separated statements. No placeholders, no ``%``.

        psycopg only accepts a multi-statement query when it has no parameters
        to interpolate, which is exactly the migration case.
        """
        async with self._acquire() as conn:
            await conn.execute(_script(script))

    async def fetch_one(self, sql: str, params: Params = _EMPTY) -> Row | None:
        async with self._acquire() as conn:
            cursor = await conn.execute(_query(sql), tuple(params))
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: Params = _EMPTY) -> list[Row]:
        async with self._acquire() as conn:
            cursor = await conn.execute(_query(sql), tuple(params))
            return list(await cursor.fetchall())

    async def fetch_val(
        self, sql: str, params: Params = _EMPTY, default: Any = None
    ) -> Any:
        """The first column of the first row, or ``default`` for NULL/no row."""
        # A tuple cursor, so ``row[0]`` keeps working while every other method
        # returns the dict rows the repository layer subscripts by name.
        async with (
            self._acquire() as conn,
            conn.cursor(row_factory=tuple_row) as cursor,
        ):
            await cursor.execute(_query(sql), tuple(params))
            row = await cursor.fetchone()
        if not row:
            return default
        return default if row[0] is None else row[0]


async def _reset_connection(conn: AsyncConnection[Row]) -> None:
    """Belt and braces: nothing half-done goes back into the pool."""
    if not conn.closed:
        await conn.rollback()


def _query(sql: str) -> LiteralString:
    """psycopg types its query parameter as ``LiteralString`` to discourage
    string-built SQL. Every statement here is a module-level literal or built
    by :func:`ctb.db.repo._util.update_sql` from literal column names, so the
    cast is the honest way to say "checked elsewhere" rather than a bypass.
    """
    return cast(LiteralString, to_pyformat(sql))


def _script(sql: str) -> LiteralString:
    return cast(LiteralString, sql)


def _task_id() -> int:
    task = asyncio.current_task()
    return 0 if task is None else id(task)


async def open_database(dsn: str, **kwargs: Any) -> Database:
    """Connect a pool and return it."""
    return await Database(dsn, **kwargs).connect()


_database: Database | None = None


def set_database(db: Database | None) -> None:
    """Install the process-wide database handle (boot path and tests)."""
    global _database
    _database = db


def get_database() -> Database:
    """The process-wide database handle installed by :func:`set_database`."""
    if _database is None:
        raise RuntimeError("no Database installed; call set_database() at boot")
    return _database
