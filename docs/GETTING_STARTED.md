# Getting started

Drive your [Conductor](https://conductor.build) coding agents from Telegram.
This is the whole setup, once, from a phone: **two messages, both in your
private chat with the bot.** A minute, not ten. A Telegram group is optional
and comes later, or never.

**What you need before you start**

- A Telegram account.
- A Conductor account with at least one project.
- Nothing else. No laptop, no terminal, no credit card for this bot.

**What it will cost you.** Conductor bills your own API key for the agents you
run. This bot adds nothing.

---

> **A note on words.** Conductor calls a checkout-plus-agent a *workspace*, and
> `/new` creates those all day. The thing you get below is a **team**: it owns
> one Conductor API key and the people allowed to use it. One team, many
> workspaces. A team can also own a Telegram group, and most never will.

## Step 1 — Say hello to the bot

Open Telegram, search for the bot's handle (whoever runs the instance will have
given it to you), and press **Start**.

That is it. `/start` creates your team, named after your Telegram account, and
asks for one thing.

> **Want to name it yourself?** Send `/register acme` instead of `/start`.
> Letters, numbers and hyphens; anything else becomes a hyphen. It shows up in
> `/members` and in logs, so keep it boring. Running `/start` twice does *not*
> make a second team.

---

## Step 2 — Give it your key

Still in the private chat:

```
/key cnd_live_xxxxxxxxxxxxxxxxxxxx
```

From Conductor → Settings → API keys. The bot checks the key works, encrypts
it, and **deletes your message**.

Optionally, to talk to it:

```
/voicekey sk_xxxxxxxxxxxx
```

That one turns voice on by itself — there is no second switch. `/voice off`
pauses it later without throwing the key away.

> **Never send a key to a group.** If you do, the bot deletes it and tells you
> to rotate it — but other members may have already seen it. It catches a key
> typed on the wrong command too, so `/voice sk_...` is handled the same way.

You should see **ready**. Sign-up is over.

---

## Step 3 — Run something

In the same private chat:

```
/new fix the flaky test in checkout_test.py
```

The bot creates a Conductor workspace, opens a **topic** for it right there in
the chat, and posts a pinned status card that updates as the agent works. The
answer arrives in that topic.

From then on, **just type in the topic** to continue that session. No command
needed — it stays on that task until your next `/new`.

Telegram publishes no link to a topic inside a private chat, so there is no
"Open topic" button to tap here. The bot says which one it opened; you pick it
from the topic list at the top of the chat.

