# Handoff

## Current state

The bot is multi-tenant on PostgreSQL. One Telegram bot token serves many
teams; each brings its own Conductor API key. Ready for a first deployment
and a live phone pass.

**Sign-up is two private messages.** `/start` creates the team, `/key` stores
the Conductor key, `/new` opens a workspace — and its topic — in that same
private chat. A Telegram group is the optional `/team` flow, for several people
who want one shared topic list. Nothing in the default path asks anyone to
create a supergroup, enable Topics or grant admin rights.

Verified offline, on every commit:

- **2,031 tests pass** against a real PostgreSQL 16.
- `ruff format --check`, `ruff check`, `pyright` — all clean.
- The real runtime boots against a real database: all six services start,
  `/health` returns `ok`, the lease is acquired, shutdown is clean.

## The group became optional (2026-07-27)

Telegram now supports topics **inside a private chat with a bot** (@BotFather →
*Threaded Mode*). A bot may create, rename and delete them there with no admin
rights and no Premium; the sibling toggle *"Disallow users to create new
threads"* governs the **user**, and `BOT_FORUM_CREATE_FORBIDDEN` is never about
the bot. So the chat model is unchanged — one topic per workspace, routed on
`(chat_id, message_thread_id)` — and only its host is now free.

**This is the one thing in the repo built on an unverified Telegram feature.**
Bot API 10.0 (2026-05-08) carries an open regression: `sendMessage` with
`message_thread_id` in a private chat has been reported answering *"message
thread not found"*, and `createForumTopic` in DMs failing outright.
`scripts/probe_dm_topics.py` answers it against a live token in about five
seconds, and **nobody has run it yet.**

Everything is therefore written to degrade, and the degradation is the tested
path, not the hoped-for one:

- `dm_topic_support` refuses only on an explicit `has_topics_enabled: False`.
  Absent means try — the created topic is the only real proof.
- The topic is created **before** `POST /workspaces`, which has no idempotency
  key. A refusal after it would strand a paid container no retry can adopt.
- A DM refusal returns the linear `thread_id = 0` seat instead of raising, so
  the workspace is still created, the prompt still queued, the chat still
  works. One line says so, once per chat: *Topics unavailable here · one
  workspace at a time.*
- `send_html` retries once without `message_thread_id` when Telegram says the
  thread is gone, sharing `THREAD_GONE_MARKERS` with the delivery path.
- The group path is byte-identical to what it was, `/setup` probe included.

Known gap: `/board` adoption in a DM (`adopt.py`) is still linear — it does not
open a topic. `tests/test_bot_adopt.py` records it with an unchanged assertion,
so fixing it will fail that test rather than change behaviour silently.

## What changed from the single-user design

| | Before | Now |
|---|---|---|
| Storage | SQLite on a Railway volume | PostgreSQL, no volume |
| Identity | `ALLOWED_TELEGRAM_USER_IDS` | `tenants` + `tenant_members` + `tenant_chats` |
| Conductor key | one, from the environment | one per team, sealed in the database |
| Isolation | n/a | row-level security, two database roles |
| Owner | first id in a list | a role on a membership row |
| Backup | whole `.db` file to the owner | per-team `/export`; managed PG backups |
| Rate limiting | one 15/min bucket | global + per-chat budgets, per-destination rotor |
| Auth failure | stopped every poller | stops one team |

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

## The second adversarial review

A six-way parallel audit of the finished branch — isolation, onboarding, crypto,
delivery, tests, UX — found four criticals and eight highs, all now fixed. The
pattern is worth more than the list: **isolation, crypto and claiming were
excellent; the second-order paths were not.** Eviction on revoke, cache
invalidation, error-path ordering, pin accounting, retention, and four config
knobs that described protections which did not exist.

What was fixed, and why each mattered:

| | Was | Now |
|---|---|---|
| `/key` from a non-owner | refused *before* deleting — the key sat in Telegram forever | deleted first, always, and the reply says if deletion failed |
| Setup codes | bearer tokens; anyone holding one could bind their group to your team | bound to the issuing user, and `/team` re-issues instead of dead-ending |
| Callback budget | a fixed 40-char cap; a 36-char UUID + `archreq` hit exactly 64 bytes, 37 removed **every** button from the card | measured per action, degrades to a non-restart-proof button |
| Last owner | could demote themselves, orphaning the team permanently | refused unless a second owner exists |
| `ctb_app` grants | could `SELECT` every tenant's sealed key blob | no grant at all on the four scope-deciding tables |
| Delivery ordering | a network blip re-ordered a topic (`1, 2, 3, 0`) | a deferral stops the batch; a terminal drop does not |
| One chat's 429 | slept up to 60s on the single outbox task — every tenant stalled | inline only under 2s, then pause that chat and move on |
| Voice recovery | no orphan window; a redeploy re-transcribed and **re-billed** in-flight jobs | same 150s guard as deliveries |
| `/use` | invalidated the wrong cache entry, then raised forever on the second call | invalidates the tenant it left; `rebind_chat` makes it repeatable |
| Deleted topic | that turn's output lost silently and permanently | rerouted to the chat root (a group's General, or the DM itself) |
| Clean shutdown | released rows with no hash guard — a SIGTERM duplicated more readily than a crash | same `content_hash` guard as crash recovery |
| `deliveries` | never pruned; agent output outlived the 30-day transcript promise | pruned on the same window |
| `MAX_ATTEMPTS`, `REGISTRATION_RATE_PER_HOUR` | declared, documented, never read | enforced |
| `api_events.tenant_id` | always NULL, so per-team `/health` was blind | attributed |
| `StatusCards` | claimed to share the outbox pacer; did not | shares it, and reports its 429s |
| Supervisor | leaked a client pin when a poller ended by itself, so a decrypted key could never be swept | unpins on every exit path |
| `/voice` | `tenants.voice_enabled` gated the feature and **no command set it** | the command exists |

### The lesson from the first review, still the most important one

**One line in the test harness hid three bugs.** Both database fixtures put
a tenant in scope for the whole test, so every background task — the FSM
storage read that runs before *every* handler, the voice workers, the
status-card writer — looked correct and failed the moment it ran for real.
Self-serve registration was completely dead, and 1,832 passing tests said
otherwise.

The fixture no longer scopes the worker pool, and `tests/test_unscoped_workers.py`
runs the real code the way a background task does. Four other tests that passed
with their feature deleted now fail with it.

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
2. **Run `scripts/probe_dm_topics.py`.** Threaded Mode on in @BotFather, then
   `TELEGRAM_BOT_TOKEN` + `TELEGRAM_DM_CHAT_ID` and go. It answers four
   questions in order: can the bot create a DM topic, send into it, rename it,
   and set the state icon. Do this *first* — it decides whether the default
   install has a topic list or the linear fallback, and everything else on this
   list is cheaper to interpret once it is known.
3. Walk the default sign-up from a phone: `/start` → `/key` → `/new` → an
   answer arrives, all in one private chat.
4. Walk the optional group: `/team` → supergroup with Topics → `/setup <code>`
   → `/new` in General.
5. Two real teams at once, each with its own key, staying separate.
6. Redeploy mid-turn; the answer arrives exactly once.
7. Watch `/health` for real Conductor and Telegram 429s, and tune the two rate
   budgets — the current numbers are under the documented ceilings but have
   never met real traffic.
8. Voice: a team storing its own speech key, then 30 owner recordings
   before moving `voice_mode` from `prompts` to `commands`.
9. Re-probe a sleeping workspace (probe assumption 8 is still unmeasured).
10. Curate real tool-heavy and error transcripts; the non-trivial renderer
    fixtures are still labelled synthetic.

## Before opening registration to strangers

You will hold other people's Conductor API keys — each can read every
transcript in their organisation and spend their money.

- Confirm with Conductor that brokering third-party keys is within their terms.
- Publish a privacy policy and terms. `/privacy` states what is stored today.
- Write the breach runbook: rotate `CTB_MASTER_KEYS`, re-wrap, notify.
- Back the master keys up **separately** from the database.
- Consider `REGISTRATION_OPEN=false` for the first weeks.
