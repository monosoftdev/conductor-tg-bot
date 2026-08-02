# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from its first tagged release.

## [Unreleased]

### Added

- `/digest` — one card ranking every live task by how much it wants you:
  errored, then stalled, then running, then finished, then asleep. Read from
  local rows only, so it is the one command that still answers during a
  Conductor outage. **Stalled** (working but silent past the machine's
  no-output threshold) is a state nothing else in the UI could show: the topic
  list wears the same ⚙️ for a healthy turn and a wedged one.
- The completion receipt names the files a turn changed, up to five and then a
  count.

### Fixed

- `TurnSummary.files_changed` was hardcoded to `0`, so both of its readers —
  the finish line and the done card — rendered a "N files" segment that could
  never appear. The count and the paths now come from the transcript, via the
  renderer's own file-edit reducer so the receipt cannot disagree with the
  chat above it about what an edit is.

### Changed

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
