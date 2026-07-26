from __future__ import annotations

from typing import Any

from ctb.bot.actions import BotActionSink
from ctb.db.connection import Database
from ctb.db.repo import prompts, sessions, workspaces
from ctb.delivery.outbox import Priority
from ctb.turn.state import (
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


async def test_sink_fans_out_cards_notice_and_topic_marker(db: Database) -> None:
    await _bound(db)
    recorder = Recorder()
    bot = Bot()
    sink = BotActionSink(bot, db, recorder, recorder)  # type: ignore[arg-type]

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
            "name": "! api/main",
        }
    ]
    workspace = await workspaces.get(db, "ws-1")
    assert workspace is not None
    assert workspace.topic_marker == TopicMarker.ERROR.value


async def test_quiet_notice_is_silent_and_deduped_by_stable_key(
    db: Database,
) -> None:
    await _bound(db)
    recorder = Recorder()
    sink = BotActionSink(Bot(), db, recorder, recorder)  # type: ignore[arg-type]

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
    sink = BotActionSink(bot, db, recorder, recorder)  # type: ignore[arg-type]

    await sink.handle(
        (Finalize(TurnSummary(prompts=1, ok=True)),),
        session_id="sess-1",
        chat_id=-1001,
        thread_id=42,
    )

    assert bot.reactions[0]["message_id"] == 77
    assert bot.reactions[0]["reaction"][0].emoji == "👍"
