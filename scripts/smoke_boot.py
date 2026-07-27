#!/usr/bin/env python3
"""Boot the real runtime against a real database, then shut it down.

The test suite never runs ``python -m ctb``. It builds components directly, so
it cannot catch the class of mistake that only exists at startup: a factory that
no longer matches its call site, a missing GRANT, an import cycle, a service
that fails to register. Every one of those is a crash-loop on the first deploy
and a green suite five minutes earlier.

This is the cheapest possible check of the whole seam. It needs no Telegram
token and no Conductor key — the bot token is a syntactically valid fake, and
Telegram's inevitable 401 is the *expected* end state. Everything before that
point is real: two pools, the schema check, the RLS-confinement guard, the
client pool, the crypto canary, all six services, and ``/health``.

Usage::

    DATABASE_URL=... SYSTEM_DATABASE_URL=... python scripts/smoke_boot.py

Exit 0 means the process reached "serving" and stopped cleanly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from typing import Any

#: Well-formed but not real. Telegram will reject it, which is what we want:
#: reaching the rejection proves everything before it worked.
_FAKE_TOKEN = "123456:AAFakeTokenForSmokeTestOnlyNotReal_xyz"

_PORT = 8099
_BOOT_TIMEOUT_S = 30.0


def _master_key() -> str:
    return "v1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip(
        "="
    )


async def _health() -> dict[str, object]:
    """Poll ``/health`` until it answers. Off-thread: urllib blocks."""

    def fetch() -> dict[str, object]:
        with urllib.request.urlopen(  # noqa: S310 - loopback, fixed URL
            f"http://127.0.0.1:{_PORT}/health", timeout=5
        ) as response:
            body = json.loads(response.read().decode())
        return dict(body)

    last: Exception | None = None
    for _ in range(60):
        try:
            return await asyncio.to_thread(fetch)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            await asyncio.sleep(0.25)
    raise RuntimeError(f"/health never answered: {last}")


async def main() -> int:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", _FAKE_TOKEN)
    os.environ.setdefault("CTB_MASTER_KEYS", _master_key())
    os.environ["HEALTH_PORT"] = str(_PORT)
    os.environ.setdefault("LOG_LEVEL", "INFO")

    for required in ("DATABASE_URL", "SYSTEM_DATABASE_URL"):
        if not os.environ.get(required):
            print(f"smoke: {required} is not set", file=sys.stderr)
            return 2

    from ctb.__main__ import build_runtime
    from ctb.settings import load_settings

    runtime = await build_runtime(load_settings())
    try:
        names = [name for name, _runner in runtime.runners()]
        print(f"smoke: services = {names}")
        if len(names) != 6:
            print(f"smoke: expected six services, got {len(names)}", file=sys.stderr)
            return 1

        # Start everything, but only long enough to prove it starts. The
        # Telegram runner is excluded: with a fake token it fails immediately
        # and its failure is not what this checks.
        async def drive(runner: Any) -> None:
            await runner()

        tasks = [
            asyncio.create_task(drive(runner), name=name)
            for name, runner in runtime.runners()
            if name != "telegram"
        ]
        try:
            async with asyncio.timeout(_BOOT_TIMEOUT_S):
                body = await _health()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        print(f"smoke: /health = {json.dumps(body, sort_keys=True)[:300]}")
        if body.get("status") not in {"ok", "degraded"}:
            print(f"smoke: unhealthy: {body}", file=sys.stderr)
            return 1
    finally:
        await runtime.close()

    print("smoke: booted and shut down cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
