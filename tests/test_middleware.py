"""The Telegram foundation: allow-list, routing, log context, nonces, app.

The load-bearing assertions, in the order the brief states them:

* a non-allow-listed user gets **silence** on every update type;
* the owner is DM'd at most once per unknown user per day;
* a nonce is single-use and expires at 60 s;
* ``(chat_id, thread_id)`` routing resolves, including the DM (``thread_id=0``)
  case.

Everything runs offline. ``RecordingSession`` stands in for the Telegram HTTP
session, so no test can reach the network even if it wants to.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.dispatcher.middlewares.user_context import (
    EVENT_CONTEXT_KEY,
    UserContextMiddleware,
)
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramConflictError
from aiogram.fsm.storage.base import (
    DEFAULT_DESTINY,
    BaseStorage,
    StateType,
    StorageKey,
)
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatJoinRequest,
    ChatMemberMember,
    ChatMemberUpdated,
    ChosenInlineResult,
    InlineQuery,
    Message,
    MessageReactionUpdated,
    PollAnswer,
    PreCheckoutQuery,
    ShippingAddress,
    ShippingQuery,
    Update,
    User,
)

from ctb.bot import app as bot_app
from ctb.bot.app import (
    BotApp,
    ConflictGuard,
    PostgresStorage,
    build_app,
    clear_routers,
    create_bot,
    install_middleware,
    register_router,
    registered_routers,
    run_polling,
)
from ctb.bot.keyboards import (
    CONTROL_TTL_S,
    NONCE_TTL_S,
    Action,
    Cb,
    NonceError,
    NonceStore,
    button,
    choice_keyboard,
    confirm_keyboard,
    confirm_label,
    keyboard,
    parse,
    reset_nonce_store,
    resolve,
    status_card_keyboard,
    url_button,
)
from ctb.bot.middleware import (
    LogContextMiddleware,
    Route,
    RoutingMiddleware,
    StrangerNotifier,
    TenantMiddleware,
)
from ctb.conductor.pool import MissingKeyError
from ctb.db.repo import tenancy as tenancy_repo
from ctb.bot.middleware.context import new_request_id
from ctb.db.connection import Database
from ctb.db.errors import DatabaseError
from ctb.db.repo import chats as chats_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import workspaces as workspaces_repo
from ctb.settings import Settings
from ctb.turn.state import CardButton
from tests.conftest import FAKE_BOT_TOKEN, FakeClock
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID

OWNER_ID = 1001
ALLOWED_ID = 1002
STRANGER_ID = 5555
GROUP_ID = -1002000000000
DM_ID = 1001
TOPIC_ID = 77

NOW = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.UTC)


# =============================================================================
# Offline Telegram plumbing
# =============================================================================


class RecordingSession(BaseSession):
    """A Bot session that records calls and never opens a socket."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.raises: Exception | None = None

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        if self.raises is not None:
            raise self.raises
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:  # pragma: no cover - never used
        yield b""

    def sent_texts(self) -> list[str]:
        return [
            str(getattr(call, "text", ""))
            for call in self.calls
            if hasattr(call, "text")
        ]


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token=FAKE_BOT_TOKEN, session=session)


@pytest.fixture
def bot_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        platform_admin_ids=f"{OWNER_ID}",
    )


@pytest.fixture(autouse=True)
def _clean_registries() -> Any:
    clear_routers()
    reset_nonce_store()
    yield
    clear_routers()
    reset_nonce_store()


# -- update builders -----------------------------------------------------------


def _user(user_id: int, username: str | None = "stranger") -> User:
    return User(id=user_id, is_bot=False, first_name="T", username=username)


def _chat(chat_id: int = GROUP_ID, chat_type: str = "supergroup") -> Chat:
    return Chat(id=chat_id, type=chat_type)


def _message(
    user_id: int,
    *,
    chat_id: int = GROUP_ID,
    chat_type: str = "supergroup",
    thread_id: int | None = None,
    text: str = "hello",
    message_id: int = 10,
    reply_to: int | None = None,
) -> Message:
    return Message(
        message_id=message_id,
        date=NOW,
        chat=_chat(chat_id, chat_type),
        from_user=_user(user_id),
        text=text,
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
        reply_to_message=(
            None
            if reply_to is None
            else Message(
                message_id=reply_to,
                date=NOW,
                chat=_chat(chat_id, chat_type),
            )
        ),
    )


def _member(user_id: int) -> ChatMemberMember:
    return ChatMemberMember(status=ChatMemberStatus.MEMBER, user=_user(user_id))


