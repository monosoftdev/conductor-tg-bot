# Operating agents from a phone — where it gets hard, and what to do

Companion to [`TOPIC_PER_SESSION.md`](TOPIC_PER_SESSION.md). That document says
*how* to give every session its own room. This one asks what that does to the
person holding the phone, and what else is worth building once it lands.

Everything here is grounded in code that exists today; each item names the file
it touches and what it costs. Nothing is implemented.

---

## The one thing the change costs

One room per workspace is roughly **one room per task**. One room per session is
**one room per attempt**. A week that produced ten rooms will produce thirty,
and the topic list is the only navigation this bot has.

That is a good trade — the rooms are what make parallel agents legible at all —
but it is only a good trade if the list stays scannable. Three of the four P0
items below exist for that reason alone. If none of them ships, this change
makes the bot worse for its heaviest user and better for nobody.

Two things already work in our favour and should not be "improved":

- **Telegram sorts topics by last activity.** The room that just finished is at
  the top, for free. Any custom ordering we invent would fight it.
- **`topic_icon_color` hashes the workspace label** (`topics.py:310`), so a
  workspace's rooms are one colour block in the list. Keep it; it is the only
  grouping Telegram will give us.

---

## Angles

**Solo owner, commuting.** Starts two tasks, pockets the phone, reads on
arrival. Needs: start in one message, learn it finished without watching, read
the answer without scrolling, act in one tap. Mostly works today; the weak link
is *reading the answer* (§C).

**Owner running five agents at once.** The topic list is the console. Needs a
one-glance answer to "who needs me?" and a way to make the finished ones go
away. Weakest area, and the one this change stresses (§A, §B).

**Returning after a night.** Twenty rooms, six of them `✅`. Needs a digest, not
a list (§B).

**Team in a group.** Several people, one topic list. Needs to know who is
driving a room and whose result this is (§F).

**Voice-first, hands busy.** Dictation works; there is no way back except the
screen (§G).

**Handoff to the laptop.** `deep_link` on every card. Already solved.

---

## P0 — ships with topic-per-session, because that change creates the need

### A1 · `✅` must mean *unread*, not *finished*

**Friction.** `TopicMarker.DONE` is applied on finalize (`machine.py:471`) and
cleared only by the *next* state transition. So a room you have already read
keeps its `✅` until you prompt it again, and after a busy afternoon every room
in the list is `✅`. A signal that is always on is not a signal, and it is the
one the eye actually uses.

**Change.** Reading a room clears it. `RoutingMiddleware` already knows which
room every update came from and marks it as the send queue's focus
(`routing.py:248`); in the same place, if the room's session marker is `DONE`,
apply `IDLE`. One extra call on an update that is already doing DB work, and
only on the transition.

**Where.** `bot/middleware/routing.py`, `bot/handlers/topics.apply_marker`.
**Size.** Half a day. **Test.** Prompting or even opening a `✅` room clears the
prefix; a room nobody touched keeps it; clearing does not fire on every update.

**Why it is P0.** It is what turns the topic list into an inbox, and an inbox is
the only structure that survives thirty rooms.

### A2 · Finished rooms retire themselves

**Friction.** `/tidy` closes stale and archived rooms (`power.py:807`) — but it
is manual, owner-only, and nobody runs a cleanup command on a phone. With one
room per session, "nobody tidies" becomes unusable within two weeks.

**Change.** A per-tenant `auto_tidy_days` (default 7, `0` disables). The `prune`
worker that already runs cross-tenant closes any room whose session is idle,
has no unread marker, and has not been prompted in that window. **Close, never
delete** — the transcript is the customer's, and `retire_topic` already treats
delete as the privileged path with close as the fallback (`topics.py:1014`).
Say it once in the room before closing, so it is never a surprise.

**Where.** `db/repo/sessions.py` (a query), the prune service, `tenant_settings`.
**Size.** One to two days. **Test.** A room with an unread `✅` is never closed; a
tenant at `0` is never touched; closing is idempotent.

