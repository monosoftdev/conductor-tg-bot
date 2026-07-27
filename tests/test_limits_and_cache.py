"""Quotas, backpressure, and the resolution cache — the knobs that were dead.

Four settings shipped documented, validated and never read: the workspace
quota, the delivery backpressure threshold, the sign-up rate limit, and the
attempt cap. Config that describes a protection which does not exist is worse
than no config, because the operator stops looking.

The cache tests are here for a different reason. Every middleware test in the
suite pins ``cache_ttl_s=0.0``, so the 30-second production cache was never
exercised and ``invalidate()`` had no coverage at all — which is how ``/use``
came to sweep the wrong key.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ctb.bot.handlers import common as handlers_common
from ctb.bot.middleware.tenancy import TenantMiddleware, TenantSettings
from ctb.db.connection import Database, now_ms
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import tenancy
from ctb.db.repo import transcript as transcript_repo
from ctb.db.repo import workspaces as workspaces_repo
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID

pytestmark = pytest.mark.db

CHAT = -1_002_000_000_888
USER = 6161


def limits(max_workspaces: int) -> TenantSettings:
    return TenantSettings(max_workspaces=max_workspaces)


class TestWorkspaceQuota:
    """``max_workspaces`` is the only thing bounding a tenant's spend here."""

    async def test_creation_is_refused_at_the_limit(
        self, db: Database, system_db: Database
    ) -> None:
        for index in range(2):
            await workspaces_repo.upsert(db, f"ws-{index}", name=f"w{index}")

        refusal = await handlers_common.quota_error(db, limits(2))

        assert refusal is not None and "limit of 2" in refusal

    async def test_creation_is_allowed_below_the_limit(
        self, db: Database, system_db: Database
    ) -> None:
        await workspaces_repo.upsert(db, "ws-0", name="w0")

        assert await handlers_common.quota_error(db, limits(2)) is None

    async def test_an_archived_workspace_does_not_count(
        self, db: Database, system_db: Database
    ) -> None:
        """Otherwise the quota is a lifetime total, not a concurrent one."""
        for index in range(2):
            await workspaces_repo.upsert(db, f"ws-{index}", name=f"w{index}")
        await workspaces_repo.mark_archived(db, "ws-0")

        assert await handlers_common.quota_error(db, limits(2)) is None


class TestDeliveryBackpressure:
    """``max_pending_deliveries`` sheds bulk so a wedged topic cannot bury the rest."""

    async def _queue(self, db: Database, count: int) -> None:
        await sessions_repo.upsert(db, "sess-backpressure")
        for index in range(count):
            await deliveries_repo.enqueue(
                db,
                session_id="sess-backpressure",
                message_id=f"m{index}",
                chat_id=CHAT,
                session_index=index,
                payload_json='{"kind":"text","html":"x"}',
            )

    async def test_over_the_threshold_nothing_new_is_queued(self, db: Database) -> None:
        await self._queue(db, 3)
        before = await deliveries_repo.pending_count(db)

        assert before == 3
        assert await transcript_repo.should_shed(db, max_pending=3) is True

    async def test_under_the_threshold_it_is_business_as_usual(
        self, db: Database
    ) -> None:
        await self._queue(db, 2)

        assert await transcript_repo.should_shed(db, max_pending=3) is False

    async def test_no_threshold_never_sheds(self, db: Database) -> None:
        await self._queue(db, 50)

        assert await transcript_repo.should_shed(db, max_pending=None) is False


