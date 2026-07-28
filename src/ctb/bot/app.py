"""The aiogram application: Bot, Dispatcher, DB-backed FSM, polling loop.

Four things live here that nothing else should have to think about.

**Long polling, not a webhook.** One Railway service, no public URL to manage.
``getUpdates`` returning ``409 Conflict`` means another instance is still
holding the poll — during a redeploy overlap, exactly the situation
PLAN §Redeploy overlap defence #3 anticipates. It is logged and retried, never
a crash-loop, because a crash-loop is how a five-second overlap becomes an
outage.

**HTML, never MarkdownV2.** Set once as the bot's default parse mode, together
with ``link_preview_is_disabled`` (PLAN §Telegram formatting).

**FSM state in PostgreSQL.** aiogram ships ``MemoryStorage``; a redeploy in the
middle of the ``/new`` wizard would drop you back to step one.
:class:`PostgresStorage` puts it in ``wizard_state``, where it also picks up the
30-minute expiry — yesterday's half-finished wizard must not hijack today's
button press.

**A router seam.** Handler modules are written independently; see
:func:`register_router` for the convention.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Final

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import DEFAULT_DESTINY, BaseStorage, StateType, StorageKey
from aiogram.fsm.strategy import FSMStrategy
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import BotCommand, CallbackQuery, ErrorEvent, Message

from ctb.bot.keyboards import NonceStore, get_nonce_store, set_nonce_store
from ctb.bot.middleware import (
    LogContextMiddleware,
    RoutingMiddleware,
    TenantMiddleware,
)
from ctb.conductor.pool import ClientPool
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, current_tenant, get_database
from ctb.db.repo import wizard as wizard_repo
from ctb.health import telegram_health
from ctb.logging import get_logger
from ctb.settings import Settings, get_settings

__all__ = [
    "DEFAULT_HANDLER_PACKAGE",
    "MAX_POLLING_BACKOFF_S",
    "BotApp",
    "ConflictGuard",
    "RouterEntry",
    "PostgresStorage",
    "build_app",
    "clear_routers",
    "create_bot",
    "create_dispatcher",
    "discover_routers",
    "install_middleware",
    "register_router",
    "registered_routers",
    "run_polling",
    "unexpected_update_error",
]

log = get_logger(__name__)

#: Handler modules live here and are imported for their side effects.
DEFAULT_HANDLER_PACKAGE: Final = "ctb.bot.handlers"

#: Routers with no explicit order land here. Catch-alls should use a larger
#: number (``ROUTER_ORDER = 900``) so ``/command`` handlers win.
DEFAULT_ROUTER_ORDER: Final = 100

MAX_POLLING_BACKOFF_S: Final = 60.0
#: A conflict is another live instance, not an error. Wait long enough for the
#: old container to drain instead of hammering ``getUpdates``.
CONFLICT_BACKOFF_S: Final = 5.0

#: The phone menu, deliberately shorter than the command list. It is the only
#: discovery surface most people ever read, so it holds the daily loop plus the
#: three onboarding steps — not everything that exists. Rarely-used commands
#: (``/forget``, ``/revoke``, ``/tidy``) and operator-only ones (``/platform``)
#: stay out: advertising them to every customer costs more than it teaches.
BOT_COMMANDS: Final[tuple[BotCommand, ...]] = (
    BotCommand(command="new", description="New workspace"),
    BotCommand(command="board", description="Live sessions"),
    BotCommand(command="attach", description="Open laptop workspace"),
    BotCommand(command="stop", description="Stop this turn"),
    BotCommand(command="find", description="Search transcripts"),
    BotCommand(command="mode", description="Current session & controls"),
    BotCommand(command="s", description="Switch session here"),
    BotCommand(command="fork", description="New session here"),
    BotCommand(command="notify", description="Topic alerts"),
    BotCommand(command="done", description="Archive workspace"),
    BotCommand(command="setup", description="Link this group to your team"),
    BotCommand(command="invite", description="Add someone to this team"),
    BotCommand(command="use", description="Pick which team your DMs mean"),
    BotCommand(command="health", description="Team status"),
    BotCommand(command="register", description="Create your team"),
    BotCommand(command="key", description="Store your Conductor API key"),
    BotCommand(command="help", description="Quick control guide"),
)


async def unexpected_update_error(
    event: ErrorEvent,
    bot: Bot,
    principal: Any = None,
    request_id: str | None = None,
) -> bool:
    """Give an allowed user a bounded failure instead of a silent spinner.

    The allow-list middleware has already populated ``principal`` when a
    handler was reached. Unknown users remain silent even on an exception.
    Never include ``repr(exception)`` in Telegram: it can contain an API URL,
    source text, or another secret. Structured logs are scrubbed separately.
    """
    log.error(
        "bot.update_failed",
        error=repr(event.exception),
        request_id=request_id,
        update_id=event.update.update_id,
    )
    if principal is None:
        return True

    callback = event.update.callback_query
    if isinstance(callback, CallbackQuery):
        try:
            await callback.answer("Request failed. Retry once.", show_alert=True)
            return True
        except Exception:
            # The callback may be too old to answer; fall through to its chat.
            message = callback.message
    else:
        message = event.update.message or event.update.edited_message

    if not isinstance(message, Message):
        return True
    try:
        await bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
            text="⚠️ Request failed · retry once. If it repeats, run /health.",
            parse_mode=None,
            disable_notification=False,
        )
    except Exception as notify_exc:
        log.error("bot.failure_notice_failed", error=repr(notify_exc))
    return True


#: Reserved key inside ``wizard_state.data_json``; see :class:`PostgresStorage`.
_NAMESPACE_KEY: Final = "__ns__"


# =============================================================================
# Router registration seam
# =============================================================================


@dataclass(frozen=True, slots=True)
class RouterEntry:
    router: Router
    order: int = DEFAULT_ROUTER_ORDER
    name: str = ""
    seq: int = 0


_registry: list[RouterEntry] = []
_seq = 0


def register_router(
    router: Router,
    *,
    order: int = DEFAULT_ROUTER_ORDER,
    name: str | None = None,
) -> Router:
    """Register a handler module's router. **This is the convention.**

    A handler module owns exactly one ``Router`` and registers it at import
    time::

        # src/ctb/bot/handlers/board.py
        from aiogram import Router
        from ctb.bot.app import register_router

        router = Router(name="ctb.bot.handlers.board")
        register_router(router)          # or: register_router(router, order=900)

        @router.message(Command("board"))
        async def board(message: Message, route: Route, db: Database) -> None: ...

    :func:`discover_routers` imports every module under
    ``ctb.bot.handlers`` (alphabetically) which triggers the calls above; a
    module that exposes a module-level ``router`` but forgets to register it is
    picked up anyway, using its optional ``ROUTER_ORDER`` attribute.

    Routers are attached to the Dispatcher sorted by ``(order, registration
    order)``, and aiogram dispatches to the **first** matching handler — so a
    catch-all "plain text is a prompt" router must declare a high ``order``.

    Handlers receive these keyword arguments by name, courtesy of the workflow
    data and the outer middleware:

    ``settings`` (:class:`~ctb.settings.Settings`) · ``db``
    (:class:`~ctb.db.connection.Database`) · ``nonces``
    (:class:`~ctb.bot.keyboards.NonceStore`) · ``route``
    (:class:`~ctb.bot.middleware.routing.Route`) · ``principal``
    (:class:`~ctb.bot.middleware.tenancy.Principal`) · ``tenant``
    (:class:`~ctb.bot.middleware.tenancy.TenantContext`) · ``is_owner`` (bool) ·
    ``request_id`` (str) · ``bot`` · ``state`` (aiogram ``FSMContext``).

    Idempotent: registering the same router twice is a no-op.
    """
    global _seq
    for entry in _registry:
        if entry.router is router:
            return router
    _seq += 1
    _registry.append(
        RouterEntry(
            router=router,
            order=order,
            name=name or router.name,
            seq=_seq,
        )
    )
    return router


def registered_routers() -> tuple[Router, ...]:
    """Every registered router, in the order they will be attached."""
    ordered = sorted(_registry, key=lambda e: (e.order, e.seq))
    return tuple(entry.router for entry in ordered)


def clear_routers() -> None:
    """Drop the registry (tests, and re-running :func:`build_app`)."""
    _registry.clear()


def discover_routers(package: str = DEFAULT_HANDLER_PACKAGE) -> tuple[Router, ...]:
    """Import every handler module so it can register itself.

    A missing handler package is tolerated (the bot boots with no commands and
    says so); a handler module that *fails* to import is fatal, because booting
    with half the commands silently missing is worse than not booting.
    """
    try:
        pkg: ModuleType = importlib.import_module(package)
    except ModuleNotFoundError:
        log.warning("bot.no_handler_package", package=package)
        return ()
    paths = list(getattr(pkg, "__path__", []))
    for info in sorted(pkgutil.iter_modules(paths), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        candidate = getattr(module, "router", None)
        if isinstance(candidate, Router):
            register_router(
                candidate,
                order=int(getattr(module, "ROUTER_ORDER", DEFAULT_ROUTER_ORDER)),
                name=f"{package}.{info.name}",
            )
    return registered_routers()


# =============================================================================
# DB-backed FSM storage
# =============================================================================


def _state_str(state: StateType) -> str | None:
    return state.state if isinstance(state, State) else state


class PostgresStorage(BaseStorage):
    """aiogram FSM storage over ``wizard_state``, so wizards survive a restart.

    aiogram's :class:`~aiogram.fsm.storage.base.StorageKey` has six fields; the
    table's primary key is ``(chat_id, thread_id, user_id)``, which is the
    routing key the rest of the bot uses. The remaining two are handled without
    widening the schema:

    * The default destiny with no business connection — the only combination
      this bot produces — maps straight onto the ``state_key`` column and the
      ``data_json`` object, so the table stays readable in ``psql``.
    * Anything else (aiogram scenes, business accounts) is namespaced under the
      reserved ``data_json`` key ``__ns__``. Handlers must not use that key.

    ``bot_id`` is deliberately ignored: one process runs exactly one bot, and a
    token rotation should not orphan an in-flight wizard.

    **A wizard belongs to a tenant.** aiogram's FSM middleware reads storage on
    *every* update, before any handler runs — including the registration
    commands, which by definition have no tenant yet. With no tenant there is no
    wizard, so every read answers empty and every write is dropped rather than
    raising. The alternative — letting :class:`~ctb.db.errors.TenantScopeError`
    escape — turns ``/register`` into "⚠️ Request failed", which is how nobody
    signs up.
    """

    def __init__(
        self,
        db: Database | None = None,
        *,
        ttl_ms: int = wizard_repo.DEFAULT_TTL_MS,
    ) -> None:
        self._db = db
        self._ttl_ms = ttl_ms

    @property
    def db(self) -> Database:
        return self._db if self._db is not None else get_database()

    # -- key mapping -----------------------------------------------------------

    @staticmethod
    def _seat(key: StorageKey) -> tuple[int, int, int]:
        return (key.chat_id, key.thread_id or NO_THREAD_ID, key.user_id)

    @staticmethod
    def _namespace(key: StorageKey) -> str | None:
        """``None`` for the plain case; a stable string for everything else."""
        if key.destiny == DEFAULT_DESTINY and not key.business_connection_id:
            return None
        return f"{key.business_connection_id or ''}#{key.destiny}"

    # -- state -----------------------------------------------------------------

    @staticmethod
    def _scoped() -> bool:
        """Is there a tenant to own a wizard? Unresolved updates have none."""
        return current_tenant() is not None

    async def get_state(self, key: StorageKey) -> str | None:
        if not self._scoped():
            return None
        chat_id, thread_id, user_id = self._seat(key)
        row = await wizard_repo.get(self.db, chat_id, thread_id, user_id=user_id)
        if row is None:
            return None
        namespace = self._namespace(key)
        if namespace is None:
            return row.state_key
        return _read_ns(row.data, namespace).get("state")

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        if not self._scoped():
            return
        chat_id, thread_id, user_id = self._seat(key)
        value = _state_str(state)
        namespace = self._namespace(key)
        db = self.db
        async with db.transaction():
            row = await wizard_repo.get(db, chat_id, thread_id, user_id=user_id)
            if namespace is None:
                data = dict(row.data) if row is not None else {}
                if value is None and not _meaningful(data):
                    await wizard_repo.clear(db, chat_id, thread_id, user_id=user_id)
                    return
                await wizard_repo.set_state(
                    db,
                    chat_id,
                    thread_id,
                    user_id=user_id,
                    state_key=value,
                    data=data,
                    ttl_ms=self._ttl_ms,
                )
                return
            data = dict(row.data) if row is not None else {}
            slot = dict(_read_ns(data, namespace))
            slot["state"] = value
            _write_ns(data, namespace, slot)
            await wizard_repo.set_state(
                db,
                chat_id,
                thread_id,
                user_id=user_id,
                state_key=row.state_key if row is not None else None,
                data=data,
                ttl_ms=self._ttl_ms,
            )

    # -- data ------------------------------------------------------------------

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        if not self._scoped():
            return {}
        chat_id, thread_id, user_id = self._seat(key)
        row = await wizard_repo.get(self.db, chat_id, thread_id, user_id=user_id)
        if row is None:
            return {}
        namespace = self._namespace(key)
        if namespace is None:
            data = dict(row.data)
            data.pop(_NAMESPACE_KEY, None)
            return data
        return dict(_read_ns(row.data, namespace).get("data") or {})

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not self._scoped():
            return
        chat_id, thread_id, user_id = self._seat(key)
        namespace = self._namespace(key)
        db = self.db
        async with db.transaction():
            row = await wizard_repo.get(db, chat_id, thread_id, user_id=user_id)
            state_key = row.state_key if row is not None else None
            stored = dict(row.data) if row is not None else {}
            if namespace is None:
                namespaces = stored.get(_NAMESPACE_KEY)
                payload = {k: v for k, v in data.items() if k != _NAMESPACE_KEY}
                if namespaces:
                    payload[_NAMESPACE_KEY] = namespaces
                if state_key is None and not _meaningful(payload):
                    await wizard_repo.clear(db, chat_id, thread_id, user_id=user_id)
                    return
            else:
                slot = dict(_read_ns(stored, namespace))
                slot["data"] = dict(data)
                _write_ns(stored, namespace, slot)
                payload = stored
            await wizard_repo.set_state(
                db,
                chat_id,
                thread_id,
                user_id=user_id,
                state_key=state_key,
                data=payload,
                ttl_ms=self._ttl_ms,
            )

    async def update_data(
        self, key: StorageKey, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read-modify-write inside one transaction.

        The base class does get-then-set with a gap; two wizard steps racing in
        that gap would lose one of them.
        """
        db = self.db
        async with db.transaction():
            current = await self.get_data(key)
            current.update(data)
            await self.set_data(key, current)
        return current.copy()

    async def close(self) -> None:
        """No-op: the :class:`Database` is owned by the process, not by us."""


