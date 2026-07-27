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
from typing import LiteralString

import psycopg

from ctb.crypto import SecretBox, generate_master_key

__all__ = ["main", "rewrap"]

#: ``(select, update, purpose)``. The purpose must match the one used when the
#: blob was sealed, or the AAD check fails — which is the whole design. The SQL
#: is written out rather than interpolated so nothing here builds a query from
#: a variable, even a module-level one.
_COLUMNS: tuple[tuple[LiteralString, LiteralString, str], ...] = (
    (
        "SELECT id, conductor_key_ct, conductor_key_kid FROM tenants "
        "WHERE conductor_key_ct IS NOT NULL",
        "UPDATE tenants SET conductor_key_ct = %s, conductor_key_kid = %s "
        "WHERE id = %s",
        "conductor_api_key",
    ),
    (
        "SELECT id, elevenlabs_key_ct, elevenlabs_key_kid FROM tenants "
        "WHERE elevenlabs_key_ct IS NOT NULL",
        "UPDATE tenants SET elevenlabs_key_ct = %s, elevenlabs_key_kid = %s "
        "WHERE id = %s",
        "elevenlabs_api_key",
    ),
)


def rewrap(dsn: str, box: SecretBox) -> int:
    """Re-seal every secret not already on the active key. Returns the count."""
    changed = 0
    with psycopg.connect(dsn) as conn:
        for select, update, purpose in _COLUMNS:
            rows = conn.execute(select).fetchall()
            for tenant_id, blob, kid in rows:
                if not box.needs_rewrap(kid):
                    continue
                plaintext = box.open(bytes(blob), tenant_id=tenant_id, purpose=purpose)
                resealed = box.seal(plaintext, tenant_id=tenant_id, purpose=purpose)
                del plaintext
                conn.execute(update, (resealed, box.active_kid, tenant_id))
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
    count = rewrap(dsn, box)
    print(f"re-sealed {count} secret(s) under {box.active_kid}")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    sys.exit(main())
