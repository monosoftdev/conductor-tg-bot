# Conductor Telegram Bot (`conductor-tg-bot`)

> **Historical.** This is the original single-user design, kept because its
> reasoning about the Conductor API, the turn state machine and the delivery
> contract is still exactly right and still implemented. Three things in it are
> now out of date: storage is PostgreSQL rather than SQLite, the bot serves
> many workspaces rather than one owner, and the private supergroup it assumes
> throughout is optional as of 2026-07-27 (see §Chat model). For the first two,
> read [`TENANCY.md`](TENANCY.md) — and where the two disagree, `TENANCY.md`
> wins.

## Context

You run many Conductor cloud agents in parallel, but you can only drive them from the Mac. Conductor
shipped a public HTTP API (`https://api.conductor.build/v0`) that exposes everything needed to run
the whole loop remotely: create workspaces, send prompts, read transcripts, cancel turns, archive.

The goal is a phone-first Telegram bot that makes a cloud agent feel like a person you're texting:
you type a message, the agent works, and the answer gets pushed back to you. No desktop required.

**The one hard constraint that shapes the whole design: the Conductor API has no webhooks and no
streaming.** The bot must poll. Everything below exists to make polling reliable enough that a reply
is never lost, never duplicated, and never announced as "done" before it arrives.

---

## Verified API facts (from `https://api.conductor.build/v0/openapi.json`, fetched 2026-07-25)

Auth: `Authorization: Bearer <key>`. **Must send an explicit `User-Agent`** — the proxy 403s some
default client signatures (docs call out Python `urllib`). Errors are
`{code?, userMessage, debugMessage?, retryable?, source?}`.

| Endpoint | Notes |
|---|---|
| `GET /v0/projects` | `{data:[{id,name,gitRemote}], offset, hasMore}` |
| `GET /v0/projects/{id}/workspaces` | only per-project listing exists |
| `POST /v0/workspaces` | `{projectId\|repositoryUrl, branch?, name?, sessionName?, agent, model?, effort?, env?}` → `201 {workspaceId, sessionId, deepLink}`. **No idempotency key.** |
| `GET /v0/workspaces/{id}` · `/rename` · `/archive` | archive is idempotent and restorable |
| `GET /v0/workspaces/{id}/status` | `initializing\|ready\|sleeping\|archived\|deleted\|updating` + `lifecycleStep` |
| `GET /v0/workspaces/{id}/sessions` · `POST /v0/sessions` | session carries `agent, model, effort, fastMode`; POST accepts a caller-supplied `sessionId` |
| `POST /v0/sessions/{id}/messages` | `{message, messageId?}` → `{messageId, state: queued\|sent}`. **`messageId` is a caller-supplied idempotency key** |
| `GET /v0/sessions/{id}/messages` | `{data:[{id, sessionId, sessionIndex, type, content, receivedAt}], offset, hasMore}`; `after=<messageId>` = exclusive incremental cursor, ascending `sessionIndex`; cannot combine with `offset` |
| `GET /v0/messages/{messageId}` | fetch one transcript envelope by its server-assigned id |
| `GET /v0/sessions/{id}/status` | `idle\|working\|error` + `errorMessage/lastError` |
| `POST /v0/sessions/{id}/cancel` | → `{status, canceledQueuedMessages}`; async, poll until idle |
| `POST /v0/sql` | read-only SELECT over **one view** `session_transcripts_view` |
| `GET /me` | identity / key scope |

`session_transcripts_view` columns: `session_id, workspace_id, transcript, session_title, agent_type,
model, workspace_name, workspace_state, repo_url, session_created_at, transcript_updated_at`.
Limits: 500 rows, 5s statement timeout, 10 000 char query, single statement, no writes, the literal
text `set_config` anywhere is rejected. **This view is also the only cross-org workspace listing** —
use it for `/board`, not N per-project calls.

Model ↔ agent pairing (a mismatch is a 400):
- `claude`: fable-5, opus-5-1m, opus-4-8-1m, opus-4-8, opus-4-7-1m, opus-4-7, opus-1m, opus,
  opus-4-6-1m, sonnet-5-1m, sonnet-4-6-1m, sonnet, haiku — effort `low…max`
- `codex`: gpt-5.5, gpt-5.4, gpt-5.6-{sol,terra,luna}, gpt-5.3-codex-spark, gpt-5.3-codex,
  gpt-5.2-codex — effort `none…ultra` (`max` needs a 5.6 model, `ultra` needs Sol/Terra)
- `cursor`: auto, composer-2.5, grok-4.5

