# One topic per session

> **Status: proposal.** Nothing below is implemented. It supersedes PLAN §Chat
> model ("one topic per workspace") if adopted, and `docs/PLAN.md`,
> `docs/HANDOFF.md`, `CLAUDE.md`, `README.md`, `docs/GETTING_STARTED.md` and
> `docs/SETUP.md` all say "one topic per workspace" today and would need the
> same edit.

## The problem, stated in the current code

`workspaces` owns the room: `chat_id`, `topic_id`, `topic_name`, `topic_marker`
(`src/ctb/db/migrations/001_init.sql:206-210`). `sessions` owns an *address*:
`chat_id`, `thread_id` (`001_init.sql:253`). Today those two agree, because
every session of a workspace is bound to that workspace's one topic.

So `/fork` (`src/ctb/bot/handlers/power.py:402`) creates a second session and
binds it to `route.thread_id` — the same room — and `chats(chat_id, thread_id)`
can only point at one `session_id` at a time. The second session takes the room
over; the first goes quiet without saying so. `/s`
(`power.py:190`) exists to undo that by hand, and `switchable_sessions`
(`power.py:121`) is the rule that a topic may switch sessions but never
workspaces.

That is the inconvenience: **inside a workspace, the room is a mutable pointer,
and the only way to see the other conversation is to move the pointer.**

## The target model

One topic per **session**. A workspace becomes a *group of rooms* rather than a
room:

- the room's name is `<marker> <session task> · <project>/<branch>` — the label
  builder (`topics.topic_label`) already takes `task=`, it is just fed the
  session's opening prompt instead of the workspace's;
- `icon_color` stays a hash of the **workspace** label, not the session label,
  so all rooms of one workspace share a colour and read as a family in the list
  (`topics.topic_icon_color`, which is already documented as identity-not-state
  and is fixed at create time);
- `icon_custom_emoji_id` and the name prefix stay per-session state, exactly as
  they are now.

### The rule that makes migration and `/attach` cheap

> **A session gets a room the first time it is opened, not when it is created.**

`/new` opens one immediately (it already does). `/fork` opens one immediately
(it is an explicit act). Everything else — the other sessions of an adopted
workspace, every session that exists today under the old model — materialises
its room lazily, on the first `/s`-style *Open*. Without this rule, `/attach` on
a laptop workspace with nine sessions opens nine rooms nobody asked for, and the
backfill has to create a room for every historical session in one migration.

## Audit: every way a room's session can change today

The point of this change is that **a topic is one session, for its whole life**.
That is only true if every path that writes `chats.session_id` or
`sessions.chat_id`/`thread_id` is accounted for. Sixteen were found; four are
real switch vectors, and two of them are worse than they look.

Grep basis: `chats_repo.bind|update|unbind`, `sessions_repo.bind|upsert|unbind`
across `src/`, plus every reader of `Route.session_id`.

### Real switch vectors — must go

**1. `/s` and its callback** (`power.py:190`, `power.py:339`). The explicit
switch. `switch_callback` writes `sessions_repo.bind` + `chats_repo.bind` on the
seat the button was minted in. Deleted (see §`/s` is deprecated).

**2. `/fork`** (`power.py:402`). Rebinds the current seat to the new session
(`power.py:446-465`). Fixed by giving the fork its own room.

**3. Nothing ever unbinds the session that was there.** `grep -rn "unbind"
src/ctb/bot/` returns three hits and none of them is in `power.py` — so both
`/s` and `/fork` leave the **previous** session with `is_bound = true` and the
same `(chat_id, thread_id)`. Two bound sessions then share one seat, and:

- `sessions.list_bound` (`repo/sessions.py:235`) returns both, so the supervisor
  polls both;
- both enqueue deliveries addressed to that same thread
  (`session_poller.py:574-586`), so the old session's output keeps landing in
  the room — interleaved with the new session's, with nothing saying which is
  which. The old session does not go quiet; it goes *anonymous*;
- `sessions.get_bound_for` (`repo/sessions.py:219`) resolves the seat with
  `ORDER BY created_at DESC LIMIT 1`, so which session a *prompt* reaches is
  decided by a tiebreak, not by anything the user did.

