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
from ctb.db.connection import Database, tenant_scope
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import tenancy
from ctb.db.repo import workspaces as workspaces_repo
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
