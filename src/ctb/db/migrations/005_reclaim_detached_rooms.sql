-- ── 005: give back the rooms a wrong conclusion took away ──────────────────
-- `apply_marker` read a refused `editForumTopic` as proof a topic had been
-- deleted. In a *private* chat Telegram answers `TOPIC_ID_INVALID` for a thread
-- it merely will not let a bot rename, which is byte-for-byte what a deleted
-- one answers — so `room_gone` detached rooms that were alive and being worked
-- in. That reading is fixed in the same change as this file.
--
-- The detach also cleared `chats.workspace_id`, and `Route.claimable_thread`
-- reads exactly `session_id` and `workspace_id` to decide a thread is
-- Telegram's empty *New Chat* seat. So every room this happened to now looks
-- like scratch space, and the next line typed into one is still answered with
-- the new-workspace confirm card — a second paid container and a second
-- Conductor chat — however correct the code above it has become. The damage is
-- in the data and only data can undo it.
--
-- What a room *was* is recoverable without guessing: `outbound_prompts` records
-- the session every prompt was sent to and the `(chat_id, thread_id)` it was
-- sent from, and unlike `deliveries` it is never pruned in bulk. A thread that
-- something was once prompted from is not scratch space, whatever the routing
-- row has since lost.
--
-- Deliberately narrow. Only rows that are *both* pointers-less (an untouched
-- room keeps its binding and is skipped), only real topics (`thread_id <> 0` —
-- the linear seat and a group's General are seats and legitimately share), and
-- only where the workspace is still usable: a room whose workspace was archived
-- is finished, and starting something new from it is the right answer.
--
-- Nothing here binds a *session*. That is a decision with a live poller behind
-- it and belongs to the reopen tap, which renames the topic first and so finds
-- out whether the room is even there.

UPDATE chats c
   SET workspace_id = s.workspace_id,
       updated_at   = (EXTRACT(epoch FROM clock_timestamp()) * 1000)::bigint
  FROM (
        SELECT DISTINCT ON (p.tenant_id, p.chat_id, p.thread_id)
               p.tenant_id, p.chat_id, p.thread_id, p.session_id
          FROM outbound_prompts p
         WHERE p.chat_id IS NOT NULL AND p.thread_id <> 0
         ORDER BY p.tenant_id, p.chat_id, p.thread_id, p.created_at DESC
       ) last
  JOIN sessions   s ON s.tenant_id = last.tenant_id AND s.id = last.session_id
  JOIN workspaces w ON w.tenant_id = s.tenant_id    AND w.id = s.workspace_id
 WHERE c.tenant_id    = last.tenant_id
   AND c.chat_id      = last.chat_id
   AND c.thread_id    = last.thread_id
   AND c.thread_id   <> 0
   AND c.workspace_id IS NULL
   AND c.session_id   IS NULL
   AND w.status NOT IN ('archived', 'deleted');

-- Second pass, same rule, weaker evidence: a room that only ever *received*.
-- `deliveries` is pruned at seven days, so this recovers the recent ones and
-- silently recovers nothing older — which is the correct amount of confidence
-- to have about a room nobody ever typed in.

UPDATE chats c
   SET workspace_id = s.workspace_id,
       updated_at   = (EXTRACT(epoch FROM clock_timestamp()) * 1000)::bigint
  FROM (
        SELECT DISTINCT ON (d.tenant_id, d.chat_id, d.thread_id)
               d.tenant_id, d.chat_id, d.thread_id, d.session_id
          FROM deliveries d
         WHERE d.thread_id <> 0
         ORDER BY d.tenant_id, d.chat_id, d.thread_id, d.created_at DESC
       ) last
  JOIN sessions   s ON s.tenant_id = last.tenant_id AND s.id = last.session_id
  JOIN workspaces w ON w.tenant_id = s.tenant_id    AND w.id = s.workspace_id
 WHERE c.tenant_id    = last.tenant_id
   AND c.chat_id      = last.chat_id
   AND c.thread_id    = last.thread_id
   AND c.thread_id   <> 0
   AND c.workspace_id IS NULL
   AND c.session_id   IS NULL
   AND w.status NOT IN ('archived', 'deleted');