This is the bug that makes the whole feature worth doing, and it is why the fix
cannot be "stop calling switch" — it has to be a constraint (below).

**4. `/attach` of a workspace whose remembered topic lost its `chats` pointer**
(`adopt.py:388-395`). When `_remembered_topic` finds the topic but no session
id, the flow falls through to `_pick_session(remote_sessions, session_hint)` and
`_bind`s *that* session into the remembered room — a different session in the
same room, by design, as a repair. Under the new model `_remembered_topic` is
keyed on the **session**, so the repair can only ever restore the room's own
session.

### Legitimate, and staying

**5. The linear seat** (`thread_id = 0`: a DM without threaded mode, a group's
General). It genuinely holds one mutable binding and always will — it is not a
room, it is a seat. This is the *only* place switching survives, and after `/s`
goes the switcher is `/board` stage 2. It must be carved out of the constraint
below explicitly, not by accident.

**6. Reply-to override** (`routing.py:322`, `Route.via_reply`). Routes one
prompt to the session that produced the message being replied to, without
touching any binding. Keep — it is a PLAN safety rail — but state the new rule:
the prompt's echo and receipt stay in the room it was typed in while the reply
lands in the target session's room. In practice Telegram only offers Reply
inside the current thread, so after this change the override is reachable almost
only from the root, which is exactly where it is useful.

**7. Cockpit "Send to «task»"** (`core.cockpit_markup`, `Action.SEND` at
`prompts.py:436`). Sends a line typed in the root to the most recently prompted
session. Nothing is rebound; the reply lands in that session's room. Keep.

**8. `/new`, direct and wizard** (`common.py:718`, `wizards/new_workspace.py:910,
1033`) — always a brand-new room for a brand-new session. Safe, and the model
`/fork` copies.

**9. `cursor.create_session` / `create_workspace`** (`cursor.py:1389`, `:1519`)
write `is_bound=False` and no `chat_id`/`thread_id`. Safe by construction.

**10. `/setup`, `tenancy.bind_chat`/`rebind_chat`** (`registration.py:525,636,
1158`) bind a *chat to a tenant*, never a session to a seat. Not a vector.

**11. Voice.** `VoiceCommand` (`voice/intent.py:32`) has no switch verb —
`new · board · stop · find · mode · done`. The voice path reaches sessions only
through the route and the cockpit. Safe; `board` stays text-only because voice
cannot tap a drill-down.

**12. `/mode`** (`core.py:682`) offers Stop, Transcript, Open, Archive. No
session picker. Safe.

### Loose ends this change should close

**13. `chats_repo.bind` is unguarded** (`repo/chats.py:210`). It will happily
repoint any seat at any session. After this change it should refuse to overwrite
a *different* non-null `session_id` when `thread_id <> 0`, and say so — a room
is not a pointer. Cheap, and it turns every future regression into an exception
instead of a silently re-addressed transcript.

**14. `UnbindTopic` leaves a zombie room** (`session_poller.py:530-540`). It
unbinds the session and clears the workspace topic, but never clears
`chats.session_id`, so the room still routes prompts to a session that is dead.
Clear the `chats` row in the same block.

**15. Outbox reroute does not free the room** (`outbox.py:1047`). A deleted
topic moves the *delivery row* to the root; the session keeps pointing at the
dead thread, so the next turn queues there and pays the reroute again, row by
row. Recommended: on reroute, also clear that session's room
(`sessions.unbind_topic`, `thread_id = 0`) so `/board` offers to open a new one.
Flagged rather than assumed — it changes routing, so it wants its own test.

**16. `retire_topic`'s delete path** (`topics.py:1044-1045`) already unbinds
both sides. It just becomes per-session.

### The database guarantee

Per CLAUDE.md's second rule — isolation and invariants are database facts, not
code-review facts — the model is enforced by an index, not by discipline:

