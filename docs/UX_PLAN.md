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
>
> **D1–D4 (2026-10-01)** close the hole underneath all of it: *a DM can be
> written to from anywhere and could only be read from inside a room it is
> impossible to link to*. See §D.

---

## D · Write from anywhere, read only from inside — the asymmetry nobody named

Measured against the live database rather than reasoned about, because the
numbers are what make this a hole rather than a preference. One owner's private
chat holds **43 threads**. Of those, **1 routes to a usable session**: 22 point at
a `DEAD` session whose workspace has since been archived, 2 at an `IDLE` session
in an archived workspace, and 18 have no session at all. Three carry a state
prefix; **40 carry none**, because a marker is only written once a rename
succeeds and these died before one did.

The structural fact underneath it is one line of `topics.jump_url`:

> Only supergroups (`-100…`) have the `/c/` form; a DM has nothing to jump to —
> not even a DM *topic*, which Telegram publishes no link syntax for.

So **every "go and look over there" affordance in the bot silently evaporates in
a private chat**, which is the default and recommended flow: `/start`, `/key`,
`/new` all happen there. The result was an asymmetry no document had stated:

| what the owner wants | before |
|---|---|
| "what needs me?" | `/digest` — good ranking, **no buttons at all in a DM** |
| "take me to it" | **impossible**, and nothing said so |
| "send a follow-up" | ✅ type in the root; the cockpit offers three seats |
| "read the last answer" | **only from inside the room** |
| "stop it" | **only from inside the room** |

Writing had a cockpit. Reading had a dead end — in the surface `§B` already calls
*"the result is the product, and it is the weakest surface"*.

### D1 · A digest row carries a verb where it cannot carry a destination · **shipped**

`digest_buttons` emitted a jump link or nothing, documented as *"correct rather
than degraded: in a DM the thread list is one swipe away and a dead button would
be worse than no button"*. The premise is right and the conclusion was not: the
card ranked the one task that wanted attention and then handed the reader a
43-entry list to find it in.

Rows now fall back to `📄` — render this task's last exchanges *here* — through
the same reducer as `/log`. A supergroup keeps `↗` and jumps, because there the
link works. The glyphs differ deliberately: `board_stage1` already learned that
two buttons which look alike must not behave differently, and these two do
differ — one moves you, one brings it to you.

### D2 · The `Transcript` button was the half of B2 that never shipped · **shipped**

B2 made `/log` readable and left the button beside it sending a `.md` of raw JSON
envelopes — the exact artefact the fix was about (*"a phone cannot read JSON"*) —
on the finished **and** errored cards, the two most-tapped surfaces there are.
The handler had no test of its own, only assertions that the button *exists*,
which is how a command and its own button came to disagree about the answer.
Both now go through `power.log_body`; `/log raw` keeps the envelopes.

### D3 · `/log` answers from the chat root · **shipped**

Every session command answers *"No session here. Use /new or /board"* outside a
room. That is right for `/stop` and useless for a read, because there is no
tappable route to a DM room to take the advice with. With no room in scope `/log`
now falls back to the seats the cockpit already offers for sending: one head is
answered directly, several put the choice on screen through the same `📄` button.
`require_session` is untouched for mutations — guessing which agent to *show* you
costs a tap when it is wrong; guessing which to *stop* costs somebody's turn.

### D4 · A buried room says so instead of asking Conductor · **shipped**

22 of those 43 threads point at a `DEAD` session. Typing in one spent a Conductor
call to be refused and reported it as `Prompt failed: …` — a stack-shaped answer
to "why did nothing happen", about a room the bot had already buried. The session
row is the one already loaded for the receipt, so the check costs no query.

### D5 · Replying to a rendered answer reaches the task · **shipped**

Found by walking the loop rather than the code: `/digest` ranks the task that
wants attention, `📄` renders it in the chat root — and then *what*? The gesture a
Telegram user already knows for "about this one" is a swipe-reply, and PLAN
§Safety rails already promises it works: *"replying to any bot message routes to
that message's session"*. It is implemented by
`deliveries.session_for_telegram_message`, which reads the delivery ledger — so a
task's output rendered **outside** the outbox was, to that lookup, not the bot's.
Reading from the root and replying addressed nothing.

`deliveries.record_sent` writes the one row that closes it, in a single statement
rather than `enqueue` + `mark_sent`: a `pending` row is a row the outbox is
entitled to claim, and the window between two writes is wide enough for it to
send the message a second time.

**And a reply is the right control here, not a row of buttons.** The same
reasoning that keeps `Stop` off a receipt bubble applies — *"a bubble is a static
message, so its Stop was still on screen, still tappable, fifteen minutes after
the turn ended"*. A reply is evaluated when it is sent, so it cannot go stale.
That is why this surface gained a reply target instead of the `Stop`/`Retry` pair
that first suggested itself.

### D6 · The console's verb survived neither a redeploy nor a coffee · **shipped**

Two defects in one button, both invisible until the payload was decoded.

