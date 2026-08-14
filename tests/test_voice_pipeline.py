"""Durability and dispatch tests for Telegram voice notes."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetFile

from ctb.bot.handlers.common import MOBILE_REPLY_INSTRUCTION
from ctb.bot.handlers.core import DM_COCKPIT_HINT
from ctb.bot.keyboards import NonceStore
from ctb.bot.middleware.routing import Route
from ctb.bot.wizards import new_workspace
from ctb.conductor.models import (
    PostMessageResult,
    PostState,
    Project,
    SqlResult,
)
from ctb.db.connection import Database, now_ms
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import prompts as prompts_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import tenancy
from ctb.db.repo import voice_inputs as voice_repo
from ctb.db.repo import wizard as wizard_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.settings import Settings
from ctb.voice import service as voice_service_module
from ctb.voice.provider import Transcription, TranscriptionError
from ctb.voice.service import VoiceEnqueueStatus, VoiceService
from tests.pg import BOOTSTRAP_TENANT_ID


class FakeBot:
    def __init__(self) -> None:
        self.downloads = 0
        self.messages: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []

    async def get_file(self, _file_id: str) -> object:
        return SimpleNamespace(file_path="voice/file.oga", file_size=9)

    async def download_file(self, _path: str, *, destination: io.BytesIO) -> io.BytesIO:
        self.downloads += 1
        destination.write(b"OggS-test")
        destination.seek(0)
        return destination

    async def send_message(self, **kwargs: Any) -> object:
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.messages))

    async def edit_message_text(self, **kwargs: Any) -> object:
        self.edits.append(kwargs)
        return SimpleNamespace(message_id=kwargs["message_id"])


class FakeProvider:
    def __init__(self, text: str = "Fix the flaky test") -> None:
        self.text = text
        self.calls = 0

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        mime_type: str,
        keyterms: list[str],
    ) -> Transcription:
        del audio, filename, mime_type, keyterms
        self.calls += 1
        return Transcription(self.text, language="en")

    async def aclose(self) -> None:
        return None


class HangingProvider(FakeProvider):
    """A provider that never returns — the shape of the live "Transcribing…"."""

    def __init__(self, *, hang_first_only: bool = False) -> None:
        super().__init__()
        self._hang_first_only = hang_first_only

    async def transcribe(self, *args: Any, **kwargs: Any) -> Transcription:
        self.calls += 1
        if not self._hang_first_only or self.calls == 1:
            await asyncio.sleep(3600)
        return Transcription(self.text, language="en")


class FakeClient:
    def __init__(self, *, projects: tuple[str, ...] = ("api",)) -> None:
        self.posts: list[tuple[str, str, str]] = []
        self.queries: list[str] = []
        self._projects = projects

    async def list_projects(self, **_: Any) -> Any:
        return SimpleNamespace(
            data=[Project(id=f"project-{name}", name=name) for name in self._projects],
            has_more=False,
        )

    async def post_message(
        self, session_id: str, text: str, message_id: str
    ) -> PostMessageResult:
        self.posts.append((session_id, text, message_id))
        return PostMessageResult(message_id=message_id, state=PostState.QUEUED)

    async def sql(self, query: str) -> SqlResult:
        self.queries.append(query)
        return SqlResult(rows=[], row_count=0)


class FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def dispatch(self, session_id: str, evidence: object) -> object:
        self.calls.append((session_id, evidence))
        return object()


class FakeProviderPool:
    """A :class:`ProviderPool` stand-in: every tenant gets the same fake.

    In production each tenant's provider is built from *their* sealed speech
    key, and a tenant without one simply has none.
    """

    def __init__(self, provider: FakeProvider | None) -> None:
        self._provider = provider

    async def get(self, _tenant: object) -> FakeProvider | None:
        return self._provider

    async def aclose(self) -> None:
        return None


class FakeClientPool:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def get(self, _tenant: object) -> FakeClient:
        return self._client

    def peek(self, _tenant_id: object) -> FakeClient:
        return self._client


async def set_voice_mode(system_db: Database, mode: str) -> None:
    """Voice mode is a tenant setting, not an environment variable."""
    await tenancy.update_defaults(system_db, BOOTSTRAP_TENANT_ID, voice_mode=mode)


def make_service(
    settings: Settings,
    db: Database,
    system_db: Database,
    *,
    bot: FakeBot | None = None,
    provider: FakeProvider | None = None,
    client: FakeClient | None = None,
    supervisor: FakeSupervisor | None = None,
) -> VoiceService:
    return VoiceService(
        settings,
        db,
        system_db,
        bot or FakeBot(),  # type: ignore[arg-type]
        cast(Any, FakeClientPool(client or FakeClient())),
        supervisor or FakeSupervisor(),  # type: ignore[arg-type]
        NonceStore(),
        cast(Any, FakeProviderPool(provider or FakeProvider())),
    )


def voice_message(*, size: int = 100, duration: int = 12, thread_id: int = 7) -> Any:
    return SimpleNamespace(
        voice=SimpleNamespace(
            file_id="file-1",
            file_unique_id="unique-1",
            duration=duration,
            file_size=size,
        ),
        chat=SimpleNamespace(id=-1001, type="supergroup"),
        message_id=55,
        message_thread_id=thread_id,
        from_user=SimpleNamespace(id=1001),
    )


async def test_replayed_update_keeps_one_job_and_operation_id(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    settings = settings_factory(voice_enabled=True)
    service = make_service(settings, db, system_db)
    message = voice_message()
    route = Route(
        tenant_voice_enabled=True,
        chat_id=-1001,
        thread_id=7,
        kind="topic",
        session=None,
    )

    first = await service.enqueue(message, route)  # type: ignore[arg-type]
    row1 = await voice_repo.get(db, -1001, 55)
    second = await service.enqueue(message, route)  # type: ignore[arg-type]
    row2 = await voice_repo.get(db, -1001, 55)

    assert first is VoiceEnqueueStatus.ACCEPTED
    assert second is VoiceEnqueueStatus.DUPLICATE
    assert row1 is not None and row2 is not None
    assert row1.action_id == row2.action_id
    assert await db.fetch_val("SELECT COUNT(*) FROM voice_inputs") == 1


async def test_uploaded_audio_preserves_filename_and_mime(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    settings = settings_factory(voice_enabled=True)
    service = make_service(settings, db, system_db)
    message = voice_message()
    message.voice = None
    message.audio = SimpleNamespace(
        file_id="audio-1",
        file_unique_id="audio-unique",
        file_name="standup.m4a",
        mime_type="audio/mp4",
        duration=20,
        file_size=1_000,
    )

    status = await service.enqueue(
        message,  # type: ignore[arg-type]
        Route(chat_id=-1001, thread_id=7, kind="topic", tenant_voice_enabled=True),
    )

    row = await voice_repo.get(db, -1001, 55)
    assert status is VoiceEnqueueStatus.ACCEPTED
    assert row is not None
    assert row.file_name == "standup.m4a"
    assert row.mime_type == "audio/mp4"


async def test_size_and_duration_reject_before_download(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    settings = settings_factory(
        voice_enabled=True,
        voice_max_duration_seconds=30,
        voice_max_file_bytes=1_000,
    )
    bot = FakeBot()
    service = make_service(settings, db, system_db, bot=bot)
    route = Route(chat_id=-1001, thread_id=7, kind="topic", tenant_voice_enabled=True)

    large = await service.enqueue(
        voice_message(size=1_001),
        route,  # type: ignore[arg-type]
    )
    long = await service.enqueue(
        voice_message(size=100, duration=31),
        route,  # type: ignore[arg-type]
    )

    assert large is VoiceEnqueueStatus.TOO_LARGE
    assert long is VoiceEnqueueStatus.TOO_LONG
    assert bot.downloads == 0
    assert await db.fetch_val("SELECT COUNT(*) FROM voice_inputs") == 0


async def test_voice_prompt_uses_snapshot_id_once_and_mobile_instruction(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    settings = settings_factory(voice_enabled=True)
    await sessions_repo.upsert(
        db,
        "session-voice",
        chat_id=-1001,
        thread_id=7,
        is_bound=True,
        title="voice target",
    )
    bot = FakeBot()
    provider = FakeProvider("Fix the flaky test")
    client = FakeClient()
    service = make_service(
        settings, db, system_db, bot=bot, provider=provider, client=client
    )
    row, _ = await voice_repo.create(
        db,
        chat_id=-1001,
        tg_message_id=55,
        thread_id=7,
        user_id=1001,
        file_id="file-1",
        file_unique_id="unique-1",
        file_name=None,
        mime_type="audio/ogg",
        duration_seconds=12,
        file_size=100,
        route_kind="topic",
        route_session_id="session-voice",
        route_workspace_id=None,
        provider="elevenlabs",
        model="scribe_v2",
        action_id="voice-operation-1",
    )
    claimed = await voice_repo.claim_next(db)
    assert claimed is not None
    await service.attach_ack(row.chat_id, row.tg_message_id, 9001)

    await service._process(claimed)

    completed = await voice_repo.get(db, row.chat_id, row.tg_message_id)
    prompt = await prompts_repo.get(db, "voice-operation-1")
    assert completed is not None and completed.state == "completed"
    assert provider.calls == 1
    assert bot.downloads == 1
    assert len(client.posts) == 1
    assert client.posts[0][2] == "voice-operation-1"
    assert MOBILE_REPLY_INSTRUCTION in client.posts[0][1]
    assert bot.messages == []
    assert bot.edits[-1]["message_id"] == 9001
    assert "Fix the flaky test" in bot.edits[-1]["text"]
    assert prompt is not None and prompt.message_id == completed.action_id
    assert await voice_repo.claim_next(db) is None


async def test_general_voice_searches_and_never_posts(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    settings = settings_factory(voice_enabled=True)
    bot = FakeBot()
    client = FakeClient()
    service = make_service(settings, db, system_db, bot=bot, client=client)
    row, _ = await voice_repo.create(
        db,
        chat_id=-1001,
        tg_message_id=56,
        thread_id=1,
        user_id=1001,
        file_id="file-1",
        file_unique_id="unique-1",
        file_name=None,
        mime_type="audio/ogg",
        duration_seconds=8,
        file_size=100,
        route_kind="general",
        route_session_id=None,
        route_workspace_id=None,
        provider="elevenlabs",
        model="scribe_v2",
        action_id="voice-operation-general",
    )
    claimed = await voice_repo.claim_next(db)
    assert claimed is not None

    await service._process(claimed)

    completed = await voice_repo.get(db, row.chat_id, row.tg_message_id)
    assert completed is not None and completed.state == "completed"
    assert len(client.queries) == 1
    assert client.posts == []
    assert "No matches" in str(bot.messages[-1]["text"])


async def test_a_dictated_task_in_the_dm_root_offers_the_topic_it_belongs_to(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    """The spoken half of the DM-root cockpit, and the reason it is shared code.

    Dictating into the root is how the dead end was reported: the session was
    one topic away and voice answered "No session here". It must now say what
    the typed path says, through the same `cockpit_markup` — the two surfaces
    disagreeing about an unaddressed line is what sent a prompt to `/find` once.
    """
    settings = settings_factory(voice_enabled=True)
    bot = FakeBot()
    client = FakeClient()
    service = make_service(settings, db, system_db, bot=bot, client=client)
    await workspaces_repo.upsert(db, "ws-voice", chat_id=1001, topic_id=99)
    await sessions_repo.upsert(
        db, "sess-voice", workspace_id="ws-voice", chat_id=1001, thread_id=99, title="A"
    )
    await chats_repo.bind(
        db, 1001, 99, workspace_id="ws-voice", session_id="sess-voice", kind="topic"
    )
    await chats_repo.touch_prompt(db, 1001, 99, focus_for_ms=1000)
    await voice_repo.create(
        db,
        chat_id=1001,
        tg_message_id=58,
        thread_id=0,
        user_id=1001,
        file_id="file-1",
        file_unique_id="unique-1",
        file_name=None,
        mime_type="audio/ogg",
        duration_seconds=8,
        file_size=100,
        route_kind="dm",
        route_session_id=None,
        route_workspace_id=None,
        provider="elevenlabs",
        model="scribe_v2",
        action_id="voice-operation-dm-root",
    )
    claimed = await voice_repo.claim_next(db)
    assert claimed is not None

    await service._process(claimed)

    assert client.posts == [], "the root never sends on its own"
    assert client.queries == [], "and it does not search either — General does"
    sent = bot.messages[-1]
    assert DM_COCKPIT_HINT in str(sent["text"])
    markup = sent.get("reply_markup")
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == "Send to A"


async def test_recovery_reuses_conductor_message_id(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    settings = settings_factory(voice_enabled=True)
    await sessions_repo.upsert(db, "session-voice")
    client = FakeClient()
    service = make_service(settings, db, system_db, client=client)
    row, _ = await voice_repo.create(
        db,
        chat_id=1001,
        tg_message_id=57,
        thread_id=0,
        user_id=1001,
        file_id="file-1",
        file_unique_id="unique-1",
        file_name=None,
        mime_type="audio/ogg",
        duration_seconds=8,
        file_size=100,
        route_kind="dm",
        route_session_id="session-voice",
        route_workspace_id=None,
        provider="elevenlabs",
        model="scribe_v2",
        action_id="stable-voice-operation",
    )
    claimed = await voice_repo.claim_next(db)
    assert claimed is not None
    await service._process(claimed)

    # Simulate a crash after the Conductor POST but before the voice job's
    # completion write. Recovery can re-POST, but only with the identical id.
    await db.execute(
        """
        UPDATE voice_inputs
           SET state = 'dispatching', completed_at = NULL, updated_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (now_ms() - voice_repo.ORPHAN_AFTER_MS - 1_000, row.chat_id, row.tg_message_id),
    )
    recovery = await voice_repo.recover_stale(db)
    assert recovery.requeued == 1 and recovery.abandoned == ()
    recovered = await voice_repo.claim_next(db)
    assert recovered is not None
    await service._process(recovered)

    assert [post[2] for post in client.posts] == [
        "stable-voice-operation",
        "stable-voice-operation",
    ]
    assert (
        await db.fetch_val(
            "SELECT COUNT(*) FROM outbound_prompts WHERE message_id = ?",
            ("stable-voice-operation",),
        )
        == 1
    )