```sql
-- One bound session per room. Thread 0 is excluded: the linear seat and a
-- group's General are seats, not rooms, and legitimately switch.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_one_per_room
    ON sessions (tenant_id, chat_id, thread_id)
 WHERE is_bound AND chat_id IS NOT NULL AND thread_id <> 0;

-- And the inverse: one session is never in two rooms.
CREATE UNIQUE INDEX IF NOT EXISTS uq_chats_one_room_per_session
    ON chats (tenant_id, session_id)
 WHERE session_id IS NOT NULL AND thread_id <> 0;
```

**Migration 003 will fail on live data unless vector 3 is cleaned up first**,
because every `/fork` and every `/s` ever run has left duplicate bound sessions
on a seat. The migration must, before creating the index, keep the newest bound
session per `(tenant_id, chat_id, thread_id)` and set `is_bound = false` on the
rest — the same tiebreak `get_bound_for` already applies, so nothing changes
where prompts go; it only stops the losers from polling and delivering. Log the
count.

### Tests for the audit

- `tests/test_repo.py` — the two indexes reject a second bound session in a room
  and a session in a second room; both **allow** it at `thread_id = 0`; the
  pre-index cleanup keeps exactly the newest and unbinds the rest.
- `tests/test_bot_handlers.py` — `chats_repo.bind` raises when repointing a room
  at a different session, and does not raise for `thread_id = 0`.
- `tests/test_session_poller.py` — `UnbindTopic` clears the `chats` row.
- `tests/test_outbox.py` — a thread-gone reroute leaves the session roomless
  (if 15 is taken).

## Schema

New migration `003_topic_per_session.sql`. `bootstrap`/`migrate` apply it; the
application never applies DDL (CLAUDE.md).

```sql
ALTER TABLE sessions ADD COLUMN topic_name   text;
ALTER TABLE sessions ADD COLUMN topic_marker text;
-- sessions.chat_id / thread_id already exist and already carry the address.

-- Backfill: the workspace's room becomes the room of the session already
-- bound to it. Only that one — the others stay roomless and materialise lazily.
UPDATE sessions s
   SET topic_name   = w.topic_name,
       topic_marker = w.topic_marker
  FROM workspaces w
 WHERE s.workspace_id = w.id
   AND s.chat_id = w.chat_id
   AND s.thread_id = w.topic_id
   AND w.topic_id IS NOT NULL;
```

`workspaces.topic_id` / `topic_name` / `topic_marker` are **kept and stop being
written** in this change. They are the only evidence a rollback would have, and
`idx_workspaces_topic` is still what `/board`'s "does this workspace have a room
here?" reads until the last caller moves. Drop them in a follow-up migration,
not this one.

`workspaces.chat_id` stays and keeps its current meaning — which Telegram chat
this workspace lives in — because `adopt` uses it to refuse "already connected
in another Telegram chat" (`handlers/adopt.py:309`).

## Code, by seam

### `db/repo/sessions.py`

Add `bind_topic`, `unbind_topic`, `set_topic_marker`, `get_by_topic(chat_id,
thread_id)`, and `topic_name`/`topic_marker` on `SessionRow`. These are
line-for-line the workspace versions (`repo/workspaces.py:266-295`).

### `bot/handlers/topics.py` — the one real refactor

`apply_marker`, `retire_topic`, `ensure_topic`, `attach_topic` are all keyed on
`workspace_id` today and read `workspaces_repo.get`. Re-key them on
`session_id`, reading the session row for `chat_id`/`topic_id`/`topic_name`/
`topic_marker` and the workspace row only for `branch`/`project` in the label
fallback and for `marker_for(workspace_status=…)`.

`marker_for` itself is unchanged: workspace lifecycle still wins over session
state (a sleeping workspace cannot be working), it is just resolved per session.

`_newest_session` (`topics.py:1056`) becomes dead and goes.

### `bot/handlers/common.py` — `/new`

`_seat_for` (`common.py:442`) and `create_and_bind_input` (`:569`) keep their
create-topic-before-`POST /workspaces` ordering exactly — that rule is about a
paid container, not about who owns the room. The only change is where the
result is written: `sessions_repo.bind_topic(...)` + `set_topic_marker(...)`
instead of `workspaces_repo.upsert(chat_id=…, topic_id=…, topic_name=…)`
(`:637-645`, `:665-693`). The nonce-replay guard at `:486` reads
`workspaces.get_by_nonce` for a *chat/topic* pair; it moves to reading the
nonce's workspace and then that workspace's newest session's topic.

