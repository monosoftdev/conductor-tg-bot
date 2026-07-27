# Telegram Voice Control Plan

Research resumed and refreshed on 2026-07-25. The durable voice pipeline
described here is now implemented. Provider quality and automatic spoken
commands remain rollout-gated until the owner's real-recording benchmark is
complete.

## Recommendation

Add voice as another durable Telegram input path:

1. Receive a Telegram voice note and preserve its already-resolved topic/session route.
2. Transcribe it in its original language.
3. Interpret a command only when the transcript starts with an explicit wake phrase such as
   `command` or `команда`.
4. Send every other transcript through the same path as typed text.
5. Reuse the existing confirmation rule for destructive actions.

Use **ElevenLabs Scribe v2** as the provisional first provider. It is the best fit for this bot,
pending a small benchmark using the owner's real voice:

- It accepts Telegram's OGG/Opus voice-note format directly, so the current slim Railway image does
  not need FFmpeg.
- It automatically handles multiple languages in one recording.
- It supports Russian and Ukrainian, and its published per-language table puts both in the
  `<=5% WER` group.
- Up to 1,000 keyterms can bias recognition toward repository, branch, model, and command names.
- Current API pricing is $0.22/audio hour plus 20% when keyterms are used. At that rate a
  15-second keyed voice note is roughly $0.0011.

Treat this as a provisional choice, not a universal accuracy claim. Vendor benchmarks do not
represent this user's accent, microphone, code-switching, or repository names. Before enabling
automatic execution, compare Scribe v2 against `gpt-4o-transcribe` and one efficient Whisper
endpoint on a private evaluation set described below.

Do **not** use a realtime speech-to-speech or voice-agent API. Telegram has already completed and
compressed the recording before the bot receives it. A batch transcription request is simpler,
cheaper, easier to retry, and gives the bot an audit transcript.

## Product behavior

### Default: a voice note is the equivalent of typed text

Inside a bound workspace topic:

> “Check why the cursor test is flaky and fix it.”

becomes a normal Conductor prompt in that topic. The bot replies with a compact audit echo:

```text
🎙 Check why the cursor test is flaky and fix it.
→ api/fix-cursor · queued
```

In `General`, the transcript follows the existing text rule: search first and offer the explicit
“Send to last session” button. Voice must not create a new General-to-session routing shortcut.

Reply-to routing also remains unchanged. The routing middleware resolves the destination before the
voice handler runs, so a voice reply can use the same reply override as typed text.

### Explicit commands

A command is recognized only when the normalized transcript begins with a configured wake phrase.
Initial wake phrases:

- `command`
- `команда`
- `slash`

Examples:

| Spoken text | Canonical operation |
|---|---|
| “Command board” | `/board` |
| “Команда стоп” | `/stop` in the resolved topic |
| “Command find cursor replay” | `/find cursor replay` |
| “Command new fix the flaky payment test” | `/new fix the flaky payment test` |
| “Command done” | `/done`, which still shows the named archive confirmation |
| “Please stop after the tests” | ordinary agent prompt, **not** `/stop` |

The verb aliases should live in data, not inside handler conditionals. Start with English,
Ukrainian, and Russian aliases for the six daily commands. Add power commands only after the daily
set is proven.

No fuzzy match may turn speech into a command. A fuzzy or unknown verb after a wake phrase produces
a preview with “I heard …” and buttons to retry or cancel. Fuzzy correction is acceptable for
keyterms inside an ordinary prompt, but not for selecting an action.

### Translation and transliteration

Do not translate or transliterate the entire prompt by default. Preserve the language the user
spoke; coding agents can receive multilingual prompts, and translation can damage identifiers.

The normalization layer may:

- normalize Unicode and whitespace;
- map only the command head to a canonical command;
- map spoken separators such as “slash” or “dash” inside fields that are known to be a branch or
  model name;
- preserve English technical words inside Ukrainian/Russian speech;
- retain the raw provider transcript alongside the normalized command preview.

For example, spoken `команда знайди conductor cursor` becomes the canonical intent
`find("conductor cursor")`; it does not require translating the whole utterance to English.

## Safety contract

Voice must not weaken the existing Telegram safety rules.

