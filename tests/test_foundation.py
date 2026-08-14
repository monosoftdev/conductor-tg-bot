"""Contract tests for the foundation modules eight other modules build on.

Deliberately narrow: these assert the promises the rest of the codebase relies
on — settings fail fast, secrets never reach a log line, the migration applies
against a real SQLite file, and the wire models survive the shapes the live API
actually returns.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ctb.conductor.client import ConductorClient
from ctb.conductor.errors import (
    Ambiguous,
    ApiError,
    AuthFatal,
    NotFound,
    PairingError,
    RateLimited,
    api_error_for_status,
    is_github_connection_required,
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
from ctb.db.errors import ForeignKeyViolation, UniqueViolation
from ctb.db.migrate import (
    MigrationError,
    _checked_sql,
    current_schema_version,
    discover_migrations,
)
from ctb.db.repo.tenancy import _TENANT_COLUMNS
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
from tests.conftest import FAKE_BOT_TOKEN, FAKE_MASTER_KEYS

API_URL = "https://api.conductor.build/v0"


def _env(**overrides: Any) -> dict[str, Any]:
    """A complete, valid environment, so one override is the only thing wrong."""
    base: dict[str, Any] = {
        "_env_file": None,
        "telegram_bot_token": FAKE_BOT_TOKEN,
        "master_keys": FAKE_MASTER_KEYS,
        "database_url": "postgresql://ctb_app@localhost/ctb",
        "system_database_url": "postgresql://ctb_worker@localhost/ctb",
    }
    base.update(overrides)
    return base


class TestSettings:
    def test_missing_configuration_fails_fast_in_one_message(self) -> None:
        """Every missing variable at once. One crash per variable is an outage."""
        with pytest.raises(SettingsError) as exc:
            load_settings(_env_file=None)
        text = str(exc.value)
        assert "TELEGRAM_BOT_TOKEN" in text
        assert "DATABASE_URL" in text
        assert "SYSTEM_DATABASE_URL" in text
        assert "CTB_MASTER_KEYS" in text

    def test_a_blank_secret_counts_as_missing(self) -> None:
        """``TELEGRAM_BOT_TOKEN=`` is "set" to pydantic; it is not to us."""
        with pytest.raises(SettingsError, match="TELEGRAM_BOT_TOKEN"):
            load_settings(**_env(telegram_bot_token="   "))

    def test_malformed_master_keys_are_rejected_at_boot(self) -> None:
        """A bad key must kill the boot, not the first user's first prompt."""
        with pytest.raises(SettingsError, match="CTB_MASTER_KEYS"):
            load_settings(**_env(master_keys="v1:not-base64!!"))

    def test_the_secret_box_round_trips(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        box = settings_factory().secret_box()
        box.self_check()
        assert box.active_kid == "v2"

    def test_platform_admins_are_optional_and_deduplicated(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        """An unattended deployment legitimately has none.

        This is *not* an allow-list for using the bot — tenancy decides that.
        """
        assert settings_factory(platform_admin_ids="").platform_admin_ids == []
        cfg = settings_factory(platform_admin_ids=" 7 , 8,7 ")
        assert cfg.platform_admin_ids == [7, 8]
        assert cfg.is_platform_admin(8) and not cfg.is_platform_admin(9)

    def test_me_lives_at_the_api_root_not_under_v0(self) -> None:
        from ctb.settings import conductor_api_root

        assert conductor_api_root("https://api.conductor.build/v0") == (
            "https://api.conductor.build"
        )

    def test_default_branch_is_settable_and_never_blank(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        """``DEFAULT_BRANCH=dev`` is what makes ``dev`` the offered button."""
        assert settings_factory().default_branch == "main"
        assert settings_factory(default_branch="dev").default_branch == "dev"
        # `cp .env.example .env` leaves `DEFAULT_BRANCH=` behind.
        assert settings_factory(default_branch="  ").default_branch == "main"

    def test_no_tenant_credential_lives_in_the_environment(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        """The whole point of the split: nothing here names a customer."""
        cfg = settings_factory()
        assert not hasattr(cfg, "conductor_api_key")
        assert not hasattr(cfg, "elevenlabs_api_key")
        assert not hasattr(cfg, "allowed_telegram_user_ids")
        assert not hasattr(cfg, "telegram_chat_id")

    def test_the_operator_dsns_are_not_settings_fields(
        self,
        settings_factory: Callable[..., Settings],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Neither operator credential may be reachable from a running bot.

        ``ADMIN_DATABASE_URL`` is a superuser and
        ``TELEGRAM_CONDUCTOR_BOT_DATABASE_URL`` holds BYPASSRLS: both sit
        outside row-level security, and both are set on the same service. So
        they must be inert here — a field named for one is a pool waiting to be
        opened.
        """
        monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql://su@h/db")
        monkeypatch.setenv(
            "TELEGRAM_CONDUCTOR_BOT_DATABASE_URL", "postgresql://ops@h/db"
        )

        cfg = settings_factory()

        assert not hasattr(cfg, "admin_database_url")
        assert not hasattr(cfg, "telegram_conductor_bot_database_url")
        assert "postgresql://su@h/db" not in repr(cfg.model_dump())
        assert "postgresql://ops@h/db" not in repr(cfg.model_dump())


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

    def test_a_tenant_key_is_never_registered_globally(self) -> None:
        """Registering every tenant's key would keep plaintext in memory.

        Conductor keys only ever appear in an ``Authorization`` header, which
        the bearer-token pattern already redacts, so the registry stays
        platform-only and O(1) in the number of tenants.
        """
        from ctb.logging import _secrets

        before = set(_secrets)
        ConductorClient(api_key="cndk_tenant_secret_0001", api_url=API_URL)
        assert set(_secrets) == before

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
        assert type(api_error_for_status(403)) is ApiError
        assert isinstance(api_error_for_status(404), NotFound)
        assert isinstance(api_error_for_status(429), RateLimited)
        assert isinstance(api_error_for_status(503), ApiError)

    def test_only_the_workspace_github_capability_refusal_is_recognized(self) -> None:
        exact = api_error_for_status(
            403,
            {
                "userMessage": (
                    "GitHub is not connected. Connect GitHub in your Conductor "
                    "settings to create cloud workspaces in this organization."
                )
            },
            method="POST",
            path="/workspaces",
        )
        unrelated = api_error_for_status(
            403,
            {"userMessage": "GitHub is not connected"},
            method="GET",
            path="/projects",
        )

        assert is_github_connection_required(exact)
        assert not is_github_connection_required(unrelated)

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

    def test_a_file_that_opens_its_own_transaction_is_rejected(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "001_bad.sql").write_text("BEGIN;\nSELECT 1;\nCOMMIT;")
        with pytest.raises(MigrationError, match="manages its own transaction"):
            _checked_sql(discover_migrations(tmp_path)[0])

    def test_a_literal_containing_dollars_does_not_disarm_the_guard(
        self, tmp_path: Path
    ) -> None:
        """A single-quoted `$$` used to open a dollar quote that never closed.

        Everything after it was skipped, so the ``BEGIN``/``COMMIT`` the guard
        exists to catch sailed straight through.
        """
        (tmp_path / "001_sneaky.sql").write_text(
            "SELECT 'costs $$ dollars';\nBEGIN;\nSELECT 1;\nCOMMIT;"
        )
        with pytest.raises(MigrationError, match="manages its own transaction"):
            _checked_sql(discover_migrations(tmp_path)[0])

    def test_a_do_block_is_not_mistaken_for_one(self, tmp_path: Path) -> None:
        """``DO $$ BEGIN … END $$`` is procedural, not transactional."""
        (tmp_path / "001_ok.sql").write_text("DO $x$\nBEGIN\n  PERFORM 1;\nEND\n$x$;")
        assert _checked_sql(discover_migrations(tmp_path)[0])

    async def test_the_schema_is_applied(self, system_db: Database) -> None:
        assert await current_schema_version(system_db) >= 1

    async def test_every_expected_table_exists(self, system_db: Database) -> None:
        rows = await system_db.fetch_all(
            "SELECT tablename AS name FROM pg_tables "
            "WHERE schemaname = current_schema() ORDER BY name"
        )
        tables = {row["name"] for row in rows}
        assert {
            "api_events",
            "chats",
            "ci_watches",
            "deliveries",
            "enrollment_tokens",
            "outbound_prompts",
            "sessions",
            "singleton_lease",
            "tenant_chats",
            "tenant_members",
            "tenants",
            "transcript_messages",
            "unknown_content_types",
            "voice_inputs",
            "wizard_state",
            "workspaces",
        } <= tables

    async def test_the_old_allowlist_table_is_gone(self, system_db: Database) -> None:
        """``tenant_members`` replaced it; a leftover would be a second door."""
        assert await system_db.fetch_val("SELECT to_regclass('allowed_users')") is None

    async def test_reading_the_version_needs_no_write_rights(
        self, db: Database
    ) -> None:
        """``/health`` calls this on every report, as a role that cannot create."""
        assert await current_schema_version(db) >= 1

    async def test_tenant_projection_treats_migration_002_fields_as_optional(
        self, system_db: Database
    ) -> None:
        """The supervisor must keep reconciling while a deploy is still on 001."""
        async with system_db.transaction():
            await system_db.execute(
                "CREATE TEMP TABLE legacy_tenants "
                "(LIKE tenants INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            for column in (
                "github_key_ct",
                "github_key_kid",
                "github_key_fp",
                "github_key_at",
            ):
                await system_db.execute(
                    f"ALTER TABLE legacy_tenants DROP COLUMN {column}"
                )
            await system_db.execute(
                "INSERT INTO legacy_tenants (slug, name) VALUES ('legacy', 'Legacy')"
            )
            projection = _TENANT_COLUMNS.replace(
                "to_jsonb(tenants)", "to_jsonb(legacy_tenants)"
            )
            row = await system_db.fetch_one(f"SELECT {projection} FROM legacy_tenants")

        assert row is not None
        assert row["slug"] == "legacy"
        assert row["github_key_ct"] is None
        assert row["github_key_kid"] is None
        assert row["github_key_fp"] is None
        assert row["github_key_at"] is None


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

    async def test_a_child_task_does_not_borrow_its_parents_connection(
        self, db: Database
    ) -> None:
        """``create_task`` copies the context, connections are not shareable.

        Without the task-identity check on the bound connection, the child
        would issue statements on the parent's open transaction concurrently.
        """
        seen: list[int] = []

        async def child() -> None:
            seen.append(await db.fetch_val("SELECT COUNT(*) FROM workspaces"))

        async with db.transaction():
            await db.execute("INSERT INTO workspaces(id) VALUES ('w')")
            await asyncio.create_task(child())
        # The child ran on its own connection, so it could not see the
        # uncommitted row — and, crucially, it did not raise.
        assert seen == [0]
        assert await db.fetch_val("SELECT COUNT(*) FROM workspaces") == 1

    async def test_on_conflict_do_nothing_makes_replay_harmless(
        self, db: Database
    ) -> None:
        await db.execute("INSERT INTO sessions(id) VALUES ('s')")
        row = ("s", "s:1:0", 0, "agent", now_ms())
        sql = (
            "INSERT INTO transcript_messages"
            "(session_id, message_id, session_index, type, received_at_ms)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
        )
        await db.execute(sql, row)
        await db.execute(sql, row)
        assert await db.fetch_val("SELECT COUNT(*) FROM transcript_messages") == 1

    async def test_foreign_keys_are_enforced(self, db: Database) -> None:
        with pytest.raises(ForeignKeyViolation):
            await db.execute(
                "INSERT INTO sessions(id, workspace_id) VALUES ('s', 'missing')"
            )

    async def test_the_delivery_claim_can_use_its_index(self, db: Database) -> None:
        """Asserts the index is *usable*, not that the planner picks it today.

        Both statements run in **one** transaction. ``SET LOCAL`` lasts until
        the end of the current transaction, and every ``execute`` outside a
        block is its own — so the setting was discarded before the ``EXPLAIN``
        and this test was silently asserting that the planner happens to prefer
        the index at whatever row count the suite left behind. It passed alone
        and flaked in a full run.
        """
        async with db.transaction():
            await db.execute("SET LOCAL enable_seqscan = off")
            plan = await db.fetch_all(
                "EXPLAIN (FORMAT JSON) SELECT * FROM deliveries "
                "WHERE state = 'pending' ORDER BY session_index, part_index"
            )
        assert "idx_deliveries_claim" in json.dumps(plan, default=str)

    async def test_thread_id_zero_keeps_the_routing_key_unique(
        self, db: Database
    ) -> None:
        await db.execute("INSERT INTO chats(chat_id, thread_id) VALUES (5, 0)")
        with pytest.raises(UniqueViolation):
            await db.execute("INSERT INTO chats(chat_id, thread_id) VALUES (5, 0)")

    async def test_timestamps_in_one_transaction_are_distinct(
        self, db: Database
    ) -> None:
        """``clock_timestamp()``, not ``now()``.

        ``now()`` is fixed at transaction start, which would give every row a
        batch inserts an identical ``created_at`` and flip every
        ``ORDER BY created_at`` tiebreak.

        The sleep is what makes this a *proof* rather than a race. The column
        is epoch **milliseconds**, and three unqualified inserts land inside one
        of them often enough to fail roughly one run in five on a fast machine —
        a red build that says nothing about the property. Two milliseconds of
        real time is a gap ``clock_timestamp()`` must show and ``now()`` cannot.
        """
        async with db.transaction():
            for index in range(3):
                await db.execute(
                    "INSERT INTO workspaces(id) VALUES (?)", (f"w{index}",)
                )
                await db.execute("SELECT pg_sleep(0.002)")
        rows = await db.fetch_all("SELECT created_at FROM workspaces")
        assert len({row["created_at"] for row in rows}) == 3

    async def test_the_pool_reports_its_stats(self, db: Database) -> None:
        """Exhaustion is the new deadlock class; it must be visible first."""
        assert db.stats()["pool_size"] >= 1
