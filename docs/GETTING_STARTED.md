# Getting started

Drive your [Conductor](https://conductor.build) coding agents from Telegram.
This is the whole setup, once, from a phone. It takes about ten minutes and the
slowest part is Telegram's own settings screens.

**What you need before you start**

- A Telegram account.
- A Conductor account with at least one project.
- Nothing else. No laptop, no terminal, no credit card for this bot.

**What it will cost you.** Conductor bills your own API key for the agents you
run. This bot adds nothing.

---

> **A note on words.** Conductor calls a checkout-plus-agent a *workspace*, and
> `/new` creates those all day. The thing you create below is a **team**: it
> owns one Conductor API key, the people allowed to use it, and one Telegram
> group. One team, many workspaces.

## Step 1 — Say hello to the bot

Open Telegram, search for the bot's handle (whoever runs the instance will have
given it to you), and press **Start**.

Send:

```
/register acme
```

Replace `acme` with a short name for your workspace. Letters, numbers and
hyphens; anything else is turned into a hyphen. It appears in `/members` and in
logs, so keep it boring.

The bot asks for your Conductor key next — everything private happens here,
before you touch a group.

---

## Step 2 — Give it your keys

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

Now the bot hands you a **setup code**, good for **15 minutes**, once.

> **Ran out of time?** Send `/register` again for a fresh one. You cannot lock
> yourself out.

---

## Step 3 — Make a group for your work

Everything happens in one Telegram group, and each of your Conductor workspaces
gets its own **topic** inside it. That is what lets you have five things running
at once without losing track.

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

---

## Step 4 — Link the group

In the group you just made, send the code from step 1:

```
/setup a1B2c3D4e5F6
```

The bot proves it can really work here: it creates a throwaway topic, deletes
it, and only then binds the group. That is deliberate — Telegram sometimes
reports "can manage topics" on a group that then refuses, and a setup that says
*Ready* while `/new` fails is worse than an honest refusal.

You should see **Ready**.

**If it does not work:**

| Message | What to do |
|---|---|
| *That code is not valid, or has expired* | Send `/register` privately for a new one |
| *That code was issued to someone else* | The person who ran `/register` has to run `/setup`. Or get your own code. |
| *No topic permission (forum topics are off)* | Turn on **Topics** in the group's Edit screen |
| *…already bound to another tenant* | This group belongs to a different workspace. Use a new group. |
| Nothing at all | The bot is not an admin, or was never added |

---

## Step 5 — Run something

Back in the group, in the **General** topic:

```
/new fix the flaky test in checkout_test.py
```

The bot creates a Conductor workspace, opens a topic for it, and posts a pinned
status card that updates as the agent works. The answer arrives in that topic.

From then on, **just type in the topic** to continue that session. No command
needed.

---

## The daily loop

Everything below happens in the group.

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
| Search your transcripts | `/find some text`, or just type in General |
| Finish and archive | `/done`, then confirm by name |

`/help` prints a short version of this in the chat.

---

## Working with other people

Everyone in the group shares one Conductor organisation and one key.

```
/invite 123456789          # their numeric Telegram id
/members                   # who is in, and their role
/remove 123456789
/leave                     # remove yourself
```

To find someone's Telegram id, have them message the bot `/start` — or use any
"what is my id" bot.

**Roles.** `owner` can do everything. `admin` can invite and remove members but
cannot remove an owner or delete the workspace. `member` can drive sessions and
nothing else.

A workspace always keeps at least one owner: the bot refuses the change that
would leave it with none, including an owner demoting themselves.

---

## If you are in more than one workspace

Group topics are unambiguous — each group belongs to exactly one workspace. Your
**private chat** with the bot is not, so tell it which one you mean:

```
/use acme          # this DM now means acme
/use               # list what you are in, and which is current
```

---

## Voice notes

Off unless the operator has enabled it *and* you have stored your own speech
key:

```
/voicekey sk_xxxxxxxxxxxx     # privately, like /key
/voice on
```

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
/export       # download everything this workspace holds
/revoke       # delete your stored keys; polling stops
/forget       # delete the workspace and everything in it (two taps)
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
| Bot ignores the group entirely | Group not linked, or you are not a member | `/setup` with a fresh code, or ask an owner to `/invite` you |
| *Conductor rejected this workspace's API key* | Key revoked or expired at Conductor | `/key` privately with a new one |
| *This workspace is suspended* | The operator suspended it | Contact whoever runs the instance |
| A button says it expired | Buttons last 15 minutes | Run the command again for fresh ones |
| Answers stop mid-turn | Usually a deleted topic | The rest is redirected to **General** |
| *at its limit of N workspaces* | Quota | `/done` something first |
| *Sign-ups are busy right now* | Instance-wide rate limit | Try again in an hour |
| Whole group went silent after an upgrade | Telegram changed the group's id | Send `/setup <new code>` in it again |

`/health` (owners only) shows what the bot thinks is wrong, including whether
your key is working and how far behind delivery is.

---

## For operators

Running the instance rather than using it? See [`DEPLOY.md`](DEPLOY.md) for the
Railway setup, and [`../SECURITY.md`](../SECURITY.md) for the key-rotation and
breach runbooks.

```
/platform list                 # every workspace and its state
/platform suspend acme         # stop it now
/platform resume acme
```

Gated on `PLATFORM_ADMIN_IDS`. An operator can *stop* a workspace and cannot
*read* one — nothing there returns customer data.
