"""Contract tests for the foundation modules eight other modules build on.

Deliberately narrow: these assert the promises the rest of the codebase relies
on — settings fail fast, secrets never reach a log line, the migration applies
against a real SQLite file, and the wire models survive the shapes the live API
actually returns.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ctb.conductor.errors import (
    Ambiguous,
    ApiError,
    AuthFatal,
    NotFound,
    PairingError,
    RateLimited,
    api_error_for_status,
)
from ctb.conductor.models import (
    Agent,
    MessagesPage,
    PostMessageResult,
    PostState,
    SessionStatus,
    SessionStatusValue,
    TranscriptMessage,
    WorkspaceStatusValue,
    validate_pairing,
)
from ctb.db.connection import Database, now_ms
from ctb.db.migrate import apply_migrations, current_schema_version, discover_migrations
from ctb.delivery.render.types import (
    ActivityLine,
    BlockKind,
    CodeBlock,
    DocumentBlock,
    TextBlock,
    Verbosity,
    activity_lines,
    chat_blocks,
    is_visible,
    payload_text,
    utf16_len,
)
from ctb.logging import REDACTED, _forget_secrets, register_secret, scrub_secrets
from ctb.settings import Settings, SettingsError, load_settings
from ctb.turn.state import (
    PendingPrompt,
    TurnContext,
    TurnState,
)


class TestSettings:
    def test_missing_secrets_fail_fast_with_a_useful_message(self) -> None:
        with pytest.raises(SettingsError) as exc:
            load_settings(_env_file=None)
        text = str(exc.value)
        assert "TELEGRAM_BOT_TOKEN" in text
        assert "CONDUCTOR_API_KEY" in text

    def test_empty_allowlist_is_rejected(self) -> None:
        with pytest.raises(SettingsError, match="ALLOWED_TELEGRAM_USER_IDS"):
            load_settings(
                _env_file=None,
                telegram_bot_token="t" * 10,
                conductor_api_key="k" * 10,
                allowed_telegram_user_ids="",
            )

    def test_owner_is_the_first_id_and_duplicates_collapse(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        cfg = settings_factory(allowed_telegram_user_ids=" 7 , 8,7 ")
        assert cfg.allowed_telegram_user_ids == [7, 8]
        assert cfg.owner_id == 7
        assert cfg.is_allowed(8) and not cfg.is_allowed(9)

    def test_me_lives_at_the_api_root_not_under_v0(self, settings: Settings) -> None:
        assert settings.me_url == "https://api.conductor.build/me"

    def test_bad_default_pairing_is_caught_at_boot(self) -> None:
        with pytest.raises(SettingsError, match="not a valid"):
            load_settings(
                _env_file=None,
                telegram_bot_token="t" * 10,
                conductor_api_key="k" * 10,
                allowed_telegram_user_ids="1",
                default_agent="claude",
                default_model="gpt-5.5",
            )


class TestScrubber:
    def teardown_method(self) -> None:
        _forget_secrets()

    def test_authorization_header_and_bearer_tokens_are_redacted(self) -> None:
        out = scrub_secrets(
            event_dict={
                "headers": {"Authorization": "Bearer abc123456789"},
                "curl": "curl -H 'Authorization: Bearer abc123456789'",
            }
        )
        assert out["headers"]["Authorization"] == REDACTED
        assert "abc123456789" not in out["curl"]

    def test_registered_secret_is_redacted_anywhere_including_nested(self) -> None:
        register_secret("supersecretvalue")
        out = scrub_secrets(
            event_dict={
                "url": "https://x/?key=supersecretvalue",
                "list": [{"deep": ["supersecretvalue"]}],
            }
        )
        assert "supersecretvalue" not in repr(out)

    def test_telegram_bot_tokens_are_redacted_even_if_unregistered(self) -> None:
        out = scrub_secrets(
            event_dict={"url": "https://api.telegram.org/bot1234567:" + "A" * 35 + "/x"}
        )
        assert "A" * 35 not in out["url"]

    def test_settings_expose_their_secret_values_for_registration(
        self, settings: Settings
    ) -> None:
        assert len(settings.secret_values()) == 2
        assert all(isinstance(v, str) and v for v in settings.secret_values())

    def test_speech_key_joins_secret_scrubbing(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        key = "elevenlabs_secret_123456"
        settings = settings_factory(elevenlabs_api_key=key)
        assert key in settings.secret_values()


class TestModels:
    def test_envelope_helpers_reach_content_turn_id_and_id(
        self, message_factory: Callable[..., TranscriptMessage]
    ) -> None:
        echo = message_factory(0, kind="userMessage", turn_id="mid-1", text="hi")
        assert echo.is_user_echo
        assert echo.content_id == "mid-1"
        assert echo.witnesses_prompt("mid-1")
        assert not echo.witnesses_prompt("other")

        reply = message_factory(1, kind="assistant", turn_id="mid-1", text="pong")
        assert reply.is_agent and reply.is_assistant_text
        assert reply.turn_id == "mid-1"
        assert reply.belongs_to_turn("mid-1")
        assert reply.blocks == [{"type": "text", "text": "pong"}]

    def test_error_result_is_detected_by_shape(
        self, message_factory: Callable[..., TranscriptMessage]
    ) -> None:
        assert message_factory(2, kind="error", text="boom").is_error
        assert not message_factory(2, kind="result", text="ok").is_error

    def test_untyped_content_never_raises(self) -> None:
        msg = TranscriptMessage.model_validate(
            {"id": "a:1:0", "sessionId": "a", "sessionIndex": 0, "content": "raw"}
        )
        assert msg.content == {"_raw": "raw"}
        assert msg.turn_id is None
        assert msg.blocks == []

    def test_unknown_enum_values_do_not_raise(self) -> None:
        assert (
            SessionStatus.model_validate({"status": "quantum"}).status
            is SessionStatusValue.UNKNOWN
        )
        assert PostMessageResult(message_id="m").state is PostState.UNKNOWN

    def test_unknown_fields_are_tolerated(self) -> None:
        page = MessagesPage.model_validate(
            {"data": [], "hasMore": True, "nextThing": 1}
        )
        assert page.has_more is True

    def test_workspace_status_classification(self) -> None:
        assert WorkspaceStatusValue.READY.is_usable
        assert WorkspaceStatusValue.SLEEPING.is_waking
        assert WorkspaceStatusValue.ARCHIVED.is_gone

    def test_pairing_validation_catches_a_400_before_it_happens(self) -> None:
        assert validate_pairing("claude", "opus-5-1m", "high")[0] is Agent.CLAUDE
        with pytest.raises(PairingError):
            validate_pairing("claude", "gpt-5.5")
        with pytest.raises(PairingError):
            validate_pairing("codex", "gpt-5.5", "ultra")
        with pytest.raises(PairingError):
            validate_pairing("gemini")
        # cursor has no documented effort levels: pass through, do not guess.
        assert validate_pairing("cursor", "auto", "high")[2] == "high"


class TestErrors:
    def test_status_mapping(self) -> None:
        assert isinstance(api_error_for_status(401), AuthFatal)
        assert isinstance(api_error_for_status(403), AuthFatal)
        assert isinstance(api_error_for_status(404), NotFound)
        assert isinstance(api_error_for_status(429), RateLimited)
        assert isinstance(api_error_for_status(503), ApiError)

    def test_auth_and_not_found_are_never_retryable(self) -> None:
        assert api_error_for_status(401).retryable is False
        assert api_error_for_status(404).retryable is False
        assert api_error_for_status(503).retryable is True

    def test_declared_retryable_beats_the_status_default(self) -> None:
        body: dict[str, Any] = {"userMessage": "nope", "retryable": False}
        assert ApiError(503, body).retryable is False
        assert ApiError(400, {"retryable": True}).retryable is True

    def test_ambiguous_carries_the_idempotency_key(self) -> None:
        err = Ambiguous("timeout", message_id="m-1", idempotent=True)
        assert err.message_id == "m-1" and err.idempotent


class TestTurnContext:
    def test_outstanding_tracks_pending_prompts_and_ages_out(self) -> None:
        ctx = TurnContext().with_prompt(PendingPrompt("m1", posted_at=100.0))
        assert ctx.outstanding == 1
        assert ctx.live_outstanding(101.0) == 1
        assert ctx.live_outstanding(100.0 + 301.0) == 0
        assert ctx.without_prompts({"m1"}).outstanding == 0

    def test_enter_stamps_the_state_clock(self) -> None:
        ctx = TurnContext().enter(TurnState.WORKING, 50.0, start_witnessed=True)
        assert ctx.state is TurnState.WORKING
        assert ctx.entered_state_at == 50.0
        assert ctx.start_witnessed
        assert ctx.elapsed_in_state(53.0) == 3.0

    def test_quiet_for_falls_back_to_state_entry(self) -> None:
        ctx = TurnContext().enter(TurnState.WORKING, 10.0)
        assert ctx.quiet_for(15.0) == 5.0
        assert ctx.evolve(last_delta_at=14.0).quiet_for(15.0) == 1.0

    def test_context_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            TurnContext().state = TurnState.DEAD  # type: ignore[misc]


class TestBlocks:
    def test_activity_lines_never_reach_the_chat(self) -> None:
        blocks = [
            TextBlock(html="<b>done</b>"),
            ActivityLine(text="running pytest"),
            DocumentBlock(filename="turn.md", content="x"),
        ]
        assert len(chat_blocks(blocks)) == 2
        assert activity_lines(blocks) == ["running pytest"]

    def test_utf16_length_is_not_python_length(self) -> None:
        assert utf16_len("😀") == 2
        assert len("😀") == 1

    def test_payload_text_is_uniform_across_block_types(self) -> None:
        assert payload_text(TextBlock(html="<i>x</i>")) == "<i>x</i>"
        assert payload_text(CodeBlock(text="y", language="py")) == "y"
        assert payload_text(ActivityLine(text="z")) == "z"

    def test_errors_are_visible_at_every_verbosity(self) -> None:
        for verbosity in Verbosity:
            assert is_visible(BlockKind.ERROR, verbosity)
        assert not is_visible(BlockKind.THINKING, Verbosity.NORMAL)
        assert is_visible(BlockKind.THINKING, Verbosity.VERBOSE)


class TestMigrations:
    def test_files_are_discovered_in_version_order(self) -> None:
        migrations = discover_migrations()
        assert [m.version for m in migrations] == sorted(m.version for m in migrations)
        assert migrations[0].version == 1

    async def test_schema_applies_to_a_real_file(self, db: Database) -> None:
        assert await current_schema_version(db) == 2
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in rows}
        assert {
            "allowed_users",
            "api_events",
            "chats",
            "deliveries",
            "outbound_prompts",
            "sessions",
            "singleton_lease",
            "transcript_messages",
            "unknown_content_types",
            "voice_inputs",
            "wizard_state",
            "workspaces",
        } <= tables

    async def test_applying_twice_is_a_no_op(self, db: Database) -> None:
        assert await apply_migrations(db) == ()

    async def test_pragmas(self, db: Database) -> None:
        assert await db.fetch_val("PRAGMA journal_mode") == "wal"
        assert await db.fetch_val("PRAGMA foreign_keys") == 1
        assert await db.fetch_val("PRAGMA busy_timeout") == 5000


class TestDatabase:
    async def test_transaction_rolls_back_on_error(self, db: Database) -> None:
        with pytest.raises(ValueError):
            async with db.transaction():
                await db.execute("INSERT INTO workspaces(id) VALUES ('w')")
                raise ValueError("boom")
        assert await db.fetch_val("SELECT COUNT(*) FROM workspaces") == 0

    async def test_nested_transactions_join_the_outer_one(self, db: Database) -> None:
        async with db.transaction():
            await db.execute("INSERT INTO workspaces(id) VALUES ('w')")
            async with db.transaction():
                await db.execute("INSERT INTO workspaces(id) VALUES ('w2')")
        assert await db.fetch_val("SELECT COUNT(*) FROM workspaces") == 2

    async def test_insert_or_ignore_makes_replay_harmless(self, db: Database) -> None:
        await db.execute("INSERT INTO sessions(id) VALUES ('s')")
        row = ("s", "s:1:0", 0, "agent", now_ms())
        sql = (
            "INSERT OR IGNORE INTO transcript_messages"
            "(session_id, message_id, session_index, type, received_at_ms)"
            " VALUES (?, ?, ?, ?, ?)"
        )
        await db.execute(sql, row)
        await db.execute(sql, row)
        assert await db.fetch_val("SELECT COUNT(*) FROM transcript_messages") == 1

    async def test_foreign_keys_are_enforced(self, db: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO sessions(id, workspace_id) VALUES ('s', 'missing')"
            )

    async def test_delivery_claim_uses_its_index(self, db: Database) -> None:
        plan = await db.fetch_all(
            "EXPLAIN QUERY PLAN SELECT * FROM deliveries WHERE state='pending' "
            "ORDER BY session_index, part_index"
        )
        assert any("idx_deliveries_claim" in str(tuple(row)) for row in plan)

    async def test_thread_id_zero_keeps_the_routing_key_unique(
        self, db: Database
    ) -> None:
        await db.execute("INSERT INTO chats(chat_id, thread_id) VALUES (5, 0)")
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO chats(chat_id, thread_id) VALUES (5, 0)")

    async def test_db_fixture_uses_a_temp_file(
        self, db: Database, db_path: Path
    ) -> None:
        assert db.path == db_path
        assert db_path.exists()
