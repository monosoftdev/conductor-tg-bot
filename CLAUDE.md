# CLAUDE.md — conductor-tg-bot

A Telegram bot that drives [Conductor](https://conductor.build) cloud coding agents from a phone.
**Multi-tenant**: one bot token serves many workspaces, each with its own Conductor API key.
Python 3.13 + aiogram 3 + httpx + PostgreSQL, one stateless Railway service.

**This repo is standalone.** It shares no code with any other project, so conventions from a
sibling repository — migration tooling, schema layout, DB attribute guides — do not apply here.

## Read first

- `docs/GETTING_STARTED.md` — the user-facing walkthrough. Read it before
  changing any onboarding message; the two must not drift.
- `docs/HANDOFF.md` — what's done, what's next, what's blocked. **Start here.**
- `docs/TENANCY.md` — how isolation works, and the seams you must not cut.
- `docs/PLAN.md` — the original single-user design. Transition tables, UX, verification plan.
- `README.md` — the short version.

## The two rules that decide everything

> **1. The transcript cursor is the source of truth for content. `GET /status` is only a cadence
> knob and a UX hint — it never gates delivery.**

The Conductor API has no webhooks and no streaming, so the bot polls. Every hazard (a queued prompt
that reports `idle`, a turn that starts *and* finishes between two polls, a redeploy mid-turn) is a
*status* problem, never a *cursor* problem — `after=<messageId>` is monotonic and replayable.

So: delivery correctness runs unconditionally on every tick in every state. The turn state machine
only drives cadence, the typing indicator, and the status card. **Never gate a `sendMessage` on a
state-machine conclusion.** If the state machine is wrong you get a stale progress line; you never
lose or double-see a reply.

> **2. Tenant isolation is a database guarantee, not a code-review guarantee.**

Every tenant-scoped table has `tenant_id` defaulted from the `ctb.tenant_id` GUC, with row-level
security ENABLEd *and* FORCEd. `Database` publishes that GUC on every connection checkout. So repo
SQL contains **no `WHERE tenant_id = ?` anywhere** — and a forgotten filter returns zero rows rather
than another customer's data.

Two roles. `ctb_app` runs under the policies; `ctb_worker` holds `BYPASSRLS` and is used only by the
cross-tenant workers (supervisor reconcile, the delivery and voice claim loops, prune, and the
tenancy lookups that decide scope in the first place). `ctb_app` is **not** a member of `ctb_worker`,
so there is no `SET ROLE` path from the request path to the bypass role.

**Never add a process-wide Conductor client, database handle or API key back.** Deleting the
`get_client()` global is what makes a cross-organisation read impossible to write by accident: a
handler that forgets `tenant: TenantContext` now fails by name instead of quietly using the wrong
key.

## Conductor API facts that are easy to get wrong

- `POST /sessions/{id}/messages` takes a caller-supplied `messageId` — an idempotency key. Write the
  DB row *before* the HTTP call and retry ambiguous failures forever with the same id.
- `POST /workspaces` has **no** idempotency key. Never blind-retry it; reconcile via a nonce
  embedded in the generated workspace name. `POST /sessions` accepts a caller-supplied
  `sessionId`, so generate it before the call and reuse it on retries.
- The prompt `messageId` is `content.id` on its user echo and `content.turnId` on every message in
  that turn; it is not the server-assigned envelope `id`.
- `sessionIndex` is monotonic but not gap-free. Never infer message loss from a gap.
- Always send an explicit `User-Agent`. The API is behind a proxy that 403s some default client
  signatures.
- `POST /v0/sql` reads exactly one view, `session_transcripts_view`, scoped entirely by the API key.
  With per-tenant clients that is now the right scope by construction — but only because the client
  is the tenant's.
- A transcript message's `content` is untyped (`{}`) and `type` is a bare string. Classify by shape,
  never by name alone.
- Models must match the agent or you get a 400. See `docs/PLAN.md` for the pairing table.
- From the docs: *"Wait for `working` before trusting `idle`. A queued prompt hasn't started a turn
  yet, and the session reports `idle` until it does."*

## PostgreSQL facts this code depends on

- Repo SQL is written with `?` placeholders and translated at the `Database` boundary
  (`ctb.db.sqlparam`). Never write `%s` in a repo module — it would be double-escaped.
- Timestamp defaults use `clock_timestamp()`, never `now()`. `now()` is fixed at transaction start,
  so a batch inserted by one `advance_cursor` call would share a `created_at` and flip every
  `ORDER BY created_at` tiebreak.
- Every id and epoch-ms column is `bigint`. Telegram ids exceed int32.
- `deliveries.claim` uses `FOR UPDATE SKIP LOCKED`, so concurrent workers take disjoint sets.
  `recover_orphaned` only reclaims rows whose `claimed_at` is older than `ORPHAN_AFTER_MS`, because
  a fresher one may still be in flight on an overlapping deployment.
- `transcript.advance_cursor` takes `SELECT … FOR UPDATE` on its session first. Under SQLite that
  serialisation came free from `BEGIN IMMEDIATE`; here it must be asked for.
- `transaction()` binds a connection to the *task id*, because `asyncio.create_task` copies the
  context. Without that, a child task would issue statements on its parent's connection.
- `content_json` and `payload_json` are `text`, not `jsonb`: agent output can contain NUL escapes,
  which `jsonb` rejects, and losing a delivery to one is not a trade worth making.
- The application never applies DDL. `python -m ctb.db.bootstrap` does, once, as an operator.

## Conventions

- Line length 88, ruff format + ruff check + pyright must all be clean before committing.
- Type hints on every signature. `async`/`await` for all I/O.
- Telegram parse mode is **HTML, never MarkdownV2**. MarkdownV2 needs 18 characters escaped
  *including inside code spans*; agent output is nothing but those characters and one miss is a 400
  and a lost reply. Any entity-parse `TelegramBadRequest` retries once with `parse_mode=None`.
- Chunk Telegram text at 4096 **UTF-16 code units**, not Python characters.
- Never let a renderer bug stall delivery. Every adapter is wrapped; a raising adapter degrades to
  the unknown-type path.

## Safety

- `TELEGRAM_BOT_TOKEN`, the two DSNs and `CTB_MASTER_KEYS` come from env only. Never commit them,
  never log them — structlog runs a mandatory scrubbing processor.
- **Tenant API keys are never registered with the log scrubber.** A process-wide set of every
  customer's plaintext key would keep it in memory for the life of the process and make scrubbing
  O(tenants) per line. They appear only in an `Authorization` header, which `_BEARER_RE` already
  redacts unconditionally.
- Stored keys are sealed with AES-256-GCM, AAD-bound to `(kid, tenant_id, purpose)`. A row swap in
  the database therefore fails authentication rather than moving a key between customers.
- **Transcript content is the customer's source code.** `LOG_TRANSCRIPT_CONTENT=false` by default;
  stored content capped at 64 KB/message; `transcript_messages` pruned after 30 days.
  `probe-out/` is gitignored for the same reason.
- **Voice leaves the perimeter.** Each workspace stores its *own* speech key; there is deliberately
  no shared fallback, so nobody is billed or exposed through somebody else's account. The bot sends
  `enable_logging=false`, but zero retention is enterprise-only.
- An API key sent to a *group* is refused **and** the message deleted, with a rotate-it warning.
  Sent privately it is validated, sealed, and its message deleted. Telegram keeps history forever.
- `/find` never concatenates user text into SQL — fixed template, escaped literal, char allowlist,
  hard `LIMIT`. Its blast radius is one organisation's transcripts, and only that one.
- `scripts/probe_transcript.py assume` sends real prompts and costs real tokens. Scratch sessions
  only.

## Running tests

> **Run only the tests for what you changed. CI runs the whole suite, in
> parallel, and nothing merges until it is green.**

The full suite is ~2,000 tests. Running all of it after every edit is the single
biggest waste of time in this repo, and it buys nothing a shared runner cannot
buy more cheaply. So locally:

```bash
docker compose up -d --wait db                     # once per session
.venv/bin/python -m pytest tests/test_outbox.py -q         # the file you touched
.venv/bin/python -m pytest tests/ -q -k "callback or nonce"  # or by keyword
.venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .
.venv/bin/python -m pyright
```

Pick the target by what the change can *break*, not by what it edits — a repo
change breaks its callers, a middleware change breaks the handlers behind it. If
you cannot name the tests that cover a change, that is the finding; write one.

Run the full suite locally only when you have a reason: a change to
`db/connection.py`, `migrations/`, `keyboards.py`, `outbox.py` or the middleware
chain touches everything, and so does anything you are about to tag.

```bash
.venv/bin/python -m pytest tests/ -q                       # ~30s, all of it
.venv/bin/python -m pytest tests/ -q -m "not db"           # ~2s, no Docker
.venv/bin/python -m pytest -q --splits 4 --group 2         # one CI shard
```

**CI is the gate, not your laptop.** `.github/workflows/ci.yml` runs format,
lint, types, the offline subset, four test shards and a real boot smoke — all in
parallel, each shard against its own PostgreSQL. The `all gates` job is the one
required check; it fails if any job fails, was skipped, or was cancelled.

Two things that are not negotiable there:

- `CI=true` turns "no database" from a skip into a **failure**. Otherwise a DSN
  typo hides every RLS, isolation, crypto and claim test behind a green tick.
- Before claiming a test has teeth, break the code it covers and watch it fail.
  An adversarial review once deleted ten `is_owner` gates at once and eight of
  them killed no test.
