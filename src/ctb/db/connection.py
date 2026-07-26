"""aiosqlite connection management. One writer, WAL, explicit transactions.

SQLite on the Railway volume is the whole storage story: one instance (a volume
attaches to exactly one), fewer than a hundred writes a minute, all point
lookups, and a backup that is one file.

The connection runs in autocommit mode (``isolation_level=None``) so single
statements are durable immediately and multi-statement work is wrapped in an
explicit ``BEGIN IMMEDIATE`` via :meth:`Database.transaction`. A per-database
lock serialises writers on the shared connection; it is re-entrant per task, so
calling :meth:`Database.execute` inside a ``transaction()`` block joins the open
transaction instead of deadlocking.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Final, Self

import aiosqlite

__all__ = [
    "PRAGMAS",
    "Database",
    "get_database",
    "now_ms",
    "open_database",
    "set_database",
]

#: Applied to every connection, in order.
PRAGMAS: Final[tuple[tuple[str, str], ...]] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "5000"),
    ("foreign_keys", "ON"),
)

_in_transaction: ContextVar[bool] = ContextVar("ctb_db_in_transaction", default=False)

#: Bind parameters. ``dict`` supports named placeholders (``:name``).
type Params = Sequence[Any] | dict[str, Any]

_EMPTY: Final[tuple[Any, ...]] = ()


def now_ms() -> int:
    """Wall-clock epoch milliseconds, UTC. Every ``*_at`` column stores this."""
    return int(time.time() * 1000)


class Database:
    """A single shared aiosqlite connection with the project's pragmas."""

    def __init__(self, path: str | Path, *, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self._timeout = timeout
        self._conn: aiosqlite.Connection | None = None
        self._lock: asyncio.Lock | None = None

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> Self:
        if self._conn is not None:
            return self
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(
            self.path,
            timeout=self._timeout,
            isolation_level=None,  # autocommit; we manage transactions ourselves
        )
        conn.row_factory = aiosqlite.Row
        for name, value in PRAGMAS:
            await conn.execute(f"PRAGMA {name}={value}")
        self._conn = conn
        self._lock = asyncio.Lock()
        return self

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        self._lock = None
        if conn is not None:
            await conn.close()

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self._conn

    # -- locking -------------------------------------------------------------

    @asynccontextmanager
    async def _write_lock(self) -> AsyncIterator[None]:
        if _in_transaction.get() or self._lock is None:
            yield
            return
        async with self._lock:
            yield

    @asynccontextmanager
    async def transaction(
        self, *, immediate: bool = True
    ) -> AsyncIterator[aiosqlite.Connection]:
        """Run a block atomically.

        ``BEGIN IMMEDIATE`` by default: the write lock is taken up front, so a
        reader/writer race fails fast against ``busy_timeout`` instead of
        halfway through. Nested calls join the outer transaction.
        """
        if _in_transaction.get():
            yield self.conn
            return
        lock = self._lock
        if lock is None:
            raise RuntimeError("Database.connect() has not been awaited")
        async with lock:
            token = _in_transaction.set(True)
            try:
                await self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                try:
                    yield self.conn
                except BaseException:
                    await self.conn.execute("ROLLBACK")
                    raise
                await self.conn.execute("COMMIT")
            finally:
                _in_transaction.reset(token)

    # -- statements ----------------------------------------------------------

    async def execute(self, sql: str, params: Params = _EMPTY) -> int:
        """Run one statement. Returns ``rowcount`` (-1 when not applicable)."""
        async with self._write_lock(), self.conn.execute(sql, params) as cursor:
            return cursor.rowcount

    async def execute_insert(self, sql: str, params: Params = _EMPTY) -> int | None:
        """Run one INSERT. Returns ``lastrowid``."""
        async with self._write_lock(), self.conn.execute(sql, params) as cursor:
            return cursor.lastrowid

    async def executemany(self, sql: str, seq: Iterable[Params]) -> int:
        async with self._write_lock(), self.conn.executemany(sql, seq) as cursor:
            return cursor.rowcount

    async def executescript(self, script: str) -> None:
        async with self._write_lock():
            await self.conn.executescript(script)

    async def fetch_one(
        self, sql: str, params: Params = _EMPTY
    ) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: Params = _EMPTY) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetch_val(
        self, sql: str, params: Params = _EMPTY, default: Any = None
    ) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None or len(row) == 0:
            return default
        value = row[0]
        return default if value is None else value

    # -- maintenance ---------------------------------------------------------

    async def vacuum_into(self, destination: str | Path) -> None:
        """Online backup. ``VACUUM INTO`` is safe while the bot is running."""
        await self.execute("VACUUM INTO ?", (str(destination),))


async def open_database(path: str | Path) -> Database:
    """Connect (creating the file and its parent directory if needed)."""
    return await Database(path).connect()


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
