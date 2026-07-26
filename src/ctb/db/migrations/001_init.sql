-- 001_init — the whole schema from PLAN.md §Persistence.
--
-- Conventions that every repo module must follow:
--
--   * Every `*_at` / `*_ms` column holds INTEGER epoch **milliseconds**, UTC.
--     The one exception is `transcript_messages.received_at`, which keeps the
--     API's own timestamp string verbatim; `received_at_ms` is the parsed copy
--     that the 30-day prune and its index use.
--   * `thread_id` is NOT NULL and uses 0 for "no forum topic" (a DM, or the
--     supergroup's General). SQLite permits NULLs in a PRIMARY KEY column, so a
--     nullable thread_id would silently destroy the uniqueness of the routing
--     key `(chat_id, thread_id)`.
--   * Booleans are INTEGER 0/1.
--   * Conductor ids are TEXT UUIDs. `outbound_prompts.message_id` is the
--     caller-supplied idempotency key we POST — it is generated locally, before
--     any HTTP call, and never changes.
--   * Timestamp defaults use the julianday form rather than `unixepoch(...,
--     'subsec')`, which needs SQLite >= 3.42 and is absent from Debian's 3.40.

-- ── who may drive the bot ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS allowed_users (
    user_id     INTEGER PRIMARY KEY,
    is_owner    INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
    username    TEXT,
    note        TEXT,
    added_by    INTEGER,
    added_at    INTEGER NOT NULL
                DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER))
);

-- ── routing: one row per (chat, forum topic) ────────────────────────────────
-- The address of a prompt is the topic your thumb is in. `kind` distinguishes
-- the cockpit (General, search-only, never a prompt target) from a workspace
-- topic and from the degraded DM mode.

CREATE TABLE IF NOT EXISTS chats (
    chat_id             INTEGER NOT NULL,
    thread_id           INTEGER NOT NULL DEFAULT 0,
    kind                TEXT    NOT NULL DEFAULT 'topic'
                        CHECK (kind IN ('general', 'topic', 'dm')),

    -- binding
    workspace_id        TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    session_id          TEXT REFERENCES sessions(id)   ON DELETE SET NULL,

    -- remembered defaults for the zero-tap /new path
    default_project_id  TEXT,
    default_branch      TEXT,
    default_agent       TEXT,
    default_model       TEXT,
    default_effort      TEXT,

    -- presentation
    verbosity           TEXT NOT NULL DEFAULT 'normal'
                        CHECK (verbosity IN ('quiet', 'normal', 'verbose')),
    notify              TEXT NOT NULL DEFAULT 'quiet'
                        CHECK (notify IN ('loud', 'quiet', 'off')),
    -- Focus rule: the session you last prompted is loud until this instant.
    focus_until_at      INTEGER,

    last_prompt_at      INTEGER,
    created_at          INTEGER NOT NULL
                        DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    updated_at          INTEGER NOT NULL
                        DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),

    PRIMARY KEY (chat_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_chats_workspace ON chats(workspace_id);
CREATE INDEX IF NOT EXISTS idx_chats_session   ON chats(session_id);

-- ── workspaces: local cache of the Conductor resource + its topic ───────────

CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,           -- Conductor workspaceId
    project_id      TEXT,
    name            TEXT,
    repo_url        TEXT,
    branch          TEXT,
    agent           TEXT,
    model           TEXT,
    effort          TEXT,
    deep_link       TEXT,                       -- "Open in Conductor"

    status          TEXT,                       -- initializing|ready|sleeping|…
    lifecycle_step  TEXT,
    last_status_at  INTEGER,

    -- Telegram forum topic bound to this workspace
    chat_id         INTEGER,
    topic_id        INTEGER,
    topic_name      TEXT,
    topic_marker    TEXT,                       -- last applied TopicMarker

    -- POST /workspaces has no idempotency key: the nonce is embedded in the
    -- generated workspace name so a create that timed out can be reconciled
    -- against GET /projects/{id}/workspaces instead of blind-retried.
    create_nonce    TEXT,
    created_at      INTEGER NOT NULL
                    DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    updated_at      INTEGER NOT NULL
                    DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    init_started_at INTEGER,
    ready_at        INTEGER,
    archived_at     INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_nonce
    ON workspaces(create_nonce) WHERE create_nonce IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workspaces_topic   ON workspaces(chat_id, topic_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_project ON workspaces(project_id);

-- ── sessions: the cursor and the turn machine's persisted context ───────────
-- cursor_session_index is the REAL cursor; cursor_message_id is the
-- optimisation that lets us pass `after=`. sessionIndex is not gapless, so a
-- gap never implies a missed message.

CREATE TABLE IF NOT EXISTS sessions (
    id                          TEXT PRIMARY KEY,   -- Conductor sessionId
    workspace_id                TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    title                       TEXT,
    agent                       TEXT,
    model                       TEXT,
    effort                      TEXT,
    fast_mode                   INTEGER NOT NULL DEFAULT 0 CHECK (fast_mode IN (0, 1)),

    -- routing
    chat_id                     INTEGER,
    thread_id                   INTEGER NOT NULL DEFAULT 0,
    is_bound                    INTEGER NOT NULL DEFAULT 1 CHECK (is_bound IN (0, 1)),

    -- cursor
    cursor_message_id           TEXT,
    cursor_session_index        INTEGER NOT NULL DEFAULT -1,
    -- First bind seeks to the end rather than replaying history.
    seeded                      INTEGER NOT NULL DEFAULT 0 CHECK (seeded IN (0, 1)),

    -- turn machine (mirrors ctb.turn.state.TurnContext)
    turn_state                  TEXT NOT NULL DEFAULT 'IDLE'
                                CHECK (turn_state IN (
                                    'IDLE', 'SUBMIT_PENDING', 'QUEUED', 'WAKING',
                                    'WORKING', 'DRAINING', 'CANCELLING', 'ERROR',
                                    'DEAD')),
    start_witnessed             INTEGER NOT NULL DEFAULT 0
                                CHECK (start_witnessed IN (0, 1)),
    index_at_post               INTEGER,
    last_delta_at               INTEGER,
    entered_state_at            INTEGER,
    turn_started_at             INTEGER,
    consecutive_idle            INTEGER NOT NULL DEFAULT 0,
    consecutive_status_failures INTEGER NOT NULL DEFAULT 0,
    cursor_only                 INTEGER NOT NULL DEFAULT 0
                                CHECK (cursor_only IN (0, 1)),
    idle_decay_step             INTEGER NOT NULL DEFAULT 0,
    poll_interval_ms            INTEGER NOT NULL DEFAULT 20000,

    -- UX state
    status_card_msg_id          INTEGER,
    waking_notified             INTEGER NOT NULL DEFAULT 0
                                CHECK (waking_notified IN (0, 1)),
    warned_no_output_at         INTEGER,
    tool_calls                  INTEGER NOT NULL DEFAULT 0,

    -- last observed API state
    last_status                 TEXT,
    error_message               TEXT,
    last_error                  TEXT,
    last_error_at               INTEGER,

    last_prompt_at              INTEGER,
    dead_at                     INTEGER,
    created_at                  INTEGER NOT NULL
                                DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    updated_at                  INTEGER NOT NULL
                                DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER))
);

