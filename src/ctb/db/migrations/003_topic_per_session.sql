-- ── 003: one topic per session ─────────────────────────────────────────────
-- `workspaces` used to own the room (chat_id, topic_id, topic_name,
-- topic_marker) and `sessions` only carried an address. Every session of a
-- workspace was therefore bound to one topic, `/fork` took that room over from
-- its sibling, and `/s` existed to move the pointer back by hand.
--
-- The room moves onto the session. A workspace becomes a *group* of rooms.
--
-- `workspaces.topic_id` / `topic_name` / `topic_marker` are deliberately kept
-- and stop being written: they are the only evidence a rollback would have.
-- A later migration drops them.
--
-- Order matters. The cleanup runs *before* the indexes, because every `/fork`
-- and every `/s` ever run left two bound sessions on one seat and the index
-- would fail on live data.

-- ── step 0: the duplicates vector 3 has been leaving since the first /fork ──
-- Nothing anywhere unbound the session a seat already held, so a seat could
-- carry several bound sessions: the supervisor polled all of them, all of them
-- delivered into the same room, and which one a *prompt* reached was decided by
-- `get_bound_for`'s ORDER BY created_at DESC.
--
-- Keep exactly the newest — the same tiebreak that lookup already applies, so
-- this does not move where any prompt goes. It only stops the losers polling.
-- Thread 0 is excluded throughout: the linear DM seat and a group's General are
-- seats, not rooms, and they legitimately hold one mutable binding.
UPDATE sessions s
   SET is_bound = false,
       updated_at = (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint
  FROM (
        SELECT tenant_id,
               id,
               row_number() OVER (
                   PARTITION BY tenant_id, chat_id, thread_id
                   ORDER BY created_at DESC, id DESC
               ) AS rank
          FROM sessions
         WHERE is_bound AND chat_id IS NOT NULL AND thread_id <> 0
       ) ranked
 WHERE ranked.tenant_id = s.tenant_id
   AND ranked.id = s.id
   AND ranked.rank > 1;

-- The inverse: one session is never in two rooms. A `/s` run in two different
-- topics pointed both `chats` rows at it. Keep the most recently touched.
UPDATE chats c
   SET session_id = NULL,
       updated_at = (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint
  FROM (
        SELECT tenant_id,
               chat_id,
               thread_id,
               row_number() OVER (
                   PARTITION BY tenant_id, session_id
                   ORDER BY updated_at DESC, chat_id, thread_id
               ) AS rank
          FROM chats
         WHERE session_id IS NOT NULL AND thread_id <> 0
       ) ranked
 WHERE ranked.tenant_id = c.tenant_id
   AND ranked.chat_id = c.chat_id
   AND ranked.thread_id = c.thread_id
   AND ranked.rank > 1;

-- ── the room, on the session ───────────────────────────────────────────────
-- `sessions.chat_id` / `thread_id` already carry the address; these two are the
-- presentation half `workspaces` used to hold. `archived_at` is terminal by the
-- user's choice, as distinct from `dead_at` (the session 404ed) — "is this the
-- last live one?" must not be fooled by either.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS topic_name   text;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS topic_marker text;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS archived_at  bigint;

-- Backfill: the workspace's room becomes the room of the session already bound
-- to it, and only that one. Every other session of that workspace stays
-- roomless and materialises a room lazily, the first time it is opened —
-- without that rule, `/attach` on a workspace with nine sessions would open
-- nine rooms nobody asked for.
--
-- The join is on the *thread*, not just the workspace: a session bound to a
-- different thread of the same workspace must not inherit the room. And on
-- `is_bound`, because several sessions can still sit on that thread — the ones
-- the step above just unbound. That is why the cleanup runs first: it is what
-- makes "the session bound to this thread" a single row.
UPDATE sessions s
   SET topic_name   = w.topic_name,
       topic_marker = w.topic_marker
  FROM workspaces w
 WHERE w.tenant_id = s.tenant_id
   AND s.workspace_id = w.id
   AND s.chat_id = w.chat_id
   AND s.thread_id = w.topic_id
   AND s.is_bound
   AND w.topic_id IS NOT NULL;

-- ── the guarantee ──────────────────────────────────────────────────────────
-- Per CLAUDE.md's second rule, the model is enforced by an index rather than by
-- discipline: a future regression fails loudly instead of quietly re-addressing
-- somebody's transcript.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_one_per_room
    ON sessions (tenant_id, chat_id, thread_id)
 WHERE is_bound AND chat_id IS NOT NULL AND thread_id <> 0;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chats_one_room_per_session
    ON chats (tenant_id, session_id)
 WHERE session_id IS NOT NULL AND thread_id <> 0;

-- `/board` stage 2 and `/done`'s last-one-left count both read a workspace's
-- sessions by room; `idx_sessions_workspace` already covers the lookup, this
-- covers "which room is this thread?" (`sessions.get_by_topic`).
CREATE INDEX IF NOT EXISTS idx_sessions_room
    ON sessions(tenant_id, chat_id, thread_id) WHERE chat_id IS NOT NULL;
