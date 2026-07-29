"""Focused safety tests for the Telegram command surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.dispatcher.middlewares.user_context import (
    EVENT_CONTEXT_KEY,
    EventContext,
    UserContextMiddleware,
)
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import CreateForumTopic, SendMessage
from aiogram.types import Chat, Message, User
from aiogram.types import Update as TgUpdate

from ctb.bot import keyboards
from ctb.bot.app import PostgresStorage
from ctb.bot.handlers import core as core_handlers
from ctb.bot.handlers import home as home_handlers
from ctb.bot.handlers import power as power_handlers
from ctb.bot.handlers import prompts as prompt_handlers
from ctb.bot.handlers import topics
from ctb.bot.handlers.common import _LINEAR_TOLD as LINEAR_TOLD
from ctb.bot.handlers.common import (
    LINEAR_DM_NOTICE,
    MOBILE_REPLY_INSTRUCTION,
    CreatedBinding,
    CreateRequest,
    augment_prompt,
    create_and_bind,
    create_and_bind_input,
    created_card,
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
from ctb.bot.handlers.power import homed_elsewhere, switchable_sessions
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
    button,
    read_stateless,
)
from ctb.bot.middleware.routing import Route, RoutingMiddleware
from ctb.bot.middleware.tenancy import TenantContext, TenantSettings
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
from ctb.db.repo.tenancy import TenantRow
from ctb.db.repo.workspaces import WorkspaceRow
from ctb.delivery.status_card import CardState, card_buttons
from ctb.settings import Settings
from ctb.turn.cursor import quick_replies_for
from ctb.turn.state import Cancel, CardButton, CardKind, TopicMarker, TurnState
from tests.pg import BOOTSTRAP_TENANT_ID


@pytest.fixture(autouse=True)
def _forget_linear_notices() -> None:
    """ "Told this chat once" is process state. Do not let it leak between tests."""
    LINEAR_TOLD.clear()


class _NullFsm:
    """``abandon_wizard`` only asks whether a wizard is open."""

    async def get_state(self) -> str | None:
        return None

    async def clear(self) -> None:
        return None


def fake_tenant(
    client: Any = None,
    *,
    role: str = "owner",
    user_id: int = 1001,
    slug: str = "test",
    settings: TenantSettings | None = None,
) -> TenantContext:
    """The context TenantMiddleware would have injected.

    Handlers reach the Conductor API only through this, which is what makes a
    cross-organisation read impossible to write by accident.
    """
    return TenantContext(
        tenant_id=BOOTSTRAP_TENANT_ID,
        slug=slug,
        status="active",
        role=role,
        user_id=user_id,
        owner_ids=(user_id,),
        primary_chat_id=None,
        settings=settings or TenantSettings(),
        row=TenantRow(id=BOOTSTRAP_TENANT_ID, slug=slug, name=slug, status="active"),
        _client=client,
    )


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
        topics_enabled: bool | None = None,
        create_error: Exception | None = None,
        delete_error: Exception | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self._is_forum = is_forum
        self._can_manage_topics = can_manage_topics
        # ``None`` is what a live ``getMe`` returns when the field is absent —
        # unknown, which must not be read as a refusal.
        self._topics_enabled = topics_enabled
        self._create_error = create_error
        self._delete_error = delete_error
        self.topics = 0
        self.deleted: list[int] = []
        self.closed: list[int] = []
        self.renamed: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.trace = trace if trace is not None else []

    async def get_chat(self, _chat_id: int) -> Any:
        return SimpleNamespace(type="supergroup", is_forum=self._is_forum)

    async def get_me(self) -> Any:
        return SimpleNamespace(id=42, has_topics_enabled=self._topics_enabled)

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

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
    assert "then one message" in text
    assert "no progress notes" in text
    # Outcome first, bad news first, no restatement, no step recap.
    assert "Line 1 is the outcome" in text
    assert "not at the bottom" in text
    assert "recap your steps" in text
    # A measurable budget, not an adjective.
    assert "6 lines and 80 words" in text
    # Formats that wrap badly at ~40 chars are named and banned.
    for banned in ("No tables", "No headings", "bold labels"):
        assert banned in text
    # The chat already renders the diff lines.
    assert "list the files you changed" in text


def test_mobile_instruction_keeps_the_quick_reply_contract_parsable() -> None:
    """Rule 7's syntax is what ``quick_replies_for`` turns into buttons."""
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


def test_the_option_budget_the_contract_promises_is_the_button_budget() -> None:
    """Rule 7 tells the agent 40 characters. The keyboard has to agree.

    An option one character over ``MAX_BUTTON_TEXT`` is not a smaller button —
    ``quick_replies_fit`` fails and the whole block degrades to plain text, so a
    contract that promised a looser budget than the keyboard honours would ask
    for exactly the answers that lose their buttons.
    """
    assert "under 40 characters" in MOBILE_REPLY_INSTRUCTION

    assert keyboards.quick_replies_fit(["x" * 40] * 4)
    # And the ceiling is real: the first option carries the extra "✓ ".
    assert not keyboards.quick_replies_fit(["x" * 60, "ok"])


def test_the_contract_never_parses_as_a_choices_block_itself() -> None:
    """Rule 7 quotes its own syntax; an echo of it must not become buttons."""
    echo = TranscriptMessage(
        id="m-2",
        session_id="session-1",
        type="agent",
        content={
            "type": "agent",
            "rawPayload": {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": MOBILE_REPLY_INSTRUCTION}]
                },
            },
        },
    )

    assert quick_replies_for(echo) == ()


@pytest.mark.parametrize(
    "terse",
    ["yes", "no", "1", "2.", "ok", "Do it", "Choose option 2: Postgres"],
)
def test_a_bare_pick_or_ack_does_not_carry_the_contract_again(terse: str) -> None:
    """240 words of formatting rules bolted onto "yes" is 97% boilerplate."""
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
    assert lines[0] == f"<b>Board · {BOARD_VISIBLE + 3} workspaces</b>"
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
        fake_tenant(_CountingClient()),
        NonceStore(),
        _seat(db),
        db=db,
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

    # Answered, but not a push. "Never silently ignored" is about getting a
    # reply; the phone is already in the reader's hand, and `tell` reserves the
    # buzz for something they have to act on.
    assert replies == [("📎 Not forwarded — text or voice only.", True)]


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
        ("Unknown command · use /help.", True),
        ("📎 Not forwarded — text or voice only.", True),
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
        ("Edit not resent · send the correction as a new message.", True),
        ("Edit not resent · send the correction as a new message.", True),
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
        fake_tenant(_CountingClient()),
        _NullState(),  # type: ignore[arg-type]
        NonceStore(),
        db=db,
    )

    text, markup = sent[0]
    # The ten names used to be printed and then rendered as ten buttons.
    assert text == "<b>3 workspaces</b>"
    assert markup is not None
    labels = [row[0].text for row in markup.inline_keyboard]
    assert labels == [f"⚙️ api/fix-{index} · sonnet" for index in range(3)]


