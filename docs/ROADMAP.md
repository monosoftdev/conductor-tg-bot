# Roadmap

Sequenced checklist derived from `PLAN.md`. Tick items as they land; `HANDOFF.md` holds the live
"you are here" note and any measured values that replace guesses.

The order is not arbitrary. Each stage removes an unknown that would otherwise be guessed at by the
next one — the probe sets the renderer's shapes and the poller's constants, and the pure state
machine is proven against a fake before any network or Telegram code exists to hide its bugs.

---

## Phase 0 — Probe · **measured; two live observations remain**

The load-bearing shapes are measured. Sleeping-workspace behavior and real
rate-limit tuning remain live-environment observations, not implementation blockers.

- [x] `scripts/probe_transcript.py`, split `dump` (read-only) / `assume` (writes)
- [x] `tests/test_probe_shapes.py` — offline coverage of the report logic
- [x] Run `dump --auto` → `probe-out/shape_report.md` + `transcripts.jsonl`
- [x] Run `assume --session <scratch>` → `probe-out/assumptions.md` + `timing_trace.json`
- [x] **Resolve assumption #7** — does re-POSTing the same `messageId` dedupe?
      *If no, the crash-safety model in PLAN.md §Cursor & dedup is invalid and must be redesigned
      before Phase 1.*
- [x] Resolve #1 (does our `messageId` appear as a transcript id), #3 (`after=` semantics),
      #4 (`after=<garbage>` — silent full replay?)
- [ ] Replace the guessed timing constants in `PLAN.md` with measured D1–D3 values
- [x] Curate a subset of `transcripts.jsonl` into `tests/fixtures/` (review first — real source code)
- [ ] Settle the three open items in `PLAN.md` §Open items (org vs workspace key scope,
      whether `session_transcripts_view.transcript` can back `/find`, observed rate limits)

## Phase 1 — Reliability core

No Telegram code. The whole phase is testable offline against `fakes/fake_conductor.py`.

- [x] `conductor/client.py` — explicit User-Agent, token bucket, semaphore, circuit breaker,
      per-endpoint timeouts, the `retryable` retry policy
- [x] `conductor/errors.py` — `StructuredError` mapping
- [x] `db/migrations/001_init.sql` + `db/connection.py` (WAL pragmas) + `db/repo/*`
- [x] `turn/machine.py` — pure `(state, evidence, clock) -> (state, [Action])`, all 23 transitions
- [x] `turn/cursor.py` — seek-to-end on first bind, delta fetch, `sessionIndex` validation,
      the offset-paging repair path
- [x] `turn/session_poller.py` + `turn/supervisor.py` — one task per bound session, lease-gated
- [x] `tests/fakes/fake_conductor.py` — scripted sequences
- [x] Tests green: queued-idle trap · fast turn · double prompt · error mid-turn · restart at both
      crash points · replay attack · overlapping pollers

**Exit criterion:** a turn survives a simulated redeploy mid-flight and delivers exactly once.

## Phase 2 — Rendering

- [x] `delivery/render/registry.py` + adapters, classified by shape not name
- [x] `delivery/render/html.py` — escape `& < >`, whitelist markdown→HTML, plaintext fallback
- [x] `delivery/render/chunk.py` — UTF-16-aware 4096 splitter, fence-safe
- [x] `delivery/outbox.py` — conditional claim, per-chat rate limit, document overflow
- [x] `delivery/status_card.py` — the edit-in-place turn card
- [x] `tests/test_render.py` against the Phase 0 fixtures + adversarial input
      (raw `<script>`, unbalanced backticks, 200 KB diff, emoji at the 4096 boundary)

**Exit criterion:** every probe fixture renders without raising and produces valid Telegram HTML.

## Phase 3 — Telegram UX

- [x] `bot/app.py`, allowlist middleware on **every** update type, DB-backed FSM storage
- [x] `/setup` — verify supergroup + `can_manage_topics`, else announce degraded DM mode
- [x] Topic lifecycle: create on workspace create, rename on state transition only, close on archive
- [x] Routing on `(chat_id, message_thread_id)`; `General` is search-only, never a prompt target
- [x] The six menu commands: `/new` `/board` `/stop` `/find` `/mode` `/done`
- [x] `bot/wizards/new_workspace.py` — edit-in-place, `Go with defaults →` on every step
- [x] Power commands, then admin (`/allow` `/deny` `/health`)
- [x] Safety rails: nonce'd callbacks, named confirm buttons, echo-with-undo, reply-to override

**Exit criterion:** the seven live phone tests in `PLAN.md` §Verification, especially #3 — redeploy
while a turn is running, reply still arrives exactly once.

## Phase 4 — Deploy

- [ ] Railway service + PostgreSQL, `numReplicas=1`, secrets set
- [ ] Confirm `overlapSeconds=0` genuinely stops the old instance first
- [ ] Watch `/health` for 429s and tune the two rate budgets
- [x] Per-workspace `/export`; the database's own backups replace `VACUUM INTO`

## Phase 5 — Voice and audio

- [x] Durable `voice_inputs` jobs with route snapshots and stable operation IDs
- [x] Telegram voice-note and audio handlers with pre-download duration/size checks
- [x] In-memory provider adapter for ElevenLabs Scribe v2; no automatic fallback
- [x] Exact multilingual wake phrases and non-fuzzy daily command aliases
- [x] Prompt/search dispatch, `/done` confirmation, failure Retry, restart recovery
- [x] Voice backlog/error/latency health counters and seven-day pruning
- [ ] Benchmark 30 private owner recordings against OpenAI and Groq challengers
- [ ] Run the live voice/redeploy/provider-outage acceptance suite on Railway

---

## Deliberately out of scope

Recorded so they don't get re-litigated:

- ~~Multi-user / multi-tenant.~~ **Done** — see `TENANCY.md`. One bot serves many workspaces,
  isolated by PostgreSQL row-level security, each with its own Conductor key.
- **Attributing replies to specific prompts.** The chat mirrors the transcript stream, which is what
  a chat UI should do.
- **Moving a prompt to another session after sending.** Would require un-sending from a running
  agent. Recovery is `/stop` then `[Resend to…]`.
- **A hard spend cap.** Visible counters, not paternalism.
- **Branch discovery.** The API exposes no branch listing; last-used memory plus free text, and we
  accept the gap rather than fake it.

## Phase 6 — Multi-tenancy on PostgreSQL

- [x] PostgreSQL replaces SQLite: psycopg pool, `?`→`%s` translator, squashed DDL
- [x] `FOR UPDATE SKIP LOCKED` claims, orphan window, per-session cursor lock
- [x] `tenants` / `tenant_members` / `tenant_chats`, row-level security, two roles
- [x] AES-256-GCM envelope encryption for stored keys, with rotation
- [x] `TenantMiddleware`, `TenantContext`, per-tenant `ClientPool` and `ProviderPool`
- [x] Per-tenant auth-fatal and poller fair share; global + per-chat rate budgets
- [x] Self-serve `/register` → `/setup <code>` → `/key`, and `/invite` for teammates
- [x] `/backup` deleted; `/export` is per workspace
- [ ] Live: two real workspaces, two real keys, on one deployment