Documented gotcha, verbatim: *"Wait for `working` before trusting `idle`. A queued prompt hasn't
started a turn yet, and the session reports `idle` until it does… A very fast turn can start and
finish between polls, so if `idle` persists, check the transcript for the reply directly."*

---

## Decisions locked

| Decision | Choice |
|---|---|
| Repo | A standalone repository, since made public under the MIT licence |
| Stack | Python 3.13, aiogram 3.30, httpx 0.28, aiosqlite, pydantic-settings, structlog |
| Hosting | One Railway service, Dockerfile builder, `numReplicas = 1`, volume at `/data`, Telegram **long polling** (no public webhook URL to manage) |
| Chat layout | Private Telegram **supergroup with Forum Topics**. One topic per workspace. `General` = cockpit. DM = degraded fallback mode |
| Storage | SQLite (WAL) on the Railway volume |
| Defaults | `claude` + `opus-5-1m` + `high` |
| Step 0 | Read-only probe of a real transcript, using a `CONDUCTOR_API_KEY` you add to Conductor's Environment settings |

---

## The core architectural idea

**The transcript cursor is the source of truth for content. `GET /status` is only a cadence knob and
a UX hint — it never gates delivery.**

Every listed hazard (queued-but-idle, turn that starts *and* finishes between polls, restart
mid-turn) is a *status* problem. None are *cursor* problems, because `after=<messageId>` is
monotonic and replayable. So the two layers are built and tested separately:

- **Delivery correctness** = cursor + dedup table. Runs unconditionally every tick, in every state.
- **Turn state machine** = drives poll cadence, the typing indicator, the status card, `/stop`.
  If it's wrong you get a stale progress line — you never lose or double-see a reply.

**The delivery path must never be gated on a state-machine conclusion.** That single rule is what
makes a 15s-polling API feel trustworthy.

---

## Phase 0 — Probe (blocks everything else)

