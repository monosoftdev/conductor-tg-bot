"""Numbered ``.sql`` migrations against a ``schema_version`` table. No ORM.

Each file is ``NNN_name.sql`` and is applied exactly once, inside its own
transaction, in version order. Files must not contain their own
``BEGIN``/``COMMIT`` — this module supplies them so a half-applied migration
cannot exist.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ctb.db.connection import Database, now_ms

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationError",
    "applied_versions",
    "apply_migrations",
    "current_schema_version",
    "discover_migrations",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_FILENAME_RE = re.compile(r"^(?P<version>\d+)_(?P<name>[A-Za-z0-9._-]+)\.sql$")
_TXN_RE = re.compile(r"(?im)^\s*(BEGIN|COMMIT|END)\b")

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at INTEGER NOT NULL
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


async def current_schema_version(db: Database) -> int:
    """The highest applied version, or 0 on a fresh database."""
    await db.execute(_SCHEMA_VERSION_DDL)
    value = await db.fetch_val("SELECT MAX(version) FROM schema_version", default=0)
    return int(value or 0)


async def applied_versions(db: Database) -> frozenset[int]:
    await db.execute(_SCHEMA_VERSION_DDL)
    rows = await db.fetch_all("SELECT version FROM schema_version")
    return frozenset(int(row[0]) for row in rows)


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
        sql = migration.read()
        if _TXN_RE.search(sql):
            raise MigrationError(
                f"{migration.path.name} manages its own transaction; "
                "migrate.py wraps each file in BEGIN/COMMIT itself"
            )
        # executescript() implicitly commits any open transaction, so the
        # BEGIN/COMMIT has to live inside the script rather than around it.
        script = f"BEGIN;\n{sql}\nCOMMIT;"
        try:
            await db.executescript(script)
        except Exception as exc:  # pragma: no cover - depends on bad SQL
            with suppress(Exception):
                await db.execute("ROLLBACK")
            raise MigrationError(
                f"migration {migration.path.name} failed: {exc}"
            ) from exc
        await db.execute(
            "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, now_ms()),
        )
        applied.append(migration)

    return tuple(applied)
