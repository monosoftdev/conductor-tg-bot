# Fault catalogue — one topic per session

Companion to [`TOPIC_PER_SESSION.md`](TOPIC_PER_SESSION.md). One entry per way
the change can go wrong, each with the test that pins it.

> **These are specifications, not passing tests.** Nothing in the plan is
> implemented, so a test file landed today would either fail the required `all
> gates` CI job or be skipped — and a skipped test has no teeth, which is the
> one thing CLAUDE.md says a test must have. Each entry below names its file so
> it can be written next to the code that makes it pass, in the same commit.
>
> Every entry states the **fault** (what a user sees when it is wrong), not just
> the assertion. Before any of these is called done, break the code it covers
> and watch it fail — an adversarial review of this repo once deleted ten
> `is_owner` gates and eight of them killed no test.

Conventions: `F-nn` is stable, referenceable in a commit message. `db` means the
test needs the `db` marker (real PostgreSQL; `CI=true` turns "no database" into
a failure, so these cannot silently skip).

---

## A · Schema, indexes, migration — `tests/test_repo.py`, `tests/test_isolation.py`

**F-01** `db` · *Two sessions claim one room and both keep delivering into it.*
The partial unique index rejects a second `is_bound` session with the same
`(tenant_id, chat_id, thread_id)` when `thread_id <> 0`. Insert one, assert the
second raises a unique violation.

**F-02** `db` · *The linear seat stops working because the index treated it as a
room.* Two bound sessions at `thread_id = 0` in one chat must **insert fine**.
This is the carve-out; without a test it will be "tidied up" by someone reading
the index and assuming symmetry.

**F-03** `db` · *One session shows up in two rooms and its transcript forks.*
`uq_chats_one_room_per_session` rejects a second `chats` row with the same
`session_id` at `thread_id <> 0`; allows it at `0`.

**F-04** `db` · *Migration 003 aborts on a live database.* Every `/fork` and
`/s` ever run left duplicate bound sessions on a seat, so creating the index
fails. Seed three bound sessions on one seat, run the migration, assert it
completes, assert exactly one is still `is_bound`, and assert it is the newest
by `created_at` — the same tiebreak `get_bound_for` applies today, so nothing
moves.

**F-05** `db` · *The cleanup unbinds the wrong one and prompts land on a dead
task.* With `created_at` tied, the surviving row must be deterministic (fall
back to `id`). Two rows with identical `created_at`, run twice, same survivor.

**F-06** `db` · *The cleanup unbinds sessions in another tenant.* Seed two
tenants with colliding seats, run the migration as the worker role, assert each
tenant keeps its own newest.

**F-07** `db` · *The backfill hands a workspace's room to the wrong session.*
Only the session whose `(chat_id, thread_id)` equals the workspace's
`(chat_id, topic_id)` inherits `topic_name`/`topic_marker`. Seed a second
session of the same workspace bound to thread 0; assert it inherits nothing.

**F-08** `db` · *The backfill runs twice and the second run breaks something.*
Run 003 twice, assert idempotent (`ADD COLUMN IF NOT EXISTS`, same row counts).

**F-09** `db` · *A tenant reads another tenant's room.* `sessions.topic_name`
and `topic_marker` are covered by the existing forced RLS policy — the same
assertion `test_isolation.py` already makes for every other session column,
extended to the new ones and to `archived_at`.

**F-10** `db` · *`archived_at` and `dead_at` get conflated and the "last
session" count is wrong.* A session that 404ed (`mark_dead`) and one the user
archived are both excluded from "live", but only the second sets `archived_at`.
Assert both flags independently.

**F-11** `db` · *Dropping the workspace topic columns in 004 breaks a reader
still in production.* Assert no `src/` reference to `workspaces.topic_id`
remains before 004 is allowed — a grep test, cheap, and the only thing that
makes a two-step column drop safe.

---

## B · Room lifecycle — `tests/test_bot_handlers.py`

**F-12** *A workspace is paid for and has nowhere to live.* `require_topic`
still runs before `POST /workspaces` and before `POST /sessions`. Make
`create_forum_topic` raise; assert neither create was called.

