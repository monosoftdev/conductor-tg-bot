#!/usr/bin/env python3
"""What icons will Telegram actually give this bot's topics?

A topic carries state twice: in its name prefix, and in
``icon_custom_emoji_id`` — the badge beside the row, which is what you scan a
long topic list by. The second one is easy to get silently wrong, and it was:

* Telegram serves bots a **fixed pack** (``getForumTopicIconStickers``) and
  refuses anything outside it.
* An id we cannot supply is not an error. aiogram omits an unset optional, and
  Telegram keeps the *existing* icon for an omitted field — so every rename
  reported success while the icon never moved.

Nothing in the source can answer "is ⚡ in the pack?", so this asks, and prints
what each :class:`TopicMarker` resolves to::

    export TELEGRAM_BOT_TOKEN=...      # the same token the bot runs on
    .venv/bin/python scripts/probe_topic_icons.py

No chat id, no side effects: this only reads. Exit status is non-zero if any
state would fall through to "icon unchanged", because that state is invisible
in the topic list.
"""

from __future__ import annotations

import asyncio
import os
import sys

try:
    from aiogram import Bot
except ImportError:  # pragma: no cover - operator-facing
    sys.exit("aiogram is required:  .venv/bin/python -m pip install aiogram")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ctb.bot.handlers.topics import icon_key, icon_pack  # noqa: E402
from ctb.turn.state import TopicMarker  # noqa: E402


async def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN to the token the bot runs on.")

    bot = Bot(token)
    try:
        pack = await icon_pack(bot)
        if not pack:
            print("\n  Telegram would not serve the icon pack. Nothing to report.\n")
            return 1
        print(f"\n{len(pack)} icons offered:\n")
        print("  " + " ".join(sorted(pack)))

        print("\nPer state — the first wanted emoji the pack carries:\n")
        missing = 0
        for marker in TopicMarker:
            wanted = marker.icons
            chosen = next((e for e in wanted if icon_key(e) in pack), None)
            if chosen is None:
                missing += 1
                detail = f"NONE OF {' '.join(wanted)} — icon will not change"
            else:
                skipped = [e for e in wanted if icon_key(e) not in pack]
                detail = chosen + (
                    f"   (pack lacks {' '.join(skipped)})" if skipped else ""
                )
            print(f"  {marker.value:<13} {detail}")

        if missing:
            print(
                f"\n  → {missing} state(s) resolve to nothing and will keep whatever\n"
                "    icon the topic already had. Add an alternative Telegram does\n"
                "    carry to `_TOPIC_ICONS` in src/ctb/turn/state.py.\n"
            )
            return 1
        print(
            "\n  → Every state has an icon. The topic list can be read at a glance.\n"
        )
        return 0
    finally:
        await bot.session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
