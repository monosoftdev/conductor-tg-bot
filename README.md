# conductor-tg-bot

[![CI](https://github.com/reclaimly/conductor-tg-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/reclaimly/conductor-tg-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)

> **Status: alpha.** Every path is covered by 2,058 tests against a real
> PostgreSQL, and CI gates every merge — but the bot has **not yet been run
> against a live Conductor account and a live Telegram deployment end to end**.
> What is proven and what is not is listed under
> [What is still unproven](#what-is-still-unproven). Treat it as something to
> read, fork and try, not something to put in front of a customer today.

**Who this is for:** developers who already run [Conductor](https://conductor.build)
cloud coding agents and want to drive them from a phone — start a workspace,
send a prompt, read the answer, stop a runaway turn — without opening a laptop.

Drive [Conductor](https://conductor.build) cloud coding agents from Telegram.
Each workspace gets its own topic — in your private chat with the bot, or in a
shared group. Send a prompt, watch one compact status card, and get the answer
on your phone.

One bot serves many teams. Each brings its own Conductor API key; their
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

Two messages, once, from a phone — the full walkthrough with screenshots' worth
of detail is in [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md):

1. `/start` in a private chat with the bot. That creates your team, named after
   your Telegram account. No group, no admin rights, no decision.
2. `/key <your Conductor API key>` in that same chat. The bot validates it,
   stores it encrypted, and **deletes your message**.

Then `/new fix the flaky checkout test`, in that same private chat. Each
workspace gets its own topic there. `/register <name>` instead of `/start` if
you would rather pick the team's name yourself.

Then the daily loop:

| Goal | Action |
|---|---|
| Start work | `/new [project:] prompt` — creates the workspace and its topic |
| Continue | Type, speak, or send audio in that topic |
| See state | `/mode` — session, branch, model, queue, safe actions |
| See everything | `/board` · switch with `/s` |
| Resume laptop work | `/attach [name]`, then tap `+ Open` |
| Stop | The pinned card's ⏹, or `/stop` |
| Search | `/find text` anywhere · plain text in a group's General |
| Finish | `/done`, then the named confirmation |

Sending an API key to a *group* is refused and the message deleted; rotate it
and send it privately instead.

### Optional: a group

One team, several people, one topic list everybody sees. Nothing above needs it.

1. `/team` privately. It mints a single-use code, good for 15 minutes; run it
   again for a fresh one.
2. Create a private supergroup, enable **Topics**, add the bot as an
   administrator (manage topics, pin, delete, send).
3. `/setup <code>` in that group.

Your co-founder joins with `/invite <their telegram id>` — same group, same
Conductor organisation, same topics. `/members` lists who is in. `/invite`
works without a group too; they just drive the team from their own private
chat.

### Topics in a private chat, and what happens when Telegram says no

Topics in a DM need Telegram's **Threaded Mode** (@BotFather → your bot). The
bot needs no admin rights and no Premium there, and the sibling toggle
*"Disallow users to create new threads"* governs the **user**, never the bot.

That rests on a Bot API 10.x feature with an open regression, so check it
against your own token before you trust it:

```bash
export TELEGRAM_BOT_TOKEN=...      # the token the bot runs on
export TELEGRAM_DM_CHAT_ID=...     # your own Telegram user id
.venv/bin/python scripts/probe_dm_topics.py
```

If Telegram refuses — threaded mode off, `createForumTopic` rejected, a thread
that cannot be addressed — the private chat degrades to **one workspace at a
time**, says so once, and `/s` switches between them. Every DM-topic path has
that fallback; a topic the bot cannot create or deliver into never costs you a
working bot, only the topic list.

## Commands

Daily: `/new` `/attach` `/board` `/stop` `/find` `/mode` `/done`
Power: `/s` `/fork` `/name` `/open` `/desk` `/here` `/log` `/notify` `/defaults` `/sql` `/tidy`
Team: `/team` `/invite` `/remove` `/leave` `/members` `/health` `/export` `/key` `/voicekey` `/gitkey` `/voice` `/revoke`
Several teams: `/use` picks which one your DMs mean · `/forget` drops one
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
contains no `WHERE tenant_id = ?` in any tenant-scoped query: a forgotten filter returns zero rows
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
.venv/bin/python -m pytest -q          # 2,058 tests
.venv/bin/python -m pytest -q -m "not db"   # the 1,442 that need no server
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
2. The phone checklist: `/start` → `/key` → `/new` → answer, all in a DM.
3. **DM topics.** `scripts/probe_dm_topics.py` against a live token: create,
   send into, rename, icon. Everything degrades if it fails, but nobody has
   watched it succeed.
4. The optional group: `/team` → `/setup` → `/new` in a supergroup.
5. Two real teams at once, each with its own key, staying separate.
6. Redeploy mid-turn; the answer arrives exactly once.
7. Real Conductor and Telegram 429s, to tune the rate budgets.
8. Voice: a workspace storing its own speech key and 30 owner recordings.

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

Architecture and the failure contract are in [`docs/`](docs/), which has its
own [index](docs/README.md) saying which files describe the system as built and
which are kept for their reasoning. Changes worth knowing about are in
[`CHANGELOG.md`](CHANGELOG.md).

## Third-party licences

This project is MIT (see [`LICENSE`](LICENSE)). Its dependencies are permissive
with one exception worth naming: **psycopg is LGPL-3.0-only**, and a Docker
image built from this repository therefore ships LGPL binaries. That does not
affect your use of this source, and psycopg's own source is available from
[PyPI](https://pypi.org/project/psycopg/); if you redistribute an image, the
LGPL's relinking and source-offer terms apply to that component.

MIT licensed, © 2026 Reclaimly, Inc. Not affiliated with Conductor or Telegram.