`content` is literally `{}` (untyped) in the OpenAPI spec and `type` is a bare string. Writing the
renderer before seeing real data means guessing. `scripts/probe_transcript.py` is **read-only** and
produces two artifacts: a JSONL dump (which becomes the renderer's test fixtures) and a PASS/FAIL
assumption report.

**A. Dump.** Page through `GET /sessions/{id}/messages` for 3–4 real sessions covering: a trivial
Q&A turn, a long tool-heavy coding turn, a turn that errored, a cancelled turn. Write verbatim JSONL.

**B. Shape report.** Histogram of `type`; per type the recursive `content` key structure to depth 3
with value types; 3 pretty-printed samples per type; string-leaf length percentiles; which types
carry human prose vs machine payload.

**C. Assumption tests — each printed PASS/FAIL:**
1. Where does the POSTed `messageId` surface? *(Verified: `content.id` on the user echo and
   `content.turnId` on every message in the turn; the envelope `id` is server-assigned.)*
2. Is `sessionIndex` unique and monotonic? *(Verified: yes, but it is not gap-free.)*
3. `after=<valid id>` — exclusive? ascending? respects `limit`? sets `hasMore` correctly?
4. `after=<garbage id>` — 4xx, empty, or full replay? *(picks the fallback path)*
5. `after=<id from another session>` — behaviour?
6. Are `offset`/`hasMore` stable while the session is actively writing?
7. **Does re-POSTing the same `messageId` dedupe or create a second prompt?** *(linchpin of the
   whole crash-safety design)*
8. `POST .../messages` to a session whose workspace is `sleeping` — wake, error, or hang?

**D. Timing trace.** During one real turn, poll `/status` every 1s and `/messages` every 2s. Extract
`POST→working` latency, `working→idle` latency, whether `working` is ever observable for a trivial
turn, whether `idle` ever appears mid-turn. These numbers set the constants below.

**E.** `SELECT * FROM session_transcripts_view LIMIT 3` — see whether `transcript` is a clean
rendered string (if so it's a better source for `/find` than reassembling `/messages`).

Everything downstream is written against the JSONL from (A). Any type absent from the dump gets the
fallback path, not a guess.

---

## Phase 1 — Reliability core (no Telegram code yet)

### Turn state machine — `src/ctb/turn/machine.py`

Pure function `(state, evidence, clock) -> (state, [Action])`. No I/O, 100% table-driven unit tests.

States: `IDLE`, `SUBMIT_PENDING`, `QUEUED`, `WAKING`, `WORKING`, `DRAINING`, `CANCELLING`, `ERROR`,
`DEAD`.
Evidence: `POST_OK`, `POST_AMBIGUOUS`, `STATUS(idle|working|error)`, `DELTA(n, max_index)`,
`WS(status)`, `TIMER(t)`, `CANCEL`, `BOOT`, `E404`.

| # | From | Evidence | To | Effect |
|---|---|---|---|---|
| 1 | IDLE | POST_OK | QUEUED | `start_witnessed=false`, `outstanding++`, post status card "queued" |
| 2 | IDLE | DELTA (agent content) | WORKING | out-of-band activity — you drove the session from the Mac; mirror it |
| 3 | SUBMIT_PENDING | BOOT \| POST_AMBIGUOUS | QUEUED | **re-POST with the identical `messageId`** |
| 4 | QUEUED | STATUS(working) | WORKING | `start_witnessed=true`; card → "started"; begin typing indicator |
| 5 | QUEUED | DELTA(agent content or matching `turnId`) | WORKING | **covers the fast turn** — same tick then runs 12→15 |
| 6 | QUEUED | STATUS(idle) | QUEUED | **the trap: `idle` is structurally ignored while `start_witnessed=false`** |
| 7 | QUEUED | TIMER(90s, no delta, ws ready) | QUEUED | card → "queued behind another turn"; cadence → 10s |
| 8 | QUEUED | TIMER(10 min, never started) | ERROR | "prompt never started" + Retry (new `messageId`) |
| 9 | QUEUED/IDLE | WS(initializing\|sleeping\|updating) | WAKING | notify once with `lifecycleStep` |
| 10 | WAKING | WS(ready) | QUEUED | resume fast cadence |
| 11 | WAKING | TIMER(10 min) | ERROR | wake timeout |
| 12 | WORKING | DELTA(n>0) | WORKING | `last_delta_at=now`; deliver (delivery is state-independent) |
| 13 | WORKING | STATUS(idle) | DRAINING | **never declare done here** |
| 14 | DRAINING | DELTA(n>0) | WORKING | trailing content; DRAINING↔WORKING ping-pong is expected |
| 15 | DRAINING | STATUS(idle) ×3 consecutive, zero delta, `outstanding==0`, no turn still open | IDLE | finalize card "done in 1m32s". A turn is *open* until its own end-of-turn record (`rawPayload.type == "result"`) arrives — `idle` mid tool call is the same observation as `idle` at the end |
| 16 | DRAINING | STATUS(working) | WORKING | |
| 17 | any | STATUS(error) | ERROR | **force a full delta drain first** (partial output may exist), then show `errorMessage ?? lastError` + Retry |
| 18 | any | CANCEL | CANCELLING | POST cancel; report `canceledQueuedMessages` |
| 19 | CANCELLING | STATUS(idle) ×2 + drain | IDLE | "stopped · N queued dropped" |
| 20 | any | E404 | DEAD | unbind topic, stop task, notify |
| 21 | WORKING | TIMER(20 min no delta) | WORKING | warn "no output for 20m"; at 60m cadence → 30s (watchdog, not a kill) |
| 22 | QUEUED/WORKING/DRAINING/WAKING | BOOT | same | **forced delta + status + ws status before any conclusion** |
| 23 | IDLE | TIMER(idle) | IDLE | cadence decay 20→30→60→120s |

Second prompt while working (case c): send straight through — Conductor queues server-side,
`outstanding++`, and rule 15 won't finalize until every posted prompt is witnessed (or ages out at
5 min). Replies are **not attributed to individual prompts**; the chat mirrors the transcript stream.

**Status-free fallback:** if `/status` fails or 429s for K consecutive polls, enter cursor-only mode —
fixed 8s cadence, `WORKING` inferred from `last_delta_at < 45s`, finalize after 45s of quiet with
`outstanding == 0`. UX degrades; delivery does not.

### Cursor & dedup — `src/ctb/turn/cursor.py`

- `cursor_message_id` is the primary replayable `after=` cursor.
  `cursor_session_index` is the monotonic replay/overlap guard and offset-repair boundary; gaps are
  valid and never imply message loss.
- **First bind = seek to end, never replay.** No count endpoint exists, so: exponential probe with
  `limit=1` at `offset = 0,1,2,4,8,…` until `hasMore == false`, then binary-search the boundary
  (~2·log₂N calls, once). Render only the single most recent agent message as a "now mirroring: …"
  preview.
- Every delta response is validated: drop anything with `sessionIndex <= cursor_session_index`
  (protects against `after` being ignored and the transcript replaying); on 4xx for an unknown
  `after` id, fall back to offset paging to find the first `sessionIndex >` cursor and repair the id;
  page within a tick while `hasMore` (bounded to 10 pages).
- Atomic advance:
  ```sql
  BEGIN;
    INSERT OR IGNORE INTO transcript_messages(...);   -- PK (session_id, message_id)
    INSERT OR IGNORE INTO deliveries(... state='pending');
    UPDATE sessions SET cursor_message_id=?, cursor_session_index=?;
  COMMIT;
  ```
  Cursor never advances past unrecorded messages ⇒ no drops. `INSERT OR IGNORE` ⇒ no duplicates
  even under replay or overlapping pollers.
- **Idempotent prompt POST:** write the `outbound_prompts` row with a `uuid4()` `messageId`
  *before* any HTTP; POST; mark posted. Ambiguous outcome (timeout/reset/5xx) → retry with **the
  same `messageId`** forever with backoff. A crash between write and response is recovered on boot
  by rule 3.
- **Asymmetry to respect:** `POST /workspaces` has **no** idempotency key. Never blind-retry it.
  Embed a nonce in the generated `name` (`tg-<chatid>-<nonce>`) and reconcile via
  `GET /projects/{id}/workspaces` before deciding to re-create. `POST /sessions` accepts a
  caller-supplied `sessionId`, which is always generated before the call and safely reused.

### Client — `src/ctb/conductor/client.py`

Singleton `httpx.AsyncClient` with: explicit `User-Agent: conductor-tg-bot/<ver>`, token bucket
(5 req/s, burst 10), `Semaphore(8)`, circuit breaker (3 consecutive 5xx → OPEN 60s jittered →
HALF_OPEN probe → CLOSED), per-endpoint timeouts (connect 5s; read 20s status/messages; 30s POST
message; 60s `/sql`).

Retry policy: connect/timeout/502/503/504 → full-jitter backoff base 0.5s cap 30s ×5; `retryable:
true` → retry regardless of code; `retryable: false` → never; 429 → honor `Retry-After` and open the
circuit; 401/403 → **fatal**, stop all pollers, DM owner once, keep bot alive for `/health` (never
retry — avoids key lockout); 404 on session/workspace → `DEAD`.

### Poller — `src/ctb/turn/{session_poller,supervisor}.py`

One asyncio task **per bound session** (not one global loop) — per-session adaptive cadence is the
whole point, and a global loop serializes a slow `/messages` behind everything else. Supervisor
reconciles DB bindings → task set every 5s and restarts crashed tasks with backoff.

| State | Interval (±20% jitter) | Calls |
|---|---|---|
| unbound | task doesn't exist | 0 |
| IDLE (recent) → cold | 20s → 30 → 60 → 120s | `/messages` |
| QUEUED | 3s (0–15s) → 5s → 10s | `/messages` + `/status` |
| WAKING | 10s | workspace `/status` |
| WORKING | 6s | `/messages` each tick, `/status` every 2nd (≈12s) |
| DRAINING | 2s ×3 | both |

Worst realistic case (3 sessions working) ≈ 0.75 req/s.

### Persistence — `src/ctb/db/migrations/001_init.sql`

SQLite + WAL on the Railway volume. Rationale: one writer, <100 writes/min, all point lookups;
backup is one file. **A Railway volume attaches to exactly one instance — that constraint prevents
the two-replica overlap problem structurally rather than mitigating it.** Pragmas:
`journal_mode=WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON`. Numbered `.sql`
migrations + `schema_version`, no ORM. The `db/repo/*.py` layer is the seam if this ever needs
Postgres.

Tables: `allowed_users`, `chats` (routing key **`(chat_id, thread_id)`**, defaults, verbosity),
`workspaces` (cache + `deep_link` + topic id), `sessions` (cursor fields + turn-machine fields +
`status_card_msg_id` + `poll_interval_ms`), `outbound_prompts` (`message_id` PK = the Conductor
idempotency key, state), `transcript_messages` (PK `(session_id, message_id)`),
`deliveries` (PK `(session_id, message_id, part_index, chat_id)`, `state`, `claim_id`,
`content_hash`), `wizard_state` (aiogram FSM, DB-backed so wizards survive restart),
`singleton_lease`, `api_events` (ring buffer for `/health`), `unknown_content_types`.

**Redeploy overlap, defence in depth:** (1) volume single-attach + `numReplicas=1`;
(2) `singleton_lease` 15s TTL heartbeated every 5s — the supervisor refuses to spawn without it and
cancels all tasks if it loses it; (3) Telegram's own `getUpdates` 409 Conflict treated as "another
instance live", logged and retried, never a crash-loop; (4) even with two live pollers,
`INSERT OR IGNORE` + the conditional `pending→sending` claim make duplicate posts impossible.

Delivery worker claims rows with `UPDATE deliveries SET state='sending', claim_id=? WHERE
state='pending'` in `(session_index, part_index)` order. The only residual window is a crash between
the Telegram call and the DB write: on boot, `'sending'` rows are re-sent (**at-least-once, chosen
deliberately** — a rare duplicate beats a silently lost reply), guarded by a `content_hash` check
against the preceding `sent` row so the common case is skipped.

---

## Phase 2 — Rendering

### Adapters — `src/ctb/delivery/render/`

Registry of adapters, each `matches(type, content) -> bool` / `render(msg, verbosity) -> [Block]`,
tried in order, `UnknownAdapter` last. **Classify by shape, not by name** — normalize type strings
but never trust them alone. Every adapter wrapped in try/except: a raising adapter degrades to
`UnknownAdapter`, never crashes the poller.

| Bucket | Default |
|---|---|
| assistant text / final answer | **show** — the primary content |
| user echo of our own prompt | suppress |
| thinking / reasoning | suppress; `verbose` → collapsed `<blockquote expandable>` |
| tool call | suppress body; feed a one-line activity string into the **status card**, ≤1 edit/3s |
| tool result | suppress; `verbose` → first 500 chars in `<pre>` |
| file edit / diff | one-line `path +12 −3`; "show diff" button → `.diff` document |
| error | **always show**, at every verbosity |
| system / meta | suppress |
| unknown | record in `unknown_content_types`; best-effort text extraction, else silent-but-counted |

Best-effort extractor (the safety net): recursive walk collecting strings from the key names seen in
the probe (`text, content, message, output, body, value`) plus bare strings and block lists.

### Telegram formatting — `parse_mode="HTML"`, never MarkdownV2

MarkdownV2 requires escaping 18 characters *including inside code spans*; agent output is nothing
but those characters and one miss is a `400` and a lost reply. HTML needs only `& < >`, and
`<pre><code class="language-python">` covers code blocks natively.

- Chunk at 4096 **UTF-16 code units**, not Python characters. Split at paragraph → line → whitespace
  → hard cut; never inside a tag or entity; close and reopen `<pre><code>` across a boundary with a
  `…(cont.)` marker. Each part is its own `deliveries` row so partial delivery resumes.
- ≤3500 chars → one message; 3500–7000 → two; >7000 → head + `… +N more` + a `turn-<id>.md`
  **document** (Telegram previews `.md` in a scrollable searchable viewer — one tap beats six bubbles).
  Any single code block >40 lines → `[code block, 120 lines →]` inline, full text in the document.
- `link_preview_options.is_disabled = True` everywhere.
- **Belt and braces:** any `TelegramBadRequest` mentioning entity parsing → exactly one retry with
  `parse_mode=None` and unmarked text. A reply may look ugly; it is never dropped.

---

## Phase 3 — Telegram UX

### Chat model: Forum Topics on, one topic per workspace

The address of a prompt is the topic your thumb is in — there is no "bound session" variable to lose
track of. This is why it beats a pinned card in a flat chat (which labels the mess but doesn't stop
ten agents interleaving), reply-to routing (precise but long-press-Reply-type on *every* prompt),
and a per-message header line (solves recognition, not navigation).

Free from Telegram: the topic list *is* the switcher, with native unread badges, native search,
per-topic mute, and per-topic push. Routing key is `(chat_id, message_thread_id)`, so DM mode falls
out as `thread_id = NULL` — a degraded single-bound-session mode with `/s` to switch, which the bot
announces once.

> **What shipped, 2026-07-27: the supergroup became optional.** This section was written
> "private supergroup, Forum Topics on", and for the whole of the single-user design and the
> multi-tenant rewrite that was literally required: sign-up did not finish until you had created a
> supergroup, enabled Topics and granted the bot four admin rights. That was three screens of
> Telegram settings standing between a new user and their first prompt, and it was the single
> largest drop-off in the flow.
>
> Telegram then shipped **topics inside a private chat with a bot** (@BotFather → *Threaded Mode*).
> A bot may create, rename and delete them there with **no admin rights and no Premium**; the
> sibling toggle *"Disallow users to create new threads"* governs the *user*, and
> `BOT_FORUM_CREATE_FORBIDDEN` is never about the bot. Non-Premium accounts get the default icon
> pack, which is the only pack this repo has ever used.
>
> So the chat model above is unchanged — one topic per workspace, addressed by
> `(chat_id, message_thread_id)` — and only its *host* is now free. The default is a private chat:
> `/start` → `/key` → `/new`, and the topic opens in the DM. A group is the optional `/team` flow,
> for several people who want one shared topic list.
>
> **The DM topic is the one path that may not be trusted.** Bot API 10.0 (2026-05-08) carries an
> open regression where `sendMessage` with `message_thread_id` in a private chat answers *"message
> thread not found"*, and `createForumTopic` in DMs has been reported failing outright.
> `scripts/probe_dm_topics.py` answers it against a live token. Until it does, every DM-topic path
> is written to degrade: the topic is created *before* the paid workspace, a refusal returns the
> linear `thread_id = 0` seat above rather than raising, and `send_html` retries once without the
> thread. A DM topic we cannot open costs the topic list and nothing else — never the prompt, the
> workspace or the answer.
>
> The group path is byte-identical to what it always was, including the `/setup` capability probe.

> Superseded by `docs/NAMING.md`, which is the current rule. The glyphs below
> were replaced by the one vocabulary in `ctb/signals.py`, and the label now
> leads with the task: `⚙️ fix the login bug · proj/branch`.

| Conductor state | Topic name |
|---|---|
| workspace initializing | `⏳ <task> · proj/branch` |
| ready + session idle | `<task> · proj/branch` |
| turn finished, unread | `✅ <task> · proj/branch` |
| session working | `⚙️ <task> · proj/branch` |
| session error | `⚠️ <task> · proj/branch` |
| workspace sleeping | `💤 <task> · proj/branch` |
| archived/deleted | topic deleted; `🗄 <task> · proj/branch` and closed if it cannot be |

Renamed only on state *transitions*, never on a timer (rename is an API call).

### Commands

BotFather menu — the daily loop. *(Shipped with seventeen: this six plus
`/attach`, `/s`, `/fork`, `/notify`, `/setup`, `/invite`, `/use`, `/health`,
`/register`, `/key` and `/help` — see `src/ctb/bot/app.py`.)*

| Command | Syntax | Help | Driven by |
|---|---|---|---|
| `/new` | `/new [<project>:] <prompt>` or bare | New workspace and first prompt. Bare = wizard. | hybrid |
| `/board` | `/board` | All live sessions with status. Tap to jump. | keyboard, edits in place |
| `/stop` | `/stop` | Cancel this session's turn and queue. | no confirm |
| `/find` | `/find <text>` | Search every transcript in the org. | argument |
| `/mode` | `/mode` | Change agent / model / effort for this session. | keyboard |
| `/done` | `/done` | Archive this workspace. Closes the topic. | 2-tap confirm |

Power (via `/help`, not in the menu): `/s [query]` switch bound session · `/fork [name]` new session
in this workspace · `/name <text>` rename (`-w` for workspace) · `/open` deep link · `/desk` handoff
card · `/log [n]` raw messages as `.md` · `/notify loud|quiet|off` · `/defaults` · `/sql <SELECT>` ·
`/tidy` close archived + 7-day-idle topics.
Admin: `/allow <id>` · `/deny <id>` *(shipped as `/invite` and `/remove`)* · `/health` (poll lag, circuit state, last 20 `api_events`,
pending deliveries, unknown content types, lease holder, uptime).

Rule applied throughout: nothing destructive is argument-driven, nothing frequent is
keyboard-driven except `/board`, which *is* the keyboard.

### Creation flow

**Zero-tap path:** `/new fix the flaky payment webhook test` → resolve defaults → create workspace →
create topic → queue the prompt for delivery on `ready`. One line of typing, zero taps. The defaults
card is posted *after* creation starts with a `Change` button, so you retune during init.
Project resolution: explicit `api:` prefix match, else last project used in this topic's lineage,
else globally last used.

**Bare `/new` wizard:** project → branch → agent → model → effort → prompt, every step carrying a
`Go with defaults →` button (1 tap minimum, 6 maximum). Each step *edits the same message* — the
wizard never grows the chat. Branch step is `[default]` `[last used for this project]` `[type…]`
because the API exposes no branch listing. `Cancel` on every step; any `/command` abandons the wizard
silently rather than trapping you in a modal.

**Defaults memory:** per-project `(project_id) -> {branch, agent, model, effort}` written on every
successful create (repo A is a codex repo, repo B is a claude repo — stop asking), plus a global
last-used project. Cold start: `claude` / `opus-5-1m` / `high`.

**The 30–90s init wait:** the prompt is captured *before* the wait, so it's dead time you never spend
staring. Topic created instantly; one **pinned status card** edited in place every 5s with elapsed
time and an ETA from the rolling median of this project's last 10 inits (coarse block bar, no fake
percentages); on `ready` the card flips to working, the queued prompt goes out, one push. Past
3× median or on `error`, the card becomes a failure card with `Retry` / `Archive`. You can fire
another `/new` from `General` while any number of workspaces initialize.

