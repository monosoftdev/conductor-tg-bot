"""Adopting a workspace that already exists in Conductor.

The owner's flow: create a cloud workspace on the laptop, see it on the phone,
see where it got to, keep going from there. ``/board`` already listed it; these
tests pin the missing link — the topic, the binding, and the one rule adoption
must never break:

    **The snapshot card is a read-only look at the tail.** It creates no
    ``deliveries`` row and it does not move the cursor. The cursor is seeded by
    ``seek_to_end`` alone, at the end, so nothing older is ever mirrored.

Driven through the real :class:`ConductorClient` over the scripted fake API, so
the URLs, the ``offset``/``after`` rules and the 404 handling are production's.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import SendMessage

from ctb.bot.app import PostgresStorage
from ctb.bot.handlers import adopt as adopt_handlers
from ctb.bot.handlers import common
from ctb.bot.handlers import core as core_handlers
from ctb.bot.handlers import power as power_handlers
from ctb.bot.handlers import prompts as prompt_handlers
from ctb.bot.handlers.adopt import (
    EXCHANGE_CHARS,
    AdoptError,
    AdoptResult,
    ack_line,
    adopt_workspace,
    snapshot_card,
)
from ctb.bot.handlers.common import (
    LINEAR_DM_NOTICE,
    MOBILE_REPLY_INSTRUCTION,
    augment_prompt,
)
from ctb.bot.keyboards import NonceStore
from ctb.bot.middleware.routing import Route
from ctb.bot.middleware.tenancy import TenantContext, TenantSettings
from ctb.conductor.client import ConductorClient
from ctb.conductor.models import TranscriptMessage, WorkspaceStatusValue
from ctb.db.connection import Database
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.tenancy import TenantRow
from ctb.settings import Settings
from ctb.turn.state import TopicMarker
from tests.conftest import FAKE_API_KEY
from tests.fakes.fake_conductor import (
    FakeConductor,
    FakeSession,
    assistant,
    result,
    user_message,
)
from tests.pg import BOOTSTRAP_TENANT_ID


def fake_tenant(
    client: Any = None,
    *,
    role: str = "owner",
    user_id: int = 1001,
    slug: str = "test",
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
        settings=TenantSettings(),
        row=TenantRow(id=BOOTSTRAP_TENANT_ID, slug=slug, name=slug, status="active"),
        _client=client,
    )


CHAT_ID = -1002000000000
FIRST_TOPIC = 101


def _seat(db: Database, *, user_id: int = 1001, thread_id: int | None = None) -> Any:
    """A DB-backed FSM context, as the dispatcher would hand a handler."""
    return FSMContext(
        storage=PostgresStorage(db),
        key=StorageKey(bot_id=0, chat_id=CHAT_ID, user_id=user_id, thread_id=thread_id),
    )


class _Bot:
    """Just enough of ``Bot`` for the topic + card half of adoption."""

    def __init__(self) -> None:
        self.topics: list[int] = []
        self.renamed: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.reactions: list[str] = []
        self.deleted: list[int] = []
        self.closed: list[int] = []
        self.edit_error: Exception | None = None
        #: Threads Telegram has forgotten — a topic deleted from the phone.
        self.dead: set[int] = set()
        #: @BotFather's Threaded Mode, as ``getMe`` reports it. ``None`` is the
        #: shape Telegram actually sends when it is off, and the bot tries
        #: anyway; ``False`` is the only explicit refusal.
        self.has_topics_enabled: bool | None = True
        #: Telegram refusing ``createForumTopic`` — the Bot API 10.0 DM
        #: regression, and the branch adoption's linear fallback exists for.
        self.create_error: Exception | None = None

    async def get_me(self) -> Any:
        return SimpleNamespace(
            id=42, has_topics_enabled=self.has_topics_enabled, username="ctb"
        )

    async def create_forum_topic(self, **kwargs: Any) -> Any:
        if self.create_error is not None:
            raise self.create_error
        thread_id = FIRST_TOPIC + len(self.topics)
        self.topics.append(thread_id)
        self.renamed.append(str(kwargs["name"]))
        return SimpleNamespace(message_thread_id=thread_id)

    async def edit_forum_topic(self, **kwargs: Any) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        if int(kwargs.get("message_thread_id") or 0) in self.dead:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=CHAT_ID, text="x"),
                message="Bad Request: message thread not found",
            )
        self.renamed.append(str(kwargs["name"]))

    async def delete_forum_topic(self, **kwargs: Any) -> None:
        self.deleted.append(int(kwargs["message_thread_id"]))

    async def close_forum_topic(self, **kwargs: Any) -> None:
        self.closed.append(int(kwargs["message_thread_id"]))

    async def send_message(self, **kwargs: Any) -> Any:
        if int(kwargs.get("message_thread_id") or 0) in self.dead:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=CHAT_ID, text="x"),
                message="Bad Request: message thread not found",
            )
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=900 + len(self.sent))

    async def set_message_reaction(self, **kwargs: Any) -> None:
        self.reactions.append(str(kwargs["reaction"][0].emoji))

    def cards_in(self, thread_id: int) -> list[str]:
        return [
            str(item["text"])
            for item in self.sent
            if int(item.get("message_thread_id") or 0) == thread_id
        ]


class _Query:
    """The half of ``CallbackQuery`` a nonce-backed handler touches."""

    def __init__(self, bot: _Bot, data: str, *, user_id: int = 1001) -> None:
        self.bot = bot
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=CHAT_ID, type="supergroup")
        )
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_: Any) -> None:
        self.answers.append(text)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def fake() -> FakeConductor:
    return FakeConductor()


@pytest.fixture
async def client(
    settings: Settings, fake: FakeConductor
) -> AsyncIterator[ConductorClient]:
    instance = ConductorClient(
        api_key=FAKE_API_KEY,
        api_url=settings.conductor_api_url,
        transport=fake.transport(),
        sleep=_no_sleep,
        rng=random.Random(20260726),
        max_attempts=2,
    )
    try:
        yield instance
    finally:
        await instance.aclose()


def _seeded(
    fake: FakeConductor,
    *,
    name: str = "checkout",
    status: WorkspaceStatusValue = WorkspaceStatusValue.READY,
    prompt: str = "fix the flaky checkout test",
    answer: str = "Fixed. The fixture was shared between two tests.",
) -> FakeSession:
    """A remote workspace with one finished exchange — the laptop's leftovers."""
    workspace = fake.add_workspace(name, status=status)
    return fake.add_session(
        workspace=workspace,
        seed=[user_message(prompt), assistant(answer), result("done")],
    )


