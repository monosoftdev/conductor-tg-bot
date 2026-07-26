# conductor-tg-bot

Drive [Conductor](https://conductor.build) cloud coding agents from Telegram, from your phone.

Type a message, the agent works, the answer gets pushed back to you. No desktop required.

## Status

**Phase 0 — probe.** The renderer and poller can't be written until we've seen real API data.
Nothing else is implemented yet.

## The constraint that shapes everything

The Conductor v0 API has **no webhooks and no streaming**. The bot must poll. Everything in the
design exists to make polling reliable enough that a reply is never lost, never duplicated, and
never announced as "done" before it arrives.

The core rule:

> **The transcript cursor is the source of truth for content. `GET /status` is only a cadence knob
> and a UX hint — it never gates delivery.**

Every hazard (a queued prompt that reports `idle`, a turn that starts *and* finishes between two
polls, a redeploy mid-turn) is a *status* problem. None are *cursor* problems, because
`after=<messageId>` is monotonic and replayable. So delivery correctness runs unconditionally on
every tick in every state, and the turn state machine only drives cadence, the typing indicator,
and the status card. If the state machine is wrong you get a stale progress line — you never lose
or double-see a reply.

## Phase 0: the probe

The OpenAPI spec types a transcript message's `content` as `{}` — completely untyped — and `type`
as a bare string. Writing a renderer against that means guessing. `scripts/probe_transcript.py`
answers it against the live API, and also tests eight load-bearing assumptions the poller design
depends on.

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'
export CONDUCTOR_API_KEY=...     # https://app.conductor.build/users/api-keys

# READ-ONLY — dumps transcripts, reports content shapes, samples the SQL view
.venv/bin/python scripts/probe_transcript.py dump --auto

# WRITES — sends throwaway prompts to ONE scratch session you nominate
.venv/bin/python scripts/probe_transcript.py assume --session <SCRATCH_SESSION_ID>
```

Output lands in `probe-out/` (gitignored — it contains real transcript text, which is your source
code). The curated fixtures that get committed live in `tests/fixtures/`.

### What `assume` is actually testing

| # | Assumption | Why it matters |
|---|---|---|
| 1 | The `messageId` returned by `POST .../messages` appears as a transcript `message.id` | Defines "this prompt has been witnessed" |
| 2 | `sessionIndex` is unique, monotonic, gap-free | It's the real cursor; `after=` is just an optimization |
| 3 | `after=<id>` is exclusive, ascending, respects `limit`/`hasMore` | The incremental read path |
| 4 | `after=<garbage id>` → 4xx, empty, or **full replay** | Picks the fallback path; a silent full replay would re-post an entire transcript to Telegram |
| 5 | `after=<id from another session>` | Same |
| 6 | `offset`/`hasMore` are stable during an active turn | Whether paging can be trusted mid-turn |
| 7 | **Re-POSTing the same `messageId` dedupes** | The linchpin. The entire crash-safety design is "write the row, POST, retry forever with the same id". If this creates two prompts, that design is invalid |
| 8 | POST to a sleeping workspace — wake, error, or hang? | Not automated; needs a sleeping workspace |

Plus a timing trace (`/status` @1s, `/messages` @2s through a real turn) that produces the actual
`POST→working` and `working→idle` latencies. Those numbers set the poller's constants instead of
guesses — and the trace directly measures how many `idle` polls arrive *before* the turn starts,
which is the trap the docs warn about:

> *"Wait for `working` before trusting `idle`. A queued prompt hasn't started a turn yet, and the
> session reports `idle` until it does."*

## Design

Full plan: `~/.claude/plans/i-want-to-create-linear-quokka.md`.

- **Chat model** — a private Telegram supergroup with forum topics, one topic per workspace,
  `General` as the cockpit. The address of a prompt is the topic your thumb is in, so there's no
  "bound session" variable to lose track of. Routing key is `(chat_id, message_thread_id)`; DM
  falls out as `thread_id = NULL`, a degraded single-session mode.
- **Storage** — SQLite (WAL) on a Railway volume. A volume attaches to exactly one instance, which
  prevents the two-replica overlap problem structurally rather than mitigating it.
- **Formatting** — Telegram HTML, never MarkdownV2. MarkdownV2 needs 18 characters escaped
  *including inside code spans*; agent output is nothing but those characters, and one miss is a
  `400` and a lost reply.
- **Idempotency** — the `outbound_prompts` row is written *before* any HTTP call, keyed by a
  client-generated `uuid4()` that doubles as Conductor's idempotency key. `POST /workspaces` and
  `POST /sessions` have **no** such key, so they are never blind-retried — they reconcile by
  listing and matching a nonce embedded in the generated name.

## Layout

```
scripts/probe_transcript.py   Phase 0 — run first
src/ctb/
  conductor/     API client: retry, token bucket, circuit breaker
  turn/          machine.py is a pure (state, evidence) -> (state, actions) fn
  delivery/      outbox, status card, the defensive renderer
  bot/           aiogram handlers, wizards, keyboards
tests/
```

Build order: probe → client → db → `turn/machine` (pure, fully tested against a fake) → poller →
delivery → bot. The pure state machine tested against scripted failure sequences is where the
reliability actually comes from; it gets written before any Telegram code.

## Development

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .
.venv/bin/python -m pyright
```

## Secrets

`TELEGRAM_BOT_TOKEN` and `CONDUCTOR_API_KEY` come from the environment only — never committed,
never logged (structlog runs a mandatory scrubbing processor). Transcript content is your source
code: content logging is off by default (`LOG_TRANSCRIPT_CONTENT=false`), stored content is capped
at 64 KB/message, and `transcript_messages` is pruned after 30 days.
