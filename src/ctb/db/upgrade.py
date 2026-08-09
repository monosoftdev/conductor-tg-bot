"""Apply pending migrations as a deploy step, so a release needs no laptop.

``ctb.db.bootstrap`` still owns the once-per-database part: creating ``ctb_app``
and ``ctb_worker`` needs a superuser and happens before the first deploy. What
happens on *every* release is narrower — apply the migration files this image
carries — and it has to happen between "push" and "the new image boots",
because boot refuses on a schema older than
:data:`ctb.__main__.REQUIRED_SCHEMA_VERSION`.

Leaving that to a human is the failure this module removes. The migration and
the image that needs it ship in the same commit, but the migration ran from
somebody's checkout, so the ordinary way to deploy was also the way to take the
bot down: push, watch *"1/1 replicas never became healthy"*, then find a
machine that can reach the database. Railway's ``preDeployCommand`` runs this
from the new image, with the service's variables, while the old instance is
still serving — which is exactly the window ``docs/DEPLOY.md`` already says the
old code tolerates.

Two deliberate exits:

``ADMIN_DATABASE_URL`` unset
    Nothing happens and the step succeeds. A deployment that never opted in
    behaves precisely as it did before this module existed, and the boot gate
    still names the manual command.

the migration fails
    Exit 1, which aborts the deploy. The old instance keeps running against the
    old schema, which it can, and the reason is in the deploy log instead of
    being a healthcheck timeout.

The DSN is read **here only**. It is not a :class:`ctb.settings.Settings`
field, so no handler and no pool can reach it: connecting as a superuser
silently disables row-level security, and this credential's whole job is to
live in a one-shot process and then exit with it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Final

import psycopg

from ctb.db.migrate import MigrationError, apply_migrations_sync

__all__ = ["ADMIN_DSN_ENV", "main", "upgrade"]

#: Set it to the superuser URL of the same database — on Railway, a variable
#: reference to the PostgreSQL service's ``DATABASE_URL``.
ADMIN_DSN_ENV: Final = "ADMIN_DATABASE_URL"

#: A pre-deploy step that hangs is indistinguishable from one that is working.
#: Long enough for a cold private-network DNS lookup, short enough that a wrong
#: host reports itself.
_CONNECT_TIMEOUT: Final = 15

_PASSWORD_RE: Final = re.compile(r"//[^/@\s]*:[^/@\s]*@")


def _redact(text: str) -> str:
    """psycopg quotes the connection string in some errors; passwords included."""
    return _PASSWORD_RE.sub("//***:***@", text)


def upgrade(admin_dsn: str) -> tuple[str, ...]:
    """Apply every migration this build carries that the database lacks.

    Returns the filenames applied, empty when there was nothing to do.
    Idempotent, and safe to run against a database already ahead of this build.
    """
    with psycopg.connect(admin_dsn, connect_timeout=_CONNECT_TIMEOUT) as conn:
        applied = apply_migrations_sync(conn)
        conn.commit()
    return tuple(migration.path.name for migration in applied)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply pending migrations.")
    parser.add_argument(
        "--admin-dsn",
        default=None,
        help=f"superuser connection URL; defaults to ${ADMIN_DSN_ENV}",
    )
    args = parser.parse_args(argv)

    dsn = (args.admin_dsn or os.environ.get(ADMIN_DSN_ENV) or "").strip()
    if not dsn:
        print(
            f"{ADMIN_DSN_ENV} is not set — no migrations applied. Set it on the "
            "service to migrate on deploy, or run `python -m ctb.db.bootstrap` "
            "by hand before this image boots."
        )
        return 0

    try:
        applied = upgrade(dsn)
    except (psycopg.Error, MigrationError, OSError) as exc:
        print(f"migrations failed: {_redact(str(exc))}", file=sys.stderr)
        return 1

    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("schema already up to date")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    sys.exit(main())
