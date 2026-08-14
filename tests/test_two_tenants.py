"""Two customers, one bot, one database — end to end through the dispatcher.

Everything else in the suite tests a layer. This drives real updates through
the real middleware stack and asserts the property the whole change exists for:
**two workspaces sharing one bot cannot see or touch each other's anything.**

The Conductor API is faked, but the routing, the tenancy lookups, row-level
security, the client pool and the delivery queue are all the production code.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message, Update, User

from ctb.bot.app import PostgresStorage, install_middleware
from ctb.bot.middleware.tenancy import TenantContext
from ctb.conductor.pool import ClientPool
from ctb.crypto import SecretBox
from ctb.db.connection import Database, now_ms, tenant_scope
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import tenancy
from ctb.db.repo import workspaces as workspaces_repo
from ctb.delivery.outbox import Outbox
from ctb.settings import Settings
from tests.conftest import FAKE_BOT_TOKEN
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID

pytestmark = pytest.mark.db

NOW = dt.datetime(2026, 7, 26, 12, 0, tzinfo=dt.UTC)

ACME_GROUP = -1_002_000_000_101
ACME_OWNER = 90_001
RIVAL_GROUP = -1_002_000_000_202
RIVAL_OWNER = 90_002
OUTSIDER = 90_099


class SilentSession(BaseSession):
    """Records outgoing calls and never opens a socket."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        return True

    async def stream_content(  # type: ignore[override]
        self, *_args: Any, **_kwargs: Any
    ) -> AsyncGenerator[bytes]:
        yield b""  # pragma: no cover - never used


class StubPool:
    """A client pool that hands out a marker per tenant instead of a client."""

    def __init__(self) -> None:
        self.built: list[uuid.UUID] = []

    async def get(self, tenant: Any) -> Any:
        self.built.append(tenant.id)
        return object()

    def peek(self, _tenant_id: uuid.UUID) -> None:
        return None


def message(chat_id: int, user_id: int, text: str = "hello") -> Update:
    private = chat_id == user_id
    return Update(
        update_id=abs(chat_id) % 100_000 + user_id,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=chat_id, type="private" if private else "supergroup"),
            from_user=User(id=user_id, is_bot=False, first_name="u"),
            text=text,
        ),
    )


@pytest.fixture
def bot() -> Bot:
    return Bot(token=FAKE_BOT_TOKEN, session=SilentSession())


@pytest.fixture
async def two_tenants(system_db: Database) -> None:
    """Acme and Rival: separate groups, separate owners, one bot."""
    await tenancy.bind_chat(system_db, ACME_GROUP, BOOTSTRAP_TENANT_ID, is_primary=True)
    await tenancy.add_member(system_db, BOOTSTRAP_TENANT_ID, ACME_OWNER, role="owner")
    await tenancy.bind_chat(system_db, RIVAL_GROUP, OTHER_TENANT_ID, is_primary=True)
    await tenancy.add_member(system_db, OTHER_TENANT_ID, RIVAL_OWNER, role="owner")


@pytest.fixture
def seen(
    settings: Settings, db: Database, system_db: Database
) -> tuple[Dispatcher, list[TenantContext | None]]:
    """The production middleware stack, recording who each update resolved to."""
    captured: list[TenantContext | None] = []
    dispatcher = Dispatcher(
        storage=PostgresStorage(db), settings=settings, db=db, nonces=None
    )
    install_middleware(
        dispatcher,
        settings=settings,
        system_db=system_db,
        clients=cast(ClientPool, StubPool()),
        db=db,
    )

    async def record(_message: Message, **data: Any) -> None:
        captured.append(data.get("tenant"))

    router = Router(name="record")
    router.message.register(record)
    dispatcher.include_router(router)
    return dispatcher, captured


