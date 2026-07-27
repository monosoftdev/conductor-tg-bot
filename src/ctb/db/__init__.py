"""PostgreSQL persistence: connection, migrations, and the repo layer.

``db/repo/*`` is the seam if this ever needs to become Postgres — nothing above
it writes SQL directly.
"""

from __future__ import annotations

from typing import Final

from ctb.db.connection import (
    Database,
    get_database,
    now_ms,
    open_database,
    set_database,
)
from ctb.db.migrate import apply_migrations, current_schema_version

#: `thread_id` is NOT NULL everywhere; 0 means "no forum topic" — a DM, or the
#: supergroup's General cockpit. A nullable column inside a primary key would
#: silently break the uniqueness of the `(chat_id, thread_id)` routing key.
NO_THREAD_ID: Final = 0

#: `content_json` is capped at this many bytes per transcript message. The
#: content is the user's source code; we keep enough to render, not an archive.
MAX_CONTENT_BYTES: Final = 64 * 1024

#: `transcript_messages` older than this are pruned.
TRANSCRIPT_RETENTION_DAYS: Final = 30

__all__ = [
    "MAX_CONTENT_BYTES",
    "NO_THREAD_ID",
    "TRANSCRIPT_RETENTION_DAYS",
    "Database",
    "apply_migrations",
    "current_schema_version",
    "get_database",
    "now_ms",
    "open_database",
    "set_database",
]