### Turn presentation

Header line on every completed turn (redundant in a topic, but survives forwarding and the roll-up):

```
● api/fix-flaky · opus-5-1m/high · 1m42s · 12 tools
```

The **pinned status card** per topic carries the live state and absorbs the tool-call noise:
`⏳ queued` → `▶️ started` → `⚙️ working 1m20s · running pytest` → `✅ done in 1m32s · 3 files`.
Buttons: `Stop` while running; `Transcript` / `Retry` / `Open in Conductor` (the `deepLink`) when
finished. `sendChatAction("typing")` every 4s while WORKING. No transient "🔄" messages that need
cleanup. At 10 min with no new messages the card adds `stalled?` + a `Check` button.

`/desk` produces the "I'm back at my Mac" handoff card: workspace, branch, last turn's one-liner,
tool count, `Open in Conductor`.

### Notifications with many agents

Topics do the heavy lifting — three agents finishing produce three messages in three topics, nothing
interleaves. What remains is push policy: `/notify loud|quiet|off`, default **quiet** (silent push,
badge). **Focus rule:** the session you last prompted is automatically `loud` for 30 min, then decays
to quiet — "I asked, tell me when it's done" with zero configuration, without 15 background agents
buzzing all evening. Errors always push loud, once per session per hour. When ≥2 turns finish within
60s and you're in none of those topics, `General` gets exactly one *edited* roll-up:

```
3 turns finished
[✓ api/fix-flaky] [✓ web/nav] [! infra/tf]
```

Outbound throttle: one global send queue ~15 msg/min prioritizing (1) the topic you're in,
(2) errors, (3) everything else. Pinned-card edits coalesce — three queued edits send only the last.

### Safety rails

- **`General` never prompts.** Plain text in the cockpit is treated as `/find`, answered with results
  plus `Send this to <last session>?`. The single most likely misroute is structurally impossible.
- **Echo with undo:** every prompt gets an instant `→ api/fix-flaky` reply with a `[Stop]` button, so
  a wrong-topic mistake is visible in under a second. Text sent to a working session echoes
  `→ queued (2 pending)` with `[Clear queue]` — honest, since cancel returns the dropped count.
- **Reply-to override:** replying to any bot message routes to *that* message's session regardless of
  topic — the escape hatch when you notice mid-typing.
- Confirm buttons **contain the name** (`[ Archive api/fix-flaky ]`) so a mis-tap is visibly wrong
  before it's fatal. Callback payloads carry `(action, id, nonce)` with
  single-use nonces: destructive confirmation expires in 60 seconds, while
  safe phone controls remain usable for 15 minutes. A stale button fails closed.
- `/stop` is never confirmed — friction on cancel is worse than an accidental cancel.
- Since `opus-5-1m/high` is your default, the heavy-cost confirm fires only on `effort ∈ {max, ultra}`.
  `/board` carries a running today's-turn counter instead of a hard budget.