def build_update(kind: str, user_id: int, update_id: int = 1) -> Update:
    """One Update per update type that carries a sender."""
    user = _user(user_id)
    match kind:
        case "message":
            return Update(update_id=update_id, message=_message(user_id))
        case "edited_message":
            return Update(update_id=update_id, edited_message=_message(user_id))
        case "business_message":
            return Update(update_id=update_id, business_message=_message(user_id))
        case "callback_query":
            return Update(
                update_id=update_id,
                callback_query=CallbackQuery(
                    id="cb-1",
                    from_user=user,
                    chat_instance="ci-1",
                    message=_message(user_id, message_id=11),
                    data="ctb:stop:abc",
                ),
            )
        case "inline_query":
            return Update(
                update_id=update_id,
                inline_query=InlineQuery(
                    id="iq-1", from_user=user, query="q", offset=""
                ),
            )
        case "chosen_inline_result":
            return Update(
                update_id=update_id,
                chosen_inline_result=ChosenInlineResult(
                    result_id="r-1", from_user=user, query="q"
                ),
            )
        case "shipping_query":
            return Update(
                update_id=update_id,
                shipping_query=ShippingQuery(
                    id="sq-1",
                    from_user=user,
                    invoice_payload="p",
                    shipping_address=ShippingAddress(
                        country_code="US",
                        state="CA",
                        city="SF",
                        street_line1="1 Main",
                        street_line2="",
                        post_code="94101",
                    ),
                ),
            )
        case "pre_checkout_query":
            return Update(
                update_id=update_id,
                pre_checkout_query=PreCheckoutQuery(
                    id="pq-1",
                    from_user=user,
                    currency="USD",
                    total_amount=100,
                    invoice_payload="p",
                ),
            )
        case "poll_answer":
            return Update(
                update_id=update_id,
                poll_answer=PollAnswer(
                    poll_id="p-1",
                    option_ids=[0],
                    option_persistent_ids=["0"],
                    user=user,
                ),
            )
        case "my_chat_member":
            return Update(
                update_id=update_id,
                my_chat_member=ChatMemberUpdated(
                    chat=_chat(),
                    from_user=user,
                    date=NOW,
                    old_chat_member=_member(user_id),
                    new_chat_member=_member(user_id),
                ),
            )
        case "chat_member":
            return Update(
                update_id=update_id,
                chat_member=ChatMemberUpdated(
                    chat=_chat(),
                    from_user=user,
                    date=NOW,
                    old_chat_member=_member(user_id),
                    new_chat_member=_member(user_id),
                ),
            )
        case "chat_join_request":
            return Update(
                update_id=update_id,
                chat_join_request=ChatJoinRequest(
                    chat=_chat(),
                    from_user=user,
                    user_chat_id=user_id,
                    date=NOW,
                ),
            )
        case "message_reaction":
            return Update(
                update_id=update_id,
                message_reaction=MessageReactionUpdated(
                    chat=_chat(),
                    message_id=10,
                    date=NOW,
                    old_reaction=[],
                    new_reaction=[],
                    user=user,
                ),
            )
    raise AssertionError(f"unknown update kind {kind!r}")


#: Every update type that carries a sender — i.e. every one the allow-list must
#: cover. A new Bot API update type added to this list must pass unchanged.
UPDATE_KINDS = (
    "message",
    "edited_message",
    "business_message",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "shipping_query",
    "pre_checkout_query",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
    "message_reaction",
)


class NullPool:
    """A :class:`ClientPool` stand-in. Tenancy resolves without a real key."""

    def peek(self, tenant_id: Any) -> None:
        return None

    async def get(self, tenant: Any) -> Any:
        raise MissingKeyError("no key in this test")


def make_tenancy(
    settings: Settings,
    system_db: Database,
    **overrides: Any,
) -> TenantMiddleware:
    options: dict[str, Any] = {
        "system_db": system_db,
        "clients": NullPool(),
        "settings": settings,
        "notifier": StrangerNotifier(enabled=False),
        # Resolution is cached for 30s in production; a test that changes
        # membership must see it immediately.
        "cache_ttl_s": 0.0,
    }
    options.update(overrides)
    return TenantMiddleware(**options)


def make_dispatcher(
    settings: Settings,
    db: Database | None,
    *,
    system_db: Database | None = None,
    auth: TenantMiddleware | None = None,
    storage: BaseStorage | None = None,
) -> tuple[Dispatcher, list[Any]]:
    """A Dispatcher with the production middleware stack and a catch-all."""
    resolved_system = system_db if system_db is not None else db
    assert resolved_system is not None
    dispatcher = Dispatcher(
        storage=storage or PostgresStorage(db), settings=settings, db=db
    )
    install_middleware(
        dispatcher,
        settings=settings,
        system_db=resolved_system,
        clients=NullPool(),
        db=db,
        tenancy=auth or make_tenancy(settings, resolved_system),
    )

    seen: list[Any] = []

    async def catch_all(event: Any, **_: Any) -> Any:
        seen.append(event)
        return None

    router = Router(name="recorder")
    for kind in UPDATE_KINDS:
        router.observers[kind].register(catch_all)
    dispatcher.include_router(router)
    return dispatcher, seen


# =============================================================================
# Tenancy — the security boundary
# =============================================================================


@pytest.fixture
async def seated(system_db: Database) -> None:
    """The bootstrap tenant, with the test group bound and two members seated.

    This is the shape of a real workspace: one supergroup, one Conductor
    organisation, several humans.
    """
    await tenancy_repo.bind_chat(
        system_db, GROUP_ID, BOOTSTRAP_TENANT_ID, is_primary=True, bound_by=OWNER_ID
    )
    await tenancy_repo.add_member(
        system_db, BOOTSTRAP_TENANT_ID, OWNER_ID, role="owner")
    await tenancy_repo.add_member(
        system_db, BOOTSTRAP_TENANT_ID, ALLOWED_ID, role="member")


@pytest.mark.parametrize("kind", UPDATE_KINDS)
async def test_a_stranger_gets_silence_on_every_update_type(
    kind: str,
    bot: Bot,
    session: RecordingSession,
    bot_settings: Settings,
    db: Database,
    system_db: Database,
    seated: None,
) -> None:
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    await dispatcher.feed_update(bot, build_update(kind, STRANGER_ID))
    assert seen == []
    # Silence means silence: not one Telegram API call was made.
    assert session.calls == []