CREATE INDEX IF NOT EXISTS idx_sessions_chat_thread ON sessions(chat_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace   ON sessions(workspace_id);
-- The supervisor reconciles bindings -> task set every 5s off this one.
CREATE INDEX IF NOT EXISTS idx_sessions_bound
    ON sessions(is_bound, turn_state) WHERE is_bound = 1;

-- ── outbound prompts: the idempotency ledger ────────────────────────────────
-- The row is written BEFORE the HTTP call. An ambiguous outcome is retried
-- forever with the same message_id — verified to dedupe server-side. A crash
-- between the write and the response is recovered on boot (transition 3).

CREATE TABLE IF NOT EXISTS outbound_prompts (
    message_id      TEXT PRIMARY KEY,           -- the Conductor idempotency key
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    chat_id         INTEGER,
    thread_id       INTEGER NOT NULL DEFAULT 0,
    tg_message_id   INTEGER,                    -- the Telegram message we echoed
    body            TEXT NOT NULL,              -- prompt text (user source; never logged)
    index_at_post   INTEGER,                    -- max sessionIndex when POSTed
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'posted', 'witnessed',
                                     'abandoned', 'failed')),
    post_state      TEXT,                       -- API's queued|sent
    turn_id         TEXT,                       -- == message_id once witnessed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      INTEGER NOT NULL
                    DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    posted_at       INTEGER,
    witnessed_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_outbound_session_state
    ON outbound_prompts(session_id, state);
CREATE INDEX IF NOT EXISTS idx_outbound_pending
    ON outbound_prompts(state, created_at) WHERE state IN ('pending', 'posted');

-- ── transcript messages: the source of truth for content ────────────────────
-- PK is (session_id, message_id) where message_id is the ENVELOPE id
-- ("<sessionId>:<seq>:<sub>"), not anything we generated. INSERT OR IGNORE
-- makes replay and overlapping pollers harmless.