async def _deliveries(db: Database) -> list[Any]:
    return await db.fetch_all("SELECT * FROM deliveries")


async def _adopt(
    bot: _Bot,
    db: Database,
    client: ConductorClient,
    session: FakeSession,
    *,
    chat_type: str = "supergroup",
    session_hint: str | None = None,
    claim_thread: int = 0,
) -> AdoptResult:
    return await adopt_workspace(
        bot=bot,  # type: ignore[arg-type]
        db=db,
        client=client,
        chat_id=CHAT_ID,
        chat_type=chat_type,
        workspace_id=session.workspace_id,
        session_hint=session_hint,
        claim_thread=claim_thread,
    )


# ── binding ──────────────────────────────────────────────────────────────────


async def test_adopting_a_remote_workspace_opens_one_topic_and_binds_it(
    db: Database, system_db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake)
    bot = _Bot()

    outcome = await _adopt(bot, db, client, session)

    assert bot.topics == [FIRST_TOPIC]
    assert bot.renamed == ["checkout/main"]
    assert outcome.thread_id == FIRST_TOPIC
    assert not outcome.already

    workspace = await workspaces_repo.get(db, session.workspace_id)
    assert workspace is not None
    assert (workspace.chat_id, workspace.topic_id) == (CHAT_ID, FIRST_TOPIC)
    assert workspace.deep_link == session.workspace.deep_link

    row = await sessions_repo.get(db, session.session_id)
    assert row is not None
    assert row.is_bound and (row.chat_id, row.thread_id) == (CHAT_ID, FIRST_TOPIC)

    chat = await chats_repo.get(db, CHAT_ID, FIRST_TOPIC)
    assert chat is not None
    assert (chat.workspace_id, chat.session_id) == (
        session.workspace_id,
        session.session_id,
    )
    # Bound is all the supervisor asks for; the poller follows within 5s.
    assert [r.id for r in await sessions_repo.list_bound(system_db)] == [
        session.session_id
    ]