async def test_exact_spoken_stop_uses_the_supervisor(
    db: Database, system_db: Database, settings_factory: Any
) -> None:
    settings = settings_factory(voice_enabled=True)
    await set_voice_mode(system_db, "commands")
    await sessions_repo.upsert(db, "session-stop")
    supervisor = FakeSupervisor()
    client = FakeClient()
    service = make_service(
        settings,
        db,
        system_db,
        provider=FakeProvider("command stop"),
        client=client,
        supervisor=supervisor,
    )
    row, _ = await voice_repo.create(
        db,
        chat_id=1001,
        tg_message_id=58,
        thread_id=0,
        user_id=1001,
        file_id="file-1",
        file_unique_id="unique-1",
        file_name=None,
        mime_type="audio/ogg",
        duration_seconds=5,
        file_size=100,
        route_kind="dm",
        route_session_id="session-stop",
        route_workspace_id=None,
        provider="elevenlabs",
        model="scribe_v2",
        action_id="voice-stop-operation",
    )
    claimed = await voice_repo.claim_next(db)
    assert claimed is not None

    await service._process(claimed)

    assert len(supervisor.calls) == 1
    assert supervisor.calls[0][0] == "session-stop"
    assert client.posts == []
    completed = await voice_repo.get(db, row.chat_id, row.tg_message_id)
    assert completed is not None and completed.state == "completed"


