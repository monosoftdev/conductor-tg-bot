from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetForumTopicIconStickers

from ctb import signals
from ctb.bot.actions import BotActionSink
from ctb.bot.handlers import topics
from ctb.bot.handlers.core import status_icon
from ctb.db.connection import Database
from ctb.db.repo import prompts, sessions, workspaces
from ctb.delivery.outbox import Priority
from ctb.delivery.status_card import CARD_EMOJI
from ctb.turn.state import (
    CardKind,
    Finalize,
    Notify,
    NotifyLevel,
    SetTopicMarker,
    TopicMarker,
    TurnSummary,
)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def handle(self, actions: Any, **kwargs: Any) -> None:
        self.calls.append(("cards", (tuple(actions), kwargs)))

    async def enqueue_notice(self, html: str, **kwargs: Any) -> int:
        self.calls.append(("notice", (html, kwargs)))
        return 1


class Bot:
    def __init__(self) -> None:
        self.renames: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []

    async def edit_forum_topic(self, **kwargs: Any) -> None:
        self.renames.append(kwargs)

    async def set_message_reaction(self, **kwargs: Any) -> None:
        self.reactions.append(kwargs)


async def _bound(db: Database) -> None:
    await workspaces.upsert(
        db,
        "ws-1",
        chat_id=-1001,
        topic_id=42,
        topic_name="api/main",
    )
    await workspaces.set_topic_marker(db, "ws-1", TopicMarker.IDLE.value)
    await sessions.upsert(
        db,
        "sess-1",
        workspace_id="ws-1",
        chat_id=-1001,
        thread_id=42,
    )


async def test_sink_fans_out_cards_notice_and_topic_marker(
    db: Database, system_db: Database
) -> None:
    await _bound(db)
    recorder = Recorder()
    bot = Bot()
    sink = BotActionSink(bot, db, system_db, recorder, recorder)  # type: ignore[arg-type]

    await sink.handle(
        (
            Notify("API unavailable", NotifyLevel.LOUD, once_key="api-down"),
            SetTopicMarker(TopicMarker.ERROR),
        ),
        session_id="sess-1",
        chat_id=-1001,
        thread_id=42,
    )

    assert recorder.calls[0][0] == "cards"
    _, (html, notice) = recorder.calls[1]
    assert html == "API unavailable"
    assert notice["key"] == "api-down"
    assert notice["priority"] is Priority.ERROR
    assert notice["silent"] is False
    assert bot.renames == [
        {
            "chat_id": -1001,
            "message_thread_id": 42,
            "name": "⚠️ api/main",
            "icon_custom_emoji_id": None,
        }
    ]
    workspace = await workspaces.get(db, "ws-1")
    assert workspace is not None
    assert workspace.topic_marker == TopicMarker.ERROR.value


async def test_quiet_notice_is_silent_and_deduped_by_stable_key(
    db: Database,
    system_db: Database,
) -> None:
    await _bound(db)
    recorder = Recorder()
    sink = BotActionSink(Bot(), db, system_db, recorder, recorder)  # type: ignore[arg-type]

    action = Notify("Still working", NotifyLevel.QUIET)
    await sink.handle(
        (action,),
        session_id="sess-1",
        chat_id=-1001,
        thread_id=42,
    )
    await sink.handle(
        (action,),
        session_id="sess-1",
        chat_id=-1001,
        thread_id=42,
    )

    notices = [payload for kind, payload in recorder.calls if kind == "notice"]
    assert notices[0][1]["key"] == notices[1][1]["key"]
    assert notices[0][1]["silent"] is True


async def test_finalize_turns_prompt_receipt_into_completion_reaction(
    db: Database,
    system_db: Database,
) -> None:
    await _bound(db)
    prompt = await prompts.create(
        db,
        session_id="sess-1",
        body="Build it",
        chat_id=-1001,
        thread_id=42,
        tg_message_id=77,
    )
    await prompts.mark_witnessed(db, prompt.message_id)
    recorder = Recorder()
    bot = Bot()
    sink = BotActionSink(bot, db, system_db, recorder, recorder)  # type: ignore[arg-type]

    await sink.handle(
        (Finalize(TurnSummary(prompts=1, ok=True)),),
        session_id="sess-1",
        chat_id=-1001,
        thread_id=42,
    )

    assert bot.reactions[0]["message_id"] == 77
    assert bot.reactions[0]["reaction"][0].emoji == "👍"