**F-13** *A failed create leaves an empty room behind, and a retry leaves
another.* Make the session create fail after the topic exists; assert
`discard_topic` was called with that exact `message_thread_id`.

**F-14** *`discard_topic` deletes a room it did not create.* It may only be
called with a thread id produced in the same call. Assert a claimed (pre-
existing) thread is never discarded on failure — closing somebody's own thread
because our create failed is unrecoverable.

**F-15** *`apply_marker` spends a Telegram call on every tick.* Same rendered
title → no `edit_forum_topic`. Assert call count 0.

**F-16** *A room born `⏳` never leaves it because the stored marker was never
recorded.* After create, `sessions.topic_marker == 'INITIALIZING'`; after the
first transition it changes exactly once.

**F-17** *A claimed thread that refused the rename records a marker it is not
wearing, and every later rename is skipped.* `Claim(True, False)` → marker
stays `NULL`. Assert the next transition **does** rename.

**F-18** *An emoji with a presentation selector misses the pack and the icon
never moves.* `icon_key` normalises `U+FE0F`/`U+FE0E`; `⚙` matches `⚙️`.

**F-19** *One missing icon costs the whole rename.* Empty icon pack → the rename
still goes out, `icon_custom_emoji_id` omitted.

**F-20** *Two rooms of one workspace look unrelated in the list.*
`topic_icon_color` hashes the **workspace** label, so siblings share a colour;
`topic_title` differs. Assert same colour, different name.

**F-21** *A room is titled with the internal `tg-<chat>-<nonce>` name.*
`human_name` filters it; assert the fallback label is used instead.

**F-22** *`room_gone` fires three times and posts three lines.* Idempotent:
second and third call are no-ops, one line total.

**F-23** *`room_gone` unbinds a session whose room is alive.* Only acts when the
session's `thread_id` matches the thread reported gone.

**F-24** *`retire_topic` cannot delete (no right in a group) and the room lies
about being finished forever.* Delete refused → rename to `🗄` **and** close, and
the binding is kept (a closed topic still exists). Assert `TopicRetirement.CLOSED`
and that `topic_id` is still set.

**F-25** *`retire_topic` deletes and the binding survives, so the next state
change addresses a dead thread.* Delete succeeded → both `sessions.unbind_topic`
and `chats_repo.unbind` ran.

---

## C · `/new` — `tests/test_bot_handlers.py`

**F-26** *`/new` in a threaded DM opens a second room beside the one Telegram
just created.* The *New Chat* composer thread is claimed, not duplicated.
`route.claimable_thread` non-zero → `claim_topic`, no `create_forum_topic`.

**F-27** *A replayed `/new` update creates a second workspace.* Same
`(chat_id, tg_message_id)` → same nonce → the existing room is reused and no
second `POST /workspaces` is issued.

**F-28** *A DM without threaded mode loses `/new` entirely.* `has_topics_enabled:
False` → linear seat, workspace still created, prompt still queued, one notice
sent once per chat.

**F-29** *The notice is sent on every create and becomes noise.* Second `/new`
in the same chat sends no second notice.

**F-30** *A group without Manage Topics fails silently.* `/new` raises with
Telegram's own words, not the hardcoded "run /setup".

---

## D · `/fork` — `tests/test_bot_handlers.py`

**F-31** *`/fork` creates a second workspace.* Assert `POST /workspaces` is never
called and the new session's `workspace_id` equals the parent's.

**F-32** *`/fork` steals the parent's room.* After the fork: the parent `chats`
row still points at the parent session; the parent session's `thread_id` is
unchanged; the new session has a different, non-zero `thread_id`.

**F-33** *The fork's room is opened but never linked, so prompts in it go
nowhere.* A `chats` row exists for the new thread with the new
`session_id` **and** `workspace_id`, `kind='topic'`.

**F-34** *The parent's marker is reset and its finished turn stops reading ✅.*
Assert the parent's `topic_marker` is untouched by `/fork`.

