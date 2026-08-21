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

### Fixed

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