class _IconBot(Bot):
    """A bot whose topic-icon pack Telegram will actually serve."""

    def __init__(self, *, pack: bool = True) -> None:
        super().__init__()
        self._pack = pack
        self.pack_calls = 0

    async def get_forum_topic_icon_stickers(self) -> list[Any]:
        self.pack_calls += 1
        if not self._pack:
            raise TelegramBadRequest(
                method=GetForumTopicIconStickers(), message="Bad Request: nope"
            )
        return [
            SimpleNamespace(emoji="✅", custom_emoji_id="id-done"),
            SimpleNamespace(emoji="⚡", custom_emoji_id="id-working"),
        ]


async def test_a_finished_topic_is_marked_done_not_left_blank(db: Database) -> None:
    """The blank IDLE prefix made "finished" and "nothing here" identical.

    That distinction is the reason to look at the topic list at all.
    """
    await _bound(db)
    bot = _IconBot()
    topics._ICON_IDS.clear()

    renamed = await topics.apply_marker(bot, db, "ws-1", TopicMarker.DONE)  # type: ignore[arg-type]

    assert renamed is True
    assert bot.renames[0]["name"].startswith(signals.DONE)
    # The icon rides along on the rename that was happening anyway.
    assert bot.renames[0]["icon_custom_emoji_id"] == "id-done"


async def test_an_unfetchable_icon_pack_still_renames(db: Database) -> None:
    """A missing icon is cosmetic. A skipped rename is a topic that lies."""
    await _bound(db)
    bot = _IconBot(pack=False)
    topics._ICON_IDS.clear()

    renamed = await topics.apply_marker(bot, db, "ws-1", TopicMarker.WORKING)  # type: ignore[arg-type]

    assert renamed is True
    assert bot.renames[0]["name"].startswith(signals.WORKING)
    assert bot.renames[0]["icon_custom_emoji_id"] is None


async def test_renaming_to_the_same_title_costs_no_api_call(db: Database) -> None:
    """The guard compared markers, so `/name -w` with an unchanged name paid."""
    await _bound(db)
    bot = _IconBot()
    topics._ICON_IDS.clear()

    double: Any = bot
    first = await topics.apply_marker(
        double, db, "ws-1", TopicMarker.IDLE, label="api/main"
    )
    second = await topics.apply_marker(
        double, db, "ws-1", TopicMarker.IDLE, label="api/main"
    )

    assert (first, second) == (False, False), "same marker, same name, no call"
    assert bot.renames == []
    # A genuinely different name still renames.
    assert await topics.apply_marker(
        double, db, "ws-1", TopicMarker.IDLE, label="api/dev"
    )


def test_every_surface_uses_one_glyph_per_state() -> None:
    """Topic prefix, card and /board disagreed on every single state."""
    assert (
        TopicMarker.WORKING.prefix.strip() == signals.WORKING == status_icon("working")
    )
    assert TopicMarker.ERROR.prefix.strip() == signals.ERROR == status_icon("error")
    assert TopicMarker.DONE.prefix.strip() == signals.DONE == status_icon("idle")
    assert (
        TopicMarker.SLEEPING.prefix.strip()
        == signals.SLEEPING
        == status_icon("sleeping")
    )
    assert CARD_EMOJI[CardKind.WORKING] == signals.WORKING
    assert CARD_EMOJI[CardKind.DONE] == signals.DONE
    assert CARD_EMOJI[CardKind.ERROR] == signals.ERROR


def test_the_reaction_vocabulary_is_one_telegram_accepts() -> None:
    """✅ and ⏳ are not valid reactions; reusing the card glyphs would 400."""
    assert signals.DONE not in signals.REACTION_SAFE
    assert signals.WAITING not in signals.REACTION_SAFE
    assert {"👀", "👍", "😢"} <= signals.REACTION_SAFE, "what the bot uses today"
