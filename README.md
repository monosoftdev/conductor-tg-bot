# conductor-tg-bot

### 👉 [@Conductor_agent_bot](https://t.me/Conductor_agent_bot) — the live bot on Telegram. Say `/start` to it.

[![CI](https://github.com/monosoftdev/conductor-tg-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/monosoftdev/conductor-tg-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-2%2C287-brightgreen.svg)](#quality-gates)

**Drive [Conductor](https://conductor.build) cloud coding agents from Telegram.**

Send a prompt from your phone, get the answer in a chat bubble. Every workspace
becomes its own Telegram topic, so a dozen agents running at once still fit on
one screen — and the topic list tells you which are working, which finished, and
which are waiting on you.

```
you  ▸ /new fix the flaky checkout test
bot  ▸ ⚙ acme/checkout · main · opus-5-1m        ← one pinned card, edited in place
bot  ▸ The test polled a live clock. Froze it and
       widened the tolerance to 50ms.

       1 ✓ open a PR   2 run the full suite   3 leave it
you  ▸ 1
```

No laptop, no SSH, no terminal on a six-inch screen. One bot serves many teams;
each brings its own Conductor API key, and their transcripts, their spending and
their data stay theirs.

---

## Why it exists

Cloud coding agents are the first kind of programming you can genuinely
supervise from a phone. The work happens somewhere else; what you need is a way
to start it, read it, and stop it. That is a chat app's native shape — and
almost nothing else about a development environment is.

So this is not a terminal in a chat window. It is a bot with opinions:

- **One topic per session.** Not one channel, not a thread you have to find.
  Each topic is named after its task, and its title carries the state. A
  workspace is a *group* of them: `/fork` adds another task to the same
  container, branch and checkout, in a room of its own.
- **One pinned status card per turn**, edited in place. It never notifies, so
  the live surface while an agent works costs nothing in your notification tray.
- **One notification per task, not per message.** Replies are held and land as a
  single batch when the turn ends.
- **Answers written for a 40-character bubble.** Every prompt carries an output
  contract: no tables, no "run this command", long code leaves as an attachment,
  and *blocked* goes at the top where you will actually see it.
- **Voice in, if you want it.** Speak into a topic and it becomes a prompt.

## Getting started

Two messages, once, from a phone. The full walkthrough with every edge case is
[`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

1. **`/start`** in a private chat with the bot. That creates your team, named
   after your Telegram account. No group, no admin rights, no decisions.
2. **`/key <your Conductor API key>`** in that same chat. The bot validates it
   against Conductor, stores it encrypted, and **deletes your message**.

Then `/new fix the flaky checkout test`. That is the whole setup.

> Send a key to a *group* and the bot refuses it, deletes the message, and tells
> you to rotate it. Telegram keeps group history forever.

### The daily loop

| Goal | How |
|---|---|
| Start work | `/new [project:] prompt` — creates the workspace and its topic |
| Continue | Just type in that topic — or send a voice note |
| Answer a question | Tap a numbered choice; `✓` marks the recommendation |
| See where you are | `/mode` — session, branch, model, queue, safe actions |
| See everything | `/board` — workspaces, then their sessions |
| Pick up laptop work | `/attach [name]`, then tap **+ Open** |
| Stop a runaway turn | The pinned card's ⏹, or `/stop` |
| Search | `/find text` — every transcript you can reach |
| Finish | `/done` — archives this task; the workspace too when it is the last |

### Working as a team

Optional. Everything above works alone, in your own private chat.

1. `/team` privately mints a single-use code, good for 15 minutes.
2. Create a private supergroup, enable **Topics**, add the bot as an
   administrator (manage topics, pin, delete, send).
3. `/setup <code>` in that group.

`/invite <telegram id>` seats someone on the team — with or without a group.
They get the same Conductor organisation and the same workspaces, driven from
their own private chat if they prefer. `/members` lists who is in.

## Commands

**Daily** — `/new` `/board` `/attach` `/mode` `/stop` `/find` `/done` `/home`

**In a topic** — `/fork` a second task, in its own topic · `/name` rename
(`-w` renames the workspace and every room of it) · `/open` deep-link into
Conductor · `/desk` the
workspace card · `/log [1-200]` dump the raw transcript · `/notify`
loud·quiet·off

**Team** — `/team` `/invite` `/remove` `/leave` `/members` `/health` `/defaults`
`/export` `/tidy`

**Keys** — `/key` Conductor · `/voicekey` speech · `/gitkey` GitHub CI ·
`/revoke` forgets the stored key. All privately; send one bare and the bot walks
you through getting it.

**Several teams** — `/use <name>` picks which one your DMs mean · `/forget`
drops one

**Anyone** — `/start` `/register` `/setup` `/privacy` `/help`

**Power** — `/sql`, a read-only query over your own transcripts

**Operator** — `/platform list|suspend|resume`, gated on `PLATFORM_ADMIN_IDS`

`/home` and `/menu` are one command, as are `/mode` and `/here`, and `/help` and
`/start`.

## How it works

One stateless Python process: long-polling Telegram, polling Conductor, with
every byte of state in PostgreSQL. Seven background services under one singleton
lease. No webhook, no Redis, no volume.

Two rules decide the rest of the design.

### 1. The transcript cursor is the source of truth

Session status is only a cadence knob.

The Conductor API has no webhooks and no streaming, so the bot polls. Every
hazard that follows — a queued prompt reporting `idle`, a turn that starts *and*
finishes between two polls, a redeploy landing mid-turn — is a *status* problem,
never a *cursor* problem: `after=<messageId>` is monotonic and replayable.

So delivery correctness runs unconditionally, on every tick, in every state. The
turn state machine drives only cadence, the typing indicator and the status
card. No `sendMessage` is ever gated on a state-machine conclusion. If the state
machine is wrong you get a stale progress line; you never lose or double-see a
reply.

### 2. Tenant isolation is a database guarantee

Not a code-review guarantee.

Every tenant-scoped table carries `tenant_id`, defaulted from a
transaction-local setting, with row-level security `ENABLE`d *and* `FORCE`d.
Repository SQL therefore contains **no `WHERE tenant_id = ?` anywhere** — and a
filter someone forgets to write returns zero rows instead of another customer's
data.

| Role | Used by | Row-level security |
|---|---|---|
| `ctb_app` | every handler, routing, FSM storage | **enforced** |
| `ctb_worker` | supervisor, delivery and voice claim loops, prune, tenancy lookups | bypassed (`BYPASSRLS`) |

`ctb_app` is deliberately not a member of `ctb_worker`, so there is no `SET ROLE`
path from the request path to the bypass. It holds no grant on `tenants` at all,
which turns a cross-tenant read into a loud failure rather than a quiet one.

There is no process-wide Conductor client and no ambient API key. A handler that
forgets to ask for a tenant fails by name instead of silently using the wrong
customer's key.

[`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) traces a message end to end;
[`docs/TENANCY.md`](docs/TENANCY.md) is the isolation model in full.

## Security

This holds other people's API keys, so the handling is written down rather than
assumed. [`SECURITY.md`](SECURITY.md) has the reporting path, the threat model
and the rotation runbook. The short version:

- **Keys are sealed, never stored.** AES-256-GCM envelope encryption, with the
  additional authenticated data bound to `(key id, tenant, purpose)` — so moving
  a row between tenants makes it undecryptable rather than useful, and a
  Conductor key cannot be opened as a GitHub token.
- **Rotation has no downtime and no dual-write window.** Every sealed blob names
  the key that sealed it, so old and new coexist while `python -m ctb.rewrap`
  works through them.
- **Nothing registers a tenant key with the log scrubber.** A process-wide set of
  every customer's plaintext would keep them all in memory for the life of the
  process; they are redacted by pattern instead, unconditionally.
- **Transcript content is the customer's source code.** Off by default in logs,
  capped at 64 KB per message, pruned after 30 days.
- **Voice is the one thing that leaves the perimeter**, so it is off by default
  and each workspace brings its own speech key. There is deliberately no shared
  fallback: nobody is billed or exposed through somebody else's account.

## Running it locally

Python 3.13 and Docker.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d --wait db          # PostgreSQL 16 on :5433, disposable
cp .env.example .env
```

Create the two roles and the schema once, with a superuser DSN:

```bash
.venv/bin/python -m ctb.db.bootstrap \
    --admin-dsn "postgresql://postgres:postgres@127.0.0.1:5433/ctb" \
    --app-password dev --worker-password dev
```

Generate a master key for `CTB_MASTER_KEYS`, then start it:

```bash
.venv/bin/python -m ctb.rewrap --new-key v1
.venv/bin/python -m ctb
curl http://127.0.0.1:8080/health
```

You need **no** Telegram token and **no** Conductor key to run the tests. Both
are faked.

### Quality gates

```bash
.venv/bin/python -m pytest -q                 # 2,287 tests, under a minute
.venv/bin/python -m pytest -q -m "not db"     # the 1,551 that need no server
.venv/bin/python -m ruff format --check src scripts tests
.venv/bin/python -m ruff check src scripts tests
.venv/bin/python -m pyright
```

CI runs all of that in parallel on every pull request — plus a secret scan over
the full history, four sharded test runs each against its own PostgreSQL, a
Docker build, and a real boot against a real database. `all gates` is the single
required check.

Two things there are not negotiable. `CI=true` turns an unreachable database
from a skip into a **failure**, because a skipped database silently disarms
every isolation, RLS, crypto and claim test in the suite. And the secret scan
proves it read the history before its verdict counts — a scanner that reports
clean having scanned nothing is worse than no scanner.

`tests/test_isolation.py` is generated from the live schema: a table added next
year is covered without anyone remembering to add it.

## Deploying

One always-on service and one PostgreSQL database. Keep exactly one replica —
Telegram's `getUpdates` and the supervisor lease both assume it.

1. `python -m ctb.db.bootstrap` once, with a superuser DSN. **Never point the
   bot itself at the superuser**: a superuser bypasses row-level security.
2. Set `TELEGRAM_BOT_TOKEN`, `DATABASE_URL` (as `ctb_app`),
   `SYSTEM_DATABASE_URL` (as `ctb_worker`) and `CTB_MASTER_KEYS`. All four are
   reported together, so set them in one go.
3. Confirm `/health` returns `{"status":"ok","ok":true,...}`.

[`docs/SETUP.md`](docs/SETUP.md) walks it from zero on Railway; the `Dockerfile`
runs anywhere. [`docs/DEPLOY.md`](docs/DEPLOY.md) is the operator's side — what
to watch, and what to do when it breaks.

### If you run this for other people

You will be holding their API keys, and each one can read every transcript in
its organisation and spend its owner's money.

- Check with Conductor that brokering third-party keys fits their terms.
- Publish a privacy policy and terms. `/privacy` states what is stored today.
- Back `CTB_MASTER_KEYS` up **separately** from the database. Together they are
  one secret; apart, neither is enough.
- Consider `REGISTRATION_OPEN=false` at first, so your early users are people
  you can call.

## Contributing

Pull requests welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the setup, the
gates, and the handful of conventions that are not negotiable — each with its
reasoning attached, so if a rule looks arbitrary, the why is written down.

Security reports go through [`SECURITY.md`](SECURITY.md), never a public issue.

Architecture lives in [`docs/`](docs/), which has an [index](docs/README.md)
saying which files describe the system as built and which are kept for their
reasoning. Notable changes are in [`CHANGELOG.md`](CHANGELOG.md).

## Licence

MIT — see [`LICENSE`](LICENSE).

Dependencies are permissive with one exception worth naming: **psycopg is
LGPL-3.0-only**, so a Docker image built from this repository ships LGPL
binaries. That does not affect your use of this source, and psycopg's own source
is available from [PyPI](https://pypi.org/project/psycopg/); if you redistribute
an image, the LGPL's relinking and source-offer terms apply to that component.

© 2026 [Monosoft LLC](https://monosoft.dev). Not affiliated with Conductor or
Telegram.
