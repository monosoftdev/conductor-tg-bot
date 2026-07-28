# Tenancy

How one bot serves many workspaces without any of them seeing each other.

## The shape

```
                     one Telegram bot token
                              │
              ┌───────────────┴───────────────┐
         Acme's chats                    Rival's chats
        (a DM, maybe a group)          (a DM, maybe a group)
              │                               │
        TenantMiddleware ──── chat_id → tenant, then membership
              │
     ┌────────┴────────┐
 ctb.tenant_id GUC   ClientPool
     │                   │
  app pool (RLS)   Acme's key │ Rival's key
     │                        │
        one PostgreSQL, one Conductor client per key
```

**A Telegram chat belongs to exactly one tenant.** `tenant_chats.chat_id` is
a primary key, which is why resolution is a single point lookup and why every
other table's `chat_id` is already tenant-unique.

**A tenant does not have to own a group.** Since the group became optional
(2026-07-27) the common shape is one tenant, one `tenant_chats` row, and that
row is a **private chat** — `kind='dm'`, `is_primary=true`, written by `/start`
itself. `/setup` adds a `kind='group'` row later and moves `is_primary` to it,
because that is then where the team lives and where owner notices belong.
Nothing else in this document distinguishes the two kinds: the GUC, the
policies, the roles and `primary_chat()` all take a `chat_id`.

Binding the DM at `/start` is load-bearing, not cosmetic. Without a row, a
private chat resolves by *sole* membership — which stops resolving the moment
somebody owns two teams, and `/use`, the command that fixes that, needs a
resolved tenant of its own. The bind is best-effort and non-destructive: a DM
already claimed by another tenant stays where it is, because creating a second
team must not silently re-point the first one's chat.

**Many Telegram users drive one tenant.** A co-founder is a `tenant_members`
row, not a second deployment — and not necessarily a groupmate: an invited
member can drive the team from their own private chat.

## Where the boundary is

`TenantMiddleware` is the only place that decides who someone is. It runs
second in the chain — after log context, before routing and before aiogram's
FSM middleware — because both of those read the database, and a tenant-scoped
query with no tenant in scope raises rather than returning everything.

It resolves in this order:

1. `tenant_chats[chat_id]` → the tenant. No binding means refusal, not
   "unknown yet": a shared bot can be added to any group by anyone.
2. `tenant_members[(tenant, user)]` → the person. Being in the right chat is
   not authorisation.
3. An update with no chat at all (inline query, poll answer, payment callback)
   resolves the way a private message does — by the sender's own membership,
   and only when it is unambiguous.

Rejection is silence. Two exceptions, both of which exist because they are how
somebody *becomes* a member, and both reach a handler with `tenant=None`:

* `/start`, `/register`, `/help`, `/privacy` and `/platform`, in a **private
  chat**. `/start` is now the whole of sign-up, so this is the only door.
* `/setup`, in a **group**. A fresh supergroup has no `tenant_chats` row by
  definition, so without this the optional group flow is unreachable. It is
  safe because it does nothing without a valid single-use code, which only its
  own tenant's owner ever saw (minted by `/team`, stored as a digest, bound to
  the user who asked) — and it proves the bot can really create a topic
  *before* spending that code, so a permissions problem does not leave a group
  permanently unbindable.

`/team` is deliberately **not** on either list: it needs a resolved tenant and
an owner, because it mints a code that binds one.

## Why row-level security

The alternative was threading `tenant_id` through 137 repository call sites.
That is a change whose correctness rests on code review — forever, including
every call site added later.

Instead every tenant-scoped table has:

```sql
tenant_id uuid NOT NULL DEFAULT current_setting('ctb.tenant_id', true)::uuid
ALTER TABLE … ENABLE ROW LEVEL SECURITY;
ALTER TABLE … FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON …
    USING      (tenant_id = current_setting('ctb.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('ctb.tenant_id')::uuid);
```

Consequences worth stating plainly:

- `SELECT … FROM sessions WHERE id = %s` is unchanged and is now tenant-scoped.
- `INSERT INTO deliveries (…)` is unchanged; the column default fills
  `tenant_id`, and `WITH CHECK` rejects anything else.
- A forgotten filter is a zero-row result, not a leak.
- `FORCE` matters: without it the table owner bypasses its own policy.
- `current_setting` is called *without* `missing_ok`, so an unscoped connection
  raises rather than silently matching nothing.

`tests/test_isolation.py` generates its checks from `pg_class` and
`information_schema`, so a table added next year is covered without anyone
remembering to add it.

## The two roles

| | `ctb_app` | `ctb_worker` |
|---|---|---|
| Row-level security | enforced | `BYPASSRLS` |
| Used by | handlers, routing, FSM storage, pollers (inside a scope) | supervisor reconcile, delivery and voice claim loops, prune, tenancy lookups |
| Grants | the nine tenant tables, and `SELECT` on `schema_version`. **No grant at all** on `tenants`, `tenant_members`, `tenant_chats` — the tables that decide scope | everything |
| Member of the other | **no** | no |

The GUC answers "who is this acting for"; the *role* answers "may this
connection see everything". They are deliberately separate — a worker with a
tenant in scope still bypasses RLS, but its inserts pick up the right
`tenant_id`, which is what lets it write on a tenant's behalf.

## The seams that must not be re-cut

Each of these was a global in the single-user design. Putting one back
reintroduces exactly the bug the tests are written to catch.

