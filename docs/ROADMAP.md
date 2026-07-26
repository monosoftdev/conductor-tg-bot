# Roadmap

Sequenced checklist derived from `PLAN.md`. Tick items as they land; `HANDOFF.md` holds the live
"you are here" note and any measured values that replace guesses.

The order is not arbitrary. Each stage removes an unknown that would otherwise be guessed at by the
next one — the probe sets the renderer's shapes and the poller's constants, and the pure state
machine is proven against a fake before any network or Telegram code exists to hide its bugs.

---

## Phase 0 — Probe · **in progress**

Blocks everything. Nothing downstream should be written against guessed shapes.

- [x] `scripts/probe_transcript.py`, split `dump` (read-only) / `assume` (writes)
- [x] `tests/test_probe_shapes.py` — offline coverage of the report logic
- [ ] Run `dump --auto` → `probe-out/shape_report.md` + `transcripts.jsonl`
- [ ] Run `assume --session <scratch>` → `probe-out/assumptions.md` + `timing_trace.json`
- [ ] **Resolve assumption #7** — does re-POSTing the same `messageId` dedupe?
      *If no, the crash-safety model in PLAN.md §Cursor & dedup is invalid and must be redesigned
      before Phase 1.*
- [ ] Resolve #1 (does our `messageId` appear as a transcript id), #3 (`after=` semantics),
      #4 (`after=<garbage>` — silent full replay?)
- [ ] Replace the guessed timing constants in `PLAN.md` with measured D1–D3 values
- [ ] Curate a subset of `transcripts.jsonl` into `tests/fixtures/` (review first — real source code)
- [ ] Settle the three open items in `PLAN.md` §Open items (org vs workspace key scope,
      whether `session_transcripts_view.transcript` can back `/find`, observed rate limits)

## Phase 1 — Reliability core

No Telegram code. The whole phase is testable offline against `fakes/fake_conductor.py`.

- [ ] `conductor/client.py` — explicit User-Agent, token bucket, semaphore, circuit breaker,
      per-endpoint timeouts, the `retryable` retry policy
- [ ] `conductor/errors.py` — `StructuredError` mapping
- [ ] `db/migrations/001_init.sql` + `db/connection.py` (WAL pragmas) + `db/repo/*`
- [ ] `turn/machine.py` — pure `(state, evidence, clock) -> (state, [Action])`, all 23 transitions
- [ ] `turn/cursor.py` — seek-to-end on first bind, delta fetch, `sessionIndex` validation,
      the offset-paging repair path
- [ ] `turn/session_poller.py` + `turn/supervisor.py` — one task per bound session, lease-gated
- [ ] `tests/fakes/fake_conductor.py` — scripted sequences
- [ ] Tests green: queued-idle trap · fast turn · double prompt · error mid-turn · restart at both
      crash points · replay attack · overlapping pollers

**Exit criterion:** a turn survives a simulated redeploy mid-flight and delivers exactly once.

## Phase 2 — Rendering

- [ ] `delivery/render/registry.py` + adapters, classified by shape not name
- [ ] `delivery/render/html.py` — escape `& < >`, whitelist markdown→HTML, plaintext fallback
- [ ] `delivery/render/chunk.py` — UTF-16-aware 4096 splitter, fence-safe
- [ ] `delivery/outbox.py` — conditional claim, per-chat rate limit, document overflow
- [ ] `delivery/status_card.py` — the edit-in-place turn card
- [ ] `tests/test_render.py` against the Phase 0 fixtures + adversarial input
      (raw `<script>`, unbalanced backticks, 200 KB diff, emoji at the 4096 boundary)

**Exit criterion:** every probe fixture renders without raising and produces valid Telegram HTML.

## Phase 3 — Telegram UX

- [ ] `bot/app.py`, allowlist middleware on **every** update type, DB-backed FSM storage
- [ ] `/setup` — verify supergroup + `can_manage_topics`, else announce degraded DM mode
- [ ] Topic lifecycle: create on workspace create, rename on state transition only, close on archive
- [ ] Routing on `(chat_id, message_thread_id)`; `General` is search-only, never a prompt target
- [ ] The six menu commands: `/new` `/board` `/stop` `/find` `/mode` `/done`
- [ ] `bot/wizards/new_workspace.py` — edit-in-place, `Go with defaults →` on every step
- [ ] Power commands, then admin (`/allow` `/deny` `/health`)
- [ ] Safety rails: nonce'd callbacks, named confirm buttons, echo-with-undo, reply-to override

**Exit criterion:** the seven live phone tests in `PLAN.md` §Verification, especially #3 — redeploy
while a turn is running, reply still arrives exactly once.

## Phase 4 — Deploy

- [ ] Railway service, volume at `/data`, `numReplicas=1`, secrets set
- [ ] Confirm `overlapSeconds=0` genuinely stops the old instance first
- [ ] Watch `/health` for 429s and tune the token bucket up from its conservative 5 req/s
- [ ] Nightly `VACUUM INTO` snapshot + `/backup`

---

## Deliberately out of scope

Recorded so they don't get re-litigated:

- **Multi-user / multi-tenant.** Single owner plus a small allowlist. SQLite and the single-instance
  lease both assume this; revisit only if that changes.
- **Attributing replies to specific prompts.** The chat mirrors the transcript stream, which is what
  a chat UI should do.
- **Moving a prompt to another session after sending.** Would require un-sending from a running
  agent. Recovery is `/stop` then `[Resend to…]`.
- **A hard spend cap.** Visible counters, not paternalism.
- **Branch discovery.** The API exposes no branch listing; last-used memory plus free text, and we
  accept the gap rather than fake it.
