# Naming, renaming, and status signalling

How the bot says *what is happening* — in the topic list, on the pinned card,
in `/board`, and with reactions. One vocabulary, one place to change it:
`src/ctb/signals.py`.

The rule that shapes everything below: **you look at the topic list to answer
one question — is anything waiting for me?** Every signal here is judged on
whether it helps answer that in one glance.

---

## Where the topic list is

Since 2026-07-27, in two places, and the rules below are identical in both.

- **A private chat with the bot.** The default. Telegram's *Threaded Mode*
  (@BotFather) — a bot may create, rename and delete topics there with **no
  admin rights and no Telegram Premium**. The sibling toggle *"Disallow users
  to create new threads"* governs the **user**, never the bot, so
  `BOT_FORUM_CREATE_FORBIDDEN` is never our error to fix.
- **A supergroup with Forum Topics on.** The optional `/team` flow, for several
  people watching one list.

One consequence for this document specifically: **non-Premium accounts can only
use the default topic-icon pack**, which is already the only pack anything here
uses — `getForumTopicIconStickers` is fetched once per process and nothing is
hard-coded. So the icon channel below needs no per-chat-kind branch.

The other consequence is the fallback. DM topics rest on a Bot API 10.x feature
with an open regression (`scripts/probe_dm_topics.py` is how you find out
whether your token has it). When Telegram refuses, a private chat holds **one
workspace at a time** and there is no list to scan — the whole vocabulary below
degrades to the pinned card and `/board`, both of which render from the same
constants. Nothing about a refused topic changes what a state *means*.

---

## The one vocabulary

| State | Glyph | Meaning |
|---|---|---|
| waiting | ⏳ | queued, waking, initializing — we have the work, nothing is running |
| working | ⚙️ | a turn is running now |
| done | ✅ | a turn finished and produced something you have not acted on |
| idle | *(none)* | bound and quiet: nothing running, nothing new |
| error | ⚠️ | the session reported an error, and can sit there indefinitely |
| cancelled | 🛑 | stopped on request |
| sleeping | 💤 | the workspace is asleep; a prompt may wake it |
| archived | 🗄 | archived in Conductor; the topic is closed too |
| unreachable | 🚫 | Conductor 404s for this session — gone, not merely quiet |

The topic prefix, `CARD_EMOJI` and `/board`'s `status_icon` all render from
these constants. They used to disagree on **every single state** — a working
session was `●` in the list and `⚙️` on the card — which is three vocabularies
to learn for one fact, and the disagreement is invisible until you see two of
them side by side.

---

## Topic titles

```
<prefix><task> · <project>/<branch>
⚙️ fix the login bug · api/main
```

Clipped to Telegram's 128-character limit, prefix included.

**The task leads.** Telegram clips a topic row from the right, and a phone shows
perhaps thirty characters of it, so whatever identifies the workspace has to be
in front. It was `<project>/<branch>` alone — and since a branch is nearly always
`main`, three workspaces on one repo were three rows reading `acme-api/main`,
in the same colour (it is a hash of the label) with the same state icon. The list
could not answer the one question you open it to ask.

The task is the first ~28 characters of the **opening** prompt, taken once, at
creation, with throat-clearing (`please`, `can you`, `let's`) stripped. It never
changes afterwards: `apply_marker` only ever rewrites the prefix. A name that
moved with the conversation would make the list unlearnable and cost a rename a
turn.

A workspace with no prompt to be named after — one adopted from the laptop, or
renamed by hand with `/name -w` — falls back to `<project>/<branch>`.

**The Conductor name is never a title.** Every workspace this bot creates is
called `tg-<chatid>-<nonce>` on the Conductor side, because `POST /v0/workspaces`
takes no idempotency key and that string is how an ambiguous create is
reconciled. It is bookkeeping. `human_name()` filters it out of every button,
list and title; the fallback is the session's own title.

**`done` and `idle` are different states.** They used to share a blank prefix,
so "finished, you haven't read it" and "quiet, nothing to do" looked identical
— which is exactly the distinction you open the list to make.

There is no read receipt for bots. So `done` cannot clear when you *look* at a
topic; it clears when you *act* in it — the next prompt moves the session to
`working` and the marker follows. Anything else would be a guess presented as
a fact.

### When a rename happens