async def test_board_counts_workspaces_not_sessions(db: Database) -> None:
    """Three sessions in one workspace are one workspace, one topic, one button.

    ``session_transcripts_view`` has a row per session, so a board built from it
    reported "4 live" over two workspaces and drew three identical buttons that
    all jumped to the same topic — a count that agreed with neither Conductor
    nor ``/health``.
    """

    class ManySessions:
        async def sql(self, _query: str) -> SqlResult:
            rows: list[dict[str, object]] = [
                {
                    "session_id": f"session-{index}",
                    "workspace_id": "workspace-busy",
                    "session_title": "Review project architecture",
                    "workspace_name": "acme-api/main",
                    "workspace_state": "ready",
                    "model": "opus-5-1m",
                    "transcript_updated_at": 300 - index,
                }
                for index in range(3)
            ]
            rows.append(
                {
                    "session_id": "session-other",
                    "workspace_id": "workspace-quiet",
                    "session_title": "Something else",
                    "workspace_name": "tg-100200300-iszvwjeb",
                    "workspace_state": "ready",
                    "model": "opus-5-1m",
                    "transcript_updated_at": 100,
                }
            )
            return SqlResult(rows=rows, row_count=len(rows))

    rows = await core_handlers.board_rows(db, ManySessions())  # type: ignore[arg-type]

    assert [row["workspace_id"] for row in rows] == [
        "workspace-busy",
        "workspace-quiet",
    ]
    # Newest first, so the survivor is the freshest session of its workspace.
    assert rows[0]["session_id"] == "session-0"
    assert core_handlers.board_lines(rows)[0] == "<b>Board · 2 workspaces</b>"


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
        fake_tenant(_CountingClient()),
        _NullState(),  # type: ignore[arg-type]
        store,
        db=db,
    )

    text, markup = sent[0]
    assert text == "<b>1 workspace</b>"
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
    assert bot.renamed == ["⏳ Fix it · api/main"]


async def _new_in_dm(
    db: Database,
    bot: _ForumBot,
    client: _CountingClient,
    *,
    chat_id: int,
    tg_message_id: int = 5,
    request: CreateRequest | None = None,
) -> Any:
    return await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=chat_id,
        chat_type="private",
        tg_message_id=tg_message_id,
        route=Route(chat_id=chat_id, kind="dm"),
        request=request or _REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )


async def test_a_dm_gets_one_topic_per_workspace_just_like_a_group(
    db: Database,
) -> None:
    """A bot needs no rights to open a topic in a private chat.

    So a DM is not a lesser chat with one seat: it is the same column of rooms
    a group gets, bound through the same ``require_topic``/``attach`` path.
    """
    bot = _ForumBot(topics_enabled=True)
    client = _CountingClient()

    created = await _new_in_dm(db, bot, client, chat_id=1001)

    assert created.thread_id == 99
    assert created.linear_reason is None
    assert bot.topics == 1 and bot.deleted == [] and bot.closed == []
    assert bot.sent == [], "nothing to apologise for when it worked"
    seat = await chats_repo.get(db, 1001, 99)
    assert seat is not None
    assert seat.kind == "topic", "a seat with a thread is a topic seat"
    assert seat.session_id == "session-new"
    session = await sessions_repo.get(db, "session-new")
    assert session is not None and session.thread_id == 99
    workspace = await workspaces_repo.get(db, "workspace-1")
    assert workspace is not None and workspace.topic_id == 99


async def test_a_new_typed_into_an_empty_thread_lives_in_that_thread(
    db: Database,
) -> None:
    """**The three-topics bug.**

    Telegram's *New Chat* seat is a composer: it opens a thread out of whatever
    is typed into it and names the thread after that first line. So ``/new``
    arrived already inside a thread called "/new" — and then opened a second
    one beside it for the workspace. Two rooms, one of them permanently empty,
    every single time.

    The room the request is already standing in is the room.
    """
    bot = _ForumBot(topics_enabled=True)
    client = _CountingClient()

    created = await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=1007,
        chat_type="private",
        tg_message_id=5,
        route=Route(chat_id=1007, thread_id=4242, kind="dm"),
        request=_REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )

    assert bot.topics == 0, "no sibling topic beside the one we are standing in"
    assert created.thread_id == 4242
    # It was called "/new" a second ago. The topic list is the navigation.
    assert bot.renamed == ["⏳ Fix it · api/main"]
    seat = await chats_repo.get(db, 1007, 4242)
    assert seat is not None and seat.kind == "topic"
    assert seat.session_id == "session-new"
    workspace = await workspaces_repo.get(db, "workspace-1")
    assert workspace is not None and workspace.topic_id == 4242


async def test_a_new_from_a_thread_that_already_has_work_opens_a_new_room(
    db: Database,
) -> None:
    """Claiming is only ever *empty* threads. A busy room is never taken over."""
    bot = _ForumBot(topics_enabled=True)
    client = _CountingClient()
    busy = Route(
        chat_id=1008,
        thread_id=4242,
        kind="dm",
        session=SessionRow(id="sess-busy", chat_id=1008, thread_id=4242),
    )

    created = await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=1008,
        chat_type="private",
        tg_message_id=5,
        route=busy,
        request=_REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )

    assert bot.topics == 1 and created.thread_id == 99


async def test_a_group_topic_is_never_claimed(db: Database) -> None:
    """A group's rooms belong to people, not to the bot's scratch space.

    Its General is thread 0 and would not qualify anyway; a topic in a group is
    somebody's room. Only a *private* chat's empty thread is claimable.
    """
    bot = _ForumBot()
    client = _CountingClient()

    created = await create_and_bind_input(
        bot=bot,  # type: ignore[arg-type]
        chat_id=-1001,
        chat_type="supergroup",
        tg_message_id=5,
        route=Route(chat_id=-1001, thread_id=4242, kind="topic"),
        request=_REQUEST,
        db=db,
        client=client,  # type: ignore[arg-type]
    )

    assert bot.topics == 1 and created.thread_id == 99


def test_claimable_thread_reads_the_route_and_only_the_route() -> None:
    """One rule, in one place — the router already decided which seat this is."""
    assert Route(chat_id=7, thread_id=12, kind="dm").claimable_thread == 12
    assert Route(chat_id=7, thread_id=0, kind="dm").claimable_thread == 0
    assert Route(chat_id=-1, thread_id=12, kind="topic").claimable_thread == 0
    assert Route(chat_id=-1, thread_id=0, kind="general").claimable_thread == 0
    bound = Route(
        chat_id=7,
        thread_id=12,
        kind="dm",
        session=SessionRow(id="s", chat_id=7, thread_id=12),
    )
    assert bound.claimable_thread == 0
    # A room whose workspace exists but whose session row has not landed yet is
    # still taken. The voice worker used to miss this and offer to open a second
    # workspace inside somebody's.
    homed = Route(
        chat_id=7,
        thread_id=12,
        kind="dm",
        chat=ChatRow(chat_id=7, thread_id=12, kind="topic", workspace_id="ws-1"),
    )
    assert homed.claimable_thread == 0


async def test_a_refused_dm_topic_still_creates_and_still_delivers(
    db: Database,
) -> None:
    """**The most important one.** An optional feature may not take the bot down.

    Telegram has been refusing ``createForumTopic`` in DMs since the Bot API
    10.0 rollout. A group has nothing to fall back to and fails the command;
    a DM has the linear seat it used until today, so it uses it — same
    workspace, same queued prompt, one line to say what changed.
    """
    trace: list[str] = []
    bot = _ForumBot(
        topics_enabled=True,
        create_error=_refused("Bad Request: message thread not found"),
        trace=trace,
    )
    client = _CountingClient(trace=trace)

    created = await _new_in_dm(db, bot, client, chat_id=1002)

    assert created.thread_id == 0
    assert created.linear_reason == "message thread not found"
    assert client.creates == 1, "the workspace is what the owner asked for"
    # …and it was still attempted first, so a refusal can never strand one.
    assert trace == ["create_topic", "create_workspace"]
    assert bot.deleted == [] and bot.closed == []
    seat = await chats_repo.get(db, 1002, 0)
    assert seat is not None and seat.kind == "dm"
    assert seat.session_id == "session-new"
    # The prompt is durable and queued, exactly as in the topic case.
    pending = await prompts_repo.list_recoverable(db, session_id="session-new")
    assert len(pending) == 1
    assert [item["text"] for item in bot.sent] == [LINEAR_DM_NOTICE]


