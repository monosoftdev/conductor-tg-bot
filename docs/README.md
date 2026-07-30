# Documentation index

Twelve files, and they do not all mean the same thing. This says which is which
before you spend time on one that describes a design rather than the system.

## Read these — they describe what is built

| File | What it answers |
|---|---|
| [`GETTING_STARTED.md`](GETTING_STARTED.md) | How a person uses the bot, from `/start` to a first reply. The user-facing walkthrough; it and the bot's own onboarding copy must not drift. |
| [`SETUP.md`](SETUP.md) | How to deploy it from zero on Railway, in order. One way to do it, not the only one. |
| [`TENANCY.md`](TENANCY.md) | How one bot serves many teams without leaking between them. Row-level security, the two roles, the seams you must not cut. **Where this and `PLAN.md` disagree, this wins.** |
| [`NAMING.md`](NAMING.md) | What every glyph, topic title and rename means, and why each one earns its place. |
| [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) | The moving parts and how a message travels through them. |
| [`DEPLOY.md`](DEPLOY.md) | Operating it: what runs where, what to watch, what to do when it breaks. |

## Reference — still true, but narrower

| File | What it is |
|---|---|
| [`RELIABILITY_AUDIT.md`](RELIABILITY_AUDIT.md) | The failure modes that were deliberately tested, and how. |
| [`VOICE_CONTROL_PLAN.md`](VOICE_CONTROL_PLAN.md) | The design of the voice path. |
| [`BOT_METADATA.md`](BOT_METADATA.md) | The BotFather copy — name, description, about text, and the four settings that are not copy. The store listing is the one piece of user-facing text that does not live in the code, so it drifts unless it is written down. |

## Historical — kept for the reasoning, not as a description

| File | Why it is still here |
|---|---|
| [`PLAN.md`](PLAN.md) | The original single-user design. Its reasoning about the Conductor API, the turn state machine and the delivery contract is exactly right and still implemented. Its storage (SQLite), its single owner, and its required supergroup are all superseded. |
| [`ROADMAP.md`](ROADMAP.md) | What was built in what order. A record, not a plan. |
| [`HANDOFF.md`](HANDOFF.md) | Working notes, newest first: why a given decision went the way it did. Written for whoever picks the work up next. Where it and `TENANCY.md` disagree, `TENANCY.md` wins. |

## Not in this directory

- [`../README.md`](../README.md) — what this is and whether you want it.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — running it locally, and the quality gates.
- [`../SECURITY.md`](../SECURITY.md) — reporting a vulnerability, and the key-rotation runbook.
- [`../CLAUDE.md`](../CLAUDE.md) — the conventions this codebase holds itself to. Written for an AI assistant working in the repo, and accurate for a human too.
