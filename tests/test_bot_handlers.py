"""Focused safety tests for the Telegram command surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import CreateForumTopic, SendMessage
from aiogram.types import Chat, Message

from ctb.bot.app import SqliteStorage
from ctb.bot.handlers import core as core_handlers
from ctb.bot.handlers import prompts as prompt_handlers
from ctb.bot.handlers.common import (
    MOBILE_REPLY_INSTRUCTION,
    CreateRequest,
    augment_prompt,
    create_and_bind,
    create_and_bind_input,
    react_received,
    request_cancel,
    submit_prompt,
    tell,
)
from ctb.bot.handlers.core import (
    ARCHIVE_CONSEQUENCE,
    BOARD_VISIBLE,
    FIND_VISIBLE,
    board_lines,
    find_query,
    find_text,
    normalize_find_term,
    session_overview_lines,
    status_icon,
)
from ctb.bot.handlers.power import switchable_sessions
from ctb.bot.handlers.topics import (
    TOPIC_ICON_COLORS,
    TopicCreateError,
    send_html,
    telegram_reason,
    topic_icon_color,
)
from ctb.bot.keyboards import (
    RESTARTABLE_ACTIONS,
    Action,
    NonceError,
    NonceStore,
    read_stateless,
)
from ctb.bot.middleware.routing import Route
from ctb.bot.wizards import new_workspace
from ctb.conductor.models import (
    PostMessageResult,
    PostState,
    Project,
    ProjectsPage,
    Session,
    SessionsPage,
    SqlResult,
    TranscriptMessage,
    WorkspaceCreateResult,
    WorkspacesPage,
)
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import prompts as prompts_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.chats import ChatRow
from ctb.db.repo.sessions import SessionRow
from ctb.db.repo.workspaces import WorkspaceRow
from ctb.settings import Settings
from ctb.turn.cursor import quick_replies_for
from ctb.turn.state import Cancel, TurnState


class PromptClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    async def post_message(
        self, session_id: str, text: str, message_id: str
    ) -> PostMessageResult:
        self.posts.append((session_id, text, message_id))
        return PostMessageResult(message_id=message_id, state=PostState.QUEUED)


class CancelSupervisor:
    def __init__(self, result: object | None = object()) -> None:
        self.result = result
        self.calls: list[tuple[str, object]] = []

    async def dispatch(self, session_id: str, evidence: object) -> object | None:
        self.calls.append((session_id, evidence))
        return self.result


class _NullState:
    """``abandon_wizard`` only asks whether a wizard is open."""

    async def get_state(self) -> str | None:
        return None

    async def clear(self) -> None:
        return None


class _ForumBot:
    """Just enough of ``Bot`` for ``forum_support`` plus the topic lifecycle."""

    def __init__(
        self,
        *,
        is_forum: bool = True,
        can_manage_topics: bool = True,
        create_error: Exception | None = None,
        delete_error: Exception | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self._is_forum = is_forum
        self._can_manage_topics = can_manage_topics
        self._create_error = create_error
        self._delete_error = delete_error
        self.topics = 0
        self.deleted: list[int] = []
        self.closed: list[int] = []
        self.renamed: list[str] = []
        self.trace = trace if trace is not None else []

    async def get_chat(self, _chat_id: int) -> Any:
        return SimpleNamespace(type="supergroup", is_forum=self._is_forum)

    async def get_me(self) -> Any:
        return SimpleNamespace(id=42)

    async def get_chat_member(self, _chat_id: int, _user_id: int) -> Any:
        return SimpleNamespace(can_manage_topics=self._can_manage_topics)

    async def create_forum_topic(self, **_: Any) -> Any:
        self.trace.append("create_topic")
        if self._create_error is not None:
            raise self._create_error
        self.topics += 1
        return SimpleNamespace(message_thread_id=99)

    async def delete_forum_topic(self, **kwargs: Any) -> None:
        self.trace.append("delete_topic")
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted.append(int(kwargs["message_thread_id"]))

    async def close_forum_topic(self, **kwargs: Any) -> None:
        self.trace.append("close_topic")
        self.closed.append(int(kwargs["message_thread_id"]))

    async def edit_forum_topic(self, **kwargs: Any) -> None:
        self.trace.append("rename_topic")
        self.renamed.append(str(kwargs["name"]))


def _refused(reason: str) -> TelegramBadRequest:
    """What aiogram raises when Telegram refuses the call."""
    return TelegramBadRequest(
        method=CreateForumTopic(chat_id=-1001, name="x"), message=reason
    )


class _CountingClient:
    """Counts the one call that costs money."""

    def __init__(
        self,
        *,
        create_error: Exception | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self.creates = 0
        self._create_error = create_error
        self.trace = trace if trace is not None else []

    async def list_project_workspaces(self, *_: Any, **__: Any) -> WorkspacesPage:
        return WorkspacesPage(data=[], has_more=False)

    async def list_projects(self, *_: Any, **__: Any) -> ProjectsPage:
        return ProjectsPage(data=[Project(id="project-1", name="api")], has_more=False)

    async def list_workspace_sessions(self, *_: Any, **__: Any) -> SessionsPage:
        return SessionsPage(data=[Session(id="session-new")], has_more=False)

    async def create_workspace(self, **_: Any) -> WorkspaceCreateResult:
        self.trace.append("create_workspace")
        self.creates += 1
        if self._create_error is not None:
            raise self._create_error
        return WorkspaceCreateResult(
            workspace_id="workspace-1",
            session_id="session-new",
            deep_link="https://example.test/open",
        )


_REQUEST = CreateRequest(
    project_id="project-1",
    project_name="api",
    branch="main",
    agent="claude",
    model="sonnet",
    effort="high",
    prompt="Fix it",
)


def test_mobile_instruction_is_stable_and_not_duplicated() -> None:
    once = augment_prompt("Fix the flaky test")
    twice = augment_prompt(once)

    assert once.endswith(MOBILE_REPLY_INSTRUCTION)
    assert once == twice
    assert once.startswith("Fix the flaky test\n\n")
    assert "Choices:" in once


def test_mobile_instruction_binds_narration_format_and_a_numeric_cap() -> None:
    """The three adjectives were replaced by rules that can be obeyed."""
    text = MOBILE_REPLY_INSTRUCTION

    # A delimiter and an explicit override, so a long task cannot average it out.
    assert text.startswith("===\n")
    assert "overrides any conflicting" in text
    # The narration between tool calls was 6 of the 9 bubbles in a real turn.
    assert "no narration between tool calls" in text
    assert "One message, at the end." in text
    # Outcome first, no restatement, no step recap.
    assert "Open with the outcome" in text
    assert "never recap the steps" in text
    # A measurable budget, not an adjective.
    assert "6 lines and 80 words" in text
    # Formats that wrap badly at ~34 chars are named and banned.
    for banned in ("No headings", "no tables", "no bold labels"):
        assert banned in text
    # The chat already renders the diff lines.
    assert "Do not list the files you changed" in text


def test_mobile_instruction_keeps_the_quick_reply_contract_parsable() -> None:
    """Rule 5's syntax is what ``quick_replies_for`` turns into buttons."""
    reply = TranscriptMessage(
        id="m-1",
        session_id="session-1",
        type="agent",
        content={
            "type": "agent",
            "rawPayload": {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Ledger needs a store.\n\nChoices:\n"
                            "1. SQLite\n2. Postgres\n",
                        }
                    ]
                },
            },
        },
    )

    assert "that is exactly 'Choices:'" in MOBILE_REPLY_INSTRUCTION
    assert "'1. ...', '2. ...'" in MOBILE_REPLY_INSTRUCTION
    assert quick_replies_for(reply) == ("SQLite", "Postgres")