### `bot/handlers/power.py` — `/fork`, `/name`, `/tidy` (and `/s` is retired)

#### `/fork`, exactly

**No new workspace.** `/fork` reuses `route.workspace_id` — the guard at
`power.py:412` already refuses a seat that has none — and adds a session *to
that existing workspace*, in a room of its own. So one workspace ends up with
several Conductor chats, each with its own Telegram topic, all sharing one
container, one branch and one checkout.

Order, and the order matters:

1. `label = topic_label(project, branch, task=<fork title>)` — the same builder
   `/new` uses, fed the same `project`/`branch` the parent room carries, so the
   new row joins the family in the list (`topic_icon_color` hashes the
   **workspace** label, so the colour matches too).
2. Open the room: `require_topic` in a group, `claim_topic` if the `/fork` was
   itself typed into an empty *New Chat* thread, `dm_topic_support` →
   `require_topic` → degrade-to-linear in a DM. Factor the DM-vs-group half of
   `_seat_for` (`common.py:442`) out so `/fork` and `/new` cannot drift.
   **Before** the session exists, exactly as `/new` does it.
3. `POST /v0/sessions` with a caller-supplied `sessionId`
   (`turn_cursor.create_session`) — idempotent, so an ambiguous failure is
   retried with the same id rather than leaving an orphan chat.
4. `sessions_repo.upsert(chat_id, thread_id=<new topic>, is_bound=True)` +
   `bind_topic` + `set_topic_marker(INITIALIZING)`, then
   `sessions_repo.seek_to_end(message_id=None, session_index=-1)` — a session
   we just created is empty, and saying so beats letting the supervisor guess.
5. `chats_repo.bind(chat_id, <new thread>, workspace_id, session_id,
   kind="topic")` — this is the "immediately linked" step: from here the router
   resolves that thread to that session and a prompt typed in it goes to the new
   chat, nowhere else.
6. On any failure after step 2, `discard_topic` the room just opened, so a retry
   finds no empty siblings.

The parent room is **not touched**: its `chats` row still points at the parent
session, its marker still describes the parent's own turn. The current "reset
the marker to IDLE because the room now points at a session that never ran"
(`power.py:472-475`) is deleted — it only existed because the fork stole the
room.

In a **linear DM** (topics refused) there is no second room to open, so `/fork`
keeps today's behaviour: the one seat is rebound to the new session, and the
one-line notice already says the chat holds one thing at a time.

Naming a bare `/fork`: today it titles the session `"Telegram fork"`
(`power.py:433`), which would become a topic reading `Telegram fork ·
acme-api/main` — useless in a list. `/fork <title>` is the good path and should
be what `/help` shows. For the bare form the default here is to label the room
`fork 2 · acme-api/main` (ordinal from `list_for_workspace`) and mark the label
*provisional* while `sessions.last_prompt_at IS NULL`; the first prompt sent
into the room upgrades it once through `apply_marker(label=…)` and never again.
That keeps "the opening prompt names the room" true without making `/fork` a
two-step command.

#### `/s` is deprecated

Every job `/s` had now belongs to something else:

- *switch session inside a workspace* — gone as a concept. Each session has its
  own room; you tap the room.
- *see the workspace's other sessions* — `/board` stage 2.
- *reach a workspace from General or the DM root* — `/board` stage 1 → 2.
- *move the binding in a linear DM* — `/board` stage 2, which in a seat with no
  topics binds that seat instead of opening a room. **This is the one piece of
  `/s` that must be reimplemented before `/s` is removed, not after**: the
  degraded DM has exactly one mutable binding and no other way to move it.

So:

- delete `BotCommand(command="s", …)` from `BOT_COMMANDS` (`app.py:111`);
- drop the `/s` line from `_HELP` (`power.py:85`); `/board` gains "workspaces,
  then their sessions";