- Authentication and route resolution run before any audio download.
- Only an explicit wake phrase can invoke a command.
- `/done` always uses the existing named confirmation keyboard.
- `/stop` remains immediate because that is already the typed-command contract, but requires an
  exact wake phrase and exact verb alias.
- General remains search-only.
- No command is executed when transcription fails, times out, is empty, or is ambiguous.
- Never construct and redispatch a fake Telegram update. Voice and text handlers call shared
  application operations.
- Never automatically fail over to a second cloud speech provider. That would send the user's
  audio to another company without an explicit configuration choice.
- Provider retries must not cause duplicate Conductor actions. Persist the Telegram message and a
  stable operation ID before transcription or dispatch.

## Architecture

```text
Telegram voice update
  -> existing auth middleware
  -> existing (chat_id, thread_id, reply-to) routing
  -> persist voice_input keyed by (chat_id, Telegram message_id)
  -> bounded voice worker
       -> validate duration and file size
       -> download OGG/Opus bytes into memory
       -> build route-aware technical keyterms
       -> speech provider -> raw transcript + language + word confidence
       -> strict normalizer/parser
          -> ordinary text -> existing prompt/search operation
          -> explicit command -> existing command operation/confirmation
          -> ambiguous -> preview only
  -> audit echo through the existing Telegram send path
```

Keep this pipeline independent of the Conductor transcript poller. Audio ingestion creates either a
normal durable `outbound_prompts` row or a normal command action; it does not change cursor,
delivery, or turn-state semantics.

## Repository integration

Suggested files:

```text
src/ctb/
  voice/
    models.py                    VoiceTranscript, VoiceIntent, VoiceInputState
    provider.py                  SpeechProvider protocol
    glossary.py                  route-aware keyterm builder
    commands.py                  pure wake-phrase and command parser
    service.py                   durable orchestration
    providers/
      elevenlabs.py              first provider, using the existing httpx stack
  bot/handlers/voice.py          F.voice entry point and callbacks
  db/repo/voice_inputs.py        dedup, claim, retry, completion
  db/migrations/002_voice.sql
scripts/
  eval_voice.py                  provider comparison over a local private corpus
tests/
  test_voice_commands.py
  test_voice_provider.py
  test_voice_pipeline.py
```

The existing code already supplies most of the seams:

- `ctb.bot.middleware.routing.Route` provides the exact topic/session destination.
- `ctb.bot.handlers.common.submit_prompt()` supplies crash-safe Conductor prompt submission.
- `ctb.bot.handlers.prompts` defines General/topic behavior that voice should share.
- `ctb.bot.handlers.core` contains the six daily operations and archive confirmation.
- `ctb.bot.app` discovers independent routers, so `voice.py` does not require application rewiring.
- `ctb.logging` already has transcript-content scrubbing rules that should be extended to speech
  provider keys and voice transcript fields.

Before adding voice, extract small application-service functions from handlers where necessary.
Both typed and voice inputs should call those functions. Do not make the voice handler invoke the
typed handler or manufacture a `Message.text` value.

### Provider interface

Keep the interface deliberately small:

```python
class SpeechProvider(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        mime_type: str,
        keyterms: Sequence[str],
        language: str | None,
    ) -> VoiceTranscript: ...
```

`VoiceTranscript` should contain:

- `text`
- provider/model identifiers
- detected language and language probability when supplied
- word tokens with log probabilities when supplied
- audio duration
- provider request ID

Do not pretend every provider exposes comparable confidence. The command parser's main safety
signal is exact grammar, not a provider-specific global confidence number.

### Route-aware glossary

Build keyterms for each note rather than maintaining one large static prompt:

1. `Conductor` and the six command names/aliases.
2. The current workspace, project, branch, and session name.
3. Known agent/model/effort names from the live Conductor model registry.
4. A bounded set of recently used project and workspace names.
5. Stable engineering terms used by this repo: `Telegram`, `aiogram`, `Railway`, `SQLite`,
   `sessionIndex`, `messageId`, `Pyright`, `Ruff`, and `pytest`.

Deduplicate case-insensitively and prefer route-local terms. Keep the builder provider-neutral;
the provider adapter converts the list to Scribe keyterms, a Deepgram `keyterm` list, or an OpenAI
context prompt.

