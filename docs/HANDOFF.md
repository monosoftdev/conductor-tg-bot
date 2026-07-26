# Handoff — 2026-07-26

## Current state

The full planned repository is implemented and ready for the first Railway
deployment/live phone pass.

Implemented:

- Conductor client with explicit User-Agent, retry policy, token bucket,
  concurrency cap, circuit breaker, fatal auth handling, and API event sink.
- SQLite/WAL schema, migrations, repository layer, 64 KB content cap,
  retention helpers, delivery claims, and singleton lease.
- Pure turn machine covering queued-idle, fast turns, persistent `error`,
  draining, cancellation, cursor-only fallback, and restart.
- Replay-safe transcript cursor, first-bind seek, offset repair, atomic
  cursor/message/delivery advance, and identical-ID prompt recovery.
- Per-session adaptive pollers and lease-gated supervisor with crash backoff.
- Defensive renderer, UTF-16 chunking, HTML fallback, document overflow,
  durable outbox, and edit-in-place status cards.
- Allowlisted aiogram application, DB-backed FSM, forum topics, daily/power/admin
  commands, safe callback nonces, and `/new` wizard.
- Structured runtime lifecycle, health server, Dockerfile, and Railway config.
- Mobile output policy: prompts ask for outcome-first, concise replies; bot
  chrome is short; tool noise stays in the status card.
- Mobile decisions: strict `Choices:` blocks become styled one-tap replies,
  with the recommended option first; prompt reactions move from 👀 to 👍.
- Topic-safe navigation: stable icon colors distinguish workspaces, General
  `/s` opens topics, and in-topic `/s` cannot cross workspace boundaries.
- Telegram voice notes and audio attachments: authenticated size/duration
  checks, in-memory download, ElevenLabs Scribe v2 adapter, strict multilingual
  wake-phrase parser, durable route snapshots, stable action IDs, restart
  recovery, retry controls, and health counters. Ordinary speech uses the same
  mobile prompt policy; General remains search-only.

## Verification

Offline:

- Ruff format/check: clean.
- Pyright: zero errors.
- Pytest: 1,537 tests passing.
- Docker image builds from the checked-in files.
- Production image smoke:
  - runs as non-root UID 10001;
  - applies migrations `001` and `002`;
  - starts Telegram, outbox, status cards, voice, supervisor, and health tasks;
  - acquires the singleton lease;
  - serves `GET /health` successfully.

**The smoke test does not prove `/data` is writable on Railway.** Docker seeds a
named volume from the image, ownership included, so the write always succeeds
locally; Railway mounts an empty root-owned volume over the same path and UID
10001 cannot open the database. `RAILWAY_RUN_UID=0` is the fix, and the image
now fails fast with `FATAL: cannot write /data …` instead of a bare
`sqlite3.OperationalError` crash-loop. No offline test can cover this.

The smoke run used fake credentials. It previously received a Telegram 401 and
kept serving a green `/health`; that is now a hard failure (see below).

## Probe facts carried into code

- Re-POSTing the same prompt `messageId` dedupes server-side (verified twice).
- The prompt ID is `content.id` on its user echo and `content.turnId` on every
  message in the turn. It is not the transcript envelope ID.
- Unknown/foreign `after=` IDs return 404 with no replay.
- `sessionIndex` is monotonic but not gap-free. Gaps never imply message loss.
- `POST /sessions` accepts a caller-supplied `sessionId`; only workspace create
  needs nonce reconciliation.
- Session status has a persistent third value, `error`; it is never treated as
  “keep waiting until idle.”
- `GET /me` lives at the API root, outside `/v0`.

Raw probe output remains gitignored in `probe-out/`.

## Reliability repairs made during final integration

- Newly created sessions are seeded at cursor `-1` before their first prompt,
  preventing a fast first answer from being skipped as historical content.
- New-workspace prompts are saved immediately and POSTed only after the
  workspace reports `ready`.
- Workspace creates derive a stable reconciliation nonce from the Telegram
  update, so an update replay checks the API/cache instead of issuing a second
  workspace POST; session creates remain caller-ID idempotent.
- Boot re-POSTs every recoverable prompt-ledger row from any cached machine
  state; recovery does not depend on a prior `SUBMIT_PENDING` state write.
- Retryable Telegram failures remain pending indefinitely; only definite
  permanent errors are parked as failed.
- `/stop` dispatches `Cancel` through the live poller instead of calling the API
  behind the state machine.
- Every status-card button is nonce-backed and has a production callback.
- `TELEGRAM_CHAT_ID` is enforced for group updates while owner/allowlisted DMs
  remain available.
- Transcript and expired-wizard retention runs on boot and daily while the
  singleton lease is held.