- keep `switch_session` registered for one release as a **silent alias** that
  renders `/board` — a command people have in muscle memory should land
  somewhere useful, not on "Unknown command · use /help" from the catch-all at
  `prompts.py`. Remove the handler, `switchable_sessions`, `homed_elsewhere`,
  `switch_callback`, `Action`'s `switch` and `GENERAL_VISIBLE` in the
  follow-up that drops the workspace topic columns.
- fix every string that tells someone to run it: `common.py:108`
  (`LINEAR_DM_NOTICE`), `common.py:826`, `prompts.py:184`, `voice/service.py:578`
  ("No session here. Use /new or /s"), `core.py:283`, `core.py:526`,
  `power.py:280`, plus `README.md:76,99`, `GETTING_STARTED.md:116,132,152`,
  `SYSTEM_OVERVIEW.md:41,58,60,61,69-72,205`, `RELIABILITY_AUDIT.md:89` and the
  module docstrings at `routing.py:13-16` and `adopt.py:543`.
- `tests/test_bot_adopt.py:893-1014` and `tests/test_bot_handlers.py:650,1793`
  test `/s` directly; they become `/board` stage-1/stage-2 tests, keeping the
  same assertions about capping, filtering and never crossing a workspace.

`/name` (`power.py:481`): `/name text` renames the session **and** its room
(that is now the same object), `/name -w text` renames the workspace and must
re-label *every* room of that workspace, because `project/branch` is in all of
them. Loop over `sessions_repo.list_for_workspace`.

`/tidy` (`power.py:807`) iterates workspaces; it becomes an iteration over
sessions with a room, keeping the same 7-day staleness test on the session's
`updated_at`.

### `bot/handlers/core.py` — `/board` becomes a two-stage picker

See [§`/board`, stage by stage](#board-stage-by-stage) below; it is the largest
single piece of UI work in this change.

`/done` archives a workspace, so `confirm_archive` (`core.py:807`) must retire
**every** room of that workspace, not one: loop `retire_topic` over
`sessions_repo.list_for_workspace`. `TopicRetirement` becomes a per-room result
and the card reports the aggregate ("3 rooms closed", "1 left open"). The
"the card lived in the topic, so it went with it" early return (`:855`) has to
check whether the *card's own* room was among the deleted ones.

`adoptable` / `board_lines` / `nothing_to_attach` (`:348`, `:501`) count
"workspaces that have a room here" from `workspaces.topic_id`; they move to
"has any session with a room".

### `bot/handlers/adopt.py` — `/attach`

`_adopt_workspace` (`adopt.py:298`) opens one room for the *chosen* session and
leaves the other remote sessions as unbound rows, exactly as it does now
(`_bind` at `:566` already upserts them unbound). `_remembered_topic`
(`adopt.py:553`) reads the chosen session's room instead of the workspace's;
`_topic_exists` is unchanged. The lazy rule then lets `/s` open the others.

### `bot/handlers/prompts.py` — the rename-notice sweeper

`tidy_rename_notice` (`prompts.py:479`) resolves `workspaces_repo.get_by_topic`
→ `sessions_repo.get_by_topic`. Same comparison, same best-effort delete.

### `turn/` and `bot/actions.py`

`SetTopicMarker` / `UnbindTopic` are already emitted *per session* by the
machine (`turn/machine.py:415` etc.). The sink `_set_topic_marker`
(`actions.py:475`) currently resolves `session → workspace_id → apply_marker`;
it drops the middle step. `session_poller.py:530-540`'s `UnbindTopic` case calls
`workspaces.unbind_topic` → `sessions.unbind_topic`.

**This is the change that actually pays off.** Right now two sessions in one
workspace fight over one `topic_marker`: whichever ticks last wins, so a room
can read `⚙️ working` for a session you are not looking at. Per-session markers
make the prefix true by construction.

### Delivery — nothing to do

`deliveries` are addressed from the **session row's** `chat_id`/`thread_id`
(`session_poller.py:574-586`), enqueued per session, ordered per
`(chat_id, thread_id)` group (`delivery/outbox.py:24`, `deliveries.py:281-299`),
and rerouted to the root on `THREAD_GONE_MARKERS`. Per-session rooms are what
this path already assumed. The status card, typing indicator, pacing rotor and
focus tracker are all keyed the same way and need no change.

That is the strongest argument that this refactor is *small*: rule 1 of
CLAUDE.md — the cursor is the source of truth for content — never touches the
topic at all.

### Routing — nothing to do

`RoutingMiddleware` resolves `(chat_id, thread_id) → chats → session`
(`middleware/routing.py:293`). More rooms is more rows, not a different shape.
`Route.claimable_thread` (`:169`) still means "an empty private thread this
update already sits in", and `/fork` typed into a fresh *New Chat* thread should
claim it exactly as `/new` does.

### Voice

`voice/service.py:485` pulls `workspace.topic_name` into the transcription
key-terms; it should pull the session's instead (`session.title` is already
there). `voice_inputs` stores `(chat_id, thread_id)` at enqueue time
specifically so a later rebind cannot move the action (`001_init.sql:505`) —
that stays correct.