class TestResolution:
    async def test_each_group_resolves_to_its_own_workspace(
        self,
        bot: Bot,
        two_tenants: None,
        seen: tuple[Dispatcher, list[TenantContext | None]],
    ) -> None:
        dispatcher, captured = seen
        await dispatcher.feed_update(bot, message(ACME_GROUP, ACME_OWNER))
        await dispatcher.feed_update(bot, message(RIVAL_GROUP, RIVAL_OWNER))

        assert [ctx.tenant_id for ctx in captured if ctx] == [
            BOOTSTRAP_TENANT_ID,
            OTHER_TENANT_ID,
        ]

    async def test_one_owner_cannot_speak_in_the_others_group(
        self,
        bot: Bot,
        two_tenants: None,
        seen: tuple[Dispatcher, list[TenantContext | None]],
    ) -> None:
        """The heart of it: the right human, the wrong workspace, is silence."""
        dispatcher, captured = seen
        await dispatcher.feed_update(bot, message(RIVAL_GROUP, ACME_OWNER))
        assert captured == []

    async def test_a_stranger_reaches_neither(
        self,
        bot: Bot,
        two_tenants: None,
        seen: tuple[Dispatcher, list[TenantContext | None]],
    ) -> None:
        dispatcher, captured = seen
        await dispatcher.feed_update(bot, message(ACME_GROUP, OUTSIDER))
        await dispatcher.feed_update(bot, message(RIVAL_GROUP, OUTSIDER))
        assert captured == []

    async def test_a_shared_member_gets_the_workspace_of_the_group_they_typed_in(
        self,
        bot: Bot,
        system_db: Database,
        two_tenants: None,
        seen: tuple[Dispatcher, list[TenantContext | None]],
    ) -> None:
        """A consultant in both workspaces still has one identity per room."""
        await tenancy.add_member(system_db, OTHER_TENANT_ID, ACME_OWNER, role="member")
        dispatcher, captured = seen

        await dispatcher.feed_update(bot, message(ACME_GROUP, ACME_OWNER))
        await dispatcher.feed_update(bot, message(RIVAL_GROUP, ACME_OWNER))

        assert [ctx.tenant_id for ctx in captured if ctx] == [
            BOOTSTRAP_TENANT_ID,
            OTHER_TENANT_ID,
        ]
        assert [ctx.role for ctx in captured if ctx] == ["owner", "member"]

    async def test_suspending_one_workspace_leaves_the_other_running(
        self,
        bot: Bot,
        system_db: Database,
        two_tenants: None,
        seen: tuple[Dispatcher, list[TenantContext | None]],
    ) -> None:
        await tenancy.set_status(system_db, BOOTSTRAP_TENANT_ID, "suspended")
        dispatcher, captured = seen

        await dispatcher.feed_update(bot, message(ACME_GROUP, ACME_OWNER))
        await dispatcher.feed_update(bot, message(RIVAL_GROUP, RIVAL_OWNER))

        assert [ctx.tenant_id for ctx in captured if ctx] == [OTHER_TENANT_ID]

    async def test_each_workspace_gets_its_own_conductor_client(
        self,
        settings: Settings,
        db: Database,
        system_db: Database,
        bot: Bot,
        two_tenants: None,
    ) -> None:
        """One key per workspace. There is no process-wide client to fall back on."""
        pool = StubPool()
        dispatcher = Dispatcher(storage=PostgresStorage(db), settings=settings, db=db)
        install_middleware(
            dispatcher,
            settings=settings,
            system_db=system_db,
            clients=cast(ClientPool, pool),
            db=db,
        )
        router = Router(name="noop")
        router.message.register(lambda *_a, **_k: None)
        dispatcher.include_router(router)

        await dispatcher.feed_update(bot, message(ACME_GROUP, ACME_OWNER))
        await dispatcher.feed_update(bot, message(RIVAL_GROUP, RIVAL_OWNER))

        assert pool.built == [BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID]