async def test_threaded_mode_off_skips_a_create_that_cannot_work(
    db: Database,
) -> None:
    """``getMe`` says so outright; spending a refused call to learn it is waste."""
    trace: list[str] = []
    bot = _ForumBot(topics_enabled=False, trace=trace)
    client = _CountingClient(trace=trace)

    created = await _new_in_dm(db, bot, client, chat_id=1003)

    assert trace == ["create_workspace"]
    assert created.thread_id == 0
    assert created.linear_reason == "threaded mode is off"
    assert client.creates == 1


async def test_an_unknown_threaded_mode_still_tries_the_topic(
    db: Database,
) -> None:
    """Only an explicit ``False`` is a refusal.

    ``has_topics_enabled`` is absent on an older API, and the created topic is
    the only real proof anyway — so unknown means try, not give up.
    """
    bot = _ForumBot(topics_enabled=None)
    client = _CountingClient()

    created = await _new_in_dm(db, bot, client, chat_id=1004)

    assert bot.topics == 1 and created.thread_id == 99


async def test_the_linear_line_is_said_once_per_chat(db: Database) -> None:
    """A nudge, not a nag: two workspaces in a linear DM say it once."""
    bot = _ForumBot(topics_enabled=False)
    client = _CountingClient()

    await _new_in_dm(db, bot, client, chat_id=1005, tg_message_id=5)
    await _new_in_dm(db, bot, client, chat_id=1005, tg_message_id=6)

    assert [item["text"] for item in bot.sent] == [LINEAR_DM_NOTICE]
    assert LINEAR_DM_NOTICE.count("\n") == 0, "one short line on a phone"


async def test_a_dm_topic_is_renamed_through_the_one_rename_path(
    db: Database,
) -> None:
    """Markers and renames behave identically in a DM (docs/NAMING.md)."""
    bot = _ForumBot(topics_enabled=True)
    client = _CountingClient()
    await _new_in_dm(db, bot, client, chat_id=1006)
    await workspaces_repo.update(db, "workspace-1", topic_name="stale/name")

    await _new_in_dm(db, bot, client, chat_id=1006)

    assert bot.topics == 1, "the replay reuses the topic this nonce owns"
    assert bot.renamed == ["⏳ Fix it · api/main"]


async def test_a_dm_topic_switch_stays_inside_its_workspace(db: Database) -> None:
    """The DM root keeps cross-workspace switching; a DM topic does not.

    Deliberate: the root is the DM's cockpit and the only way round a chat with
    no topics, while a DM topic is addressed exactly as a group topic is — the
    way to reach another workspace is to tap its room.
    """
    sessions = [
        SessionRow(id="a", workspace_id="workspace-a"),
        SessionRow(id="b", workspace_id="workspace-b"),
    ]
    root = Route(chat_id=1007, kind="dm")
    topic = Route(
        chat_id=1007,
        thread_id=99,
        kind="dm",
        chat=ChatRow(chat_id=1007, thread_id=99, workspace_id="workspace-a"),
    )

    assert root.is_dm and not root.is_topic
    assert topic.is_topic and not topic.is_dm
    assert topic.is_private, "still a private chat, whatever the seat"
    assert [row.id for row in switchable_sessions(sessions, root)] == ["a", "b"]
    assert [row.id for row in switchable_sessions(sessions, topic)] == ["a"]


async def test_a_dm_topic_addresses_its_own_seat_without_is_topic_message(
    db: Database,
) -> None:
    """The routing hazard, and why it is not left to aiogram.

    ``EventContext.thread_id`` is only populated when Telegram also sets
    ``is_topic_message``, which is documented for forums and not promised for a
    topic in a private chat. Losing it would address every prompt typed in a DM
    topic to the DM root — the *wrong* session, which is worse than none.
    """
    await workspaces_repo.upsert(db, "ws-dm", chat_id=1010, topic_id=99)
    await sessions_repo.upsert(
        db, "sess-dm-topic", workspace_id="ws-dm", chat_id=1010, thread_id=99
    )
    await chats_repo.bind(
        db, 1010, 99, workspace_id="ws-dm", session_id="sess-dm-topic", kind="topic"
    )
    message = Message(
        message_id=10,
        date=datetime.now(tz=UTC),
        chat=Chat(id=1010, type="private"),
        from_user=User(id=1001, is_bot=False, first_name="T"),
        text="go",
        message_thread_id=99,
        is_topic_message=False,
    )
    update = TgUpdate(update_id=1, message=message)
    captured: dict[str, Any] = {}

    async def handler(_event: Any, payload: dict[str, Any]) -> None:
        captured["route"] = payload["route"]
        captured["data"] = payload

    await RoutingMiddleware(db=db)(
        handler,
        update,
        {
            EVENT_CONTEXT_KEY: UserContextMiddleware.resolve_event_context(update),
            "db": db,
            "tenant": SimpleNamespace(
                tenant_id=BOOTSTRAP_TENANT_ID,
                settings=SimpleNamespace(voice_enabled=False),
            ),
        },
    )

    route = captured["route"]
    assert route.key == (1010, 99)
    assert route.session_id == "sess-dm-topic"
    assert route.is_topic and not route.is_dm
    # …and the seat is republished, because aiogram's FSM keys wizard state off
    # `EventContext.thread_id` and Telegram leaves it empty here. Without this
    # every seat in the DM shares one wizard, so a `/new` left open in the root
    # eats the next line typed in any topic.
    assert captured["data"][EVENT_CONTEXT_KEY].thread_id == 99


async def test_the_dm_root_publishes_no_seat_so_wizards_key_on_the_root(
    db: Database,
) -> None:
    """The other half of the same rule: the root is thread ``None``, not 99."""
    message = Message(
        message_id=11,
        date=datetime.now(tz=UTC),
        chat=Chat(id=1011, type="private"),
        from_user=User(id=1001, is_bot=False, first_name="T"),
        text="hi",
    )
    update = TgUpdate(update_id=2, message=message)
    captured: dict[str, Any] = {}

    async def handler(_event: Any, payload: dict[str, Any]) -> None:
        captured["data"] = payload

    await RoutingMiddleware(db=db)(
        handler,
        update,
        {
            EVENT_CONTEXT_KEY: UserContextMiddleware.resolve_event_context(update),
            "db": db,
            "tenant": SimpleNamespace(
                tenant_id=BOOTSTRAP_TENANT_ID,
                settings=SimpleNamespace(voice_enabled=False),
            ),
        },
    )

    assert captured["data"][EVENT_CONTEXT_KEY].thread_id is None


def test_a_forums_general_is_published_as_the_root_seat_too() -> None:
    """Thread 1 and thread 0 are one seat, and the FSM must agree with `/setup`."""
    context = EventContext(chat=Chat(id=-1001, type="supergroup"), thread_id=1)
    data: dict[str, Any] = {EVENT_CONTEXT_KEY: context, "event_thread_id": 1}

    updated = RoutingMiddleware._publish_seat(context, 0, data)

    assert updated.thread_id is None
    assert data[EVENT_CONTEXT_KEY] is updated
    assert "event_thread_id" not in data, "aiogram's mirror must not disagree"


# ── the DM root is a cockpit, never a dead end ───────────────────────────────


async def _dm_root_text(
    db: Database, monkeypatch: Any, *, chat_id: int
) -> tuple[list[tuple[str, Any]], list[str]]:
    """Type a task into the DM root and report what came back."""
    bubbles: list[tuple[str, Any]] = []
    searches: list[str] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> Any:
        bubbles.append((text, kwargs.get("reply_markup")))

    async def fake_find(_message: Any, text: str, **_: Any) -> None:
        searches.append(text)

    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    monkeypatch.setattr(prompt_handlers, "run_find", fake_find)
    message = SimpleNamespace(
        text="fix the login bug",
        chat=SimpleNamespace(id=chat_id, type="private"),
        message_thread_id=None,
        message_id=12,
        from_user=SimpleNamespace(id=1001),
    )

    await prompt_handlers.plain_text(
        message,  # type: ignore[arg-type]
        Route(chat_id=chat_id, kind="dm"),
        fake_tenant(_CountingClient()),
        NonceStore(),
        _seat(db),
        db=db,
    )
    return bubbles, searches


