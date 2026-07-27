# conductor-tg-bot

Drive Conductor cloud agents from Telegram. Each workspace gets a forum topic:
send a prompt, watch one compact status card, and receive the answer on your
phone.

## Status

Ready for a first Railway deployment and live Telegram testing.

- Python 3.13, aiogram 3, httpx, SQLite/WAL
- Telegram long polling; no public webhook
- One Railway replica with a persistent `/data` volume
- Durable prompt idempotency, transcript cursor, delivery outbox, and
  singleton poller lease
- New-workspace prompts are held durably until the workspace is ready
- Transient Telegram failures retry without a terminal attempt cap
- Defensive HTML rendering and UTF-16-safe Telegram chunking
- Daily, power, and owner command surfaces
- Durable Telegram voice-note and audio transcription with replay-safe actions
- Mobile reply guidance automatically added to every Telegram prompt

The transcript cursor is the source of truth. Session status only changes poll
cadence and progress UX; it never gates delivery.

## Telegram setup

1. Create a bot with `@BotFather`.
2. Create a private supergroup and enable Forum Topics.
3. Add the bot as an administrator with permission to manage topics, pin
   messages, delete messages, and send messages/documents.
4. Add only trusted Telegram user IDs to
   `ALLOWED_TELEGRAM_USER_IDS`. The first ID is the owner. This is the security
   boundary.
5. Start the bot and run `/setup` in the group. Wait for
   `Ready · General is search-only; /new creates topics.` before the first
   `/new` — `/new` creates the Conductor workspace before the topic, so a
   missing Manage Topics permission strands a live cloud workspace per attempt.
6. Leave `TELEGRAM_CHAT_ID` unset at first. It is a second fence on top of the
   allowlist, `/setup` does not write it, nothing in the bot prints a chat id,
   and a wrong value silently drops every group update. Add it later from the
   Telegram web client URL if you want it.
7. Keep `General` as the cockpit. Plain text there searches; it never becomes a
   prompt without an explicit button tap.

DMs work as a degraded, single-session fallback.

## Daily control loop

Use General as the cockpit and workspace topics as focused control rooms:

1. `/new [project:] prompt` starts work and creates its topic.
2. Send text, voice, or audio in that topic to continue.
3. `/mode` shows the current session, branch, model, queue, and safe actions.
4. `/board` gives a compact cross-workspace view; `/s` switches sessions.
5. A Conductor cloud workspace created on the laptop shows up there as
   `+ Open <name>`; `/attach name` finds it directly. Tapping opens a topic,
   binds the newest session, and posts the last exchange as a read-only
   snapshot; the cursor starts at the end, so nothing is replayed. A
   Mac-local-only workspace is outside Railway's reach.
6. Stop from the pinned card or `/stop`; finish with `/done` plus confirmation.

The pinned status card absorbs progress/tool noise. Final answers stay concise,
outcome-first, and easy to scan on a phone. Prompts use 👀/👍 reactions instead
of receipt bubbles, and agent decisions become one-tap numbered buttons with
the recommended choice first.

## Local development

Python 3.13 is required.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

