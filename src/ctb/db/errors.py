"""The driver's exceptions, re-exported under names this project owns.

Nothing outside :mod:`ctb.db` imports psycopg. Tests assert against
``ctb.db.errors.UniqueViolation`` rather than the driver's own class, so the
next engine swap is a change to this one file instead of a grep across the
suite.

:class:`TenantScopeError` is ours, not the driver's: it is raised *before* a
statement is sent when a tenant-scoped pool has no tenant in scope. It lives
here so callers have one place to catch "the database refused this".
"""

from __future__ import annotations

from psycopg import errors as _pg
from psycopg.errors import (
    DatabaseError,
    DataError,
    ForeignKeyViolation,
    IntegrityError,
    NotNullViolation,
    OperationalError,
    ProgrammingError,
    SerializationFailure,
    UniqueViolation,
)

from psycopg_pool import PoolTimeout  # isort: skip

__all__ = [
    "CheckViolation",
    "DataError",
    "DatabaseError",
    "ForeignKeyViolation",
    "IntegrityError",
    "InsufficientPrivilege",
    "NotNullViolation",
    "OperationalError",
    "PoolTimeout",
    "ProgrammingError",
    "SerializationFailure",
    "TenantScopeError",
    "UniqueViolation",
]

#: A ``CHECK`` constraint rejected the row.
CheckViolation = _pg.CheckViolation
#: Row-level security (or a missing ``GRANT``) refused the statement.
InsufficientPrivilege = _pg.InsufficientPrivilege


class TenantScopeError(RuntimeError):
    """A tenant-scoped query was attempted with no tenant in scope.

    Raised by :class:`ctb.db.connection.Database` before the statement reaches
    the server. Row-level security would also reject it — this is the earlier,
    louder failure, so a missing scope surfaces as a named bug rather than as a
    query that mysteriously returns nothing.
    """
