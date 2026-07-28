from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Final

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetForumTopicIconStickers

from ctb import signals
from ctb.bot.actions import BotActionSink, finish_line
from ctb.bot.handlers import topics
from ctb.bot.handlers.core import status_icon
from ctb.db.connection import Database
from ctb.db.repo import chats, prompts, sessions, workspaces
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


# ── one buzz per turn, at the end of it ──────────────────────────────────────


def _notices(recorder: Recorder) -> list[tuple[str, dict[str, Any]]]:
    return [payload for kind, payload in recorder.calls if kind == "notice"]


async def test_a_finished_turn_is_the_only_thing_that_buzzes(
    db: Database, system_db: Database
) -> None:
    """**The point of the change.**

    Every line a turn emits is delivered silently under the default `quiet`,
    because a phone that buzzes eight times for one task is a phone you mute.
    This is the one push, and it fires when the work is done — which is also
    where the per-file counts went after they left the chat.
    """
    await _bound(db)
    recorder = Recorder()
    sink = BotActionSink(Bot(), db, system_db, recorder, recorder)  # type: ignore[arg-type]

    await sink.handle(
        (
            Finalize(
                TurnSummary(
                    duration_ms=92_000, tool_calls=12, files_changed=5, prompts=1
                )
            ),
        ),
        session_id="sess-1",
        chat_id=-1001,
        thread_id=42,
    )

    notices = _notices(recorder)
    assert len(notices) == 1
    html, sent = notices[0]
    assert html == "✅ <b>Done</b> · 1m32s · 12 tools · 5 files"
    assert sent["silent"] is False, "this is the notification"
    assert sent["chat_id"] == -1001 and sent["thread_id"] == 42


async def test_the_same_turn_is_never_announced_twice(
    db: Database, system_db: Database
) -> None:
    """A `Finalize` re-derived after a redeploy must land on the same row."""
    await _bound(db)
    recorder = Recorder()
    sink = BotActionSink(Bot(), db, system_db, recorder, recorder)  # type: ignore[arg-type]
    action = Finalize(TurnSummary(duration_ms=1000, prompts=1))

    for _ in range(2):
        await sink.handle((action,), session_id="sess-1", chat_id=-1001, thread_id=42)

    keys = {sent["key"] for _html, sent in _notices(recorder)}
    assert len(keys) == 1, "the outbox dedupes on this key; it must be stable"


async def test_notify_off_gets_no_completion_buzz_either(
    db: Database, system_db: Database
) -> None:
    """`off` means off. A setting that leaks one push is not a setting."""
    await _bound(db)
    await chats.ensure(db, -1001, 42, kind="topic")
    await chats.set_notify(db, -1001, 42, notify="off")
    recorder = Recorder()
    sink = BotActionSink(Bot(), db, system_db, recorder, recorder)  # type: ignore[arg-type]

    await sink.handle(
        (Finalize(TurnSummary(prompts=1)),),
        session_id="sess-1",
        chat_id=-1001,
        thread_id=42,
    )

    assert _notices(recorder) == []


async def test_a_failed_turn_leads_with_the_reason(
    db: Database, system_db: Database
) -> None:
    assert finish_line(TurnSummary(ok=False, error="rate limited")) == (
        "⚠️ <b>Stopped</b> · rate limited"
    )
    # One file is a file, not "1 files".
    assert finish_line(TurnSummary(files_changed=1)) == "✅ <b>Done</b> · 1 file"
    # A turn that did nothing measurable still says it finished.
    assert finish_line(TurnSummary()) == "✅ <b>Done</b>"


async def test_an_unenqueueable_receipt_never_takes_down_the_turn(
    db: Database, system_db: Database
) -> None:
    """The receipt describes the work. It may not be able to destroy it."""
    await _bound(db)

    class Exploding(Recorder):
        async def enqueue_notice(self, html: str, **kwargs: Any) -> int:
            raise RuntimeError("outbox is down")

    recorder = Exploding()
    sink = BotActionSink(Bot(), db, system_db, recorder, recorder)  # type: ignore[arg-type]

    await sink.handle(
        (Finalize(TurnSummary(prompts=1)),),
        session_id="sess-1",
        chat_id=-1001,
        thread_id=42,
    )


#: What a real ``getForumTopicIconStickers`` looks like enough of: every state
#: served, and — as Telegram genuinely does — some of them carrying the U+FE0F
#: presentation selector the state table writes without.
_FULL_PACK: Final[tuple[tuple[str, str], ...]] = (
    ("✅", "id-done"),
    ("⚡️", "id-working"),
    ("⌛", "id-initializing"),
    ("💭", "id-idle"),
    ("❗️", "id-error"),
    ("💤", "id-sleeping"),
    ("🏁", "id-archived"),
)


