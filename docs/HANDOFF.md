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

- **2,094 tests pass** against a real PostgreSQL 16.
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

*(The gap this section used to record — `/board` adoption in a DM staying
linear — is fixed; see "New Chat is a composer" below.)*

## A DM with topics had three dead ends (2026-07-28)

The first live pass on a topics-enabled DM found the same complaint from three
directions — *"`/new` doesn't do anything and then it says there is no
session"*. Each has its own cause and its own test; all three are fixed.

1. **The root was orphaned, not a cockpit.** `/new` binds the session to the
   topic it just opened, so the DM root holds nothing — and answered "No session
   here" to every line typed or dictated into it. It now answers the way a
   cockpit does: one *Send to «task»* button, shared by the typed and the spoken
   path through `core.cockpit_markup` so they cannot drift. Deliberately **not**
   General's search: a task typed into your own chat wants sending, not looking
   up. With nothing ever run there is no cockpit to be, and the old nudge stands.
2. **`/s` moved a session instead of reaching it.** Binding re-addresses the
   transcript, so `/s` from the root pointed a topic-bound session at thread 0
   and its topic went quiet — the recovery broke what it was recovering.
   `power.homed_elsewhere` now decides: a workspace with a room of its own is
   *opened* (a jump button where Telegram gives a link, its name where it does
   not, as in a DM), never switched to. The callback re-checks, so a button
   minted before the topic existed is refused rather than obeyed.
3. **One wizard served every seat in a chat.** aiogram keyed FSM state on
   `(chat, user)`, so a half-finished `/new` swallowed the next line typed in
   *any* topic and spent it on a second workspace. Fixed in two halves that only
   work together: `FSMStrategy.USER_IN_TOPIC`, plus
   `RoutingMiddleware._publish_seat`, because Telegram omits `is_topic_message`
   in a private chat and aiogram would otherwise read no thread at all.
   `tests/test_middleware.py::test_each_dm_topic_gets_its_own_wizard` drives the
   production `create_dispatcher` for exactly that reason.

One thing found on the way and worth remembering: **`jump_url` is `None` for a
private chat.** Telegram publishes no link syntax for a DM topic, so `/new` there
had produced a bare `→ label` and no button — which is why it read as having
done nothing. `common.created_card` now names the room instead, and is the one
face `/new`, the wizard and adopt all use.

## "New Chat" is a composer, not a room (2026-07-28)

The first live pass on a threaded DM produced a topic list reading *New Chat ·
/new · We would love…/dev · /attach · /attach · /attach · We have som…/main* —
three workspaces' worth of rooms and four pieces of litter. One misunderstanding
caused all of it, and it was ours:

**Telegram's *New Chat* seat cannot be typed into.** It is a thread composer. A
message sent there makes the client open a *new thread*, named after that first
line, and the bot's update arrives already inside it. So `/new` was never
handled at the DM root — it was handled inside a thread called "/new", and then
opened a *second* topic beside it for the workspace. Two rooms per `/new`, one
of them permanently empty; `/attach` the same.

Everything below follows from taking that seriously.

- **The room a request is standing in is the room it gets.**
  `common.claimable_thread` answers "is this a private chat's thread with
  nothing bound to it?" from the `Route` and nothing else, and `_seat_for`
  claims that thread instead of creating a sibling. Claimed, not created — so
  it is never discarded on a failed create, and it *is* renamed afterwards,
  because Telegram named it "/new" and the topic list is the navigation.
  `apply_marker(force=True)` exists for exactly that: the stored title is a
  claim about a room nobody has ever renamed.
- **An empty thread is a task composer.** Plain text there used to answer "No
  session here", which was true and useless — the person had just said what they
  wanted. It now starts `NewWorkspace.confirm`: the task read back, the project
  and model that would be used, and one **▶️ Start workspace** button. Typing
  again *replaces* the task. Nothing bills until the button is tapped.
- **`/attach` in a DM was delivering into a room nobody can read.** `adopt.py`
  skipped both the topic-create and the remembered-topic paths for
  `chat_type == "private"`, binding to thread 0 — fine in a linear DM, a black
  hole in a threaded one. That is the "I failed to attach" report. Both paths
  now run in a DM, and the second `/attach` of a workspace jumps instead of
  opening a sibling.
- **A reply keyboard is the only control surface a threaded DM has.** There is
  no message the bot can pin in *New Chat* and no room it can answer in, but a
  reply keyboard belongs to the chat. `keyboards.home_keyboard` is deliberately
  two entries — **➕ New workspace** and **📎 Attach existing** — because both
  *consume* the thread their press creates. A `/board` button was drafted and
  cut: it answers and abandons the room, and in a threaded DM the thread list
  already is the board, with live state icons no message could draw.
  `handlers/home.py` sits at router order 4, ahead of the wizard, so a press
  mid-`/new` means start over rather than "the branch is called ➕ New
  workspace".
