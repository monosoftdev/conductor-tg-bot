# System overview

## What this is

A Telegram control plane for Conductor cloud workspaces and sessions. Telegram
is the mobile interface; Conductor remains the execution system.

**One bot, many workspaces.** Each brings its own Conductor API key, its own
supergroup and its own members. Isolation is enforced by PostgreSQL row-level
security — see [`TENANCY.md`](TENANCY.md), which is the document to read before
changing anything in this area.

- **General** is the cockpit: search history, inspect all work, or create a
  workspace. Ordinary text or audio never prompts an agent from General.
- **One forum topic maps to one workspace.** The topic's current session is the
  prompt destination, so routing is visible and predictable.
- **One pinned status card represents the live turn.** Progress edits that card
  instead of creating tool-noise bubbles.
- **Answers are mobile-first.** Every Telegram prompt asks the agent to lead
  with the outcome, stay concise, omit filler, and include only essentials.
  When a decision is required, the agent gives one recommendation and 2–4
  numbered choices; Telegram renders them as one-tap buttons.

## Fast operator loop

| Goal | Fastest action |
|---|---|
| Start work | In General: `/new [project:] prompt` |
| Resume laptop work | General: `/attach [name]`, then tap `+ Open` |
| Continue work | Open its topic and send text, voice, or audio |
| See current state | `/mode` — status, workspace, branch, model, queue, actions |
| See all work | `/board` — ten compact recent rows with topic jump buttons |
| Change session | `/s [search]` — active/recent sessions first |
| Stop | Tap **Stop** on the card or run `/stop` |
| Search history | General text or `/find text` |
| Finish | `/done`, then tap the named archive confirmation |

Replying to a bot message uses that message's session, even from General. This
is the escape hatch for continuing a specific result without opening its topic.

## Phone simulations

| Situation | Phone interaction | Guardrail |
|---|---|---|
| Start immediately | `/new project: prompt` | Creates one colored workspace topic and queues the prompt |
| Configure first | `/new`, then tap project/model/effort | One edited wizard card; state is retained for 30 minutes |
| Send a prompt | Type in its topic | The bot reacts 👀 instead of adding a receipt bubble |
| Prompt finishes | Read the answer; reaction becomes 👍 | Pinned card also becomes Done |
| Agent needs a decision | Tap one of 2–4 buttons | Recommended option is first, green, and checked |
| Open another workspace | General `/board` or `/s` | Deep-link button opens its topic; no binding is changed |
| Resume a laptop cloud workspace | `/attach name`, tap `+ Open`, then type in its new topic | Starts at the current transcript tail; history is not replayed |
| Change session | Topic `/s` | Only sessions from that workspace are offered |
| Run parallel approaches | `/fork name`, then `/s` | Sessions share the workspace topic without cross-workspace routing |
| Phone was locked | Tap the pinned-card control | Safe controls live for 15 minutes; destructive confirmation stays 60 seconds |
| Too many queued prompts | Tap Clear queue on the card | Goes through the turn machine; no direct row deletion |
| Long result | Tap the generated `.md`/`.diff` file | Telegram's native searchable preview replaces message floods |
| Send a screenshot/file | Bot says it was not forwarded | No false claim that Conductor saw unsupported binary content |
| Error or stall | Tap Check, Retry, Transcript, or Open | Error topic marker and loud alert remain visible |
| Finish work | Tap Archive, then named confirmation | Topic is marked archived and closed |

General never accepts an accidental ordinary prompt. `/s` in General is
navigation, and `/s` inside a topic cannot bind a session from another
workspace.

A workspace started in the Conductor app can be resumed from Telegram when it
is a **Conductor cloud workspace visible to the configured API account**.
Railway cannot reach a Mac-local-only process or files that were never pushed
to the cloud.

## Telegram-native interface

- Forum topics are the primary navigation. Names carry live state markers and
  each project/branch gets a stable Telegram topic color.
- Inline buttons use primary, success, and danger styles so the recommended,
  navigational, and destructive actions are visually distinct.
- Prompt reactions provide receipt and completion state without extra messages.
- The pinned status card, typing indicator, callback toasts, deep links, command
  menu, native voice notes, and document previews cover the daily interaction
  loop.
- `/notify` uses buttons for Loud, Quiet, and Off. Normal answers respect the
  topic setting; errors stay loud.
- Confirmation and wizard messages edit in place, so completed controls do not
  leave stale actionable keyboards behind.

Ephemeral messages are not used for status or decisions because Telegram does
not guarantee delivery when the user is offline and they may disappear after an
app restart. Rich-message draft streaming is also unnecessary here: Conductor
is polled, while the pinned card already provides a stable live surface.
Telegram checklists require a connected business account, so ordinary inline
choice buttons remain the portable decision control.

## Voice and audio

Telegram mic-button notes and uploaded audio files use the same durable input
path.

