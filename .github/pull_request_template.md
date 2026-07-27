## What changed

<!-- What is different for someone using the bot. One or two sentences. -->

## Why

<!-- If it is not obvious from the above. Link the issue if there is one. -->

## Verified

- [ ] `pytest -q`
- [ ] `ruff format --check` · `ruff check` · `pyright`
- [ ] Tried it by hand (say where: a group topic, a DM, a live deploy)

<!-- Say what you did NOT verify. "Not tested against a real Conductor org"
     is a useful sentence, not an admission. -->

## Risk

<!-- Delete the lines that do not apply. -->

- [ ] Touches tenant isolation, RLS policies, or a database role
- [ ] Touches secret handling, logging, or the crypto envelope
- [ ] Changes the schema (there is one squashed migration; adding a second is
      a decision, not a detail)
- [ ] Changes delivery, claiming, or the transcript cursor
