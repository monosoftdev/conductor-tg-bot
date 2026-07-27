-- 001_init — the whole schema: tenancy, runtime, and row-level security.
--
-- Conventions every repo module follows:
--
--   * Every `*_at` / `*_ms` column holds `bigint` epoch **milliseconds**, UTC.
--     The one exception is `transcript_messages.received_at`, which keeps the
--     API's own timestamp string verbatim; `received_at_ms` is the parsed copy
--     that the 30-day prune and its index use.
--   * Defaults use `clock_timestamp()`, never `now()`. `now()` is fixed at
--     transaction start, so a batch inserted by one `advance_cursor` call would
--     share a `created_at` and flip every `ORDER BY created_at` tiebreak.
--   * `thread_id` is NOT NULL and uses 0 for "no forum topic" (a DM, or the
--     supergroup's General), so the routing key `(chat_id, thread_id)` never
--     contains a NULL.
--   * Telegram ids exceed int32. Every id column is `bigint`.
--   * Conductor ids are TEXT UUIDs. `outbound_prompts.message_id` is the
--     caller-supplied idempotency key we POST — generated locally, before any
--     HTTP call, and never changed.
--
-- Tenancy. Every tenant-scoped table carries `tenant_id uuid NOT NULL`
-- defaulted from the `ctb.tenant_id` GUC, has row-level security ENABLEd and
-- FORCEd, and one policy filtering on that GUC. Repo SQL therefore contains no
-- `WHERE tenant_id = ?`: a forgotten filter returns zero rows instead of
-- another tenant's data. `ctb_app` runs under those policies; `ctb_worker` has
-- BYPASSRLS and is used only by the cross-tenant workers (supervisor reconcile,
-- delivery/voice claim loops, prune, migrations, tenancy lookups).

-- ── tenants ─────────────────────────────────────────────────────────────────
-- The tenancy tables decide scope, so they cannot themselves be scoped by it.
-- They have no RLS and are granted to the worker role only; handlers reach them
-- through TenantMiddleware, never directly.

CREATE TABLE IF NOT EXISTS tenants (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Short, human-typable, safe to log. Never the tenant's name.
    slug                   text NOT NULL UNIQUE,
    name                   text NOT NULL,
    status                 text NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'active', 'suspended',
                                             'deleted')),

    conductor_api_url      text NOT NULL DEFAULT 'https://api.conductor.build/v0',
    -- Sealed by ctb.crypto.SecretBox: AES-GCM, AAD-bound to (kid, tenant, use).
    -- Plaintext exists only inside ClientPool.get(), never on disk or in a log.
    conductor_key_ct       bytea,
    conductor_key_kid      text,
    conductor_key_fp       text,
    conductor_key_at       bigint,
    conductor_key_by       bigint,
    elevenlabs_key_ct      bytea,
    elevenlabs_key_kid     text,
    elevenlabs_key_fp      text,
    elevenlabs_key_at      bigint,

    default_agent          text NOT NULL DEFAULT 'claude',
    default_model          text NOT NULL DEFAULT 'opus-5-1m',
    default_effort         text NOT NULL DEFAULT 'high',
    default_branch         text NOT NULL DEFAULT 'main',
    voice_enabled          boolean NOT NULL DEFAULT false,
    voice_mode             text NOT NULL DEFAULT 'prompts'
                           CHECK (voice_mode IN ('shadow', 'prompts', 'commands')),

    -- Fair-share ceilings. The supervisor and the cursor read these directly.
    max_pollers            integer NOT NULL DEFAULT 20 CHECK (max_pollers > 0),
    max_pending_deliveries integer NOT NULL DEFAULT 5000
                           CHECK (max_pending_deliveries > 0),
    max_workspaces         integer NOT NULL DEFAULT 50 CHECK (max_workspaces > 0),

    -- Set when the Conductor key is rejected. Stops this tenant's pollers on
    -- the next reconcile pass without touching anyone else's.
    auth_failed_at         bigint,
    auth_failed_reason     text,

    created_at             bigint NOT NULL
                           DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    updated_at             bigint NOT NULL
                           DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    suspended_at           bigint,
    deleted_at             bigint
);

CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status);

-- Many Telegram users drive one tenant: a co-founder is a row here, not a
-- second deployment. This table replaces the old global `allowed_users`.
CREATE TABLE IF NOT EXISTS tenant_members (
    tenant_id uuid   NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id   bigint NOT NULL,
    role      text   NOT NULL DEFAULT 'member'
              CHECK (role IN ('owner', 'admin', 'member')),
    username  text,
    note      text,
    added_by  bigint,
    added_at  bigint NOT NULL
              DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    PRIMARY KEY (tenant_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_members_user ON tenant_members(user_id);
CREATE INDEX IF NOT EXISTS idx_tenant_members_owner
    ON tenant_members(tenant_id) WHERE role IN ('owner', 'admin');

-- A Telegram chat maps to at most one tenant, ever. That is why `chat_id` is
-- the primary key and not half of a composite: resolution is one point lookup
-- with no ambiguity, and every existing `chat_id` column is therefore already
-- globally tenant-unique.
CREATE TABLE IF NOT EXISTS tenant_chats (
    chat_id     bigint PRIMARY KEY,
    tenant_id   uuid   NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind        text   NOT NULL DEFAULT 'group'
                CHECK (kind IN ('group', 'dm')),
    is_primary  boolean NOT NULL DEFAULT false,
    title       text,
    bound_by    bigint,
    bound_at    bigint NOT NULL
                DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    verified_at bigint
);

CREATE INDEX IF NOT EXISTS idx_tenant_chats_tenant ON tenant_chats(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_chats_primary
    ON tenant_chats(tenant_id) WHERE is_primary;

-- Single-use, hashed, short-lived. Backs `/setup <code>` group binding today
-- and the out-of-band key-entry page later. The plaintext token is shown once
-- and never stored.
CREATE TABLE IF NOT EXISTS enrollment_tokens (
    token_hash text PRIMARY KEY,
    tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id    bigint NOT NULL,
    purpose    text NOT NULL CHECK (purpose IN ('bind_chat', 'set_key')),
    created_at bigint NOT NULL
               DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    expires_at bigint NOT NULL,
    used_at    bigint
);

CREATE INDEX IF NOT EXISTS idx_enrollment_expires ON enrollment_tokens(expires_at);

-- ── routing: one row per (chat, forum topic) ────────────────────────────────
-- The address of a prompt is the topic your thumb is in. `kind` distinguishes
-- the cockpit (General, search-only, never a prompt target) from a workspace
-- topic and from the degraded DM mode.

CREATE TABLE IF NOT EXISTS chats (
    tenant_id           uuid NOT NULL
                        DEFAULT current_setting('ctb.tenant_id', true)::uuid
                        REFERENCES tenants(id) ON DELETE CASCADE,
    chat_id             bigint NOT NULL,
    thread_id           bigint NOT NULL DEFAULT 0,
    kind                text   NOT NULL DEFAULT 'topic'
                        CHECK (kind IN ('general', 'topic', 'dm')),

    -- binding (composite FKs added after the referenced tables exist)
    workspace_id        text,
    session_id          text,

    -- remembered defaults for the zero-tap /new path
    default_project_id  text,
    default_branch      text,
    default_agent       text,
    default_model       text,
    default_effort      text,

    -- presentation
    verbosity           text NOT NULL DEFAULT 'normal'
                        CHECK (verbosity IN ('quiet', 'normal', 'verbose')),
    notify              text NOT NULL DEFAULT 'quiet'
                        CHECK (notify IN ('loud', 'quiet', 'off')),
    -- Focus rule: the session you last prompted is loud until this instant.
    focus_until_at      bigint,

    last_prompt_at      bigint,
    created_at          bigint NOT NULL
                        DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    updated_at          bigint NOT NULL
                        DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,

    PRIMARY KEY (chat_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_chats_tenant     ON chats(tenant_id);
CREATE INDEX IF NOT EXISTS idx_chats_workspace  ON chats(tenant_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_chats_session    ON chats(tenant_id, session_id);

-- ── workspaces: local cache of the Conductor resource + its topic ───────────

CREATE TABLE IF NOT EXISTS workspaces (
    tenant_id       uuid NOT NULL
                    DEFAULT current_setting('ctb.tenant_id', true)::uuid
                    REFERENCES tenants(id) ON DELETE CASCADE,
    id              text NOT NULL,              -- Conductor workspaceId
    project_id      text,
    name            text,
    repo_url        text,
    branch          text,
    agent           text,
    model           text,
    effort          text,
    deep_link       text,                       -- "Open in Conductor"

    status          text,                       -- initializing|ready|sleeping|…
    lifecycle_step  text,
    last_status_at  bigint,

    -- Telegram forum topic bound to this workspace
    chat_id         bigint,
    topic_id        bigint,
    topic_name      text,
    topic_marker    text,                       -- last applied TopicMarker

    -- POST /workspaces has no idempotency key: the nonce is embedded in the
    -- generated workspace name so a create that timed out can be reconciled
    -- against GET /projects/{id}/workspaces instead of blind-retried.
    create_nonce    text,
    created_at      bigint NOT NULL
                    DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    updated_at      bigint NOT NULL
                    DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    init_started_at bigint,
    ready_at        bigint,
    archived_at     bigint,

    PRIMARY KEY (tenant_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_nonce
    ON workspaces(tenant_id, create_nonce) WHERE create_nonce IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workspaces_topic
    ON workspaces(tenant_id, chat_id, topic_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_project
    ON workspaces(tenant_id, project_id);

-- ── sessions: the cursor and the turn machine's persisted context ───────────
-- cursor_session_index is the REAL cursor; cursor_message_id is the
-- optimisation that lets us pass `after=`. sessionIndex is not gapless, so a
-- gap never implies a missed message.

CREATE TABLE IF NOT EXISTS sessions (
    tenant_id                   uuid NOT NULL
                                DEFAULT current_setting('ctb.tenant_id', true)::uuid
                                REFERENCES tenants(id) ON DELETE CASCADE,
    id                          text NOT NULL,      -- Conductor sessionId
    workspace_id                text,
    title                       text,
    agent                       text,
    model                       text,
    effort                      text,
    fast_mode                   boolean NOT NULL DEFAULT false,

    -- routing
    chat_id                     bigint,
    thread_id                   bigint NOT NULL DEFAULT 0,
    is_bound                    boolean NOT NULL DEFAULT true,

    -- cursor
    cursor_message_id           text,
    cursor_session_index        bigint NOT NULL DEFAULT -1,
    -- First bind seeks to the end rather than replaying history.
    seeded                      boolean NOT NULL DEFAULT false,

    -- turn machine (mirrors ctb.turn.state.TurnContext)
    turn_state                  text NOT NULL DEFAULT 'IDLE'
                                CHECK (turn_state IN (
                                    'IDLE', 'SUBMIT_PENDING', 'QUEUED', 'WAKING',
                                    'WORKING', 'DRAINING', 'CANCELLING', 'ERROR',
                                    'DEAD')),
    start_witnessed             boolean NOT NULL DEFAULT false,
    index_at_post               bigint,
    last_delta_at               bigint,
    entered_state_at            bigint,
    turn_started_at             bigint,
    consecutive_idle            integer NOT NULL DEFAULT 0,
    consecutive_status_failures integer NOT NULL DEFAULT 0,
    cursor_only                 boolean NOT NULL DEFAULT false,
    idle_decay_step             integer NOT NULL DEFAULT 0,
    poll_interval_ms            bigint NOT NULL DEFAULT 20000,

    -- UX state
    status_card_msg_id          bigint,
    waking_notified             boolean NOT NULL DEFAULT false,
    warned_no_output_at         bigint,
    tool_calls                  integer NOT NULL DEFAULT 0,

    -- last observed API state
    last_status                 text,
    error_message               text,
    last_error                  text,
    last_error_at               bigint,

    last_prompt_at              bigint,
    dead_at                     bigint,
    created_at                  bigint NOT NULL
                                DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    updated_at                  bigint NOT NULL
                                DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,

    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES workspaces(tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_chat_thread
    ON sessions(tenant_id, chat_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace
    ON sessions(tenant_id, workspace_id);
-- The supervisor reconciles bindings -> task set every 5s off this one.
CREATE INDEX IF NOT EXISTS idx_sessions_bound
    ON sessions(tenant_id, turn_state, updated_at) WHERE is_bound;

-- `chats` points at a workspace and a session; the composite FKs keep a chat
-- from ever referencing another tenant's rows. ON DELETE SET NULL preserves the
-- routing row when the thing it points at goes away.
-- `ON DELETE SET NULL (column)` is the load-bearing detail: the plain form
-- nulls every referencing column, and `tenant_id` is NOT NULL.
DO $chatfks$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chats_workspace_fkey') THEN
        ALTER TABLE chats
            ADD CONSTRAINT chats_workspace_fkey
            FOREIGN KEY (tenant_id, workspace_id)
            REFERENCES workspaces(tenant_id, id)
            ON DELETE SET NULL (workspace_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chats_session_fkey') THEN
        ALTER TABLE chats
            ADD CONSTRAINT chats_session_fkey
            FOREIGN KEY (tenant_id, session_id)
            REFERENCES sessions(tenant_id, id)
            ON DELETE SET NULL (session_id);
    END IF;
END
$chatfks$;

-- ── outbound prompts: the idempotency ledger ────────────────────────────────
-- The row is written BEFORE the HTTP call. An ambiguous outcome is retried
-- forever with the same message_id — verified to dedupe server-side. A crash
-- between the write and the response is recovered on boot (transition 3).

CREATE TABLE IF NOT EXISTS outbound_prompts (
    tenant_id       uuid NOT NULL
                    DEFAULT current_setting('ctb.tenant_id', true)::uuid
                    REFERENCES tenants(id) ON DELETE CASCADE,
    message_id      text NOT NULL,              -- the Conductor idempotency key
    session_id      text NOT NULL,
    chat_id         bigint,
    thread_id       bigint NOT NULL DEFAULT 0,
    tg_message_id   bigint,                     -- the Telegram message we echoed
    user_id         bigint,                     -- who asked; attribution only
    body            text NOT NULL,              -- prompt text (never logged)
    index_at_post   bigint,                     -- max sessionIndex when POSTed
    state           text NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'posted', 'witnessed',
                                     'abandoned', 'failed')),
    post_state      text,                       -- API's queued|sent
    turn_id         text,                       -- == message_id once witnessed
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text,
    created_at      bigint NOT NULL
                    DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    posted_at       bigint,
    witnessed_at    bigint,

    PRIMARY KEY (tenant_id, message_id),
    FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions(tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_outbound_session_state
    ON outbound_prompts(tenant_id, session_id, state);
CREATE INDEX IF NOT EXISTS idx_outbound_pending
    ON outbound_prompts(state, created_at) WHERE state IN ('pending', 'posted');

-- ── transcript messages: the source of truth for content ────────────────────
-- PK is (tenant_id, session_id, message_id) where message_id is the ENVELOPE id
-- ("<sessionId>:<seq>:<sub>"), not anything we generated. ON CONFLICT DO
-- NOTHING makes replay and overlapping pollers harmless.

CREATE TABLE IF NOT EXISTS transcript_messages (
    tenant_id         uuid NOT NULL
                      DEFAULT current_setting('ctb.tenant_id', true)::uuid
                      REFERENCES tenants(id) ON DELETE CASCADE,
    session_id        text NOT NULL,
    message_id        text NOT NULL,
    session_index     bigint NOT NULL,
    type              text,
    -- content is untyped in the API. Stored verbatim as JSON text, capped at
    -- 64 KB; content_truncated records that the cap bit. Deliberately `text`
    -- and not `jsonb`: agent output can contain NUL escapes, which jsonb rejects,
    -- and losing a delivery to a NUL escape is not a trade worth making.
    content_json      text,
    content_truncated boolean NOT NULL DEFAULT false,
    content_bytes     integer NOT NULL DEFAULT 0,
    turn_id           text,                     -- content.turnId
    content_id        text,                     -- content.id (our messageId on echoes)
    received_at       text,                     -- verbatim API timestamp
    received_at_ms    bigint NOT NULL DEFAULT 0,
    inserted_at       bigint NOT NULL
                      DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,

    PRIMARY KEY (tenant_id, session_id, message_id),
    FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions(tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_transcript_session_index
    ON transcript_messages(tenant_id, session_id, session_index);
CREATE INDEX IF NOT EXISTS idx_transcript_turn
    ON transcript_messages(tenant_id, session_id, turn_id);
-- Prune: DELETE FROM transcript_messages WHERE received_at_ms < :cutoff
CREATE INDEX IF NOT EXISTS idx_transcript_received
    ON transcript_messages(received_at_ms);

-- ── deliveries: one row per chunk per destination chat ──────────────────────
-- Deliberately at-least-once: on boot, rows left in 'sending' past the orphan
-- window are re-sent, guarded by content_hash against a recent 'sent' row. A
-- rare duplicate beats a silently lost reply.
--
-- No FK to transcript_messages: the 30-day prune must not cascade delivery
-- history away, and synthetic (non-transcript) deliveries are legal.

CREATE TABLE IF NOT EXISTS deliveries (
    tenant_id      uuid NOT NULL
                   DEFAULT current_setting('ctb.tenant_id', true)::uuid
                   REFERENCES tenants(id) ON DELETE CASCADE,
    session_id     text NOT NULL,
    message_id     text NOT NULL,
    part_index     integer NOT NULL DEFAULT 0,
    chat_id        bigint NOT NULL,
    thread_id      bigint NOT NULL DEFAULT 0,

    session_index  bigint NOT NULL DEFAULT 0,   -- claim ordering
    kind           text NOT NULL DEFAULT 'text'
                   CHECK (kind IN ('text', 'code', 'document', 'activity')),
    -- Send order, lower first (see ctb.delivery.outbox.Priority). A column and
    -- not a JSON field because the scheduler orders *destinations* by it,
    -- before any payload has been read.
    priority       integer NOT NULL DEFAULT 20,
    state          text NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending', 'sending', 'sent', 'skipped',
                                    'failed')),
    claim_id       text,
    -- When the current claim was taken. `recover_orphaned` only reclaims rows
    -- older than the orphan window, so a live peer's in-flight row is never
    -- stolen and double-sent.
    claimed_at     bigint,
    content_hash   text,
    payload_json   text,                        -- the rendered Block
    tg_message_id  bigint,
    attempts       integer NOT NULL DEFAULT 0,
    last_error     text,
    created_at     bigint NOT NULL
                   DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    updated_at     bigint NOT NULL
                   DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    sent_at        bigint,

    PRIMARY KEY (tenant_id, session_id, message_id, part_index, chat_id),
    FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions(tenant_id, id) ON DELETE CASCADE
);

-- The claim path: pending rows for one destination, in transcript order. The
-- leading (chat_id, thread_id) serve the outbox's per-destination round robin.
CREATE INDEX IF NOT EXISTS idx_deliveries_claim
    ON deliveries(chat_id, thread_id, priority, session_index, part_index)
    WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS idx_deliveries_session
    ON deliveries(tenant_id, session_id, state, session_index, part_index);
CREATE INDEX IF NOT EXISTS idx_deliveries_claim_id
    ON deliveries(claim_id) WHERE claim_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_deliveries_sending
    ON deliveries(claimed_at) WHERE state = 'sending';
-- The boot-time duplicate guard looks up recent sends by payload hash.
CREATE INDEX IF NOT EXISTS idx_deliveries_hash
    ON deliveries(chat_id, thread_id, content_hash, sent_at)
    WHERE state = 'sent' AND content_hash IS NOT NULL;

-- ── wizard state: aiogram FSM, DB-backed so wizards survive a restart ───────

CREATE TABLE IF NOT EXISTS wizard_state (
    tenant_id     uuid NOT NULL
                  DEFAULT current_setting('ctb.tenant_id', true)::uuid
                  REFERENCES tenants(id) ON DELETE CASCADE,
    chat_id       bigint NOT NULL,
    thread_id     bigint NOT NULL DEFAULT 0,
    user_id       bigint NOT NULL,
    state_key     text,
    data_json     text,
    -- The wizard edits one message in place; this is that message.
    tg_message_id bigint,
    updated_at    bigint NOT NULL
                  DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    expires_at    bigint,

    PRIMARY KEY (chat_id, thread_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_wizard_expires ON wizard_state(expires_at);

-- ── voice: durable Telegram voice-note and audio jobs ──────────────────────
-- Audio itself is never stored. The resolved route is snapshotted here before
-- transcription so a later topic rebind cannot move the resulting action.

CREATE TABLE IF NOT EXISTS voice_inputs (
    tenant_id          uuid NOT NULL
                       DEFAULT current_setting('ctb.tenant_id', true)::uuid
                       REFERENCES tenants(id) ON DELETE CASCADE,
    chat_id            bigint NOT NULL,
    tg_message_id      bigint NOT NULL,
    thread_id          bigint NOT NULL DEFAULT 0,
    user_id            bigint NOT NULL,
    file_id            text NOT NULL,
    file_unique_id     text,
    file_name          text,
    mime_type          text,
    duration_seconds   integer,
    file_size          bigint,
    route_kind         text NOT NULL DEFAULT 'unknown',
    route_session_id   text,
    route_workspace_id text,
    provider           text NOT NULL,
    model              text NOT NULL,
    state              text NOT NULL DEFAULT 'received'
                       CHECK (state IN ('received', 'transcribing',
                                        'transcribed', 'dispatching',
                                        'waiting_for_user', 'completed',
                                        'failed')),
    transcript         text,
    language           text,
    intent_json        text,
    action_id          text NOT NULL,
    ack_message_id     bigint,
    attempts           integer NOT NULL DEFAULT 0,
    last_error         text,
    created_at         bigint NOT NULL,
    updated_at         bigint NOT NULL,
    completed_at       bigint,

    PRIMARY KEY (chat_id, tg_message_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_action_id
    ON voice_inputs(action_id);
CREATE INDEX IF NOT EXISTS idx_voice_claim
    ON voice_inputs(state, created_at)
    WHERE state IN ('received', 'transcribed', 'dispatching');
CREATE INDEX IF NOT EXISTS idx_voice_completed
    ON voice_inputs(completed_at)
    WHERE completed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_voice_tenant ON voice_inputs(tenant_id);

-- ── unknown_content_types: what the renderer could not classify ────────────
-- Counted, never crashed. Deliberately stores a POINTER to a sample message
-- rather than its content — transcript content is the user's source code — but
-- a pointer still names a tenant's session, so this is tenant-scoped.

CREATE TABLE IF NOT EXISTS unknown_content_types (
    tenant_id         uuid NOT NULL
                      DEFAULT current_setting('ctb.tenant_id', true)::uuid
                      REFERENCES tenants(id) ON DELETE CASCADE,
    type              text NOT NULL,
    shape_signature   text NOT NULL,            -- sorted key path digest
    count             integer NOT NULL DEFAULT 0,
    sample_session_id text,
    sample_message_id text,
    first_seen_at     bigint NOT NULL
                      DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    last_seen_at      bigint NOT NULL
                      DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,

    PRIMARY KEY (tenant_id, type, shape_signature)
);

CREATE INDEX IF NOT EXISTS idx_unknown_last_seen
    ON unknown_content_types(last_seen_at);

-- ── system tables: no RLS, worker role only ────────────────────────────────

-- Only one supervisor may poll. 15s TTL heartbeated every 5s. The name is a
-- TEXT primary key so sharding later ('supervisor:3') needs no schema change.
CREATE TABLE IF NOT EXISTS singleton_lease (
    name         text PRIMARY KEY,
    holder       text NOT NULL,                 -- instance id
    acquired_at  bigint NOT NULL,
    heartbeat_at bigint NOT NULL,
    expires_at   bigint NOT NULL
);

-- Ring buffer behind /health. `tenant_id` is nullable: a request can fail
-- before a tenant is resolved. Written and read through the worker pool only,
-- and filtered explicitly when a tenant asks for its own health.
CREATE TABLE IF NOT EXISTS api_events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     uuid REFERENCES tenants(id) ON DELETE CASCADE,
    at            bigint NOT NULL
                  DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    method        text,
    endpoint      text,
    status_code   integer,
    duration_ms   integer,
    attempt       integer NOT NULL DEFAULT 1,
    ok            boolean NOT NULL DEFAULT true,
    error         text,
    circuit_state text,
    request_id    text,
    session_id    text
);

CREATE INDEX IF NOT EXISTS idx_api_events_at ON api_events(at);
CREATE INDEX IF NOT EXISTS idx_api_events_tenant ON api_events(tenant_id, at);

-- ── row-level security ─────────────────────────────────────────────────────
-- ENABLE applies the policy to everyone but the table owner; FORCE closes that
-- gap too. `current_setting` is called WITHOUT missing_ok, so an unscoped app
-- connection raises instead of silently matching nothing. `ctb_worker` holds
-- BYPASSRLS and never evaluates these.

DO $rls$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'chats', 'workspaces', 'sessions', 'outbound_prompts',
        'transcript_messages', 'deliveries', 'wizard_state', 'voice_inputs',
        'unknown_content_types'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target);
        IF NOT EXISTS (SELECT 1 FROM pg_policies
                        WHERE tablename = target
                          AND policyname = 'tenant_isolation') THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I '
                'USING (tenant_id = current_setting(''ctb.tenant_id'')::uuid) '
                'WITH CHECK (tenant_id = current_setting(''ctb.tenant_id'')::uuid)',
                target);
        END IF;
    END LOOP;
END
$rls$;

-- ── grants ─────────────────────────────────────────────────────────────────
-- The roles are created by `python -m ctb.db.bootstrap`, which needs superuser
-- once. Migrations only grant, so an ordinary deploy needs no elevated rights.

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ctb_app') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO ctb_app';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON '
                'chats, workspaces, sessions, outbound_prompts, '
                'transcript_messages, deliveries, wizard_state, voice_inputs, '
                'unknown_content_types TO ctb_app';
        -- Read-only on the tables that decide scope; TenantMiddleware uses the
        -- worker pool for them, but a stray read must not be a write.
        EXECUTE 'GRANT SELECT ON tenants, tenant_members, tenant_chats TO ctb_app';
        -- /health reports the applied version on every request.
        EXECUTE 'GRANT SELECT ON schema_version TO ctb_app';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ctb_worker') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO ctb_worker';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN '
                'SCHEMA public TO ctb_worker';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public '
                'TO ctb_worker';
    END IF;
END
$grants$;