For Scribe v2, leave `language_code` unset to retain smart multi-language behavior. Disable audio
event tagging and diarization because a Telegram voice note is a single-speaker command. Do not
enable “no verbatim” initially: false starts are safer to expose in the audit transcript than to
silently rewrite.

## Persistence and recovery

Add a `voice_inputs` table. A minimal schema:

```sql
CREATE TABLE voice_inputs (
    chat_id           INTEGER NOT NULL,
    tg_message_id     INTEGER NOT NULL,
    thread_id         INTEGER NOT NULL DEFAULT 0,
    user_id           INTEGER NOT NULL,
    file_id           TEXT NOT NULL,
    file_unique_id    TEXT,
    duration_seconds  INTEGER,
    file_size         INTEGER,
    route_session_id  TEXT,
    route_workspace_id TEXT,
    provider          TEXT,
    model             TEXT,
    state             TEXT NOT NULL,
    transcript        TEXT,
    language          TEXT,
    intent_json       TEXT,
    action_id         TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    completed_at      INTEGER,
    PRIMARY KEY (chat_id, tg_message_id)
);
```

States:

```text
received -> transcribing -> transcribed -> dispatching -> completed
                    \-> failed
                              ambiguous -> waiting_for_user
```

Important invariants:

- The primary key deduplicates replayed Telegram updates.
- Persist the resolved route snapshot with the input. A topic being rebound while transcription is
  running must not silently move the command.
- Allocate and persist `action_id` before dispatch. For a prompt it is also the
  `outbound_prompts.message_id` sent to Conductor, so a crash retry reuses the same idempotency key.
- Extend `submit_prompt()`/`prompts_repo.create()` to accept an optional caller-supplied
  `message_id`; do not let a resumed voice job mint a new UUID.
- For a spoken `new`, derive the workspace-create nonce from the persisted operation ID and pass it
  into the shared create service. A retry must reconcile the same generated workspace name, not
  generate a new nonce and create a second workspace.
- Read-only commands may safely repeat. `/done` only creates a confirmation ticket. `/stop` should
  persist completion, even though repeating cancel is expected to be harmless.
- Claim rows conditionally so only one voice worker transcribes a message.
- On boot, requeue stale `received` and `transcribing` rows. Re-transcription may incur duplicate
  provider billing after an ambiguous network failure, but the action remains exactly once.
- Do not store audio. Hold bytes in memory only for the request.
- Cap stored transcript length and prune completed voice rows after seven days. The durable
  Conductor prompt ledger remains the long-term record for submitted prompts.

The handler should persist quickly and return work to a bounded worker owned by the application's
main task group. Do not create untracked background tasks from the handler.

## Configuration

Add settings only when implementation starts:

```dotenv
VOICE_ENABLED=false
VOICE_STT_PROVIDER=elevenlabs
VOICE_STT_MODEL=scribe_v2
VOICE_MAX_DURATION_SECONDS=180
VOICE_MAX_CONCURRENT=2
VOICE_LANGUAGE=auto
VOICE_WAKE_PHRASES=command,команда,slash
VOICE_COMPLETED_RETENTION_DAYS=7
ELEVENLABS_API_KEY=
```

Rules:

- The speech API key is required only when `VOICE_ENABLED=true`.
- Add it to `Settings.secret_values()` so the current structured-log scrubber removes it.
- Do not log audio or transcript text unless the existing content-logging debug flag explicitly
  permits it.
- Reject oversize notes before downloading when Telegram supplies `file_size`.
- Enforce duration independently of file size. A highly compressed note can be small but long.
- Default to 180 seconds; make it configurable rather than silently truncating.

Telegram currently allows bots to download files only up to 20 MB, so that is a hard ceiling even
if a speech provider accepts larger files.

## Provider comparison