### A3 · A new room inherits the tenant's notification default

**Friction.** `chats.notify` defaults to `'quiet'` per row
(`001_init.sql:150-179`), and `/notify` is per room. Thirty rooms means thirty
`/notify` calls for anyone who wants `loud`, which nobody will do.

**Change.** `tenant_settings.default_notify`, inherited at room creation;
`/notify` with no argument in the root sets the tenant default and says so.

**Where.** `db/repo/chats.ensure`, `handlers/power.notify`.
**Size.** Half a day. **Test.** A fork's room starts at the tenant default, not
at its parent's setting (this is already **F-88** in the fault catalogue).

### A4 · `/digest` — the answer to "what happened while I was away"

**Friction.** `/board` lists workspaces; nothing anywhere answers "three
finished, one wants an answer, two are still going". After this change that
question is asked against thirty rooms instead of ten.

**Change.** One card, computed entirely from local rows — no API call, so it
works when Conductor is down:

```
Since 18:20 · 3 done · 1 needs you · 2 running

✅ fix flaky login · acme-api · 4m ago      [open]
✅ port billing to v2 · acme-api · 22m      [open]
⚠️ rename CLI flags · web · error 1h        [open]
⚙️ upgrade deps · infra · 12m in            [open]
```

Grouped by state, newest first, jump buttons, capped with a `+N more` line —
the same conventions `/board` already uses. Optional daily push at a
tenant-configured hour, silent, off by default.

**Where.** New `handlers/digest.py`, reusing `signals`, `status_icon`,
`jump_url`. **Size.** Two days for the command, one more for the scheduled push.
**Test.** Counts match the rows; a room with no topic renders an *Open here*
button rather than a dead link; the push never fires twice for one window.

---

## P1 — the result is the product, and it is the weakest surface

### B1 · The finish line should say *what changed*

**Friction.** `finish_line` reads `✅ Done · 1m32s · 12 tools · 5 files`
(`bot/actions.py:57`). Five files — which? The answer is in the transcript that
just went past, above a wall of tool narration.

**Change.** Append the changed paths (up to five, then `+N`) and the pull
request URL when there is one. The CI watcher already extracts a
`github.com/owner/repo/pull/NN` from the transcript tail
(`ci/watcher.py`, and `HANDOFF.md` calls the link "the announcement"), so the
same extraction feeds this. No new state, no new API call.

**Where.** `bot/actions.finish_line`, `turn/state.TurnSummary`.
**Size.** One to two days. **Test.** A turn touching 20 files shows 5 + `+15`; a
turn with no PR link renders exactly as today.

### B2 · `/log` should be readable

**Friction.** `/log` sends a `.md` file of raw JSON blocks
(`power.py:599-609`). On a phone that is unreadable, and it is the only
"show me what happened" command there is.

**Change.** `/log` renders the last N *exchanges* — prompt, reply, tool count —
as text in the room. `/log raw` keeps today's JSON dump for debugging.

**Where.** `handlers/power.log_command`, reusing the render adapters.
**Size.** One day. **Test.** `/log` never exceeds the 4096 UTF-16 chunk; `/log
raw` is byte-identical to today.

### B3 · One tap for the three things everyone types

**Friction.** The most common phone prompts are "run the tests", "open a PR",
"fix CI". The first two are typed by hand every time; only the third has a
button, and only after CI fails (`Action.FIX_CI`).

**Change.** Per-tenant snippets — `/snip add test run the test suite and report
failures` — offered as buttons under the finish card, beside `Retry`. The
quick-reply plumbing (`quick_reply_keyboard`, `keyboards.py`) already renders
button-shaped canned text; this is a stored set rather than a per-turn one.

**Where.** New `tenant_snippets` table, `keyboards.status_card_keyboard`.
**Size.** Two days. **Test.** A snippet is sent as a prompt to *this* room's
session; snippets are tenant-scoped by RLS like everything else.

### B4 · The cockpit should offer more than one destination

