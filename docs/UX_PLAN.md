# Operating agents from a phone — where it gets hard, and what to do

Companion to [`TOPIC_PER_SESSION.md`](TOPIC_PER_SESSION.md). That document says
*how* to give every session its own room. This one asks what that does to the
person holding the phone, and what else is worth building once it lands.

Everything here is grounded in code that exists today; each item names the file
it touches and what it costs.

> **Shipped so far** (see the section for each): **A4** `/digest`, **B1** the
> receipt names the files it changed, **B2** `/log` is readable, **B4** the
> cockpit offers three destinations. **A1 turned out to be impossible as
> written** — Telegram gives a bot no read event — and the correction is
> recorded in place rather than deleted.

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

### A1 · `✅` must mean *unread*, not *finished* — **and it cannot, quite**

**Friction.** `TopicMarker.DONE` is applied on finalize (`machine.py:471`) and
cleared only by the *next* state transition. So a room you have already read
keeps its `✅` until you prompt it again, and after a busy afternoon every room
in the list is `✅`. A signal that is always on is not a signal.

> **Correction to the first draft of this plan, which said "reading a room
> clears it".** Telegram gives a bot **no read event** — no "chat opened", no
> read receipt. The only evidence a room was looked at is an update *from* it,
> which means a message or a button tap. Opening a room and reading it is, to
> the bot, indistinguishable from never opening it. So this item cannot be
> built as written.

**What is left, and it is worth less.** Any interaction in a room can clear a
stale `DONE`, but prompting already does (the marker follows the session to
`WORKING`), which leaves only "ran a command in the room" — a rare event.

**So the value moved to A4.** `/digest` answers the question the prefix was
being asked to answer, ranks by what actually wants attention, and needs no read
event to be correct. **Shipped there instead.**

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

### A4 · `/digest` — the answer to "what happened while I was away" · **shipped**

**Friction.** `/board` lists workspaces; nothing anywhere answers "three
finished, one wants an answer, two are still going". After this change that
question is asked against thirty rooms instead of ten.

**Shipped**, and it ranks rather than lists — worst first, because the card's
job is "what needs me?" and not "what happened?":

```
Last 1d · 1 errored · 1 stalled · 2 running · 3 finished

⚠️ rename CLI flags · web/main · model overloaded · 1h04m
⏳ upgrade deps · infra/main · no output · 41m00s
⚙️ port billing · acme-api/main · WORKING · 12m03s
✅ fix flaky login · acme-api/main · 4m12s
```

Three decisions worth keeping:

- **Stalled is its own bucket.** Working-but-silent past `NO_OUTPUT_WARN_S` is
  the state nothing else in the UI can show: the topic list wears `⚙️` for both,
  and it is the most common reason somebody picks the phone up.
- **The window never hides something broken.** A session that errored two days
  ago is precisely what this card is for; filtering it out under "nothing
  happened recently" is how it stays broken. Only *finished* work ages out.
- **Local rows only** — no Conductor call, so it is the one command that still
  answers during an API outage.

A daily push at a tenant-set hour is still worth building and is not here.

---

## P1 — the result is the product, and it is the weakest surface

### B1 · The finish line should say *what changed* · **shipped**

**Friction.** `finish_line` reads `✅ Done · 1m32s · 12 tools · 5 files`
(`bot/actions.py:57`). Five files — which? The answer is in the transcript that
just went past, above a wall of tool narration.

**And it was worse than that.** `files_changed` was **hardcoded to `0`** in
`machine._finalize`, so both readers of it — the finish line and the done card —
rendered a segment that could never appear. Every turn since the machine was
written has said "12 tools" and nothing at all about files.

**Shipped.** `Delta` now carries `edited_paths`, collected in
`cursor.build_delta` with the renderer's own `describe_file_edit` so the receipt
cannot disagree with the transcript about what an edit is; `TurnContext`
accumulates them per turn, first-seen order, capped at `EDITED_PATHS_CAP`; the
finish line names up to five and counts the rest. Not persisted — a redeploy
mid-turn degrades it to what every turn showed before, which is nothing.

The PR half needed no work: `_share_review_pr` already finds and pins the link
(`bot/actions.py:210`).

### B2 · `/log` should be readable · **shipped**

**Friction.** `/log` sends a `.md` file of raw JSON blocks
(`power.py:599-609`). On a phone that is unreadable, and it is the only
"show me what happened" command there is.

**Shipped.** `/log` renders the last N exchanges as one line each — `›` for
your prompt, `·` for anything the agent said or did — through
`cursor.preview_text`, which is the same reducer the first-bind preview uses, so
the log phrases a tool call exactly as the status card does. `/log raw` keeps
the JSON document, unchanged. An envelope the renderers cannot parse (the 64 KB
cap can cut one mid-object) is dropped rather than printed as a blank row.

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

### B4 · The cockpit should offer more than one destination · **shipped**

**Friction.** `cockpit_target` returns exactly one session — the most recently
prompted (`core.py:570`) — so a line typed in the DM root can only go to the
last thing you touched. With thirty rooms that is a coin flip.

**Shipped.** `cockpit_targets` returns up to `COCKPIT_TARGETS` (3), newest
first, deduplicated by session; `cockpit_target` stays as the single-head
helper. Both the typed and the spoken path get it, because both already go
through `cockpit_markup`.

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

**Done:** A4, B1, B2, B4 — the four that stand alone, need no migration and are
worth having whether or not topic-per-session ever lands. A1 was dropped for the
reason recorded above.

**Next, in order:**

1. **A2 and A3**, with the topic-per-session change: they are what keeps a
   thirty-room list usable, and both become wrong to skip the moment rooms
   multiply. Both want a `tenant_settings` column, which is the migration this
   first batch deliberately avoided.
2. **C1** — the diff is already rendered and merely switched off, so "Show
   changes" is a button rather than a feature.
3. **B3, B5**, then the rest of P2 as they earn their place.

One rule for all of it: **every one of these reads from rows the bot already
writes.** Nothing here needs a new Conductor endpoint, a webhook, or a second
poller, which is why the whole list is weeks rather than quarters.
