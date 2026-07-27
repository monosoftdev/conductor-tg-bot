"""What runs with no tenant in scope, and must not fall over.

Three real bugs hid here at once, and every one of them was invisible because
the test fixtures supplied a tenant scope that production never has:

* aiogram's FSM middleware reads storage on **every** update, before any
  handler — including the registration commands, which by definition have no
  tenant yet. An unhandled raise there turned ``/register`` into
  "⚠️ Request failed", so nobody could sign up.
* The voice workers polled the tenant-scoped pool from a process-level task.
  Every tick raised, was swallowed into a five-second backoff, and the feature
  was silently dead.
* The status-card tick loop wrote ``sessions.status_card_msg_id`` from the same
  kind of task, so the id went stale across a redeploy.

Each test below runs the real code inside :func:`tests.pg.unscoped`, which is
what a background task actually looks like.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from aiogram.fsm.storage.base import StorageKey

from ctb.bot.app import PostgresStorage
from ctb.db.connection import Database, current_tenant, tenant_scope
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import voice_inputs as voice_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.delivery.status_card import StatusCards
from tests.pg import BOOTSTRAP_TENANT_ID, as_tenant, unscoped

pytestmark = pytest.mark.db

CHAT = -1_002_000_000_555


def storage_key(user_id: int = 4242) -> StorageKey:
    return StorageKey(bot_id=1, chat_id=CHAT, user_id=user_id, thread_id=0)


class TestFsmStorageWithoutATenant:
    """The registration path. Reads answer empty; writes are dropped."""

    async def test_reading_state_answers_empty_rather_than_raising(
        self, db: Database
    ) -> None:
        storage = PostgresStorage(db)
        async with unscoped():
            assert await storage.get_state(storage_key()) is None
            assert await storage.get_data(storage_key()) == {}

    async def test_writing_is_dropped_rather_than_raising(self, db: Database) -> None:
        """There is no tenant to own a wizard, so there is no wizard."""
        storage = PostgresStorage(db)
        async with unscoped():
            await storage.set_state(storage_key(), "NewWorkspace:branch")
            await storage.set_data(storage_key(), {"project": "api"})
        assert await db.fetch_val("SELECT COUNT(*) FROM wizard_state") == 0

    async def test_a_scoped_wizard_still_works(self, db: Database) -> None:
        """The tolerance must not have turned the storage into a no-op."""
        storage = PostgresStorage(db)
        await storage.set_state(storage_key(), "NewWorkspace:branch")
        assert await storage.get_state(storage_key()) == "NewWorkspace:branch"


class TestVoiceClaimWithoutATenant:
    async def test_claiming_a_job_needs_no_scope(
        self, db: Database, system_db: Database
    ) -> None:
        """The claim is cross-tenant; only the processing is scoped."""
        async with as_tenant():
            await voice_repo.create(
                db,
                chat_id=CHAT,
                tg_message_id=9_001,
                thread_id=0,
                user_id=7,
                file_id="f",
                file_unique_id=None,
                file_name=None,
                mime_type="audio/ogg",
                duration_seconds=3,
                file_size=64,
                route_kind="topic",
                route_session_id=None,
                route_workspace_id=None,
                provider="elevenlabs",
                model="scribe_v2",
                action_id="a-1",
            )

        async with unscoped():
            claimed = await voice_repo.claim_next(system_db)

        assert claimed is not None
        assert claimed.tenant_id == BOOTSTRAP_TENANT_ID

    async def test_maintenance_needs_no_scope(self, system_db: Database) -> None:
        async with unscoped():
            recovery = await voice_repo.recover_stale(system_db)
        assert recovery.requeued == 0

    async def test_a_claimed_job_carries_the_tenant_to_scope_with(
        self, db: Database, system_db: Database
    ) -> None:
        """Without it the worker has nothing to enter, and does nothing."""
        async with as_tenant():
            await voice_repo.create(
                db,
                chat_id=CHAT,
                tg_message_id=9_002,
                thread_id=0,
                user_id=7,
                file_id="f",
                file_unique_id=None,
                file_name=None,
                mime_type="audio/ogg",
                duration_seconds=3,
                file_size=64,
                route_kind="topic",
                route_session_id=None,
                route_workspace_id=None,
                provider="elevenlabs",
                model="scribe_v2",
                action_id="a-2",
            )
        async with unscoped():
            row = await voice_repo.claim_next(system_db)
            assert row is not None
            async with tenant_scope(row.tenant_id or uuid.UUID(int=0)):
                assert current_tenant() == BOOTSTRAP_TENANT_ID


class TestStatusCardWithoutATenant:
    async def test_the_card_id_is_persisted_from_an_unscoped_tick(
        self, db: Database
    ) -> None:
        """The tick loop is process-wide; the card carries its own tenant."""
        await workspaces_repo.upsert(db, "ws-card", name="cards")
        await sessions_repo.upsert(db, "sess-card", workspace_id="ws-card")

        cards = StatusCards(_NullSender(), db)  # type: ignore[arg-type]
        card = cards._card(  # noqa: SLF001 - the seam under test
            session_id="sess-card", chat_id=CHAT, thread_id=0, deep_link=None
        )
        assert card.tenant_id == BOOTSTRAP_TENANT_ID

        async with unscoped():
            await cards._persist_card_id(card, 4321)  # noqa: SLF001

        row = await sessions_repo.get(db, "sess-card")
        assert row is not None and row.status_card_msg_id == 4321

    async def test_a_card_with_no_tenant_says_so_instead_of_writing(
        self, db: Database
    ) -> None:
        await workspaces_repo.upsert(db, "ws-card2", name="cards")
        await sessions_repo.upsert(db, "sess-card2", workspace_id="ws-card2")
        cards = StatusCards(_NullSender(), db)  # type: ignore[arg-type]

        async with unscoped():
            orphan = cards._card(  # noqa: SLF001
                session_id="sess-card2", chat_id=CHAT, thread_id=1, deep_link=None
            )
            assert orphan.tenant_id is None
            await cards._persist_card_id(orphan, 99)  # noqa: SLF001

        row = await sessions_repo.get(db, "sess-card2")
        assert row is not None and row.status_card_msg_id is None


class _NullSender:
    async def send_message(self, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("this suite never sends")

    async def edit_message_text(self, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("this suite never sends")

    async def send_chat_action(self, **_kwargs: Any) -> Any:  # pragma: no cover
        return True

    async def pin_chat_message(self, **_kwargs: Any) -> Any:  # pragma: no cover
        return True