- Allowlist checked on **every** update type (messages, callbacks, inline, edits). Non-allowlisted
  users get **silence** (a rejection reply just confirms the bot exists); owner gets one DM per
  unknown user per day.
- `/find` **never concatenates user text into SQL** — fixed template, escaped literal, character
  allowlist, hard `LIMIT 20`, `truncated` surfaced. Owner-only `/sql` is the labelled escape hatch.
  The blast radius is every workspace's transcripts, so treat injection as real.

### Logging & secrets

structlog → JSON on stdout, bound `request_id, chat_id, session_id, workspace_id, turn_state,
endpoint, status_code, duration_ms, attempt`. One line per state transition
(`turn.transition from=QUEUED to=WORKING evidence=delta`) — that's the debugging surface.
A **scrubbing processor is mandatory**: redact `Authorization`, `Bearer \S+`, and exact matches of
configured secret values anywhere in the event dict. Secrets via `pydantic-settings` + `SecretStr`
from Railway env only; fail fast at boot if `TELEGRAM_BOT_TOKEN`, `CONDUCTOR_API_KEY`, or
`ALLOWED_TELEGRAM_USER_IDS` are missing. **Transcript content is your source code** — gate content
logging behind `LOG_TRANSCRIPT_CONTENT=false` (default), cap stored `content_json` at 64 KB/message,
prune `transcript_messages` older than 30 days.

