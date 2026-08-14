-- Create (or refresh) the operator role `ctb_ops`.
--
-- A fourth role, alongside `ctb_app` and `ctb_worker`, for a human or an agent at a
-- psql prompt against production: read *and* write, every table, every tenant. It is
-- deliberately powerful, and it is deliberately not a superuser — it can be revoked
-- with one DROP ROLE, it cannot read `pg_authid`, and it cannot drop the database.
--
--   * `BYPASSRLS`, so cross-tenant queries need no `ctb.tenant_id` in scope. That
--     also means row-level security is *not* a safety net here: a bare UPDATE with
--     no WHERE hits every customer at once.
--   * `ALL PRIVILEGES` on schema `public` and everything in it, plus default
--     privileges, so a table added by a later migration is covered automatically and
--     this script does not have to be re-run for it.
--   * `lock_timeout = 5s` and `idle_in_transaction_session_timeout = 60s`. Neither
--     limits what may be done; they stop an ad-hoc ALTER or a forgotten open
--     transaction from parking behind — and in front of — the live bot.
--
-- The application never applies DDL and neither does this: it is an operator script,
-- like `ctb.db.bootstrap`, run by hand with a superuser DSN. It is idempotent.
--
--   psql "$ADMIN_DATABASE_URL" -v pw="$(openssl rand -hex 24)" \
--        -f scripts/ops_role.sql
--
-- The password is passed in and never lives in this file. The resulting DSN belongs
-- in the environment as TELEGRAM_CONDUCTOR_BOT_DATABASE_URL; see CLAUDE.md.

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ctb_ops') THEN
        CREATE ROLE ctb_ops LOGIN;
    END IF;
END
$$;

ALTER ROLE ctb_ops WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION BYPASSRLS INHERIT CONNECTION LIMIT 8 PASSWORD :'pw';

ALTER ROLE ctb_ops SET lock_timeout = '5s';
ALTER ROLE ctb_ops SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE ctb_ops SET search_path = public;

GRANT ALL ON SCHEMA public TO ctb_ops;
GRANT ALL ON ALL TABLES IN SCHEMA public TO ctb_ops;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ctb_ops;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO ctb_ops;

-- Migrations run as the bootstrap superuser, so pin the default privileges to it:
-- a table created by a future `ctb.db.upgrade` is writable by `ctb_ops` on creation.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT ALL ON TABLES TO ctb_ops;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT ALL ON SEQUENCES TO ctb_ops;
