"""Repository layer — the only place that writes SQL.

One module per table group. Every function takes a
:class:`ctb.db.connection.Database` as its first argument and returns frozen
row dataclasses, never live cursors, so nothing above this layer has to know
about SQLite. That is the seam if this ever has to become Postgres.

Import the module, not the function, so the call site reads as the table it
touches::

    from ctb.db import repo

    row = await repo.sessions.get(db, session_id)
    result = await repo.transcript.advance_cursor(db, session_id, items)
    claimed = await repo.deliveries.claim(db, claim_id=repo.deliveries.new_claim_id())

Two operations here carry the whole design and are documented at length in
their own modules:

* :func:`ctb.db.repo.transcript.advance_cursor` — record messages, queue
  deliveries and move the cursor in one transaction, so the cursor can never
  advance past an unrecorded message and a replay can never duplicate one.
* :func:`ctb.db.repo.deliveries.claim` — the conditional ``pending -> sending``
  update, so two overlapping pollers cannot both send the same chunk.
"""

from __future__ import annotations

from ctb.db.repo import (
    allowlist,
    chats,
    deliveries,
    events,
    lease,
    prompts,
    sessions,
    transcript,
    voice_inputs,
    wizard,
    workspaces,
)
from ctb.db.repo._util import UNSET, Maybe, Unset, content_hash

__all__ = [
    "UNSET",
    "Maybe",
    "Unset",
    "allowlist",
    "chats",
    "content_hash",
    "deliveries",
    "events",
    "lease",
    "prompts",
    "sessions",
    "transcript",
    "voice_inputs",
    "wizard",
    "workspaces",
]
