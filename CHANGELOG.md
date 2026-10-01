# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from its first tagged release.

## [Unreleased]

### Added

- **Migrations can ride the deploy.** `python -m ctb.db.upgrade`, wired to
  Railway's `preDeployCommand`, applies pending migrations from the new image
  while the old instance is still serving. It is a no-op unless
  `ADMIN_DATABASE_URL` is set, and a failure aborts the deploy rather than
  starting an image that cannot boot. Until now the migration and the image
  requiring it shipped in one commit but only the image deployed itself, so the
  ordinary way to release was also the way to take the bot down — schema 1
  under a build needing 4, presenting as a healthcheck failure, until somebody
  with a laptop ran `bootstrap`.

- **One topic per session.** A workspace is now a *group* of rooms sharing one
  container, branch and checkout, rather than one room its sessions took turns
  owning. `/fork` opens its own topic and leaves the parent's alone; `/board`
  became a two-stage picker (workspaces, then that workspace's sessions) in one
  card edited in place; `/done` archives *this task* and takes the workspace
  only when it was the last live one. Migration `003_topic_per_session`.
- `topics.room_gone` — the one seam for a deleted topic, which Telegram reports
  through no update at all. It unbinds the room, clears the routing row and says
  so once in the chat root. A detach, not an archive.
- `/digest` — one card ranking every live task by how much it wants you:
  errored, then stalled, then running, then finished, then asleep. Read from
  local rows only, so it is the one command that still answers during a
  Conductor outage. **Stalled** (working but silent past the machine's
  no-output threshold) is a state nothing else in the UI could show: the topic
  list wears the same ⚙️ for a healthy turn and a wedged one.
- The completion receipt names the files a turn changed, up to five and then a
  count.

### Added

- **A watchdog that tells you the bot has stopped working.** `ctb.watchdog` is
  a new optional service that messages a team's owners — once per episode, with
  the reason and the fix — when sessions they are waiting on have gone
  unwatched. It runs outside the supervisor on purpose, since a watchdog on a
  wedged loop is not a watchdog, and dedupes on `deliveries`' primary key using
  the moment the silence began, so one outage is one message even across a
  redeploy. `ctb.silence` attributes the cause from durable evidence only —
  `auth_failed_at`, and whether any API call was even attempted — because when
  polling stops the client pool is swept and there is nothing live left to ask.
- **Unexplained silence now fails the healthcheck.** `/health` gained its first
  new fatal condition since the database check: 30 minutes of silence with
  nothing to blame — no rejected key, no failing upstream, no calls attempted —
  returns 503 so Railway recycles the process. The bar is "would a restart
  plausibly fix this?", not severity, so a rejected key and a dead Conductor
  both stay at 200 however long they last; restarting into either would stack a
  restart loop on top of the outage.

### Changed

- **A `/digest` row is now actionable in a private chat.** `jump_url` answers
  `None` for *every* DM — Telegram publishes no link syntax for a topic — so the
  card that ranks what needs you emitted no buttons at all in the default flow,
  documented as "the thread list is one swipe away". Measured on the live
  database, that swipe lands in 43 threads of which 1 routes to a usable session.
  Rows now fall back to `📄`, which renders the task's last exchanges in place;
  a supergroup keeps `↗` and jumps, because there the link works.

- **Replying to a rendered answer reaches the task it is about.** PLAN §Safety
  rails promises "replying to any bot message routes to that message's session",
  implemented by `deliveries.session_for_telegram_message` — which reads the
  delivery ledger, so output rendered *outside* the outbox (`/log`, the card's
  Transcript button, a `📄` tap) was not the bot's as far as a reply was concerned.
  `deliveries.record_sent` writes that row in one statement rather than `enqueue`
  + `mark_sent`, because a `pending` row is one the outbox may claim and send a
  second time. This is a reply target rather than a row of `Stop`/`Retry` buttons
  for the same reason `Stop` is kept off a receipt bubble: a static button
  outlives the state it was drawn for, and a reply is evaluated when it is sent.

- **The console's verb survives a redeploy, and a coffee.** `NonceStore` is in
  memory and `button(restartable=…)` defaults to `False`, so the `📄` button minted
  a single-use handle that died with the process — on a service that redeploys
  often, a card whose only verb answers "expired" for reasons the reader cannot
  see. `Action.TRANSCRIPT` was already in `RESTARTABLE_ACTIONS`, so it now gets the
  signed self-describing payload that mechanism exists for. And a new `READ_TTL_S`
  (6h) sits beside the two tiers already there — 60s for a destructive confirm,
  15min for a safe control — because fifteen minutes is sized for `Stop`, whose
  target may have moved on, and a transcript has no such hazard.

- **The status card's `Transcript` button answers in prose.** B2 made `/log`
  readable and left the button beside it sending a `.md` of raw JSON envelopes —
  the exact artefact that fix was about — on the finished *and* errored cards.
  Both now share `power.log_body`; `/log raw` keeps the envelopes.

- **`/log` answers from the chat root.** Every session command refuses outside a
  room with "use /new or /board", which is right for `/stop` and useless for a
  read when no tappable route to a DM room exists. With no room in scope it falls
  back to the cockpit's seats: one is answered, several offer a choice.
  `require_session` is unchanged for mutations.

- **A room whose session is `DEAD` says so instead of asking Conductor.** 22 of
  those 43 threads point at one; typing in them spent an API call to be refused
  and reported `Prompt failed: …`. Read off the session row already loaded for
  the receipt, so it costs no query.

- **Typing at a button-only `/new` step now answers it.** Four of the wizard's
  seven steps draw buttons and had no text handler, so a line typed at "Project?"
  fell through to `plain_text` — which declines to start a task while a wizard is
  open and replied with the chat-root cockpit hint. The word was discarded, the
  answer was about something else, and the wizard sat waiting to be tapped.
  `match_option` resolves typed text against the options already on screen (exact,
  then a unique substring), so `opus` finds `opus-5-1m` and an ambiguous `acme` is
  refused rather than guessed — picking the wrong repository costs a paid container
  against it.

- **`/board` names a way out of its empty state.** "No live workspaces." was three
  words in the command whose whole purpose is getting you somewhere — the first
  thing a new team sees, and the last thing an owner sees after archiving
  everything. A filter matching nothing now repeats what failed.

- **`/invite` confirms a person, not a number.** A mistyped-but-real Telegram id
  seated a stranger in the organisation — its workspaces, transcripts and Conductor
  key behind it — and the bot said "Added 12345 as member." either way. The id is
  resolved with `get_chat` and the owner confirms the name. That resolution also
  enforces the precondition the command only mentioned in its usage line: a user
  the bot cannot see is a seat that could not be used.

### Fixed

- **A transient Conductor wobble no longer takes a team dark for ever.** The
  circuit breaker's half-open probe slot was claimed in `check` and handed back
  only by `record_ok`/`record_failure`, both inside the retry loop — so a request
  that left by any other route leaked it. The route it leaves by is the one a
  spurious 401 causes: `AuthFatal` cancels the tenant's sibling pollers, one of
  them mid-request. After that `_probe_in_flight` stays set for the life of the
  process, because the only reset on the read path is the `open` → `half_open`
  edge a wedged breaker never crosses again, and every later call fails fast with
  `retry_after=1s` without a request being attempted. Live: zero Conductor calls
  for **sixteen days** for two tenants, with no `api_events` row to show for it,
  ended only by a redeploy. `_request` now returns the claim in a `finally`, the
  claim is generation-checked so a late release cannot free somebody else's
  probe, and `PROBE_ABANDON_SECONDS` reclaims a slot held longer than any single
  request could run. `/health` reports `conductor.circuit.probes_abandoned`,
  which should stay zero for ever: non-zero means some path out of a request is
  skipping that `finally`.

- **A four-week-old rejection no longer reports itself as current.** Nothing but
  `/key` ever cleared `auth_failed_at`. `sessions.list_bound` had the clock,
  while `/health`, the watchdog and `/teams` each asked `auth_failed_at is not
  None` — so `silence.attribute` returned `auth_rejected` for every silence a
  once-rejected tenant would ever have. An explained silence never fails the
  healthcheck, so the stamp permanently disabled the one thing that recovers a
  wedged process, and both owners were told to re-send a key that was returning
  200 to every call. `tenancy.auth_latched` is now the single answer, used by all
  four readers, and the supervisor clears a stamp the database has judged spent.

- **A wedge is a wedge whatever the tenant row says.** `silence.attribute` put
  `auth_failed` first, on the reasoning that a latched tenant makes no calls
  *because* it was latched. That stopped being true when the latch got a clock:
  `list_bound` readmits one poller every `AUTH_RETRY_AFTER_MS`, so a genuinely
  rejected key shows up as calls that all *fail*, never as no calls at all. Zero
  calls now outranks the row — otherwise a circuit that wedges within fifteen
  minutes of a fresh 401 (the live wobble latched and wedged 67 seconds apart)
  still reads as explained and still runs for ever.

- **`/health` no longer asks to be recycled on the way out of an outage.** When
  it starts answering, the supervisor has not taken the lease yet, so every
  session still carries the stale `updated_at` of the outage being recovered
  from — which is a textbook fatal wedge except for whose fault it is. The fatal
  wedge now also requires the process to have been up `POLL_SILENT_MS`, the same
  number that makes a session count as silent at all. The degradation still shows
  from the first report; only the recycle waits.

- **A rejected tenant is no longer cancelled and restarted every five seconds.**
  `auth_fatal_tenants` derived its `rejecting` half from `_tenant_of`, and the
  only use of that set is to cancel exactly those sessions — `_drop` pops
  `_tenant_of`, so the latch read empty again by the time the spawn loop asked in
  the same pass. 304 poller starts in thirteen minutes, none completing a tick.
  It reads off the client pool now, which outlives the pollers.

- **The finished-turn and error cards render again.** Conductor returns
  `deepLink` as `conductor://workspace?id=…`; Telegram answers `Unsupported URL
  protocol` on an inline button and discards the *whole* `editMessageText`. Since
  `OPEN` is in both `_DONE_BUTTONS` and `_ERROR_BUTTONS`, a turn would finish,
  its answer would arrive, and the card above it would still read "working".
  `button_url` admits only `http`/`https`/`tg` and drops the button otherwise —
  the path already taken when no deep link is known.

- **The voice recovery sweep runs at all.** `_sweep` polled the tenant-scoped
  pool from a process-level task where the boot-time `_recover` beside it
  correctly uses the worker pool, so it raised on every pass
  (`voice.recover_failed  no tenant in scope`, every two minutes) and the
  "Transcribing…" hang it exists to end went on ending only at the next
  redeploy.

- **A note that runs out of attempts now tells its owner so.** Both recovery
  passes are cross-tenant and have no tenant of their own, while `_send_failure`
  reads the note's acknowledgement id off the tenant-scoped pool. `_recover`
  wrapped that in a bare `suppress(Exception)`, so every "Transcription kept
  failing" card died unseen; the sweep could not reach the call at all. Fixing the
  pool made it reachable — and reachable unscoped it raises into the voice
  `TaskGroup`. `_notify_abandoned` answers each row inside its own scope, and logs
  a failure rather than swallowing it.

- **The watchdog no longer alarms in the same second as boot.** `run` ran its
  first census before its first pause, when the supervisor has not taken the lease
  and every session still carries the stale `updated_at` of the outage being
  recovered from. Live, it fired for both tenants on the deploy that was fixing
  them. The first census now waits one interval — a minute against a ten-minute
  threshold.

- **`button_url` rejects what `is_safe_url` rejects.** It reuses the renderer's
  check, which has always degraded an unsafe `href` to plain text, so a URL
  carrying a newline, tab or space no longer reaches Telegram as the same 400 a
  bare scheme test would have waved through. The two allowlists stay separate:
  `mailto:` is legal in an `href` and not on a button.

- **A sleeping workspace is no longer mistaken for a waking one.** Every live
  workspace reports `sleeping` between turns and `ready` may never be observed,
  but `is_waking` counted it — so an idle room was pushed into `WAKING`, which
  arms `WAKE_TIMEOUT_S`, and ten minutes later announced "The workspace did not
  become ready within 10 minutes" about a workspace with nothing wrong with it.
  With nothing outstanding it now stays put and says 💤.

- **A follow-up in a DM topic no longer offers to build a second workspace.**
  `apply_marker` treated a refused `editForumTopic` as proof the topic was
  deleted; in a *private* chat Telegram answers `TOPIC_ID_INVALID` for a thread
  it merely will not let a bot rename, which is byte-for-byte what a deleted one
  answers (`claim_topic` already declined to read anything into it). So a room
  somebody was working in was detached mid-task — and because `chats.unbind`
  cleared the workspace pointer as well as the session,
  `Route.claimable_thread` then read that room as Telegram's empty *New Chat*
  seat and answered the next line typed there with the new-workspace confirm
  card: a second paid container and a second Conductor chat instead of the
  follow-up it was. `rename_proves_deletion` limits the conclusion to
  supergroups, where a dead topic refuses the send too; `chats.detach_session`
  keeps the workspace so a detached room stays recoverable; and a room that kept
  its workspace now answers a typed or dictated line with one **reopen** button
  rather than "No session here", which was false of a thread nobody had left.
  That button lands in the room it was tapped in — `Route.reclaimable_thread`,
  the narrow counterpart to the refusal above — and reopens the session that
  room was last used to talk to (`prompts.last_session_in_room`) rather than the
  workspace's newest, which is a coin flip once a workspace has several.
  Migration `005_reclaim_detached_rooms` repairs the rooms already detached,
  from the prompt ledger; it changes no schema and is optional to boot.

- **A deleted group stops being the team's alarm bell.** Every silence alarm and
  all-clear also goes to `tenant_chats.is_primary`, and a group deleted from
  Telegram made each of those a permanent `failed` delivery — 17 in two days on
  the live database, and every failed row in it. Nothing was lost, because the
  owners' DMs are separate targets, and nothing corrected it either: `/health`'s
  failure digest reported a fault with no cure, which is where a real delivery
  failure can hide. A terminal send failure whose words mean *the chat is gone*
  now withdraws that chat's nomination — never its tenancy, never a private
  chat, and only on `CHAT_GONE_MARKERS`.

- **`/health` can finally see its own absence.** During the four-day polling
  outage the report read `ok` at HTTP 200 the whole time, so Railway never
  restarted anything and nothing paged anybody: `polling` is built from
  `list_bound`, and that query's `auth_failed_at` filter *was* the outage;
  `unwatched` needs `NOT is_bound`; the auth counter iterates live clients and
  every client had been swept; nothing polled means nothing queued. Zero
  pollers doing zero work is indistinguishable from perfect health to all of
  them. `sessions.list_silent` counts the *work owed* instead of the workers —
  bound, unarchived sessions of an active, keyed tenant that nothing has
  touched in 10 minutes — with no `auth_failed_at` filter, and raises the new
  `poll_silent` degradation. Deliberately still zero for a tenant with no key,
  a suspended tenant or an archived session, so it cannot become the amber
  light nobody reads.
- **One transient 401 no longer takes a whole team off the air indefinitely.**
  A single `401` — the only two in 2,246 live requests, arriving either side of
  a `500 timeout exceeded when trying to connect` and a `ReadTimeout` — stamped
  `tenants.auth_failed_at` for two unrelated teams sixty-nine seconds apart.
  `sessions.list_bound` drops any tenant carrying that stamp, so both stopped
  polling entirely; the process stayed up, every command kept working on the
  same keys, and no background API call was made for four days. Only re-sending
  `/key` cleared it, and nothing on screen said so. Now a 401 is corroborated
  with one `GET /me` before a team is stopped at all, and the stamp expires
  after 15 minutes so one poller is let back through to ask again — refreshed
  on each fresh rejection, so a genuinely revoked key still waits. Suspension
  keeps its permanence: that one is an operator's decision, not a proxy's.
- **A voice note no longer dies of one network timeout.** `MAX_ATTEMPTS = 3`
  was enforced only for a job whose *process* died; a worker that caught its own
  exception failed the row on attempt 1 regardless of the error. Live, that
  turned a 60-second `TelegramNetworkError` fetching a 28 KB file into a dead
  note with a Retry button somebody had to notice and tap. Infrastructure
  errors now requeue while attempts remain, while `TranscriptionError` — the
  class the provider wraps every one of its own failures in, alongside "no
  clear speech" and "over 20 MB" — stays terminal on the first try, because
  those say the same thing twice and were already billed. A job holding its
  transcript resumes at dispatch, so no retry pays the speech vendor twice.
- **A database one migration behind now fails the boot, by name.** The gate
  only asked whether *any* schema was present, but the repo layer names its
  columns — so a deploy that skipped `ctb.db.bootstrap` came up, `/health`
  asked for the bound sessions, PostgreSQL answered `UndefinedColumn`, and
  Railway reported *"Network › Healthcheck · Healthcheck failure"* with nothing
  in the log naming the cause. Boot compares against
  `REQUIRED_SCHEMA_VERSION` and prints the command to run; a test refuses to
  let a new migration leave that constant behind.
- **`DEFAULT_BRANCH` (and `DEFAULT_AGENT` / `DEFAULT_MODEL` / `DEFAULT_EFFORT`)
  reached no tenant at all.** The four `tenants.default_*` columns were NOT NULL
  with the shipped literal as their column default, `TenantSettings` read the
  row, and nothing in the bot ever wrote it — so every team answered `main` /
  `claude` / `opus-5-1m` / `high` forever, whatever the environment said. The
  columns are an *override* now, and NULL means "follow the platform"
  (migration `004_platform_defaults`).
- **A create pinned its seat's branch.** `create_and_bind` wrote every request's
  branch back onto the chat, and the chat outranks both the tenant and the
  platform, so one workspace made on `main` fixed that chat to `main` for good.
  The project, agent, model and effort are still remembered; the branch is not,
  and `/defaults branch <name>` is now the only thing that sets it.
- **Every `/fork` and `/s` left two bound sessions on one seat.** Nothing
  unbound the session a room already held, so the supervisor polled both and
  both delivered into the same topic with nothing saying which was which; which
  one a prompt reached was a `created_at` tiebreak. Now a constraint
  (`uq_sessions_one_per_room`, `uq_chats_one_room_per_session`) rather than
  discipline, with the existing duplicates resolved by the migration.
- Two sessions of one workspace no longer fight over a single topic marker, so a
  room can no longer read `⚙️ working` for a session nobody is looking at.
- A thread-gone delivery reroute now frees the room instead of moving one row at
  a time and paying the reroute again on the next turn.
- The delivery dedup guard compared `(chat_id, thread_id, content_hash)` across
  *sessions*. Harmless while a workspace's sessions shared a room; with two
  rooms deleted and both rerouted to the chat root, two forks answering "Done."
  would have lost one. Scoped to the session.
- `TurnSummary.files_changed` was hardcoded to `0`, so both of its readers —
  the finish line and the done card — rendered a "N files" segment that could
  never appear. The count and the paths now come from the transcript, via the
  renderer's own file-edit reducer so the receipt cannot disagree with the
  chat above it about what an edit is.

### Changed

- **`/s` is retired.** Every job it had moved: per-session rooms removed
  "switch session inside a workspace" as a concept, `/board` stage 2 lists a
  workspace's other sessions, stage 1 reaches a workspace, and stage 2's
  bind-the-seat branch moves the one binding a chat without topics has. It stays
  registered as a silent alias to `/board` for the muscle memory.
- The wizard's branch step always offers `main` as well as the configured
  default, so it stops being a one-button formality you have to type your way
  out of. With `DEFAULT_BRANCH=dev` that is `dev` then `main`, `dev` first.
- `chats.bind` refuses to repoint a *topic* at a different session. A room is not
  a pointer; thread 0 — the linear seat and a group's General — is exempt.
- `/log` renders the last exchanges as readable lines instead of a `.md` file
  of raw JSON; `/log raw` keeps the old document for debugging a shape.
- The DM cockpit's "Send to …" offers the three most recently prompted tasks
  rather than only the newest, which was a coin flip once a chat held more
  than a couple.

## [0.1.0] — 2026-07-30

First public release, under the MIT licence.

### Added

- DM-first sign-up: `/start` alone creates the team and asks only for a
  Conductor key. A Telegram group is now an optional `/team` step, not a
  prerequisite, and workspaces get their own topic inside a private chat.
- Topic names lead with the task, taken once from the opening prompt, so
  several workspaces on one repository are told apart at a glance.
- `tidy_rename_notice` removes the "changed the topic name to …" service
  message the bot's own renames provoke.
- A cancel that Conductor never confirms now times out, says so, and hands the
  controls back instead of sitting on "stopping…" forever.
- A reply the outbox gives up on now says so in the topic, with a pointer to
  the transcript.
- `docs/README.md` indexes the documentation; `docs/BOT_METADATA.md` pins the
  BotFather copy.

### Changed


- One task is one notification. Under anything but `/notify loud` a topic's
  replies are now *held* until the turn ends and land as one batch ahead of the
  finish line, because a silent message still occupies a line in the tray. The
  live surface while the work runs is the pinned card, which is an edit and
  never notifies. Two valves keep it from becoming a delivery gate: a session
  whose poller has stopped writing is ignored, and no queue is held longer than
  half an hour whatever the state machine believes.
- The output contract appended to every prompt now states the constraints it
  used to imply: the bubble is 40 characters wide, a table has nowhere to go,
  long code leaves the chat as an attachment, a reader on a phone has no shell
  to run a suggested command in, and a `Choices:` option over 40 characters
  loses its button. It also spends its words on the two round trips that cost
  the most — burying "blocked" at the bottom, and asking a question the repo
  already answers.
- `/notify off` means off. It previously behaved identically to `quiet`,
  because the 30-minute focus window promoted both.
- A split reply pushes one notification, not one per chunk.
- The status card no longer runs its own stall detector, which fired on healthy
  text-only turns and never cleared. The turn machine owns that question.
- `/help` lists `/stop`, `/find` and `/name`, which are part of the daily loop.
- The General topic is addressed as one seat whether Telegram reports it as
  thread 0 or thread 1.

### Fixed

- A deploy landing mid-turn no longer posts a second status card and strands
  the first with a live Stop button on it.
- `/board` counts workspaces rather than transcript rows, so a workspace with
  several sessions is one entry, not several.
- The `tg-<chat>-<nonce>` reconciliation key no longer reaches buttons, lists
  or topic titles.

### Security

- `python -m ctb.rewrap` now refuses to run under a role that row-level
  security applies to. Pointed at the application DSN it failed with
  `permission denied for table tenants`, three steps into a breach runbook,
  saying nothing about which of two DSNs to reach for.
- Rotation now recomputes each stored key fingerprint. `fingerprint_of` is
  keyed by a subkey of the *active* master key, so every `*_key_fp` was left
  behind by a rotation — which silently broke the "that is already the stored
  key" check in `/key` for every tenant.
- Master-key rotation has tests. It is the module `SECURITY.md` points an
  operator at after a leak, and it had none: seal under `v1`, load `v2`, re-seal,
  drop `v1`, and confirm every secret still opens.
- CI scans the full git history for committed credentials on every pull request,
  and fails if the scanner reports having read no commits.
- Every GitHub Action is pinned by commit SHA, and the `actionlint` image by
  digest. A tag is mutable; on a public repository that is somebody else's push
  away from being ours.
- CodeQL runs on `main` and weekly.
- The published image is pinned to the Python version CI tests on. It ran 3.14
  while every test job ran 3.13, which made the artifact that reaches production
  the one interpreter the suite never executed against.
- `tests/fixtures/probe_verified.jsonl` no longer contains identifiers from a
  live account.
- CI runs with `permissions: contents: read` and a pinned `actionlint` image.

[Unreleased]: https://github.com/monosoftdev/conductor-tg-bot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/monosoftdev/conductor-tg-bot/releases/tag/v0.1.0