- **`/attach` had one sentence for three situations.** "No unattached cloud
  workspace matches" meant *nothing exists*, *everything is already open*, and
  *the view has no row for it yet* — and the common one is the least alarming.
  `core.nothing_to_attach` counts the union of the transcript view and the local
  cache, because a workspace opened a minute ago is in neither the view nor the
  list it was filtered out of.

**The one new unverified assumption:** whether Telegram lets a bot
`editForumTopic` a thread the *user* created in a DM. The docs say a bot may
manage "bot forum topics", which may or may not include this one. No probe can
answer it — the bot cannot make a user type — so `topics.claim_topic` reads all
three answers off that single call and only one of them is a refusal: renamed,
*or* "cannot rename but the thread is there" (use it, leave `topic_marker` NULL
so the next transition retries, and let the room keep the first line of the
task, which is a reasonable name for it), *or* thread-not-found (open a real one
instead). Nothing else depends on the rename landing.

That call is also the claim path's **proof of life**, and it has to be: a
claimed room gets the same contract as a created one — *the only proof that a
room is usable is an API call that used it*. `require_topic` exists because a
paid container bound to a room that does not exist is unrecoverable, and the
confirm card puts a human-length pause between "Telegram opened a thread" and
"we spend money", which is plenty of time to delete it from the phone.

### What the review caught, and what it says about the shape

An eight-angle adversarial pass over the finished branch found nine real
defects. Every one of them was in a *second-order* path — the first-order flows
were right — and two of them share a root worth naming:

- **A second rule for the same question.** `claimable_thread` began as a helper
  in `handlers/common.py`, so the voice worker — which has no `Route`, only a
  durable row — asked "is this seat empty?" its own way and got a different
  answer: a workspace whose session row had not landed yet was *taken* to text
  and *empty* to voice. It is now `Route.claimable_thread`, beside `is_dm` and
  `is_topic`, and the voice worker rebuilds the `Route` rather than the
  predicate. The rule is one property, in the place that already owns the seat.
- **A reply keyboard is a promise the bot has to keep.** It is persistent and
  chat-wide, so `/start` handed one to strangers, whose presses are plain text,
  are therefore dropped in silence, *and* page the owners with a stranger
  notice. It now appears only where both buttons work — a private chat of a
  team with a key (`power._launchable`) — and `📎 Attach existing` passes
  `query=""` explicitly, because `command_text` was reading the button's own
  label as a search term and answering "nothing matches" every time.

The rest, each with a test that fails when the fix is removed: the wizard never
checked the workspace quota (harmless while it was the long way round, not now
it is the front door); a line typed at a wizard step with no text handler
started a rival task and killed the live card's buttons; `⚙️ Change` could not
reach the project step, so the one field the card *guesses* was the one it could
not correct; a re-dictated task posted a second card instead of editing the
first; `adopt_callback` suppressed its reply on the assumption a card had landed
rather than on the card having landed (CLAUDE.md: never gate a `sendMessage` on
a conclusion); `/attach` counted workspaces that exist rather than rooms this
chat holds, and re-ran `board_rows` to do it.

## The topic icon was the same on every topic (2026-07-28)

A topic carries state in two places and only one of them was moving. The name
prefix changed on every transition; ``icon_custom_emoji_id`` — the badge you
actually scan a list by — did not, for two independent reasons:

- **Presentation selectors.** Telegram's pack serves `⚡️` (U+26A1 U+FE0F) where
  the state table asked for `⚡`. The lookup was an exact string match, so it
  missed — and an unresolved icon is *not* an error: aiogram omits an unset
  optional, and Telegram keeps the existing value for an omitted field. Every
  rename returned success and the badge never moved. `icon_key` now compares by
  identity, and each state names several acceptable emoji instead of one, so a
  pack that lacks the first choice costs a fallback rather than the icon.
- **`IDLE` and `SLEEPING` both asked for 💤.** A topic is idle most of its life,
  so one shared sleep badge was most of what "they all look the same" was.

The pack is also warmed at boot rather than on first rename — fetched lazily,
the first state change after a deploy was the one that got no icon — and
`scripts/probe_topic_icons.py` prints what a live token is actually offered.
The emoji in `_TOPIC_ICONS` are still educated guesses until somebody runs it.

## One notification per task, not twenty (2026-07-28)

A single prompt could vibrate a phone eight times before the work was done: an
agentic turn narrates, and every assistant message was a push. Three changes,
none of which touches delivery — content is still queued and sent exactly as
before, only the push flag and the destination of *progress* moved.

