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

### `bot/handlers/power.py` — `/fork`, `/s`, `/name`, `/tidy`

**`/fork` is the centre of this change.** It becomes a small `/new`:

1. build `label = topic_label(project, branch, task=<fork title>)`;
2. `require_topic` / `claim_topic` / degrade-to-linear, through the same
   `_seat_for` logic — factor the DM-vs-group half of `_seat_for` out of
   `common.py` so `/fork` and `/new` cannot drift;
3. `POST /sessions` with a caller-supplied `sessionId` (already idempotent —
   `turn_cursor.create_session`);
4. bind the session to the *new* thread, `chats_repo.bind` that thread;
5. on failure, `discard_topic` the room it just opened.

The current "reset the topic marker to IDLE because the room now points at a
session that never ran" (`power.py:472-475`) disappears — the new room is born
`⏳`/`💭` and the old room keeps its own true marker.

**`/s` loses its switching job in a room and keeps it in a seat.** With every
session owning a room, `homed_elsewhere` (`power.py:133`) is true of almost
everything, so:

- in a **topic**: `/s` lists the sibling sessions of this workspace as *jump*
  buttons (`jump_url`), plus an *Open* button for a sibling that has no room
  yet — which mints one lazily and binds it. No `switch` action is offered.
- in the **linear DM root / General**: unchanged. That seat genuinely has one
  mutable binding and `/s` is the only way to move it, and the whole degraded
  path depends on it.
- `switch_callback` (`power.py:339`) keeps its refusal (`"That task has its own
  topic"`) and gains an `open` sibling that creates-and-binds.

`/name` (`power.py:481`): `/name text` renames the session **and** its room
(that is now the same object), `/name -w text` renames the workspace and must
re-label *every* room of that workspace, because `project/branch` is in all of
them. Loop over `sessions_repo.list_for_workspace`.

`/tidy` (`power.py:807`) iterates workspaces; it becomes an iteration over
sessions with a room, keeping the same 7-day staleness test on the session's
`updated_at`.

### `bot/handlers/core.py` — `/board`, `/done`

`/board` (`core.py:229`) is the workspace list and should stay one row per
workspace. Its jump target becomes "the workspace's most recently active session
with a room" — a `sessions` lookup instead of `local.topic_id` (`:257-261`).
Worth adding `· N tasks` to a row with more than one session, since the list can
no longer show them.

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

1. Migration 003 + `sessions` repo helpers + backfill. No behaviour change.
2. Move the writers (`common.py`, `adopt.py`, `actions.py`, `session_poller.py`)
   to the session columns; keep every reader dual-reading
   `session.topic_id ?? workspace.topic_id` for one deploy.
3. Move `/fork` to open its own room. This is the user-visible flip.
4. Move `/s`, `/board`, `/done`, `/tidy`, `/name -w`, `prompts.tidy_rename_notice`.
5. Delete the dual reads; migration 004 drops the workspace columns.
6. Docs: `PLAN.md` §Chat model, `HANDOFF.md`, `CLAUDE.md`, `README.md`,
   `GETTING_STARTED.md:255`, `SETUP.md:101`, and the `/help` card
   (`power.py:76-95`) — `/fork` now reads "new session, new topic".

Steps 1–2 are safely deployable on their own; step 3 is the one that changes
what a person sees.

## Tests

Named by what the change can break, per CLAUDE.md:

- `tests/test_repo.py` — the new session topic columns, RLS still forced on
  them, the backfill's exact `WHERE` (a session bound to a *different* thread of
  the same workspace must not inherit the room).
- `tests/test_bot_handlers.py` — `/fork` opens a room and leaves the old one
  bound to the old session; `/fork` in a linear DM still rebinds the one seat;
  `/done` retires every room of the workspace; `/s` in a room offers jump/open
  and never `switch`; `/name -w` re-labels every room.
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

## Open question worth deciding before step 3

`/board` currently shows workspaces. With rooms per session, is the primary list
still *workspaces* (one row, jumps to the newest room) or *sessions* (one row
per room, matching the topic list one-for-one)? This proposal assumes
workspaces, because `/board` is the cross-org view and the topic list is already
the per-room switcher — but it is a one-function change either way
(`core.board_rows`).