### CI notices

`ci/notice.py` sends into the session's destination; unchanged.

## Telegram-side risks

- **Room count.** A busy week is ~10 workspaces today; with forks it could be
  30–50 rooms in one DM. Telegram has no documented cap, and the list is
  scrollable and searchable, but `/done`-retires-many and `/tidy` get more
  important, not less. Keep `retire_topic`'s delete-first behaviour.
- **Rename budget.** Unchanged per session — `apply_marker` is still a no-op
  when the rendered title would not change (`topics.py:930`) — but the *total*
  rename rate scales with rooms, and they share one bot token's flood budget.
  Worth a cheap global check: markers are only applied on transitions, never on
  a timer, and that rule must not soften here.
- **DM degradation.** Every new create path (`/fork`) must degrade to the linear
  seat the same way `/new` does, or a DM without threaded mode loses `/fork`
  entirely. In the linear seat, `/fork` keeps today's behaviour exactly:
  rebind the one seat, and `/s` switches back.

## Migration and rollout

0. Clean up the duplicate bound sessions vector 3 has been leaving on seats
   since the first `/fork`, and add the two uniqueness indexes. This is the step
   that makes "one session per room" true rather than intended, and it is also
   the one that can fail on live data — run its `SELECT` count first.
1. Migration 003 + `sessions` repo helpers + backfill. No behaviour change.
2. Move the writers (`common.py`, `adopt.py`, `actions.py`, `session_poller.py`)
   to the session columns; keep every reader dual-reading
   `session.topic_id ?? workspace.topic_id` for one deploy.
3. Move `/fork` to open its own room. This is the user-visible flip.
4. Move `/done`, `/tidy`, `/name -w`, `prompts.tidy_rename_notice`.
5. The `/board` two-stage picker — including its bind-the-seat branch for a chat
   with no topics — then, in the *same* release, retire `/s`: out of
   `BOT_COMMANDS` and `_HELP`, kept as a silent alias to `/board`. The second
   user-visible flip and the only one with new callbacks.
6. Delete the dual reads; migration 004 drops the workspace columns.
7. Docs: `PLAN.md` §Chat model, `HANDOFF.md`, `CLAUDE.md`, `README.md`,
   `GETTING_STARTED.md:255`, `SETUP.md:101`, and the `/help` card
   (`power.py:76-95`) — `/fork` now reads "new session, new topic" and `/board`
   "workspaces, then their sessions".

Steps 1–2 are safely deployable on their own; step 3 is the one that changes
what a person sees; step 5 can ship separately from it, in either order —
the picker's stage 2 is correct under the old model too, it just lists sessions
that all share one room.

## Tests

Named by what the change can break, per CLAUDE.md:

- `tests/test_repo.py` — the new session topic columns, RLS still forced on
  them, the backfill's exact `WHERE` (a session bound to a *different* thread of
  the same workspace must not inherit the room).
- `tests/test_bot_handlers.py` — `/fork` adds a session to the **existing**
  workspace (no `POST /workspaces`), opens a new room, binds `chats` for that
  thread to the new session, and leaves the parent room's `chats` row and marker
  untouched; a failed `POST /sessions` discards the room it opened; `/fork` in a
  linear DM still rebinds the one seat; `/done` retires every room of the
  workspace; `/name -w` re-labels every room.