@pytest.mark.parametrize(
    "terse",
    ["yes", "no", "1", "2.", "ok", "Do it", "Choose option 2: Postgres"],
)
def test_a_bare_pick_or_ack_does_not_carry_the_contract_again(terse: str) -> None:
    """140 words of formatting rules bolted onto "yes" is 95% boilerplate."""
    assert augment_prompt(terse) == terse.strip()


@pytest.mark.parametrize("real", ["Fix it", "ship the fixture fix", "no tests fail?"])
def test_a_real_instruction_always_carries_the_contract(real: str) -> None:
    assert augment_prompt(real).endswith(MOBILE_REPLY_INSTRUCTION)


async def test_prompt_reaction_is_the_zero_noise_ack() -> None:
    class Bot:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def set_message_reaction(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    bot = Bot()
    message = SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=-1001),
        message_id=44,
    )

    assert await react_received(message)  # type: ignore[arg-type]
    assert bot.calls[0]["message_id"] == 44
    assert bot.calls[0]["reaction"][0].emoji == "👀"  # type: ignore[index,union-attr]


async def test_cancel_goes_through_supervisor_machine() -> None:
    supervisor = CancelSupervisor()

    accepted = await request_cancel(
        supervisor,  # type: ignore[arg-type]
        "session-1",
        requested_by=1001,
    )

    assert accepted
    assert supervisor.calls == [("session-1", Cancel(requested_by=1001))]
    assert not await request_cancel(None, "session-1", requested_by=1001)


async def test_submit_persists_and_posts_same_augmented_body(db: Database) -> None:
    await sessions_repo.upsert(db, "session-1")
    client = PromptClient()

    message_id, state = await submit_prompt(
        db=db,
        client=client,  # type: ignore[arg-type]
        session_id="session-1",
        text="Ship it",
        chat_id=100,
        thread_id=7,
        tg_message_id=9,
    )

    stored = await prompts_repo.get(db, message_id)
    assert stored is not None
    assert stored.body == augment_prompt("Ship it")
    assert client.posts == [("session-1", stored.body, message_id)]
    assert state == "queued"


def test_find_query_never_contains_raw_punctuation() -> None:
    malicious = "needle'; DROP TABLE outbound_prompts; --"
    normalized = normalize_find_term(malicious)
    query = find_query(malicious)

    assert "'" not in normalized
    assert ";" not in normalized
    assert query.count(";") == 0
    assert malicious not in query
    assert "LIMIT 20" in query


def test_mobile_board_is_compact_and_scannable() -> None:
    rows: list[dict[str, object]] = [
        {
            "workspace_id": f"w-{index}",
            "workspace_name": f"workspace-{index}",
            "workspace_state": "working" if index == 0 else "ready",
            "model": "sonnet",
        }
        for index in range(BOARD_VISIBLE + 3)
    ]
    lines = board_lines(rows)
    assert lines[0] == f"<b>Board · {BOARD_VISIBLE + 3} recent</b>"
    assert lines[1].startswith("⚙️")
    assert len(lines) == BOARD_VISIBLE + 2
    assert lines[-1] == "<i>+3 more · use /s to switch</i>"


def test_session_overview_is_a_concise_control_summary() -> None:
    session = SessionRow(
        id="session-123",
        workspace_id="workspace-123",
        title="Fix checkout",
        agent="claude",
        model="sonnet",
        effort="high",
        turn_state=str(TurnState.WORKING),
    )
    workspace = WorkspaceRow(
        id="workspace-123",
        name="api",
        branch="feature/checkout",
    )
    lines = session_overview_lines(session, workspace, pending=2)
    # Two lines, not four: the workspace/branch line only repeated the topic
    # title bar, and the queue depth rides on the model line.
    assert lines == [
        "⚙️ <b>Fix checkout</b> · working",
        "claude · sonnet/high · 2 pending",
    ]
    assert session_overview_lines(session, workspace) == [
        "⚙️ <b>Fix checkout</b> · working",
        "claude · sonnet/high",
    ]
    assert status_icon("error") == "⚠️"


def test_agent_decision_contract_becomes_quick_replies_only_with_marker() -> None:
    message = TranscriptMessage(
        id="m-1",
        session_id="session-1",
        type="agent",
        content={
            "type": "agent",
            "rawPayload": {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Recommendation: use SQLite.\n\nChoices:\n"
                                "1. SQLite (recommended)\n"
                                "2. Postgres\n"
                            ),
                        }
                    ]
                },
            },
        },
    )
    numbered_steps = message.model_copy(
        update={
            "content": {
                **message.content,
                "rawPayload": {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Steps:\n1. Build\n2. Test"}
                        ]
                    },
                },
            }
        }
    )

    assert quick_replies_for(message) == ("SQLite (recommended)", "Postgres")
    assert quick_replies_for(numbered_steps) == ()


def test_topic_color_is_stable_and_uses_telegrams_palette() -> None:
    first = topic_icon_color("api/feature-checkout")
    assert first == topic_icon_color("API/feature-checkout")
    assert first in TOPIC_ICON_COLORS