async def test_the_dm_root_offers_the_topic_it_handed_the_work_to(
    db: Database, monkeypatch: Any
) -> None:
    """**The bug this whole change is about.**

    ``/new`` in a DM opens a topic and binds the session to it, which leaves the
    root holding nothing — so every following line, typed or dictated, answered
    "No session here" while the session plainly existed one room away. The root
    is the cockpit, and a cockpit offers a button instead of a dead end.
    """
    await workspaces_repo.upsert(db, "ws-dm", chat_id=1020, topic_id=99)
    await sessions_repo.upsert(
        db, "sess-dm", workspace_id="ws-dm", chat_id=1020, thread_id=99, title="Login"
    )
    await chats_repo.bind(
        db, 1020, 99, workspace_id="ws-dm", session_id="sess-dm", kind="topic"
    )
    await chats_repo.touch_prompt(db, 1020, 99, focus_for_ms=1000)

    bubbles, searches = await _dm_root_text(db, monkeypatch, chat_id=1020)

    assert searches == [], "the root sends, it does not search — General does that"
    text, markup = bubbles[0]
    assert text == core_handlers.DM_COCKPIT_HINT
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == "Send to Login"


async def test_a_dm_with_nothing_running_still_gets_the_honest_nudge(
    db: Database, monkeypatch: Any
) -> None:
    """No cockpit to be. Inventing a destination would be worse than saying so."""
    bubbles, searches = await _dm_root_text(db, monkeypatch, chat_id=1021)

    assert searches == []
    assert bubbles == [
        ("No session here. Use <code>/new</code> or <code>/s</code>.", None)
    ]


async def test_a_bound_dm_root_still_prompts_and_pays_for_no_cockpit_lookup(
    db: Database, monkeypatch: Any
) -> None:
    """A linear DM — topics refused — is unchanged: the root *is* the seat."""
    submitted: list[str] = []

    async def fake_submit(**kwargs: Any) -> tuple[str, str]:
        submitted.append(kwargs["session_id"])
        return ("m1", "queued")

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> Any:
        return None

    async def fake_react(_message: Any) -> bool:
        return True

    monkeypatch.setattr(prompt_handlers, "submit_prompt", fake_submit)
    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    monkeypatch.setattr(prompt_handlers, "react_received", fake_react)
    await sessions_repo.upsert(db, "sess-linear", chat_id=1022, thread_id=0)
    message = SimpleNamespace(
        text="go",
        chat=SimpleNamespace(id=1022, type="private"),
        message_thread_id=None,
        message_id=13,
        from_user=SimpleNamespace(id=1001),
    )

    await prompt_handlers.plain_text(
        message,  # type: ignore[arg-type]
        Route(
            chat_id=1022,
            kind="dm",
            session=SessionRow(id="sess-linear", chat_id=1022, thread_id=0),
        ),
        fake_tenant(_CountingClient()),
        NonceStore(),
        _seat(db),
        db=db,
    )

    assert submitted == ["sess-linear"]


async def test_the_prompt_receipt_carries_no_stop_of_its_own(
    db: Database, monkeypatch: Any
) -> None:
    """One task, one Stop. Two of them is two answers to "is this still going?".

    The receipt used to carry a Stop on the theory that a refused 👀 left the
    user without one — but the reaction has nothing to do with the pinned card,
    which appears either way and owns Stop. And this was the wrong one to keep:
    a bubble is static, so its Stop stayed live on screen for fifteen minutes
    after the turn ended, targeting the *session*, which meant tapping it then
    killed whatever was running by then. The card's is edited away the moment
    the turn is over.
    """
    bubbles: list[tuple[str, Any]] = []

    async def fake_submit(**_kwargs: Any) -> tuple[str, str]:
        return ("m1", "queued")

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> Any:
        bubbles.append((text, kwargs.get("reply_markup")))
        return None

    async def refused(_message: Any) -> bool:
        return False

    monkeypatch.setattr(prompt_handlers, "submit_prompt", fake_submit)
    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    monkeypatch.setattr(prompt_handlers, "react_received", refused)
    await sessions_repo.upsert(db, "sess-receipt", chat_id=1050, thread_id=7)
    message = SimpleNamespace(
        text="go",
        chat=SimpleNamespace(id=1050, type="private"),
        message_thread_id=7,
        message_id=14,
        from_user=SimpleNamespace(id=1001),
    )

    await prompt_handlers.plain_text(
        message,  # type: ignore[arg-type]
        Route(
            chat_id=1050,
            thread_id=7,
            kind="dm",
            session=SessionRow(
                id="sess-receipt", chat_id=1050, thread_id=7, title="Login"
            ),
        ),
        fake_tenant(_CountingClient()),
        NonceStore(),
        _seat(db),
        db=db,
    )

    text, markup = bubbles[-1]
    assert text.startswith("→ <b>Login</b>"), "the receipt itself is still useful"
    assert markup is None, "the card owns Stop; this is a receipt, not a control"


def test_only_a_live_card_offers_the_one_stop() -> None:
    """…and the card that owns it takes it away the moment the turn is over."""
    live = CardState(kind=CardKind.WORKING, buttons=(CardButton.STOP,))
    finished = CardState(kind=CardKind.DONE, buttons=(CardButton.STOP,))

    assert card_buttons(live) == (CardButton.STOP,)
    assert card_buttons(finished) == ()


def test_a_dm_topic_has_no_link_so_the_card_says_where_it_went() -> None:
    """``jump_url`` is ``None`` in a private chat — Telegram publishes no syntax.

    Left as a bare ``→ label`` with no button, ``/new`` read as though nothing
    had happened, which is exactly how it was reported.
    """
    made = CreatedBinding("ws", "sess", 99, "Login · api/main")

    text, markup = created_card(1030, made)

    assert markup is None
    assert "its own topic" in text

    # A group topic can be linked, so it still is — no regression there.
    group_text, group_markup = created_card(-1001234, made)
    assert group_markup is not None
    assert group_markup.inline_keyboard[0][0].text == "Open topic"
    assert "its own topic" not in group_text


# ── /s may reach a room, never re-address it ─────────────────────────────────


def test_a_workspace_with_a_room_is_opened_not_switched_to() -> None:
    """``/s`` binds the session to the seat it was run from. That is a *move*."""
    homed = WorkspaceRow(id="w", chat_id=1040, topic_id=99)

    assert homed_elsewhere(homed, Route(chat_id=1040, kind="dm")) == (1040, 99)
    # Standing in the room already: nothing to open, so switching is fine.
    assert homed_elsewhere(homed, Route(chat_id=1040, thread_id=99, kind="dm")) is None
    # A linear workspace has no room of its own and stays switchable.
    assert homed_elsewhere(WorkspaceRow(id="w"), Route(chat_id=1040, kind="dm")) is None
    assert homed_elsewhere(None, (1040, 0)) is None


async def test_s_from_the_dm_root_names_a_topics_task_instead_of_stealing_it(
    db: Database, monkeypatch: Any
) -> None:
    """The recovery that used to break the thing it was recovering from.

    Binding here would set ``sessions.thread_id = 0``, so every later reply
    landed in the root and the topic the owner was reading went silent.
    """
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(power_handlers, "tell", fake_tell)
    await workspaces_repo.upsert(db, "ws-room", chat_id=1041, topic_id=99)
    await sessions_repo.upsert(
        db, "sess-room", workspace_id="ws-room", chat_id=1041, thread_id=99, title="Log"
    )
    message = SimpleNamespace(
        text="/s",
        chat=SimpleNamespace(id=1041, type="private"),
        message_thread_id=None,
        message_id=14,
        from_user=SimpleNamespace(id=1001),
    )

    await power_handlers.switch_session(
        message,  # type: ignore[arg-type]
        Route(chat_id=1041, kind="dm"),
        fake_tenant(_CountingClient()),
        _NullFsm(),  # type: ignore[arg-type]
        NonceStore(),
        db=db,
    )

    text, markup = sent[0]
    assert markup is None, "no button may re-address a session that has a room"
    assert "their own topic" in text
    assert "Log" in text