**F-35** *A failed `POST /sessions` leaves an empty room.* Session create raises
→ `discard_topic` called, no `sessions` row bound, no `chats` row written.

**F-36** *An ambiguous `POST /sessions` creates two chats in Conductor.* The
caller-supplied `sessionId` is reused on retry; assert the same id on both
attempts.

**F-37** *The server ignores our session id and the bot polls a session that
does not exist.* `cursor.create_session` already handles this
(`cursor.py:1536-1547`); assert the local row is moved to the returned id and
that the *room* follows it.

**F-38** *`/fork` in a linear DM opens nothing and answers nothing.* Topics
unavailable → the one seat is rebound, and — the new part — the previously bound
session is **unbound**, so F-01's index would not have been violated had it
applied.

**F-39** *A bare `/fork` produces a room called `Telegram fork · api/main`.*
Bare form gets the ordinal label; assert `fork 2 · api/main` for the second
session of the workspace.

**F-40** *The provisional label is upgraded on every prompt, renaming the room
forever.* Upgrade happens once, on the first prompt only
(`last_prompt_at IS NULL` gate); a second prompt issues no rename.

**F-41** *`/fork` from the root (no workspace) is accepted and creates
something.* Assert the existing "No workspace here." refusal survives.

**F-42** *Two `/fork`s in flight race and both claim the same thread.* Two
concurrent forks in one room → two distinct threads, two distinct sessions.

---

## E · `/board` — `tests/test_bot_handlers.py`, `tests/test_bot_adopt.py`

**F-43** *Stage 1 and stage 2 read alike and the user cannot tell what they are
picking.* Assert stage 1 contains "Workspaces" and "sessions" only as the
*destination* noun, never the verb "open"; assert stage 2 contains the workspace
name and "open it in this chat". Assert the two headers are not equal.

**F-44** *Tapping a workspace posts a second card and the chat fills with
boards.* Same `message_id` edited; `send_message` not called.

**F-45** *Back posts a third card.* Same, in reverse.

**F-46** *Back is unreachable because its ticket was minted at stage 1 and
already spent.* Every render mints fresh tickets; assert Back works after
two round trips.

**F-47** *One tap mints forty tickets.* Stage 1 with 10 workspaces × 4 sessions
mints 10 tickets, not 40.

**F-48** *A stale stage-2 ticket rebinds a session that has since got its own
room.* The callback re-checks and refuses with "That task has its own topic."

**F-49** *`/board` shows a jump button to a room that was deleted.* A session
whose room is gone renders as *Open here*, not as a `url_button`.

**F-50** *A DM topic renders a dead `url_button`.* `jump_url` returns `None` for
a private chat; assert the fallback callback button, not a broken link.

**F-51** *Stage 2 misses a session created on the laptop.* The list is the union
of `list_workspace_sessions` and `list_for_workspace`; seed a remote-only
session and assert it appears.

**F-52** *`POST /v0/sql` is down and `/board` fails.* Stage 1 falls back to the
local cache (existing behaviour at `core.py:324`), and says so.

**F-53** *Stage 2's API call fails and the card is destroyed.* The card keeps
its text, appends one error line, keeps Back.

**F-54** *A workspace with zero sessions dead-ends.* Stage 2 says so and offers
Back; no empty keyboard.

**F-55** *Stage 1 silently truncates.* More than `BOARD_VISIBLE` → the `+N more`
line is present and the number is right.

**F-56** *The session count on a stage-1 row is the number of view rows, not
sessions.* Seed a workspace with three sessions of which one has never spoken;
assert the count is 3.

**F-57** *Connecting from a chat with no topics does nothing.* In a linear seat,
tapping a session **rebinds that seat**, unbinds the previous session, and does
not attempt `createForumTopic`.

**F-58** *Connecting twice opens two rooms for one session.* Second connect
jumps; assert exactly one `create_forum_topic` across both.

**F-59** *Two people tap the same session at once and get two rooms.* The
per-workspace lock (`adopt.py:122`) serialises; assert one room.

