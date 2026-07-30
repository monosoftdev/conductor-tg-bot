# Contributing

Thanks for looking. This is a small, opinionated codebase; the fastest way to
get a change merged is to match what is already there.

## Getting a working tree

Python 3.13 and Docker.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d --wait db      # PostgreSQL 16 on :5433, disposable
cp .env.example .env
```

Create the roles and schema once, against the compose database:

```bash
.venv/bin/python -m ctb.db.bootstrap \
    --admin-dsn "postgresql://postgres:postgres@127.0.0.1:5433/ctb" \
    --app-password dev --worker-password dev
```

You do **not** need a Telegram bot token or a Conductor API key to run the
tests. Both are faked.

## Running tests

**Run only the tests for what you changed.** CI runs the whole suite in
parallel and nothing merges until it is green, so a full local run after every
edit buys nothing.

```bash
.venv/bin/python -m pytest tests/test_outbox.py -q         # the file you touched
.venv/bin/python -m pytest tests/ -q -k "callback"         # or by keyword
.venv/bin/python -m pytest tests/ -q -m "not db"           # ~2s, no Docker
```

Pick the target by what the change can *break*, not by what it edits. Run the
whole suite locally only when the change is broad — `db/connection.py`,
`migrations/`, `keyboards.py`, `outbox.py`, the middleware chain — or before
tagging.

Always run these three before opening a pull request; they are fast and they
are the first thing CI checks:

```bash
.venv/bin/python -m ruff format --check src scripts tests
.venv/bin/python -m ruff check src scripts tests
.venv/bin/python -m pyright
```

## What CI runs

`.github/workflows/ci.yml`, all in parallel:

| Job | What it proves |
|---|---|
| `format · lint · types` | style and types, in seconds |
| `secret scan` | no credential has ever been committed, over the whole history |
| `tests (no database)` | the pure-logic half really is offline |
| `tests 1–4/4` | the full suite, sharded, each on its own PostgreSQL |
| `docker build` | the image builds and can import its own entrypoint |
| `boot` | `python -m ctb` starts for real against a real database |
| `all gates` | the single required check — fails if any of the above did |

`CI=true` makes an unreachable database a **failure** rather than a skip. A
skipped database is not a passing build; it means every isolation, RLS, crypto
and claim test silently did not run.

The secret scan is allowlisted in [`.gitleaks.toml`](.gitleaks.toml). If it
flags something of yours, read the entry before adding one next to it: a broad
`paths` exclusion is how a scanner quietly stops covering the directory a real
key eventually lands in. And if a match turns out to be genuine, **rotate it** —
deleting the line does not remove it from history, which is the reason the scan
reads history at all.

To reproduce a failing shard locally, use the same two flags CI used:

```bash
.venv/bin/python -m pytest -q --splits 4 --group 3
```

## What the codebase expects

- **Type hints on every signature.** `pyright` runs in strict-ish mode and a
  new `Any` needs a reason in a comment.
- **`async`/`await` for all I/O.** A blocking call on the event loop stalls
  every tenant, not one.
- **Line length 88**, `ruff format` decides the rest. Do not hand-format.
- **HTML parse mode, never MarkdownV2.** MarkdownV2 needs 18 characters escaped
  including inside code spans; agent output is made of those characters and one
  miss is a 400 and a lost reply.
- **No `WHERE tenant_id = ?` in repository SQL.** Row-level security does that.
  Adding the filter by hand hides the case where the scope was never set.
- **Never gate a `sendMessage` on the turn state machine.** The transcript
  cursor is the source of truth for content; status is a cadence knob. If the
  state machine is wrong you get a stale progress line, never a lost reply.

The reasoning behind each of these is in [`CLAUDE.md`](CLAUDE.md) and
[`docs/`](docs/); if a rule looks arbitrary, the why is written down.

## Tests

- Name the behaviour, not the function:
  `test_a_customer_cannot_suspend_anyone`, not `test_suspend_2`.
- A test that needs the database gets `pytestmark = pytest.mark.db`; the `db`
  and `system_db` fixtures are in `tests/pg.py`.
- Background work (pollers, claim loops, prune) runs with **no tenant in
  scope**. If you are testing that, use `unscoped()` — a test that passes only
  because a fixture left a tenant in scope is a test that passes while the
  feature is dead. That exact mistake once hid three real bugs.
- Before claiming a test has teeth, break the code it covers and watch it fail.

## Commits and pull requests

One idea per commit; the subject is what changed for a user, not which file
moved. Explain *why* in the body if it is not obvious.

In the pull request, say what you verified and what you did not. "Tested by
hand on a phone" is a useful sentence. So is "not tested against a real
Conductor org".

## Security

Do not open a public issue for anything that could expose a key or one tenant's
data to another. [`SECURITY.md`](SECURITY.md) has the private path.

## Code of conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