def _meaningful(data: Mapping[str, Any]) -> bool:
    """True when the row still holds something worth keeping."""
    return any(name != _NAMESPACE_KEY or value for name, value in data.items())


def _read_ns(data: Mapping[str, Any], namespace: str) -> dict[str, Any]:
    bucket = data.get(_NAMESPACE_KEY)
    if not isinstance(bucket, dict):
        return {}
    slot = bucket.get(namespace)  # pyright: ignore[reportUnknownMemberType]
    return dict(slot) if isinstance(slot, dict) else {}


def _write_ns(data: dict[str, Any], namespace: str, slot: Mapping[str, Any]) -> None:
    bucket = data.get(_NAMESPACE_KEY)
    buckets: dict[str, Any] = dict(bucket) if isinstance(bucket, dict) else {}
    if slot.get("state") is None and not slot.get("data"):
        buckets.pop(namespace, None)
    else:
        buckets[namespace] = dict(slot)
    if buckets:
        data[_NAMESPACE_KEY] = buckets
    else:
        data.pop(_NAMESPACE_KEY, None)


# =============================================================================
# Bot
# =============================================================================


class ConflictGuard(BaseRequestMiddleware):
    """Name the ``409 Conflict`` for what it is: another live instance.

    aiogram's poller already swallows and backs off every ``getUpdates``
    failure, so this does not change behaviour — it makes the one failure we
    actually expect during a Railway redeploy legible in the logs (and
    countable in ``/health``) instead of hiding inside a generic
    "Failed to fetch updates" line.

    It is also the only place that sees *every* Bot API round trip, so a
    successful one is recorded as Telegram liveness (:mod:`ctb.health`).
    """

    def __init__(self) -> None:
        self.conflicts = 0
        self.last_conflict_method: str | None = None

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            response = await make_request(bot, method)
        except TelegramConflictError as exc:
            self.conflicts += 1
            self.last_conflict_method = type(method).__name__
            log.warning(
                "telegram.conflict",
                detail="another instance is polling; retrying",
                method=self.last_conflict_method,
                conflicts=self.conflicts,
                error=str(exc),
            )
            raise
        # Any answered Bot API call — the long poll returning included — proves
        # Telegram is reachable, so it clears the polling-failure counter that
        # `/health` degrades on.
        telegram_health().record_ok()
        return response


