#!/usr/bin/env python3
"""Phase 0 probe for **topics in a private chat** — run before building on them.

Telegram's protocol docs say a bot can create, rename and delete topics in a DM
with no admin rights and no Premium::

    "The bot will be able to create, modify and delete bot forum topics in
     chats with users."

and that the @BotFather toggle *"Disallow users to create new threads"* governs
whether the **user** may also do so — the bot always may.

But there is an open regression: after the Bot API 10.0 rollout (2026-05-08),
``sendMessage`` with ``message_thread_id`` in private chats began returning
*"message thread not found"* for threads that worked hours earlier, and at least
one report has ``createForumTopic`` failing in DMs too. Documentation describes
intent; only a call describes behaviour.

So this asks the API directly, and cleans up after itself.

    export TELEGRAM_BOT_TOKEN=...      # the same token the bot runs on
    export TELEGRAM_DM_CHAT_ID=...     # your own Telegram user id
    .venv/bin/python scripts/probe_dm_topics.py

Send the bot a DM first — Telegram refuses to open a conversation a user has
never started. Threaded Mode must be ON in @BotFather.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

try:
    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError
except ImportError:  # pragma: no cover - operator-facing
    sys.exit("aiogram is required:  .venv/bin/python -m pip install aiogram")

PROBE_NAME = "ctb probe · delete me"


def _report(step: str, ok: bool | None, detail: str) -> None:
    mark = "PASS" if ok else ("FAIL" if ok is False else "INFO")
    print(f"  [{mark}] {step}: {detail}")


async def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw_chat = os.environ.get("TELEGRAM_DM_CHAT_ID", "").strip()
    if not token or not raw_chat:
        sys.exit(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_DM_CHAT_ID (your own user id).\n"
            "Get your id from @userinfobot, and DM the bot once first."
        )
    chat_id = int(raw_chat)

    bot = Bot(token)
    verdict = 1
    thread_id: int | None = None
    try:
        me = await bot.get_me()
        print(f"\nBot @{me.username} · DM chat {chat_id}\n")

        # Does this bot even have threaded mode on? `has_topics_enabled` is
        # reported on the *user* the bot is talking to.
        try:
            chat = await bot.get_chat(chat_id)
            flag = getattr(chat, "has_topics_enabled", None)
            _report(
                "0 threaded mode reported",
                None,
                f"has_topics_enabled={flag!r} (None just means not reported here)",
            )
        except TelegramAPIError as exc:
            _report("0 threaded mode reported", None, f"could not read chat: {exc}")

        # 1 — the load-bearing one. Everything else is moot if this fails.
        try:
            topic: Any = await bot.create_forum_topic(chat_id=chat_id, name=PROBE_NAME)
            thread_id = int(topic.message_thread_id)
            _report("1 bot creates a topic in a DM", True, f"thread_id={thread_id}")
        except TelegramAPIError as exc:
            _report("1 bot creates a topic in a DM", False, str(exc))
            print(
                "\n  → DM topics are not usable from this bot right now.\n"
                "    BOT_FORUM_CREATE_FORBIDDEN means the *user* is blocked, not the\n"
                "    bot; anything else is the Bot API 10.0 regression or a disabled\n"
                "    Threaded Mode. Do not build the DM-topic flow on this yet.\n"
            )
            return 1

        # 2 — the regression in issue #847 is exactly here.
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text="Probe: this landed inside the thread.",
            )
            _report(
                "2 send into that thread",
                sent.message_thread_id == thread_id,
                f"message_thread_id={sent.message_thread_id}",
            )
        except TelegramAPIError as exc:
            _report("2 send into that thread", False, str(exc))
            print(
                "\n  → This is the known Bot API 10.0 regression: the topic exists\n"
                "    but cannot be addressed. A topic we cannot deliver into is\n"
                "    worse than no topic.\n"
            )

        # Past step 1 this is an int — that branch returns on failure.
        thread: int = thread_id

        # 3 — renaming is how state reaches the topic list.
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread,
                name=f"✅ {PROBE_NAME}",
            )
            _report("3 rename the topic", True, "edit_forum_topic accepted")
        except TelegramAPIError as exc:
            _report("3 rename the topic", False, str(exc))

        # 4 — the state icon, which the group flow already uses.
        try:
            stickers = await bot.get_forum_topic_icon_stickers()
            wanted = {"✅", "⚡", "⌛", "❗", "💤", "🏁"}
            found = {
                s.emoji: s.custom_emoji_id
                for s in stickers
                if getattr(s, "emoji", None) in wanted
            }
            await bot.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread,
                name=f"✅ {PROBE_NAME}",
                icon_custom_emoji_id=found.get("✅"),
            )
            _report(
                "4 set the state icon",
                bool(found),
                f"{len(found)}/{len(wanted)} of our icons exist in the free pack",
            )
        except TelegramAPIError as exc:
            _report("4 set the state icon", False, str(exc))

        verdict = 0
    finally:
        if thread_id is not None:
            try:
                await bot.delete_forum_topic(
                    chat_id=chat_id, message_thread_id=thread_id
                )
                _report("5 clean up", True, "probe topic deleted")
            except TelegramAPIError as exc:
                _report("5 clean up", False, f"delete it by hand: {exc}")
        await bot.session.close()
    return verdict


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