- `tests/test_bot_adopt.py` — `/attach` opens exactly one room for N remote
  sessions; a second `/attach` jumps rather than opening a sibling.
- `tests/test_bot_actions.py` — `SetTopicMarker` renames the *session's* room;
  two sessions of one workspace in different states hold different prefixes at
  the same time (this is the regression the whole change buys).
- `tests/test_session_poller.py` — `UnbindTopic` clears the session's room only.
- `tests/test_outbox.py` — should need no edit. If it does, something moved that
  was not supposed to.

Before claiming any of these have teeth, break the code they cover and watch
them fail (CLAUDE.md).

<a id="board-stage-by-stage"></a>

## `/board`, stage by stage

`/board` becomes a two-stage picker in **one message**, edited in place. Stage 1
picks a workspace; stage 2 picks a session inside it; picking a session connects
it — opens or jumps to its Telegram topic. A session is one Conductor chat, and
that is what a topic is now per.

The wording is the feature. At every moment the card must say which of the two
things is being chosen, so the two stages share no noun, no verb and no button
shape.

### Stage 1 — choose a workspace

```
Workspaces · 4
Tap one to see its sessions.

[ ✅ acme-api · 3 sessions ]
[ ⚙️ billing-svc · 1 session ]
[ 💤 web · 2 sessions ]
[ 💭 infra · no sessions yet ]
```

- Header names the thing being chosen (`Workspaces`) and the next step
  (`see its sessions`). It never says "open" — in this card nothing opens.
- One button per workspace, from `board_rows` (`core.py:289`), which already
  collapses the per-session view rows to one row per workspace. The session
  count comes from that same fetch: count the view's rows per `workspace_id`
  *before* collapsing, which is free and is currently thrown away.
- The icon is the workspace's **most active** session state, not the first row's
  — `status_icon` over the max of `_ACTIVE_STATES`, so a workspace with one
  working session reads `⚙️` even if its newest session is idle.
- **Every row behaves identically**, whether or not this chat already has rooms
  for it. `/board` loses its `adopt_button` / `+ Open …` special case
  (`core.py:262-278`): a laptop workspace and a local one both drill down, and
  the difference only shows up in stage 2 (jump vs. open). `adopt_button` stays
  in `/attach` and `/s`.
- Cap `BOARD_VISIBLE` (10) with the existing `+N more · /board name` line.

### Stage 2 — choose a session

```
acme-api · 3 sessions
Tap one to open it in this chat.
main · claude/opus-5

[ ✅ fix flaky login test · opus-5 ]
[ ⚙️ port billing to v2 · opus-5 ]
[ 💭 rename the CLI flags · sonnet-5 ]
[ « All workspaces ]
```

- Header names the **workspace** as context and `sessions` as the thing being
  chosen. The verb changes to `open … in this chat`, which is the thing stage 1
  deliberately never says.
- Back is always last and always reads `« All workspaces` — it names the
  destination, not the direction, so it is readable alone.
- Session rows come from `GET /v0/workspaces/{id}/sessions` (adopt's
  `_all_sessions`, `SESSION_SCAN` = 20) unioned with
  `sessions_repo.list_for_workspace`, because a session created on the laptop
  seconds ago is in neither the view nor the local cache reliably. Label:
  `{icon} {session title} · {model}`.
- A session that **already has a room in this chat** is a `url_button`
  (`jump_url`) — no ticket, no work, Telegram just jumps. In a DM topic
  `jump_url` returns `None` (Telegram publishes no link syntax for one), so it
  falls back to a connect ticket whose handler answers "already open here" and
  does nothing else.
- A session with **no room** gets a connect ticket.
- Zero sessions: `No sessions in acme-api yet.` plus Back. Not an error.

### Connecting

Tapping a session runs the adopt path scoped to that one session:
`adopt.adopt_workspace(..., session_hint=<session_id>)` already picks by hint
(`adopt.py:_pick_session`) — with per-session rooms it stops meaning "which
session gets the workspace's room" and starts meaning "which session to open",
so repeated calls for different sessions of one workspace open different rooms.
The per-workspace `_locks` (`adopt.py:122`) still serialise them.