def test_topic_session_switch_cannot_cross_workspaces() -> None:
    sessions = [
        SessionRow(id="a", workspace_id="workspace-a"),
        SessionRow(id="b", workspace_id="workspace-b"),
    ]
    topic = Route(
        kind="topic",
        chat=ChatRow(chat_id=-1001, thread_id=7, workspace_id="workspace-a"),
    )
    dm = Route(kind="dm")

    assert [row.id for row in switchable_sessions(sessions, topic)] == ["a"]
    assert [row.id for row in switchable_sessions(sessions, dm)] == ["a", "b"]


async def test_general_plain_text_searches_and_never_posts(
    db: Database, monkeypatch: Any
) -> None:
    calls: list[tuple[str, Any]] = []
    bubbles: list[str] = []

    async def fake_find(message: Any, text: str, **kwargs: Any) -> None:
        calls.append((text, kwargs.get("reply_markup")))

    async def fake_tell(_message: Any, text: str, **__: Any) -> None:
        bubbles.append(text)

    monkeypatch.setattr(prompt_handlers, "run_find", fake_find)
    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    await sessions_repo.upsert(db, "session-1", title="api/fix-flaky")
    await chats_repo.bind(
        db, -1001, 7, workspace_id=None, session_id="session-1", kind="topic"
    )
    await chats_repo.touch_prompt(db, -1001, 7, focus_for_ms=1000)
    message = SimpleNamespace(
        text="search this",
        chat=SimpleNamespace(id=-1001),
        message_thread_id=None,
        message_id=4,
        from_user=SimpleNamespace(id=1001),
    )

    await prompt_handlers.plain_text(
        message,  # type: ignore[arg-type]
        Route(chat_id=-1001, kind="general"),
        NonceStore(),
        db=db,
        client=None,
    )

    # One typed line, one bubble: the Send button rides on the search results
    # instead of arriving as a second push, and it names its target.
    assert [text for text, _ in calls] == ["search this"]
    assert bubbles == []
    markup = calls[0][1]
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == "Send to api/fix-flaky"


