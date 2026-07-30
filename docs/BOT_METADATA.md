# BotFather copy

The store listing is the one piece of user-facing text that does not live in
the code, so it drifts silently. This is the source of truth for it; paste it
into [@BotFather](https://t.me/BotFather) after any change to onboarding.

## `/setname`

```
Conductor
```

## `/setdescription` — the empty-chat splash, before anyone taps Start

```
Drive your Conductor cloud coding agents from your phone.

Send a prompt, get the answer. Each workspace becomes its own topic, so a
dozen agents running at once still fit on one screen — and the topic list
tells you which are working, which finished, and which need you.

Bring your own Conductor API key. Your key, your workspaces, your spend.

Tap Start, then send /key.
```

## `/setabouttext` — the profile blurb, 120 characters

```
Run Conductor cloud coding agents from Telegram. One topic per workspace. Bring your own API key.
```

## `/setcommands`

The bot registers this list itself on every boot, so BotFather only needs it if
you want the menu to be right before the first deploy. Telegram shows whichever
was set last, which is why the two must agree.

```
new - New workspace
board - Live sessions
attach - Open laptop workspace
home - Show the launcher buttons
stop - Stop this turn
find - Search transcripts
mode - Current session & controls
s - Switch session here
fork - New session here
notify - Topic alerts
done - Archive workspace
setup - Link this group to your team
invite - Add someone to this team
use - Pick which team your DMs mean
health - Team status
register - Create your team
key - Store your Conductor API key
voicekey - Store your speech API key
gitkey - Store your GitHub CI token
help - Quick control guide
```

Regenerate it after any change to `BOT_COMMANDS` in `src/ctb/bot/app.py`:

```bash
.venv/bin/python -c "from ctb.bot.app import BOT_COMMANDS
for c in BOT_COMMANDS: print(f'{c.command} - {c.description}')"
```

Twenty of the forty commands, on purpose. Destructive ones (`/forget`,
`/revoke`, `/tidy`) and operator-only ones (`/platform`) stay out of the menu:
advertising them to every customer costs more than it teaches. They all still
work, and `/help` lists the ones a person needs.

## Settings that are not copy

| Setting | Value | Why |
|---|---|---|
| Group Privacy | **disabled** | The bot must read plain text in topics, not only commands. |
| Threaded Mode | **enabled** | This is what lets a topic exist in a private chat. Without it every DM falls back to one workspace at a time. |
| Allow Groups | **enabled** | The optional `/team` flow needs it. |
| Inline mode | disabled | Nothing uses it. |