class TestDeliveryRetention:
    """``payload_json`` is the customer's source code in a second table."""

    async def test_settled_rows_older_than_the_window_are_deleted(
        self, db: Database
    ) -> None:
        await sessions_repo.upsert(db, "sess-old")
        await deliveries_repo.enqueue(
            db,
            session_id="sess-old",
            message_id="old",
            chat_id=CHAT,
            payload_json='{"kind":"text","html":"secret source"}',
        )
        key = ("sess-old", "old", 0, CHAT)
        await deliveries_repo.mark_sent(db, key, tg_message_id=1)
        ancient = now_ms() - 31 * 24 * 60 * 60 * 1000
        await db.execute(
            "UPDATE deliveries SET updated_at = ? WHERE message_id = 'old'", (ancient,)
        )

        removed = await deliveries_repo.prune_terminal(db)

        assert removed == 1
        assert await deliveries_repo.get(db, key) is None

    async def test_pending_work_is_never_pruned_however_old(self, db: Database) -> None:
        """An unsent row is work still owed, whatever its timestamp says."""
        await sessions_repo.upsert(db, "sess-old")
        await deliveries_repo.enqueue(
            db,
            session_id="sess-old",
            message_id="waiting",
            chat_id=CHAT,
            payload_json='{"kind":"text","html":"x"}',
        )
        ancient = now_ms() - 365 * 24 * 60 * 60 * 1000
        await db.execute("UPDATE deliveries SET updated_at = ?", (ancient,))

        assert await deliveries_repo.prune_terminal(db) == 0
        assert (
            await deliveries_repo.get(db, ("sess-old", "waiting", 0, CHAT)) is not None
        )


class TestNoticeOrdering:
    """A notice must not overtake the content it is commenting on."""

    async def test_the_next_index_is_past_everything_queued(self, db: Database) -> None:
        await sessions_repo.upsert(db, "sess-order")
        for index in (10, 40, 25):
            await deliveries_repo.enqueue(
                db,
                session_id="sess-order",
                message_id=f"m{index}",
                chat_id=CHAT,
                session_index=index,
                payload_json='{"kind":"text","html":"x"}',
            )

        assert await deliveries_repo.max_pending_index(db, chat_id=CHAT) == 41

    async def test_an_empty_destination_starts_at_zero(self, db: Database) -> None:
        assert await deliveries_repo.max_pending_index(db, chat_id=CHAT) == 0

    async def test_a_sent_row_no_longer_holds_the_queue_open(
        self, db: Database
    ) -> None:
        await sessions_repo.upsert(db, "sess-order")
        await deliveries_repo.enqueue(
            db,
            session_id="sess-order",
            message_id="done",
            chat_id=CHAT,
            session_index=99,
            payload_json='{"kind":"text","html":"x"}',
        )
        await deliveries_repo.mark_sent(
            db, ("sess-order", "done", 0, CHAT), tg_message_id=1
        )

        assert await deliveries_repo.max_pending_index(db, chat_id=CHAT) == 0


class TestRegistrationRate:
    async def test_the_hourly_window_only_counts_recent_signups(
        self, system_db: Database
    ) -> None:
        hour_ago = now_ms() - 60 * 60 * 1000
        assert await tenancy.created_since(system_db, since_ms=hour_ago) >= 1
        # Age both seeded tenants past the window.
        await system_db.execute("UPDATE tenants SET created_at = ?", (hour_ago - 1,))
        assert await tenancy.created_since(system_db, since_ms=hour_ago) == 0