async def test_spoken_done_only_creates_named_confirmation(
    db: Database, system_db: Database, settings_factory: Any
) -> None:
    settings = settings_factory(voice_enabled=True)
    await set_voice_mode(system_db, "commands")
    await workspaces_repo.upsert(db, "workspace-done", name="api/fix")
    bot = FakeBot()
    service = make_service(
        settings,
        db,
        system_db,
        bot=bot,
        provider=FakeProvider("command done"),
    )
    row, _ = await voice_repo.create(
        db,
        chat_id=1001,
        tg_message_id=59,
        thread_id=0,
        user_id=1001,
        file_id="file-1",
        file_unique_id="unique-1",
        file_name=None,
        mime_type="audio/ogg",
        duration_seconds=5,
        file_size=100,
        route_kind="dm",
        route_session_id=None,
        route_workspace_id="workspace-done",
        provider="elevenlabs",
        model="scribe_v2",
        action_id="voice-done-operation",
    )
    claimed = await voice_repo.claim_next(db)
    assert claimed is not None

    await service._process(claimed)

    assert bot.messages[-1]["text"] == "Archive <b>api/fix</b>?"
    assert bot.messages[-1]["reply_markup"] is not None
    workspace = await workspaces_repo.get(db, "workspace-done")
    assert workspace is not None and workspace.status != "archived"
    completed = await voice_repo.get(db, row.chat_id, row.tg_message_id)
    assert completed is not None and completed.state == "completed"