def create_bot(settings: Settings | None = None) -> Bot:
    """The Bot, with HTML and link previews settled once and for all."""
    resolved = settings if settings is not None else get_settings()
    bot = Bot(
        token=resolved.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode="HTML",
            link_preview_is_disabled=True,
        ),
    )
    return bot


def install_middleware(
    dispatcher: Dispatcher,
    *,
    settings: Settings,
    system_db: Database,
    clients: ClientPool,
    db: Database | None = None,
    tenancy: TenantMiddleware | None = None,
) -> TenantMiddleware:
    """Register the three outer middlewares, in the one correct order.

    Log context first (so a rejection is traceable), then tenancy (so an
    unresolved update never reaches a lookup, and so the database scope exists
    before anything queries), then routing.

    ``Dispatcher.__init__`` registers its FSM middleware before anything we can
    add, and that middleware *reads the storage* (``await context.get_state()``)
    on every update. That would put a database read from an unauthenticated
    stranger ahead of the security check — and, worse, an unscoped read that
    now raises — so it is moved behind us via the manager's own
    ``unregister``/``register``, no private state touched.
    """
    tenant_middleware = tenancy or TenantMiddleware(
        system_db=system_db, clients=clients, settings=settings
    )
    manager = dispatcher.update.outer_middleware
    manager.register(LogContextMiddleware())
    manager.register(tenant_middleware)
    manager.register(RoutingMiddleware(db=db))
    if dispatcher.fsm in manager:
        manager.unregister(dispatcher.fsm)
        manager.register(dispatcher.fsm)
    return tenant_middleware


