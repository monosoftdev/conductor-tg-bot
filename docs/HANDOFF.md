# Handoff

## Current state

The bot is multi-tenant on PostgreSQL. One Telegram bot token serves many
workspaces; each brings its own Conductor API key. Ready for a first deployment
and a live phone pass.

Verified offline, on every commit:

- **1,832 tests pass** against a real PostgreSQL 16.
- `ruff format --check`, `ruff check`, `pyright` — all clean.
- The real runtime boots against a real database: all six services start,
  `/health` returns `ok`, the lease is acquired, shutdown is clean.

## What changed from the single-user design

| | Before | Now |
|---|---|---|
| Storage | SQLite on a Railway volume | PostgreSQL, no volume |
| Identity | `ALLOWED_TELEGRAM_USER_IDS` | `tenants` + `tenant_members` + `tenant_chats` |
| Conductor key | one, from the environment | one per workspace, sealed in the database |
| Isolation | n/a | row-level security, two database roles |
| Owner | first id in a list | a role on a membership row |
| Backup | whole `.db` file to the owner | per-workspace `/export`; managed PG backups |
| Rate limiting | one 15/min bucket | global + per-chat budgets, per-destination rotor |
| Auth failure | stopped every poller | stops one workspace |

`docs/TENANCY.md` explains the isolation model and lists the seams that must
not be re-cut.

## Things that were fixed on the way, and why they matter

- **The delivery claim.** Under SQLite its correctness rested on
  `BEGIN IMMEDIATE`. Under MVCC that is not enough, so it is now one statement
  with `FOR UPDATE SKIP LOCKED` and a re-asserted `state = 'pending'`.
  `tests/test_claims.py` drains 400 rows through eight independent pools and
  asserts every row is claimed exactly once, with per-topic order intact.
- **Boot recovery could have stranded rows.** The new orphan window means a
  fast restart finds rows too young to touch; recovery therefore stays armed
  until a pass finds none left, rather than latching after the first.
- **`advance_cursor` needed an explicit row lock.** Recording messages, queueing
  their deliveries and moving the cursor is one indivisible step; SQLite gave
  that for free.
- **`/backup` was deleted, not ported.** The same code against a shared database
  would have handed one customer every other customer's transcripts.
- **`now()` vs `clock_timestamp()`.** `now()` is fixed at transaction start,
  which would have given every row in a batch the same `created_at` and flipped
  every `ORDER BY created_at` tiebreak.
- **A flaky test surfaced.** `test_wizard_state_round_trip_and_expiry` assumed
  the whole test ran inside one millisecond, which was true on SQLite and is not
  on PostgreSQL. It is now deterministic.
- **A chat-id collision across tenants used to raise "row vanished".** It now
  raises `ChatOwnedElsewhereError` and says what actually happened.

## Probe facts still carried in code

- Re-POSTing the same prompt `messageId` dedupes server-side (verified twice).
- The prompt ID is `content.id` on its user echo and `content.turnId` on every
  message in the turn. It is not the transcript envelope ID.
- Unknown/foreign `after=` IDs return 404 with no replay.
- `sessionIndex` is monotonic but not gap-free. Gaps never imply message loss.
- `POST /sessions` accepts a caller-supplied `sessionId`; only workspace create
  needs nonce reconciliation.
- Session status has a persistent third value, `error`.
- `GET /me` lives at the API root, outside `/v0`.

## Remaining live-only work

None of this can be proven offline.

1. Deploy to Railway with a real PostgreSQL and real secrets.
2. Walk the sign-up flow from a phone: `/register` → `/setup <code>` → `/key`
   → `/new` → an answer arrives.
3. Two real workspaces at once, each with its own key, staying separate.
4. Redeploy mid-turn; the answer arrives exactly once.
5. Watch `/health` for real Conductor and Telegram 429s, and tune the two rate
   budgets — the current numbers are under the documented ceilings but have
   never met real traffic.
6. Voice: a workspace storing its own speech key, then 30 owner recordings
   before moving `voice_mode` from `prompts` to `commands`.
7. Re-probe a sleeping workspace (probe assumption 8 is still unmeasured).
8. Curate real tool-heavy and error transcripts; the non-trivial renderer
   fixtures are still labelled synthetic.

## Before opening registration to strangers

You will hold other people's Conductor API keys — each can read every
transcript in their organisation and spend their money.

- Confirm with Conductor that brokering third-party keys is within their terms.
- Publish a privacy policy and terms. `/privacy` states what is stored today.
- Write the breach runbook: rotate `CTB_MASTER_KEYS`, re-wrap, notify.
- Back the master keys up **separately** from the database.
- Consider `REGISTRATION_OPEN=false` for the first weeks.