**F-60** *Connecting replays the whole history into the room.* `seek_to_end` is
called and no `deliveries` rows are written for past messages.

---

## F · `/done` and the archive cascade — `tests/test_bot_handlers.py`

**F-61** *`/done` on one task archives the whole workspace and kills its
siblings.* With 2 live sessions: `archive_session` called once,
`archive_workspace` **not** called, only this room retired.

**F-62** *The last task is archived and the workspace keeps burning.* With 1 live
session: both calls made, in that order.

**F-63** *A laptop session is invisible to the count and gets archived from a
phone.* Local cache says 0 remaining, the API says 1 → the workspace is **not**
archived. This is the expensive one.

**F-64** *The count lookup fails and the bot guesses.* API raises → session
archived, workspace untouched, one line saying the workspace was left alone.

**F-65** *The two confirm cards read alike.* Assert the "last task" card names
the workspace and the word "whole workspace"; assert the ordinary card names the
sibling count; assert they are different strings.

**F-66** *The count is taken after the card is drawn and the card lies.* Assert
the API call happens before the confirm text is rendered.

**F-67** *Double-tap archives twice.* The confirm ticket is single-use; second
tap answers "Already done".

**F-68** *`archive_session` succeeds, the room delete fails, and the state is
half-applied.* Room refused → session still archived and unbound, room renamed
`🗄` and closed, receipt says so.

**F-69** *The receipt is posted into a room that no longer exists.* Room deleted
→ no edit attempted (the existing early return), and — when the workspace was
archived too — one line in the chat root instead.

**F-70** *An archived session keeps polling.* `is_bound = false` and
`list_bound` no longer returns it.

**F-71** *An archived session's queued deliveries are lost or sent into a deleted
room.* Decide and test: pending rows for that session are dropped, not
rerouted — the room is gone on purpose.

**F-72** *A workspace archived on the laptop leaves three rooms live.* Each
session's own poller reaches `_die` → `SetTopicMarker(ARCHIVED)` + `UnbindTopic`
→ its own room retires. Assert three rooms retired from three pollers, not one
fan-out.

**F-73** *`/done` in the root archives whatever was most recent.* No session in
scope → refusal naming `/board`.

**F-74** *`/mode`'s Archive button and `Action.ARCHIVE_REQUEST` still target the
workspace.* Both go through the session path; assert the same two-card flow.

---

## G · A deleted or closed topic — `tests/test_bot_handlers.py`, `tests/test_outbox.py`

**F-75** *The bot waits for a "topic deleted" update that never comes.* There is
no such service message — assert `_SERVICE_CONTENT` has no deleted member and
that discovery is exercised only through the three failure paths.

**F-76** *A deleted room silently swallows a reply.* `send_html` resends to the
root when the thread is gone, and returns a `Message`, not `None`.

**F-77** *The reroute is paid per row forever.* First thread-gone reroute calls
`room_gone`; the next turn's deliveries are enqueued at `thread_id = 0`, so the
second turn triggers zero reroutes.

**F-78** *Two sessions rerouted to the root silently swallow each other's
identical replies.* The dedup guard matches on `(chat_id, thread_id,
content_hash)` across sessions (`deliveries.py:651-657`, `:741-745`) — two
rooms deleted, both sessions answer "Done.", assert **both** land. This one is
newly reachable because per-session rooms make the root a shared destination it
never was.

**F-79** *The same message in two live rooms is deduped and one is dropped.* The
inverse: identical `content_hash` in different threads, assert both send.

**F-80** *A closed (not deleted) topic is treated as gone and the session is
detached.* A closed topic still accepts nothing but still exists; assert
`room_gone` is not called for a "topic is closed" error.

**F-81** *Deleting a topic archives the workspace.* It must not: assert
`archive_session`/`archive_workspace` are never called from `room_gone`.

**F-82** *The `room_gone` line is posted into the deleted room.* It goes to
`thread_id = 0`.

**F-83** *A user's own hand-rename is deleted as if it were ours.*
`tidy_rename_notice` compares against `sessions.topic_name` + marker; a
different title keeps its service message.