def create_dispatcher(
    *,
    settings: Settings,
    system_db: Database,
    clients: ClientPool,
    db: Database | None = None,
    storage: BaseStorage | None = None,
    nonces: NonceStore | None = None,
    routers: Sequence[Router] = (),
    tenancy: TenantMiddleware | None = None,
    **workflow: Any,
) -> tuple[Dispatcher, TenantMiddleware]:
    """Build a Dispatcher with our storage, middleware and workflow data."""
    dispatcher = Dispatcher(
        storage=storage or PostgresStorage(db),
        # One wizard per *seat*, not per chat. aiogram's default keys FSM state
        # on (chat, user), so a `/new` left half-finished in one topic ate the
        # next line typed in every other topic of the same chat — the wizard
        # router runs first, and its state filter matched. `wizard_state` was
        # always keyed by (chat, thread, user); this is the strategy that agrees
        # with it. `RoutingMiddleware._publish_seat` supplies the thread, which
        # Telegram itself omits for a topic in a private chat.
        fsm_strategy=FSMStrategy.USER_IN_TOPIC,
        settings=settings,
        db=db,
        system_db=system_db,
        clients=clients,
        nonces=nonces if nonces is not None else get_nonce_store(),
        **workflow,
    )
    tenant_middleware = install_middleware(
        dispatcher,
        settings=settings,
        system_db=system_db,
        clients=clients,
        db=db,
        tenancy=tenancy,
    )
    dispatcher.errors.register(unexpected_update_error)
    for router in routers:
        dispatcher.include_router(router)
    return dispatcher, tenant_middleware