async def test_binary_attachment_is_never_silently_ignored(monkeypatch: Any) -> None:
    replies: list[tuple[str, bool]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        replies.append((text, bool(kwargs.get("silent", True))))

    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    await prompt_handlers.unsupported_attachment(SimpleNamespace())  # type: ignore[arg-type]

    # Shorter, but still a push: the user believes they just sent something.
    assert replies == [("📎 Not forwarded — text or voice only.", False)]


async def test_unknown_commands_and_future_payloads_always_answer(
    monkeypatch: Any,
) -> None:
    replies: list[tuple[str, bool]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        replies.append((text, bool(kwargs.get("silent", True))))

    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    await prompt_handlers.unsupported_message(
        SimpleNamespace(content_type=ContentType.TEXT)  # type: ignore[arg-type]
    )
    await prompt_handlers.unsupported_message(
        SimpleNamespace(content_type=ContentType.UNKNOWN)  # type: ignore[arg-type]
    )

    assert replies == [
        ("Unknown command · use /help.", False),
        ("📎 Not forwarded — text or voice only.", False),
    ]


async def test_service_messages_do_not_create_bot_noise(monkeypatch: Any) -> None:
    replies: list[str] = []

    async def fake_tell(_message: Any, text: str, **_kwargs: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    await prompt_handlers.unsupported_message(
        SimpleNamespace(content_type=ContentType.FORUM_TOPIC_CREATED)  # type: ignore[arg-type]
    )

    assert replies == []


async def test_edit_and_stale_callback_never_fail_silently(monkeypatch: Any) -> None:
    replies: list[tuple[str, bool]] = []
    answers: list[tuple[str, bool]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        replies.append((text, bool(kwargs.get("silent", True))))

    async def answer(text: str, **kwargs: Any) -> None:
        answers.append((text, bool(kwargs.get("show_alert"))))

    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    await prompt_handlers.edited_text(SimpleNamespace())  # type: ignore[arg-type]
    await prompt_handlers.edited_payload(
        SimpleNamespace(content_type=ContentType.PHOTO)  # type: ignore[arg-type]
    )
    await prompt_handlers.unknown_callback(
        SimpleNamespace(answer=answer)  # type: ignore[arg-type]
    )

    assert replies == [
        ("Edit not resent · send the correction as a new message.", False),
        ("Edit not resent · send the correction as a new message.", False),
    ]
    assert answers == [("Expired control · run /mode for fresh buttons.", True)]


async def test_command_reply_retries_a_transient_telegram_failure(
    monkeypatch: Any,
) -> None:
    calls = 0
    sleeps: list[float] = []

    class Bot:
        async def send_message(self, **_kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TelegramNetworkError(
                    method=SendMessage(chat_id=-1001, text="x"),
                    message="connection reset",
                )
            return SimpleNamespace(message_id=7)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ctb.bot.handlers.topics.asyncio.sleep", fake_sleep)
    result = await send_html(Bot(), -1001, "ok")  # type: ignore[arg-type]

    assert result is not None and result.message_id == 7
    assert calls == 2
    assert sleeps == [0.2]


async def test_new_session_is_seeded_before_first_prompt(
    db: Database,
) -> None:
    class Client:
        async def list_project_workspaces(self, *_: Any, **__: Any) -> WorkspacesPage:
            return WorkspacesPage(data=[], has_more=False)

        async def create_workspace(self, **_: Any) -> WorkspaceCreateResult:
            return WorkspaceCreateResult(
                workspace_id="workspace-1",
                session_id="session-new",
                deep_link="https://example.test/open",
            )

    message = SimpleNamespace(
        chat=SimpleNamespace(id=1001, type="private"),
        message_id=7,
        message_thread_id=None,
    )

    await create_and_bind(
        message=message,  # type: ignore[arg-type]
        route=Route(chat_id=1001, kind="dm"),
        request=CreateRequest(
            project_id="project-1",
            project_name="api",
            branch="main",
            agent="claude",
            model="sonnet",
            effort="high",
            prompt="Fix it",
        ),
        db=db,
        client=Client(),  # type: ignore[arg-type]
    )

    row = await sessions_repo.get(db, "session-new")
    assert row is not None
    assert row.seeded and row.cursor_session_index == -1
    assert row.state.value == "WAKING"
    pending = await prompts_repo.list_recoverable(db, session_id="session-new")
    assert len(pending) == 1
    assert pending[0].body == augment_prompt("Fix it")


def test_general_topic_id_one_is_also_cockpit() -> None:
    message = SimpleNamespace(
        chat=SimpleNamespace(type="supergroup"),
        message_thread_id=1,
    )

    assert prompt_handlers.is_general_cockpit(
        message,  # type: ignore[arg-type]
        Route(chat_id=-1001, thread_id=1, kind="topic"),
    )


async def test_find_is_five_one_line_rows_not_three_screens() -> None:
    class SqlClient:
        async def sql(self, _query: str) -> SqlResult:
            return SqlResult(
                rows=[
                    {
                        "workspace_name": f"api/fix-{index}",
                        "preview": "the quick brown fox " * 20,
                    }
                    for index in range(12)
                ],
                row_count=12,
            )

    rendered = await find_text(SqlClient(), "fox")  # type: ignore[arg-type]
    lines = rendered.split("\n")

    # Header (only because there are more hits than shown) + five rows.
    assert lines[0] == "<b>12 matches</b>"
    assert len(lines) == FIND_VISIBLE + 1
    assert all(line.count("\n") == 0 for line in lines)
    assert lines[1].startswith("<b>api/fix-0</b> · the quick brown fox")
    # 90 characters of preview, not 180, and no blank separator line.
    assert len(lines[1]) < 130
    assert "" not in lines


async def test_find_drops_the_header_when_every_hit_is_shown() -> None:
    class SqlClient:
        async def sql(self, _query: str) -> SqlResult:
            return SqlResult(
                rows=[{"workspace_name": "api/fix", "preview": "hit"}], row_count=1
            )

    assert await find_text(SqlClient(), "hit") == "<b>api/fix</b> · hit"  # type: ignore[arg-type]


async def test_board_sends_one_line_and_buttons_never_both_lists(
    db: Database, monkeypatch: Any
) -> None:
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(core_handlers, "tell", fake_tell)
    for index in range(3):
        await workspaces_repo.upsert(
            db,
            f"workspace-{index}",
            name=f"api/fix-{index}",
            model="sonnet",
            chat_id=-1001,
            topic_id=100 + index,
        )

    async def fake_rows(*_: Any, **__: Any) -> list[dict[str, object]]:
        return [
            {
                "workspace_id": f"workspace-{index}",
                "workspace_name": f"api/fix-{index}",
                "display_state": "working",
                "model": "sonnet",
            }
            for index in range(3)
        ]

    monkeypatch.setattr(core_handlers, "board_rows", fake_rows)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001),
        message_thread_id=None,
        message_id=3,
        from_user=SimpleNamespace(id=1001),
    )

    await core_handlers.board(
        message,  # type: ignore[arg-type]
        _NullState(),  # type: ignore[arg-type]
        NonceStore(),
        db=db,
        client=_CountingClient(),  # type: ignore[arg-type]
    )

    text, markup = sent[0]
    # The ten names used to be printed and then rendered as ten buttons.
    assert text == "<b>3 live</b>"
    assert markup is not None
    labels = [row[0].text for row in markup.inline_keyboard]
    assert labels == [f"⚙️ api/fix-{index} · sonnet" for index in range(3)]


async def test_board_offers_one_tap_adoption_for_a_topicless_workspace(
    db: Database, monkeypatch: Any
) -> None:
    """A laptop-made workspace used to render as dead text. Now it opens."""
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    async def fake_rows(*_: Any, **__: Any) -> list[dict[str, object]]:
        return [
            {
                "workspace_id": "workspace-orphan",
                "session_id": "session-orphan",
                "workspace_name": "api/orphan",
                "display_state": "working",
                "model": "sonnet",
            }
        ]

    monkeypatch.setattr(core_handlers, "tell", fake_tell)
    monkeypatch.setattr(core_handlers, "board_rows", fake_rows)
    store = NonceStore()
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001),
        message_thread_id=None,
        message_id=3,
        from_user=SimpleNamespace(id=1001),
    )

    await core_handlers.board(
        message,  # type: ignore[arg-type]
        _NullState(),  # type: ignore[arg-type]
        store,
        db=db,
        client=_CountingClient(),  # type: ignore[arg-type]
    )

    text, markup = sent[0]
    assert text == "<b>1 live</b>"
    assert markup is not None
    tap = markup.inline_keyboard[0][0]
    assert tap.text == "+ Open api/orphan"
    # Nonce-backed, and it carries the newest session so adoption does not
    # have to guess which one the board was talking about.
    ticket = store.peek(tap.callback_data.rsplit(":", 1)[-1])
    assert ticket is not None
    assert ticket.action == "adopt"
    assert ticket.target == "workspace-orphan\nsession-orphan"


async def test_stop_acknowledges_with_a_reaction_not_a_bubble(
    monkeypatch: Any,
) -> None:
    reactions: list[str] = []
    bubbles: list[str] = []

    class Bot:
        async def set_message_reaction(self, **kwargs: Any) -> None:
            reactions.append(kwargs["reaction"][0].emoji)

    async def fake_tell(_message: Any, text: str, **__: Any) -> None:
        bubbles.append(text)

    monkeypatch.setattr(core_handlers, "tell", fake_tell)
    message = SimpleNamespace(
        bot=Bot(),
        chat=SimpleNamespace(id=-1001),
        message_id=12,
        message_thread_id=7,
        from_user=SimpleNamespace(id=1001),
    )

    await core_handlers.stop(
        message,  # type: ignore[arg-type]
        Route(
            chat_id=-1001,
            thread_id=7,
            kind="topic",
            chat=ChatRow(chat_id=-1001, thread_id=7, session_id="session-1"),
        ),
        _NullState(),  # type: ignore[arg-type]
        supervisor=CancelSupervisor(),  # type: ignore[arg-type]
    )

    assert reactions == ["👀"]
    assert bubbles == []


async def test_stop_still_says_something_when_reactions_are_refused(
    monkeypatch: Any,
) -> None:
    bubbles: list[str] = []

    async def fake_tell(_message: Any, text: str, **__: Any) -> None:
        bubbles.append(text)

    monkeypatch.setattr(core_handlers, "tell", fake_tell)
    message = SimpleNamespace(
        bot=None,
        chat=SimpleNamespace(id=-1001),
        message_id=12,
        message_thread_id=7,
        from_user=SimpleNamespace(id=1001),
    )

    await core_handlers.stop(
        message,  # type: ignore[arg-type]
        Route(
            chat_id=-1001,
            thread_id=7,
            kind="topic",
            chat=ChatRow(chat_id=-1001, thread_id=7, session_id="session-1"),
        ),
        _NullState(),  # type: ignore[arg-type]
        supervisor=CancelSupervisor(),  # type: ignore[arg-type]
    )

    assert bubbles == ["Stopping…"]


async def test_a_failed_stop_is_the_one_thing_worth_a_push(monkeypatch: Any) -> None:
    sent: list[tuple[str, bool]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, bool(kwargs.get("silent", True))))

    monkeypatch.setattr(core_handlers, "tell", fake_tell)
    message = SimpleNamespace(
        bot=None,
        chat=SimpleNamespace(id=-1001),
        message_id=12,
        message_thread_id=7,
        from_user=SimpleNamespace(id=1001),
    )

    await core_handlers.stop(
        message,  # type: ignore[arg-type]
        Route(
            chat_id=-1001,
            thread_id=7,
            kind="topic",
            chat=ChatRow(chat_id=-1001, thread_id=7, session_id="session-1"),
        ),
        _NullState(),  # type: ignore[arg-type]
        supervisor=CancelSupervisor(result=None),  # type: ignore[arg-type]
    )

    assert sent == [("Stop unavailable — retry.", False)]