| Was | Now | If you put it back |
|---|---|---|
| `get_client()` | `TenantContext.client` | handlers silently use the wrong organisation's key |
| `Settings.conductor_api_key` | `tenants.conductor_key_ct`, sealed | one key for everyone |
| `Settings.owner_id` | `tenant_members.role` | one workspace's owner administers all of them |
| `Settings.telegram_chat_id` | `tenant_chats` | one chat — DM or group — per deployment |
| one `TokenBucket` | `TelegramPacer`, global + per chat | one customer's backlog starves the rest |
| `FocusTracker` single slot | bounded dict | one user's thumb overwrites everyone's |
| `auth_fatal` boolean | per-tenant set | one rejected key stops every workspace |

## Secrets

`ctb.crypto.SecretBox`: AES-256-GCM, envelope, AAD-bound to
`(kid, tenant_id, purpose)`.

The AAD binding is the point. Fernet has none, so copying one row's blob onto
another row would decrypt cleanly; here it fails authentication. `purpose`
separates the Conductor key from the speech key on the same row.

There is **no plaintext cache**. `ClientPool.get()` decrypts once, hands the key
to the client's default headers, and drops the reference. The client is the
cache; idle eviction after fifteen minutes destroys the last copy in memory.
`conductor_key_fp` — a truncated HMAC under a key derived from the master,
scoped to the tenant — exists so "is this the same key?" never needs a decrypt.
HMAC rather than a bare digest because the application role can read that
column, and an unsalted hash of an API key is offline-guessable for any key
with predictable structure.

Rotation is prepend, deploy, `python -m ctb.rewrap --rewrap`, drop. Every blob
names the key that sealed it, so old and new coexist and there is no window.

## Fairness

One shared bot token is one shared Telegram rate budget. Two budgets, kept
separate because Telegram has two limits:

- **global**, ~25/s against the token;
- **per chat**, ~15/min, under Telegram's ~20/min per group.

`DestinationRotor` then decides which `(chat, topic)` spends the global budget
next: most urgent first, then least recently served. Fairness is
per-destination rather than per tenant, which is strictly stronger — a customer
with forty busy topics cannot starve one with a single topic, and a runaway
topic cannot starve its own siblings.

A 429 pauses **only the chat that caused it**. Several distinct chats reporting
one within ten seconds escalates to a global pause, on the theory that this is
the signature of a token-level limit rather than a group-level one. Telegram's
error does not distinguish, so that is a documented heuristic, not a fact.

The supervisor applies the same idea to pollers: a per-tenant `max_pollers` and
a process-wide ceiling, allocated round-robin by least-recently-served tenant.
Because the reconcile query orders active turn states first *within* each
tenant, a tenant at its ceiling still gets its busy sessions polled. Reaching
the global ceiling logs `supervisor.pollers_starved`, which is the signal to
shard the lease — the lease name is already a `text` primary key for that.

## Suspension and failure

Both are joins, not code paths:

```sql
JOIN tenants t ON t.id = s.tenant_id
 WHERE t.status = 'active' AND t.auth_failed_at IS NULL
```

Setting `status='suspended'` stops that tenant's polling on the next five-second
pass. A rejected API key stamps `auth_failed_at`, cancels *that tenant's*
pollers, and tells its owners — everyone else keeps running. Storing a new key
clears the stamp, which is the only thing that restarts it.


## What runs with no tenant at all

Three bugs hid here at once, so it is worth stating explicitly. These run as
process-level tasks or before any handler, and therefore have **no scope**:

* aiogram's FSM middleware, which reads storage on *every* update — including
  the registration commands, which by definition have no tenant yet. The
  storage treats "no tenant" as "no wizard": reads answer empty, writes drop.
* The delivery, voice and status-card loops. Each **claims** cross-tenant on
  the worker pool, then enters the claimed row's own tenant to do the work.
* The supervisor's reconcile pass. Each poller runs inside
  ``tenant_scope(session.tenant_id)`` for its whole lifetime.

The rule: **claim wide, act narrow.** A worker may look across tenants to find
work; it may not touch a tenant's rows without entering that tenant.

`tests/pg.py::unscoped` runs a block the way these tasks run, and
`tests/test_unscoped_workers.py` uses it. A fixture that supplies a scope makes
every one of these look fine.

## Callback payloads

Buttons that must survive a redeploy cannot be handles in a dead process's
memory, so they describe themselves: action, expiry, target. That makes every
field attacker-supplied unless it is signed — and unsigned, "single use" and
"expires in fifteen minutes" were decorative, and a ``stop`` payload could name
any session id its sender had ever seen.

They carry a truncated HMAC under a key derived from the master key
(``SecretBox.derive``), so every replica of a deployment agrees and nothing
outside it can mint one. Rotating the master key invalidates in-flight payloads,
which is the safe direction for a button that already expires in minutes.

The signature is a *fallback*, not a bypass: the nonce store is still asked
first, so a ticket it knows — spent, expired, revoked, minted for someone else —
is refused as before.

`stop` additionally reads its session back through the tenant-scoped pool.
``Supervisor.dispatch`` looks up a process-global task map with no tenant of its
own, so that read is the only thing between a stray payload and somebody else's
running agent.

## Roles

``owner`` and ``admin`` both pass the ``is_owner`` command gate, and that is
deliberate — but it means an admin holds most of an owner's power. Two guards
keep the door open:

* Only an **owner** may change an owner's role. Nothing in Telegram grants the
  ``owner`` role, so a demotion is a one-way door.
* The last **owner** cannot be removed, counting the ``owner`` role only.
  Counting admins would let an admin remove the owner and leave a workspace
  nobody can administer.

Anyone can seat anyone with ``/invite``, so ``/leave`` exists and DM resolution
prefers the workspace you own. Without that preference, seating someone in your
workspace would silence every private command they send — including the
``/key`` that finishes their own sign-up.
