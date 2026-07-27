# Setup, once, from zero

Everything that has to exist before this bot serves its first prompt, in the
order it has to happen. Roughly 40 minutes, most of it waiting for Railway.

Each step says **who** does it, because some of it only you can do — anything
involving a browser login, a payment method, or a secret that must never reach
a transcript.

| Step | Who | Why it cannot be automated |
|---|---|---|
| 1. Branch protection | **You** | GitHub's API refuses it to an app integration |
| 2. Telegram bot | **You** | BotFather is a chat, and the token is a secret |
| 3. Railway project | **You** | Browser login and billing |
| 4. Database roles | You, one command | Needs the superuser DSN |
| 5. Master key | **You** | It must never appear in a transcript |
| 6. Service variables | **You** | They are the secrets from 2–5 |
| 7. First deploy | Either | — |
| 8. Phone checklist | **You** | It is a phone |

---

## 1 · Branches and CI protection — you, 5 minutes

`dev` already exists and points at `main`. The intended flow:

```
feature branch  →  PR into dev  →  PR into main  →  deploy
```

CI runs on every pull request. Nothing merges until it is green — but only once
you turn that on, because a required check is a repository setting, not a file.

**GitHub → Settings → Branches → Add branch ruleset** (or *Add rule* on older
UIs). Do this twice, once for `main` and once for `dev`:

- Branch name pattern: `main` (then repeat for `dev`)
- ☑ **Require a pull request before merging** → approvals: `0` is fine while
  you are the only committer; raise it when someone else joins
- ☑ **Require status checks to pass before merging**
  - ☑ Require branches to be up to date before merging
  - In the search box add exactly one check: **`all gates`**
- ☑ **Do not allow bypassing the above settings** — otherwise the rule is
  advice, and an accidental `git push main` skips every test

> Add **`all gates`** and nothing else. It is a single job that fails if any of
> the other seven failed, was skipped, or was cancelled. Listing the individual
> jobs means editing the rule every time a job is added or renamed; listing
> `all gates` means never touching it again.
>
> If `all gates` does not appear in the search box, open any pull request first
> — GitHub only offers checks it has seen run at least once.

---

## 2 · The Telegram bot — you, 5 minutes

One bot serves every workspace. Customers add *this* bot to *their* group.

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Give it a display name and a `_bot`-suffixed username.
3. Copy the token. **This is a secret** — it can read every message in every
   group the bot is in.

Then, still in BotFather, two settings that are not optional:

- `/setprivacy` → select your bot → **Disable**.
  Privacy mode ON means the bot only sees messages starting with `/`. The whole
  point of this bot is that you type a prompt in a topic without a command, so
  with privacy on it appears to ignore you.
- `/setjoingroups` → **Enable**. It has to be addable to groups.

Verify before you go further:

```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/getMe"
```

`{"ok":true,...}` means the token is good. Anything else, and every later step
will fail for a reason that looks like something else.