@pytest.mark.parametrize("kind", UPDATE_KINDS)
async def test_a_member_passes_on_every_update_type(
    kind: str, bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    await dispatcher.feed_update(bot, build_update(kind, ALLOWED_ID))
    assert len(seen) == 1


async def test_an_unbound_group_is_refused_even_for_a_member(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    """Anyone can add a shared bot to a group. Being added is not consent."""
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    update = Update(update_id=1, message=_message(ALLOWED_ID, chat_id=GROUP_ID - 1))

    await dispatcher.feed_update(bot, update)

    assert seen == []


async def test_a_stranger_in_a_bound_group_is_still_refused(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    """Membership is checked separately from the chat.

    Being in the right supergroup is not authorisation; a customer's colleague
    who was never invited to the workspace gets nothing.
    """
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    await dispatcher.feed_update(bot, build_update("message", STRANGER_ID))
    assert seen == []


async def test_a_members_dm_resolves_to_their_only_workspace(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    update = Update(
        update_id=1,
        message=_message(ALLOWED_ID, chat_id=ALLOWED_ID, chat_type="private"),
    )

    await dispatcher.feed_update(bot, update)

    assert len(seen) == 1


async def test_a_dm_from_someone_in_two_workspaces_needs_an_explicit_binding(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    """A prompt must never silently land in the wrong organisation."""
    await tenancy_repo.add_member(
        system_db, OTHER_TENANT_ID, ALLOWED_ID, role="member")
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    update = Update(
        update_id=1,
        message=_message(ALLOWED_ID, chat_id=ALLOWED_ID, chat_type="private"),
    )

    await dispatcher.feed_update(bot, update)

    assert seen == []


async def test_a_channel_post_without_a_sender_is_dropped(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    """No ``from_user`` means nobody to authorise — fail closed."""
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    update = Update(
        update_id=1,
        channel_post=Message(
            message_id=1, date=NOW, chat=_chat(-100999, "channel"), text="hi"
        ),
    )
    await dispatcher.feed_update(bot, update)
    assert seen == []


async def test_inviting_someone_lets_them_in(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    """``/invite`` writes a row; this is the whole co-founder story."""
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    await dispatcher.feed_update(bot, build_update("message", 4242))
    assert seen == []

    await tenancy_repo.add_member(
        system_db, BOOTSTRAP_TENANT_ID, 4242, role="member")
    await dispatcher.feed_update(bot, build_update("message", 4242, update_id=2))
    assert len(seen) == 1


async def test_removing_someone_locks_them_out(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    await dispatcher.feed_update(bot, build_update("message", ALLOWED_ID))
    assert len(seen) == 1

    await tenancy_repo.remove_member(
        system_db, BOOTSTRAP_TENANT_ID, ALLOWED_ID)
    await dispatcher.feed_update(bot, build_update("message", ALLOWED_ID, update_id=2))
    assert len(seen) == 1


async def test_a_suspended_workspace_stops_answering(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    await tenancy_repo.set_status(
        system_db, BOOTSTRAP_TENANT_ID, "suspended")
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    await dispatcher.feed_update(bot, build_update("message", ALLOWED_ID))
    assert seen == []


async def test_a_database_failure_fails_closed(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    """A broken lookup must not open the door."""

    class Broken(Database):
        async def fetch_one(self, sql: str, params: Any = ()) -> Any:
            raise DatabaseError("connection reset")

    broken = Broken(system_db.dsn, system=True)
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=broken)
    await dispatcher.feed_update(bot, build_update("message", ALLOWED_ID))
    assert seen == []


async def test_registration_commands_reach_a_handler_without_a_tenant(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database
) -> None:
    """``/start`` in a DM is how someone becomes a tenant in the first place."""
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    update = Update(
        update_id=1,
        message=_message(
            9999, chat_id=9999, chat_type="private", text="/start"
        ),
    )

    await dispatcher.feed_update(bot, update)

    assert len(seen) == 1


async def test_registration_commands_do_not_open_a_group(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database
) -> None:
    """The entry point is a private message, never someone else's supergroup."""
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    update = Update(update_id=1, message=_message(9999, text="/start"))

    await dispatcher.feed_update(bot, update)

    assert seen == []


async def test_ordinary_text_from_a_stranger_is_still_silence(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database
) -> None:
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db)
    update = Update(
        update_id=1,
        message=_message(9999, chat_id=9999, chat_type="private", text="hello"),
    )

    await dispatcher.feed_update(bot, update)

    assert seen == []


async def test_the_tenant_context_reaches_the_handler(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    dispatcher = Dispatcher(storage=PostgresStorage(db), settings=bot_settings, db=db)
    dispatcher.update.outer_middleware(make_tenancy(bot_settings, system_db))
    captured: list[dict[str, Any]] = []

    async def handler(event: Any, **data: Any) -> None:
        captured.append(data)

    router = Router(name="capture")
    router.message.register(handler)
    dispatcher.include_router(router)

    await dispatcher.feed_update(bot, build_update("message", OWNER_ID))
    await dispatcher.feed_update(bot, build_update("message", ALLOWED_ID, update_id=2))

    assert [data["tenant"].role for data in captured] == ["owner", "member"]
    assert [data["is_owner"] for data in captured] == [True, False]
    assert {data["tenant"].tenant_id for data in captured} == {BOOTSTRAP_TENANT_ID}


async def test_owners_are_told_about_a_stranger_once_per_day(
    bot: Bot, session: RecordingSession, bot_settings: Settings, db: Database,
    system_db: Database, seated: None,
) -> None:
    clock = FakeClock()
    notifier = StrangerNotifier(clock=clock)
    auth = make_tenancy(bot_settings, system_db, notifier=notifier)
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db, auth=auth)

    for index in range(5):
        await dispatcher.feed_update(
            bot, build_update("message", STRANGER_ID, index + 1)
        )
        clock.advance(60)
    assert seen == []
    assert notifier.sent == 1


async def test_a_different_stranger_gets_their_own_notice(
    bot: Bot, session: RecordingSession, bot_settings: Settings, db: Database,
    system_db: Database, seated: None,
) -> None:
    notifier = StrangerNotifier(clock=FakeClock())
    auth = make_tenancy(bot_settings, system_db, notifier=notifier)
    dispatcher, _ = make_dispatcher(bot_settings, db, system_db=system_db, auth=auth)

    await dispatcher.feed_update(bot, build_update("message", STRANGER_ID, 1))
    await dispatcher.feed_update(bot, build_update("message", STRANGER_ID + 1, 2))

    assert notifier.sent == 2


async def test_a_flood_of_strangers_cannot_flood_the_owners(
    bot: Bot, session: RecordingSession, bot_settings: Settings, db: Database,
    system_db: Database, seated: None,
) -> None:
    """"Once per stranger" without a cap is a spam amplifier, not a guard."""
    notifier = StrangerNotifier(clock=FakeClock(), max_per_window=3)
    auth = make_tenancy(bot_settings, system_db, notifier=notifier)
    dispatcher, _ = make_dispatcher(bot_settings, db, system_db=system_db, auth=auth)

    for index in range(20):
        await dispatcher.feed_update(
            bot, build_update("message", 90_000 + index, index + 1)
        )

    assert notifier.sent == 3


async def test_a_failing_notice_does_not_break_the_rejection_path(
    bot: Bot, session: RecordingSession, bot_settings: Settings, db: Database,
    system_db: Database, seated: None,
) -> None:
    session.raises = RuntimeError("blocked by user")
    notifier = StrangerNotifier(clock=FakeClock())
    auth = make_tenancy(bot_settings, system_db, notifier=notifier)
    dispatcher, seen = make_dispatcher(bot_settings, db, system_db=system_db, auth=auth)

    await dispatcher.feed_update(bot, build_update("message", STRANGER_ID))

    assert seen == []
    assert notifier.sent == 0


class SpyStorage(PostgresStorage):
    """Counts reads, to prove a stranger never reaches the storage."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.reads = 0

    async def get_state(self, key: StorageKey) -> str | None:
        self.reads += 1
        return await super().get_state(key)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        await super().set_state(key, state)


async def test_a_stranger_never_touches_the_fsm_storage(
    bot: Bot,
    bot_settings: Settings,
    db: Database,
    system_db: Database,
    seated: None,
) -> None:
    """aiogram's FSM middleware reads storage per update; it must run after us."""
    spy = SpyStorage(db)
    dispatcher, seen = make_dispatcher(
        bot_settings, db, system_db=system_db, storage=spy
    )
    await dispatcher.feed_update(bot, build_update("message", STRANGER_ID))
    assert seen == []
    assert spy.reads == 0

    await dispatcher.feed_update(bot, build_update("message", OWNER_ID, update_id=2))
    assert len(seen) == 1
    assert spy.reads == 1


async def test_the_principal_reflects_the_members_role(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    """``is_owner`` is a role on ``tenant_members``, not a position in a list."""
    dispatcher = Dispatcher(storage=PostgresStorage(db), settings=bot_settings, db=db)
    dispatcher.update.outer_middleware(make_tenancy(bot_settings, system_db))
    captured: list[dict[str, Any]] = []

    async def handler(event: Any, **data: Any) -> None:
        captured.append(data)

    router = Router(name="principal")
    router.message.register(handler)
    dispatcher.include_router(router)

    await dispatcher.feed_update(bot, build_update("message", OWNER_ID))
    await dispatcher.feed_update(bot, build_update("message", ALLOWED_ID, update_id=2))

    assert [data["principal"].is_owner for data in captured] == [True, False]
    assert all(data["principal"].source == "member" for data in captured)


async def test_an_admin_also_passes_the_owner_gate(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database, seated: None
) -> None:
    await tenancy_repo.add_member(
        system_db, BOOTSTRAP_TENANT_ID, ALLOWED_ID, role="admin"
    )
    dispatcher = Dispatcher(storage=PostgresStorage(db), settings=bot_settings, db=db)
    dispatcher.update.outer_middleware(make_tenancy(bot_settings, system_db))
    captured: list[dict[str, Any]] = []

    async def handler(event: Any, **data: Any) -> None:
        captured.append(data)

    router = Router(name="admin")
    router.message.register(handler)
    dispatcher.include_router(router)

    await dispatcher.feed_update(bot, build_update("message", ALLOWED_ID))

    assert captured[0]["is_owner"] is True


# =============================================================================
# Routing
# =============================================================================


async def seed_binding(
    db: Database,
    *,
    chat_id: int,
    thread_id: int,
    session_id: str,
    workspace_id: str = "ws-1",
    kind: str = "topic",
) -> None:
    await workspaces_repo.upsert(db, workspace_id, name="api/fix-flaky")
    await sessions_repo.upsert(
        db,
        session_id,
        workspace_id=workspace_id,
        chat_id=chat_id,
        thread_id=thread_id,
        is_bound=True,
    )
    await chats_repo.bind(
        db,
        chat_id,
        thread_id,
        workspace_id=workspace_id,
        session_id=session_id,
        kind=kind,
    )


async def route_for(
    middleware: RoutingMiddleware, update: Update, db: Database
) -> Route:
    data: dict[str, Any] = {
        EVENT_CONTEXT_KEY: UserContextMiddleware.resolve_event_context(update),
        "db": db,
        # Routing runs behind tenancy in production; without a tenant there is
        # no scope, and it deliberately resolves nothing.
        "tenant": SimpleNamespace(
            tenant_id=BOOTSTRAP_TENANT_ID,
            settings=SimpleNamespace(voice_enabled=False),
        ),
    }
    captured: dict[str, Route] = {}

    async def handler(event: Any, payload: dict[str, Any]) -> None:
        captured["route"] = payload["route"]

    await middleware(handler, update, data)
    return captured["route"]


async def test_topic_routes_to_its_session(db: Database) -> None:
    await seed_binding(
        db, chat_id=GROUP_ID, thread_id=TOPIC_ID, session_id="sess-topic"
    )
    update = Update(
        update_id=1, message=_message(OWNER_ID, thread_id=TOPIC_ID, text="go")
    )
    route = await route_for(RoutingMiddleware(db=db), update, db)
    assert route.key == (GROUP_ID, TOPIC_ID)
    assert route.session_id == "sess-topic"
    assert route.workspace_id == "ws-1"
    assert route.is_topic and route.is_bound
    assert not route.is_general and not route.is_dm


async def test_general_is_thread_zero_and_is_not_a_topic(db: Database) -> None:
    await seed_binding(
        db, chat_id=GROUP_ID, thread_id=TOPIC_ID, session_id="sess-topic"
    )
    update = Update(update_id=1, message=_message(OWNER_ID, thread_id=None))
    route = await route_for(RoutingMiddleware(db=db), update, db)
    assert route.key == (GROUP_ID, 0)
    assert route.is_general
    # General is not bound to the topic's session — the cockpit never prompts.
    assert route.session_id is None


async def test_dm_falls_out_as_thread_zero(db: Database) -> None:
    """The NULL/DM case: a private chat has no thread, so ``thread_id == 0``."""
    await seed_binding(
        db,
        chat_id=DM_ID,
        thread_id=0,
        session_id="sess-dm",
        workspace_id="ws-dm",
        kind="dm",
    )
    update = Update(
        update_id=1,
        message=_message(OWNER_ID, chat_id=DM_ID, chat_type="private", thread_id=None),
    )
    route = await route_for(RoutingMiddleware(db=db), update, db)
    assert route.key == (DM_ID, 0)
    assert route.is_dm
    assert route.session_id == "sess-dm"


async def test_two_topics_in_one_chat_do_not_cross(db: Database) -> None:
    await seed_binding(db, chat_id=GROUP_ID, thread_id=11, session_id="sess-a")
    await seed_binding(
        db,
        chat_id=GROUP_ID,
        thread_id=22,
        session_id="sess-b",
        workspace_id="ws-2",
    )
    middleware = RoutingMiddleware(db=db)
    a = await route_for(
        middleware, Update(update_id=1, message=_message(OWNER_ID, thread_id=11)), db
    )
    b = await route_for(
        middleware, Update(update_id=2, message=_message(OWNER_ID, thread_id=22)), db
    )
    assert (a.session_id, b.session_id) == ("sess-a", "sess-b")


async def test_unbound_topic_resolves_to_no_session(db: Database) -> None:
    update = Update(update_id=1, message=_message(OWNER_ID, thread_id=999))
    route = await route_for(RoutingMiddleware(db=db), update, db)
    assert route.session_id is None
    assert not route.is_bound


async def test_session_bound_without_a_chat_row_is_still_found(db: Database) -> None:
    await workspaces_repo.upsert(db, "ws-9", name="w")
    await sessions_repo.upsert(
        db,
        "sess-orphan",
        workspace_id="ws-9",
        chat_id=GROUP_ID,
        thread_id=33,
        is_bound=True,
    )
    update = Update(update_id=1, message=_message(OWNER_ID, thread_id=33))
    route = await route_for(RoutingMiddleware(db=db), update, db)
    assert route.session_id == "sess-orphan"
    assert route.workspace_id == "ws-9"


async def test_reply_to_override_uses_the_injected_resolver(db: Database) -> None:
    await seed_binding(
        db, chat_id=GROUP_ID, thread_id=TOPIC_ID, session_id="sess-topic"
    )
    await workspaces_repo.upsert(db, "ws-other", name="other")
    await sessions_repo.upsert(db, "sess-other", workspace_id="ws-other")

    async def resolver(_db: Database, _chat_id: int, message_id: int) -> str | None:
        return "sess-other" if message_id == 4321 else None

    middleware = RoutingMiddleware(db=db, reply_resolver=resolver)
    update = Update(
        update_id=1,
        message=_message(OWNER_ID, thread_id=TOPIC_ID, reply_to=4321),
    )
    route = await route_for(middleware, update, db)
    assert route.reply_to_message_id == 4321
    assert route.session_id == "sess-other"
    assert route.via_reply


async def test_routing_survives_a_broken_database(db: Database) -> None:
    class Broken(Database):
        async def fetch_one(self, sql: str, params: Any = ()) -> Any:
            raise DatabaseError("connection reset")

    db = Broken(db.dsn)
    update = Update(update_id=1, message=_message(OWNER_ID, thread_id=TOPIC_ID))
    route = await route_for(RoutingMiddleware(db=db), update, db)
    assert route.session_id is None
    assert route.key == (GROUP_ID, TOPIC_ID)


# =============================================================================
# Log context
# =============================================================================


async def test_log_context_binds_and_unbinds() -> None:
    import structlog

    middleware = LogContextMiddleware(request_id_factory=lambda: "rid-1")
    update = Update(update_id=42, message=_message(OWNER_ID, thread_id=TOPIC_ID))
    data: dict[str, Any] = {
        EVENT_CONTEXT_KEY: UserContextMiddleware.resolve_event_context(update)
    }
    inside: dict[str, Any] = {}

    async def handler(event: Any, payload: dict[str, Any]) -> None:
        inside.update(structlog.contextvars.get_contextvars())

    await middleware(handler, update, data)
    assert inside["request_id"] == "rid-1"
    assert inside["chat_id"] == GROUP_ID
    assert inside["thread_id"] == TOPIC_ID
    assert inside["update_id"] == 42
    assert data["request_id"] == "rid-1"
    assert "request_id" not in structlog.contextvars.get_contextvars()


def test_request_ids_are_distinct() -> None:
    assert len({new_request_id() for _ in range(100)}) == 100


# =============================================================================
# Nonces and keyboards
# =============================================================================


def test_nonce_is_single_use() -> None:
    store = NonceStore()
    ticket = store.issue(Action.ARCHIVE, "ws-1", user_id=OWNER_ID)
    assert store.consume(ticket.nonce, user_id=OWNER_ID).target == "ws-1"
    with pytest.raises(NonceError) as exc:
        store.consume(ticket.nonce, user_id=OWNER_ID)
    assert exc.value.reason == "used"
    assert "Already done" in exc.value.user_message


def test_nonce_expires_at_sixty_seconds() -> None:
    clock = FakeClock()
    store = NonceStore(clock=clock)
    ticket = store.issue(Action.ARCHIVE, "ws-1")
    clock.advance(NONCE_TTL_S - 0.01)
    assert store.peek(ticket.nonce) is not None
    clock.advance(0.02)
    assert store.peek(ticket.nonce) is None
    with pytest.raises(NonceError) as exc:
        store.consume(ticket.nonce)
    assert exc.value.reason == "expired"
    assert "expired" in exc.value.user_message


def test_a_button_tapped_tomorrow_says_expired() -> None:
    clock = FakeClock()
    store = NonceStore(clock=clock)
    markup = confirm_keyboard(
        Action.ARCHIVE, "ws-1", "api/fix-flaky", verb="Archive", store=store
    )
    payload = markup.inline_keyboard[0][0].callback_data
    assert payload is not None
    clock.advance(24 * 3600)
    query = CallbackQuery(
        id="cb", from_user=_user(OWNER_ID), chat_instance="ci", data=payload
    )
    with pytest.raises(NonceError) as exc:
        resolve(query, store=store)
    assert exc.value.reason == "expired"


def test_action_mismatch_is_rejected() -> None:
    store = NonceStore()
    ticket = store.issue(Action.STOP, "sess-1")
    with pytest.raises(NonceError) as exc:
        store.consume(ticket.nonce, action=Action.ARCHIVE)
    assert exc.value.reason == "mismatch"
    # ...and the ticket is NOT spent by a failed attempt.
    assert store.consume(ticket.nonce, action=Action.STOP).target == "sess-1"


def test_another_user_cannot_tap_your_button() -> None:
    store = NonceStore()
    ticket = store.issue(Action.ARCHIVE, "ws-1", user_id=OWNER_ID)
    with pytest.raises(NonceError) as exc:
        store.consume(ticket.nonce, user_id=ALLOWED_ID)
    assert exc.value.reason == "wrong_user"


def test_unknown_and_malformed_payloads_are_rejected() -> None:
    store = NonceStore()
    query = CallbackQuery(
        id="cb", from_user=_user(OWNER_ID), chat_instance="ci", data="garbage"
    )
    with pytest.raises(NonceError) as exc:
        resolve(query, store=store)
    assert exc.value.reason == "malformed"

    forged = Cb(action="archive", nonce="not-a-real-nonce").pack()
    query = CallbackQuery(
        id="cb", from_user=_user(OWNER_ID), chat_instance="ci", data=forged
    )
    with pytest.raises(NonceError) as exc:
        resolve(query, store=store)
    assert exc.value.reason == "unknown"


def test_resolve_round_trips_a_real_button() -> None:
    store = NonceStore()
    markup = keyboard(
        [[button("⏹ Stop", Action.STOP, "sess-1", store=store, user_id=OWNER_ID)]]
    )
    payload = markup.inline_keyboard[0][0].callback_data
    assert payload is not None
    query = CallbackQuery(
        id="cb", from_user=_user(OWNER_ID), chat_instance="ci", data=payload
    )
    ticket = resolve(query, expect=Action.STOP, store=store)
    assert ticket.target == "sess-1"
    assert ticket.label == "⏹ Stop"


def test_callback_payloads_fit_telegrams_64_byte_budget() -> None:
    store = NonceStore()
    for action in Action:
        ticket = store.issue(action, "0d1e6f45-8a11-4b7c-9c3e-2f5a6b7c8d9e")
        assert len(ticket.callback_data.encode()) <= 64


def test_confirm_button_contains_the_name() -> None:
    store = NonceStore()
    markup = confirm_keyboard(
        Action.ARCHIVE, "ws-1", "api/fix-flaky", verb="Archive", store=store
    )
    assert markup.inline_keyboard[0][0].text == "Archive api/fix-flaky"
    assert markup.inline_keyboard[1][0].text == "Cancel"


def test_a_very_long_name_still_shows_its_distinguishing_tail() -> None:
    label = confirm_label("Archive", "monorepo/" + "x" * 80 + "/feature-branch")
    assert label.startswith("Archive ")
    assert label.endswith("feature-branch")
    assert len(label) <= 48 + len("Archive ")


def test_status_card_keyboard_maps_card_buttons() -> None:
    store = NonceStore()
    markup = status_card_keyboard(
        [CardButton.STOP, CardButton.OPEN],
        "sess-1",
        deep_link="https://app.conductor.build/w/1",
        store=store,
    )
    assert markup is not None
    flat = [b for row in markup.inline_keyboard for b in row]
    assert flat[0].callback_data is not None and flat[0].url is None
    assert flat[1].url == "https://app.conductor.build/w/1"
    assert flat[1].callback_data is None


def test_open_is_dropped_without_a_deep_link() -> None:
    store = NonceStore()
    markup = status_card_keyboard([CardButton.OPEN], "sess-1", store=store)
    assert markup is None


def test_status_card_archive_requires_confirmation_request() -> None:
    store = NonceStore()
    markup = status_card_keyboard([CardButton.ARCHIVE], "sess-1", store=store)
    assert markup is not None
    packed = markup.inline_keyboard[0][0].callback_data
    parsed = parse(packed)
    ticket = store.consume(parsed.nonce, action=Action.ARCHIVE_REQUEST)
    assert ticket.target == "sess-1"


def test_status_controls_survive_a_normal_phone_interruption() -> None:
    clock = FakeClock()
    store = NonceStore(clock=clock)
    markup = status_card_keyboard([CardButton.STOP], "sess-1", store=store)
    assert markup is not None
    packed = markup.inline_keyboard[0][0].callback_data
    parsed = parse(packed)

    clock.advance(NONCE_TTL_S + 1)
    assert store.peek(parsed.nonce) is not None
    clock.advance(CONTROL_TTL_S)
    assert store.peek(parsed.nonce) is None


def test_destructive_buttons_are_visually_distinct() -> None:
    store = NonceStore()
    markup = confirm_keyboard(
        Action.ARCHIVE, "ws-1", "api/fix", verb="Archive", store=store
    )
    assert markup.inline_keyboard[0][0].style == "danger"
    assert button("Stop", Action.STOP, "s-1", store=store).style == "danger"
    assert url_button("Open", "https://example.test").style == "primary"


def test_choice_keyboard_lays_out_columns() -> None:
    store = NonceStore()
    markup = choice_keyboard(
        [(f"opt{i}", f"t{i}") for i in range(5)],
        Action.PICK,
        columns=2,
        store=store,
        extra=[url_button("Docs", "https://example.com")],
    )
    assert [len(row) for row in markup.inline_keyboard] == [2, 2, 1, 1]


def test_revoke_target_kills_every_live_button_for_a_thing() -> None:
    store = NonceStore()
    a = store.issue(Action.STOP, "sess-1")
    b = store.issue(Action.RETRY, "sess-1")
    c = store.issue(Action.STOP, "sess-2")
    assert store.revoke_target("sess-1") == 2
    assert store.peek(a.nonce) is None and store.peek(b.nonce) is None
    assert store.peek(c.nonce) is not None


def test_expired_tickets_are_purged() -> None:
    clock = FakeClock()
    store = NonceStore(clock=clock)
    for i in range(200):
        store.issue(Action.PICK, f"t{i}")
    clock.advance(NONCE_TTL_S + 1)
    store.issue(Action.PICK, "fresh")
    assert len(store) == 1


def test_an_invalid_action_is_a_programming_error() -> None:
    store = NonceStore()
    with pytest.raises(ValueError):
        store.issue("Archive Workspace!", "ws-1")


# =============================================================================
# DB-backed FSM storage
# =============================================================================


def storage_key(
    *, chat_id: int = GROUP_ID, thread_id: int = TOPIC_ID, user_id: int = OWNER_ID
) -> StorageKey:
    return StorageKey(bot_id=1, chat_id=chat_id, user_id=user_id, thread_id=thread_id)


async def test_fsm_state_survives_a_restart(db: Database) -> None:
    key = storage_key()
    storage = PostgresStorage(db)
    await storage.set_state(key, "NewWorkspace:branch")
    await storage.set_data(key, {"project": "api", "prompt": "fix the test"})

    # A brand-new storage object over the same database is what a redeploy
    # looks like from the wizard's point of view.
    restarted = PostgresStorage(db)
    assert await restarted.get_state(key) == "NewWorkspace:branch"
    assert await restarted.get_data(key) == {
        "project": "api",
        "prompt": "fix the test",
    }


async def test_fsm_seats_are_isolated_by_chat_thread_and_user(db: Database) -> None:
    storage = PostgresStorage(db)
    a = storage_key(thread_id=1)
    b = storage_key(thread_id=2)
    c = storage_key(thread_id=1, user_id=ALLOWED_ID)
    await storage.set_state(a, "A")
    await storage.set_state(b, "B")
    await storage.set_state(c, "C")
    assert await storage.get_state(a) == "A"
    assert await storage.get_state(b) == "B"
    assert await storage.get_state(c) == "C"


async def test_fsm_dm_key_has_no_thread(db: Database) -> None:
    storage = PostgresStorage(db)
    key = StorageKey(bot_id=1, chat_id=DM_ID, user_id=OWNER_ID, thread_id=None)
    await storage.set_state(key, "wizard")
    assert await storage.get_state(key) == "wizard"
    row = await db.fetch_one(
        "SELECT thread_id FROM wizard_state WHERE chat_id = ?", (DM_ID,)
    )
    assert row is not None and row["thread_id"] == 0


async def test_fsm_clearing_removes_the_row(db: Database) -> None:
    storage = PostgresStorage(db)
    key = storage_key()
    await storage.set_state(key, "step")
    await storage.set_data(key, {"x": 1})
    await storage.set_state(key, None)
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {"x": 1}  # clearing state keeps data
    await storage.set_data(key, {})
    assert await storage.get_data(key) == {}
    assert (await db.fetch_val("SELECT COUNT(*) FROM wizard_state", default=0)) == 0


async def test_fsm_update_data_merges(db: Database) -> None:
    storage = PostgresStorage(db)
    key = storage_key()
    await storage.set_data(key, {"a": 1})
    merged = await storage.update_data(key, {"b": 2})
    assert merged == {"a": 1, "b": 2}
    assert await storage.get_data(key) == {"a": 1, "b": 2}


async def test_fsm_non_default_destiny_is_namespaced(db: Database) -> None:
    storage = PostgresStorage(db)
    plain = storage_key()
    scene = StorageKey(
        bot_id=1,
        chat_id=GROUP_ID,
        user_id=OWNER_ID,
        thread_id=TOPIC_ID,
        destiny="scene",
    )
    await storage.set_state(plain, "plain")
    await storage.set_data(plain, {"k": "plain"})
    await storage.set_state(scene, "scene-state")
    await storage.set_data(scene, {"k": "scene"})

    assert await storage.get_state(plain) == "plain"
    assert await storage.get_data(plain) == {"k": "plain"}
    assert await storage.get_state(scene) == "scene-state"
    assert await storage.get_data(scene) == {"k": "scene"}
    assert DEFAULT_DESTINY == "default"


async def test_fsm_expired_wizard_reads_as_absent(db: Database) -> None:
    storage = PostgresStorage(db, ttl_ms=1)
    key = storage_key()
    await storage.set_state(key, "yesterday")
    await asyncio.sleep(0.01)
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}


async def test_fsm_is_reachable_through_the_dispatcher(
    bot: Bot,
    bot_settings: Settings,
    db: Database,
    system_db: Database,
    seated: None,
) -> None:
    """Moving the FSM middleware behind tenancy must not break it."""
    dispatcher = Dispatcher(storage=PostgresStorage(db), settings=bot_settings, db=db)
    install_middleware(
        dispatcher,
        settings=bot_settings,
        system_db=system_db,
        clients=NullPool(),
        db=db,
        tenancy=make_tenancy(bot_settings, system_db),
    )
    seen: list[str | None] = []
    routes: list[Route] = []

    async def handler(message: Message, state: Any, route: Route, **_: Any) -> None:
        await state.update_data(step=1)
        await state.set_state("asked")
        seen.append(await state.get_state())
        routes.append(route)

    router = Router(name="fsm")
    router.message.register(handler)
    dispatcher.include_router(router)
    await dispatcher.feed_update(bot, build_update("message", OWNER_ID))
    assert seen == ["asked"]
    assert routes[0].chat_id == GROUP_ID
    stored = await db.fetch_val(
        "SELECT state_key FROM wizard_state WHERE user_id = ?", (OWNER_ID,)
    )
    assert stored == "asked"


# =============================================================================
# App assembly and polling
# =============================================================================


def test_routers_attach_in_order_order(bot_settings: Settings) -> None:
    late = Router(name="catch-all")
    early = Router(name="commands")
    register_router(late, order=900)
    register_router(early, order=10)
    assert [r.name for r in registered_routers()] == ["commands", "catch-all"]


def test_registering_the_same_router_twice_is_a_no_op() -> None:
    router = Router(name="once")
    register_router(router)
    register_router(router)
    assert len(registered_routers()) == 1


def test_discover_tolerates_a_missing_handler_package() -> None:
    assert bot_app.discover_routers("ctb.bot.definitely_not_here") == ()


def test_build_app_wires_everything(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database
) -> None:
    router = Router(name="x")
    app = build_app(
        settings=bot_settings,
        db=db,
        system_db=system_db,
        clients=NullPool(),
        bot=bot,
        routers=[router])
    assert app.dispatcher["settings"] is bot_settings
    assert app.dispatcher["db"] is db
    assert app.dispatcher["nonces"] is app.nonces
    assert isinstance(app.storage, PostgresStorage)
    assert app.routers == (router,)
    assert app.health()["routers"] == ["x"]


async def test_an_unexpected_handler_error_replies_without_leaking(
    bot: Bot,
    session: RecordingSession,
    bot_settings: Settings,
    db: Database, system_db: Database,
    seated: None,
) -> None:
    router = Router(name="broken")

    async def boom(_message: Message) -> None:
        raise RuntimeError("secret internal detail")

    router.message.register(boom)
    app = build_app(
        settings=bot_settings,
        db=db,
        system_db=system_db,
        clients=NullPool(),
        bot=bot,
        routers=[router])

    await app.dispatcher.feed_update(bot, build_update("message", OWNER_ID))

    assert session.sent_texts() == [
        "⚠️ Request failed · retry once. If it repeats, run /health."
    ]
    assert "secret internal detail" not in session.sent_texts()[0]


def test_created_bot_is_html_with_link_previews_off(bot_settings: Settings) -> None:
    """HTML, never MarkdownV2 (CLAUDE.md), and no link previews (PLAN §2)."""
    made = create_bot(bot_settings)
    assert made.default.parse_mode == "HTML"
    assert made.default.link_preview_is_disabled is True
    assert made.token == bot_settings.telegram_bot_token.get_secret_value()


async def test_conflict_guard_counts_and_reraises(bot: Bot) -> None:
    guard = ConflictGuard()

    async def make_request(_bot: Bot, _method: Any) -> Any:
        raise TelegramConflictError(method=None, message="terminated by other")  # type: ignore[arg-type]

    with pytest.raises(TelegramConflictError):
        await guard(make_request, bot, object())  # type: ignore[arg-type]
    assert guard.conflicts == 1


async def test_polling_retries_a_conflict_instead_of_crashing(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database
) -> None:
    """PLAN §Redeploy overlap defence #3: 409 is 'wait', never 'die'."""
    app = build_app(
        settings=bot_settings,
        db=db,
        system_db=system_db,
        clients=NullPool(),
        bot=bot,
        routers=[])
    attempts = 0

    async def fake_start_polling(*_args: Any, **_kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TelegramConflictError(method=None, message="conflict")  # type: ignore[arg-type]

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    app.dispatcher.start_polling = fake_start_polling  # type: ignore[method-assign]
    await run_polling(app, sleep=fake_sleep)
    assert attempts == 3
    assert len(slept) == 2
    assert all(5.0 <= d <= 6.0 for d in slept)


async def test_polling_backs_off_on_an_unexpected_error(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database
) -> None:
    app = build_app(
        settings=bot_settings,
        db=db,
        system_db=system_db,
        clients=NullPool(),
        bot=bot,
        routers=[])
    calls = 0

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("network gone")

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    app.dispatcher.start_polling = boom  # type: ignore[method-assign]
    await run_polling(app, sleep=fake_sleep, max_restarts=3)
    assert calls == 4
    assert slept[0] < slept[-1] <= 60.0 * 1.2


async def test_polling_propagates_cancellation(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database
) -> None:
    app = build_app(
        settings=bot_settings,
        db=db,
        system_db=system_db,
        clients=NullPool(),
        bot=bot,
        routers=[])

    async def cancelled(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    app.dispatcher.start_polling = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await run_polling(app)


async def test_app_close_closes_the_session(
    bot: Bot, bot_settings: Settings, db: Database, system_db: Database
) -> None:
    app: BotApp = build_app(
        settings=bot_settings,
        db=db,
        system_db=system_db,
        clients=NullPool(),
        bot=bot,
        routers=[])
    await app.close()
