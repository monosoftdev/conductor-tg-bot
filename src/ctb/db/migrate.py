"""Numbered ``.sql`` migrations against a ``schema_version`` table. No ORM.

Each file is ``NNN_name.sql``, applied exactly once, in version order, inside a
transaction this module opens. PostgreSQL DDL is transactional, so the
``schema_version`` row commits atomically with the schema change it records —
there is no window where a migration is applied but unrecorded.

Files must not manage their own transaction. ``DO $$ … BEGIN … END $$`` blocks
are fine: the guard ignores anything inside dollar quoting.

Both a sync and an async runner live here. The sync one exists because
migrations must happen *before* the pool opens — at boot, and in the
session-scoped test fixture, where an async fixture would build its pool on an
event loop that is torn down after the first test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psycopg

from ctb.db.connection import Database, now_ms

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationError",
    "applied_versions",
    "apply_migrations",
    "apply_migrations_sync",
    "current_schema_version",
    "discover_migrations",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_FILENAME_RE: Final = re.compile(r"^(?P<version>\d+)_(?P<name>[A-Za-z0-9._-]+)\.sql$")
_TXN_RE: Final = re.compile(r"(?im)^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION)\b")
_DOLLAR_RE: Final = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")

#: Held for the duration of a migration run so two booting instances cannot
#: apply the same file twice. An arbitrary but stable project-specific key.
_ADVISORY_LOCK_KEY: Final = 0x63_74_62_01

_SCHEMA_VERSION_DDL: Final = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    applied_at bigint NOT NULL
)
"""


class MigrationError(RuntimeError):
    """A migration file is malformed or failed to apply."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


def _strip_quoted(sql: str) -> str:
    """Blank out quoted bodies so the transaction guard only sees real SQL.

    Both kinds matter. A ``DO`` block's own ``BEGIN``/``END`` is procedural, not
    transactional, so ``$tag$ … $tag$`` must be skipped — and a *single-quoted
    literal* containing ``$$`` would otherwise open a dollar quote that never
    closes, swallowing the rest of the file and disarming the guard entirely.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        if char == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            stop = min(j + 1, n)
        elif (match := _DOLLAR_RE.match(sql, i)) is not None:
            tag = match.group(0)
            end = sql.find(tag, match.end())
            stop = n if end < 0 else end + len(tag)
        else:
            out.append(char)
            i += 1
            continue
        # Preserve newlines so the (?m) anchors still line up.
        out.append("\n" * sql.count("\n", i, stop))
        i = stop
    return "".join(out)


def discover_migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    """All ``NNN_name.sql`` files in ``directory``, ordered by version."""
    root = directory or MIGRATIONS_DIR
    if not root.is_dir():
        raise MigrationError(f"migrations directory not found: {root}")
    found: dict[int, Migration] = {}
    for path in sorted(root.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(f"migration {path.name!r} does not match NNN_name.sql")
        version = int(match.group("version"))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{found[version].path.name} and {path.name}"
            )
        found[version] = Migration(version, match.group("name"), path)
    return tuple(found[v] for v in sorted(found))


def _checked_sql(migration: Migration) -> str:
    sql = migration.read()
    if _TXN_RE.search(_strip_quoted(sql)):
        raise MigrationError(
            f"{migration.path.name} manages its own transaction; "
            "migrate.py wraps each file in one itself"
        )
    return sql


async def current_schema_version(db: Database) -> int:
    """The highest applied version, or 0 when nothing has been applied.

    Read-only on purpose: ``/health`` calls this on every report, and the roles
    the application runs as have no rights to create anything. Only
    :func:`apply_migrations` may touch the schema.
    """
    value = await db.fetch_val(
        "SELECT MAX(version) FROM schema_version "
        "WHERE to_regclass('schema_version') IS NOT NULL",
        default=0,
    )
    return int(value or 0)


async def applied_versions(db: Database) -> frozenset[int]:
    await db.execute(_SCHEMA_VERSION_DDL)
    rows = await db.fetch_all("SELECT version FROM schema_version")
    return frozenset(int(row["version"]) for row in rows)


async def apply_migrations(
    db: Database, directory: Path | None = None
) -> tuple[Migration, ...]:
    """Apply every migration not yet recorded. Returns the ones applied.

    Idempotent: running it against an up-to-date database applies nothing.
    """
    migrations = discover_migrations(directory)
    already = await applied_versions(db)
    pending = [m for m in migrations if m.version not in already]
    applied: list[Migration] = []

    for migration in pending:
        sql = _checked_sql(migration)
        try:
            async with db.transaction():
                await db.execute(
                    "SELECT pg_advisory_xact_lock(?)", (_ADVISORY_LOCK_KEY,)
                )
                await db.execute_script(sql)
                await db.execute(
                    "INSERT INTO schema_version(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.name, now_ms()),
                )
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                f"migration {migration.path.name} failed: {exc}"
            ) from exc
        applied.append(migration)

    return tuple(applied)


def apply_migrations_sync(
    conn: psycopg.Connection[object], directory: Path | None = None
) -> tuple[Migration, ...]:
    """The same run, on a plain blocking connection.

    Used by ``python -m ctb.db.bootstrap`` and by the session-scoped test
    fixture, both of which need the schema in place before any pool exists.
    """
    migrations = discover_migrations(directory)
    applied: list[Migration] = []
    with conn.transaction():
        conn.execute(_SCHEMA_VERSION_DDL)  # type: ignore[arg-type]
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        already = {int(row[0]) for row in rows}  # type: ignore[index]
        for migration in migrations:
            if migration.version in already:
                continue
            sql = _checked_sql(migration)
            try:
                conn.execute(sql)  # type: ignore[arg-type]
                conn.execute(
                    "INSERT INTO schema_version(version, name, applied_at) "
                    "VALUES (%s, %s, %s)",
                    (migration.version, migration.name, now_ms()),
                )
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationError(
                    f"migration {migration.path.name} failed: {exc}"
                ) from exc
            applied.append(migration)
    return tuple(applied)
