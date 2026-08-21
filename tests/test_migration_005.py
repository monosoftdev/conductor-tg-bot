"""Migration 005 against the shape the *live* database is actually in.

`room_gone` cleared both of a room's pointers, and `Route.claimable_thread`
reads exactly those two to decide a thread is Telegram's empty *New Chat* seat.
So a room detached by the DM rename bug still reads as scratch space after the
code fix, and the next line typed into it is still answered with the
new-workspace confirm card. The damage is in the data; only data undoes it.
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

_BEFORE = (
    "001_init.sql",
    "002_ci_watch.sql",
    "003_topic_per_session.sql",
    "004_platform_defaults.sql",
)
_SCRATCH = "ctb_migration_005"


@pytest.fixture
def before_005(tmp_path: Path) -> Iterator[str]:
    """A database carrying every migration that shipped before this one."""
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
    yield dsn
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH}" WITH (FORCE)')


def _tenant(conn: psycopg.Connection, slug: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES (%s, %s, %s)",
        (tenant_id, slug, slug.upper()),
    )
    conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (str(tenant_id),))
    return tenant_id


def _workspace(conn: psycopg.Connection, ws: str, *, status: str = "sleeping") -> None:
    conn.execute(
        "INSERT INTO workspaces (id, name, status) VALUES (%s, %s, %s)",
        (ws, ws, status),
    )


def _session(conn: psycopg.Connection, sid: str, ws: str) -> None:
    conn.execute(
        "INSERT INTO sessions (id, workspace_id, title) VALUES (%s, %s, %s)",
        (sid, ws, sid),
    )


def _prompted(
    conn: psycopg.Connection, sid: str, *, chat_id: int, thread_id: int, at: int
) -> None:
    conn.execute(
        "INSERT INTO outbound_prompts (message_id, session_id, body, chat_id,"
        " thread_id, created_at) VALUES (%s, %s, 'hello', %s, %s, %s)",
        (str(uuid.uuid4()), sid, chat_id, thread_id, at),
    )


def test_005_gives_a_detached_room_its_workspace_back(before_005: str) -> None:
    """The two-pointer clear is what made a worked-in room look brand new."""
    with psycopg.connect(before_005) as conn:
        _tenant(conn, "acme")
        _workspace(conn, "ws-live")
        _session(conn, "sess-1", "ws-live")
        # The room as `room_gone` left it: still a topic row, both pointers gone.
        conn.execute(
            "INSERT INTO chats (chat_id, thread_id, kind) VALUES (1132334, 1711456,"
            " 'topic')"
        )
        _prompted(conn, "sess-1", chat_id=1132334, thread_id=1711456, at=1_000)
        conn.commit()

        apply_migrations_sync(conn, MIGRATIONS_DIR)
        conn.commit()

        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (_only(conn),))
        row = conn.execute(
            "SELECT workspace_id, session_id FROM chats WHERE thread_id = 1711456"
        ).fetchone()

    assert row == ("ws-live", None), (
        "the room is still scratch space, so a follow-up still offers a new workspace"
    )


def test_005_recovers_a_room_that_only_ever_received(before_005: str) -> None:
    """No prompt was ever *sent* from it — the delivery ledger still knows."""
    with psycopg.connect(before_005) as conn:
        _tenant(conn, "acme")
        _workspace(conn, "ws-live")
        _session(conn, "sess-1", "ws-live")
        conn.execute(
            "INSERT INTO chats (chat_id, thread_id, kind) VALUES (1132334, 1709527,"
            " 'topic')"
        )
        conn.execute(
            "INSERT INTO deliveries (session_id, message_id, chat_id, thread_id,"
            " state) VALUES ('sess-1', 'm-1', 1132334, 1709527, 'sent')"
        )
        conn.commit()

        apply_migrations_sync(conn, MIGRATIONS_DIR)
        conn.commit()

        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (_only(conn),))
        row = conn.execute(
            "SELECT workspace_id FROM chats WHERE thread_id = 1709527"
        ).fetchone()

    assert row == ("ws-live",)


def test_005_leaves_scratch_seats_and_live_rooms_exactly_as_they_are(
    before_005: str,
) -> None:
    """Three things it must not touch, and each for its own reason."""
    with psycopg.connect(before_005) as conn:
        _tenant(conn, "acme")
        _workspace(conn, "ws-live")
        _workspace(conn, "ws-gone", status="archived")
        _session(conn, "sess-1", "ws-live")
        _session(conn, "sess-2", "ws-live")
        _session(conn, "sess-old", "ws-gone")
        conn.execute(
            "INSERT INTO chats (chat_id, thread_id, kind) VALUES"
            "  (1132334, 0, 'dm'),"  # the seat: shared by design
            "  (1132334, 4242, 'topic'),"  # never used: real scratch space
            "  (1132334, 5150, 'topic'),"  # its workspace is archived
            "  (1132334, 6060, 'topic')"  # still bound; nothing to repair
        )
        conn.execute(
            "UPDATE chats SET workspace_id = 'ws-live', session_id = 'sess-2'"
            " WHERE thread_id = 6060"
        )
        _prompted(conn, "sess-1", chat_id=1132334, thread_id=0, at=1_000)
        _prompted(conn, "sess-old", chat_id=1132334, thread_id=5150, at=1_000)
        conn.commit()

        apply_migrations_sync(conn, MIGRATIONS_DIR)
        conn.commit()

        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (_only(conn),))
        rooms = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT thread_id, workspace_id, session_id FROM chats"
            ).fetchall()
        }

    assert rooms[0] == (None, None), "the linear seat is a seat, not a room"
    assert rooms[4242] == (None, None), "a thread nobody used is genuinely scratch"
    assert rooms[5150] == (None, None), "an archived workspace's room is finished"
    assert rooms[6060] == ("ws-live", "sess-2"), "a live room was rewritten"


def test_005_keeps_two_tenants_apart(before_005: str) -> None:
    """The migration runs as the superuser, so RLS is not the guard here.

    ``chats_pkey`` is ``(chat_id, thread_id)`` and deliberately *not* tenant
    scoped — one Telegram chat belongs to one workspace forever — so the room
    below is unambiguously the second tenant's. Its prompt ledger is empty and
    the first tenant's still carries that address, which is exactly the shape a
    chat re-registered under a new team leaves behind. Joining on `(chat_id,
    thread_id)` alone would hand one team's workspace to another team's room.
    """
    with psycopg.connect(before_005) as conn:
        _tenant(conn, "one")
        _workspace(conn, "ws-one")
        _session(conn, "sess-one", "ws-one")
        _prompted(conn, "sess-one", chat_id=777, thread_id=12, at=1_000)
        second = _tenant(conn, "two")
        _workspace(conn, "ws-two")
        conn.execute(
            "INSERT INTO chats (chat_id, thread_id, kind) VALUES (777, 12, 'topic')"
        )
        conn.commit()

        apply_migrations_sync(conn, MIGRATIONS_DIR)
        conn.commit()

        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (str(second),))
        theirs = conn.execute(
            "SELECT workspace_id FROM chats WHERE chat_id = 777"
        ).fetchone()

    assert theirs == (None,), "another tenant's prompt history repaired this room"


def _only(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()
    assert row is not None
    return str(row[0])