Set the three required values in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=...
CONDUCTOR_API_KEY=...
ALLOWED_TELEGRAM_USER_IDS=123456789
```

Then run:

```bash
.venv/bin/python -m ctb
```

### Voice and audio

The bot accepts Telegram mic-button voice notes and uploaded audio messages.
Audio stays in memory only; the durable job stores the transcript and its
snapshotted topic/session route.

Enable ordinary spoken prompts with:

```dotenv
VOICE_ENABLED=true
ELEVENLABS_API_KEY=...
VOICE_MODE=prompts
```

Modes:

- `shadow` — transcribe and preview; execute nothing.
- `prompts` — submit ordinary topic/DM speech; General remains search-only.
  Spoken commands are preview-only.
- `commands` — also execute exact wake-phrase commands such as
  “command stop” or “команда знайди SQLite”. `/done` still requires the named
  confirmation button. There is no fuzzy command matching.

The defaults cap audio at 180 seconds and Telegram's 20 MB bot-download limit.
Keep `VOICE_LANGUAGE=auto` to preserve multilingual and code-switched speech.

Health is served on `PORT` (default `8080`):

```bash
curl http://127.0.0.1:8080/health
```

## Railway deployment

### Before your first deploy

1. **Set `RAILWAY_RUN_UID=0`.** The image runs as UID 10001 and Railway mounts
   a fresh *root-owned* volume over `/data`, so without this SQLite cannot open
   the database and the service crash-loops. The container now fails with
   `FATAL: cannot write /data …` instead of a bare `OperationalError`, but the
   variable is the fix. A local `docker run` cannot reproduce this — Docker
   seeds a named volume from image content, Railway does not.
2. **Mount the volume at exactly `/data`.** Any other path silently writes to
   ephemeral container disk and loses the cursors on every deploy.
3. **Set all three required variables in one go** — the bot reports them
   together, so a partial set just crashes again:
   `TELEGRAM_BOT_TOKEN`, `CONDUCTOR_API_KEY`, `ALLOWED_TELEGRAM_USER_IDS`.
4. **Verify the token first:** `curl "https://api.telegram.org/bot<TOKEN>/getMe"`.
   A rejected token now fails the deploy loudly rather than retrying forever.
5. **Set `HEALTH_TOKEN`** to a random string if you generate a public Railway
   domain. Without it the detailed `/health` body is served to loopback only,
   so a public domain sees the status summary and nothing identifying.
6. Optional: `TELEGRAM_CHAT_ID` (leave unset at first), and for voice/audio
   `VOICE_ENABLED=true` plus `ELEVENLABS_API_KEY`.

### Deploy

1. Create a Railway service from this private repository.
2. Deploy with the checked-in `Dockerfile` and `railway.toml`.
3. Confirm `/health` returns `{"status":"ok","ok":true,...}`.
4. In Telegram, run `/setup`, then `/new <prompt>`, then `/health` — expect
   `circuit closed` and `0 overdue`.

Keep exactly one replica. SQLite, the volume, Telegram `getUpdates`, and the
singleton lease all assume a single active service. `overlapSeconds=0` stops
the old deployment before the new poller starts.

## Commands

Daily:

- `/new [project:] prompt`
- `/attach [name]`
- `/board`
- `/stop`
- `/find text`
- `/mode`
- `/done`

Power:

- `/s`, `/fork`, `/name`, `/open`, `/desk`, `/log`
- `/notify`, `/defaults`, `/sql`, `/tidy`, `/setup`

Owner:

- `/allow`, `/deny`, `/health`, `/backup`

## Quality gates

```bash
.venv/bin/ruff format --check src scripts tests
.venv/bin/ruff check src scripts tests
.venv/bin/pyright
.venv/bin/pytest -q
docker build -t conductor-tg-bot:local .
```

The local production-image smoke test verifies migrations, lease acquisition,
all six long-lived services, and `/health`. It cannot verify volume
permissions: Docker seeds a named volume from the image (ownership included),
while Railway mounts an empty root-owned one. That is what `RAILWAY_RUN_UID=0`
is for.

## Live acceptance checklist

Run these from a phone after the first deploy:

1. `/new <prompt>` creates a topic and delivers one answer.
2. `/stop` mid-turn reports stopped state and dropped queued prompts.
3. Redeploy mid-turn; the answer arrives without a lost transcript message.
4. Replace the Conductor key with a bad value; pollers stop without a retry
   storm and `/health` reports the auth failure.
5. Run three workspaces concurrently; answers stay in their own topics.
6. `/find` returns a known historical phrase.
7. `/done` archives the workspace and closes its topic.
8. Send a voice note in a topic; its prompt reaches that topic's session once.
9. Send audio in General; it searches and never submits a prompt.
10. With `VOICE_MODE=commands`, “command stop” stops the current session and
    “command done” only shows the archive confirmation.
11. Create a workspace on the laptop, then tap `+ Open <name>` in `/board`: one
    topic appears, the snapshot card shows the last exchange, and the next line
    typed there reaches that session.

The renderer corpus currently contains probe-verified simple turns plus clearly
labelled synthetic tool/diff/error shapes. Expand it with curated real
tool-heavy transcripts after the account has such sessions.

Full architecture and verification details are in
[`docs/PLAN.md`](docs/PLAN.md). Probe findings are in
[`docs/HANDOFF.md`](docs/HANDOFF.md). The final operator and architecture map is
in [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md). The failure contract
and fault matrix are in
[`docs/RELIABILITY_AUDIT.md`](docs/RELIABILITY_AUDIT.md).
