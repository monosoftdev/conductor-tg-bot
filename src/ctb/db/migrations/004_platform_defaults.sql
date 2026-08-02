-- ── 004: DEFAULT_BRANCH (and its three siblings) actually mean something ───
-- `tenants.default_agent/model/effort/branch` were introduced as "per-tenant
-- knobs that used to be environment variables", NOT NULL with the shipped
-- literal as the column default. Nothing in the bot ever writes them:
-- `tenancy.update_defaults` has three callers and all three set voice fields.
--
-- So every tenant has carried 'claude' / 'opus-5-1m' / 'high' / 'main' since
-- creation, `TenantSettings.of` reads the row, and the platform's
-- `DEFAULT_AGENT` / `DEFAULT_MODEL` / `DEFAULT_EFFORT` / `DEFAULT_BRANCH` have
-- been dead for the whole life of the multi-tenant build. `DEFAULT_BRANCH=dev`
-- in Railway changed nothing; "go with defaults" still went with main.
--
-- The column becomes an **override**: NULL means "follow the platform", which
-- is the only reading under which an operator env var can reach a tenant that
-- already exists.

ALTER TABLE tenants ALTER COLUMN default_agent  DROP NOT NULL;
ALTER TABLE tenants ALTER COLUMN default_agent  DROP DEFAULT;
ALTER TABLE tenants ALTER COLUMN default_model  DROP NOT NULL;
ALTER TABLE tenants ALTER COLUMN default_model  DROP DEFAULT;
ALTER TABLE tenants ALTER COLUMN default_effort DROP NOT NULL;
ALTER TABLE tenants ALTER COLUMN default_effort DROP DEFAULT;
ALTER TABLE tenants ALTER COLUMN default_branch DROP NOT NULL;
ALTER TABLE tenants ALTER COLUMN default_branch DROP DEFAULT;

-- Only rows still carrying the shipped literal. A value that differs was set
-- by hand against the database — nothing in the bot could have written it — and
-- a deliberate choice outranks an env var. A hand-set 'main' is indistinguishable
-- from the default and is therefore released; that is the intended direction.
UPDATE tenants SET default_agent  = NULL WHERE default_agent  = 'claude';
UPDATE tenants SET default_model  = NULL WHERE default_model  = 'opus-5-1m';
UPDATE tenants SET default_effort = NULL WHERE default_effort = 'high';
UPDATE tenants SET default_branch = NULL WHERE default_branch = 'main';

-- ── and the same, one level down ───────────────────────────────────────────
-- `chats.default_branch` is remembered from the last create — `create_and_bind`
-- wrote every request's branch back onto the seat — so a chat that ever made a
-- workspace on `main` kept offering `main` forever, outranking both the tenant
-- and the platform. That write-back is removed in this change; `/defaults
-- branch <name>` stays, and is now the only thing that sets this column.
--
-- Existing rows cannot distinguish the implicit write from an explicit
-- `/defaults branch main`, and the implicit one is what every create did, so
-- rows equal to the shipped default are released to follow the platform. A
-- chat that deliberately pinned some other branch keeps it.
UPDATE chats SET default_branch = NULL WHERE default_branch = 'main';