async def test_command_replies_are_silent_by_default() -> None:
    """Routine acks must not buzz the phone; ``send_html`` gets the flag."""
    captured: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> None:
            captured.append(kwargs)

    message = SimpleNamespace(
        bot=Bot(), chat=SimpleNamespace(id=-1001), message_thread_id=7
    )

    await tell(message, "Renamed.")  # type: ignore[arg-type]
    await tell(message, "Rename failed: nope", silent=False)  # type: ignore[arg-type]

    assert captured[0]["disable_notification"] is True
    assert captured[1]["disable_notification"] is None


def test_archive_confirm_states_the_consequence_not_the_name() -> None:
    """The name is already in the button, the topic title and the title bar."""
    assert ARCHIVE_CONSEQUENCE == "Closes this topic. Restorable in Conductor."
    assert "<b>" not in ARCHIVE_CONSEQUENCE


async def test_new_checks_topic_permission_before_paying_for_a_workspace(
    db: Database,
) -> None:
    """``POST /workspaces`` has no idempotency key: each retry would strand one."""
    bot = _ForumBot(can_manage_topics=False)
    client = _CountingClient()

    with pytest.raises(RuntimeError) as error:
        await create_and_bind_input(
            bot=bot,  # type: ignore[arg-type]
            chat_id=-1001,
            chat_type="supergroup",
            tg_message_id=5,
            route=Route(chat_id=-1001, kind="general"),
            request=_REQUEST,
            db=db,
            client=client,  # type: ignore[arg-type]
        )

    assert "No topic permission" in str(error.value)
    assert "/setup" in str(error.value)
    assert client.creates == 0
    assert bot.topics == 0


async def test_new_still_creates_the_workspace_when_topics_are_allowed(
    db: Database,
) -> None:
    bot = _ForumBot()
    client = _CountingClient()

    created = await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=-1001,
        chat_type="supergroup",
        tg_message_id=5,
        route=Route(chat_id=-1001, kind="general"),
        request=_REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )

    assert client.creates == 1
    assert bot.topics == 1
    assert created.thread_id == 99


async def test_no_workspace_is_paid_for_when_telegram_refuses_the_topic(
    db: Database,
) -> None:
    """The money test.

    Telegram reported ``can_manage_topics`` true and then refused
    ``createForumTopic`` — the permission bit is a hint, the created topic is
    the proof. ``POST /workspaces`` has no idempotency key, so a workspace
    created before that refusal is a paid container nothing can adopt.
    """
    bot = _ForumBot(create_error=_refused("Bad Request: not enough rights"))
    client = _CountingClient()
    assert (await bot.get_chat_member(-1001, 42)).can_manage_topics is True

    with pytest.raises(TopicCreateError) as error:
        await create_and_bind_input(
            bot=bot,  # type: ignore[arg-type]
            chat_id=-1001,
            chat_type="supergroup",
            tg_message_id=5,
            route=Route(chat_id=-1001, kind="general"),
            request=_REQUEST,
            db=db,
            client=client,  # type: ignore[arg-type]
        )

    assert client.creates == 0
    assert await workspaces_repo.list_all(db) == []
    # …and the reason is Telegram's own, not a guess that sends the owner to
    # a /setup that reports everything is fine.
    assert str(error.value) == "Topic creation failed · not enough rights"


async def test_topic_is_created_before_the_workspace_is_paid_for(
    db: Database,
) -> None:
    trace: list[str] = []
    bot = _ForumBot(trace=trace)
    client = _CountingClient(trace=trace)

    await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=-1001,
        chat_type="supergroup",
        tg_message_id=5,
        route=Route(chat_id=-1001, kind="general"),
        request=_REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )

    assert trace == ["create_topic", "create_workspace"]


async def test_orphan_topic_is_discarded_when_the_workspace_create_fails(
    db: Database,
) -> None:
    """A topic is free and deletable; a retry must not stack empty rooms."""
    bot = _ForumBot()
    client = _CountingClient(create_error=RuntimeError("conductor is down"))

    with pytest.raises(RuntimeError, match="conductor is down"):
        await create_and_bind_input(
            bot=bot,  # type: ignore[arg-type]
            chat_id=-1001,
            chat_type="supergroup",
            tg_message_id=5,
            route=Route(chat_id=-1001, kind="general"),
            request=_REQUEST,
            db=db,
            client=client,  # type: ignore[arg-type]
        )

    assert bot.deleted == [99]
    assert bot.closed == []


async def test_orphan_topic_is_closed_when_it_cannot_be_deleted(
    db: Database,
) -> None:
    bot = _ForumBot(delete_error=_refused("Bad Request: topic not found"))
    client = _CountingClient(create_error=RuntimeError("conductor is down"))

    with pytest.raises(RuntimeError, match="conductor is down"):
        await create_and_bind_input(
            bot=bot,  # type: ignore[arg-type]
            chat_id=-1001,
            chat_type="supergroup",
            tg_message_id=5,
            route=Route(chat_id=-1001, kind="general"),
            request=_REQUEST,
            db=db,
            client=client,  # type: ignore[arg-type]
        )

    assert bot.deleted == []
    assert bot.closed == [99]