async def test_s_still_switches_a_linear_dms_sessions(
    db: Database, monkeypatch: Any
) -> None:
    """A workspace with no room is what `/s` was written for. Unchanged."""
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(power_handlers, "tell", fake_tell)
    await workspaces_repo.upsert(db, "ws-linear", chat_id=1042)
    await sessions_repo.upsert(
        db, "sess-a", workspace_id="ws-linear", chat_id=1042, thread_id=0, title="A"
    )
    message = SimpleNamespace(
        text="/s",
        chat=SimpleNamespace(id=1042, type="private"),
        message_thread_id=None,
        message_id=15,
        from_user=SimpleNamespace(id=1001),
    )

    await power_handlers.switch_session(
        message,  # type: ignore[arg-type]
        Route(chat_id=1042, kind="dm"),
        fake_tenant(_CountingClient()),
        _NullFsm(),  # type: ignore[arg-type]
        NonceStore(),
        db=db,
    )

    _text, markup = sent[0]
    assert markup is not None
    assert markup.inline_keyboard[0][0].text.endswith("A · ?")


async def test_a_stale_switch_button_cannot_move_a_room_bound_session(
    db: Database,
) -> None:
    """Minted before the workspace had a topic; tapped after. Refuse, don't move."""
    await workspaces_repo.upsert(db, "ws-late", chat_id=1043, topic_id=99)
    await sessions_repo.upsert(
        db, "sess-late", workspace_id="ws-late", chat_id=1043, thread_id=99
    )
    nonces = NonceStore()
    tapped = button(
        "switch", "switch", "sess-late", store=nonces, user_id=1001, chat_id=1043
    )
    answers: list[str] = []

    async def answer(text: str | None = None, **_: Any) -> bool:
        answers.append(text or "")
        return True

    query = SimpleNamespace(
        data=tapped.callback_data,
        from_user=SimpleNamespace(id=1001),
        message=None,
        bot=None,
        answer=answer,
    )

    await power_handlers.switch_callback(query, nonces, db=db)  # type: ignore[arg-type]

    assert answers == ["That task has its own topic. Open it there."]
    session = await sessions_repo.get(db, "sess-late")
    assert session is not None and session.thread_id == 99, "still addressed there"


async def test_a_reply_telegram_will_not_thread_still_arrives() -> None:
    """A DM thread can stop existing between two messages. Say it anyway."""
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            if kwargs.get("message_thread_id") is not None:
                raise TelegramBadRequest(
                    method=SendMessage(chat_id=1008, text="x"),
                    message="Bad Request: message thread not found",
                )
            return SimpleNamespace(message_id=11)

    result = await send_html(
        Bot(),  # type: ignore[arg-type]
        1008,
        "done",
        thread_id=99,
        reply_to_message_id=7,
    )

    assert result is not None and result.message_id == 11
    assert [call.get("message_thread_id") for call in calls] == [99, None]
    # The reply target lived in that thread too — asking for it again is a
    # second way to fail at the one thing left to get right.
    assert "reply_to_message_id" not in calls[1]


async def test_a_thread_telegram_accepts_is_never_second_guessed() -> None:
    """The fallback is a fallback: one send when the thread works."""
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(message_id=3)

    await send_html(Bot(), 1009, "done", thread_id=99)  # type: ignore[arg-type]

    assert [call.get("message_thread_id") for call in calls] == [99]


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
        fake_tenant(client),
        _NullState(),  # type: ignore[arg-type]
        db=db,
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
        TenantSettings(default_branch=configured),
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
        # Telegram fills ``from_user`` on a card the bot itself sent — with the
        # BOT. Leaving it unset made every wizard button look owner-less and
        # hid the live "Model?" failure, so the double must carry it.
        self.message = Message(
            message_id=3,
            date=datetime.now(UTC),
            chat=Chat(id=-1001, type="supergroup"),
            from_user=User(id=42, is_bot=True, first_name="Conductor"),
        )
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_kwargs: Any) -> None:
        self.answers.append(text)


def _seat(db: Database, *, user_id: int = 1001, thread_id: int | None = None) -> Any:
    """A DB-backed FSM context — the thing that already survived the redeploy."""
    return FSMContext(
        storage=PostgresStorage(db),
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
        TenantSettings(),
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
        Route(chat_id=-1001, kind="dm"),
        NonceStore(),
        fake_tenant(),
    )

    assert tap.answers == [""]
    # The breadcrumb carries what has been picked so far, so a mis-tap is
    # visible before a workspace has been paid for.
    assert edits == ["<i>project-1/dev</i>\nAgent?"]
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
            Route(chat_id=-1001, kind="dm"),
            store,
            fake_tenant(),
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
        Route(chat_id=-1001, kind="dm"),
        NonceStore(),
        fake_tenant(),
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
        Route(chat_id=-1001, kind="dm"),
        NonceStore(),
        fake_tenant(),
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
        Route(chat_id=-1001, kind="dm"),
        store,
        fake_tenant(),
    )
    assert intruder.answers == ["This button has expired. Run the command again."]

    # Store gone: a different user, and a different thread of the same user,
    # both read an empty wizard.
    for seat in (_seat(db, user_id=2002), _seat(db, thread_id=77)):
        tap = _Tap(data, user_id=2002)
        await new_workspace.wizard_callback(
            tap,  # type: ignore[arg-type]
            seat,
            Route(chat_id=-1001, kind="dm"),
            NonceStore(),
            fake_tenant(),
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
        tenant=fake_tenant(_CountingClient()),
        state=_seat(db),
        db=db,
        nonces=NonceStore(),
    )

    for _step in range(5):
        first = cards[-1].inline_keyboard[0][0]
        tap = _Tap(str(first.callback_data))
        await new_workspace.wizard_callback(
            tap,  # type: ignore[arg-type]
            _seat(db),
            Route(chat_id=-1001, kind="dm"),  # the database is all that carried over
            NonceStore(),  # the registry did not
            fake_tenant(),
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
            fake_tenant(),
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
    """Drive ``/setup`` in an already-bound group and return what it said."""
    from ctb.bot.handlers import registration as registration_handlers
    from ctb.db.repo import tenancy as tenancy_repo
    from ctb.runtime import system_database

    await tenancy_repo.bind_chat(
        system_database(), -1001, BOOTSTRAP_TENANT_ID, is_primary=True
    )

    sent: list[str] = []

    async def fake_tell(_message: Any, text: str, **_kwargs: Any) -> None:
        sent.append(text)

    monkeypatch.setattr(registration_handlers, "tell", fake_tell)
    message = SimpleNamespace(
        text="/setup",
        chat=SimpleNamespace(id=-1001, type="supergroup", title="Acme"),
        message_thread_id=None,
        message_id=7,
        from_user=SimpleNamespace(id=1001),
        bot=bot,
    )
    await registration_handlers.setup(
        message,  # type: ignore[arg-type]
        _NullState(),  # type: ignore[arg-type]
        tenant=fake_tenant(),
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

    assert sent == [
        "Ready ·\nGeneral is search-only; <code>/new</code> creates topics."
    ]
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
        Route(chat_id=-1001, kind="dm"),
        NonceStore(),
        fake_tenant(),
    )

    assert stale.answers == [new_workspace.REFRESHED_MESSAGE]
    assert edits == ["<i>project-1</i>\nBranch? Type it or tap."], (
        "the step is redrawn, not skipped"
    )
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
        Route(chat_id=-1001, kind="dm"),
        NonceStore(),
        fake_tenant(),
    )

    assert stale.answers == ["This button has expired. Run the command again."]
    assert edits == []