| Option | Strengths for this bot | Costs/limitations | Plan role |
|---|---|---|---|
| **ElevenLabs Scribe v2** | Direct OGG/Opus; smart multi-language; 90+ languages; up to 1,000 keyterms; published strong Russian/Ukrainian results | Vendor benchmark needs local validation; keyterms add 20%; zero-retention is enterprise-only | Provisional default |
| **OpenAI `gpt-4o-transcribe`** | OpenAI documents better WER/language recognition than original Whisper; accepts a rich context prompt; logprobs available | Current file guide does not list OGG, so Telegram notes need safe transcoding; token-metered price is less intuitive | Accuracy challenger in eval |
| **Groq `whisper-large-v3`** | Direct OGG; multilingual; 189x published realtime factor; context prompt; $0.111/audio hour | Whisper vocabulary prompt is limited; lower domain adaptation than Scribe keyterms; 10-second minimum billing | Efficient/high-accuracy challenger |
| **Groq `whisper-large-v3-turbo`** | Direct OGG; very fast; $0.04/audio hour | Groq reports higher WER than its full large-v3; no translation endpoint support | Cheapest cloud fallback |
| **Deepgram Nova-3** | Direct prerecorded API; route-specific keyterm prompting; per-second billing; strong noisy/far-field focus | Multilingual auto mode currently covers a smaller language set than Scribe; Ukrainian is supported as a fixed language but is not listed in Nova-3 `multi` | Candidate if latency/noise wins |
| **Local faster-whisper / whisper.cpp** | Audio never leaves infrastructure; no per-note provider fee | Large model/image, Railway memory and cold starts, model distribution, CPU latency, operational burden | Privacy-driven later option, not v1 |

`gpt-4o-mini-transcribe` can be included as a cost control, but short voice notes make absolute
speech cost tiny. Optimize for command and identifier accuracy first.

Do not select a general audio-understanding LLM that returns an action directly. A cascaded
`audio -> transcript -> strict intent` design provides an audit trail, provider portability,
deterministic safety, and much better failure diagnosis.

## Private evaluation before auto-execution

Create a gitignored corpus under `.context/voice-eval/`. Voice is biometric data and the clips may
contain project names or source-code details; do not commit them.

Record 60-100 clips from the actual owner and actual phone:

- each daily command in English and the languages normally spoken;
- normal prompts containing words such as “stop”, “done”, and “archive” that must **not** execute;
- project, repository, branch, model, and tool names;
- mixed-language sentences with English identifiers;
- quiet room, outdoors, car noise, AirPods, and phone microphone;
- short corrections and false starts;
- silence, accidental taps, and music as negative cases.

`scripts/eval_voice.py` should call each candidate through the same provider protocol and produce:

- exact command-intent accuracy;
- command false-positive rate;
- exact argument match;
- critical-token error rate for identifiers;
- WER/CER as a secondary metric;
- p50/p95 end-to-end latency;
- actual provider charge or calculated audio duration;
- failures and blank/hallucinated transcripts.

Selection gates:

- zero destructive-command false positives in the corpus;
- 100% recognition of the explicit wake phrase and daily command intent, or manual review for the
  failing shape;
- at least 98% critical-token accuracy after route-aware keyterms;
- p95 completed-note latency below five seconds for a 30-second voice note;
- no silent action on provider failure.

Start deployment in shadow mode: transcribe and show the canonical intent, but do not execute it.
After at least 30 real notes with no unsafe interpretation, enable explicit commands. Then enable
automatic prompt submission. Keep a configuration switch that returns to shadow mode without code
changes.

## Implementation sequence

### Milestone 1 — pure parsing and benchmark

- Add `VoiceTranscript` and `VoiceIntent`.
- Implement Unicode normalization, exact wake-phrase parsing, aliases, and command arguments as pure
  functions.
- Add adversarial parser tests before any provider call.
- Implement the provider protocol and evaluation script.
- Record the private corpus and select the provider/model.

Exit: chosen model meets the evaluation gates, and the parser has zero command false positives.

### Milestone 2 — shadow transcription

- Add settings and secret scrubbing.
- Add `voice_inputs`, repository methods, conditional claims, and pruning.
- Add the `F.voice` handler and bounded worker.
- Download authenticated, size/duration-approved notes into memory.
- Transcribe with route-aware keyterms.
- Reply with transcript + inferred action, but execute nothing.
- Add provider timeout, rate-limit, malformed-response, and restart tests.

Exit: 30 real voice notes are transcribed and recovered across a test redeploy with no duplicate
processing visible to the user.

### Milestone 3 — shared dispatch