class TestDataIsolation:
    async def _seed(
        self, db: Database, tenant_id: uuid.UUID, mark: str, chat_id: int
    ) -> None:
        """One workspace's worth of rows. Chat ids differ, as they must.

        A Telegram chat belongs to exactly one workspace — that is what
        ``tenant_chats.chat_id`` being a primary key means — so seeding two
        tenants against one chat id would be modelling something that cannot
        happen.
        """
        async with tenant_scope(tenant_id):
            await workspaces_repo.upsert(db, f"ws-{mark}", name=f"{mark} repo")
            await sessions_repo.upsert(
                db, f"sess-{mark}", workspace_id=f"ws-{mark}", chat_id=chat_id
            )
            await chats_repo.ensure(db, chat_id, 0, kind="general")
            await deliveries_repo.enqueue(
                db,
                session_id=f"sess-{mark}",
                message_id=f"m-{mark}",
                chat_id=chat_id,
                payload_json=f'{{"text":"{mark} secret"}}',
            )

    async def test_neither_workspace_can_read_the_others_rows(
        self, db: Database, two_tenants: None
    ) -> None:
        await self._seed(db, BOOTSTRAP_TENANT_ID, "acme", ACME_GROUP)
        await self._seed(db, OTHER_TENANT_ID, "rivl", RIVAL_GROUP)

        async with tenant_scope(BOOTSTRAP_TENANT_ID):
            assert [w.id for w in await workspaces_repo.list_all(db)] == ["ws-acme"]
            assert [s.id for s in await sessions_repo.list_all(db)] == ["sess-acme"]
            assert await sessions_repo.get(db, "sess-rivl") is None

        async with tenant_scope(OTHER_TENANT_ID):
            assert [w.id for w in await workspaces_repo.list_all(db)] == ["ws-rivl"]
            assert await sessions_repo.get(db, "sess-acme") is None

    async def test_a_delivery_claim_stays_inside_its_workspace(
        self, db: Database, two_tenants: None
    ) -> None:
        """The outbox claims across tenants; a *scoped* claim must not."""
        await self._seed(db, BOOTSTRAP_TENANT_ID, "acme", ACME_GROUP)
        await self._seed(db, OTHER_TENANT_ID, "rivl", RIVAL_GROUP)

        async with tenant_scope(BOOTSTRAP_TENANT_ID):
            rows = await deliveries_repo.claim(db, claim_id="acme-worker", limit=99)
        assert [row.message_id for row in rows] == ["m-acme"]

    async def test_the_worker_pool_sees_both_which_is_how_delivery_works(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        await self._seed(db, BOOTSTRAP_TENANT_ID, "acme", ACME_GROUP)
        await self._seed(db, OTHER_TENANT_ID, "rivl", RIVAL_GROUP)

        destinations = await deliveries_repo.pending_destinations(system_db)

        assert {d.tenant_id for d in destinations} == {
            BOOTSTRAP_TENANT_ID,
            OTHER_TENANT_ID,
        }

    async def test_deleting_a_workspace_takes_only_its_own_data(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        await self._seed(db, BOOTSTRAP_TENANT_ID, "acme", ACME_GROUP)
        await self._seed(db, OTHER_TENANT_ID, "rivl", RIVAL_GROUP)

        await tenancy.delete_tenant(system_db, BOOTSTRAP_TENANT_ID)

        assert await system_db.fetch_val("SELECT COUNT(*) FROM sessions") == 1
        async with tenant_scope(OTHER_TENANT_ID):
            assert await sessions_repo.get(db, "sess-rivl") is not None


class TestKeyIsolation:
    async def test_one_workspaces_sealed_key_cannot_be_opened_as_anothers(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        """A row swap in the database must not move a key between customers."""
        from ctb.crypto import SecretError
        from ctb.runtime import secret_box

        box: SecretBox = secret_box()
        sealed = box.seal(
            "cndk_acme_only",
            tenant_id=BOOTSTRAP_TENANT_ID,
            purpose="conductor_api_key",
        )
        await tenancy.set_conductor_key(
            system_db,
            OTHER_TENANT_ID,
            ciphertext=sealed,
            kid=box.active_kid,
            fingerprint="stolen",
        )

        stolen = await tenancy.get(system_db, OTHER_TENANT_ID)
        assert stolen is not None
        with pytest.raises(SecretError, match="authentication"):
            box.open(
                stolen.conductor_key_ct,
                tenant_id=OTHER_TENANT_ID,
                purpose="conductor_api_key",
            )


class TestDeliveryNeverCrossesTenants:
    """The outbox runs on the BYPASSRLS pool, so its own filter must be right.

    A chat belongs to one workspace — but that is one table's invariant, and
    ``unbind_chat`` breaks it while leaving the old workspace's rows behind.
    The delivery path must be safe without relying on that.
    """

    async def _queue(
        self, db: Database, tenant_id: uuid.UUID, mark: str, chat_id: int
    ) -> None:
        async with tenant_scope(tenant_id):
            await workspaces_repo.upsert(db, f"ws-{mark}", name=mark)
            await sessions_repo.upsert(
                db, f"sess-{mark}", workspace_id=f"ws-{mark}", chat_id=chat_id
            )
            await deliveries_repo.enqueue(
                db,
                session_id=f"sess-{mark}",
                message_id=f"m-{mark}",
                chat_id=chat_id,
                payload_json=f'{{"text":"{mark} secret"}}',
            )

    async def test_a_claim_for_one_workspace_cannot_take_anothers_rows(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        """Even when both workspaces somehow queued for the same chat id."""
        shared = -1_002_000_000_999
        await self._queue(db, BOOTSTRAP_TENANT_ID, "acme", shared)
        await self._queue(db, OTHER_TENANT_ID, "rivl", shared)

        rows = await deliveries_repo.claim(
            system_db,
            claim_id="outbox",
            limit=99,
            tenant_id=BOOTSTRAP_TENANT_ID,
            chat_id=shared,
            thread_id=0,
        )

        assert [row.message_id for row in rows] == ["m-acme"]

    async def test_the_outbox_carries_the_tenant_from_the_destination(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        """`pending_destinations` reports it; `run_once` must actually use it."""
        shared = -1_002_000_000_998
        await self._queue(db, BOOTSTRAP_TENANT_ID, "acme", shared)
        await self._queue(db, OTHER_TENANT_ID, "rivl", shared)

        claimed: list[tuple[uuid.UUID | None, int | None]] = []
        real_claim = deliveries_repo.claim

        async def spy(*args: Any, **kwargs: Any) -> Any:
            claimed.append((kwargs.get("tenant_id"), kwargs.get("chat_id")))
            return await real_claim(*args, **kwargs)

        outbox = Outbox(cast(Any, _NullBot()), system_db, orphan_after_ms=0)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(deliveries_repo, "claim", spy)
            await outbox.run_once()

        assert claimed, "run_once claimed nothing"
        assert all(tenant is not None for tenant, _chat in claimed), (
            "a claim went out with no tenant filter"
        )


class _NullBot:
    """Swallows every send; this suite is about what gets *claimed*."""

    async def send_message(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(message_id=1)

    async def send_document(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(message_id=1)


class TestPollingIsGatedByTheTenantRow:
    """``sessions.list_bound`` joins ``tenants``, and that join *is* the switch.

    Suspension and a rejected key both stop polling with no code path involved,
    which is why the join has to be there and has to be tested.
    """

    async def _bind(self, db: Database, tenant_id: uuid.UUID, mark: str) -> None:
        async with tenant_scope(tenant_id):
            await workspaces_repo.upsert(db, f"ws-{mark}", name=mark)
            await sessions_repo.upsert(
                db, f"sess-{mark}", workspace_id=f"ws-{mark}", is_bound=True
            )

    async def test_both_workspaces_are_polled_while_active(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        await self._bind(db, BOOTSTRAP_TENANT_ID, "acme")
        await self._bind(db, OTHER_TENANT_ID, "rivl")
        bound = await sessions_repo.list_bound(system_db)
        assert {row.id for row in bound} == {"sess-acme", "sess-rivl"}

    async def test_suspending_one_stops_only_its_polling(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        await self._bind(db, BOOTSTRAP_TENANT_ID, "acme")
        await self._bind(db, OTHER_TENANT_ID, "rivl")

        await tenancy.set_status(system_db, BOOTSTRAP_TENANT_ID, "suspended")

        bound = await sessions_repo.list_bound(system_db)
        assert {row.id for row in bound} == {"sess-rivl"}

    async def test_a_rejected_key_stops_only_its_polling(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        await self._bind(db, BOOTSTRAP_TENANT_ID, "acme")
        await self._bind(db, OTHER_TENANT_ID, "rivl")

        await tenancy.mark_auth_failed(system_db, OTHER_TENANT_ID, reason="401")

        bound = await sessions_repo.list_bound(system_db)
        assert {row.id for row in bound} == {"sess-acme"}

    async def test_a_new_key_starts_it_again(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        """The fast way to clear the stamp, and the intended flow."""
        await self._bind(db, BOOTSTRAP_TENANT_ID, "acme")
        await tenancy.mark_auth_failed(system_db, BOOTSTRAP_TENANT_ID, reason="401")
        assert await sessions_repo.list_bound(system_db) == []

        await tenancy.set_conductor_key(
            system_db,
            BOOTSTRAP_TENANT_ID,
            ciphertext=b"sealed",
            kid="v2",
            fingerprint="fp",
        )
        assert [row.id for row in await sessions_repo.list_bound(system_db)] == [
            "sess-acme"
        ]

    async def test_the_stamp_expires_so_a_false_latch_is_not_permanent(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        """The slow way, and the only one that works with nobody watching.

        A 401 through a proxy wobble used to stop a team until a human noticed
        and re-sent ``/key`` — which, live, meant four days of a bot that
        accepted every command and never answered one.
        """
        await self._bind(db, BOOTSTRAP_TENANT_ID, "acme")
        await tenancy.mark_auth_failed(
            system_db,
            BOOTSTRAP_TENANT_ID,
            reason="401",
            at=now_ms() - tenancy.AUTH_RETRY_AFTER_MS - 1,
        )
        assert [row.id for row in await sessions_repo.list_bound(system_db)] == [
            "sess-acme"
        ]

    async def test_a_fresh_rejection_restarts_the_wait(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        """The stamp must move forward, or the window never closes again."""
        await self._bind(db, BOOTSTRAP_TENANT_ID, "acme")
        await tenancy.mark_auth_failed(
            system_db,
            BOOTSTRAP_TENANT_ID,
            reason="401",
            at=now_ms() - tenancy.AUTH_RETRY_AFTER_MS - 1,
        )
        # The retry went through and was rejected again.
        await tenancy.mark_auth_failed(system_db, BOOTSTRAP_TENANT_ID, reason="401")
        assert await sessions_repo.list_bound(system_db) == []

    async def test_suspension_has_no_such_clock(
        self, db: Database, system_db: Database, two_tenants: None
    ) -> None:
        """An operator's decision is not undone by waiting."""
        await self._bind(db, BOOTSTRAP_TENANT_ID, "acme")
        await tenancy.set_status(system_db, BOOTSTRAP_TENANT_ID, "suspended")
        await tenancy.mark_auth_failed(
            system_db,
            BOOTSTRAP_TENANT_ID,
            reason="401",
            at=now_ms() - tenancy.AUTH_RETRY_AFTER_MS - 1,
        )
        assert await sessions_repo.list_bound(system_db) == []
