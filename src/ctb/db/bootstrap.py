"""Create the two database roles, then run the migrations.

Run once per database, as a superuser:

.. code-block:: bash

   python -m ctb.db.bootstrap \\
       --admin-dsn "$ADMIN_DATABASE_URL" \\
       --app-password "$CTB_APP_PASSWORD" \\
       --worker-password "$CTB_WORKER_PASSWORD"

Two roles, because tenant isolation should be a property of the connection and
not of anyone's diligence:

``ctb_app``
    What every handler and the FSM storage connect as. Row-level security
    applies, so a query with no tenant filter returns nothing rather than
    another tenant's rows.

``ctb_worker``
    ``BYPASSRLS``. What the cross-tenant workers connect as — supervisor
    reconcile, the delivery and voice claim loops, prune, migrations, and the
    tenancy lookups that decide scope in the first place.

``ctb_app`` is deliberately **not** granted membership in ``ctb_worker``: there
is no ``SET ROLE`` path from the request path to the bypass role.

Creating a role and granting ``BYPASSRLS`` both need superuser. Ordinary
deploys only run migrations, which grant to roles that already exist.
"""

from __future__ import annotations

import argparse
import sys
from typing import Final

import psycopg
from psycopg import sql

from ctb.db.migrate import apply_migrations_sync

__all__ = ["APP_ROLE", "WORKER_ROLE", "bootstrap", "main"]

APP_ROLE: Final = "ctb_app"
WORKER_ROLE: Final = "ctb_worker"


def _ensure_role(
    conn: psycopg.Connection[object], name: str, password: str, *, bypass_rls: bool
) -> None:
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)
    ).fetchone()
    identifier = sql.Identifier(name)
    if exists is None:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                identifier, sql.Literal(password)
            )
        )
    else:
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                identifier, sql.Literal(password)
            )
        )
    conn.execute(
        sql.SQL("ALTER ROLE {} WITH {}").format(
            identifier, sql.SQL("BYPASSRLS" if bypass_rls else "NOBYPASSRLS")
        )
    )


def bootstrap(
    admin_dsn: str,
    *,
    app_password: str,
    worker_password: str,
    database: str | None = None,
) -> None:
    """Create/refresh both roles and bring the schema up to date."""
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        _ensure_role(conn, APP_ROLE, app_password, bypass_rls=False)
        _ensure_role(conn, WORKER_ROLE, worker_password, bypass_rls=True)
        target = database or conn.info.dbname
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                sql.Identifier(target),
                sql.Identifier(APP_ROLE),
                sql.Identifier(WORKER_ROLE),
            )
        )

    with psycopg.connect(admin_dsn) as conn:
        apply_migrations_sync(conn)
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-dsn", required=True, help="superuser connection URL")
    parser.add_argument("--app-password", required=True)
    parser.add_argument("--worker-password", required=True)
    parser.add_argument("--database", default=None, help="defaults to the DSN's")
    args = parser.parse_args(argv)
    bootstrap(
        args.admin_dsn,
        app_password=args.app_password,
        worker_password=args.worker_password,
        database=args.database,
    )
    print("roles ensured and migrations applied")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    sys.exit(main())