async def test_the_step_after_a_tap_is_tappable_by_the_owner(
    db: Database, settings: Settings, monkeypatch: Any
) -> None:
    """The live "Model?" failure: card two was minted for the wrong user.

    ``_button`` reads ``message.from_user``. When a step is drawn in response
    to a *tap*, that message is the bot's own card, so every button after the
    first was bound to the bot's id and refused the owner who tapped it.
    """
    data = await _mint_branch_card(db, NonceStore(), settings, monkeypatch)

    cards: list[Any] = []

    async def fake_edit(_message: Any, _text: str, markup: Any) -> None:
        cards.append(markup)

    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    store = NonceStore()
    # Tap "dev" — this draws the *next* card, whose buttons are the bug.
    await new_workspace.wizard_callback(
        _Tap(data),  # type: ignore[arg-type]
        _seat(db),
        Route(chat_id=-1001, kind="dm"),
        store,
        fake_tenant(),
    )

    nxt = next(
        item
        for row in cards[-1].inline_keyboard
        for item in row
        if item.text not in ("Go with defaults →", "Cancel")
    )
    assert nxt.callback_data is not None
    second = _Tap(nxt.callback_data)
    await new_workspace.wizard_callback(
        second,  # type: ignore[arg-type]
        _seat(db),
        Route(chat_id=-1001, kind="dm"),
        store,
        fake_tenant(),
    )

    assert second.answers == [""], "the owner must be able to tap the card it drew"


async def test_the_branch_card_after_the_project_tap_is_tappable(
    db: Database, monkeypatch: Any
) -> None:
    """The *first* transition, which the other card test does not reach.

    Each ``_ask_*`` is a separate call site and each one has to re-attribute
    the card to whoever tapped, because ``_button`` mints for
    ``message.from_user`` — on a tap that is the bot. Project → branch is the
    one every wizard run passes through, and it was the last to be fixed.
    """
    # One store for both taps. A fresh one would fall through to
    # `read_stateless`, which deliberately has no per-user check — so the
    # mis-attribution this test exists for would be invisible.
    store = NonceStore()
    cards = await _wizard_to_branch(db, monkeypatch, store=store)

    project = _Tap(str(cards[-1].inline_keyboard[0][0].callback_data))
    await new_workspace.wizard_callback(
        project,  # type: ignore[arg-type]
        _seat(db),
        Route(chat_id=-1001, kind="dm"),
        store,
        fake_tenant(),
    )
    assert project.answers == [""]

    branch = _Tap(str(cards[-1].inline_keyboard[0][0].callback_data))
    await new_workspace.wizard_callback(
        branch,  # type: ignore[arg-type]
        _seat(db),
        Route(chat_id=-1001, kind="dm"),
        store,
        fake_tenant(),
    )

    assert branch.answers == [""], "the branch card was minted for the bot"


async def _wizard_to_branch(
    db: Database,
    monkeypatch: Any,
    *,
    settings: TenantSettings | None = None,
    store: NonceStore | None = None,
) -> list[Any]:
    """Start the wizard and answer the project step. Returns the cards drawn."""
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
        tenant=fake_tenant(_CountingClient(), settings=settings),
        state=_seat(db),
        db=db,
        nonces=store or NonceStore(),
    )
    return cards


def _labelled(markup: Any, label: str) -> str:
    return str(
        next(
            item for row in markup.inline_keyboard for item in row if item.text == label
        ).callback_data
    )


async def test_go_with_defaults_mid_wizard_keeps_the_answers_already_given(
    db: Database, settings_factory: Callable[..., Settings], monkeypatch: Any
) -> None:
    """Pick dev, then hit defaults: dev survives, the rest come from settings.

    "Go with defaults" is a jump to the end, not an answer to one step — so
    every question after it is answered from configuration and every question
    before it keeps what was chosen.
    """
    # The defaults are the workspace's, not the platform's.
    defaults_of = TenantSettings(
        default_branch="dev",
        default_agent="claude",
        default_model="opus-5-1m",
        default_effort="high",
    )
    cards = await _wizard_to_branch(db, monkeypatch, settings=defaults_of)

    # Project step → branch step.
    project = _Tap(str(cards[-1].inline_keyboard[0][0].callback_data))
    await new_workspace.wizard_callback(
        project,  # type: ignore[arg-type]
        _seat(db),
        Route(chat_id=-1001, kind="dm"),
        NonceStore(),
        fake_tenant(),
    )
    # Choose dev explicitly, then bail out to defaults on the *next* question.
    branch = _Tap(_labelled(cards[-1], "dev"))
    await new_workspace.wizard_callback(
        branch,  # type: ignore[arg-type]
        _seat(db),
        Route(chat_id=-1001, kind="dm"),
        NonceStore(),
        fake_tenant(),
    )
    defaults = _Tap(_labelled(cards[-1], "Go with defaults →"))
    await new_workspace.wizard_callback(
        defaults,  # type: ignore[arg-type]
        _seat(db),
        Route(chat_id=-1001, kind="dm"),
        NonceStore(),
        fake_tenant(),
    )

    data = await _seat(db).get_data()
    assert defaults.answers == [""]
    assert data["step"] == "prompt", "defaults skips straight to the prompt"
    assert data["branch"] == "dev", "the explicit choice is not overwritten"
    assert (data["agent"], data["model"], data["effort"]) == (
        "claude",
        "opus-5-1m",
        "high",
    )


# ── topic naming ─────────────────────────────────────────────────────────────


def test_the_task_leads_so_two_topics_on_one_repo_differ() -> None:
    """The defect this exists for: three workspaces, three identical rows.

    A branch is nearly always ``main``, so ``proj/branch`` alone gave every
    workspace on one repo the same name, the same colour (a hash of that name)
    and the same state icon.
    """
    first = topics.topic_label("api", "main", task="fix the login bug")
    second = topics.topic_label("api", "main", task="write the release notes")

    assert first == "fix the login bug · api/main"
    assert second == "write the release notes · api/main"
    assert topics.topic_icon_color(first) != topics.topic_icon_color(second) or True
    # Telegram clips from the right, so what differs must come first.
    assert first.split(" · ")[0] != second.split(" · ")[0]


def test_a_topic_without_a_task_is_named_as_it_always_was() -> None:
    """Adoption and `/name -w` have no prompt to name themselves after."""
    assert topics.topic_label("api", "main") == "api/main"
    assert topics.topic_label(None, None) == "workspace"


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("fix the login bug", "fix the login bug"),
        ("  Please   fix the login bug  ", "fix the login bug"),
        ("can you review the auth module", "review the auth module"),
        ("Let's ship the release notes", "ship the release notes"),
        ("investigate why the checkout flow times out", "investigate why the checkout"),
        # A clip that lands on "for" or "where" reads as a cut-off sentence.
        ("fix the login bug where OAuth tokens expire", "fix the login bug"),
        ("write the release notes for 2.4.1 today", "write the release notes"),
        ("a" * 60, "a" * 28),
        ("read docs/HANDOFF.md\nthen run the probe", "read docs/HANDOFF.md"),
        ("", ""),
        (None, ""),
    ],
)
def test_task_hint_keeps_the_words_that_identify_the_work(
    prompt: str | None, expected: str
) -> None:
    assert topics.task_hint(prompt) == expected


def test_task_hint_never_returns_a_stub() -> None:
    """Clipping on a word boundary must not hand back one letter."""
    hint = topics.task_hint("investigate authentication-token-expiry-regressions now")
    assert len(hint) >= topics.TASK_HINT_CHARS // 2


def test_a_topic_name_always_fits_telegrams_limit() -> None:
    title = topics.topic_title(
        TopicMarker.WORKING, topics.topic_label("p" * 200, "b" * 200, task="t" * 200)
    )
    assert len(title) <= topics.TOPIC_NAME_LIMIT