async def test_a_replayed_new_reuses_the_workspace_and_the_topic(
    db: Database,
) -> None:
    """Same update twice: one paid workspace, one topic, no siblings."""
    bot = _ForumBot()
    client = _CountingClient()
    kwargs: dict[str, Any] = {
        "bot": bot,
        "chat_id": -1001,
        "chat_type": "supergroup",
        "tg_message_id": 5,
        "route": Route(chat_id=-1001, kind="general"),
        "request": _REQUEST,
        "db": db,
        "client": client,
    }

    first = await create_and_bind_input(**kwargs)
    second = await create_and_bind_input(**kwargs)

    assert client.creates == 1
    assert bot.topics == 1
    assert bot.deleted == [] and bot.closed == []
    assert first.thread_id == second.thread_id == 99
    assert first.workspace_id == second.workspace_id
    assert len(await workspaces_repo.list_all(db)) == 1


async def test_a_replay_renames_a_topic_whose_title_drifted(
    db: Database,
) -> None:
    """The label is fixed through the one rename path, not a second one."""
    bot = _ForumBot()
    client = _CountingClient()
    await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=-1001,
        chat_type="supergroup",
        tg_message_id=5,
        route=Route(chat_id=-1001, kind="general"),
        request=_REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )
    await workspaces_repo.update(db, "workspace-1", topic_name="stale/name")

    await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=-1001,
        chat_type="supergroup",
        tg_message_id=5,
        route=Route(chat_id=-1001, kind="general"),
        request=_REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )

    assert bot.topics == 1
    assert bot.renamed == ["~ api/main"]


async def test_a_dm_creates_no_topic_and_still_binds(db: Database) -> None:
    """Degraded DM mode never touches the forum API."""
    bot = _ForumBot()
    client = _CountingClient()

    created = await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=1001,
        chat_type="private",
        tg_message_id=5,
        route=Route(chat_id=1001, kind="dm"),
        request=_REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )

    assert created.thread_id == 0
    assert bot.topics == 0 and bot.deleted == [] and bot.closed == []
    assert client.creates == 1


async def test_new_tells_the_owner_what_telegram_actually_said(
    db: Database, settings: Any, monkeypatch: Any
) -> None:
    """The hardcoded "run /setup" sent the owner in a circle: /setup was happy."""
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("silent", True)))

    monkeypatch.setattr(core_handlers, "tell", fake_tell)
    bot = _ForumBot(
        create_error=_refused("Bad Request: not enough rights to create a topic")
    )
    client = _CountingClient()
    message = SimpleNamespace(
        text="/new fix the flaky test",
        chat=SimpleNamespace(id=-1001, type="supergroup"),
        message_thread_id=None,
        message_id=3,
        bot=bot,
    )

    await core_handlers.new_workspace(
        message,  # type: ignore[arg-type]
        Route(chat_id=-1001, kind="general"),
        settings,
        _NullState(),  # type: ignore[arg-type]
        db=db,
        client=client,  # type: ignore[arg-type]
    )

    assert client.creates == 0
    assert sent == [
        (
            "New failed: Topic creation failed · not enough rights to create a topic",
            False,
        )
    ]


def test_the_user_sees_telegrams_own_words_not_a_guess() -> None:
    """One phone line: the reason, without the wrapper that is always the same."""
    reason = telegram_reason(
        _refused("Bad Request: not enough rights to create a topic")
    )

    assert reason == "not enough rights to create a topic"
    assert "Telegram server says" not in reason
    assert str(TopicCreateError(reason)) == (
        "Topic creation failed · not enough rights to create a topic"
    )


def test_telegram_reason_survives_an_unwrapped_error() -> None:
    assert telegram_reason(RuntimeError("boom\nstack")) == "boom"
    assert telegram_reason(RuntimeError("")) == "RuntimeError"


class _WizardState:
    """``_ask_branch`` only reads the data and moves the FSM forward."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.state: Any = None

    async def get_data(self) -> dict[str, Any]:
        return self._data

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def update_data(self, data: dict[str, Any]) -> dict[str, Any]:
        self._data.update(data)
        return self._data


def _wizard_message(chat_id: int = -1001, user_id: int = 1001) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_thread_id=None,
        message_id=3,
        from_user=SimpleNamespace(id=user_id),
    )


async def _branch_buttons(
    branch: str,
    monkeypatch: Any,
    settings_factory: Callable[..., Settings],
    *,
    configured: str = "main",
) -> list[str]:
    cards: list[Any] = []

    async def fake_edit(_message: Any, _text: str, markup: Any) -> None:
        cards.append(markup)

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    await new_workspace._ask_branch(
        _wizard_message(),  # type: ignore[arg-type]
        _WizardState({"branch": branch}),  # type: ignore[arg-type]
        NonceStore(),
        settings_factory(default_branch=configured),
    )
    return [
        item.text
        for row in cards[0].inline_keyboard
        for item in row
        if item.text not in ("Go with defaults →", "Cancel")
    ]


async def test_branch_step_offers_main_once_when_it_is_also_the_default(
    monkeypatch: Any, settings_factory: Callable[..., Settings]
) -> None:
    """Two taps that do the same thing were rendering as two buttons."""
    assert await _branch_buttons("main", monkeypatch, settings_factory) == ["main"]


async def test_branch_step_still_offers_both_when_they_differ(
    monkeypatch: Any, settings_factory: Callable[..., Settings]
) -> None:
    assert await _branch_buttons("dev", monkeypatch, settings_factory) == [
        "main",
        "dev",
    ]


async def test_branch_step_offers_the_configured_default_first(
    monkeypatch: Any, settings_factory: Callable[..., Settings]
) -> None:
    """``DEFAULT_BRANCH=dev`` is how the owner stops being shown ``main``."""
    assert await _branch_buttons(
        "dev", monkeypatch, settings_factory, configured="dev"
    ) == ["dev"]
    assert await _branch_buttons(
        "release", monkeypatch, settings_factory, configured="dev"
    ) == ["dev", "release"]


async def test_a_created_workspace_makes_its_branch_the_next_offer(
    db: Database, monkeypatch: Any, settings_factory: Callable[..., Settings]
) -> None:
    """Type ``dev`` once and it is the button from then on."""
    await create_and_bind_input(
        bot=None,
        chat_id=1001,
        chat_type="private",
        tg_message_id=5,
        route=Route(chat_id=1001, kind="dm"),
        request=replace(_REQUEST, branch="dev"),
        db=db,
        client=_CountingClient(),  # type: ignore[arg-type]
    )

    chat = await chats_repo.get(db, 1001, 0)
    assert chat is not None and chat.default_branch == "dev"
    # Seeded exactly as ``start_wizard`` seeds it: chat default beats settings.
    assert await _branch_buttons(
        chat.default_branch or "", monkeypatch, settings_factory
    ) == ["main", "dev"]


# ── the wizard's buttons have to outlive the process ─────────────────────────


class _Tap:
    """Just enough ``CallbackQuery``. A real one refuses to answer unbound."""

    def __init__(self, data: str, *, user_id: int = 1001) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = Message(
            message_id=3,
            date=datetime.now(UTC),
            chat=Chat(id=-1001, type="supergroup"),
        )
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_kwargs: Any) -> None:
        self.answers.append(text)


def _seat(db: Database, *, user_id: int = 1001, thread_id: int | None = None) -> Any:
    """A DB-backed FSM context — the thing that already survived the redeploy."""
    return FSMContext(
        storage=SqliteStorage(db),
        key=StorageKey(bot_id=0, chat_id=-1001, user_id=user_id, thread_id=thread_id),
    )


async def _mint_branch_card(
    db: Database,
    store: NonceStore,
    settings: Settings,
    monkeypatch: Any,
) -> str:
    """Draw the branch step and return the ``dev`` button's callback_data."""
    cards: list[Any] = []

    async def fake_edit(_message: Any, _text: str, markup: Any) -> None:
        cards.append(markup)

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    state = _seat(db)
    await state.set_data({"project_id": "project-1", "branch": "dev"})
    await new_workspace._ask_branch(
        _wizard_message(),  # type: ignore[arg-type]
        state,
        store,
        settings,
    )
    button = next(
        item for row in cards[0].inline_keyboard for item in row if item.text == "dev"
    )
    assert button.callback_data is not None
    assert len(button.callback_data.encode()) <= 64  # Telegram's hard cap
    cards.clear()
    return button.callback_data