@dataclass(slots=True)
class BotApp:
    """Everything the process needs to serve Telegram, wired together."""

    bot: Bot
    dispatcher: Dispatcher
    settings: Settings
    storage: BaseStorage
    nonces: NonceStore
    tenancy: TenantMiddleware
    conflicts: ConflictGuard
    routers: tuple[Router, ...] = field(default_factory=tuple)

    async def close(self) -> None:
        await self.bot.session.close()

    def health(self) -> dict[str, Any]:
        """Numbers for ``/health``. No secrets, no content."""
        return {
            "routers": [r.name for r in self.routers],
            "conflicts": self.conflicts.conflicts,
            **self.tenancy.health(),
            "live_nonces": len(self.nonces),
        }


def build_app(
    *,
    system_db: Database,
    clients: ClientPool,
    settings: Settings | None = None,
    db: Database | None = None,
    bot: Bot | None = None,
    storage: BaseStorage | None = None,
    nonces: NonceStore | None = None,
    routers: Sequence[Router] | None = None,
    discover: bool = True,
    handler_package: str = DEFAULT_HANDLER_PACKAGE,
    **workflow: Any,
) -> BotApp:
    """Assemble the bot. Called once from ``__main__``.

    ``routers=None`` + ``discover=True`` is the production path: import every
    module under ``ctb.bot.handlers`` and attach what registered itself.
    """
    resolved = settings if settings is not None else get_settings()
    store = nonces if nonces is not None else NonceStore()
    set_nonce_store(store)

    if routers is None:
        found = discover_routers(handler_package) if discover else registered_routers()
    else:
        found = tuple(routers)

    telegram_bot = bot if bot is not None else create_bot(resolved)
    guard = ConflictGuard()
    telegram_bot.session.middleware(guard)

    dispatcher, tenancy = create_dispatcher(
        settings=resolved,
        system_db=system_db,
        clients=clients,
        db=db,
        storage=storage,
        nonces=store,
        routers=found,
        **workflow,
    )
    log.info(
        "bot.built",
        routers=[r.name for r in found],
        handler_package=handler_package,
    )
    return BotApp(
        bot=telegram_bot,
        dispatcher=dispatcher,
        settings=resolved,
        storage=dispatcher.storage,
        nonces=store,
        tenancy=tenancy,
        conflicts=guard,
        routers=found,
    )