- **A file edit is progress, so it goes on the card.** `📝 path +12 −3` was a
  chat bubble per edited file: names you cannot act on from a phone, arriving
  ahead of the answer that explains them. It is now an `ActivityLine`, which
  lands on the one pinned message that is edited in place, and the count lands
  on the finished card and in the completion line. `/mode verbose` still puts
  the line *and* the patch in the chat.
- **`quiet` finally means quiet.** The focus window used to promote a quiet
  topic to loud for 30 minutes after a prompt — exactly the window a long task
  runs in — so the default setting felt like it did nothing. `loud` pushes every
  reply and the finish, `quiet` pushes only the finish, `off` pushes nothing.
  Three settings, three behaviours.
- **Quiet now *holds*, not just mutes.** `disable_notification` removes the
  sound, not the line in the tray, so a narrating agent was still eight
  notifications for one task. Under anything but `loud`, `Outbox._sendable`
  drops a destination whose session is mid-turn, and the batch goes out when
  the turn ends — ahead of the finish line, which `max_pending_index` already
  sorts last. The live surface in that window is the card, which is an edit and
  never notifies. Two valves keep this from becoming a delivery gate and both
  fail open: `deliveries.held_destinations` ignores a session whose poller has
  stopped writing `updated_at`, and `MAX_HOLD_MS` releases a queue held half an
  hour whatever the state machine believes.
- **`BotActionSink._announce_finish` is the one buzz.** It fires on the
  machine's `Finalize`, keyed on the cursor the session finished at so a
  redeploy cannot announce one turn twice. It has to be a *message*: the status
  card already says `done`, but a card is an edit and Telegram never notifies
  for an edit. A card cannot ring a phone.

`Destination.silent` now defaults to `True`, matching `chats.notify`'s own
default — left at `False`, a session bound without a `chats` row pushed every
line, which is the loudest possible behaviour from the absence of a setting.

### …and one Stop, not two

The prompt receipt (`→ Login · queued`, shown when the 👀 reaction is refused)
carried its own Stop, on the theory that a refused reaction left the owner
without a control. It does not: the reaction has nothing to do with the pinned
card, which appears either way and already owns Stop. Every task showed two,
which is two answers to "is this still going?".

The card is the right owner and the bubble was the wrong one to keep, not merely
the spare. A bubble is a *static* message: its Stop stayed live on screen for
fifteen minutes after the turn ended, and it targets the **session**, so tapping
it then cancelled whatever was running by then. `card_buttons` strips Stop from a
terminal card the moment the turn is over, which is the property that makes a
control honest.

Two Stops came back anyway, on a road neither `_supersede` nor `_adopt_once`
could close: **the card was posted twice.** `_post` learns the message id only
when `sendMessage` *returns*, so for the length of that round trip the `_Card` is
dirty with `message_id is None` — and `StatusCards.tick()` is a separate task
reading the same object every second. It saw what the action batch saw and posted
the card again. The loser kept the text it was posted with (`⏳ waking ·
preparing`) while the winner ticked on to `⚙️ working 1m2s`, which is exactly the
two-Stop screen, from one topic's first card. `_lock_for` now gives each topic
one lock; `handle` holds it from the adoption read through the flush, and the
tick *skips* a topic another task is already sending for rather than queueing
behind it.

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
3. **Run `scripts/probe_topic_icons.py`.** Token only, no chat, read-only, five
   seconds. It prints the icon pack Telegram actually serves this bot and what
   each state resolves to, and exits non-zero if any state would fall through
   to "icon unchanged". The wanted emoji in `_TOPIC_ICONS` are educated
   guesses; this is the thing that turns them into facts.
4. Walk the default sign-up from a phone: `/start` → `/key` → `/new` → an
   answer arrives, all in one private chat.
5. Walk the optional group: `/team` → supergroup with Topics → `/setup <code>`
   → `/new` in General.
6. Two real teams at once, each with its own key, staying separate.
7. Redeploy mid-turn; the answer arrives exactly once.
8. Watch `/health` for real Conductor and Telegram 429s, and tune the two rate
   budgets — the current numbers are under the documented ceilings but have
   never met real traffic.
9. Voice: a team storing its own speech key, then 30 owner recordings
   before moving `voice_mode` from `prompts` to `commands`.
10. Re-probe a sleeping workspace (probe assumption 8 is still unmeasured).
11. Curate real tool-heavy and error transcripts; the non-trivial renderer
    fixtures are still labelled synthetic.

## Before opening registration to strangers

You will hold other people's Conductor API keys — each can read every
transcript in their organisation and spend their money.

- Confirm with Conductor that brokering third-party keys is within their terms.
- Publish a privacy policy and terms. `/privacy` states what is stored today.
- Write the breach runbook: rotate `CTB_MASTER_KEYS`, re-wrap, notify.
- Back the master keys up **separately** from the database.
- Consider `REGISTRATION_OPEN=false` for the first weeks.
