"""Scripted reliability checks for the per-session poller."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from ctb.conductor.client import ConductorClient
from ctb.conductor.errors import CircuitOpen
from ctb.conductor.models import (
    CancelResult,
    MessagesPage,
    PostMessageResult,
    PostState,
    SessionStatus,
    SessionStatusValue,
    TranscriptMessage,
    WorkspaceStatus,
    WorkspaceStatusValue,
)
from ctb.db.connection import Database
from ctb.db.repo import chats, prompts, sessions, workspaces
from ctb.turn.session_poller import SessionPoller
from ctb.turn.state import (
    E404,
    Action,
    Cancel,
    CardKind,
    Notify,
    PostStatusCard,
    TurnState,
    UnbindTopic,
)

SESSION = "session-poller"
WORKSPACE = "workspace-poller"
CHAT = -100_222
THREAD = 17


class ScriptClient:
    def __init__(
        self,
        messages: Sequence[TranscriptMessage] = (),
        *,
        statuses: Sequence[SessionStatus | BaseException] = (),
        workspace_status: WorkspaceStatus | None = None,
    ) -> None:
        self.messages = list(messages)
        self.statuses = list(statuses)
        self.workspace_status = workspace_status or WorkspaceStatus(
            status=WorkspaceStatusValue.READY
        )
        self.message_calls = 0
        self.status_calls = 0
        self.workspace_status_calls = 0
        self.posted_ids: list[str] = []
        self.cancel_calls = 0

    async def get_messages(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        after: str | None = None,
    ) -> MessagesPage:
        assert session_id == SESSION
        self.message_calls += 1
        start = 0 if offset is None else offset
        if after is not None:
            positions = [
                i for i, message in enumerate(self.messages) if message.id == after
            ]
            start = len(self.messages) if not positions else positions[0] + 1
        size = 50 if limit is None else limit
        data = self.messages[start : start + size]
        return MessagesPage(
            data=data,
            offset=start,
            has_more=start + len(data) < len(self.messages),
        )

    async def get_session_status(self, session_id: str) -> SessionStatus:
        assert session_id == SESSION
        self.status_calls += 1
        item = (
            self.statuses.pop(0)
            if self.statuses
            else SessionStatus(status=SessionStatusValue.IDLE)
        )
        if isinstance(item, BaseException):
            raise item
        return item

    async def get_workspace_status(self, workspace_id: str) -> WorkspaceStatus:
        assert workspace_id == WORKSPACE
        self.workspace_status_calls += 1
        return self.workspace_status

    async def cancel_session(self, session_id: str) -> CancelResult:
        assert session_id == SESSION
        self.cancel_calls += 1
        return CancelResult(status="cancelling", canceled_queued_messages=0)

    async def post_message(
        self, session_id: str, text: str, message_id: str
    ) -> PostMessageResult:
        assert session_id == SESSION
        assert text
        self.posted_ids.append(message_id)
        return PostMessageResult(message_id=message_id, state=PostState.SENT)


class RecordingSink:
    def __init__(self) -> None:
        self.batches: list[tuple[Action, ...]] = []

    async def handle(
        self,
        actions: Sequence[Action],
        *,
        session_id: str,
        chat_id: int,
        thread_id: int = 0,
        deep_link: str | None = None,
    ) -> None:
        assert (session_id, chat_id, thread_id) == (SESSION, CHAT, THREAD)
        self.batches.append(tuple(actions))

    @property
    def actions(self) -> tuple[Action, ...]:
        return tuple(action for batch in self.batches for action in batch)


async def seed(
    db: Database,
    *,
    state: TurnState = TurnState.IDLE,
    seeded: bool = True,
) -> None:
    await workspaces.upsert(
        db,
        WORKSPACE,
        name="workspace",
        deep_link="https://example.test/workspace",
        status=WorkspaceStatusValue.READY.value,
    )
    await sessions.upsert(
        db,
        SESSION,
        workspace_id=WORKSPACE,
        chat_id=CHAT,
        thread_id=THREAD,
        is_bound=True,
    )
    if seeded:
        await sessions.seek_to_end(db, SESSION, message_id=None, session_index=-1)
    await sessions.update(
        db,
        SESSION,
        turn_state=state.value,
        entered_state_at=1_000_000,
        poll_interval_ms=3_000 if state is TurnState.QUEUED else 20_000,
    )


async def test_boot_forces_delivery_status_and_workspace_before_error_card(
    db: Database,
    message_factory: Any,
) -> None:
    await seed(db, state=TurnState.WORKING)
    answer = message_factory(
        4, session_id=SESSION, turn_id="turn-error", text="partial answer"
    )
    client = ScriptClient(
        [answer],
        statuses=[
            SessionStatus(
                status=SessionStatusValue.ERROR,
                error_message="Codex auth missing",
            )
        ],
    )
    sink = RecordingSink()
    poller = SessionPoller(cast(ConductorClient, client), db, SESSION, action_sink=sink)

    report = await poller.tick()

    assert report.state is TurnState.ERROR
    assert report.messages == 1
    assert report.deliveries_created == 1
    assert report.status_requested
    assert report.workspace_status_requested
    assert client.message_calls >= 2  # boot drain + error's forced drain
    assert (
        await db.fetch_val(
            "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
            (SESSION,),
        )
        == 1
    )
    assert any(
        isinstance(action, PostStatusCard) and action.kind is CardKind.ERROR
        for action in sink.actions
    )


async def test_status_failures_enter_cursor_only_but_messages_run_every_tick(
    db: Database,
) -> None:
    await seed(db, state=TurnState.QUEUED)
    failures = [
        CircuitOpen(retry_after=1.0),
        CircuitOpen(retry_after=1.0),
        CircuitOpen(retry_after=1.0),
    ]
    client = ScriptClient(statuses=failures)
    sink = RecordingSink()
    poller = SessionPoller(cast(ConductorClient, client), db, SESSION, action_sink=sink)

    reports = [await poller.tick(), await poller.tick(), await poller.tick()]

    assert client.message_calls >= 3
    assert client.status_calls == 3
    assert reports[-1].cursor_only
    assert poller.context is not None
    assert poller.context.cadence_ms == 8_000
    notices = [action for action in sink.actions if isinstance(action, Notify)]
    entries = [notice for notice in notices if notice.once_key == "cursor-only"]
    assert len(entries) == 1
    assert entries[0].text == "Status API down · replies still arrive."


async def test_boot_reposts_saved_prompt_with_identical_message_id(
    db: Database,
) -> None:
    await seed(db, state=TurnState.SUBMIT_PENDING)
    prompt = await prompts.create(
        db,
        session_id=SESSION,
        body="resume this",
        chat_id=CHAT,
        thread_id=THREAD,
        message_id="stable-idempotency-key",
    )
    client = ScriptClient()
    poller = SessionPoller(cast(ConductorClient, client), db, SESSION)

    report = await poller.tick()

    assert client.posted_ids == [prompt.message_id]
    stored = await prompts.get(db, prompt.message_id)
    assert stored is not None
    assert stored.state == "posted"
    assert report.state is TurnState.QUEUED
    assert poller.context is not None
    assert [item.message_id for item in poller.context.pending_prompts] == [
        prompt.message_id
    ]


async def test_boot_recovers_pending_prompt_from_queued_state(db: Database) -> None:
    await seed(db, state=TurnState.QUEUED)
    prompt = await prompts.create(
        db,
        session_id=SESSION,
        body="survive handler crash",
        chat_id=CHAT,
        thread_id=THREAD,
        message_id="queued-but-unposted",
    )
    client = ScriptClient()
    poller = SessionPoller(cast(ConductorClient, client), db, SESSION)

    report = await poller.tick()

    assert client.posted_ids == [prompt.message_id]
    stored = await prompts.get(db, prompt.message_id)
    assert stored is not None and stored.state == "posted"
    assert report.state is TurnState.QUEUED


async def test_boot_recovers_pending_prompt_from_idle_state(db: Database) -> None:
    await seed(db, state=TurnState.IDLE)
    prompt = await prompts.create(
        db,
        session_id=SESSION,
        body="survive before state write",
        chat_id=CHAT,
        thread_id=THREAD,
        message_id="idle-but-unposted",
    )
    client = ScriptClient()
    poller = SessionPoller(cast(ConductorClient, client), db, SESSION)

    report = await poller.tick()

    assert client.posted_ids == [prompt.message_id]
    stored = await prompts.get(db, prompt.message_id)
    assert stored is not None and stored.state == "posted"
    assert report.state is TurnState.QUEUED


async def test_waking_workspace_holds_prompt_until_ready(db: Database) -> None:
    await seed(db, state=TurnState.WAKING)
    await workspaces.update_status(
        db,
        WORKSPACE,
        status=WorkspaceStatusValue.INITIALIZING,
        lifecycle_step="cloning",
    )
    prompt = await prompts.create(
        db,
        session_id=SESSION,
        body="wait for ready",
        chat_id=CHAT,
        thread_id=THREAD,
        message_id="held-until-ready",
    )
    client = ScriptClient(
        workspace_status=WorkspaceStatus(
            status=WorkspaceStatusValue.INITIALIZING,
            lifecycle_step="cloning",
        )
    )
    poller = SessionPoller(cast(ConductorClient, client), db, SESSION)

    first = await poller.tick()

    assert first.state is TurnState.WAKING
    assert client.posted_ids == []

    client.workspace_status = WorkspaceStatus(status=WorkspaceStatusValue.READY)
    second = await poller.tick()

    assert second.state is TurnState.QUEUED
    assert client.posted_ids == [prompt.message_id]


async def test_first_bind_seeks_to_end_and_only_previews_history(
    db: Database,
    message_factory: Any,
) -> None:
    await seed(db, seeded=False)
    history = [
        message_factory(
            index,
            session_id=SESSION,
            turn_id="old-turn",
            text=f"old answer {index}",
        )
        for index in range(3)
    ]
    client = ScriptClient(history)
    sink = RecordingSink()
    poller = SessionPoller(cast(ConductorClient, client), db, SESSION, action_sink=sink)

    await poller.tick()

    row = await sessions.get(db, SESSION)
    assert row is not None
    assert row.seeded
    assert row.cursor_session_index == 2
    assert (
        await db.fetch_val(
            "SELECT COUNT(*) FROM transcript_messages WHERE session_id = ?",
            (SESSION,),
        )
        == 0
    )
    previews = [
        action.text
        for action in sink.actions
        if isinstance(action, Notify) and action.once_key == "first-bind-preview"
    ]
    assert previews == ["Now mirroring: old answer 2"]


async def test_dispatch_serializes_cancel_and_executes_cancel_action(
    db: Database,
) -> None:
    await seed(db, state=TurnState.WORKING)
    client = ScriptClient()
    sink = RecordingSink()
    poller = SessionPoller(cast(ConductorClient, client), db, SESSION, action_sink=sink)

    state = await poller.dispatch(Cancel(requested_by=123))

    assert state is TurnState.CANCELLING
    assert client.cancel_calls == 1
    row = await sessions.get(db, SESSION)
    assert row is not None
    assert row.state is TurnState.CANCELLING


async def test_dead_sink_runs_before_db_routing_is_cleared(db: Database) -> None:
    await seed(db, state=TurnState.WORKING)
    await workspaces.bind_topic(
        db,
        WORKSPACE,
        chat_id=CHAT,
        topic_id=THREAD,
        topic_name="workspace",
    )
    observations: list[tuple[bool, int | None]] = []

    class OrderingSink(RecordingSink):
        async def handle(
            self,
            actions: Sequence[Action],
            *,
            session_id: str,
            chat_id: int,
            thread_id: int = 0,
            deep_link: str | None = None,
        ) -> None:
            await super().handle(
                actions,
                session_id=session_id,
                chat_id=chat_id,
                thread_id=thread_id,
                deep_link=deep_link,
            )
            if any(isinstance(action, UnbindTopic) for action in actions):
                session = await sessions.get(db, SESSION)
                assert session is not None
                observations.append((session.is_bound, session.thread_id))

    sink = OrderingSink()
    poller = SessionPoller(
        cast(ConductorClient, ScriptClient()), db, SESSION, action_sink=sink
    )

    await poller.dispatch(E404("session"))

    assert observations == [(True, THREAD)]
    session = await sessions.get(db, SESSION)
    assert session is not None
    assert not session.is_bound
    # The room is the session's now, and the `chats` row goes with it: leaving
    # it behind left a zombie room routing prompts to a session that is dead.
    assert not session.has_room
    route = await chats.get(db, CHAT, THREAD)
    assert route is None or route.session_id is None