# =============================================================================
# Polling
# =============================================================================


def _token_rejected(exc: TelegramUnauthorizedError) -> None:
    """Record and name a 401 from Telegram before it takes the process down."""
    telegram_health().record_failure(f"unauthorized: {exc}")
    log.error(
        "bot.token_rejected",
        detail=(
            "Telegram rejected TELEGRAM_BOT_TOKEN. Verify it with "
            "curl https://api.telegram.org/bot<TOKEN>/getMe and redeploy — "
            "retrying cannot fix it."
        ),
        error=str(exc),
    )


async def run_polling(
    app: BotApp,
    *,
    polling_timeout: int = 30,
    allowed_updates: list[str] | None = None,
    handle_signals: bool = False,
    max_restarts: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> None:
    """Long-poll forever, surviving anything Telegram does to us.

    ``dp.start_polling`` already retries inside its update reader, so this loop
    only sees failures that escape it — a conflict raised while resolving the
    bot, a session torn down mid-flight, a bug. Every one of them is retried
    with backoff rather than propagated, because the process exiting is how a
    redeploy overlap turns into an outage (PLAN §Redeploy overlap defence #3).

    The single exception is ``TelegramUnauthorizedError``: a rejected token can
    never come good, and retrying it forever is indistinguishable from a
    healthy deploy — Railway sees a listening ``/health`` while the bot answers
    nobody. It propagates, the TaskGroup unwinds, and the deploy fails.

    ``handle_signals`` defaults to ``False``: the supervising ``TaskGroup`` in
    ``__main__`` owns SIGINT/SIGTERM and cancels this task, which unwinds
    cleanly through :class:`asyncio.CancelledError`.

    ``allowed_updates=None`` leaves aiogram to derive the list from the
    registered handlers, which is what enables ``chat_member`` updates only if
    something actually handles them. Pass an explicit list to override.
    """
    jitter = rng if rng is not None else random.Random()
    try:
        await app.bot.set_my_commands(list(BOT_COMMANDS))
    except TelegramUnauthorizedError as exc:
        _token_rejected(exc)
        raise
    except Exception as exc:
        # A stale command menu is inconvenient, never a reason to stop polling.
        log.warning("bot.menu_failed", error=repr(exc))
    # Warm the topic-icon pack before anything renames. It is one call, cached
    # for the life of the process, and fetching it lazily meant the *first*
    # state change after a deploy — the `/new` somebody is watching — was the
    # one that got no icon. `icon_pack` swallows its own failures.
    #
    # Imported here, not at module scope: every handler module imports this one
    # for `register_router`, so the reverse edge only exists inside a call.
    from ctb.bot.handlers.topics import icon_pack

    await icon_pack(app.bot)
    extra: dict[str, Any] = (
        {} if allowed_updates is None else {"allowed_updates": allowed_updates}
    )
    restarts = 0
    failures = 0
    while True:
        try:
            await app.dispatcher.start_polling(
                app.bot,
                polling_timeout=polling_timeout,
                handle_signals=handle_signals,
                close_bot_session=False,
                **extra,
            )
            log.info("bot.polling_stopped")
            return
        except asyncio.CancelledError:
            log.info("bot.polling_cancelled")
            raise
        except TelegramUnauthorizedError as exc:
            # The one failure that cannot self-heal: no amount of backoff turns
            # a wrong token into a right one. Backing off forever would leave a
            # green Railway deploy running a bot that answers nothing, so let
            # it out and take the process down loudly.
            _token_rejected(exc)
            raise
        except TelegramConflictError as exc:
            failures += 1
            telegram_health().record_failure(f"conflict: {exc}")
            delay = CONFLICT_BACKOFF_S
            log.warning(
                "bot.polling_conflict",
                detail="another instance is live; waiting for it to drain",
                error=str(exc),
                delay_s=delay,
            )
        except Exception as exc:
            failures += 1
            telegram_health().record_failure(repr(exc))
            delay = min(MAX_POLLING_BACKOFF_S, 2.0 ** min(failures, 6))
            log.error(
                "bot.polling_failed",
                error=repr(exc),
                delay_s=delay,
                failures=failures,
            )
        restarts += 1
        if max_restarts is not None and restarts > max_restarts:
            log.error("bot.polling_gave_up", restarts=restarts)
            return
        await sleep(delay * (1.0 + jitter.random() * 0.2))