class _IconBot(Bot):
    """A bot whose topic-icon pack Telegram will actually serve."""

    def __init__(
        self,
        *,
        pack: bool = True,
        icons: tuple[tuple[str, str], ...] = _FULL_PACK,
    ) -> None:
        super().__init__()
        self._pack = pack
        self._icons = icons
        self.pack_calls = 0

    async def get_forum_topic_icon_stickers(self) -> list[Any]:
        self.pack_calls += 1
        if not self._pack:
            raise TelegramBadRequest(
                method=GetForumTopicIconStickers(), message="Bad Request: nope"
            )
        return [
            SimpleNamespace(emoji=emoji, custom_emoji_id=icon_id)
            for emoji, icon_id in self._icons
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


async def test_the_icon_moves_with_the_state_not_just_the_name(db: Database) -> None:
    """**The point of the icon channel.**

    The badge beside the row is what you scan a topic list by, and it was
    landing on every state as the same emoji. A transition has to move both
    channels or the list stops answering the question you open it for.
    """
    await _bound(db)
    bot = _IconBot()
    topics._ICON_IDS.clear()
    double: Any = bot

    for marker in (TopicMarker.WORKING, TopicMarker.DONE, TopicMarker.SLEEPING):
        assert await topics.apply_marker(double, db, "ws-1", marker)

    assert [rename["icon_custom_emoji_id"] for rename in bot.renames] == [
        "id-working",
        "id-done",
        "id-sleeping",
    ]
    assert bot.pack_calls == 1, "the pack is a Telegram constant, fetched once"


async def test_a_pack_that_spells_an_emoji_with_a_selector_still_matches(
    db: Database,
) -> None:
    """The silent failure, in one line.

    Telegram serves ``⚡️`` (U+26A1 U+FE0F); the state table asks for ``⚡``.
    An exact-string lookup missed, returned ``None``, and aiogram then omitted
    the field — for which Telegram keeps the *existing* icon. Every rename
    reported success and no icon ever moved.
    """
    await _bound(db)
    bot = _IconBot(icons=(("⚡️", "id-working"),))
    topics._ICON_IDS.clear()

    assert await topics.apply_marker(bot, db, "ws-1", TopicMarker.WORKING)  # type: ignore[arg-type]

    assert bot.renames[0]["icon_custom_emoji_id"] == "id-working"


async def test_a_state_falls_back_to_an_emoji_the_pack_does_carry(
    db: Database,
) -> None:
    """Nobody can read the pack from the source, so a state names alternatives."""
    await _bound(db)
    # No ⚡ — the second choice for WORKING is 🛠.
    bot = _IconBot(icons=(("🛠", "id-tools"),))
    topics._ICON_IDS.clear()

    assert await topics.apply_marker(bot, db, "ws-1", TopicMarker.WORKING)  # type: ignore[arg-type]

    assert bot.renames[0]["icon_custom_emoji_id"] == "id-tools"


async def test_a_pack_of_an_unexpected_shape_still_renames(db: Database) -> None:
    """``getForumTopicIconStickers`` is untyped at the wire and every field is
    optional, so a shape we cannot walk has to be as survivable as a 500."""
    await _bound(db)
    bot = _IconBot()
    bot.get_forum_topic_icon_stickers = _not_a_list  # type: ignore[method-assign]
    topics._ICON_IDS.clear()

    assert await topics.apply_marker(bot, db, "ws-1", TopicMarker.WORKING)  # type: ignore[arg-type]

    assert bot.renames[0]["name"].startswith(signals.WORKING)
    assert bot.renames[0]["icon_custom_emoji_id"] is None
    assert topics._ICON_IDS == {}, "a half-parsed pack is not a pack"


async def _not_a_list() -> Any:
    return True


async def test_a_pack_carrying_none_of_them_still_renames(db: Database) -> None:
    """A missing icon is cosmetic. A skipped rename is a topic that lies."""
    await _bound(db)
    bot = _IconBot(icons=(("🍕", "id-pizza"),))
    topics._ICON_IDS.clear()

    assert await topics.apply_marker(bot, db, "ws-1", TopicMarker.WORKING)  # type: ignore[arg-type]

    assert bot.renames[0]["name"].startswith(signals.WORKING)
    assert bot.renames[0]["icon_custom_emoji_id"] is None


def test_no_two_states_wear_the_same_first_icon() -> None:
    """``IDLE`` and ``SLEEPING`` both asked for 💤.

    A topic is idle most of its life, so one shared sleep badge *was* most of
    what "all the icons are the same" looked like from the topic list.
    """
    first = [marker.icons[0] for marker in TopicMarker]

    assert all(marker.icons for marker in TopicMarker), "every state wants an icon"
    assert len(set(first)) == len(first), f"duplicate state icons: {first}"
    assert TopicMarker.IDLE.icons[0] != TopicMarker.SLEEPING.icons[0]


def test_an_icon_is_compared_by_identity_not_presentation() -> None:
    assert topics.icon_key("⚡️") == topics.icon_key("⚡") == "⚡"
    assert topics.icon_key("✅") == "✅", "an emoji with no selector is untouched"


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
