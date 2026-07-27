# Deploying

One always-on service and one PostgreSQL database. Nothing else — no volume,
no Redis, no public webhook, no cron. Telegram is reached by long polling, so
the container needs outbound network and nothing inbound except the health
check.

These instructions are written for Railway because that is where it runs, but
nothing here is Railway-specific beyond the variable names.

## 1. Add PostgreSQL

**Yes, you need one.** SQLite is gone; there is no fallback and the process
refuses to boot without both DSNs. In Railway: **New → Database → PostgreSQL**,
in the same project as the bot service so they share a private network.

Railway gives you a `postgres` superuser and two URLs on that service:

| Variable | What it is | Use it for |
|---|---|---|
| `DATABASE_URL` | private, `*.railway.internal` | nothing directly — see below |
| `DATABASE_PUBLIC_URL` | the TCP proxy, reachable from your laptop | the one-time bootstrap |

Do **not** point the bot at Railway's `DATABASE_URL` as-is. That is the
superuser, and connecting as a superuser silently disables row-level security:
`FORCE ROW LEVEL SECURITY` does not apply to `BYPASSRLS` roles, so every
tenant's rows would be visible to every handler. The whole isolation model
rests on connecting as `ctb_app`.

## 2. Create the roles and the schema, once

From your laptop, against the public proxy URL:

```bash
python -m ctb.db.bootstrap \
    --admin-dsn "$DATABASE_PUBLIC_URL" \
    --app-password "$(openssl rand -base64 24)" \
    --worker-password "$(openssl rand -base64 24)"
```

Keep both passwords; you need them in the next step. This creates `ctb_app`
(row-level security enforced) and `ctb_worker` (`BYPASSRLS`), applies the one
migration, and grants each role only what it needs. It is safe to re-run.

The bot itself never applies DDL. At boot it *verifies* the schema and refuses
to start if it is missing, so a half-migrated database fails loudly at deploy
time rather than at the first prompt.

## 3. Generate the master key

```bash
python -m ctb.rewrap --new-key v1
```

This is what seals every customer's Conductor API key. **Back it up somewhere
that is not the database backup.** Together they are one secret; apart, neither
is enough. Losing it makes every stored key permanently unreadable — every
workspace would have to re-run `/key`.

## 4. Set the service variables

Deploy this repository as a service (`railway.toml` selects the Dockerfile),
then set:

```
TELEGRAM_BOT_TOKEN   from @BotFather
DATABASE_URL         postgresql://ctb_app:<app-password>@<internal-host>:5432/railway
SYSTEM_DATABASE_URL  postgresql://ctb_worker:<worker-password>@<internal-host>:5432/railway
CTB_MASTER_KEYS      v1:<the key from step 3>
```

Take `<internal-host>` and the database name from Railway's `DATABASE_URL` —
you are reusing its host and swapping the credentials. Using the internal host
keeps database traffic off the public proxy and off your egress bill.

The bot reports all four missing variables at once, so a boot failure names
everything you forgot rather than one thing per redeploy.

Worth setting straight away:

- `PLATFORM_ADMIN_IDS` — your Telegram user id, so `/platform` works.
- `HEALTH_TOKEN` — a random string. Without it the detailed `/health` body is
  served to loopback only, which means a public domain shows you the summary
  and nothing useful.
- `REGISTRATION_OPEN=false` for the first weeks, so your first users are people
  you can call when something breaks.

## 5. Confirm

```bash
curl https://<your-domain>/health
# {"status":"ok","ok":true,...}

curl -H "Authorization: Bearer $HEALTH_TOKEN" https://<your-domain>/health
# pool stats, lease holder, per-workspace poller counts, recent API calls
```

Then from a phone: `/register` → create a supergroup with Topics on → add the
bot as an admin → `/setup <code>` → `/key` privately → `/new fix the typo in
the readme`. An answer in the topic means every layer works.

## Exactly one replica

Keep it at 1. Two would fight over Telegram's `getUpdates` (409 Conflict) and
only one process may hold the supervisor lease. `overlapSeconds = 0` stops the
old container before the new one polls.

The lease in the database is the belt to that braces: a second process would
find the lease held, poll nothing, and log `supervisor.lease_lost`. Delivery
claiming tolerates a brief overlap regardless — `FOR UPDATE SKIP LOCKED` plus
an orphan window means a redeploy mid-turn delivers the answer exactly once.

Scaling past one process means sharding the lease (`singleton_lease.name` is
already a text primary key, so `'supervisor:3'` needs no schema change). That
is not needed until `supervisor.pollers_starved` starts appearing.

## Backups

Railway's managed PostgreSQL backups cover the database. They do **not** cover
`CTB_MASTER_KEYS` — that is the point. Store the master key in a password
manager or a separate secrets store, and write down which `kid` is active.

There is deliberately no `/backup` command. The single-user version had one; it
uploaded the whole database file to the owner, which under multi-tenancy would
hand one customer every other customer's transcripts. `/export` gives a
workspace its own data, scoped by row-level security.

## Upgrading

Push to the default branch. The image is stateless, so a redeploy is a restart:
in-flight deliveries are re-claimed after the orphan window, the transcript
cursor picks up where it left off, and wizards survive because their state is
in `wizard_state` rather than in memory.

If a release adds a migration, run it before deploying the new image — the same
`ctb.db.bootstrap` command, which is idempotent. The old image keeps running
against the new schema in the window between.

## Key rotation

No downtime, no dual-write window; every sealed blob names the key that sealed
it, so old and new coexist.

```bash
python -m ctb.rewrap --new-key v2     # prepend to CTB_MASTER_KEYS, deploy
python -m ctb.rewrap --rewrap         # re-seal rows still on v1
                                      # drop v1 from the variable, deploy again
```

## What to watch in the first week

| Signal | Where | What it means |
|---|---|---|
| `rate_limited` in `/health` | `/health` | Conductor 429s; the per-tenant breaker is holding |
| `outbox.rate_limited` | logs | Telegram 429s; tune the pacer budgets |
| `supervisor.pollers_starved` | logs | more live sessions than poller slots; time to shard |
| `transcript.deliveries_shed` | logs | a workspace is over `max_pending_deliveries` |
| `auth_failed_at` | `/platform list` | a workspace's Conductor key was rejected |
| pool `waiting` | `/health` | connections are queuing; raise `DB_POOL_MAX` |

## Before you open registration to strangers

You will be holding other people's Conductor API keys. Each can read every
transcript in their organisation and spend their money.

- Confirm with Conductor that brokering third-party keys is within their terms.
- Publish a privacy policy and terms; `/privacy` states what is stored today.
- Have the breach runbook ready — it is in [`SECURITY.md`](../SECURITY.md).
- Leave `VOICE_ENABLED=false` unless you have read what it sends where.
