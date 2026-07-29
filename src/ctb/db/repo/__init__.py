"""Repository layer — the only place that writes SQL.

One module per table group. Every function takes a
:class:`ctb.db.connection.Database` as its first argument and returns frozen
row dataclasses, never live cursors, so nothing above this layer has to know
about psycopg.

Tenant isolation is enforced *below* this layer, by PostgreSQL row-level
security keyed on the ``ctb.tenant_id`` GUC that
:class:`ctb.db.connection.Database` publishes on every checkout. That is why no
statement here carries a ``WHERE tenant_id = ?``: a forgotten filter returns
zero rows rather than another tenant's data. The exceptions are deliberate and
named — :mod:`ctb.db.repo.tenancy`, ``sessions.list_bound``, the claim loops and
the prune helpers all run on the system pool, which bypasses RLS.

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
* :func:`ctb.db.repo.deliveries.claim` — a ``FOR UPDATE SKIP LOCKED`` claim
  that re-asserts ``state = 'pending'``, so two overlapping workers cannot both
  send the same chunk.
"""

from __future__ import annotations

from ctb.db.repo import (
    chats,
    ci,
    deliveries,
    events,
    lease,
    prompts,
    sessions,
    tenancy,
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
    "chats",
    "ci",
    "content_hash",
    "deliveries",
    "events",
    "lease",
    "prompts",
    "sessions",
    "tenancy",
    "transcript",
    "voice_inputs",
    "wizard",
    "workspaces",
]
