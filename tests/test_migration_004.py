"""Migration 004 against the shape a *deployed* database is actually in.

`tenants.default_agent/model/effort/branch` shipped NOT NULL with the platform
literal as their column default, and nothing in the bot has ever written them —
so `DEFAULT_BRANCH=dev` in the environment reached no tenant at all. The column
becomes an override, and NULL means "follow the platform".
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psycopg
import pytest

from ctb.bot.middleware.tenancy import TenantSettings
from ctb.db.migrate import MIGRATIONS_DIR, apply_migrations_sync
from ctb.db.repo.tenancy import TenantRow
from ctb.settings import Settings
from tests.pg import admin_dsn

pytestmark = pytest.mark.db

_BEFORE = ("001_init.sql", "002_ci_watch.sql", "003_topic_per_session.sql")
_SCRATCH = "ctb_migration_004"


@pytest.fixture
def before_004(tmp_path: Path) -> Iterator[str]:
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


def test_004_releases_the_shipped_literals_and_keeps_a_deliberate_pin(
    before_004: str,
) -> None:
    """A value that differs was set by hand and outranks an env var.

    A hand-set `main` is indistinguishable from the column default and is
    released — that is the intended direction, and the whole reason the env var
    exists.
    """
    untouched, pinned = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(before_004) as conn:
        conn.execute(
            "INSERT INTO tenants (id, slug, name) VALUES (%s, 'a', 'A')", (untouched,)
        )
        conn.execute(
            "INSERT INTO tenants (id, slug, name, default_branch, default_agent)"
            " VALUES (%s, 'b', 'B', 'release', 'codex')",
            (pinned,),
        )
        # And the seat a create pinned by side effect, which outranked both.
        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (str(untouched),))
        conn.execute(
            "INSERT INTO chats (chat_id, thread_id, default_branch)"
            " VALUES (-1001, 0, 'main'), (-1001, 7, 'feature/x')"
        )
        conn.commit()

        before = conn.execute(
            "SELECT default_branch FROM tenants WHERE id = %s", (untouched,)
        ).fetchone()
        assert before == ("main",), "the shape a deployed database is in"

        apply_migrations_sync(conn, MIGRATIONS_DIR)
        conn.commit()

        rows = {
            str(r[0]): (r[1], r[2], r[3], r[4])
            for r in conn.execute(
                "SELECT id, default_branch, default_agent, default_model,"
                " default_effort FROM tenants"
            ).fetchall()
        }
        conn.execute("SELECT set_config('ctb.tenant_id', %s, false)", (str(untouched),))
        seats = dict(
            conn.execute(
                "SELECT thread_id, default_branch FROM chats ORDER BY thread_id"
            ).fetchall()
        )

    assert rows[str(untouched)] == (None, None, None, None), "follow the platform"
    assert rows[str(pinned)] == ("release", "codex", None, None), "a pin is kept"
    assert seats == {0: None, 7: "feature/x"}


def test_a_released_tenant_answers_with_the_platform_default(
    before_004: str,
) -> None:
    """The end-to-end point of the migration, in one assertion."""
    tenant_id = uuid.uuid4()
    with psycopg.connect(before_004) as conn:
        conn.execute(
            "INSERT INTO tenants (id, slug, name) VALUES (%s, 'a', 'A')", (tenant_id,)
        )
        conn.commit()
        apply_migrations_sync(conn, MIGRATIONS_DIR)
        conn.commit()
        branch = conn.execute(
            "SELECT default_branch FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()

    assert branch == (None,)
    row = TenantRow(id=tenant_id, slug="a", name="A", default_branch=None)
    settings = TenantSettings.of(row, _platform("dev"))
    assert settings.default_branch == "dev"


def _platform(branch: str) -> Settings:
    """Only the fields ``TenantSettings.of`` reads off the platform."""
    return cast(
        Settings,
        SimpleNamespace(
            default_agent="claude",
            default_model="opus-5-1m",
            default_effort="high",
            default_branch=branch,
            voice_enabled=False,
        ),
    )