async def test_the_snapshot_card_creates_no_deliveries_and_never_moves_the_cursor(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """The one rule this feature must not break.

    The card is rendered and sent outside the outbox: no ``deliveries`` row can
    exist for it, and the cursor sits on the newest message — so the three
    messages it quotes are behind the cursor and will never be mirrored again.
    """
    session = _seeded(fake)
    newest = session.messages_model()[-1]
    bot = _Bot()

    await _adopt(bot, db, client, session)

    assert await _deliveries(db) == []
    row = await sessions_repo.get(db, session.session_id)
    assert row is not None
    assert row.seeded
    assert row.cursor_session_index == newest.session_index
    assert row.cursor_message_id == newest.id
    # …and the card that quoted them went to the topic, not through the outbox.
    card = bot.cards_in(FIRST_TOPIC)[0]
    assert "👤 fix the flaky checkout test" in card
    assert "🤖 Fixed. The fixture was shared between two tests." in card
    assert card.endswith("<i>Snapshot · live from here</i>")


async def test_a_second_adoption_jumps_to_the_topic_it_already_opened(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake)
    bot = _Bot()

    first = await _adopt(bot, db, client, session)
    second = await _adopt(bot, db, client, session)

    assert bot.topics == [FIRST_TOPIC]
    assert first.thread_id == second.thread_id == FIRST_TOPIC
    assert second.already and not first.already
    assert len(await workspaces_repo.list_all(db)) == 1
    # Re-opening is navigation, not another snapshot or a session switch.
    assert await _deliveries(db) == []
    assert len(bot.cards_in(FIRST_TOPIC)) == 1


async def test_three_concurrent_adoptions_create_only_one_topic(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake)
    bot = _Bot()

    outcomes = await asyncio.gather(
        _adopt(bot, db, client, session),
        _adopt(bot, db, client, session),
        _adopt(bot, db, client, session),
    )

    assert bot.topics == [FIRST_TOPIC]
    assert [outcome.already for outcome in outcomes].count(False) == 1
    assert [outcome.already for outcome in outcomes].count(True) == 2
    assert session.workspace_id not in adopt_handlers._locks


async def test_a_second_adoption_keeps_the_visible_and_stored_prefix_in_sync(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """The live topic rename and cached marker must move as one observation."""
    session = _seeded(fake)
    bot = _Bot()
    await _adopt(bot, db, client, session)
    await workspaces_repo.set_topic_marker(
        db, session.workspace_id, TopicMarker.WORKING.value
    )

    await _adopt(bot, db, client, session)

    workspace = await workspaces_repo.get(db, session.workspace_id)
    assert workspace is not None
    assert workspace.topic_marker == TopicMarker.IDLE.value
    assert bot.renamed[-1] == "checkout/main"


async def test_a_topic_deleted_out_from_under_us_is_reopened(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """Only Telegram's explicit not-found response permits a replacement."""
    session = _seeded(fake)
    bot = _Bot()
    await _adopt(bot, db, client, session)

    bot.dead.add(FIRST_TOPIC)
    outcome = await _adopt(bot, db, client, session)

    assert outcome.thread_id == FIRST_TOPIC + 1
    assert not outcome.already
    workspace = await workspaces_repo.get(db, session.workspace_id)
    assert workspace is not None
    assert workspace.topic_id == FIRST_TOPIC + 1
    assert bot.cards_in(FIRST_TOPIC + 1)


async def test_a_workspace_bound_to_another_chat_is_not_silently_moved(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake)
    await workspaces_repo.upsert(
        db, session.workspace_id, chat_id=-1009999, topic_id=7, topic_name="elsewhere"
    )
    bot = _Bot()

    with pytest.raises(AdoptError, match="another Telegram chat"):
        await _adopt(bot, db, client, session)

    workspace = await workspaces_repo.get(db, session.workspace_id)
    assert workspace is not None
    assert (workspace.chat_id, workspace.topic_id) == (-1009999, 7)
    assert bot.topics == []


async def test_an_ambiguous_topic_check_never_creates_a_duplicate(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake)
    bot = _Bot()
    await _adopt(bot, db, client, session)
    bot.edit_error = TelegramNetworkError(
        method=SendMessage(chat_id=CHAT_ID, text="x"),
        message="connection reset",
    )

    with pytest.raises(AdoptError, match="Topic check unavailable"):
        await _adopt(bot, db, client, session)

    assert bot.topics == [FIRST_TOPIC]


async def test_a_partial_binding_repairs_the_remembered_topic_in_place(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """A crash between workspace and route writes must not clone the topic."""
    session = _seeded(fake)
    await workspaces_repo.upsert(
        db,
        session.workspace_id,
        chat_id=CHAT_ID,
        topic_id=77,
        topic_name="checkout/main",
    )
    bot = _Bot()

    outcome = await _adopt(bot, db, client, session)

    assert outcome.thread_id == 77
    assert not outcome.already
    assert bot.topics == []
    route = await chats_repo.get(db, CHAT_ID, 77)
    assert route is not None and route.session_id == session.session_id
    assert bot.cards_in(77)


async def test_a_dm_that_can_host_topics_gets_one_like_any_other_chat(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """The two entry points finally agree.

    ``/new`` has opened a topic per workspace in a private chat for a while;
    adoption did not, so a workspace opened from ``/board`` took the DM's
    single seat instead. In a *threaded* DM that seat is Telegram's "New Chat"
    composer, which shows nothing and creates a thread out of anything typed
    into it — so the transcript was being delivered to a room the owner cannot
    read. This is the fix, and the assertion that used to record the gap.
    """
    session = _seeded(fake)
    bot = _Bot()

    outcome = await _adopt(bot, db, client, session, chat_type="private")

    assert bot.topics == [FIRST_TOPIC]
    assert outcome.thread_id == FIRST_TOPIC
    chat = await chats_repo.get(db, CHAT_ID, FIRST_TOPIC)
    assert chat is not None and chat.kind == "topic"
    assert chat.session_id == session.session_id
    assert bot.cards_in(FIRST_TOPIC)


async def test_a_dm_with_threads_off_still_falls_back_to_the_linear_seat(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """A refusal degrades; it never fails the adoption.

    Only an explicit ``has_topics_enabled: False`` counts as a refusal, and
    thread 0 is a perfectly readable seat in a DM that has no threads — it is
    unreadable only in one that does.
    """
    session = _seeded(fake)
    bot = _Bot()
    bot.has_topics_enabled = False

    outcome = await _adopt(bot, db, client, session, chat_type="private")

    assert bot.topics == []
    assert outcome.thread_id == 0
    chat = await chats_repo.get(db, CHAT_ID, 0)
    assert chat is not None and chat.kind == "dm"
    assert chat.session_id == session.session_id


async def test_adopting_into_the_thread_it_was_asked_from_opens_nothing_new(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """``/attach`` typed into "New Chat" lands in the thread Telegram just made.

    Telegram opened that thread the moment the command was sent and named it
    after the command — so ``/attach`` produced a stray thread called
    "/attach" *and* a second one for the workspace. The room the request is
    already in is the room, and it is renamed to the workspace.
    """
    session = _seeded(fake)
    bot = _Bot()

    outcome = await _adopt(
        bot, db, client, session, chat_type="private", claim_thread=555
    )

    assert bot.topics == []
    assert outcome.thread_id == 555
    # It wore "/attach" until this rename; the topic list is how you navigate.
    assert any(outcome.name in name for name in bot.renamed)
    chat = await chats_repo.get(db, CHAT_ID, 555)
    assert chat is not None and chat.session_id == session.session_id
    workspace = await workspaces_repo.get(db, session.workspace_id)
    assert workspace is not None and workspace.topic_id == 555


async def test_a_second_attach_in_a_dm_jumps_instead_of_opening_a_sibling(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """The remembered-topic path used to be skipped in a private chat entirely."""
    session = _seeded(fake)
    bot = _Bot()

    first = await _adopt(bot, db, client, session, chat_type="private")
    second = await _adopt(bot, db, client, session, chat_type="private")

    assert bot.topics == [FIRST_TOPIC]
    assert second.thread_id == first.thread_id
    assert second.already


# ── which session ────────────────────────────────────────────────────────────


async def test_the_newest_session_is_adopted_and_the_reply_says_so(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake)
    newer = fake.add_session(
        workspace=session.workspace,
        title="rewrite the checkout fixture",
        seed=[user_message("rewrite it"), assistant("Done.")],
    )
    bot = _Bot()

    outcome = await _adopt(bot, db, client, session, session_hint=newer.session_id)

    assert outcome.session_id == newer.session_id
    assert outcome.sessions == 2
    assert (
        bot.cards_in(FIRST_TOPIC)[0].splitlines()[0]
        == "2 sessions · <b>rewrite the checkout fixture</b>"
    )
    imported = await sessions_repo.list_for_workspace(db, session.workspace_id)
    assert {row.id for row in imported} == {session.session_id, newer.session_id}
    assert [row.id for row in imported if row.is_bound] == [newer.session_id]


async def test_an_unknown_session_hint_falls_back_to_the_listing(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake)

    outcome = await _adopt(_Bot(), db, client, session, session_hint="not-in-here")

    assert outcome.session_id == session.session_id


# ── edge cases ───────────────────────────────────────────────────────────────


async def test_a_workspace_with_no_sessions_is_refused_before_a_topic_exists(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    empty = fake.add_workspace("empty")
    bot = _Bot()

    with pytest.raises(AdoptError, match="No sessions in this workspace yet."):
        await adopt_workspace(
            bot=bot,  # type: ignore[arg-type]
            db=db,
            client=client,
            chat_id=CHAT_ID,
            chat_type="supergroup",
            workspace_id=empty.id,
        )

    assert bot.topics == []
    assert await workspaces_repo.list_all(db) == []


async def test_an_archived_workspace_is_refused_with_the_restore_path(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake, status=WorkspaceStatusValue.ARCHIVED)
    bot = _Bot()

    with pytest.raises(AdoptError, match="Archived in Conductor"):
        await _adopt(bot, db, client, session)

    assert bot.topics == []


async def test_a_missing_workspace_says_so_instead_of_crashing(
    db: Database, client: ConductorClient
) -> None:
    bot = _Bot()

    with pytest.raises(AdoptError, match="Workspace unavailable"):
        await adopt_workspace(
            bot=bot,  # type: ignore[arg-type]
            db=db,
            client=client,
            chat_id=CHAT_ID,
            chat_type="supergroup",
            workspace_id="workspace-gone",
        )

    assert bot.topics == []


async def test_a_failed_seek_removes_the_fresh_topic_and_partial_cache(
    db: Database,
    client: ConductorClient,
    fake: FakeConductor,
    monkeypatch: Any,
) -> None:
    session = _seeded(fake)
    bot = _Bot()

    async def fail_seek(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("transcript unavailable")

    monkeypatch.setattr(adopt_handlers.cursor, "seek_to_end", fail_seek)
    with pytest.raises(RuntimeError, match="transcript unavailable"):
        await _adopt(bot, db, client, session)

    assert bot.deleted == [FIRST_TOPIC]
    assert await workspaces_repo.get(db, session.workspace_id) is None
    assert await sessions_repo.get(db, session.session_id) is None


async def test_a_sleeping_workspace_is_adopted_and_the_card_is_honest(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """Probe assumption 8 is unmeasured, so the card promises nothing."""
    session = _seeded(fake, status=WorkspaceStatusValue.SLEEPING)
    bot = _Bot()

    outcome = await _adopt(bot, db, client, session)

    assert outcome.sleeping and outcome.thread_id == FIRST_TOPIC
    card = bot.cards_in(FIRST_TOPIC)[0]
    assert "💤 Sleeping. A prompt may wake it — unverified." in card
    # The sleeping marker is on the topic, not just in the bubble.
    assert bot.renamed == ["💤 checkout/main"]


async def test_an_empty_transcript_says_nothing_yet_rather_than_faking_one(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    workspace = fake.add_workspace("fresh")
    session = fake.add_session(workspace=workspace)
    bot = _Bot()

    await _adopt(bot, db, client, session)

    assert bot.cards_in(FIRST_TOPIC) == ["<i>Nothing yet · live from here</i>"]
    row = await sessions_repo.get(db, session.session_id)
    assert row is not None and row.seeded and row.cursor_session_index == -1


# ── the card itself ──────────────────────────────────────────────────────────


def test_the_card_shows_the_prompt_without_the_output_contract(
    message_factory: Callable[..., TranscriptMessage],
) -> None:
    """Every Telegram prompt carries 240 words of formatting rules. Not content."""
    assert "OUTPUT CONTRACT" in MOBILE_REPLY_INSTRUCTION
    echo = message_factory(
        4, kind="userMessage", text=augment_prompt("fix the flaky test")
    )

    card = snapshot_card([echo])

    assert card.splitlines()[0] == "👤 fix the flaky test"
    assert "OUTPUT CONTRACT" not in card


def test_the_card_clips_each_side_to_a_phone_length() -> None:
    long_answer = TranscriptMessage(
        id="m-1",
        session_id="session-1",
        type="agent",
        content={
            "type": "agent",
            "rawPayload": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "word " * 400}]},
            },
        },
    )

    line = snapshot_card([long_answer]).splitlines()[0]

    assert line.startswith("🤖 ")
    assert line.endswith("…")
    assert len(line) <= EXCHANGE_CHARS + 3


def test_the_card_says_what_a_tool_call_did_not_what_it_was_called() -> None:
    """Exhibit A, end to end.

    The live card read: ``🤖 claude-opus-5 msg_011Cd… message assistant
    tool_use toolu_01Jz… Bash git add app/models/org.py && git commit -q -m
    "$(cat <<'EOF' chore:…``. Every token in that string is machine
    bookkeeping the owner cannot act on.
    """
    tool_call = TranscriptMessage(
        id="m-1",
        session_id="session-1",
        type="agentMessage",
        content={
            "type": "agentMessage",
            "rawPayload": {
                "type": "assistant",
                "message": {
                    "id": "msg_011CdRjDXXYG6KcJeuk1oXiu",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01Jzh4cfPmYAZJLZK4CKTXN1",
                            "name": "Bash",
                            "input": {
                                "command": (
                                    "git add app/models/org.py && git commit "
                                    "-q -m \"$(cat <<'EOF'\n"
                                    "chore: add hello world comment to Org "
                                    "model (Conductor)\n"
                                    "EOF\n"
                                    ')"'
                                )
                            },
                        }
                    ],
                },
            },
        },
    )

    card = snapshot_card([tool_call])
    line = card.splitlines()[0]

    assert line == "🤖 Bash · git add app/models/org.py"
    for token in ("msg_011", "toolu_", "claude-opus-5", "EOF", "<<"):
        assert token not in card
    for word in ("tool_use", "assistant", "message"):
        assert word not in card


def test_the_card_escapes_transcript_html() -> None:
    reply = TranscriptMessage(
        id="m-1",
        session_id="session-1",
        type="agent",
        content={
            "type": "agent",
            "rawPayload": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "<b>not bold</b>"}]},
            },
        },
    )

    assert "🤖 &lt;b&gt;not bold&lt;/b&gt;" in snapshot_card([reply])


def test_the_ack_is_two_words_and_a_name() -> None:
    opened = AdoptResult("w", "s", 5, "checkout/main")

    assert ack_line(opened) == "→ <b>checkout/main</b>"
    assert ack_line(AdoptResult("w", "s", 5, "checkout/main", already=True)) == (
        "→ <b>checkout/main</b> · already open"
    )


# ── resuming ─────────────────────────────────────────────────────────────────


async def test_a_prompt_in_the_adopted_topic_reaches_the_adopted_session(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """Adoption is only worth anything if the next line typed is a prompt."""
    session = _seeded(fake)
    bot = _Bot()
    await _adopt(bot, db, client, session)

    chat = await chats_repo.get(db, CHAT_ID, FIRST_TOPIC)
    row = await sessions_repo.get(db, session.session_id)
    assert chat is not None and row is not None
    message = SimpleNamespace(
        text="now do the same for the cart test",
        bot=bot,
        chat=SimpleNamespace(id=CHAT_ID, type="supergroup"),
        message_thread_id=FIRST_TOPIC,
        message_id=51,
        from_user=SimpleNamespace(id=1001),
    )

    await prompt_handlers.plain_text(
        message,  # type: ignore[arg-type]
        Route(
            chat_id=CHAT_ID,
            thread_id=FIRST_TOPIC,
            kind="topic",
            chat=chat,
            session=row,
        ),
        fake_tenant(client),
        NonceStore(),
        _seat(db),
        db=db,
    )

    posted = session.posted_ids
    assert len(posted) == 1
    body = session._posted[posted[0]]
    assert body.startswith("now do the same for the cart test")
    # The adopted path is the normal path, so it is phone-shaped too.
    assert body.endswith(MOBILE_REPLY_INSTRUCTION)


# ── the surfaces ─────────────────────────────────────────────────────────────


async def test_attach_command_lists_only_matching_unattached_workspaces(
    db: Database, client: ConductorClient, fake: FakeConductor, monkeypatch: Any
) -> None:
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(core_handlers, "tell", fake_tell)
    _seeded(fake, name="checkout")
    _seeded(fake, name="billing")
    message = SimpleNamespace(
        text="/attach billing",
        chat=SimpleNamespace(id=CHAT_ID, type="supergroup"),
        message_thread_id=None,
        message_id=3,
        from_user=SimpleNamespace(id=1001),
    )

    await core_handlers.attach_workspace(
        message,  # type: ignore[arg-type]
        fake_tenant(client),
        _NullState(),  # type: ignore[arg-type]
        NonceStore(),
        db=db,
    )

    text, markup = sent[0]
    assert text == "<b>Open laptop workspace</b> · continues from now"
    assert markup is not None
    assert [row[0].text for row in markup.inline_keyboard] == ["+ Open billing"]


async def test_general_switch_offers_adoption_and_says_what_it_capped(
    db: Database, client: ConductorClient, fake: FakeConductor, monkeypatch: Any
) -> None:
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(power_handlers, "tell", fake_tell)
    for index in range(15):
        _seeded(fake, name=f"checkout-{index:02d}")
    message = SimpleNamespace(
        text="/s checkout",
        chat=SimpleNamespace(id=CHAT_ID, type="supergroup"),
        message_thread_id=None,
        message_id=3,
        from_user=SimpleNamespace(id=1001),
    )

    await power_handlers.switch_session(
        message,  # type: ignore[arg-type]
        Route(chat_id=CHAT_ID, kind="general"),
        fake_tenant(client),
        _NullState(),  # type: ignore[arg-type]
        NonceStore(),
        db=db,
    )

    text, markup = sent[0]
    assert text == (
        "<b>Open workspace</b> · + opens a laptop one here\n<i>+3 more · /s name</i>"
    )
    assert markup is not None
    labels = [row[0].text for row in markup.inline_keyboard]
    assert len(labels) == power_handlers.GENERAL_VISIBLE
    assert labels[0] == "+ Open checkout-00"


async def test_general_switch_filters_adoptable_workspaces_by_name(
    db: Database, client: ConductorClient, fake: FakeConductor, monkeypatch: Any
) -> None:
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(power_handlers, "tell", fake_tell)
    _seeded(fake, name="checkout")
    _seeded(fake, name="billing")
    message = SimpleNamespace(
        text="/s billing",
        chat=SimpleNamespace(id=CHAT_ID, type="supergroup"),
        message_thread_id=None,
        message_id=3,
        from_user=SimpleNamespace(id=1001),
    )

    await power_handlers.switch_session(
        message,  # type: ignore[arg-type]
        Route(chat_id=CHAT_ID, kind="general"),
        fake_tenant(client),
        _NullState(),  # type: ignore[arg-type]
        NonceStore(),
        db=db,
    )

    _, markup = sent[0]
    assert markup is not None
    assert [row[0].text for row in markup.inline_keyboard] == ["+ Open billing"]


async def test_general_switch_finds_an_attached_topic_by_workspace_name(
    db: Database, client: ConductorClient, monkeypatch: Any
) -> None:
    """The session title often differs from the workspace name typed on phone."""
    sent: list[tuple[str, Any]] = []

    async def fake_tell(_message: Any, text: str, **kwargs: Any) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(power_handlers, "tell", fake_tell)
    await workspaces_repo.upsert(
        db,
        "workspace-checkout",
        name="checkout",
        chat_id=CHAT_ID,
        topic_id=44,
        topic_name="checkout/main",
    )
    await sessions_repo.upsert(
        db,
        "session-unrelated",
        workspace_id="workspace-checkout",
        title="investigate flaky fixture",
        is_bound=True,
    )
    message = SimpleNamespace(
        text="/s checkout",
        chat=SimpleNamespace(id=CHAT_ID, type="supergroup"),
        message_thread_id=None,
        message_id=3,
        from_user=SimpleNamespace(id=1001),
    )

    await power_handlers.switch_session(
        message,  # type: ignore[arg-type]
        Route(chat_id=CHAT_ID, kind="general"),
        fake_tenant(client),
        _NullState(),  # type: ignore[arg-type]
        NonceStore(),
        db=db,
    )

    _, markup = sent[0]
    assert markup is not None
    assert [row[0].text for row in markup.inline_keyboard] == ["✅ checkout/main"]


async def test_the_view_being_down_leaves_general_switch_working(
    db: Database, monkeypatch: Any
) -> None:
    """``/s`` degrades to the topics it knows; it never fails on the view."""

    async def boom(*_: Any, **__: Any) -> list[dict[str, object]]:
        raise RuntimeError("sql is down")

    monkeypatch.setattr(core_handlers, "board_rows", boom)

    assert await core_handlers.adoptable_rows(db, cast(ConductorClient, None)) == []


async def test_the_callback_opens_the_topic_and_answers_with_a_jump(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    session = _seeded(fake)
    bot = _Bot()
    store = NonceStore()
    ticket = store.issue(
        "adopt",
        f"{session.workspace_id}\n{session.session_id}",
        user_id=1001,
        chat_id=CHAT_ID,
        thread_id=0,
    )
    query = _Query(bot, ticket.callback_data)

    await adopt_handlers.adopt_callback(
        query,  # type: ignore[arg-type]
        Route(chat_id=CHAT_ID, kind="supergroup"),
        store,
        fake_tenant(client),
        db=db,
    )

    assert query.answers == ["Opening…"]
    assert bot.topics == [FIRST_TOPIC]
    ack = [item for item in bot.sent if not item.get("message_thread_id")][0]
    assert ack["text"] == "→ <b>checkout/main</b>"
    assert ack["reply_markup"].inline_keyboard[0][0].text == "Open topic"


async def test_a_refused_adoption_answers_in_the_chat_not_only_the_toast(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    empty = fake.add_workspace("empty")
    bot = _Bot()
    store = NonceStore()
    ticket = store.issue(
        "adopt", f"{empty.id}\n", user_id=1001, chat_id=CHAT_ID, thread_id=0
    )
    query = _Query(bot, ticket.callback_data)

    await adopt_handlers.adopt_callback(
        query,  # type: ignore[arg-type]
        Route(chat_id=CHAT_ID, kind="supergroup"),
        store,
        fake_tenant(client),
        db=db,
    )

    assert bot.topics == []
    assert bot.sent[0]["text"] == "Open failed · No sessions in this workspace yet."
    assert bot.sent[0]["disable_notification"] is None


class _NullState:
    """``abandon_wizard`` only asks whether a wizard is open."""

    async def get_state(self) -> str | None:
        return None

    async def clear(self) -> None:
        return None


def _refused(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(
        method=SendMessage(chat_id=CHAT_ID, text="x"), message=message
    )


async def test_a_dm_that_cannot_open_a_topic_says_so_and_still_binds(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """The likeliest real DM path, and it was on no test at all.

    `dm_topic_support` says go ahead — `getMe` reports threaded mode on — and
    `createForumTopic` then refuses, which is the Bot API 10.0 regression the
    whole degradation story is written for. Adoption must not fail, and the one
    line explaining why this chat suddenly holds one workspace must be said.
    """
    common._LINEAR_TOLD.discard(CHAT_ID)
    session = _seeded(fake)
    bot = _Bot()
    bot.create_error = _refused("Bad Request: not enough rights to create a topic")

    outcome = await _adopt(bot, db, client, session, chat_type="private")

    assert outcome.thread_id == 0, "the linear seat, not a failed command"
    chat = await chats_repo.get(db, CHAT_ID, 0)
    assert chat is not None and chat.session_id == session.session_id
    assert LINEAR_DM_NOTICE in [str(item["text"]) for item in bot.sent]


async def test_a_thread_deleted_before_the_tap_gets_a_real_topic_instead(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """A claimed thread is only a promise until an API call uses it.

    `/attach` posts a list and waits; the room Telegram opened for the command
    can be deleted from the phone in that window. Binding a workspace to it
    would leave the transcript addressed to nothing.
    """
    session = _seeded(fake)
    bot = _Bot()
    bot.dead.add(555)

    outcome = await _adopt(
        bot, db, client, session, chat_type="private", claim_thread=555
    )

    assert outcome.thread_id == FIRST_TOPIC
    assert bot.topics == [FIRST_TOPIC], "a room that exists, not the one that did not"


async def test_a_claimed_thread_that_refuses_its_new_name_is_still_used(
    db: Database, client: ConductorClient, fake: FakeConductor
) -> None:
    """Whether a bot may rename a thread a *user* opened in a DM is unverified.

    Being unable to retitle a room is not being unable to use it — so the
    workspace moves in, and the marker is deliberately left unrecorded so the
    next state transition retries the rename rather than believing a title
    Telegram never showed.
    """
    session = _seeded(fake)
    bot = _Bot()
    bot.edit_error = _refused("Bad Request: not enough rights to manage this topic")

    outcome = await _adopt(
        bot, db, client, session, chat_type="private", claim_thread=555
    )

    assert outcome.thread_id == 555
    workspace = await workspaces_repo.get(db, session.workspace_id)
    assert workspace is not None
    assert workspace.topic_id == 555
    assert workspace.topic_marker is None, "nothing to skip on the next transition"


def test_an_already_open_workspace_names_its_room_when_it_cannot_link_to_one() -> None:
    """A DM has no thread-link syntax, so "already open" must say *where*."""
    result = AdoptResult(
        workspace_id="w", session_id="s", thread_id=42, name="api/main", already=True
    )

    assert "thread list" in ack_line(result, linkable=False)
    assert "thread list" not in ack_line(result, linkable=True)
