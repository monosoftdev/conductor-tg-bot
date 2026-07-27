# Security

This bot holds other people's Conductor API keys. Each one can read every
transcript in its organisation and spend its owner's money. Treat a report
about key handling, tenant isolation or log content as urgent.

## Reporting a vulnerability

Email **info@reclaimly.com** with `SECURITY` in the subject, or open a
[private advisory](https://github.com/reclaimly/conductor-tg-bot/security/advisories/new).
Please do not open a public issue for anything that could expose a key or one
tenant's data to another.

Include what you did, what you saw, and — if you can — the smallest reproducing
input. You will get an acknowledgement within 72 hours and a fix or a plan
within 14 days. There is no bounty; there is credit in the release notes if you
want it.

## What is in scope

- Reading another tenant's rows, transcripts, sessions or deliveries
- Recovering a stored API key without `CTB_MASTER_KEYS`
- Binding the bot to a workspace you were not invited to, or promoting yourself
- Getting a secret into a log line, an error message or a Telegram reply
- SQL injection through `/find`, `/sql` or any wizard input
- Forging or replaying an inline-button payload to act on someone else's session

## What is not

- Anything that needs `CTB_MASTER_KEYS`, a database superuser, or the bot token
- Denial of service against your own workspace's quotas
- Telegram platform behaviour (message retention, group history, screenshots)
- ElevenLabs retaining audio on a non-enterprise tier — that is documented
  behaviour of the vendor and the reason `VOICE_ENABLED` defaults to `false`

## The security model, briefly

Read [`docs/TENANCY.md`](docs/TENANCY.md) for the full version. The short one:

- **Isolation is a database guarantee, not a code-review guarantee.** Every
  tenant table has `tenant_id`, `ENABLE` + `FORCE ROW LEVEL SECURITY`, and a
  policy keyed on the transaction-local `ctb.tenant_id` setting. Repository SQL
  contains no `WHERE tenant_id = ?`; a forgotten filter returns zero rows.
- **Two roles.** `ctb_app` runs every handler with RLS enforced. `ctb_worker`
  has `BYPASSRLS` and runs the claim loops. `ctb_app` is not a member of
  `ctb_worker`, so there is no `SET ROLE` path from a request to the bypass.
- **Keys are sealed, not stored.** AES-256-GCM envelope encryption; the AAD
  binds each ciphertext to `(kid, tenant_id, purpose)`, so moving a row between
  tenants makes it undecryptable rather than useful. Plaintext lives only
  inside a `ConductorClient`'s headers and dies with it.
- **Nothing registers a tenant key with the log scrubber.** A process-wide set
  of every customer's plaintext key would keep them all in memory for the life
  of the process. Keys appear only in an `Authorization` header, which is
  redacted by pattern, unconditionally.
- **Transcript content is the customer's source code.** `LOG_TRANSCRIPT_CONTENT`
  is `false` by default, stored content is capped at 64 KB per message, and
  `transcript_messages` is pruned after 30 days.

## If a master key leaks

`CTB_MASTER_KEYS` plus a database dump is every tenant's Conductor key. Back
the two up separately — together they are one secret.

1. Generate a new key: `python -m ctb.rewrap --new-key v2`.
2. Prepend it to `CTB_MASTER_KEYS` (keep the old one) and deploy. New writes
   use `v2`; old rows still open with `v1`.
3. Re-seal everything: `python -m ctb.rewrap --rewrap`.
4. Drop the leaked key from `CTB_MASTER_KEYS` and deploy again.
5. **Tell every tenant to rotate their Conductor key at Conductor**, and revoke
   the bot's copy with `/revoke` if they want it gone before they do. Step 1–4
   protects future data; it does not un-leak a key an attacker already read.

There is no downtime and no dual-write window: every sealed blob names the key
that sealed it, so old and new coexist.

## If the bot token leaks

Revoke it in BotFather immediately, set the new one, and redeploy. A leaked
token lets an attacker read every message in every group the bot is in.
Rotating it does not remove them from those groups — the token is the bot.
