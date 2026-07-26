# Handoff — 2026-07-25

## Where we are

Phase 0 is written but **has never been run against the live API**. Nothing else is implemented.

Done:
- Repo scaffold, `Dockerfile`, `railway.toml` (`numReplicas=1`, `/data` volume, `overlapSeconds=0`).
- `scripts/probe_transcript.py` — the Phase 0 probe. Lint-clean, CLI smoke-tested.
- `tests/test_probe_shapes.py` — 14 passing, no network needed.
- `docs/PLAN.md` — the full approved design.

Not started: everything in `src/ctb/`. The directory is empty on purpose — the build order is
probe → client → db → `turn/machine` → poller → delivery → bot, and the probe's results set the
constants for the two stages after it.

## Next action

```bash
export CONDUCTOR_API_KEY=...          # https://app.conductor.build/users/api-keys
.venv/bin/python scripts/probe_transcript.py dump --auto
```

`dump` is read-only. It discovers recent sessions via the SQL view, pages full transcripts to
`probe-out/transcripts.jsonl`, and writes `probe-out/shape_report.md` — the `type` histogram and the
recursive `content` structure per type. That alone unblocks the renderer.

Then, against a **scratch** session (this one sends real prompts and costs tokens):

```bash
.venv/bin/python scripts/probe_transcript.py assume --session <SCRATCH_SESSION_ID>
```

## The result that could invalidate the plan

**Assumption test #7 — does re-POSTing the same `messageId` dedupe?**

The whole crash-safety model is: write the `outbound_prompts` row → POST → on any ambiguous failure
(timeout, reset, 5xx) retry forever with the same id. That rests on `messageId` being an idempotency
key, which is *inferred from the parameter name*, not documented anywhere.

If the duplicate POST produces two prompts, that design is invalid and the recovery path has to be
rethought before any of Phase 1 gets written. The probe prints this explicitly:

> `7 re-POST with same messageId deduped: N user-echo messages carry the marker. If this is 2, the
> idempotency-key design is INVALID.`

Also worth reading closely in the output:

- **Test 4** — `after=<garbage id>`. If it silently returns a full replay instead of 4xx, the
  `sessionIndex` filter in `turn/cursor.py` is the only thing standing between a cursor glitch and
  re-posting an entire transcript to Telegram.
- **Test 1** — whether our POSTed `messageId` shows up as a transcript `message.id`. This defines
  "this prompt has been witnessed", which is what stops a turn finalizing early. If it fails, fall
  back to the `index_at_post` snapshot comparison (already designed in `docs/PLAN.md`).
- **D1/D3** — the timing trace. D3 counts how many `idle` polls arrive *before* the turn starts,
  i.e. exactly how badly a naive "idle means done" poller would misfire. These numbers replace the
  guessed constants (`QUEUED` cadence, `DRAIN_CONFIRMS`, `QUIET_FINALIZE`) in `docs/PLAN.md`.

## After the probe

1. Commit a curated subset of `probe-out/transcripts.jsonl` to `tests/fixtures/` — these become the
   renderer's test corpus. Review before committing; it's real transcript text.
2. Update `docs/PLAN.md`'s timing constants with measured values.
3. Record the assumption results in this file, then start `src/ctb/conductor/client.py`.

## Environment notes

- `probe-out/` is gitignored. It contains real transcript content.
- Python 3.13.13 venv at `.venv/`. `python3` on this Mac is 3.12 — use `~/.local/bin/python3.13`.
- If run inside a Conductor **cloud** workspace, `CONDUCTOR_API_KEY` and `CONDUCTOR_API_URL` are
  already in the environment (workspace-scoped). Locally you must export your own.