---

## H · Delivery, ordering, pacing — `tests/test_outbox.py`, `tests/test_pacing.py`

**F-84** *A working session holds a sibling's queue.* `held_destinations` groups
by `(chat_id, thread_id)` (`deliveries.py:346-350`); with one room each, a
working session in room A must not hold room B's pending rows.

**F-85** *One workspace's rooms starve every other tenant.* The round-robin
rotor is per destination; assert N rooms of one workspace do not push another
tenant's single room out of a pass.

**F-86** *Ordering within one room breaks.* Unchanged behaviour, re-asserted
with two sessions in two rooms interleaving.

**F-87** *The focus window follows the workspace instead of the room.*
`focus_tracker` is keyed `(chat_id, thread_id)`; typing in room A must not make
room B loud.

**F-88** *`/notify` set on the parent room applies to the fork's room.* `chats`
rows are per thread; assert the fork's room starts at the default, not the
parent's.

---

## I · Turn machine and markers — `tests/test_machine.py`, `tests/test_bot_actions.py`

**F-89** *Two sessions of one workspace fight over one marker and a room reads
`⚙️` for work you are not looking at.* The regression this whole change buys:
session A `WORKING`, session B `IDLE`, assert two different prefixes at the same
time, on two rooms.

**F-90** *A sleeping workspace's room reads "working".* `marker_for` still lets
workspace lifecycle win, resolved per session.

**F-91** *`SetTopicMarker` renames the workspace's first room instead of the
session's.* Assert the edited `message_thread_id` is this session's.

**F-92** *A renderer or Telegram failure in a marker update stalls delivery.*
`_set_topic_marker` swallows and logs; assert the delivery after it still goes.

**F-93** *`UnbindTopic` leaves the `chats` row routing to a dead session.*
Assert both sides cleared.

---

## J · Routing and seats — `tests/test_middleware.py`

**F-94** *A prompt typed in a DM topic is addressed to the DM root.*
`_thread_id`'s private-chat fallback; re-assert with several rooms in one DM.

**F-95** *A forum's General is seen as thread 1 and grows a second identity.*
Normalised to 0; re-assert.

**F-96** *An unfinished `/new` wizard in one room swallows a line typed in
another.* `wizard_state` PK is `(chat_id, thread_id, user_id)` and
`_publish_seat` feeds it; assert two rooms hold two independent wizards.

**F-97** *`claimable_thread` treats an occupied room as scratch space.* A thread
with a session or workspace bound is never claimable.

**F-98** *The reply-to override moves a binding.* It must only address one
prompt; assert `chats.session_id` and `sessions.thread_id` are unchanged after a
`via_reply` prompt.

**F-99** *A routing DB error swallows the update.* Existing fail-open behaviour,
re-asserted.

---

## K · Voice — `tests/test_voice_pipeline.py`, `tests/test_voice_commands.py`

**F-100** *A dictated prompt lands in the wrong room after a fork.* `voice_inputs`
stores `(chat_id, thread_id)` at enqueue; assert a room opened between enqueue
and transcription does not move it.

**F-101** *Voice gains a switch verb by accident.* Assert `VoiceCommand` still
has exactly `new · board · stop · find · mode · done`.

**F-102** *Voice `board` tries to render a drill-down nobody can tap.*
`board_lines` stays text-only and says how to open one by name.

**F-103** *Key-terms leak another session's title into this one's
transcription.* Terms come from this session and its workspace only.

---

## L · Concurrency, restart, idempotency — `tests/test_claims.py`, `tests/test_supervisor.py`

**F-104** *A redeploy mid-`/fork` leaves a room with no session.* Restart between
topic create and session create → the orphan room is discarded or adopted on the
next `/fork`, never left as a silent empty row.

**F-105** *Two outbox workers claim one room's rows.* `FOR UPDATE SKIP LOCKED`,
re-asserted with many rooms.

**F-106** *`recover_orphaned` reclaims a row still in flight on the other
deployment.* `ORPHAN_AFTER_MS` respected.