Only on a state transition, and only when the rendered title would actually
change (`apply_marker`). Never on a timer: a rename is an API call, and a
5-second init card that renamed on every tick would spend the whole flood
budget on cosmetics.

**Every rename costs a permanent line in the topic.** Telegram answers
`editForumTopic` with a `forum_topic_edited` service message, and there is no
way to suppress it. Two things follow.

First, a prefix that lives for one poll interval is not worth a rename. A waking
workspace used to go `⏳ → (blank) → ⚙️` inside two polls, and a sleeping one
`💤 → (blank) → ⚙️`, because rule 10 marked the topic *idle* the moment the
workspace came up — while a prompt was outstanding and a turn was about to
start. Both now hold `⏳` until the turn actually begins: same information, two
fewer lines. `💤` is reserved for a workspace with nothing queued, which is the
only time it is true.

Second, `tidy_rename_notice` deletes the service message when the new title is
the one we just applied. A rename somebody made by hand keeps its receipt. It
needs a delete permission a group grants and a DM does not, so it is best
effort — a topic with one extra line in it is still a working topic.

A prompt accepted into a quiet topic marks it `⏳` immediately. That is one more
rename than before in the common flow, and it is the one worth paying for: the
list is the only surface you can read without opening anything, and it used to
keep saying `✅` from the *previous* turn until the agent produced output.

`/tidy` renames to archived **before** closing a topic — closing alone left
whatever prefix was last applied, so a swept topic could sit in the list
reading "⚙️ working" about a session nobody was running.

`/fork` resets the marker to idle. The title is unchanged (a fork shares the
workspace, so the label still describes it), but the previous session's ✅
would otherwise be a claim about work the new session has not done.

---

## Topic icons

Two channels, and they mean different things:

**`icon_color`** — set once at creation, `sha256(label) % 6`. **Cannot be
changed afterwards at any API level.** So it can only ever carry *identity*
(which workspace this is), never state. That it is stable is the point.

**`icon_custom_emoji_id`** — *can* be changed on every rename, and is. This is
the state signal, and it rides along on a rename that was happening anyway, so
it costs no extra API call.

Telegram serves bots a fixed pack (`getForumTopicIconStickers`) and refuses
anything outside it, so the ids **cannot be hard-coded from documentation** —
they are fetched once per process and cached. If the pack cannot be fetched, or
lacks the wanted emoji, the rename proceeds without touching the icon:

> A missing icon is cosmetic. A stale title is a lie.

That is also why the fetch catches *every* exception rather than just
`TelegramAPIError`.

Note Telegram ignores `icon_color` entirely once a custom emoji is set. That is
the right trade — state is what you scan for.

---

## Reactions

The receipt vocabulary, on the user's own message:

| Reaction | When |
|---|---|
| 👀 | prompt received |
| 👍 | turn finished successfully |
| 😢 | turn failed |

**Not every emoji is a valid reaction.** `setMessageReaction` accepts a fixed
list, and **`✅` and `⏳` are not in it** — both return `REACTION_INVALID`. The
obvious "upgrade" from 👍 to ✅ therefore breaks, which is why the card glyphs
above cannot simply be reused here. `signals.REACTION_SAFE` is the subset that
is verified to work.

A reaction is preferable to a message whenever the user just performed the
action being acknowledged: it costs no bubble and no scroll. That is why
`/stop`, `/name`, `/fork` and `/notify` react instead of replying.

---

## Ordering — the constraint that limits all of this

**The topic list sorts on last-message time, and nothing else.**

- A rename does **not** bump a topic.
- A reaction does **not** bump a topic.
- Only sending a message does.

So a topic that changes to "needs you" while other topics stay chatty will
*sink*, however clearly it is labelled. Renames and icons make a topic legible
once you are looking at it; they cannot make you look. Raising attention needs
either a message into the topic or a triage view (`/board`).

Any future "needs your input" work has to solve this too, or it is a signal
nobody sees.

---

## What we deliberately do not have

**A `needs input` state.** Conductor's status is `idle | working | error |
unknown` — there is no fourth value and no webhook, so "waiting on you" can
only be *inferred* from the shape of the transcript. A false positive is a
topic that lies, which is worse than no signal at all: it costs trust in every
other signal here. If it is built, it needs a confidence threshold and live
data to validate against.