async def test_a_wizard_button_minted_before_a_redeploy_still_works(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """The FSM survived the restart; the buttons used to die with the store."""
    data = await _mint_branch_card(db, NonceStore(), settings, monkeypatch)

    edits: list[str] = []

    async def fake_edit(_message: Any, text: str, _markup: Any) -> None:
        edits.append(text)

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    tap = _Tap(data)
    # The redeploy: a brand-new empty registry, the same SQLite file.
    await new_workspace.wizard_callback(
        tap,  # type: ignore[arg-type]
        _seat(db),
        NonceStore(),
        settings,
    )

    assert tap.answers == [""]
    assert edits == ["Agent?"]
    assert (await _seat(db).get_data())["branch"] == "dev"


async def test_a_wizard_button_is_still_single_use_while_the_process_lives(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    store = NonceStore()
    data = await _mint_branch_card(db, store, settings, monkeypatch)

    async def fake_edit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    taps = [_Tap(data), _Tap(data)]
    for tap in taps:
        await new_workspace.wizard_callback(
            tap,  # type: ignore[arg-type]
            _seat(db),
            store,
            settings,
        )

    assert taps[0].answers == [""]
    assert taps[1].answers == ["Already done — that button was single-use."]


async def test_a_wizard_button_for_a_step_already_passed_is_refused(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """Typing the branch moves the FSM on; the branch button must go stale."""
    data = await _mint_branch_card(db, NonceStore(), settings, monkeypatch)

    async def fake_edit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    state = _seat(db)
    await state.update_data({"branch": "release"})
    await new_workspace._ask_agent(
        _wizard_message(),  # type: ignore[arg-type]
        state,
        NonceStore(),
    )

    tap = _Tap(data)
    await new_workspace.wizard_callback(
        tap,  # type: ignore[arg-type]
        _seat(db),
        NonceStore(),
        settings,
    )

    assert tap.answers == [new_workspace.STALE_MESSAGE]
    assert (await _seat(db).get_data())["branch"] == "release"


async def test_a_wizard_button_says_closed_when_the_wizard_is_gone(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """A finished wizard is not an expired button; the line has to say so."""
    data = await _mint_branch_card(db, NonceStore(), settings, monkeypatch)
    await _seat(db).clear()

    tap = _Tap(data)
    await new_workspace.wizard_callback(
        tap,  # type: ignore[arg-type]
        _seat(db),
        NonceStore(),
        settings,
    )

    assert tap.answers == [new_workspace.GONE_MESSAGE]


async def test_a_wizard_button_is_useless_to_another_seat(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """Ownership is the FSM key, so it survives the restart the store did not."""
    store = NonceStore()
    data = await _mint_branch_card(db, store, settings, monkeypatch)

    # Store alive: the ticket itself remembers who it was minted for.
    intruder = _Tap(data, user_id=2002)
    await new_workspace.wizard_callback(
        intruder,  # type: ignore[arg-type]
        _seat(db, user_id=2002),
        store,
        settings,
    )
    assert intruder.answers == ["This button has expired. Run the command again."]

    # Store gone: a different user, and a different thread of the same user,
    # both read an empty wizard.
    for seat in (_seat(db, user_id=2002), _seat(db, thread_id=77)):
        tap = _Tap(data, user_id=2002)
        await new_workspace.wizard_callback(
            tap,  # type: ignore[arg-type]
            seat,
            NonceStore(),
            settings,
        )
        assert tap.answers == [new_workspace.GONE_MESSAGE]

    # And the real owner is untouched by any of it.
    assert (await _seat(db).get_data())["step"] == "branch"


async def test_the_whole_wizard_survives_a_redeploy_at_every_step(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """The owner redeploys constantly. Restart before every single tap."""
    cards: list[Any] = []

    async def fake_edit(_message: Any, _text: str, markup: Any) -> None:
        cards.append(markup)

    async def fake_tell(_message: Any, _text: str, **kwargs: Any) -> Any:
        cards.append(kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=3)

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    monkeypatch.setattr(new_workspace, "tell", fake_tell)
    await new_workspace.start_wizard(
        _wizard_message(),  # type: ignore[arg-type]
        route=Route(chat_id=-1001, kind="topic"),
        settings=settings,
        state=_seat(db),
        db=db,
        client=_CountingClient(),  # type: ignore[arg-type]
        nonces=NonceStore(),
    )

    for _step in range(5):
        first = cards[-1].inline_keyboard[0][0]
        tap = _Tap(str(first.callback_data))
        await new_workspace.wizard_callback(
            tap,  # type: ignore[arg-type]
            _seat(db),  # the DB is all that carried over
            NonceStore(),  # the registry did not
            settings,
        )
        assert tap.answers == [""], f"step {_step} refused a live button"

    data = await _seat(db).get_data()
    assert data["step"] == "prompt"
    assert (data["project_id"], data["branch"], data["agent"]) == (
        "project-1",
        "main",
        "claude",
    )


async def test_a_wizard_payload_cannot_be_repointed_at_a_destructive_action(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """Making ``wiz`` restart-proof must not make ``archive`` restart-proof."""
    data = await _mint_branch_card(db, NonceStore(), settings, monkeypatch)
    nonce = data.split(":")[-1]

    assert Action.ARCHIVE.value not in RESTARTABLE_ACTIONS
    assert Action.CLEAR_QUEUE.value not in RESTARTABLE_ACTIONS
    with pytest.raises(NonceError) as caught:
        read_stateless(nonce, Action.ARCHIVE.value)
    assert caught.value.reason == "mismatch"


async def test_defaults_can_set_and_show_the_branch(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """The branch is remembered from a create; it must be settable without one."""
    from ctb.bot.handlers import power as power_handlers

    sent: list[str] = []

    async def fake_tell(_message: Any, text: str, **_kwargs: Any) -> None:
        sent.append(text)

    monkeypatch.setattr(power_handlers, "tell", fake_tell)
    route = Route(chat_id=-1001, kind="topic", thread_id=99)

    async def run(text: str) -> None:
        await power_handlers.defaults(
            SimpleNamespace(  # type: ignore[arg-type]
                text=text,
                chat=SimpleNamespace(id=-1001, type="supergroup"),
                message_thread_id=99,
                message_id=7,
                from_user=SimpleNamespace(id=1001),
            ),
            route,
            settings,
            _NullState(),  # type: ignore[arg-type]
            db=db,
        )

    await run("/defaults branch dev")
    await run("/defaults")

    chat = await chats_repo.get(db, -1001, 99)
    assert chat is not None and chat.default_branch == "dev"
    assert sent[0] == "Default branch: <b>dev</b>."
    assert sent[1].startswith("Defaults: <b>claude</b> · opus-5-1m/high · <b>dev</b>")


async def _run_setup(bot: Any, db: Database, monkeypatch: Any) -> list[str]:
    """Drive ``/setup`` and return the lines it sent."""
    from ctb.bot.handlers import power as power_handlers

    sent: list[str] = []

    async def fake_tell(_message: Any, text: str, **_kwargs: Any) -> None:
        sent.append(text)

    monkeypatch.setattr(power_handlers, "tell", fake_tell)
    message = SimpleNamespace(
        text="/setup",
        chat=SimpleNamespace(id=-1001, type="supergroup"),
        message_thread_id=None,
        message_id=7,
        from_user=SimpleNamespace(id=1001),
        bot=bot,
    )
    await power_handlers.setup(
        message,  # type: ignore[arg-type]
        Route(chat_id=-1001, kind="general"),
        _NullState(),  # type: ignore[arg-type]
        db=db,
    )
    return sent


async def test_setup_proves_the_topic_right_instead_of_trusting_the_flag(
    db: Database, monkeypatch: Any
) -> None:
    """The live failure: ``can_manage_topics`` true, ``createForumTopic`` refused.

    /setup used to answer "Ready" here while every /new failed, leaving no way
    to tell which answer was lying.
    """
    bot = _ForumBot(
        can_manage_topics=True,
        create_error=TelegramBadRequest(
            method=CreateForumTopic(chat_id=-1001, name="x"),
            message="Bad Request: not enough rights to create a topic",
        ),
    )
    assert (await bot.get_chat_member(-1001, 42)).can_manage_topics is True

    sent = await _run_setup(bot, db, monkeypatch)

    assert sent == ["Setup blocked · not enough rights to create a topic."]
    assert not any("Ready" in line for line in sent)


async def test_setup_deletes_the_topic_it_probed_with(
    db: Database, monkeypatch: Any
) -> None:
    bot = _ForumBot(can_manage_topics=True)

    sent = await _run_setup(bot, db, monkeypatch)

    assert sent == ["Ready · General is search-only; /new creates topics."]
    assert bot.topics == 1, "the probe really created a topic"
    # 99 is the id the stub hands back from create_forum_topic — so this asserts
    # it deleted the very topic it made, not merely that it deleted something.
    assert bot.deleted == [99], "and cleaned it up, leaving no residue"


async def test_a_button_from_an_older_build_redraws_the_card_instead_of_dead_ending(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """The live failure the restart-proof payload could not reach.

    A button minted before that format existed is an opaque random handle —
    there is nothing in it to re-derive, so it can never resolve. The wizard
    behind it is still perfectly alive in SQLite, so redraw its step rather
    than telling the owner a live wizard has "expired".
    """
    await _mint_branch_card(db, NonceStore(), settings, monkeypatch)

    edits: list[str] = []

    async def fake_edit(_message: Any, text: str, _markup: Any) -> None:
        edits.append(text)

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    # Exactly what an old build put in callback_data: a bare random handle.
    stale = _Tap("ctb:wiz:Ky7cQ2mFq1sVb3Nd")
    await new_workspace.wizard_callback(
        stale,  # type: ignore[arg-type]
        _seat(db),
        NonceStore(),
        settings,
    )

    assert stale.answers == [new_workspace.REFRESHED_MESSAGE]
    assert edits == ["Branch? Type it or tap."], "the step is redrawn, not skipped"
    # The redraw is not a state change: the wizard is still on branch.
    assert (await _seat(db).get_data())["step"] == "branch"


async def test_a_stale_button_with_no_wizard_open_still_says_so(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """Nothing to redraw — do not invent a card the owner did not ask for."""
    edits: list[str] = []

    async def fake_edit(_message: Any, text: str, _markup: Any) -> None:
        edits.append(text)

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    stale = _Tap("ctb:wiz:Ky7cQ2mFq1sVb3Nd")
    await new_workspace.wizard_callback(
        stale,  # type: ignore[arg-type]
        _seat(db),
        NonceStore(),
        settings,
    )

    assert stale.answers == ["This button has expired. Run the command again."]
    assert edits == []