- Daily `VACUUM INTO` snapshots retain seven copies on the volume; owner
  `/backup` creates and downloads a fresh copy.
- Fatal Conductor authentication stops pollers and durably alerts the owner
  once.
- Machine actions fan out to status cards, durable notices, and real Telegram
  topic renames.
- Topic rename runs before its marker is persisted; unbind runs only after the
  final notice/topic action still has routing context.
- Ambiguous and unreachable prompt POSTs remain pending for identical-ID retry
  instead of being marked failed.
- Quiet/off chats send silent pushes; the 30-minute focus window promotes the
  most recently prompted topic, while errors remain loud.
- `/mode` is now the topic control panel, `/board` is capped and scannable,
  `/s` prioritizes active/recent work, and `/help` explains the fast loop.
- Every archive path now requires named two-tap confirmation, including
  timeout/status-card actions.
- Voice acknowledgements are edited into their result instead of adding a
  second bot bubble.
- Safe status controls remain usable for 15 minutes after a phone interruption;
  destructive confirmation still expires after 60 seconds.
- `/notify` is button-driven, completed confirmations edit in place, and the
  `/new` wizard reuses its single card for the final workspace link.
- The non-root image owns `/data`, and `.dockerignore` excludes secrets,
  transcripts, SQLite files, tests, and repository metadata from build context.

## Deploy-risk pass (first-hour failures)

Fixed after an audit reproduced these against the built image:

- **A transient 403 no longer latches the bot dead.** `client._request` reset
  `_auth_failures` only never; the supervisor treats `auth_failures > 0` as
  fatal and cancels every poller, so one proxy hiccup silently ended all
  delivery while commands kept answering. Any 2xx now clears the counter (a
  genuinely bad key never gets one). *Still latched: `supervisor._auth_fatal`,
  set when a poller crashes with `AuthFatal`, is never cleared — see blockers.*
- **A rejected `TELEGRAM_BOT_TOKEN` fails the deploy.** `run_polling` used to
  swallow `TelegramUnauthorizedError` and back off forever: green Railway
  deploy, healthy `/health`, a bot that answers nothing. It now propagates,
  with a log line naming `getMe` as the check.
- **`/health` reports Telegram.** `TelegramHealth` in `health.py` counts polling
  restarts that escape aiogram's own reader; any successful Bot API call clears
  it. Three in a row is the `telegram_polling` degradation (HTTP 200).
- **Missing configuration reports in one round.** `ALLOWED_TELEGRAM_USER_IDS`
  is now a required field checked in a *field* validator, alongside blank
  checks for both secrets, so all three arrive in a single message instead of
  one crash per variable. Blank `TELEGRAM_CHAT_ID=`/`ELEVENLABS_API_KEY=` (what
  `cp .env.example .env` leaves behind) now mean "unset" instead of failing
  validation.
- **`/health` detail is loopback-only without `HEALTH_TOKEN`.** Railway's edge
  reaches the container from a private address, so the old private/loopback
  gate published DB path, session ids and the last 20 API calls on any
  generated domain.
- **The volume trap is documented, not just detected.** README has a numbered
  "Before your first deploy" list led by `RAILWAY_RUN_UID=0`; the container
  preflights `DB_PATH`'s directory and says what to set.
- `.env.example` and README now match reality: exactly three required vars,
  `HEALTH_TOKEN`/`HEALTH_PORT` documented, and the false claim that `/setup`
  writes `TELEGRAM_CHAT_ID` removed (nothing prints a chat id — leave it unset).
- `/setup` and `/health` are in the BotFather command menu, so the command
  README tells you to run is offered when you type `/`.

## Remaining live-only work

The following cannot be proven offline:

1. Deploy to Railway with a real `/data` volume and real secrets.
2. Run the seven phone tests in `README.md`, especially redeploy mid-turn.
3. Re-probe a sleeping workspace (probe assumption 8 remains unmeasured).
4. Curate real tool-heavy/error/cancelled transcripts when the org has them;
   current non-trivial renderer fixtures are explicitly labelled synthetic.
5. Watch `/health` for real Conductor/Telegram 429s and tune the conservative
   request bucket if needed.
6. Set `VOICE_ENABLED=true`, add `ELEVENLABS_API_KEY`, and validate at least 30
   owner recordings before changing `VOICE_MODE` from `prompts` to `commands`.
   Compare Scribe v2 against the challengers in `VOICE_CONTROL_PLAN.md` before
   treating the provider choice as final.

The prior org had no useful real transcript corpus, the Codex session failed
with `Codex ChatGPT auth not found`, and the account reported
`overageDisabledReason: out_of_credits`. Claude/Sonnet completed probe turns.
