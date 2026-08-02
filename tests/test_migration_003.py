"""Migration 003 against the shape a *deployed* database is actually in.

Every other test starts from a schema built by applying all migrations at once,
which is exactly the state migration 003 never has to survive. The one that can
fail on real data is the upgrade: two bound sessions per room is what every
`/fork` and every `/s` has been leaving since day one, and the uniqueness index
cannot be created until they are resolved.

Run against a scratch database of its own so the session-scoped schema fixture
is untouched.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ctb.db.migrate import MIGRATIONS_DIR, apply_migrations_sync
from tests.pg import admin_dsn

pytestmark = pytest.mark.db

#: The versions a database deployed before this change has already recorded.
_BEFORE = ("001_init.sql", "002_ci_watch.sql")
_SCRATCH = "ctb_migration_003"


@pytest.fixture
def previous_schema(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A fresh database carrying only the migrations that shipped before 003."""
    base = admin_dsn()
    try:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{_SCRATCH}"')
    except psycopg.OperationalError as exc:  # pragma: no cover - no server
        pytest.skip(f"no PostgreSQL for the migration test: {exc}")
    older = tmp_path / "before"
    older.mkdir()
    for name in _BEFORE:
        shutil.copy(MIGRATIONS_DIR / name, older / name)
    dsn = " ".join(
        [
            *(p for p in base.split() if not p.startswith("dbname=")),
            f"dbname={_SCRATCH}",
        ]
    )
    with psycopg.connect(dsn) as conn:
        apply_migrations_sync(conn, older)
        conn.commit()
    yield dsn, older
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH}" WITH (FORCE)')


def test_003_resolves_the_duplicates_it_would_otherwise_fail_on(
    previous_schema: tuple[str, Path],
) -> None:
    """Cleanup first, backfill second, index last — and the order is the point.

    The backfill asks "which session is bound to this thread?", which is only a
    single row *because* the cleanup already ran. Reverse them and both sessions
    on the seat inherit the room.
    """
    dsn, _ = previous_schema
    tenant = str(uuid.uuid4())
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO tenants (id, slug, name) VALUES (%s, 't', 'T')", (tenant,)
        )
        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (tenant,))
        conn.execute(
            "INSERT INTO workspaces (id, chat_id, topic_id, topic_name, topic_marker)"
            " VALUES ('ws', -1001, 7, 'fix login · api/main', 'working')"
        )
        # Two bound sessions on one room: the supervisor polled both and both
        # delivered into it, with `created_at DESC` deciding where a prompt went.
        for name, created in (("older", 100), ("newer", 200)):
            conn.execute(
                "INSERT INTO sessions (id, workspace_id, chat_id, thread_id,"
                " is_bound, created_at) VALUES (%s, 'ws', -1001, 7, true, %s)",
                (name, created),
            )
        # A sibling on a *different* thread of the same workspace.
        conn.execute(
            "INSERT INTO sessions (id, workspace_id, chat_id, thread_id, is_bound,"
            " created_at) VALUES ('other', 'ws', -1001, 8, true, 150)"
        )
        # And one session two rooms still routed to, which `/s` in two topics left.
        conn.execute(
            "INSERT INTO chats (chat_id, thread_id, session_id, workspace_id,"
            " updated_at) VALUES (-1001, 7, 'newer', 'ws', 200),"
            " (-1001, 9, 'newer', 'ws', 100)"
        )
        conn.commit()

        applied = apply_migrations_sync(conn, MIGRATIONS_DIR)
        conn.commit()

        assert [m.version for m in applied] == [3], "only the new one runs"
        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (tenant,))
        sessions = {
            str(row[0]): (bool(row[1]), row[2])
            for row in conn.execute(
                "SELECT id, is_bound, topic_name FROM sessions"
            ).fetchall()
        }
        chats = conn.execute(
            "SELECT thread_id, session_id FROM chats ORDER BY thread_id"
        ).fetchall()

    # The newest survives — the same tiebreak `get_bound_for` already applied,
    # so nothing moves; the loser only stops polling and delivering.
    assert sessions["newer"] == (True, "fix login · api/main")
    assert sessions["older"] == (False, None)
    # A session bound to another thread of the same workspace inherits nothing.
    assert sessions["other"] == (True, None)
    # One session, one room.
    assert chats == [(7, "newer"), (9, None)]


def test_003_makes_the_model_a_constraint_rather_than_a_convention(
    previous_schema: tuple[str, Path],
) -> None:
    """After the migration, a second bound session in a room is rejected."""
    dsn, _ = previous_schema
    tenant = str(uuid.uuid4())
    with psycopg.connect(dsn) as conn:
        apply_migrations_sync(conn, MIGRATIONS_DIR)
        conn.commit()
        conn.execute(
            "INSERT INTO tenants (id, slug, name) VALUES (%s, 't', 'T')", (tenant,)
        )
        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (tenant,))
        conn.execute("INSERT INTO workspaces (id) VALUES ('ws')")
        conn.execute(
            "INSERT INTO sessions (id, workspace_id, chat_id, thread_id, is_bound)"
            " VALUES ('a', 'ws', -1001, 7, true)"
        )
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO sessions (id, workspace_id, chat_id, thread_id,"
                " is_bound) VALUES ('b', 'ws', -1001, 7, true)"
            )
        conn.rollback()

        # Thread 0 is a seat, not a room, and is carved out explicitly.
        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (tenant,))
        conn.execute(
            "INSERT INTO sessions (id, workspace_id, chat_id, thread_id, is_bound)"
            " VALUES ('c', 'ws', 1001, 0, true), ('d', 'ws', 1001, 0, true)"
        )
        conn.commit()
