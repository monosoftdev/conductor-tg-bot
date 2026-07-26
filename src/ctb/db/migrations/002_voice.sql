-- Durable Telegram voice-note input jobs.
--
-- Audio itself is never stored. The resolved route is snapshotted here before
-- transcription so a later topic rebind cannot move the resulting action.

CREATE TABLE IF NOT EXISTS voice_inputs (
    chat_id            INTEGER NOT NULL,
    tg_message_id      INTEGER NOT NULL,
    thread_id          INTEGER NOT NULL DEFAULT 0,
    user_id            INTEGER NOT NULL,
    file_id            TEXT NOT NULL,
    file_unique_id     TEXT,
    file_name          TEXT,
    mime_type          TEXT,
    duration_seconds   INTEGER,
    file_size          INTEGER,
    route_kind         TEXT NOT NULL DEFAULT 'unknown',
    route_session_id   TEXT,
    route_workspace_id TEXT,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'received'
                       CHECK (state IN ('received', 'transcribing',
                                        'transcribed', 'dispatching',
                                        'waiting_for_user', 'completed',
                                        'failed')),
    transcript         TEXT,
    language           TEXT,
    intent_json        TEXT,
    action_id          TEXT NOT NULL,
    ack_message_id     INTEGER,
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    completed_at       INTEGER,
    PRIMARY KEY (chat_id, tg_message_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_action_id
    ON voice_inputs(action_id);
CREATE INDEX IF NOT EXISTS idx_voice_claim
    ON voice_inputs(state, updated_at)
    WHERE state IN ('received', 'transcribed', 'dispatching');
CREATE INDEX IF NOT EXISTS idx_voice_completed
    ON voice_inputs(completed_at)
    WHERE completed_at IS NOT NULL;
