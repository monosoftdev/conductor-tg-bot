-- ── 002: CI watches ────────────────────────────────────────────────────────
-- A turn that opens a pull request has not finished its job until CI has run
-- on it. The bot watches that run and says so once, in the topic the work
-- happened in, with a button that hands the failure straight back to the agent.
--
-- Two additions:
--   * a fourth sealed credential on `tenants` — a GitHub token per team. There
--     is deliberately no shared fallback: the token reads a customer's private
--     source, so borrowing another team's would be the exact cross-tenant read
--     the rest of this schema exists to make impossible.
--   * `ci_watches`, one row per pull request being watched, tenant-scoped like
--     everything else.
--
-- Written as 002 rather than edited into 001: a deployed database has already
-- recorded 001 as applied and would never see the change.

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS github_key_ct  bytea;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS github_key_kid text;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS github_key_fp  text;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS github_key_at  bigint;

CREATE TABLE IF NOT EXISTS ci_watches (
    tenant_id       uuid NOT NULL
                    DEFAULT current_setting('ctb.tenant_id', true)::uuid
                    REFERENCES tenants(id) ON DELETE CASCADE,
    owner           text NOT NULL,
    repo            text NOT NULL,
    pr_number       integer NOT NULL,
    -- Where to say it, and whose session to hand a failure back to. Snapshotted
    -- at watch time for the same reason `voice_inputs` snapshots its route: a
    -- later rebind must not move the message.
    session_id      text NOT NULL,
    chat_id         bigint NOT NULL,
    thread_id       bigint NOT NULL DEFAULT 0,
    state           text NOT NULL DEFAULT 'watching'
                    CHECK (state IN ('watching', 'done', 'gave_up')),
    -- The commit the last poll saw. A push moves it, which re-arms the watch:
    -- a second failure on new code is news, the same failure re-read is not.
    head_sha        text,
    last_status     text,
    notified_sha    text,
    notified_status text,
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text,
    created_at      bigint NOT NULL
                    DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    updated_at      bigint NOT NULL
                    DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    next_poll_at    bigint NOT NULL
                    DEFAULT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
    expires_at      bigint NOT NULL,

    -- One watch per pull request, not per turn: a second turn that pushes to
    -- the same PR re-arms this row instead of racing a duplicate of it.
    PRIMARY KEY (tenant_id, owner, repo, pr_number)
);

-- Not tenant-led on purpose: the watcher claims across tenants, exactly like
-- `idx_deliveries_claim` and `idx_voice_claim`.
CREATE INDEX IF NOT EXISTS idx_ci_watches_due
    ON ci_watches(next_poll_at)
    WHERE state = 'watching';
CREATE INDEX IF NOT EXISTS idx_ci_watches_session
    ON ci_watches(tenant_id, session_id);

DO $rls$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY['ci_watches']
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

-- 001's worker grant said ALL TABLES, which is evaluated at grant time and so
-- does not reach a table created afterwards. Both roles are named again here.
DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ctb_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ci_watches TO ctb_app';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ctb_worker') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ci_watches TO ctb_worker';
    END IF;
END
$grants$;