# ── an optional feature must not be able to take the bot down ────────────────


async def seed(
    db: Database,
    *,
    tg_message_id: int,
    state: str = "received",
    attempts: int = 0,
    transcript: str | None = None,
    updated_at: int | None = None,
    completed_at: int | None = None,
) -> None:
    """Insert one voice job directly in the state a crash would have left.

    ``updated_at`` defaults to *older than the orphan window*, because that is
    what a job left behind by a dead process looks like. A job stamped `now` is
    one a live peer may still be holding, and `recover_stale` deliberately
    refuses to touch those — see
    ``test_a_fresh_claim_is_left_for_the_peer_that_holds_it``.
    """
    if updated_at is None:
        updated_at = now_ms() - voice_repo.ORPHAN_AFTER_MS - 1_000
    await voice_repo.create(
        db,
        chat_id=1001,
        tg_message_id=tg_message_id,
        thread_id=0,
        user_id=1001,
        file_id=f"file-{tg_message_id}",
        file_unique_id=f"unique-{tg_message_id}",
        file_name=None,
        mime_type="audio/ogg",
        duration_seconds=5,
        file_size=100,
        route_kind="dm",
        route_session_id=None,
        route_workspace_id=None,
        provider="elevenlabs",
        model="scribe_v2",
        action_id=f"operation-{tg_message_id}",
    )
    await db.execute(
        """
        UPDATE voice_inputs
           SET state = ?, attempts = ?, transcript = ?,
               updated_at = COALESCE(?, updated_at), completed_at = ?
         WHERE chat_id = ? AND tg_message_id = ?
        """,
        (state, attempts, transcript, updated_at, completed_at, 1001, tg_message_id),
    )