When Conductor is down: first failure silent (most blips resolve within a poll interval); circuit
opens → **one** message per outage, "⚠️ Conductor API unreachable. Your prompt is saved and will be
sent automatically" with the pending count — true, not a comforting lie, because prompts are
persisted before the HTTP call; recovery → "✅ Conductor is back" and the outbox flushes.

---

## Repo layout

```
conductor-tg-bot/
├── pyproject.toml            # aiogram>=3.30, httpx>=0.28, aiosqlite, pydantic-settings, structlog, aiohttp
├── Dockerfile                # python:3.13-slim, non-root
├── railway.toml              # numReplicas=1, volume /data, healthcheck /health, restart ON_FAILURE
├── .env.example
├── scripts/probe_transcript.py       # PHASE 0 — run first
└── src/ctb/
    ├── __main__.py           # settings → migrate → lease → TaskGroup(bot, supervisor, health)
    ├── settings.py  logging.py  health.py
    ├── db/{connection,migrate}.py  db/migrations/001_init.sql  db/repo/*.py
    ├── conductor/{client,errors,models}.py
    ├── turn/{state,machine,cursor,session_poller,supervisor}.py
    ├── delivery/{outbox,status_card}.py  delivery/render/{registry,html,chunk,adapters/*}.py
    └── bot/{app,keyboards}.py  bot/middleware/*  bot/handlers/*  bot/wizards/new_workspace.py
tests/
├── test_machine.py           # every row of the transition table
├── test_cursor.py            # replay, id-disappeared, hasMore paging
├── test_dedup.py             # crash points, overlapping pollers
├── test_render.py            # fixtures = probe JSONL + adversarial markdown
└── fakes/fake_conductor.py   # scripted status/delta sequences: queued-idle trap, fast turn, error
```

