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

Must match `BOT_COMMANDS` in `src/ctb/bot/app.py` — that list is what the code
registers on boot, and Telegram shows whichever was set last.

## Settings that are not copy

| Setting | Value | Why |
|---|---|---|
| Group Privacy | **disabled** | The bot must read plain text in topics, not only commands. |
| Threaded Mode | **enabled** | This is what lets a topic exist in a private chat. Without it every DM falls back to one workspace at a time. |
| Allow Groups | **enabled** | The optional `/team` flow needs it. |
| Inline mode | disabled | Nothing uses it. |