> **No topics in your chat?** You may see *Topics unavailable here · one
> workspace at a time.* Telegram, not the bot, decides whether a private chat
> can hold topics — see [below](#when-your-private-chat-has-no-topics). The bot
> keeps working; it just holds one workspace at a time, and `/s` switches.

---

## The daily loop

| What you want | What you send |
|---|---|
| Start something new | `/new [project:] your prompt` |
| Continue | Type, speak, or send audio **in that topic** |
| Stop the current turn | Tap **⏹** on the pinned card, or `/stop` |
| See what this session is doing | `/mode` |
| See everything at once | `/board` |
| Jump to another session | `/s` (or `/s name`) |
| Another session, same workspace | `/fork` |
| Open a laptop-made workspace here | `/attach`, then tap **+ Open** |
| Search your transcripts | `/find some text` |
| Finish and archive | `/done`, then confirm by name |

`/help` prints a short version of this in the chat.

Plain text is a **prompt** wherever a session is bound: a workspace topic, or
your private chat itself when it has no topics. Two places it is not:

- A group's **General** searches instead. That is the cockpit, and a prompt
  typed there would be a prompt sent to whichever session you last used.
- **The main chat of a private chat that does have topics.** Once `/new` has
  moved your work into topics, the root is a cockpit too. Type a task there and
  you get one button — *Send to «that task»* — instead of a guess. `/new` starts
  a new one.

`/s` reaches a session; it never moves one. A task that has a topic of its own
is listed so you can open it, because binding it to where you typed `/s` would
re-address its replies and leave its topic silent. Only tasks without a topic —
a linear chat's — are switched to in place.

### While it works, your phone stays quiet

A task is not one reply — the agent narrates, edits files, runs things. Two
rules keep that from becoming twenty notifications:

- **Progress goes on the pinned card, not into the chat.** Tool calls, the file
  it is editing right now, the elapsed clock — one message, edited in place. No
  bubble per changed file.
- **Nothing pushes until it is finished.** Then one line arrives and your phone
  buzzes once: `✅ Done · 1m32s · 12 tools · 5 files`. Errors are the exception
  and always push immediately.

`/notify` changes that per topic: `loud` also pushes every reply, `quiet` (the
default) pushes only the finish, `off` never pushes. `/mode verbose` puts the
file edits and their patches back into the chat if you want them there.

### Reading the topic list

Each topic says what its task is doing twice, so you can read the list without
opening anything: the **badge** beside the row, and the **first character** of
the name.

| | Badge | Name |
|---|---|---|
| Starting up | ⌛ | `⏳ fix login · api/main` |
| Working now | ⚡ | `⚙️ fix login · api/main` |
| Finished, unread | ✅ | `✅ fix login · api/main` |
| Quiet | 💭 | `fix login · api/main` |
| Error | ❗ | `⚠️ fix login · api/main` |
| Asleep | 💤 | `💤 fix login · api/main` |
| Archived | 🏁 | `🗄 fix login · api/main` |

The colour of a topic never changes — Telegram fixes it when the topic is made,
so it means *which task*, not what it is doing.

---

## When your private chat has no topics

Topics inside a private chat are a Telegram feature, and the bot can only ask
for them. Two things decide whether you get them:

- **Threaded Mode** must be on for this bot — the operator sets it in
  @BotFather. You need no Premium, and the bot needs no rights in your chat.
- Telegram's own Bot API has to accept the call. This is new ground, and there
  is a known regression where a thread is created but cannot be written to.

If either fails, the bot says **Topics unavailable here · one workspace at a
time** — once, not every time — and carries on linearly: one bound workspace in
this chat, `/s` to switch to another, everything else identical. Nothing is
lost and nothing is stranded; the workspace is created either way.

Operators: `scripts/probe_dm_topics.py` answers this against a live token in
about five seconds. Run it before promising anybody a topic list.

If you want a guaranteed topic list today, use the optional group below —
group topics have worked for years.

---

## Optional — a group for several people

One team, several people, one topic list everybody sees. Skip this entirely if
you are working alone; nothing above needs it.

### 1. Get a code

Privately, to the bot:

```
/team
```

It prints the steps and a **single-use code**, good for **15 minutes**. Run
`/team` again for a fresh one. You cannot lock yourself out.

### 2. Make the group

On your phone:

1. **New Message → New Group**.
2. Add *any* contact so Telegram lets you create it — you can remove them
   immediately afterwards. Name the group anything; `Acme dev` is fine.
3. Open the group → tap its name → **Edit** → turn **Topics** on.
   *If you do not see Topics, the group is not a supergroup yet. Adding a second
   member and enabling Topics converts it automatically.*
4. Still in **Edit** → **Administrators** → **Add Admin** → pick the bot.

Give it these four permissions and no others:

| Permission | Why |
|---|---|
| **Manage Topics** | it creates one topic per workspace |
| **Pin Messages** | the status card is pinned so it stays reachable |
| **Delete Messages** | it deletes an API key you paste by mistake |
| **Send Messages** | it has to answer you |

> **Keep the group private.** Anything an agent prints — file contents, diffs,
> stack traces — is posted here.

### 3. Link it

In the group you just made, send the code from step 1:

```
/setup a1B2c3D4e5F6
```

The bot proves it can really work here: it creates a throwaway topic, deletes
it, and only then binds the group. That is deliberate — Telegram sometimes
reports "can manage topics" on a group that then refuses, and a setup that says
*Ready* while `/new` fails is worse than an honest refusal.

You should see **Ready**. From then on `/new` in that group's **General** opens
the workspace's topic there.

**If it does not work:**

| Message | What to do |
|---|---|
| *That code is not valid, or has expired* | Send `/team` privately for a new one |
| *That code was issued to someone else* | The person who ran `/team` has to run `/setup`. Or get your own code. |
| *Setup blocked · forum topics are off* | Turn on **Topics** in the group's Edit screen |
| *Use a private supergroup with Topics enabled* | It is still a basic group; add a member and enable Topics |
| *…already bound to another tenant* | This group belongs to a different team. Use a new group. |
| Nothing at all | The bot is not an admin, or was never added |

---

## Working with other people

Everyone on the team shares one Conductor organisation and one key.

```
/invite 123456789          # their numeric Telegram id
/members                   # who is in, and their role
/remove 123456789
/leave                     # remove yourself
```

A group is not required for this. Someone you invite can drive the team from
their own private chat with the bot; the group only adds a topic list you all
watch together.

To find someone's Telegram id, have them message the bot `/start` — or use any
"what is my id" bot.

**Roles.** `owner` and `admin` are treated the same almost everywhere: both can
`/team`, `/key`, `/invite`, `/remove`, `/members` and `/health`. The one thing
only an owner can do is `/forget`, which deletes the team and everything in it.
An `admin` also cannot remove an owner. `member` can drive sessions and nothing
else.

A team always keeps at least one owner: the bot refuses the change that would
leave it with none, including an owner demoting themselves.

---

## If you are in more than one team

A group is unambiguous — each one belongs to exactly one team. Your **private
chat** with the bot is not, so tell it which one you mean:

```
/use acme          # this DM now means acme
/use               # list what you are in, and which is current
```

---

## Voice notes

Off unless the operator has enabled it *and* you have stored your own speech
key. Storing the key **is** the request to use it — there is no second switch:

```
/voicekey sk_xxxxxxxxxxxx     # privately, like /key
```

`/voice off` turns it back off, and `/voice on` afterwards turns it on again.

Then send a voice note in a topic and it becomes a prompt. To use voice for
*commands* rather than prompts, start with the word "command" ("command stop").

> Voice is the one feature whose data leaves this system: your audio goes to a
> speech vendor under **your** key, billed to you. The bot asks the vendor not
> to retain it, but zero retention is an enterprise-tier promise. If that
> matters to you, leave voice off.

---

## Your data

```
/privacy      # exactly what is stored and what leaves
/export       # download everything this team holds
/revoke       # delete your stored keys; polling stops
/forget       # delete the team and everything in it (two taps)
```

What is kept: your API key, encrypted; workspace, session and delivery
bookkeeping; transcript text, capped and **deleted after 30 days**.

`/revoke` deletes the key from the database *and* from memory immediately. Your
Conductor account is untouched — rotate the key at Conductor too if you think it
leaked.

---

## When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Bot says nothing to `/new` in a DM | You never finished sign-up | `/start`, then `/key` |
| *Topics unavailable here · one workspace at a time* | Telegram will not open a topic in this chat | Nothing to fix — the bot runs linearly. See [above](#when-your-private-chat-has-no-topics) |
| Bot ignores a group entirely | Group not linked, or you are not a member | `/setup` with a fresh code from `/team`, or ask an owner to `/invite` you |
| *Conductor rejected this team's API key* | Key revoked or expired at Conductor | `/key` privately with a new one |
| *This team is suspended* | The operator suspended your team | Contact whoever runs the instance |
| A button says it expired | Buttons last 15 minutes | Run the command again for fresh ones |
| Answers stop mid-turn | Usually a deleted topic | The rest is redirected to the chat root |
| *at its limit of N workspaces* | Quota | `/done` something first |
| *Sign-ups are busy right now* | Instance-wide rate limit | Try again in an hour |
| A whole group went silent after an upgrade | Telegram changed the group's id | Send `/setup <new code>` in it again |

`/health` (owners and admins) shows what the bot thinks is wrong, including whether
your key is working and how far behind delivery is.

---

## For operators

Running the instance rather than using it? See [`DEPLOY.md`](DEPLOY.md) for the
Railway setup, and [`../SECURITY.md`](../SECURITY.md) for the key-rotation and
breach runbooks.

Turn **Threaded Mode** on in @BotFather, then run
`scripts/probe_dm_topics.py` once against the live token — that is the only
way to know whether your users get a topic list in their private chats or the
linear fallback.

```
/platform list                 # every team and its state
/platform suspend acme         # stop it now
/platform resume acme
```

Gated on `PLATFORM_ADMIN_IDS`. An operator can *stop* a team and cannot *read*
one — nothing there returns customer data.