**Build order:** probe → `conductor/client` → db+repos → `turn/machine` (pure, fully tested against
the fake) → poller+supervisor → delivery → bot. **The pure state machine tested against scripted
`fake_conductor` sequences is where the reliability actually comes from — write it before any
Telegram code.**

---

## Verification

**Phase 0.** Run `probe_transcript.py` against a real session. Every assumption test prints PASS/FAIL;
tests 1, 3, 4 and 7 must be resolved before Phase 1 constants are finalized. Deliverable: the JSONL
dump committed as test fixtures + the shape report in the repo README.

**Phase 1 (offline, no network).** `fake_conductor.py` replays scripted sequences and
`test_machine.py` asserts the resulting state + actions for each:
- *queued-idle trap* — `POST → idle, idle, idle, working, idle×3`: must not finalize before the
  `working` observation.
- *fast turn* — `POST → idle, [delta with final answer], idle×3`: must deliver and finalize despite
  never observing `working`.
- *double prompt* — two POSTs while working: exactly two prompts witnessed, one finalize.
- *error mid-turn* — must drain partial output *before* posting the error.
- *restart* — kill the process between DB write and POST, and between Telegram send and DB write;
  assert exactly-one prompt and no duplicate delivery.
- *replay attack* — `after=` returns the whole transcript; assert `sessionIndex` filter drops it all.
- *overlapping pollers* — two supervisors on one DB; assert one holds the lease and zero duplicate
  `deliveries` rows reach `sent`.

**Phase 2.** `test_render.py` runs every probe fixture through the registry and asserts: no
exceptions, valid Telegram HTML (round-tripped through an HTML parser), correct UTF-16 chunk
boundaries, code fences never split open, unknown types counted not crashed. Plus adversarial inputs:
raw `<script>`, unbalanced backticks, a 200 KB diff, emoji at the 4096 boundary.

**Phase 3, live.** Against a real Conductor account, on the phone:
1. `/new <prompt>` on a real project → topic appears, init card ticks, prompt fires on ready, reply
   arrives. Time it.
2. `/stop` mid-turn → reports dropped count, session returns idle.
3. Redeploy on Railway *while a turn is running* → the reply still arrives exactly once
   (this is the headline test).
4. Kill the Conductor key → the 401 path DMs you and stops pollers without crash-looping.
5. Three concurrent workspaces → roll-up in `General`, no interleaving, no Telegram 429.
6. `/find` on a phrase you know is in an old transcript.
7. `/done` → workspace archived, topic deleted (closed where the bot may not delete).

**Rollback:** the bot only writes to Conductor via prompts, workspace create, and archive — all
reversible. Stopping the Railway service stops everything; nothing in Conductor depends on the bot.

---

## Open items to settle during Phase 0

- Does `POST /workspaces` with an **org** API key require the repo be present on the org machine?
  The spec says so — verify against your key's scope via `GET /me` before building the create flow.
- Whether `session_transcripts_view.transcript` is good enough to power `/find` result previews
  directly (probe step E).
- Conductor API rate limits are undocumented. The token bucket starts conservative (5 req/s) and
  `/health` surfaces any 429s so it can be tuned up.
