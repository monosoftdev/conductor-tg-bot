# Reliability audit

Audited 2026-07-26 against the repository, scripted Conductor/Telegram faults,
the complete automated suite, and the production container. This is the
operator-facing failure contract—not a claim that third-party outages are
impossible.

## Reply contract

For an authenticated user in an allowed chat:

- Every supported command, text prompt, voice/audio message, unsupported
  attachment, unknown command, edited message, and callback gets a visible
  result or acknowledgement.
- Unexpected handler failures return one generic recovery line and put the
  detailed, scrubbed exception only in logs.
- Old or malformed buttons stop spinning and say how to get fresh controls.
- Telegram service events such as topic creation are intentionally ignored;
  replying to them would create noise.
- Users outside the allowlist and updates outside `TELEGRAM_CHAT_ID` are
  intentionally silent. Silence is the security boundary, not a failure.

Interactive command replies retry transient Telegram failures three times.
Final Conductor output is stronger: it is persisted in the delivery outbox and
keeps retrying transient failures without a terminal attempt cap.

No software can send a Telegram reply while Railway is down, the bot token is
revoked, Telegram is unavailable, or the user blocked the bot. A rejected token
now fails the deployment instead of presenting a false-green service.

## Failure matrix

| Boundary | Fault simulated or inspected | Expected behavior |
|---|---|---|
| Telegram update | Duplicate `update_id`/message delivery | Stable prompt/action IDs make reprocessing idempotent |
| Telegram update | Unknown slash command or future content type | One short help/unsupported reply |
| Telegram update | Edited prompt after submission | Says edits are not resent; correction must be a new message |
| Telegram callback | Expired, reused, malformed, or unknown button | Alert closes spinner; destructive callbacks stay single-use |
| Handler | Unexpected exception | Allowed user sees generic retry/health line; no secret or stack trace |
| Authorization | Unknown user or wrong configured group | Silently rejected before download, DB mutation, or API call |
| Owner command | Allowed non-owner invokes `/health`, `/backup`, `/sql`, `/allow`, `/deny` | `Owner only.`; never silent and never executed |
| Command reply | Telegram timeout, 5xx, or `retry_after` | Bounded retry; HTML entity failure falls back to plain text |
| Prompt accept | Telegram redelivers the same message | Existing durable prompt row and identical Conductor `messageId` reused |
| Prompt POST | Timeout after Conductor may have accepted it | Remains pending; retry uses the identical ID |
| Prompt POST | Workspace is still initializing | Prompt remains durable and submits when ready |
| Prompt POST | Fast answer arrives before first normal poll | Cursor is seeded before submission; answer is not classified as history |
| Conductor API | 429/5xx/network interruption | Rate limit, backoff, concurrency cap, and circuit breaker apply |
| Conductor API | Genuine repeated 401/403 | Pollers stop and owner is alerted once; no retry storm |
| Conductor API | One transient 403 overlaps a successful 2xx | Client counter and supervisor fatal latch recover; pollers restart |
| Transcript | Status is stale, idle, error, or changes mid-drain | Cursor still drains; status controls cadence, never delivery eligibility |
| Transcript | Cursor ID becomes unknown/404 | Offset/session-index repair avoids replaying the transcript |
| Transcript | Same-looking messages or non-gap-free indexes | Envelope ID and ordered cursor rules prevent content-based guessing |
| Delivery | Process dies before/after Telegram send | Claim/lease/hash ledger minimizes overlap; pending output resumes on boot |
| Delivery | Telegram transient failure | Durable output stays pending indefinitely |
| Delivery | Telegram permanent rejection | Delivery is parked as failed and surfaced by health instead of hot-looping |
| Rendering | Bad agent HTML, Unicode, huge result, or diff | Escaped HTML, UTF-16-safe chunks, plain fallback, and document overflow |
| Topic | Missing manage-topic permission | Setup detects it; creation fails explicitly and fresh partial topics are cleaned |
| Topic | Remembered topic was deleted | Only Telegram's explicit not-found response permits one replacement |
| Topic | Topic check times out | Adoption stops; it never guesses and creates a duplicate |
| Topic | Two `/attach` taps race | Per-workspace lock creates/binds one topic |
| Topic | Crash left workspace row but no chat/session route | Existing topic is repaired in place, not cloned |
| Topic | Workspace is already connected in another chat | Refused; original routing is not silently moved |
| Adoption | Cursor seek or DB binding fails | Fresh topic and newly cached partial rows are cleaned; retry is safe |
| Adoption | Existing transcript has history | Cursor starts at its end; one read-only last-exchange card is shown |
| Session switch | Search uses workspace name while session has another title | Attached workspace is still found; topic switches remain workspace-local |
| Voice | Duplicate Telegram voice update or redeploy mid-job | Durable unique job/operation ID prevents a second action |
| Voice | Too large/long, disabled, missing key, empty/ambiguous transcript | One concrete result; no command or prompt executes |
| Voice | Speech resembles a command without wake phrase | Treated as ordinary speech; destructive action cannot fire |
| Voice | Spoken archive command | Named confirmation is still required |
| PostgreSQL | Restart/redeploy during work | Prompt/cursor/outbox rows and voice jobs resume; the container holds no state |
| PostgreSQL | Two runtime instances overlap | Singleton lease gates pollers; claims use `FOR UPDATE SKIP LOCKED`, and recovery leaves a peer's fresh claim alone |
| PostgreSQL | Schema missing or older than the code | Boot fails naming `python -m ctb.db.bootstrap`; the app never applies DDL |
| PostgreSQL | Connection pool exhausted | `PoolTimeout` raises with a stack; pool stats are in `/health` |
| Tenancy | A query forgets its tenant filter | Row-level security returns zero rows, not another workspace's data |
| Tenancy | One workspace's Conductor key is rejected | Only that workspace's pollers stop; its owners are told |
| Tenancy | One workspace floods Telegram | Its chats are paused; the rotor keeps serving everyone else |
| Runtime | Any long-lived critical service exits | Structured runtime exits so Railway restarts the complete unit |
| Maintenance | Retention or backup fails | Delivery continues; maintenance retries on the next reconciliation |