class TestResolutionCache:
    """The production TTL is 30s. Every other test in the suite pins it to 0."""

    def _middleware(self, system_db: Database, clock: Any) -> TenantMiddleware:
        return TenantMiddleware(
            system_db=system_db,
            clients=cast(Any, SimpleNamespace()),
            settings=cast(Any, SimpleNamespace()),
            cache_ttl_s=30.0,
            clock=clock,
        )

    async def test_invalidating_a_tenant_drops_its_cached_chats(
        self, system_db: Database
    ) -> None:
        """`/remove` and `/revoke` must take effect now, not in 30 seconds."""
        now = [0.0]
        middleware = self._middleware(system_db, lambda: now[0])
        await tenancy.bind_chat(system_db, CHAT, BOOTSTRAP_TENANT_ID)

        chat = SimpleNamespace(id=CHAT, type="supergroup")
        user = SimpleNamespace(id=USER, username=None)
        await tenancy.add_member(system_db, BOOTSTRAP_TENANT_ID, USER)
        assert await middleware._resolve(cast(Any, chat), cast(Any, user)) is not None

        await tenancy.remove_member(system_db, BOOTSTRAP_TENANT_ID, USER)
        middleware.invalidate(BOOTSTRAP_TENANT_ID)

        assert await middleware._resolve(cast(Any, chat), cast(Any, user)) is None

    async def test_without_invalidation_the_stale_entry_is_served(
        self, system_db: Database
    ) -> None:
        """Documents the window the invalidation exists to close."""
        now = [0.0]
        middleware = self._middleware(system_db, lambda: now[0])
        await tenancy.bind_chat(system_db, CHAT, BOOTSTRAP_TENANT_ID)
        chat = SimpleNamespace(id=CHAT, type="supergroup")
        user = SimpleNamespace(id=USER, username=None)
        await tenancy.add_member(system_db, BOOTSTRAP_TENANT_ID, USER)
        assert await middleware._resolve(cast(Any, chat), cast(Any, user)) is not None

        await tenancy.remove_member(system_db, BOOTSTRAP_TENANT_ID, USER)

        assert (
            await middleware._resolve(cast(Any, chat), cast(Any, user)) is not None
        )  # cached
        now[0] += 31.0
        assert (
            await middleware._resolve(cast(Any, chat), cast(Any, user)) is None
        )  # expired

    async def test_switching_a_dm_invalidates_the_workspace_it_left(
        self, system_db: Database
    ) -> None:
        """The `/use` bug: sweeping by the *new* tenant matches nothing.

        The stale entry is keyed on the chat and holds the tenant being
        switched *away from*, so invalidating the target leaves it in place and
        the next prompt lands in the wrong Conductor organisation.
        """
        now = [0.0]
        middleware = self._middleware(system_db, lambda: now[0])
        dm_chat = 90_002
        await tenancy.add_member(system_db, BOOTSTRAP_TENANT_ID, USER)
        await tenancy.add_member(system_db, OTHER_TENANT_ID, USER)
        await tenancy.rebind_chat(system_db, dm_chat, BOOTSTRAP_TENANT_ID)

        chat = SimpleNamespace(id=dm_chat, type="private")
        user = SimpleNamespace(id=USER, username=None)
        first = await middleware._resolve(cast(Any, chat), cast(Any, user))
        assert first is not None and first.tenant.id == BOOTSTRAP_TENANT_ID

        await tenancy.rebind_chat(system_db, dm_chat, OTHER_TENANT_ID)
        middleware.invalidate(BOOTSTRAP_TENANT_ID)  # the one we left
        middleware.invalidate(OTHER_TENANT_ID)

        second = await middleware._resolve(cast(Any, chat), cast(Any, user))
        assert second is not None and second.tenant.id == OTHER_TENANT_ID


class TestCallbackBudget:
    """Telegram's 64-byte cap is measured in bytes, per action."""

    def test_no_action_and_target_can_exceed_the_limit(self) -> None:
        from ctb.bot.keyboards import (
            CALLBACK_DATA_LIMIT,
            RESTARTABLE_ACTIONS,
            NonceStore,
            button,
        )

        store = NonceStore()
        for length in (1, 12, 32, 36, 37, 40, 64, 200):
            for action in sorted(RESTARTABLE_ACTIONS):
                item = button(
                    "X",
                    action,
                    "x" * length,
                    store=store,
                    ttl=900.0,
                    restartable=True,
                )
                size = len(str(item.callback_data).encode())
                assert size <= CALLBACK_DATA_LIMIT, (action, length, size)

    def test_a_uuid_target_still_survives_a_restart(self) -> None:
        """The graceful degradation must not kick in for the normal case."""
        from ctb.bot.keyboards import Action, NonceStore, button, read_stateless

        store = NonceStore()
        target = str(uuid.uuid4())
        item = button(
            "Stop", Action.STOP, target, store=store, ttl=900.0, restartable=True
        )
        _prefix, _action, nonce = str(item.callback_data).split(":", 2)

        assert read_stateless(nonce, Action.STOP.value).target == target
