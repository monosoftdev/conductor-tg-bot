# conductor-tg-bot

[![CI](https://github.com/reclaimly/conductor-tg-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/reclaimly/conductor-tg-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)

Drive [Conductor](https://conductor.build) cloud coding agents from Telegram.
Each workspace gets a forum topic: send a prompt, watch one compact status card,
and get the answer on your phone.

One bot serves many workspaces. Each brings its own Conductor API key; their
transcripts, their spending and their data stay theirs.

- Python 3.13, aiogram 3, httpx, PostgreSQL
- Telegram long polling; no public webhook
- Tenant isolation enforced by PostgreSQL row-level security, not by code review
- API keys sealed with AES-256-GCM, bound to the tenant that owns them
- Durable prompt idempotency, transcript cursor, delivery outbox, singleton lease
- One stateless replica; every byte of state is in the database

The transcript cursor is the source of truth. Session status only changes poll
cadence and progress UX; it never gates delivery.

## Using it

Four steps, once, from a phone — the full walkthrough with screenshots' worth of
detail is in [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md):

1. `/register your-team-name` in a private chat with the bot.
2. Create a private supergroup, enable **Topics**, add the bot as an
   administrator (manage topics, pin, delete, send).
3. `/setup <code>` in that group, using the code from step 1.
4. `/key <your Conductor API key>` privately. The bot validates it, stores it
   encrypted, and **deletes your message**.

Ran out of time on the 15-minute code? `/register` again gives you a fresh one.

Then the daily loop, from the group:

| Goal | Action |
|---|---|
| Start work | `/new [project:] prompt` — creates the workspace and its topic |
| Continue | Type, speak, or send audio in that topic |
| See state | `/mode` — session, branch, model, queue, safe actions |
| See everything | `/board` · switch with `/s` |
| Resume laptop work | `/attach [name]`, then tap `+ Open` |
| Stop | The pinned card's ⏹, or `/stop` |
| Search | Plain text in General, or `/find text` |
| Finish | `/done`, then the named confirmation |

Your co-founder joins with `/invite <their telegram id>` — same group, same
Conductor organisation, same topics. `/members` lists who is in.

Sending an API key to a *group* is refused and the message deleted; rotate it
and send it privately instead.

## Commands

Daily: `/new` `/attach` `/board` `/stop` `/find` `/mode` `/done`
Power: `/s` `/fork` `/name` `/open` `/desk` `/here` `/log` `/notify` `/defaults` `/sql` `/tidy`
Workspace: `/invite` `/remove` `/leave` `/members` `/health` `/export` `/key` `/voicekey` `/voice` `/revoke`
Multiple workspaces: `/use` picks which one your DMs mean · `/forget` drops one
Anyone: `/start` `/register` `/setup` `/privacy` `/help`
Operator: `/platform list|suspend|resume`, gated on `PLATFORM_ADMIN_IDS`

## Running it

Python 3.13 and a PostgreSQL 16+ server (tested on 16 in CI, verified on 18).

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d --wait db          # PostgreSQL on :5433, disposable
cp .env.example .env                    # then fill in the four required values
```

Create the roles and schema once, with a superuser DSN:

```bash
.venv/bin/python -m ctb.db.bootstrap \
    --admin-dsn "postgresql://postgres:postgres@127.0.0.1:5433/ctb" \
    --app-password "..." --worker-password "..."
```

Generate a master key for `CTB_MASTER_KEYS`:

```bash
.venv/bin/python -m ctb.rewrap --new-key v1
```

Then:

```bash
.venv/bin/python -m ctb
curl http://127.0.0.1:8080/health
```

### Two database roles

| Role | Used by | Row-level security |
|---|---|---|
| `ctb_app` | every handler, routing, FSM storage | **enforced** |
| `ctb_worker` | supervisor, delivery and voice claim loops, prune, tenancy lookups | bypassed (`BYPASSRLS`) |

`ctb_app` is deliberately not a member of `ctb_worker`, so there is no
`SET ROLE` path from the request path to the bypass role. Repository SQL
contains no `WHERE tenant_id = ?` anywhere: a forgotten filter returns zero rows
rather than another customer's data.

### Key rotation

No downtime, no dual-write window:

```bash
.venv/bin/python -m ctb.rewrap --new-key v2   # prepend to CTB_MASTER_KEYS, deploy
.venv/bin/python -m ctb.rewrap --rewrap       # re-seal rows still on v1
                                              # drop v1 on the next deploy
```

Every sealed blob names the key that sealed it, so old and new coexist.

## Deploying

One always-on service and one PostgreSQL database. No volume, no Redis, no
public webhook. **Setting it up from zero is [`docs/SETUP.md`](docs/SETUP.md)** —
branches, CI, BotFather, Railway, roles, keys and variables, in order. The
shape of it:

1. Add a PostgreSQL database. You need one — there is no SQLite fallback.
2. `python -m ctb.db.bootstrap` once, with a superuser DSN, to create the two
   roles and the schema. **Do not point the bot at the superuser** — a
   superuser bypasses row-level security and every tenant would see every row.
3. Set `TELEGRAM_BOT_TOKEN`, `DATABASE_URL` (as `ctb_app`), `SYSTEM_DATABASE_URL`
   (as `ctb_worker`), `CTB_MASTER_KEYS`. All four are reported together, so set
   them in one go.
4. Confirm `/health` returns `{"status":"ok","ok":true,...}`.

Keep exactly one replica. Telegram's `getUpdates` and the supervisor lease both
assume it; `overlapSeconds=0` stops the old deployment before the new one polls.

## Quality gates

```bash
docker compose up -d --wait db
.venv/bin/python -m pytest -q          # 1927 tests
.venv/bin/python -m pytest -q -m "not db"   # the ~1400 that need no server
.venv/bin/python -m ruff format --check src scripts tests
.venv/bin/python -m ruff check src scripts tests
.venv/bin/python -m pyright
```

The suite migrates once per session and truncates between tests, so a full run
is under a minute. `tests/test_isolation.py` is generated from the live schema:
a table added later is covered without anyone remembering to add it.

## What is still unproven

Everything below is verified offline against a real PostgreSQL and a faked
Conductor API. These need a live deployment:

1. First Railway deploy with a real database and real secrets.
2. The phone checklist: `/register` → `/setup` → `/key` → `/new` → answer.
3. Two real workspaces at once, each with its own key, staying separate.
4. Redeploy mid-turn; the answer arrives exactly once.
5. Real Conductor and Telegram 429s, to tune the rate budgets.
6. Voice: a workspace storing its own speech key and 30 owner recordings.

## Before you open this to strangers

You will be holding other people's Conductor API keys. Each one can read every
transcript in their organisation and spend their money.

- Confirm with Conductor that brokering third-party keys is within their terms.
- Publish a privacy policy and terms; `/privacy` states what is stored today.
- Write the breach runbook: rotate `CTB_MASTER_KEYS`, re-wrap, notify.
- Back the master keys up **separately** from the database. Together they are
  the same secret; apart, neither is enough.
- Consider `REGISTRATION_OPEN=false` for the first weeks, so your first users
  are people you can call.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) for the four gates and the
conventions that are not negotiable. Security reports go through
[`SECURITY.md`](SECURITY.md), never a public issue.

Architecture and the failure contract are in [`docs/`](docs/).

MIT licensed, © 2026 Reclaimly, Inc. Not affiliated with Conductor or Telegram.