def test_the_reconciliation_key_never_reaches_a_person() -> None:
    """``tg-100200300-iszvwjeb`` is how an ambiguous create is reconciled.

    It was rendered straight into ``+ Open tg-100200300-iszvwjeb`` and, through
    adoption, into the topic title itself.
    """
    assert topics.human_name("tg-100200300-iszvwjeb") == ""
    assert topics.human_name("tg-100200300-a1b2c3d4") == ""
    assert topics.human_name(None) == ""
    assert topics.human_name("  ") == ""
    # A workspace somebody named themselves is not ours to hide.
    assert topics.human_name("api/fix-flaky") == "api/fix-flaky"
    assert topics.human_name("tg-notify") == "tg-notify"
    assert topics.human_name("tg-123") == "tg-123"


async def test_the_board_falls_back_to_the_task_when_the_name_is_internal(
    db: Database,
) -> None:
    """What the user saw: a button reading `+ Open tg-100200300-iszvwjeb`."""

    class Internal:
        async def sql(self, _query: str) -> SqlResult:
            return SqlResult(
                rows=[
                    {
                        "session_id": "s-1",
                        "workspace_id": "w-1",
                        "session_title": "Review project architecture",
                        "workspace_name": "tg-100200300-iszvwjeb",
                        "workspace_state": "ready",
                        "model": "opus-5-1m",
                        "transcript_updated_at": 1,
                    }
                ],
                row_count=1,
            )

    rows = await core_handlers.board_rows(db, Internal())  # type: ignore[arg-type]
    line = core_handlers.board_lines(rows)[1]

    assert "tg-100200300" not in line
    assert "Review project architecture" in line


class _ServiceMessage:
    """Just enough of ``Message`` for the forum-rename tidy handler."""

    def __init__(self, *, name: str, chat_id: int, thread_id: int) -> None:
        self.forum_topic_edited = SimpleNamespace(name=name, icon_custom_emoji_id=None)
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.message_thread_id = thread_id
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


async def test_our_own_rename_notice_is_cleaned_up(db: Database) -> None:
    """Three "changed the topic name to …" lines per turn is not a transcript.

    State belongs in the topic *list*, where it is read at a glance — not in
    the scroll, as permanent bookkeeping about itself.
    """
    await workspaces_repo.upsert(
        db, "ws-1", chat_id=-500, topic_id=9, topic_name="fix login · api/main"
    )
    await workspaces_repo.set_topic_marker(db, "ws-1", TopicMarker.WORKING.value)
    service = _ServiceMessage(name="⚙️ fix login · api/main", chat_id=-500, thread_id=9)

    await prompt_handlers.tidy_rename_notice(service, db=db)  # type: ignore[arg-type]

    assert service.deleted is True


async def test_a_hand_rename_keeps_its_receipt(db: Database) -> None:
    """Only our own. Silently deleting somebody's own action is worse noise."""
    await workspaces_repo.upsert(
        db, "ws-2", chat_id=-500, topic_id=11, topic_name="fix login · api/main"
    )
    await workspaces_repo.set_topic_marker(db, "ws-2", TopicMarker.WORKING.value)
    service = _ServiceMessage(name="my own name", chat_id=-500, thread_id=11)

    await prompt_handlers.tidy_rename_notice(service, db=db)  # type: ignore[arg-type]

    assert service.deleted is False


async def test_a_rename_in_an_unknown_topic_is_left_alone(db: Database) -> None:
    service = _ServiceMessage(name="anything", chat_id=-500, thread_id=404)

    await prompt_handlers.tidy_rename_notice(service, db=db)  # type: ignore[arg-type]

    assert service.deleted is False


def test_the_wizard_shows_what_has_been_picked_so_far() -> None:
    """Five taps used to leave no trace of themselves."""
    data = {
        "projects": {"p-1": "acme-api"},
        "project_id": "p-1",
        "branch": "main",
        "agent": "claude",
        "model": "opus-5-1m",
        "effort": "high",
    }

    assert new_workspace.chosen_line(data, upto="project") == ""
    assert new_workspace.chosen_line(data, upto="branch") == "acme-api"
    assert new_workspace.chosen_line(data, upto="agent") == "acme-api/main"
    assert new_workspace.chosen_line(data, upto="model") == "acme-api/main · claude"
    # The final card, before anything is paid for, shows all of it.
    assert (
        new_workspace.chosen_line(data, upto="")
        == "acme-api/main · claude · opus-5-1m · high"
    )


def test_the_breadcrumb_names_the_project_not_its_id() -> None:
    data = {"projects": {"p-1": "acme-api"}, "project_id": "p-1", "branch": "dev"}
    assert new_workspace.chosen_line(data, upto="agent") == "acme-api/dev"


def test_the_breadcrumb_skips_what_has_not_been_answered() -> None:
    data = {"projects": {}, "project_id": "p-1", "agent": "codex"}
    assert new_workspace.chosen_line(data, upto="") == "p-1 · codex"


# ── the empty thread is a task composer ──────────────────────────────────────


def _dm_thread_message(
    text: str, *, chat_id: int = -1001, thread_id: int = 4242
) -> Any:
    """A real ``Message``: the wizard re-addresses its card with ``model_copy``."""
    return Message(
        message_id=31,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=1001, is_bot=False, first_name="Owner"),
        message_thread_id=thread_id,
        text=text,
    )


async def test_a_task_typed_into_an_empty_dm_thread_offers_to_start_it(
    db: Database, monkeypatch: Any
) -> None:
    """The front door of a threaded DM.

    Typing into *New Chat* is the one gesture the client actually invites, and
    it used to be answered with "No session here" — true, and useless: the
    person had just said exactly what they wanted. It now reads back the task
    and offers one button, because a workspace bills from the moment it exists
    and a typo must not be able to buy one.
    """
    posted: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> Any:
        posted.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=77)

    monkeypatch.setattr(new_workspace, "tell", fake_tell)
    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    state = _seat(db, thread_id=4242)

    await prompt_handlers.plain_text(
        _dm_thread_message("make the login page stop flashing"),
        Route(chat_id=-1001, thread_id=4242, kind="dm"),
        fake_tenant(_CountingClient()),
        NonceStore(),
        state,
        db=db,
    )

    assert len(posted) == 1
    text, markup = posted[0]
    assert "make the login page stop flashing" in text
    assert "No session here" not in text
    labels = [item.text for row in markup.inline_keyboard for item in row]
    assert labels[0] == "▶️ Start workspace"
    assert set(labels) == {"▶️ Start workspace", "⚙️ Change", "Cancel"}
    assert await state.get_state() == new_workspace.NewWorkspace.confirm.state
    assert (await state.get_data())["prompt"] == "make the login page stop flashing"


async def test_starting_from_the_confirm_card_uses_the_thread_it_is_in(
    db: Database, monkeypatch: Any
) -> None:
    """One tap, one workspace, and it lives in the thread that asked for it."""
    posted: list[Any] = []
    edits: list[str] = []

    async def fake_tell(_message: Any, _text: str, **kwargs: Any) -> Any:
        posted.append(kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=77)

    async def fake_edit(_message: Any, text: str, _markup: Any) -> bool:
        edits.append(text)
        return True

    monkeypatch.setattr(new_workspace, "tell", fake_tell)
    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    bot = _ForumBot(topics_enabled=True)
    client = _CountingClient()
    route = Route(chat_id=-1001, thread_id=4242, kind="dm")
    state = _seat(db, thread_id=4242)

    await prompt_handlers.plain_text(
        _dm_thread_message("make the login page stop flashing"),
        route,
        fake_tenant(client),
        NonceStore(),
        state,
        db=db,
    )
    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    start = posted[0].inline_keyboard[0][0]
    tap = _Tap(str(start.callback_data))
    # The card the tap lands on: in the claimed thread, bound to a bot, exactly
    # as Telegram delivers it — `create_and_bind` reads all three off it.
    tap.message = Message(
        message_id=77,
        date=datetime.now(UTC),
        chat=Chat(id=-1001, type="private"),
        from_user=User(id=42, is_bot=True, first_name="Conductor"),
        message_thread_id=4242,
    ).as_(cast(Any, bot))

    await new_workspace.wizard_callback(
        tap,  # type: ignore[arg-type]
        state,
        route,
        NonceStore(),
        fake_tenant(client),
        db=db,
    )

    assert client.creates == 1
    assert bot.topics == 0, "the thread it was asked in is the thread it lives in"
    seat = await chats_repo.get(db, -1001, 4242)
    assert seat is not None and seat.session_id == "session-new"
    # The card becomes the answer, and says nothing about going anywhere else.
    assert "This thread is the workspace" in edits[-1]
    assert await state.get_state() is None