**F-107** *The supervisor starts a poller for an archived session.* `list_bound`
excludes `archived_at IS NOT NULL`.

**F-108** *A poller keeps running after its room is deleted.* It keeps running on
purpose — the transcript is still the truth — but delivers to the root. Assert
that explicitly so nobody "fixes" it into a stop.

---

## M · Tenancy — `tests/test_two_tenants.py`, `tests/test_isolation.py`

**F-109** *`/board` stage 2 lists another tenant's sessions.* RLS covers it, but
the union with the API result does not — assert the API client used is the
tenant's, and that a session id from another tenant is refused at connect.

**F-110** *A stage-2 ticket minted in tenant A is spent in tenant B.* The ticket
carries `chat_id`/`user_id`; assert the mismatch is refused.

**F-111** *A cross-tenant worker writes a room.* Room writes happen only on the
tenant pool; assert `ctb_worker` is not used for them.

---

## N · Degradation — `tests/test_bot_handlers.py`

**F-112** *Threaded mode is turned off mid-life and every command dies.* An
existing room keeps working until Telegram refuses it, then `room_gone`; new
sessions degrade to the seat.

**F-113** *`has_topics_enabled` absent is treated as "off".* Absent means try.

**F-114** *A group loses Manage Topics and `/fork` takes the bot down.* One clear
refusal, no half-written state.

**F-115** *Telegram 429s during a rename storm and a room is left mis-titled.*
The rename is retried on the next transition; assert the marker is not recorded
when the rename failed.

---

## O · Copy and command surface — `tests/test_help_card.py`, `tests/test_main.py`

**F-116** *`/s` is gone from the menu but still in `/help`, or vice versa.*
Assert `s` is absent from `BOT_COMMANDS` **and** from `_HELP`.

**F-117** *A string still tells someone to run `/s`.* Grep test over `src/` for
`/s` as a command token — the nine call sites listed in the plan.

**F-118** *The docs and the onboarding message drift.* `docs/GETTING_STARTED.md`
is the walkthrough; assert the `/help` card and it agree on what `/board` and
`/fork` do (the repo already treats this as a rule).

**F-119** *Button labels overflow a narrow screen.* `truncate_label` applied to
every new button; assert ≤ `MAX_BUTTON_TEXT`.

**F-120** *A topic title exceeds Telegram's 128.* `topic_title` clips prefix +
label; assert with a 200-character task hint and a long project name.

---

## Coverage map

| Area | Entries | Files |
|---|---|---|
| Schema & migration | F-01…F-11 | `test_repo.py`, `test_isolation.py` |
| Room lifecycle | F-12…F-25 | `test_bot_handlers.py` |
| `/new` | F-26…F-30 | `test_bot_handlers.py` |
| `/fork` | F-31…F-42 | `test_bot_handlers.py` |
| `/board` | F-43…F-60 | `test_bot_handlers.py`, `test_bot_adopt.py` |
| `/done` | F-61…F-74 | `test_bot_handlers.py` |
| Deleted topic | F-75…F-83 | `test_bot_handlers.py`, `test_outbox.py` |
| Delivery | F-84…F-88 | `test_outbox.py`, `test_pacing.py` |
| Machine | F-89…F-93 | `test_machine.py`, `test_bot_actions.py` |
| Routing | F-94…F-99 | `test_middleware.py` |
| Voice | F-100…F-103 | `test_voice_pipeline.py` |
| Concurrency | F-104…F-108 | `test_claims.py`, `test_supervisor.py` |
| Tenancy | F-109…F-111 | `test_two_tenants.py` |
| Degradation | F-112…F-115 | `test_bot_handlers.py` |
| Copy | F-116…F-120 | `test_help_card.py`, `test_main.py` |

The three that would hurt most in production, and should be written first:
**F-63** (a phone archives a laptop colleague's live workspace), **F-78** (two
rooms deleted, one reply silently swallowed by the dedup guard) and **F-04** (a
migration that cannot run on real data).
