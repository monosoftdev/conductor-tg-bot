"""`python -m ctb.db.upgrade` — the migration step a deploy runs for itself.

The failure this covers is a release that pushes an image needing schema N at a
database still on N-1: the healthcheck fails and the bot is down until somebody
with a laptop runs `bootstrap`. So the tests that matter are the ones about
*when it does nothing*: an unconfigured service must deploy exactly as before,
and a broken DSN must abort the deploy rather than pass it.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

from ctb.db import upgrade as upgrade_mod
from ctb.db.migrate import MIGRATIONS_DIR, MigrationError
from ctb.db.upgrade import ADMIN_DSN_ENV, main
from tests.pg import admin_dsn

_BEFORE = ("001_init.sql", "002_ci_watch.sql", "003_topic_per_session.sql")
_SCRATCH = "ctb_upgrade_step"


def test_no_admin_dsn_is_a_successful_no_op(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A service that never opted in must still deploy."""

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("connected without a DSN")

    monkeypatch.delenv(ADMIN_DSN_ENV, raising=False)
    monkeypatch.setattr(upgrade_mod.psycopg, "connect", _forbidden)

    assert main([]) == 0
    assert ADMIN_DSN_ENV in capsys.readouterr().out


def test_blank_admin_dsn_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A variable pasted into a dashboard as whitespace is not a DSN."""
    monkeypatch.setenv(ADMIN_DSN_ENV, "   \n")
    monkeypatch.setattr(
        upgrade_mod.psycopg,
        "connect",
        lambda *a, **k: pytest.fail("connected on a blank DSN"),
    )

    assert main([]) == 0
    assert ADMIN_DSN_ENV in capsys.readouterr().out


def test_connection_failure_aborts_the_deploy_without_leaking_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 keeps the old instance up; the log must still be pasteable."""
    monkeypatch.setenv(ADMIN_DSN_ENV, "postgresql://postgres:hunter2@db:5432/ctb")
    monkeypatch.setattr(
        upgrade_mod.psycopg,
        "connect",
        lambda *a, **k: (_ for _ in ()).throw(
            psycopg.OperationalError(
                'connection to "postgresql://postgres:hunter2@db:5432/ctb" failed'
            )
        ),
    )

    assert main([]) == 1
    captured = capsys.readouterr()
    assert "hunter2" not in captured.err
    assert "***:***@" in captured.err


def test_a_failing_migration_aborts_the_deploy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        upgrade_mod,
        "upgrade",
        lambda dsn: (_ for _ in ()).throw(MigrationError("004 exploded")),
    )

    assert main(["--admin-dsn", "postgresql://postgres@db/ctb"]) == 1
    assert "004 exploded" in capsys.readouterr().err


@pytest.fixture
def before_the_last_migration(tmp_path: Path) -> Iterator[str]:
    """A scratch database carrying every migration but the newest one."""
    base = admin_dsn()
    try:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{_SCRATCH}"')
    except psycopg.OperationalError as exc:  # pragma: no cover - no server
        pytest.skip(f"no PostgreSQL for the upgrade test: {exc}")
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
        from ctb.db.migrate import apply_migrations_sync

        apply_migrations_sync(conn, older)
        conn.commit()
    yield dsn
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH}" WITH (FORCE)')


def _literal(sql: str) -> LiteralString:
    """Interpolated only from constants and a locally generated role name."""
    return cast(LiteralString, sql)


def _version(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.db
def test_pre_deploy_step_brings_a_stale_database_current(
    before_the_last_migration: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole point: schema 3 in, `REQUIRED_SCHEMA_VERSION` out."""
    from ctb.__main__ import REQUIRED_SCHEMA_VERSION

    assert _version(before_the_last_migration) == len(_BEFORE)
    monkeypatch.setenv(ADMIN_DSN_ENV, before_the_last_migration)

    assert main([]) == 0
    assert _version(before_the_last_migration) >= REQUIRED_SCHEMA_VERSION
    assert "applied" in capsys.readouterr().out


@pytest.mark.db
def test_running_it_twice_applies_nothing_the_second_time(
    before_the_last_migration: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every deploy runs it, and almost none of them carry a migration."""
    monkeypatch.setenv(ADMIN_DSN_ENV, before_the_last_migration)
    assert main([]) == 0
    before = _version(before_the_last_migration)

    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main([]) == 0
    assert "already up to date" in buffer.getvalue()
    assert _version(before_the_last_migration) == before


@pytest.mark.db
def test_the_worker_role_cannot_stand_in_for_the_admin_dsn(
    before_the_last_migration: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the variable is a superuser one: migrations are owner-level DDL.

    ``ctb_worker`` holds BYPASSRLS but owns nothing, so pointing the step at it
    fails loudly here instead of half-applying a migration on a deploy.
    """
    scratch_role = f"ctb_upgrade_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(before_the_last_migration, autocommit=True) as conn:
        conn.execute(
            _literal(f"CREATE ROLE \"{scratch_role}\" LOGIN PASSWORD 'x' BYPASSRLS")
        )
        # What 001 grants ctb_worker: read and write everything, create nothing.
        conn.execute(_literal(f'GRANT USAGE ON SCHEMA public TO "{scratch_role}"'))
        conn.execute(
            _literal(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                f'public TO "{scratch_role}"'
            )
        )
    try:
        parts = [
            p
            for p in before_the_last_migration.split()
            if not p.startswith(("user=", "password="))
        ]
        parts.extend([f"user={scratch_role}", "password=x"])
        monkeypatch.setenv(ADMIN_DSN_ENV, " ".join(parts))
        assert main([]) == 1
        assert _version(before_the_last_migration) == len(_BEFORE)
    finally:
        with psycopg.connect(before_the_last_migration, autocommit=True) as conn:
            conn.execute(_literal(f'DROP OWNED BY "{scratch_role}"'))
            conn.execute(_literal(f'DROP ROLE IF EXISTS "{scratch_role}"'))