async def boom(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("database is locked")


@pytest.mark.parametrize(
    ("enabled", "configured"),
    ((False, True), (True, False), (False, False)),
)
async def test_unavailable_voice_starts_no_workers_and_polls_nothing(
    db: Database, settings_factory: Any, enabled: bool, configured: bool
) -> None:
    """Off or keyless, voice must cost zero tasks and zero SQLite traffic."""
    settings = settings_factory(voice_enabled=enabled)
    bot = FakeBot()
    service = VoiceService(
        settings,
        db,
        db,
        bot,  # type: ignore[arg-type]
        cast(Any, FakeClientPool(FakeClient())),
        FakeSupervisor(),  # type: ignore[arg-type]
        NonceStore(),
        cast(Any, FakeProviderPool(FakeProvider() if configured else None))
        if configured
        else None,
    )
    await seed(db, tg_message_id=70)
    before = len(asyncio.all_tasks())

    task = asyncio.create_task(service.run())
    for _ in range(10):
        await asyncio.sleep(0.001)

    assert not task.done()
    assert [t.get_name() for t in asyncio.all_tasks() if "voice" in t.get_name()] == []
    assert len(asyncio.all_tasks()) == before + 1  # only run() itself
    untouched = await voice_repo.get(db, 1001, 70)
    assert untouched is not None and untouched.state == "received"
    assert untouched.attempts == 0  # claim_next was never called
    assert bot.downloads == 0

    await service.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_a_claim_error_keeps_the_worker_loop_alive(
    db: Database,
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    system_db: Database,
) -> None:
    settings = settings_factory(voice_enabled=True, voice_max_concurrent=1)
    service = make_service(settings, db, system_db)
    calls = 0

    async def failing_claim(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await boom(*args, **kwargs)

    monkeypatch.setattr(voice_repo, "claim_next", failing_claim)
    monkeypatch.setattr(voice_service_module, "_ERROR_BACKOFF_SECONDS", 0.001)

    task = asyncio.create_task(service.run())
    for _ in range(50):
        await asyncio.sleep(0.001)
        if calls > 2:
            break

    assert calls > 2
    assert not task.done()

    await service.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_a_locked_database_during_fail_does_not_stop_the_service(
    db: Database,
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    system_db: Database,
) -> None:
    """The audit's headline: ``fail()`` ran inside the worker's own handler."""
    settings = settings_factory(voice_enabled=True, voice_max_concurrent=1)
    bot = FakeBot()

    async def no_file(_file_id: str) -> object:
        raise RuntimeError("Telegram is down")

    monkeypatch.setattr(bot, "get_file", no_file)
    monkeypatch.setattr(voice_repo, "fail", boom)
    monkeypatch.setattr(voice_service_module, "_ERROR_BACKOFF_SECONDS", 0.001)
    service = make_service(settings, db, system_db, bot=bot)
    await seed(db, tg_message_id=71)

    task = asyncio.create_task(service.run())
    for _ in range(50):
        await asyncio.sleep(0.001)
        if task.done():
            break

    assert not task.done()

    await service.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_a_maintenance_prune_error_does_not_stop_the_service(
    db: Database,
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    system_db: Database,
) -> None:
    settings = settings_factory(voice_enabled=True, voice_max_concurrent=1)
    service = make_service(settings, db, system_db)
    prunes = 0

    async def failing_prune(*args: Any, **kwargs: Any) -> Any:
        nonlocal prunes
        prunes += 1
        return await boom(*args, **kwargs)

    monkeypatch.setattr(voice_repo, "prune_terminal", failing_prune)
    monkeypatch.setattr(voice_service_module, "_MAINTENANCE_SECONDS", 0.001)

    task = asyncio.create_task(service.run())
    for _ in range(50):
        await asyncio.sleep(0.001)
        if prunes > 2:
            break

    assert prunes > 2  # boot prune plus repeated hourly prunes
    assert not task.done()

    await service.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_a_fresh_claim_is_left_for_the_peer_that_holds_it(
    db: Database,
) -> None:
    """A deploy overlaps two containers, and voice costs money per call.

    Without the window the new container requeues a note the old one is still
    transcribing: two speech-vendor bills, two prompts, one recording.
    """
    await seed(db, tg_message_id=80, state="transcribing", updated_at=now_ms())

    recovery = await voice_repo.recover_stale(db)

    assert recovery.requeued == 0 and recovery.abandoned == ()
    row = await voice_repo.get(db, 1001, 80)
    assert row is not None and row.state == "transcribing"


async def test_recover_stale_gives_up_after_three_attempts(db: Database) -> None:
    await seed(db, tg_message_id=72, state="transcribing", attempts=2)
    await seed(db, tg_message_id=73, state="transcribing", attempts=3)
    await seed(db, tg_message_id=74, state="dispatching", attempts=9, transcript="hi")

    recovery = await voice_repo.recover_stale(db)

    assert recovery.requeued == 1
    assert {row.tg_message_id for row in recovery.abandoned} == {73, 74}
    survivor = await voice_repo.get(db, 1001, 72)
    assert survivor is not None and survivor.state == "received"
    for tg_message_id in (73, 74):
        abandoned = await voice_repo.get(db, 1001, tg_message_id)
        assert abandoned is not None and abandoned.state == "failed"
        assert abandoned.last_error == "Gave up after 3 attempts."
    # A row that has been given up on is not re-claimed, so it cannot be billed.
    assert await voice_repo.claim_next(db) is not None
    assert await voice_repo.claim_next(db) is None


async def test_a_crash_looping_note_is_abandoned_and_reported(
    db: Database,
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    system_db: Database,
) -> None:
    """Boot → claim → crash, three times, then the loop stops costing money."""
    settings = settings_factory(voice_enabled=True)
    bot = FakeBot()
    provider = FakeProvider()

    async def oom(_file_id: str) -> object:
        raise MemoryError

    monkeypatch.setattr(bot, "get_file", oom)
    service = make_service(settings, db, system_db, bot=bot, provider=provider)
    await seed(db, tg_message_id=75)

    for _ in range(5):
        # Age the row: each pass models a *separate* boot after a crash, and
        # recovery deliberately ignores a claim young enough to still be live
        # on a peer.
        await db.execute(
            "UPDATE voice_inputs SET updated_at = ? WHERE tg_message_id = 75",
            (now_ms() - voice_repo.ORPHAN_AFTER_MS - 1_000,),
        )
        await service._recover()
        claimed = await voice_repo.claim_next(db)
        if claimed is None:
            break
        with pytest.raises(MemoryError):
            await service._process(claimed)  # the row stays 'transcribing'

    row = await voice_repo.get(db, 1001, 75)
    assert row is not None and row.state == "failed"
    assert row.attempts == 3
    assert provider.calls == 0
    assert "🎙 Failed" in str(bot.messages[-1]["text"])


async def test_a_network_blip_is_retried_instead_of_killing_the_note(
    db: Database,
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    system_db: Database,
) -> None:
    """The live failure: 28 KB, one 60s Telegram timeout, note dead on attempt 1.

    ``MAX_ATTEMPTS`` was enforced only for a job whose *process* died. A worker
    that caught its own exception went straight to ``fail``, so the transient
    infrastructure error and the "no clear speech" verdict were the same
    outcome — and the only way back was for the owner to spot the card and tap
    Retry.
    """
    settings = settings_factory(voice_enabled=True)
    bot = FakeBot()
    provider = FakeProvider()
    calls = 0

    async def flaky(_file_id: str) -> object:
        nonlocal calls
        calls += 1
        raise TelegramNetworkError(method=GetFile(file_id="f"), message="timeout")

    monkeypatch.setattr(bot, "get_file", flaky)
    service = make_service(settings, db, system_db, bot=bot, provider=provider)
    await seed(db, tg_message_id=90)

    await service._tick(0)
    row = await voice_repo.get(db, 1001, 90)
    assert row is not None
    assert row.state == "received"  # queued again, not buried
    assert row.attempts == 1
    assert bot.messages == []  # and nothing shouted about it

    # It keeps its budget, then reports honestly rather than looping on a bill.
    await service._tick(0)
    await service._tick(0)
    row = await voice_repo.get(db, 1001, 90)
    assert row is not None
    assert row.state == "failed"
    assert row.attempts == 3
    assert calls == 3
    assert provider.calls == 0
    assert "🎙 Failed" in str(bot.messages[-1]["text"])


async def test_a_verdict_about_the_note_is_not_retried(
    db: Database,
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    system_db: Database,
) -> None:
    """``TranscriptionError`` says the same thing on a second run, and was paid for.

    Retrying it would bill the customer again to reach the identical answer,
    which is why the split is on the exception class and not on a status code.
    """
    settings = settings_factory(voice_enabled=True)
    bot = FakeBot()
    provider = FakeProvider()

    async def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise TranscriptionError("No clear speech detected.")

    monkeypatch.setattr(provider, "transcribe", refused)
    service = make_service(settings, db, system_db, bot=bot, provider=provider)
    await seed(db, tg_message_id=91)

    await service._tick(0)
    row = await voice_repo.get(db, 1001, 91)
    assert row is not None
    assert row.state == "failed"
    assert row.attempts == 1
    assert "No clear speech detected." in str(bot.messages[-1]["text"])


async def test_a_retry_after_transcription_never_pays_the_vendor_twice(
    db: Database,
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    system_db: Database,
) -> None:
    """A dispatch that blips resumes at dispatch, and its budget still moves."""
    settings = settings_factory(voice_enabled=True)
    bot = FakeBot()
    provider = FakeProvider()
    service = make_service(settings, db, system_db, bot=bot, provider=provider)
    await seed(db, tg_message_id=92, state="transcribed", transcript="ship it")

    # Fails on the step immediately before dispatch, which is what a blip
    # anywhere past transcription looks like to this handler.
    monkeypatch.setattr(voice_repo, "mark_dispatching", boom)
    await service._tick(0)

    row = await voice_repo.get(db, 1001, 92)
    assert row is not None
    assert row.state == "transcribed"  # not 'received' — the audio is spent
    # `claim_next` does not count a 'transcribed' claim, so the requeue must,
    # or a failing dispatch retries forever on a counter that never moves.
    assert row.attempts == 1
    assert provider.calls == 0


async def test_retention_covers_failed_and_waiting_rows(db: Database) -> None:
    """Every terminal state still holds a transcript, so all of them expire."""
    old = now_ms() - 30 * 86_400_000
    await seed(db, tg_message_id=80, state="completed", completed_at=old)
    await seed(db, tg_message_id=81, state="failed", updated_at=old)
    await seed(db, tg_message_id=82, state="waiting_for_user", updated_at=old)
    await seed(db, tg_message_id=83, state="failed")  # fresh, still actionable
    await seed(db, tg_message_id=84, state="received", updated_at=old)

    pruned = await voice_repo.prune_terminal(db, before=now_ms() - 86_400_000)

    remaining = await db.fetch_all("SELECT tg_message_id FROM voice_inputs")
    assert pruned == 3
    assert sorted(int(row["tg_message_id"]) for row in remaining) == [83, 84]


async def test_a_job_that_hangs_becomes_a_message_not_a_stuck_card(
    db: Database, system_db: Database, settings_factory: Any, monkeypatch: Any
) -> None:
    """The live failure: a note parked on "Transcribing…" indefinitely.

    Every step inside ``_process`` is individually bounded, which is a claim
    about code that was read rather than code that ran. The deadline is the
    claim that still holds when one of those bounds is wrong.
    """
    settings = settings_factory(voice_enabled=True)
    service = make_service(settings, db, system_db, provider=HangingProvider())
    # Long enough that `_process` always reaches the provider — the deadline
    # starts before the download and its database round-trips, so a 10ms
    # budget fired before `transcribe` was ever called on a loaded machine
    # and the test failed roughly one full-suite run in four. Still four
    # orders of magnitude below the hang it is proving.
    monkeypatch.setattr(voice_service_module, "_JOB_DEADLINE_SECONDS", 0.25)
    await service.enqueue(voice_message(), _route())

    await service._tick(0)

    row = await voice_repo.get(db, -1001, 55)
    assert row is not None
    assert row.state == "failed", "the worker must not sit on it forever"
    assert "timed out" in (row.last_error or "").casefold()


async def test_the_worker_takes_the_next_note_after_one_hangs(
    db: Database, system_db: Database, settings_factory: Any, monkeypatch: Any
) -> None:
    """A hung job used to take the worker with it, not just its own note."""
    settings = settings_factory(voice_enabled=True)
    provider = HangingProvider(hang_first_only=True)
    service = make_service(settings, db, system_db, provider=provider)
    # Long enough that `_process` always reaches the provider — the deadline
    # starts before the download and its database round-trips, so a 10ms
    # budget fired before `transcribe` was ever called on a loaded machine
    # and the test failed roughly one full-suite run in four. Still four
    # orders of magnitude below the hang it is proving.
    monkeypatch.setattr(voice_service_module, "_JOB_DEADLINE_SECONDS", 0.25)

    await service.enqueue(voice_message(), _route())
    await service._tick(0)
    second = voice_message()
    second.message_id = 56
    await service.enqueue(second, _route())
    await service._tick(0)

    assert provider.calls == 2, "the second note is still picked up"


def _route(kind: str = "topic", thread_id: int = 7) -> Route:
    # `tenant_voice_enabled` is the per-workspace half of the switch; without
    # it `enqueue` refuses and nothing is stored.
    return Route(
        chat_id=-1001,
        thread_id=thread_id,
        kind=kind,
        tenant_voice_enabled=True,
    )


async def test_a_voice_note_answers_an_open_wizard_instead_of_searching(
    db: Database, system_db: Database, settings_factory: Any, monkeypatch: Any
) -> None:
    """The live failure: the wizard asked, voice answered, /find replied.

    Typed text reaches the wizard because aiogram routes on FSM state before
    any handler runs. Voice never passes through aiogram, so in General the
    transcript hit the search-only rule — the answer to the question the bot
    had *just asked* was run as a query, and reported "No matches."
    """
    settings = settings_factory(voice_enabled=True)
    client = FakeClient()
    service = make_service(settings, db, system_db, client=client)

    created: list[Any] = []

    async def fake_create(**kwargs: Any) -> Any:
        created.append(kwargs["request"])
        return SimpleNamespace(
            label="api/dev", thread_id=42, deep_link=None, workspace_id="w1"
        )

    monkeypatch.setattr(voice_service_module, "create_and_bind_input", fake_create)
    await wizard_repo.set_state(
        db,
        -1001,
        0,
        user_id=1001,
        state_key=new_workspace.PROMPT_STATE_KEY,
        data={
            "projects": {"p1": "api"},
            "project_urls": {"p1": "https://github.com/acme/api.git"},
            "project_id": "p1",
            "branch": "dev",
        },
    )

    message = voice_message(thread_id=0)
    await service.enqueue(message, _route(kind="general", thread_id=0))
    await service._tick(0)

    assert client.queries == [], "a wizard answer is never a search"
    assert len(created) == 1, "the wizard was completed instead"
    assert created[0].branch == "dev", "answers given before the prompt survive"
    assert created[0].prompt == "Fix the flaky test", "the transcript is the prompt"
    assert created[0].repository_url == "https://github.com/acme/api.git"
    # And the wizard is finished, so the next note routes normally.
    assert await wizard_repo.get(db, -1001, 0, user_id=1001) is None


async def test_a_task_dictated_into_an_empty_thread_offers_to_start_it(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    """Voice into "New Chat" is the whole reason this is on a phone.

    Telegram opens a thread for the note; the thread is empty, so the note is a
    task nobody has started. The typed path answers with a confirm card, so this
    one must answer with **the same** card — built by the same ``task_data`` and
    ``confirm_card``, and left in the same ``wizard_state`` row, so the tap that
    follows is served by the ordinary typed handler.
    """
    settings = settings_factory(voice_enabled=True)
    bot = FakeBot()
    client = FakeClient()
    service = make_service(settings, db, system_db, bot=bot, client=client)
    await voice_repo.create(
        db,
        chat_id=1001,
        tg_message_id=71,
        thread_id=4242,
        user_id=1001,
        file_id="file-1",
        file_unique_id="unique-1",
        file_name=None,
        mime_type="audio/ogg",
        duration_seconds=8,
        file_size=100,
        route_kind="dm",
        route_session_id=None,
        route_workspace_id=None,
        provider="elevenlabs",
        model="scribe_v2",
        action_id="voice-operation-empty-thread",
    )
    claimed = await voice_repo.claim_next(db)
    assert claimed is not None

    await service._process(claimed)

    assert client.posts == [], "nothing is prompted — there is no session yet"
    sent = bot.messages[-1]
    assert "No session here" not in str(sent["text"])
    markup = sent.get("reply_markup")
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == "▶️ Start workspace"
    # The state the tap will be checked against, written where aiogram reads it.
    row = await wizard_repo.get(db, 1001, 4242, user_id=1001)
    assert row is not None
    assert row.state_key == new_workspace.CONFIRM_STATE_KEY
    assert row.data["prompt"] == "Fix the flaky test"
    assert row.data["project_id"] == "project-api"


async def test_a_re_dictated_task_edits_the_card_instead_of_stacking_one(
    db: Database, settings_factory: Any, system_db: Database
) -> None:
    """Saying it again is the repair the confirm card exists for.

    The typed path edits the card in place and keeps the run's ``wid``, so the
    buttons already on screen stay live. A second card with a fresh ``wid``
    would leave the visible one answering "Wizard closed · /new to start
    again" — the opposite of a cheap repair.
    """
    settings = settings_factory(voice_enabled=True)
    bot = FakeBot()
    provider = FakeProvider("fix the lonely page")
    service = make_service(settings, db, system_db, bot=bot, provider=provider)

    async def dictate(tg_message_id: int) -> None:
        await voice_repo.create(
            db,
            chat_id=1001,
            tg_message_id=tg_message_id,
            thread_id=4242,
            user_id=1001,
            file_id="file-1",
            file_unique_id=f"unique-{tg_message_id}",
            file_name=None,
            mime_type="audio/ogg",
            duration_seconds=8,
            file_size=100,
            route_kind="dm",
            route_session_id=None,
            route_workspace_id=None,
            provider="elevenlabs",
            model="scribe_v2",
            action_id=f"voice-op-{tg_message_id}",
        )
        claimed = await voice_repo.claim_next(db)
        assert claimed is not None
        await service._process(claimed)

    await dictate(81)
    first = await wizard_repo.get(db, 1001, 4242, user_id=1001)
    assert first is not None
    provider.text = "fix the login page"
    await dictate(82)

    row = await wizard_repo.get(db, 1001, 4242, user_id=1001)
    assert row is not None
    assert row.data["prompt"] == "fix the login page"
    assert row.data["wid"] == first.data["wid"], "the live buttons stay live"
    assert row.tg_message_id == first.tg_message_id, "one card, edited"
