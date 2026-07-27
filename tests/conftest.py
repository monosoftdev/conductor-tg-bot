"""Shared fixtures.

``asyncio_mode = auto`` is set in ``pyproject.toml``, so ``async def test_…``
and async fixtures need no decorator.

The environment is scrubbed for every test: a developer with a real
``CONDUCTOR_API_KEY`` exported (or a ``.env`` on disk) must not have it leak
into a test run and make an offline test pass for the wrong reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ctb.conductor.models import TranscriptMessage
from ctb.crypto import SecretBox
from ctb.settings import Settings, reset_settings, set_settings
from tests.pg import (  # noqa: F401 - fixtures are used by name
    BOOTSTRAP_TENANT_ID,
    OTHER_TENANT_ID,
    TEST_MASTER_KEYS,
    app_dsn,
    db,
    pg_reset,
    pg_schema,
    system_db,
    worker_dsn,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_OUT = REPO_ROOT / "probe-out"

#: Env vars that must never bleed in from the developer's shell.
_LEAKY_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "CTB_MASTER_KEYS",
    "DATABASE_URL",
    "SYSTEM_DATABASE_URL",
    "CONDUCTOR_API_URL",
    "PLATFORM_ADMIN_IDS",
    "REGISTRATION_OPEN",
    "DEFAULT_AGENT",
    "DEFAULT_MODEL",
    "DEFAULT_EFFORT",
    "DEFAULT_BRANCH",
    "LOG_TRANSCRIPT_CONTENT",
    "LOG_LEVEL",
    "VOICE_ENABLED",
    "VOICE_STT_PROVIDER",
    "VOICE_STT_MODEL",
    "VOICE_MODE",
    "VOICE_MAX_DURATION_SECONDS",
    "VOICE_MAX_FILE_BYTES",
    "VOICE_MAX_CONCURRENT",
    "VOICE_LANGUAGE",
    "VOICE_WAKE_PHRASES",
    "VOICE_COMPLETED_RETENTION_DAYS",
    "ELEVENLABS_API_KEY",
)

#: Not real. Long enough to exercise the log scrubber's minimum secret length.
FAKE_BOT_TOKEN = "1234567890:TESTtokenTESTtokenTESTtokenTESTtoken12"
FAKE_API_KEY = "ctb_test_api_key_0000000000"

#: Deterministic so a failing test is reproducible; never used anywhere real.
FAKE_MASTER_KEYS = TEST_MASTER_KEYS


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    """Build a :class:`Settings` with safe defaults and no ``.env`` on disk."""

    def make(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "_env_file": None,
            "telegram_bot_token": FAKE_BOT_TOKEN,
            "master_keys": FAKE_MASTER_KEYS,
            "database_url": app_dsn(),
            "system_database_url": worker_dsn(),
            "conductor_api_url": "https://api.conductor.build/v0",
            "platform_admin_ids": "1001,1002",
            "log_transcript_content": False,
            "log_level": "DEBUG",
        }
        base.update(overrides)
        return Settings(**base)

    return make


@pytest.fixture
def secret_box() -> SecretBox:
    """A real :class:`SecretBox` over throwaway keys."""
    return SecretBox.from_env_value(FAKE_MASTER_KEYS)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Iterator[Settings]:
    """Install process-wide settings for the duration of one test."""
    value = settings_factory()
    set_settings(value)
    yield value
    reset_settings()


class FakeClock:
    """A deterministic monotonic clock for the turn machine and pollers.

    Call it like ``time.monotonic``; move it with :meth:`advance`.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def message_factory() -> Callable[..., TranscriptMessage]:
    """Build transcript envelopes in the exact shape the live API returns.

    Verified against ``probe-out/`` — the envelope id is the server-assigned
    composite ``"<sessionId>:<seq>:<sub>"``, and our POSTed ``messageId``
    appears as ``content.id`` on the user echo and as ``content.turnId``
    everywhere in the turn.
    """

    def make(
        session_index: int,
        *,
        session_id: str = "sess-1",
        turn_id: str = "turn-1",
        kind: str = "assistant",
        text: str = "hello",
        seq: int | None = None,
    ) -> TranscriptMessage:
        envelope_id = f"{session_id}:{seq if seq is not None else session_index + 1}:0"
        content: dict[str, Any]
        if kind == "userMessage":
            content = {
                "eventId": envelope_id,
                "type": "userMessage",
                "senderId": "user-1",
                "senderApiKeyName": "test-key",
                "id": turn_id,
                "message": text,
                "state": "sent",
                "turnId": turn_id,
                "config": {
                    "collaborationMode": "default",
                    "model": "sonnet",
                    "thinkingLevel": "none",
                },
            }
            envelope_type = "userMessage"
        else:
            payload: dict[str, Any]
            match kind:
                case "assistant":
                    payload = {
                        "type": "assistant",
                        "message": {
                            "model": "claude-sonnet-4-6",
                            "id": "msg_test",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "text", "text": text}],
                        },
                        "session_id": session_id,
                        "uuid": f"uuid-{session_index}",
                    }
                case "result":
                    payload = {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": text,
                        "stop_reason": "end_turn",
                        "num_turns": 1,
                        "total_cost_usd": 0.01,
                        "duration_ms": 6_300,
                        "session_id": session_id,
                        "uuid": f"uuid-{session_index}",
                    }
                case "error":
                    payload = {
                        "type": "result",
                        "subtype": "error",
                        "is_error": True,
                        "result": text,
                        "session_id": session_id,
                        "uuid": f"uuid-{session_index}",
                    }
                case "system":
                    payload = {
                        "type": "system",
                        "subtype": "session_state_changed",
                        "state": "running",
                        "session_id": session_id,
                        "uuid": f"uuid-{session_index}",
                    }
                case _:
                    payload = {"type": kind, "session_id": session_id}
            content = {
                "eventId": envelope_id,
                "type": "agent",
                "rawPayload": payload,
                "userMessageId": turn_id,
                "turnId": turn_id,
            }
            envelope_type = "agent"

        return TranscriptMessage(
            id=envelope_id,
            session_id=session_id,
            session_index=session_index,
            type=envelope_type,
            content=content,
            received_at="2026-07-26 02:00:37.434+00",
        )

    return make


@pytest.fixture
def probe_transcripts() -> list[TranscriptMessage]:
    """Real envelopes from the Phase 0 probe, when they exist locally.

    ``probe-out/`` is gitignored (it holds real transcript content, which is the
    user's source code), so this skips rather than fails on a clean checkout.
    """
    path = PROBE_OUT / "transcripts.jsonl"
    if not path.exists():
        pytest.skip("probe-out/transcripts.jsonl not present (run the Phase 0 probe)")
    messages: list[TranscriptMessage] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            messages.append(TranscriptMessage.model_validate(json.loads(line)))
    return messages