1. Authentication and route resolution happen before download.
2. Duration and the 20 MB ceiling are checked before download.
3. Audio is held in memory only and sent to the configured speech provider.
4. The transcript is parsed without translation or fuzzy command matching.
5. The “Transcribing…” acknowledgement is edited into the final result, keeping
   the chat to one bot bubble per recording.

Ordinary speech behaves like typed text. Commands require a wake phrase at the
start, such as “command stop” or “команда знайди SQLite”.

- `VOICE_MODE=shadow`: preview only.
- `VOICE_MODE=prompts`: ordinary prompts/search execute; commands preview.
- `VOICE_MODE=commands`: exact commands also execute.

Spoken `done` still creates a named confirmation and never archives directly.

## Runtime flow

```text
┌──────────────────── phone ────────────────────┐
│ Telegram app · General + workspace topics     │
└──────────────────────┬────────────────────────┘
                       │ Telegram Bot API
┌──────────────────────▼ Railway ───────────────┐
│ One Python process                            │
│ auth/routing → handlers → prompt/voice ledger │
│ supervisor → transcript cursor → outbox       │
│ health server                                 │
│                                               │
│ PostgreSQL · two roles · RLS · no volume      │
└──────────────┬───────────────────┬─────────────┘
               │ HTTPS             │ HTTPS
┌──────────────▼──────────┐  ┌─────▼────────────┐
│ Conductor cloud API    │  │ ElevenLabs STT   │
│ workspaces + sessions  │  │ only when voice  │
│ prompts + transcripts  │  │ is enabled       │
└─────────────────────────┘  └──────────────────┘
```

Six long-lived services share one structured runtime:

1. Telegram polling
2. Delivery outbox
3. Status cards
4. Voice workers
5. Session supervisor/pollers
6. Health server

Any unexpected critical-service exit stops the process cleanly so Railway can
restart the complete unit.

## Configuration and keys

All secrets are Railway environment variables; none belongs in Telegram,
the database, or the repository.

| Variable | Where used | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | The one shared bot | Yes |
| `DATABASE_URL` | The `ctb_app` role; row-level security applies | Yes |
| `SYSTEM_DATABASE_URL` | The `ctb_worker` role; BYPASSRLS, workers only | Yes |
| `CTB_MASTER_KEYS` | Seals every tenant's stored API key | Yes |
| `HEALTH_TOKEN` | Protects detailed public health output | Strongly recommended |
| `PLATFORM_ADMIN_IDS` | Platform commands; not an allow-list for using the bot | No |
| `REGISTRATION_OPEN` | Self-serve sign-up | No |
| `VOICE_ENABLED` | Platform kill switch; each tenant stores its own speech key | No |

Per-workspace settings — the Conductor key, agent/model defaults, voice mode,
quotas — live in the `tenants` table, not in the environment. Nothing in the
environment identifies a customer.

The bot does not call an LLM itself to write code. The selected Conductor
session owns its agent/model/effort settings. The bot is a durable control and
delivery layer; ElevenLabs is only the provisional speech-to-text model.

## Reliability model

- Prompt rows are written before HTTP and use stable Conductor `messageId`
  values. Ambiguous failures retry with the identical ID.
- Transcript delivery never depends on session status. The cursor drains on
  every tick and advances atomically with recorded messages and deliveries.
- Delivery claims and content hashes prevent overlapping workers from sending
  the same result twice.
- Voice jobs preserve the original route and operation ID across redeploys.
- Workspace creation uses a stable reconciliation nonce; session creation uses
  a caller-supplied ID.
- PostgreSQL holds all state; the container is stateless. Retention runs
  daily under the singleton lease. Backups are the database's own.

## Efficiency model

- Session polling cadence adapts from active seconds to idle minutes.
- One supervisor owns all session pollers under a singleton lease.
- Status-card edits coalesce and are capped at one per three seconds.
- Tool activity stays on the card; substantive transcript content becomes chat
  messages.
- Normal topics default to quiet notifications. The last-prompted topic gets a
  temporary focus window; errors remain loud.
- `/board`, `/s`, `/mode`, and `/help` are optimized for narrow phone screens
  and put active/current work first.
- `/attach name` directly finds cloud work started from the laptop; an
  idempotent one-tap control opens exactly one topic and preserves its current
  session.

## Safety boundaries

- Environment allowlist plus a DB allowlist protects every Telegram update.
- An optional group ID confines operation to one private supergroup while
  retaining allowlisted DMs.
- General has no direct ordinary-prompt path.
- Destructive actions use short-lived, single-use callback nonces.
- Every archive entry point, including status cards, requires a named second
  confirmation.
- Voice commands require exact wake phrases and aliases; fuzzy recognition
  cannot execute actions.
- `/sql` is owner-only and restricted to a single read-only `SELECT`.
- Secrets and transcript content are scrubbed from normal structured logs.

## Deployment boundary

Run exactly one Railway replica. There is no volume.
The final remaining validation is live: real Telegram permissions, real
Conductor credentials, real speech recordings, and redeploys during active
turns/transcription.
