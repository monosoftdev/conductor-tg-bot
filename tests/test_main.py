"""Focused tests for production runtime wiring and teardown."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramUnauthorizedError

from ctb.__main__ import (
    RuntimeFactories,
    ServiceStoppedError,
    _assert_app_role_is_confined,
    build_runtime,
    run,
)
from ctb.bot.app import BotApp, run_polling
from ctb.db.connection import Database as PgDatabase
from ctb.health import TelegramHealth, reset_telegram_health, telegram_health
from ctb.settings import Settings, SettingsError
from tests import pg


class Runner:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail: BaseException | None = None,
        returns: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.fail = fail
        self.returns = returns
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self) -> None:
        self.events.append(f"run:{self.name}")
        self.started.set()
        if self.fail is not None:
            raise self.fail
        if self.returns:
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            self.events.append(f"cancel:{self.name}")
            raise

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")


class Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("close:bot-session")


class Bot:
    def __init__(self, events: list[str]) -> None:
        self.session = Session(events)


class App(Runner):
    def __init__(self, events: list[str]) -> None:
        super().__init__("telegram", events)
        self.bot = Bot(events)

    async def close(self) -> None:
        self.events.append("close:telegram")


class Client:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def aclose(self) -> None:
        self.events.append("close:clients")


class Database:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("close:db")


@dataclass
class Built:
    db: Database
    system_db: Database
    clients: Client
    bot: Bot
    outbox: Runner
    cards: Runner
    supervisor: Runner
    voice: Runner
    health: Runner
    app: App


def fake_factories(
    settings: Settings,
    events: list[str],
    *,
    fail_service: str | None = None,
    returning_service: str | None = None,
    fail_at: str | None = None,
) -> tuple[RuntimeFactories, Built]:
    def runner(name: str) -> Runner:
        return Runner(
            name,
            events,
            fail=RuntimeError("worker exploded") if fail_service == name else None,
            returns=returning_service == name,
        )

    built = Built(
        db=Database(events),
        system_db=Database(events),
        clients=Client(events),
        bot=Bot(events),
        outbox=runner("outbox"),
        cards=runner("status_cards"),
        supervisor=runner("supervisor"),
        voice=runner("voice"),
        health=runner("health"),
        app=App(events),
    )

    async def open_databases(_settings: Settings) -> tuple[Database, Database]:
        events.append("build:db")
        return built.db, built.system_db

    async def verify(db: Database) -> int:
        # The schema is checked on the worker pool; nothing here applies DDL.
        assert db is built.system_db
        events.append("build:migrate")
        if fail_at == "migrate":
            raise RuntimeError("bad migration")
        return 1

    def make(name: str, value: Any) -> Callable[..., Any]:
        def factory(*_args: Any) -> Any:
            events.append(f"build:{name}")
            if fail_at == name:
                raise RuntimeError(f"bad {name}")
            return value

        return factory

    factories = RuntimeFactories(
        load_settings=lambda: settings,
        configure_logging=lambda _settings: events.append("build:logging"),
        open_databases=open_databases,
        verify_schema=verify,
        make_client_pool=make("clients", built.clients),
        make_bot=make("bot", built.bot),
        make_outbox=make("outbox", built.outbox),
        make_status_cards=make("status_cards", built.cards),
        make_supervisor=make("supervisor", built.supervisor),
        make_voice=make("voice", built.voice),
        make_health_monitor=make("health_monitor", object()),
        make_health_server=make("health_server", built.health),
        make_app=make("app", built.app),
        run_telegram=lambda app: app.run(),
        make_holder=lambda: "test:1:holder",
    )
    return factories, built


async def test_boot_order_and_signal_style_shutdown_are_clean(
    settings: Settings,
) -> None:
    events: list[str] = []
    factories, built = fake_factories(settings, events)
    shutdown = asyncio.Event()

    task = asyncio.create_task(
        run(
            settings,
            factories=factories,
            stop_event=shutdown,
            install_signals=False,
        )
    )
    await asyncio.wait_for(built.health.started.wait(), timeout=1)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)

    assert events[:12] == [
        "build:logging",
        "build:db",
        "build:migrate",
        "build:clients",
        "build:bot",
        "build:outbox",
        "build:status_cards",
        "build:supervisor",
        "build:voice",
        "build:health_monitor",
        "build:health_server",
        "build:app",
    ]
    for service in (
        built.app,
        built.outbox,
        built.cards,
        built.voice,
        built.supervisor,
        built.health,
    ):
        assert service.started.is_set()
        assert service.cancelled
    assert events[-9:] == [
        "stop:voice",
        "stop:supervisor",
        "stop:status_cards",
        "stop:outbox",
        "stop:health",
        "close:telegram",
        "close:clients",
        "close:db",
        "close:db",  # both pools: the app one, then the worker one
    ]


async def test_critical_failure_cancels_siblings_and_closes_everything(
    settings: Settings,
) -> None:
    events: list[str] = []
    factories, built = fake_factories(settings, events, fail_service="outbox")

    with pytest.raises(ExceptionGroup) as caught:
        await run(settings, factories=factories, install_signals=False)

    assert any(
        isinstance(exc, RuntimeError) and str(exc) == "worker exploded"
        for exc in caught.value.exceptions
    )
    assert built.app.cancelled
    assert built.cards.cancelled
    assert built.voice.cancelled
    assert built.supervisor.cancelled
    assert built.health.cancelled
    assert events[-1] == "close:db"


async def test_a_critical_service_returning_is_a_failure(settings: Settings) -> None:
    events: list[str] = []
    factories, _built = fake_factories(settings, events, returning_service="outbox")

    with pytest.raises(ExceptionGroup) as caught:
        await run(settings, factories=factories, install_signals=False)

    assert any(isinstance(exc, ServiceStoppedError) for exc in caught.value.exceptions)


@pytest.mark.parametrize("mode", ("fails", "returns"))
async def test_an_optional_service_never_stops_the_bot(
    settings: Settings, mode: str
) -> None:
    """Voice is a bonus. Telegram, the outbox and the cursor outlive its bugs."""
    events: list[str] = []
    factories, built = fake_factories(
        settings,
        events,
        fail_service="voice" if mode == "fails" else None,
        returning_service="voice" if mode == "returns" else None,
    )
    shutdown = asyncio.Event()

    task = asyncio.create_task(
        run(settings, factories=factories, stop_event=shutdown, install_signals=False)
    )
    await asyncio.wait_for(built.voice.started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not task.done()  # the voice task ended; the process did not

    shutdown.set()
    await asyncio.wait_for(task, timeout=1)

    assert built.app.cancelled
    assert built.outbox.cancelled
    assert built.supervisor.cancelled
    assert not built.voice.cancelled  # it was already gone
    assert events[-1] == "close:db"


async def test_partial_build_failure_closes_only_constructed_resources(
    settings: Settings,
) -> None:
    events: list[str] = []
    factories, _built = fake_factories(settings, events, fail_at="outbox")

    with pytest.raises(RuntimeError, match="bad outbox"):
        await run(settings, factories=factories, install_signals=False)

    assert "build:status_cards" not in events
    assert events[-4:] == [
        "close:bot-session",
        "close:clients",
        "close:db",
        "close:db",
    ]


async def test_invalid_configuration_fails_before_logging_or_io(
    settings: Settings,
) -> None:
    events: list[str] = []
    factories, _built = fake_factories(settings, events)

    def invalid() -> Settings:
        events.append("build:settings")
        raise RuntimeError("invalid config")

    factories = RuntimeFactories(
        load_settings=invalid,
        configure_logging=factories.configure_logging,
        open_databases=factories.open_databases,
        verify_schema=factories.verify_schema,
        make_client_pool=factories.make_client_pool,
        make_bot=factories.make_bot,
        make_outbox=factories.make_outbox,
        make_status_cards=factories.make_status_cards,
        make_supervisor=factories.make_supervisor,
        make_voice=factories.make_voice,
        make_health_monitor=factories.make_health_monitor,
        make_health_server=factories.make_health_server,
        make_app=factories.make_app,
        run_telegram=factories.run_telegram,
        make_holder=factories.make_holder,
    )

    with pytest.raises(RuntimeError, match="invalid config"):
        await run(factories=factories, install_signals=False)

    assert events == ["build:settings"]


# ── a rejected bot token must fail the deploy, not hide behind backoff ───────


@pytest.fixture
def telegram_record() -> Iterator[TelegramHealth]:
    reset_telegram_health()
    yield telegram_health()
    reset_telegram_health()


class _PollingBot:
    """Just enough Bot for :func:`run_polling`."""

    def __init__(self, *, menu_error: BaseException | None = None) -> None:
        self.menu_error = menu_error
        self.menu_calls = 0

    async def set_my_commands(self, _commands: Any) -> bool:
        self.menu_calls += 1
        if self.menu_error is not None:
            raise self.menu_error
        return True


class _PollingDispatcher:
    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.calls = 0

    async def start_polling(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class _PollingApp:
    def __init__(
        self,
        *,
        error: BaseException | None = None,
        menu_error: BaseException | None = None,
    ) -> None:
        self.bot = _PollingBot(menu_error=menu_error)
        self.dispatcher = _PollingDispatcher(error)


def _polling_app(
    *, error: BaseException | None = None, menu_error: BaseException | None = None
) -> tuple[BotApp, _PollingDispatcher]:
    app = _PollingApp(error=error, menu_error=menu_error)
    return cast(BotApp, app), app.dispatcher


def _unauthorized() -> TelegramUnauthorizedError:
    return TelegramUnauthorizedError(method=None, message="Unauthorized")  # type: ignore[arg-type]


async def test_a_rejected_token_propagates_instead_of_backing_off_forever(
    telegram_record: TelegramHealth,
) -> None:
    """deploy-risk #2: a bad TELEGRAM_BOT_TOKEN reported healthy forever.

    Retrying cannot mint a valid token, so the TaskGroup must unwind and let
    Railway fail the deploy loudly.
    """
    app, dispatcher = _polling_app(error=_unauthorized())

    with pytest.raises(TelegramUnauthorizedError):
        await run_polling(app, sleep=_never_sleeps)

    assert dispatcher.calls == 1  # no retry storm
    assert telegram_record.failures == 1
    assert "unauthorized" in telegram_record.last_error.lower()


async def test_a_rejected_token_on_the_command_menu_also_propagates(
    telegram_record: TelegramHealth,
) -> None:
    app, dispatcher = _polling_app(menu_error=_unauthorized())

    with pytest.raises(TelegramUnauthorizedError):
        await run_polling(app, sleep=_never_sleeps)

    assert dispatcher.calls == 0
    assert telegram_record.failures == 1


async def test_polling_failures_are_recorded_for_health(
    telegram_record: TelegramHealth,
) -> None:
    """Everything else still retries — but ``/health`` can now see it."""
    app, dispatcher = _polling_app(error=RuntimeError("network gone"))

    await run_polling(app, sleep=_never_sleeps, max_restarts=2)

    assert dispatcher.calls == 3
    assert telegram_record.failures == 3
    assert "network gone" in telegram_record.last_error


async def _never_sleeps(_delay: float) -> None:
    return None


async def test_production_factories_build_every_component_without_network(
    settings_factory: Callable[..., Settings],
    pg_reset: object,
) -> None:
    """The whole runtime, wired for real, with no socket to Telegram."""
    settings = settings_factory()
    runtime = await build_runtime(settings)
    assert runtime.supervisor is not None
    assert runtime.clients is not None
    assert runtime.db is not None
    assert runtime.system_db is not None
    db = runtime.db
    try:
        assert [name for name, _runner in runtime.runners()] == [
            "telegram",
            "outbox",
            "status_cards",
            "voice",
            "supervisor",
            "health",
        ]
        assert runtime.supervisor.holder == runtime.holder
        # No process-wide Conductor client exists any more: one per tenant,
        # built on demand from that tenant's sealed key.
        assert runtime.clients.health()["clients"] == 0
        assert db.is_connected
    finally:
        await runtime.close()
    assert not db.is_connected


class TestAppRoleConfinement:
    """The one misconfiguration that fails silently in the worst direction.

    Row-level security does not apply to a superuser or a ``BYPASSRLS`` role,
    not even with ``FORCE``. Point ``DATABASE_URL`` at a managed provider's
    default ``postgres`` user and everything keeps working while every tenant
    reads every other tenant's transcripts.
    """

    async def check(self, dsn: str) -> None:
        pool = await PgDatabase(dsn, min_size=1, max_size=2).connect()
        try:
            await _assert_app_role_is_confined(pool)
        finally:
            await pool.close()

    async def test_the_app_role_is_accepted(self, pg_reset: object) -> None:
        await self.check(pg.app_dsn())

    async def test_a_superuser_dsn_refuses_to_boot(self, pg_reset: object) -> None:
        with pytest.raises(SettingsError, match="bypasses row-level security"):
            await self.check(pg.admin_dsn())

    async def test_the_worker_role_refuses_to_boot_as_the_app_pool(
        self, pg_reset: object
    ) -> None:
        """BYPASSRLS without superuser is the subtler half of the same mistake."""
        with pytest.raises(SettingsError, match="bypasses row-level security"):
            await self.check(pg.worker_dsn())
