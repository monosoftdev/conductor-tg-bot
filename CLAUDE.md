# CLAUDE.md — conductor-tg-bot

A Telegram bot that drives [Conductor](https://conductor.build) cloud coding agents from a phone.
Single-user tool. Python 3.13 + aiogram 3 + httpx + SQLite, one always-on Railway service.

**This repo is standalone.** It is not part of `reclaimly-be` and shares no code with it. Ignore any
reclaimly-be conventions (Alembic migrations, `schema/db.sql`, DB attribute guides, org statuses) —
none apply here.

## Read first

- `docs/PLAN.md` — the full approved design. Transition tables, DDL, UX, verification plan.
- `docs/HANDOFF.md` — what's done, what's next, what's blocked.
- `README.md` — the short version.

## The rule that decides everything

> **The transcript cursor is the source of truth for content. `GET /status` is only a cadence knob
> and a UX hint — it never gates delivery.**

The Conductor API has no webhooks and no streaming, so the bot polls. Every hazard (a queued prompt
that reports `idle`, a turn that starts *and* finishes between two polls, a redeploy mid-turn) is a
*status* problem, never a *cursor* problem — `after=<messageId>` is monotonic and replayable.

So: delivery correctness runs unconditionally on every tick in every state. The turn state machine
only drives cadence, the typing indicator, and the status card. **Never gate a `sendMessage` on a
state-machine conclusion.** If the state machine is wrong you get a stale progress line; you never
lose or double-see a reply.

## Conductor API facts that are easy to get wrong

- `POST /sessions/{id}/messages` takes a caller-supplied `messageId` — an idempotency key. Write the
  DB row *before* the HTTP call and retry ambiguous failures forever with the same id.
- `POST /workspaces` and `POST /sessions` have **no** idempotency key. Never blind-retry them.
  Reconcile via a nonce embedded in the generated workspace name.
- Always send an explicit `User-Agent`. The API is behind a proxy that 403s some default client
  signatures.
- `POST /v0/sql` reads exactly one view, `session_transcripts_view`. It is also the **only**
  cross-org workspace listing — there is no global `GET /workspaces`.
- A transcript message's `content` is untyped (`{}`) and `type` is a bare string. Classify by shape,
  never by name alone.
- Models must match the agent or you get a 400. See `docs/PLAN.md` for the pairing table.
- From the docs: *"Wait for `working` before trusting `idle`. A queued prompt hasn't started a turn
  yet, and the session reports `idle` until it does."*

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

- `TELEGRAM_BOT_TOKEN` and `CONDUCTOR_API_KEY` come from env only. Never commit them, never log
  them — structlog runs a mandatory scrubbing processor.
- **Transcript content is the user's source code.** `LOG_TRANSCRIPT_CONTENT=false` by default;
  stored content capped at 64 KB/message; `transcript_messages` pruned after 30 days.
  `probe-out/` is gitignored for the same reason.
- `/find` never concatenates user text into SQL — fixed template, escaped literal, char allowlist,
  hard `LIMIT`. The blast radius is every workspace's transcripts.
- `scripts/probe_transcript.py assume` sends real prompts and costs real tokens. Scratch sessions
  only.

## Commands

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .
.venv/bin/python -m pyright
```
