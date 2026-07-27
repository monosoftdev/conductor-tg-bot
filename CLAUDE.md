# CLAUDE.md — conductor-tg-bot

A Telegram bot that drives [Conductor](https://conductor.build) cloud coding agents from a phone.
**Multi-tenant**: one bot token serves many workspaces, each with its own Conductor API key.
Python 3.13 + aiogram 3 + httpx + PostgreSQL, one stateless Railway service.

**This repo is standalone.** It is not part of `reclaimly-be` and shares no code with it. Ignore any
reclaimly-be conventions (Alembic migrations, `schema/db.sql`, DB attribute guides, org statuses) —
none apply here.

## Read first

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

## Commands

```bash
docker compose up -d --wait db
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .
.venv/bin/python -m pyright
```