**Get your own Telegram user id** while you are here — you need it for
`PLATFORM_ADMIN_IDS`. Message [@userinfobot](https://t.me/userinfobot); it
replies with a number.

---

## 3 · Railway — you, 10 minutes

### 3a. The database

**New Project → Deploy PostgreSQL.** Nothing to configure.

Open the Postgres service → **Variables**. You need two of them:

| Variable | Looks like | What it is for |
|---|---|---|
| `DATABASE_PUBLIC_URL` | `…@viaduct.proxy.rlwy.net:41234/railway` | step 4, from your laptop |
| `DATABASE_URL` | `…@postgres.railway.internal:5432/railway` | the host you reuse in step 6 |

> **Do not give either of these to the bot.** Both are the `postgres`
> superuser, and row-level security does not apply to a superuser — not even
> with `FORCE`. Every query would keep working while every workspace read every
> other workspace's transcripts. The bot refuses to start if you try
> (`_assert_app_role_is_confined`), but the reason to know this is that the
> refusal will otherwise look like a bug.

### 3b. The service

**New → GitHub Repo → `Reclaimly/conductor-tg-bot`.**

- Branch: `main`
- `railway.toml` already selects the Dockerfile, the health check and the
  restart policy. Nothing to set.
- **Replicas: 1.** Confirm the service reads *1 Replica*. Two would fight over
  Telegram's `getUpdates` (409 Conflict) and only one may hold the supervisor
  lease.
- **No volume.** The image is stateless; every byte of state is in PostgreSQL.

Do not deploy yet — it has no variables. Railway will crash-loop it once and
that is expected.

---

## 4 · Create the two database roles — one command

From your laptop, with the repo checked out and `pip install -e '.[dev]'` done:

```bash
python -m ctb.db.bootstrap \
    --admin-dsn "<DATABASE_PUBLIC_URL from step 3a>" \
    --app-password "$(openssl rand -base64 24 | tr -d '/+=')" \
    --worker-password "$(openssl rand -base64 24 | tr -d '/+=')"
```

**Print those two passwords and keep them** — the command does not store them
anywhere and you need both in step 6. Generate them into variables first if
that is easier:

```bash
APP_PW=$(openssl rand -base64 24 | tr -d '/+=')
WORKER_PW=$(openssl rand -base64 24 | tr -d '/+=')
echo "app:    $APP_PW"
echo "worker: $WORKER_PW"
```

What it creates:

| Role | Row-level security | Used by |
|---|---|---|
| `ctb_app` | **enforced** | every handler, routing, FSM storage |
| `ctb_worker` | bypassed (`BYPASSRLS`) | supervisor, claim loops, prune, tenancy lookups |

`ctb_app` is deliberately not a member of `ctb_worker`, so there is no
`SET ROLE` path from a request to the bypass role.

It is idempotent — safe to re-run, and that is how you apply a future migration.

---

## 5 · Generate the master key — you

```bash
python -m ctb.rewrap --new-key v1
```

Output looks like `v1:8mK3n...`. This is what seals every customer's Conductor
API key.

> **Back this up somewhere that is not the database backup.** Together they are
> one secret; apart, neither is enough. Lose it and every stored key becomes
> permanently unreadable — every workspace would have to re-run `/key`.
>
> A password manager entry is fine. Note which `kid` (`v1`) is active.

---

## 6 · Service variables — you

> **Railway injects `DATABASE_URL` and `PGHOST`/`PGUSER`/`PGPASSWORD`/… when
> you link a database. Do not rely on any of them.**
>
> The injected `DATABASE_URL` is the **`postgres` superuser**, and row-level
> security does not apply to a superuser — not with `ENABLE`, not with `FORCE`.
> Every query would keep working while every workspace read every other
> workspace's transcripts and sealed keys. The isolation in this codebase is a
> property of *which role connects*, so connecting as the wrong one removes it
> entirely and nothing else changes.
>
> The bot refuses to start in that configuration (`_assert_app_role_is_confined`),
> which is the intended outcome but looks like a bug until you know why.
>
> The `PG*` variables are ignored — nothing in the code reads them. Leave them;
> they are harmless. **Overwrite `DATABASE_URL`** with the `ctb_app` URL.

Railway → the bot service → **Variables** → *Raw Editor*, and paste:

```env
TELEGRAM_BOT_TOKEN=<from step 2>
DATABASE_URL=postgresql://ctb_app:<APP_PW>@<internal-host>:5432/railway
SYSTEM_DATABASE_URL=postgresql://ctb_worker:<WORKER_PW>@<internal-host>:5432/railway
CTB_MASTER_KEYS=<from step 5>
PLATFORM_ADMIN_IDS=<your Telegram user id from step 2>
HEALTH_TOKEN=<openssl rand -hex 16>
REGISTRATION_OPEN=false
```

`<internal-host>` and the database name come from the Postgres service's own
`DATABASE_URL` — you are reusing its host and swapping the credentials. Using
the internal host keeps database traffic off the public proxy and off your
egress bill.

The four required ones are reported together on failure, so a boot error names
everything you forgot rather than one thing per redeploy.

Why the last two are in the *initial* list:

- `HEALTH_TOKEN` — without it, the detailed `/health` body is served to
  loopback only, so a public domain shows you a summary and nothing useful.
- `REGISTRATION_OPEN=false` — you are holding other people's API keys. Open it
  when you have watched it work, not before.

Everything else has a working default. The complete list with explanations is
in [`.env.example`](../.env.example). The ones you are most likely to want
later:

| Variable | Default | When to change it |
|---|---|---|
| `REGISTRATION_OPEN` | `true` | `false` until you trust it |
| `REGISTRATION_RATE_PER_HOUR` | `20` | lower if you get sign-up spam |
| `VOICE_ENABLED` | `false` | only after reading what it sends where |
| `LOG_LEVEL` | `INFO` | `DEBUG` while diagnosing, never for long |
| `LOG_TRANSCRIPT_CONTENT` | `false` | **leave it** — that is customer source code |
| `DB_POOL_MAX` | `10` | raise if `/health` shows connections waiting |

Do **not** set `PORT` or `HEALTH_PORT`. Railway injects `PORT` and the app
reads it.

---

## 7 · Deploy and verify

Railway deploys on save. Watch the logs for:

```
runtime.database_ready     schema_version=1
bot.built                  routers=[...]
runtime.ready
supervisor.lease_acquired
health.listening           port=8080
```

Then, from anywhere:

```bash
curl https://<your-domain>/health
# {"status":"ok","ok":true,...}

curl -H "Authorization: Bearer $HEALTH_TOKEN" https://<your-domain>/health
# pool stats, lease holder, per-workspace poller counts, recent API calls
```

If it crash-loops, the log line names the cause. The three that actually happen:

| Log says | Fix |
|---|---|
| `The database has no schema` | Step 4 did not run, or ran against a different database |
| `DATABASE_URL connects as a role that bypasses row-level security` | You used the superuser URL. Use `ctb_app`. |
| `Telegram rejected TELEGRAM_BOT_TOKEN` | Re-check with the `getMe` curl from step 2 |

---

## 8 · The phone checklist — you

This is the part no test covers. Follow
[`GETTING_STARTED.md`](GETTING_STARTED.md) as a real user:

1. `/register acme` in a private chat with the bot.
2. Create a private supergroup, turn on **Topics**, add the bot as an admin
   with *manage topics*, *pin*, *delete*, *send*.
3. `/setup <code>` in that group. Expect **Ready**.
4. `/key <your Conductor API key>` privately. It should confirm the key works,
   that it stored it, and that it deleted your message — and your message
   should be gone.
5. `/new fix the typo in the readme` in the group.
6. An answer arrives in a new topic, under a pinned status card.

If step 6 works, every layer works: Telegram polling, tenancy resolution, the
sealed key, the Conductor client, the transcript cursor, the delivery outbox
and the status card.

Then, before opening registration:

- `/platform list` — confirm you are recognised as the operator.
- Register a **second** workspace from a different Telegram account with a
  different Conductor key, and confirm neither sees the other's `/board`.
- Redeploy while a turn is running. The answer must arrive exactly once.

---

## What to give me, if you want me to do more

I can already reach the repository, so branches, code, CI and docs need nothing
from you. For anything that touches the live deployment I need one of:

| To do this | I need |
|---|---|
| Read deploy logs, restart, set variables | a Railway API token (Account → Tokens), or paste the logs |
| Diagnose a live database | the `SYSTEM_DATABASE_URL`, or the output of a query you run |
| Reproduce a Telegram bug | the exact message you sent and what came back |
| Set branch protection | nothing — GitHub refuses it to an integration; it is the UI |

**Do not paste `CTB_MASTER_KEYS`, the bot token, or a customer's Conductor key
into a chat with me or anyone else.** If one has already been pasted somewhere,
treat it as leaked: [`SECURITY.md`](../SECURITY.md) has the rotation runbook,
and the short version is that rotating the master key protects future data but
does not un-leak what an attacker already read.

---

## The ongoing loop

```bash
git checkout dev && git pull
git checkout -b fix-the-thing
# ... work ...
python -m pytest tests/test_the_thing.py -q     # just what you changed
python -m ruff format . && python -m ruff check . && python -m pyright
git push -u origin fix-the-thing
gh pr create --base dev
```

CI runs the full suite in four parallel shards plus a real boot. When
`all gates` is green, merge to `dev`. When you want it live, open `dev → main`
and merge; Railway deploys `main` on push.

Rolling back is Railway → Deployments → the previous one → **Redeploy**. The
image is stateless, so a rollback is a restart: in-flight deliveries are
re-claimed, the transcript cursor picks up where it left off, and open wizards
survive because their state is in the database.