## High-value phone scenarios

1. **Laptop to phone:** create a Conductor cloud workspace on the laptop, run
   `/attach name` (or `/board`), tap `+ Open`, read the snapshot, then type in
   that topic. A Mac-local-only workspace is not reachable from Railway.
2. **Bad connection:** send once, see 👀 or a short queued line, then lock the
   phone. The prompt ledger and outbox—not the phone connection—carry the turn.
3. **Redeploy mid-turn:** the new process reacquires the lease, recovers pending
   prompts/deliveries, and continues from the stored cursor.
4. **Parallel work:** each workspace stays in its forum topic; `/s name` searches
   workspace and session names; a topic can switch only among its own sessions.
5. **Uncertain button:** every decision has a recommended first option; archive
   and other destructive controls name the consequence and require confirmation.
6. **Something feels stuck:** run `/mode`, then `/health`. Use Check/Retry/
   Transcript/Open from the status card instead of guessing whether to resend.

## Residual live-only verification

Automated tests use faithful scripted transports, but the following require the
real deployment:

- bot admin permissions and Telegram forum behavior in the actual supergroup;
- real PostgreSQL behaviour under a redeploy, and two workspaces at once;
- real Conductor credentials, sleeping-workspace wake behavior, and account
  rate limits;
- Telegram/Conductor outage duration beyond the bounded command-reply window;
- at least 30 owner voice recordings before enabling `VOICE_MODE=commands`;
- a redeploy during a real active turn and during a real transcription.

Use the phone checklist in `README.md` after deployment. Do not interpret an
offline green suite as proof of these external conditions.