**Friction.** `cockpit_target` returns exactly one session — the most recently
prompted (`core.py:570`) — so a line typed in the DM root can only go to the
last thing you touched. With thirty rooms that is a coin flip.

**Change.** Offer the three most recent, and let the fourth be `/board`.

**Where.** `handlers/core.cockpit_target`/`cockpit_markup`.
**Size.** Half a day. **Test.** Three buttons, newest first, each addressing its
own session; unchanged when only one exists.

### B5 · `/spend`

**Friction.** Cost appears once, on a finish line, and is then gone.
`format_cost` already exists (`delivery/render/adapters/result.py`) and the
`result` records that carry it are stored in `transcript_messages`. The roadmap's
stated position is "visible counters, not paternalism" — the counter is simply
not visible past the moment it scrolls away.

**Change.** `/spend` — today, this week, by workspace. Local rows only.

**Where.** New handler, `db/repo/transcript`. **Size.** One to two days.
**Test.** A workspace with no result records reports zero, not an error.

---

## P2 — worth doing, not worth blocking on

### C1 · The diff already renders — it is just switched off

`BlockKind.DIFF` has a full adapter (`render/adapters/diff.py`) and is gated at
`Verbosity.VERBOSE` (`render/types.py:128`), while a room's `verbosity` defaults
to `'normal'` (`repo/chats.py:81`). So the diff exists, is rendered correctly,
and nobody sees it — and turning the whole room verbose to get it also turns on
tool spam and thinking, which is why nobody does.

Cheaper than building a diff view: a **`Show changes`** button on the finish
card that re-renders *this turn's* `DIFF` blocks at verbose, once, on demand.
The blocks are already in `transcript_messages`.

Separately: `Action.JUMP`, `Action.DIFF` and `Action.CHANGE`
(`keyboards.py:169,176,177`) have no handler and no producer anywhere — grep
finds only `BlockKind.DIFF`, a different enum. Wire or delete; three dead
members that read like features are how somebody concludes the bot has a diff
view it does not have.

### C2 · Parallel attempts, now that they have somewhere to live

`SYSTEM_OVERVIEW.md:61` already advertises "run parallel approaches" via `/fork`
— which, until this change, meant several sessions sharing one room, which is
exactly the thing that did not work. With a room each, `/fork -3 <task>` (three
sessions, three rooms, one workspace, one checkout) becomes genuinely useful,
and the shared `icon_color` makes them read as a set in the list.

### C3 · Say who is driving

In a group, a room is shared and the finish line names nobody. Stamp
`chats.owner_user_id` on first prompt and show `· @who` in `/digest` and on the
finish line. Also lets `/board` in a group default to "mine".

### C4 · Voice gets `open`

`VoiceCommand` is `new · board · stop · find · mode · done`
(`voice/intent.py:32`). With a room per session, "open the billing one" is the
natural spoken verb and the one thing voice cannot do. Adding it is a phrase
table entry plus a call into `/board`'s stage-2 connect.

### C5 · Spoken finish lines

The tenant already stores its own speech key. A one-sentence TTS of the finish
line, opt-in per room, is the difference between usable and not while driving.
Costs money on every turn, so: opt-in, finish line only, never transcript
content — the same perimeter rule the rest of voice follows.

---

## Sequence

1. **With the topic-per-session change:** A1, A3 — both are small and both
   become wrong to skip the moment rooms multiply.
2. **Immediately after:** A2, A4. This is the pair that keeps a thirty-room list
   usable; ship them before anyone accumulates thirty rooms.
3. **Then:** B1, C1, B4, B2 — in that order. B1 is the largest single
   improvement to "easier to see reports" and touches one function; C1 is the
   second largest and is mostly a button, because the renderer is already
   written and merely switched off.
4. **Then:** B3, B5, and the P2 items as they earn their place.

One rule for all of it: **every one of these reads from rows the bot already
writes.** Nothing here needs a new Conductor endpoint, a webhook, or a second
poller, which is why the whole list is weeks rather than quarters.