CREATE TABLE IF NOT EXISTS transcript_messages (
    session_id        TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id        TEXT NOT NULL,
    session_index     INTEGER NOT NULL,
    type              TEXT,
    -- content is untyped in the API. Stored verbatim as JSON, capped at 64 KB;
    -- content_truncated records that the cap bit.
    content_json      TEXT,
    content_truncated INTEGER NOT NULL DEFAULT 0
                      CHECK (content_truncated IN (0, 1)),
    content_bytes     INTEGER NOT NULL DEFAULT 0,
    turn_id           TEXT,                     -- content.turnId
    content_id        TEXT,                     -- content.id (our messageId on echoes)
    received_at       TEXT,                     -- verbatim API timestamp
    received_at_ms    INTEGER NOT NULL DEFAULT 0,
    inserted_at       INTEGER NOT NULL
                      DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),

    PRIMARY KEY (session_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_transcript_session_index
    ON transcript_messages(session_id, session_index);
CREATE INDEX IF NOT EXISTS idx_transcript_turn
    ON transcript_messages(session_id, turn_id);
-- Prune: DELETE FROM transcript_messages WHERE received_at_ms < :cutoff
CREATE INDEX IF NOT EXISTS idx_transcript_received
    ON transcript_messages(received_at_ms);

-- ── deliveries: one row per chunk per destination chat ──────────────────────
-- Deliberately at-least-once: on boot, rows left in 'sending' are re-sent,
-- guarded by content_hash against the preceding 'sent' row. A rare duplicate
-- beats a silently lost reply.
--
-- No FK to transcript_messages: the 30-day prune must not cascade delivery
-- history away, and synthetic (non-transcript) deliveries are legal.

CREATE TABLE IF NOT EXISTS deliveries (
    session_id     TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id     TEXT NOT NULL,
    part_index     INTEGER NOT NULL DEFAULT 0,
    chat_id        INTEGER NOT NULL,
    thread_id      INTEGER NOT NULL DEFAULT 0,

    session_index  INTEGER NOT NULL DEFAULT 0,  -- claim ordering
    kind           TEXT NOT NULL DEFAULT 'text'
                   CHECK (kind IN ('text', 'code', 'document', 'activity')),
    state          TEXT NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending', 'sending', 'sent', 'skipped',
                                    'failed')),
    claim_id       TEXT,
    content_hash   TEXT,
    payload_json   TEXT,                        -- the rendered Block
    tg_message_id  INTEGER,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     INTEGER NOT NULL
                   DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    updated_at     INTEGER NOT NULL
                   DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    sent_at        INTEGER,

    PRIMARY KEY (session_id, message_id, part_index, chat_id)
);

-- The claim query: pending rows in (session_index, part_index) order.
CREATE INDEX IF NOT EXISTS idx_deliveries_claim
    ON deliveries(state, session_index, part_index);
CREATE INDEX IF NOT EXISTS idx_deliveries_session
    ON deliveries(session_id, state, session_index, part_index);
CREATE INDEX IF NOT EXISTS idx_deliveries_claim_id
    ON deliveries(claim_id) WHERE claim_id IS NOT NULL;

-- ── wizard state: aiogram FSM, DB-backed so wizards survive a restart ───────

CREATE TABLE IF NOT EXISTS wizard_state (
    chat_id     INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL DEFAULT 0,
    user_id     INTEGER NOT NULL,
    state_key   TEXT,
    data_json   TEXT,
    -- The wizard edits one message in place; this is that message.
    tg_message_id INTEGER,
    updated_at  INTEGER NOT NULL
                DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    expires_at  INTEGER,

    PRIMARY KEY (chat_id, thread_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_wizard_expires ON wizard_state(expires_at);

-- ── singleton lease: only one supervisor may poll ──────────────────────────
-- 15s TTL heartbeated every 5s. The supervisor refuses to spawn without it and
-- cancels every task if it loses it.

CREATE TABLE IF NOT EXISTS singleton_lease (
    name         TEXT PRIMARY KEY,              -- 'supervisor'
    holder       TEXT NOT NULL,                 -- instance id
    acquired_at  INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL
);

-- ── api_events: ring buffer behind /health ─────────────────────────────────
-- Pruned by row id: DELETE FROM api_events
--   WHERE id <= (SELECT MAX(id) FROM api_events) - :keep;

CREATE TABLE IF NOT EXISTS api_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            INTEGER NOT NULL
                  DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    method        TEXT,
    endpoint      TEXT,
    status_code   INTEGER,
    duration_ms   INTEGER,
    attempt       INTEGER NOT NULL DEFAULT 1,
    ok            INTEGER NOT NULL DEFAULT 1 CHECK (ok IN (0, 1)),
    error         TEXT,
    circuit_state TEXT,
    request_id    TEXT,
    session_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_events_at ON api_events(at);

-- ── unknown_content_types: what the renderer could not classify ────────────
-- Counted, never crashed. Deliberately stores a POINTER to a sample message
-- rather than its content — transcript content is the user's source code.

CREATE TABLE IF NOT EXISTS unknown_content_types (
    type              TEXT NOT NULL,
    shape_signature   TEXT NOT NULL,            -- sorted key path digest
    count             INTEGER NOT NULL DEFAULT 0,
    sample_session_id TEXT,
    sample_message_id TEXT,
    first_seen_at     INTEGER NOT NULL
                      DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
    last_seen_at      INTEGER NOT NULL
                      DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),

    PRIMARY KEY (type, shape_signature)
);

CREATE INDEX IF NOT EXISTS idx_unknown_last_seen ON unknown_content_types(last_seen_at);
