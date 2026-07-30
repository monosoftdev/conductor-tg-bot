"""Generate a master key, and re-seal stored secrets under the active one.

Rotation has three steps and no downtime:

1. ``python -m ctb.rewrap --new-key v2`` and prepend the result to
   ``CTB_MASTER_KEYS``. Deploy. New writes use ``v2``; ``v1`` still opens
   everything already stored, because every blob names the key that sealed it.
2. ``python -m ctb.rewrap --rewrap`` re-seals every row still on an older key.
   It only re-encrypts the wrapped data key — 48 bytes — never the payload.
3. Drop ``v1`` from ``CTB_MASTER_KEYS`` on the next deploy.

There is no dual-write window and no atomic swap, which is the point.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, LiteralString

import psycopg

from ctb.crypto import SecretBox, generate_master_key

__all__ = ["RewrapError", "main", "rewrap"]


class RewrapError(RuntimeError):
    """Rotation cannot proceed — wrong role, or a blob that will not open."""


#: ``(select, update, purpose)``. The purpose must match the one used when the
#: blob was sealed, or the AAD check fails — which is the whole design. The SQL
#: is written out rather than interpolated so nothing here builds a query from
#: a variable, even a module-level one.
#:
#: The UPDATE rewrites the fingerprint as well as the ciphertext.
#: :meth:`SecretBox.fingerprint_of` is keyed by a subkey derived from the
#: *active* master key, so after a rotation every stored ``*_key_fp`` was
#: computed under a key that no longer exists. Leaving it stale is not a leak —
#: the pools only need the value to be stable — but re-sending an unchanged key
#: then stops matching, and ``/key`` answers "stored" instead of "that is
#: already the stored key". We hold the plaintext here anyway; recomputing it
#: costs one HMAC.
_COLUMNS: tuple[tuple[LiteralString, LiteralString, str], ...] = (
    (
        "SELECT id, conductor_key_ct, conductor_key_kid FROM tenants "
        "WHERE conductor_key_ct IS NOT NULL",
        "UPDATE tenants SET conductor_key_ct = %s, conductor_key_kid = %s, "
        "conductor_key_fp = %s WHERE id = %s",
        "conductor_api_key",
    ),
    (
        "SELECT id, elevenlabs_key_ct, elevenlabs_key_kid FROM tenants "
        "WHERE elevenlabs_key_ct IS NOT NULL",
        "UPDATE tenants SET elevenlabs_key_ct = %s, elevenlabs_key_kid = %s, "
        "elevenlabs_key_fp = %s WHERE id = %s",
        "elevenlabs_api_key",
    ),
    (
        "SELECT id, github_key_ct, github_key_kid FROM tenants "
        "WHERE github_key_ct IS NOT NULL",
        "UPDATE tenants SET github_key_ct = %s, github_key_kid = %s, "
        "github_key_fp = %s WHERE id = %s",
        "github_api_token",
    ),
)


def assert_bypasses_rls(conn: psycopg.Connection[Any]) -> None:
    """Refuse to run under a role row-level security applies to.

    ``ctb_app`` holds no grant on ``tenants`` at all, so the wrong DSN already
    fails — with ``permission denied for table tenants``, three steps into a
    breach runbook, saying nothing about which of two DSNs to reach for. Asking
    the role first turns that into a sentence naming the fix.

    It is also the honest invariant rather than a proxy for one. Any role that
    evaluates the isolation policy sees no tenant rows without a scope, and a
    rotation that reports ``re-sealed 0 secret(s)`` and exits 0 while every key
    is still sealed under the leaked master is the one failure this tool must
    not have.
    """
    row = conn.execute(
        "SELECT current_user, rolbypassrls OR rolsuper "
        "FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if row is None or not row[1]:
        who = row[0] if row else "unknown"
        raise RewrapError(
            f"rewrap is connected as {who!r}, which row-level security applies "
            "to: every tenant row would be invisible and this would report "
            "success having re-sealed nothing. Use SYSTEM_DATABASE_URL (the "
            "ctb_worker role), which holds BYPASSRLS."
        )


def rewrap(dsn: str, box: SecretBox) -> int:
    """Re-seal every secret not already on the active key. Returns the count."""
    changed = 0
    with psycopg.connect(dsn) as conn:
        assert_bypasses_rls(conn)
        for select, update, purpose in _COLUMNS:
            rows = conn.execute(select).fetchall()
            for tenant_id, blob, kid in rows:
                if not box.needs_rewrap(kid):
                    continue
                plaintext = box.open(bytes(blob), tenant_id=tenant_id, purpose=purpose)
                resealed = box.seal(plaintext, tenant_id=tenant_id, purpose=purpose)
                fingerprint = box.fingerprint_of(plaintext, tenant_id=tenant_id)
                del plaintext
                conn.execute(update, (resealed, box.active_kid, fingerprint, tenant_id))
                changed += 1
        conn.commit()
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--new-key",
        metavar="KID",
        help="print a fresh '<kid>:<base64>' entry and exit",
    )
    parser.add_argument("--rewrap", action="store_true", help="re-seal stored keys")
    parser.add_argument("--dsn", help="defaults to SYSTEM_DATABASE_URL")
    args = parser.parse_args(argv)

    if args.new_key:
        print(generate_master_key(args.new_key))
        return 0

    if not args.rewrap:
        parser.error("pass --new-key KID or --rewrap")

    from ctb.settings import get_settings

    settings = get_settings()
    box = settings.secret_box()
    box.self_check()
    dsn = args.dsn or settings.system_database_url.get_secret_value()
    try:
        count = rewrap(dsn, box)
    except RewrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"re-sealed {count} secret(s) under {box.active_kid}")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    sys.exit(main())
