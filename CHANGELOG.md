# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from its first tagged release.

## [Unreleased]

Nothing is tagged yet. The bot has not been run against a live Conductor
account and a live Telegram deployment end to end — see
[What is still unproven](README.md#what-is-still-unproven).

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

- `tests/fixtures/probe_verified.jsonl` no longer contains identifiers from a
  live account.
- CI runs with `permissions: contents: read` and a pinned `actionlint` image.