- Refactor typed handlers into reusable prompt and command operations.
- Add caller-supplied operation IDs to prompt and workspace-create operations before enabling
  dispatch.
- Dispatch ordinary topic transcripts through `submit_prompt()`.
- Preserve General search-first behavior.
- Enable the six explicit commands; retain the `/done` confirmation.
- Store the resulting prompt/action link in `voice_inputs.action_id`.
- Add audit echoes and Retry/Cancel controls for ambiguous results.

Exit: every voice path produces the same database and Conductor effects as its typed equivalent.

### Milestone 4 — production hardening

- Enable automatic prompts after the shadow gate.
- Add health counters: pending voice inputs, transcription latency, provider errors, ambiguous
  intents, and command false-positive reports.
- Add a provider circuit breaker independent from the Conductor circuit.
- Add manual provider fallback only if operational experience justifies it.
- Re-run the corpus when changing model, provider, command aliases, or keyterm strategy.

Exit: live phone tests pass through a Railway redeploy and provider outage without lost or duplicate
Conductor actions.

## Tests that must exist

Pure parser:

- wake phrase case, punctuation, Unicode normalization, and aliases;
- ordinary “please stop after tests” remains a prompt;
- wake phrase with unknown/fuzzy verb remains ambiguous;
- `/done` returns a confirmation intent, never a direct archive action;
- mixed Cyrillic/Latin identifiers remain intact;
- spoken separators are normalized only in typed fields.

Pipeline:

- unauthorized update never downloads a file;
- General cannot directly submit an ordinary voice prompt;
- topic/reply route snapshot is preserved;
- replayed Telegram update creates one `voice_inputs` row;
- crash before and after transcription reuses one operation ID and results in at most one
  Conductor prompt;
- crash during spoken `/new` reconciles one workspace name and never creates a second workspace;
- provider timeout/error causes no action and exposes Retry;
- oversize and over-duration notes cause no download/action;
- empty, low-information, and hallucinated output cause no action;
- transcript text and provider key never appear in normal logs;
- `/done` still requires the existing nonce confirmation;
- an ordinary prompt containing a command word cannot invoke the command.

Live:

1. Voice prompt in a topic reaches the correct session once.
2. Voice reply uses the reply-to route override.
3. Voice note in General searches and does not directly prompt.
4. “Command stop” cancels the correct session.
5. “Command done” shows the named confirmation and does not archive before the tap.
6. Redeploy during transcription resumes without a duplicate action.
7. Remove the speech key: the bot remains healthy, typed commands still work, and voice exposes one
   clear configuration error.

## Primary sources

- [Telegram Bot API: File and getFile limits](https://core.telegram.org/bots/api#getfile)
- [ElevenLabs Scribe v2 transcription documentation](https://elevenlabs.io/docs/overview/capabilities/speech-to-text/)
- [ElevenLabs speech-to-text API reference](https://elevenlabs.io/docs/api-reference/speech-to-text/convert)
- [ElevenLabs API pricing](https://elevenlabs.io/pricing/api?price.section=speech_to_text)
- [OpenAI speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text)
- [OpenAI GPT-4o Transcribe model page](https://developers.openai.com/api/docs/models/gpt-4o-transcribe)
- [Groq speech-to-text documentation](https://console.groq.com/docs/speech-to-text)
- [Deepgram Nova-3 model/language overview](https://developers.deepgram.com/docs/models-languages-overview/)
- [Deepgram keyterm prompting](https://developers.deepgram.com/docs/keyterm)
- [Deepgram pricing](https://deepgram.com/pricing)
- [OpenAI Whisper model sizes and tradeoffs](https://github.com/openai/whisper#available-models-and-languages)
- [faster-whisper benchmarks and deployment requirements](https://github.com/SYSTRAN/faster-whisper#benchmark)

## Decisions to make at implementation time

Only three product choices remain:

1. Which spoken languages and wake phrases the owner actually wants enabled.
2. Whether ordinary voice prompts auto-submit immediately or remain in preview mode permanently.
3. Whether provider-side retention is acceptable; if not, choose an enterprise zero-retention
   option or the self-hosted path before sending real source-related audio.

None of these choices changes the architecture above.