`NonceStore` is **in memory**, and `button(..., restartable=…)` defaults to
`False` — so the `📄` added in D1 minted a random single-use handle and died with
the process. On a service that redeploys as often as this one, the ranked card's
only verb would answer *expired* for reasons no reader could see. `TRANSCRIPT` was
already in `RESTARTABLE_ACTIONS` ("Stop, Retry, Transcript and Check are all safe
to repeat"), so the fix is one keyword and the signed self-describing payload the
mechanism was built for.

The second is the window. `CONTROL_TTL_S` is fifteen minutes, sized for *"the
phone was locked"* — correct for `Stop`, whose target may not be the same turn by
the time a stale tap lands. A transcript carries no such hazard, and a ranked card
is exactly what somebody scrolls back to twenty minutes later. `READ_TTL_S` is six
hours: a third tier beside the two the file already distinguishes (60s for a
destructive confirm, 15min for a safe control), and not a wider grant than the
team already has, since row-level security is per *team* and any member can read
these transcripts by command.

## E · Dead ends, dead loops, and a number nobody can proof-read

A pass with one question: *where can this bot leave somebody with nothing to do
next, or let them make a mistake it could have caught?* Built from an inventory —
every `Usage:` line, every bare refusal, every step that accepts free text — rather
than from intuition.

### E1 · Typing at a button-only wizard step was swallowed · **shipped**

The `/new` wizard draws buttons for four of its seven steps (project, agent, model,
effort) and registered a text handler for **none** of them. A phone composer invites
typing — the launcher's placeholder reads *"Describe a task…"* — so a line typed
at *"Project?"* fell through to `plain_text`, which declines to start a task
while a wizard is open and answers with the **chat-root cockpit hint** instead. The
typed word was discarded, the reply was about something else, and the wizard was
still sitting there waiting to be tapped. A dead loop in the one flow that spends
money.

`match_option` resolves the text against the options already on screen: exact first,
then a unique substring, so `opus` finds `opus-5-1m` and an ambiguous `acme` is
**refused rather than guessed** — picking the wrong repository costs a paid container
against it. Labels as well as values, because the project step's values are Conductor
ids and the label is what the reader is looking at.

This is free entry becoming a *choice*, not the other way about: the outcome is always
one of the buttons. Unmatched text redraws the step rather than answering "that is not
one of them" with no list, which on a phone is a wall.

`_advance` is now shared by the tap and typed paths, so the two cannot drift about what
choosing means — the agent step also has to reset the model, and a second copy of that
is a second place to forget it.

### E2 · `/board` had no way out of its own empty state · **shipped**

`No live workspaces.` — three words, in the command whose whole purpose is
getting you somewhere. It is the first thing a new team sees, and the last thing
an owner sees after archiving everything. `/digest` has always named the two ways
out of its empty state; this did not. A filter matching nothing said `No match.`
without repeating what failed.

### E3 · `/invite <id>` could seat a stranger on a typo · **shipped**

The one input in this bot where a mistake does not fail. A mistyped-but-real Telegram
id was a stranger seated in the organisation — its workspaces, transcripts and
Conductor key behind it — and the bot answered `Added 12345 as member.` either way.
Nobody can proof-read a number they have never seen.

The digits are now resolved with `get_chat` and the owner confirms a **name**:
*"Add Dana Scully (@dscully) to acme as admin? They will see every workspace and
transcript in it."* A two-tap confirm on the existing 60-second tier.

That resolution is also the honest way to enforce the precondition the command only
ever mentioned in its usage line — *"they must send the bot a message once before this
works"*. A user the bot cannot see is a seat that could not be used, so refusing names
the fix instead of reporting a silent half-success.

### Considered and deliberately not done

- **A status button on the home keyboard.** `handlers/home.py` records that a
  `/board` button was drafted there and cut, because *"in a threaded DM the topic
  list already is the board — with live state icons the bot could not draw in a
  message"*. Tested against the live rows: the three marked rooms are live and do
  sort near the top, so the claim mostly holds; what it misses is that the 18
  unbound rooms are interleaved with them by date, so about half of the recent
  list is dead and unmarked. Noisy, not broken — not enough to overturn a
  reasoned decision, and a third entry on a chat-wide keyboard is not free.
- **A `/stop` chooser for the chat root.** The same shape as `/log`'s, and wrong:
  the chooser bubble is static, so fifteen minutes later its button stops whatever
  is running *by then* — the precise hazard that keeps `Stop` off a receipt bubble.
  A 60-second TTL would bound it, but a destructive control offered against a list
  the reader did not ask to act on is a mistake generator, which is what this pass
  was supposed to remove.
- **A contact picker for `/invite`.** `KeyboardButtonRequestUsers` is the native
  answer and would remove the number entirely — but it is a *reply* keyboard, and
  this bot already spends that one surface on the launcher, which `handlers/home.py`
  curates deliberately ("anything on it has to make sense in every room at once").
  Swapping it out mid-flow and restoring it afterwards is more moving parts than
  resolving the id and showing the name. Worth revisiting if the launcher ever goes.
- **`Stop` / `Retry` buttons under rendered output.** The obvious next step after
  "read it here", and wrong for a reason this codebase had already written down: a
  static button outlives the state it was drawn for. D5's reply target does the
  same job without that failure mode.
- **Auto-retiring finished rooms (A2).** Still the right idea, still unshipped,
  and worth re-costing first: Telegram keeps a *closed* topic in the list, so
  "close, never delete" may not reduce the scroll it is meant to reduce. That
  wants measuring before it is built, not after.

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