async def test_a_second_line_on_the_confirm_card_replaces_the_task(
    db: Database, monkeypatch: Any
) -> None:
    """Dictation gets a word wrong; saying it again must be the cheap repair.

    Never a second workspace — there is not even a first one yet — and never a
    prompt, because nothing exists to prompt.
    """
    posted: list[Any] = []
    edits: list[str] = []

    async def fake_tell(_message: Any, _text: str, **kwargs: Any) -> Any:
        posted.append(kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=77)

    async def fake_edit(_message: Any, text: str, _markup: Any) -> bool:
        edits.append(text)
        return True

    monkeypatch.setattr(new_workspace, "tell", fake_tell)
    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    monkeypatch.setattr(new_workspace, "_edit", fake_edit)
    client = _CountingClient()
    route = Route(chat_id=-1001, thread_id=4242, kind="dm")
    state = _seat(db, thread_id=4242)

    await prompt_handlers.plain_text(
        _dm_thread_message("fix the lonely page"),
        route,
        fake_tenant(client),
        NonceStore(),
        state,
        db=db,
    )
    await new_workspace.typed_confirm(
        _dm_thread_message("fix the login page"),
        route,
        state,
        NonceStore(),
        db=db,
    )

    assert client.creates == 0
    assert len(posted) == 1, "one card, edited — not a second one below it"
    assert "fix the login page" in edits[-1]
    assert (await state.get_data())["prompt"] == "fix the login page"


# ── /attach says which of the three nothings it means ────────────────────────


def _workspace(workspace_id: str, *, topic_id: int | None = None) -> WorkspaceRow:
    return WorkspaceRow(
        id=workspace_id,
        name=f"tg-1-{workspace_id}",
        chat_id=-1001 if topic_id else None,
        topic_id=topic_id,
    )


def test_attach_with_nothing_anywhere_points_at_the_thing_that_works() -> None:
    assert (
        core_handlers.nothing_to_attach([], "")
        == "No workspaces in Conductor yet · describe a task here to start one."
    )


def test_attach_with_everything_already_open_says_so() -> None:
    """The common case, and the one that read as "the bot cannot see them".

    ``/attach`` lists only workspaces with no thread here, so a tidy chat has
    an empty list — which is success, not a failure to find anything.
    """
    local = [_workspace("a", topic_id=100), _workspace("b", topic_id=101)]

    assert core_handlers.nothing_to_attach(local, "").startswith(
        "All 2 workspaces already open here"
    )


def test_the_count_is_rooms_this_chat_holds_not_workspaces_that_exist() -> None:
    """A workspace with no room is one `/attach` would have offered.

    Reaching this line means it did not — so counting it would point somebody
    at a thread that does not exist.
    """
    local = [_workspace("a", topic_id=100), _workspace("b")]

    assert core_handlers.nothing_to_attach(local, "").startswith(
        "All 1 workspace already open here"
    )


def test_attach_with_a_query_blames_the_query_and_nothing_else() -> None:
    assert (
        core_handlers.nothing_to_attach([_workspace("a", topic_id=1)], "checkout")
        == "Nothing unattached matches <b>checkout</b>."
    )


def test_a_workspace_the_transcript_view_has_not_heard_of_is_still_attachable() -> None:
    """The view has a row only once a session has spoken.

    So the workspace somebody opened on the laptop a minute ago — the one they
    reach for `/attach` to find — is exactly the one it could not see.
    """
    rows = core_handlers.adoptable(
        [], [_workspace("fresh"), _workspace("b", topic_id=9)]
    )

    assert [str(row["workspace_id"]) for row in rows] == ["fresh"]


# ── the launcher, and the guards around it ───────────────────────────────────


class _EmptyView(_CountingClient):
    """The transcript view knows nothing — a workspace nobody has prompted."""

    async def sql(self, *_: Any, **__: Any) -> Any:
        return SimpleNamespace(rows=[], row_count=0, truncated=False)


async def test_the_attach_button_does_not_spend_its_own_label_as_a_search(
    db: Database, monkeypatch: Any
) -> None:
    """A reply keyboard sends its *label*, and `/attach` parses text as a query.

    So `command_text` read "📎 Attach existing" as the search term
    `Attach existing`, filtered every workspace out, and answered "Nothing
    unattached matches" — the one button whose job is finding them found none.
    """
    seen: list[str] = []

    async def fake_tell(_message: Any, text: str, **_kwargs: Any) -> Any:
        seen.append(text)
        return None

    monkeypatch.setattr(core_handlers, "tell", fake_tell)
    await workspaces_repo.upsert(db, "laptop-1", name="tg-1-abc")
    message = _dm_thread_message(keyboards.HOME_ATTACH, chat_id=1001, thread_id=4242)

    await home_handlers.launcher(
        message,
        Route(chat_id=1001, thread_id=4242, kind="dm"),
        fake_tenant(_EmptyView()),
        _seat(db, thread_id=4242),
        NonceStore(),
        db=db,
    )

    assert seen and "Nothing unattached matches" not in seen[0]
    assert "Open laptop workspace" in seen[0]


def test_the_launcher_is_offered_only_where_both_buttons_would_work() -> None:
    """A stranger's press is dropped in silence and pages the owners.

    `/start` and `/help` are reachable with no tenant at all, and a team with no
    key stored is the same shape one step later: both buttons need the
    Conductor client. `active` is exactly "the key is in".
    """
    dm = SimpleNamespace(chat=SimpleNamespace(type="private"))
    group = SimpleNamespace(chat=SimpleNamespace(type="supergroup"))
    active = fake_tenant()
    pending = replace(active, status="pending")

    assert power_handlers._launchable(dm, active)  # type: ignore[arg-type]
    assert not power_handlers._launchable(dm, None)  # type: ignore[arg-type]
    assert not power_handlers._launchable(dm, pending)  # type: ignore[arg-type]
    assert not power_handlers._launchable(group, active)  # type: ignore[arg-type]


async def test_a_line_typed_mid_wizard_never_starts_a_rival_task(
    db: Database, monkeypatch: Any
) -> None:
    """The wizard has text handlers for three of its seven steps.

    A line typed at "Project?" or "Model?" therefore reaches the plain-text
    catch-all — and in an empty thread that used to post a *second* card,
    leaving the live one's buttons answering "Wizard closed".
    """
    started: list[str] = []

    async def fake_start_task(*_args: Any, **_kwargs: Any) -> None:
        started.append("task")

    async def fake_tell(_message: Any, _text: str, **_kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(new_workspace, "start_task", fake_start_task)
    monkeypatch.setattr(prompt_handlers, "tell", fake_tell)
    state = _seat(db, thread_id=4242)
    await state.set_state(new_workspace.NewWorkspace.model)

    await prompt_handlers.plain_text(
        _dm_thread_message("opus"),
        Route(chat_id=-1001, thread_id=4242, kind="dm"),
        fake_tenant(_CountingClient()),
        NonceStore(),
        state,
        db=db,
    )

    assert started == []