Order, unchanged from `/new`: create the topic first, then write the binding,
then `seek_to_end`, then post the snapshot card into the new room.

**In a seat that cannot host topics** — a DM with threaded mode off, or a group
General — connecting binds *that seat* to the chosen session instead of opening
a room, which is precisely what `/s`'s `switch_callback` (`power.py:339`) does
today. This is the piece that lets `/s` be retired; it must land in the same
release, or the degraded DM loses its only switcher. The refusal `/s` carries
("That task has its own topic") stays, keyed on the session now rather than the
workspace: a session with a room is jumped to, never rebound, because rebinding
re-addresses its transcript and silences the room somebody is reading.

### Callbacks and nonces

Three new `Action` members in `keyboards.py:151`:

- `BOARD_WS = "bws"` — target `workspace_id`. Renders stage 2 in place.
- `BOARD_SESSION = "bsess"` — target `workspace_id\nsession_id` (the `\n`
  packing `adopt_callback` already uses at `adopt.py:697`). Connects.
- `BOARD_BACK = "bback"` — target `""`. Re-renders stage 1 in place.

Tickets are **single-use** (`NonceStore.consume`), so each render mints a fresh
keyboard and the outgoing stage's tickets simply expire — going back and forth
works because every transition re-mints, not because a ticket is reused. Stage-2
tickets are minted when stage 2 is *rendered*, never fanned out at stage 1: one
tap must not mint forty tickets.

Both stages redraw with `edit_html` (`topics.py:577`), which already treats
"message is not modified" as success and falls back to plain text on an
entity-parse 400. If the edit fails — the card was deleted — send a fresh card
rather than dropping the tap.

`CONTROL_TTL_S` (15 min) is the right TTL: a board left open on a lock screen
should expire into "run /board again", which is the existing message.

### Where else the wording has to move

- `board_lines` (`core.py:511`) is the **voice** path's text-only board. It gets
  the stage-1 wording and says nothing about tapping, because voice cannot tap.
  Its `+N more · use /s to switch` tail becomes `· say "open <name>"`.
- `/help` (`power.py:76`): `/board` becomes "workspaces, then their sessions".
- `nothing_to_attach` (`core.py:486`) counts rooms from `workspaces.topic_id`
  and moves to counting sessions with a room.
- `/attach` is now nearly a subset of `/board` stage 1. Keep it — it filters to
  *unattached* workspaces and takes a text query — but the two must use the same
  noun for a workspace and the same noun for a session.

### Tests for the picker

In `tests/test_bot_handlers.py`:

- stage 1 renders one button per workspace with a session count, and **no**
  `+ Open` adopt buttons;
- tapping a workspace edits the same message (same `message_id`) and the new
  text names that workspace and the word `sessions`;
- Back returns to stage 1 and the stage-1 text is byte-identical to the first
  render except for its nonces;
- a session with a room renders a `url_button`; one without renders a callback
  button;
- tapping a session opens exactly one room and binds that session, and a second
  tap on a *different* session of the same workspace opens a second room;
- an expired stage-2 ticket answers "run /board again" and leaves the card
  alone;
- in a chat with no topics, tapping a session rebinds that seat instead of
  opening a room — the assertion `tests/test_bot_handlers.py:1793` makes about
  `/s` today, moved;
- `/s` is absent from `BOT_COMMANDS` and from `_HELP`, and still routes to the
  `/board` renderer while the alias lasts.

### Deliberately not in v1

- Pagination inside stage 2 (`Action.PAGE` exists; a 20-session workspace shows
  10 and a `+N more` line).
- A `+ New session` button in stage 2. It is one `/fork` away and adds a third
  verb to a card whose whole point is having exactly two.
- Renaming the noun. The bot says **session** everywhere today (`/s Switch
  session`, `/fork New session here`, `sessions_repo`, `POST /v0/sessions`), so
  this spec says session; if Conductor's own UI calls them chats, it is one
  constant and one pass over the copy above.
