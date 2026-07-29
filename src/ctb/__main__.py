"""Production process lifecycle.

Boot is intentionally linear and fail-fast:

``settings -> logging -> database -> migrations -> client/bot -> workers``.

Once built, every long-lived worker runs in one :class:`asyncio.TaskGroup`.  A
*critical* worker that raises *or returns unexpectedly* cancels its siblings and
closes all resources.  SIGINT/SIGTERM use that same path instead of relying on
aiogram's independent signal handling.

:data:`OPTIONAL_SERVICES` is the exception.  Those workers are supervised, not
critical: a failure is logged and the task ends, leaving the rest of the process
running.  The bot exists to answer "did my background agent finish?", so an
optional extra must never be able to take Telegram, the outbox, the supervisor
and the cursor down with it.

The factory seam is deliberately small but complete.  Production uses
:func:`production_factories`; tests can replace constructors without opening a
socket, creating a Telegram session, or importing the poller implementation.
"""

from __future__ import annotations

import asyncio
import inspect
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Final, cast

from ctb.logging import get_logger
from ctb.settings import Settings, SettingsError

__all__ = [
    "OPTIONAL_SERVICES",
    "RuntimeFactories",
    "RuntimeServices",
    "ServiceStoppedError",
    "build_runtime",
    "main",
    "production_factories",
    "run",
    "run_services",
]

log = get_logger(__name__)

#: Built once per process by :func:`_shared_pacer`.
_PACER: Any = None

type Factory = Callable[..., Any]
type AsyncFactory = Callable[..., Awaitable[Any]]

_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGINT, signal.SIGTERM)
# Migration 002 contains only the optional GitHub token fields and CI ledger.
# Core Telegram/Conductor behavior remains available on schema 001.
_CI_SCHEMA_VERSION: Final = 2

#: Services whose failure is logged and contained instead of ending the process.
#: Voice is off by default, costs money, and depends on a third party — it is a
#: bonus, not a reason to stop delivering agent replies.
#: ``ci`` joins it for the same reasons: it needs a token most teams will not
#: have stored, it depends on GitHub being up, and a CI verdict nobody hears is
#: not worth losing an agent reply over.
OPTIONAL_SERVICES: Final[frozenset[str]] = frozenset({"voice", "ci"})


class ServiceStoppedError(RuntimeError):
    """A service documented to run forever returned without a shutdown."""


class _ShutdownRequested(Exception):
    """Internal TaskGroup sentinel used to cancel every critical service."""


@dataclass(frozen=True, slots=True)
class RuntimeFactories:
    """Constructors used by :func:`build_runtime`.

    Keeping this data-only avoids a mocking framework in lifecycle tests and,
    more importantly, makes ownership explicit: this module constructs and
    therefore closes every object returned here.
    """

    load_settings: Callable[[], Settings]
    configure_logging: Callable[[Settings], None]
    open_databases: Callable[[Settings], Awaitable[tuple[Any, Any]]]
    verify_schema: Callable[[Any], Awaitable[int]]
    make_client_pool: Factory
    make_bot: Factory
    make_outbox: Factory
    make_status_cards: Factory
    make_supervisor: Factory
    make_voice: Factory
    make_ci: Factory
    make_health_monitor: Factory
    make_health_server: Factory
    make_app: Factory
    run_telegram: Callable[[Any], Awaitable[None]]
    make_holder: Callable[[], str]


@dataclass(slots=True)
class RuntimeServices:
    """All process-owned resources, including partially built runtimes."""

    settings: Settings
    holder: str
    db: Any | None = None
    system_db: Any | None = None
    clients: Any | None = None
    bot: Any | None = None
    outbox: Any | None = None
    status_cards: Any | None = None
    supervisor: Any | None = None
    voice: Any | None = None
    ci: Any | None = None
    health_monitor: Any | None = None
    health_server: Any | None = None
    app: Any | None = None
    telegram_runner: Callable[[Any], Awaitable[None]] | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def runners(self) -> tuple[tuple[str, Callable[[], Awaitable[None]]], ...]:
        """The long-lived services in startup order.

        Every one of them must exist before the process serves traffic; whether
        its failure is fatal is decided by :data:`OPTIONAL_SERVICES`.
        """
        values = (
            ("telegram", self.app),
            ("outbox", self.outbox),
            ("status_cards", self.status_cards),
            ("voice", self.voice),
            ("ci", self.ci),
            ("supervisor", self.supervisor),
            ("health", self.health_server),
        )
        missing = [name for name, value in values if value is None]
        if missing:
            raise RuntimeError(f"runtime is incomplete: missing {', '.join(missing)}")
        runners: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        for name, value in values:
            if name == "telegram":
                if self.telegram_runner is None:
                    raise RuntimeError("runtime is incomplete: missing telegram runner")

                async def run_telegram(
                    value: Any = value,
                    runner: Callable[[Any], Awaitable[None]] = self.telegram_runner,
                ) -> None:
                    await runner(value)

                runners.append((name, run_telegram))
                continue
            method = getattr(value, "run", None)
            if not callable(method):
                raise TypeError(f"{name} has no async run() method")
            runners.append((name, cast(Callable[[], Awaitable[None]], method)))
        return tuple(runners)

    async def close(self) -> None:
        """Close everything once, continuing after individual close failures."""
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []

        # Stop producers before consumers, then close network and storage.
        for name, component, method_name in (
            ("voice", self.voice, "stop"),
            ("ci", self.ci, "stop"),
            ("supervisor", self.supervisor, "stop"),
            ("status_cards", self.status_cards, "stop"),
            ("outbox", self.outbox, "stop"),
            ("health", self.health_server, "stop"),
            ("telegram", self.app, "close"),
        ):
            if component is None:
                continue
            method = getattr(component, method_name, None)
            if not callable(method):
                continue
            try:
                await _maybe_await(method())
            except BaseException as exc:  # finish closing the remaining resources
                failures.append(exc)
                log.error("runtime.close_failed", component=name, error=repr(exc))

        # If app construction failed, it never took ownership of the Bot.
        if self.app is None and self.bot is not None:
            session = getattr(self.bot, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    await _maybe_await(close())
                except BaseException as exc:
                    failures.append(exc)
                    log.error(
                        "runtime.close_failed", component="telegram", error=repr(exc)
                    )

        if self.clients is not None:
            close = getattr(self.clients, "aclose", None)
            if callable(close):
                try:
                    await _maybe_await(close())
                except BaseException as exc:
                    failures.append(exc)
                    log.error(
                        "runtime.close_failed", component="conductor", error=repr(exc)
                    )
        for name, pool in (("database", self.db), ("system_database", self.system_db)):
            if pool is None:
                continue
            close = getattr(pool, "close", None)
            if callable(close):
                try:
                    await _maybe_await(close())
                except BaseException as exc:
                    failures.append(exc)
                    log.error("runtime.close_failed", component=name, error=repr(exc))

        # These imports are cheap and kept here to avoid hidden global ownership.
        from ctb.db.connection import set_database
        from ctb.runtime import reset_runtime
        from ctb.settings import reset_settings

        set_database(None)
        reset_runtime()
        reset_settings()
        if failures:
            raise BaseExceptionGroup("runtime cleanup failed", failures)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _run_telegram(app: Any) -> None:
    from ctb.bot.app import run_polling

    await run_polling(app, handle_signals=False)


async def _critical(name: str, runner: Callable[[], Awaitable[None]]) -> None:
    log.info("runtime.service_started", service=name)
    await runner()
    raise ServiceStoppedError(f"critical service {name!r} stopped unexpectedly")


async def _optional(name: str, runner: Callable[[], Awaitable[None]]) -> None:
    """Run a non-essential service; contain anything short of a cancellation.

    Returning ends only this task.  ``asyncio.CancelledError`` is a
    :class:`BaseException`, so shutdown still propagates untouched.
    """
    log.info("runtime.service_started", service=name, optional=True)
    try:
        await runner()
    except Exception as exc:
        log.error("runtime.optional_service_failed", service=name, error=repr(exc))
        return
    log.warning("runtime.optional_service_stopped", service=name)


async def _request_shutdown(event: asyncio.Event) -> None:
    await event.wait()
    raise _ShutdownRequested


async def run_services(
    services: RuntimeServices,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run every service until a signal or a *critical* service failure."""
    shutdown = stop_event or asyncio.Event()
    try:
        async with asyncio.TaskGroup() as tasks:
            for name, runner in services.runners():
                supervise = _optional if name in OPTIONAL_SERVICES else _critical
                tasks.create_task(supervise(name, runner), name=f"ctb-{name}")
            tasks.create_task(_request_shutdown(shutdown), name="ctb-shutdown-watcher")
    except* _ShutdownRequested:
        log.info("runtime.shutdown_requested")


def _default_load_settings() -> Settings:
    from ctb.settings import load_settings

    return load_settings()


def _default_configure_logging(settings: Settings) -> None:
    from ctb.logging import configure_logging

    configure_logging(settings)


async def _default_open_databases(settings: Settings) -> tuple[Any, Any]:
    """Two pools: the tenant-scoped app role, then the BYPASSRLS worker role.

    Both are opened here so a bad DSN fails at boot rather than on the first
    update, and so shutdown has one owner for both.
    """
    from ctb.db.connection import open_database

    app = await open_database(
        settings.database_url.get_secret_value(),
        max_size=settings.db_pool_max,
    )
    system = await open_database(
        settings.system_database_url.get_secret_value(),
        max_size=settings.system_db_pool_max,
        system=True,
    )
    await _assert_app_role_is_confined(app)
    return app, system


async def _assert_app_role_is_confined(app: Any) -> None:
    """Refuse to boot if ``DATABASE_URL`` names a role that bypasses RLS.

    This is the one misconfiguration that fails silently in the worst possible
    direction. Row-level security does not apply to a superuser or to a role
    with ``BYPASSRLS`` — not even with ``FORCE`` — so pointing the app pool at,
    say, a managed provider's default ``postgres`` user would leave every query
    working, every test passing, and every tenant reading every other tenant's
    transcripts. Nothing downstream would notice.

    ``pg_roles`` is world-readable and unaffected by RLS, so one query settles
    it. The scope is arbitrary: we need a tenant set only because the app pool
    refuses to hand out a connection without one.
    """
    from ctb.db.connection import tenant_scope

    async with tenant_scope(uuid.UUID(int=0)):
        unconfined = await app.fetch_val(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
    if unconfined:
        raise SettingsError(
            "DATABASE_URL connects as a role that bypasses row-level security "
            "(superuser or BYPASSRLS). Tenant isolation would be off and "
            "nothing else would tell you. Point it at the ctb_app role created "
            "by `python -m ctb.db.bootstrap`."
        )


async def _default_verify_schema(db: Any) -> int:
    """Check the schema is present. The application never applies migrations.

    Neither database role may create anything: DDL belongs to
    ``python -m ctb.db.bootstrap``, run once by an operator with the rights to
    do it. Booting against an unmigrated database must therefore fail here,
    loudly, rather than crash-loop on the first query.
    """
    from ctb.db.migrate import current_schema_version

    version = await current_schema_version(db)
    if version < 1:
        raise SettingsError(
            "The database has no schema. Run:\n"
            "  python -m ctb.db.bootstrap --admin-dsn ... "
            "--app-password ... --worker-password ..."
        )
    return version


def _default_make_client_pool(settings: Settings, system_db: Any) -> Any:
    """One Conductor client per tenant, sharing one HTTP transport.

    API events are recorded on the **system** pool: ``api_events`` has no
    row-level security precisely because a request can fail before a tenant is
    resolved, and the sink must never be the thing that raises.
    """
    from ctb.conductor.pool import ClientPool
    from ctb.db.repo import events as events_repo

    async def record(event: Any) -> None:
        await events_repo.record_api_event(system_db, **event.as_row())

    return ClientPool(settings.secret_box(), on_event=record)


def _default_make_bot(settings: Settings) -> Any:
    from ctb.bot.app import create_bot

    return create_bot(settings)


def _default_make_outbox(bot: Any, db: Any) -> Any:
    from ctb.delivery.outbox import Outbox

    return Outbox(bot, db, pacer=_shared_pacer())


def _shared_pacer() -> Any:
    """One budget for both streams that talk to Telegram.

    Transcript deliveries and status-card edits spend the same bot token
    against the same per-second ceiling. Two independent pacers would each
    stay politely under a limit they are jointly blowing through.
    """
    from ctb.delivery.pacing import TelegramPacer

    global _PACER
    if _PACER is None:
        _PACER = TelegramPacer()
    return _PACER


def _default_make_status_cards(bot: Any, db: Any) -> Any:
    from ctb.bot.keyboards import status_card_keyboard
    from ctb.delivery.status_card import StatusCards

    def keyboard(
        buttons: Any,
        *,
        session_id: str,
        deep_link: str | None = None,
    ) -> Any:
        return status_card_keyboard(buttons, session_id, deep_link=deep_link)

    return StatusCards(bot, db, keyboard=keyboard, pacer=_shared_pacer())


def _default_make_supervisor(
    clients: Any,
    db: Any,
    system_db: Any,
    bot: Any,
    outbox: Any,
    status_cards: Any,
    holder: str,
) -> Any:
    # The poller is intentionally a late import: lifecycle tests do not need it,
    # and it lets its implementation evolve behind the documented constructor.
    from ctb.bot.actions import BotActionSink
    from ctb.turn.supervisor import Supervisor

    return Supervisor(
        clients,
        db,
        system_db,
        action_sink=BotActionSink(
            bot,
            db,
            system_db,
            outbox,
            status_cards,
        ),
        holder=holder,
    )


def _default_make_ci(system_db: Any, outbox: Any, enabled: bool = True) -> Any:
    from ctb.ci.watcher import CiWatcher
    from ctb.github.pool import GitHubPool
    from ctb.runtime import secret_box, set_github_pool

    # One client per tenant, from that tenant's own sealed token. No shared
    # fallback: the token reads a customer's private source, so borrowing
    # another team's would be exactly the cross-tenant read the schema exists
    # to prevent.
    clients = GitHubPool(secret_box())
    # Published so /revoke can evict a decrypted token rather than leaving it
    # live in an Authorization header for the life of the process.
    set_github_pool(clients)
    return CiWatcher(
        system_db=system_db,
        outbox=outbox,
        clients=clients,
        enabled=enabled,
    )


def _default_make_health_monitor(system_db: Any, clients: Any, holder: str) -> Any:
    from ctb.health import HealthMonitor

    return HealthMonitor(
        database=lambda: system_db,
        clients=lambda: clients,
        holder=holder,
        instance=holder,
    )


def _default_make_voice(
    settings: Settings,
    db: Any,
    system_db: Any,
    bot: Any,
    clients: Any,
    supervisor: Any,
) -> Any:
    from ctb.bot.keyboards import NonceStore
    from ctb.runtime import secret_box, set_provider_pool
    from ctb.voice.pool import ProviderPool
    from ctb.voice.service import VoiceService

    # One provider per tenant, built from that tenant's own sealed speech key.
    # There is deliberately no platform-wide fallback key: voice is the one
    # feature whose data leaves the perimeter, so nobody is billed — or
    # exposed — through somebody else's account.
    providers = (
        ProviderPool(
            secret_box(),
            model=settings.voice_stt_model,
            language=settings.voice_language,
        )
        if settings.voice_enabled
        else None
    )
    # Published so /revoke and /forget can evict a decrypted speech key.
    # ProviderPool has no idle sweep, so without this the plaintext would
    # outlive the row it came from for the life of the process.
    set_provider_pool(providers)
    return VoiceService(
        settings,
        db,
        system_db,
        bot,
        clients,
        supervisor,
        NonceStore(),
        providers,
    )


def _default_make_health_server(monitor: Any) -> Any:
    from ctb.health import HealthServer

    return HealthServer(monitor)


def _default_make_app(
    settings: Settings,
    db: Any,
    system_db: Any,
    bot: Any,
    clients: Any,
    outbox: Any,
    status_cards: Any,
    supervisor: Any,
    voice: Any,
    health_monitor: Any,
) -> Any:
    from ctb.bot.app import build_app

    return build_app(
        settings=settings,
        db=db,
        system_db=system_db,
        clients=clients,
        bot=bot,
        outbox=outbox,
        status_cards=status_cards,
        supervisor=supervisor,
        voice_service=voice,
        health_monitor=health_monitor,
        nonces=voice.nonces,
    )


def _default_make_holder() -> str:
    from ctb.db.repo.lease import instance_id

    return instance_id()


def production_factories() -> RuntimeFactories:
    """Return the real constructors without constructing any resources."""
    return RuntimeFactories(
        load_settings=_default_load_settings,
        configure_logging=_default_configure_logging,
        open_databases=_default_open_databases,
        verify_schema=_default_verify_schema,
        make_client_pool=_default_make_client_pool,
        make_bot=_default_make_bot,
        make_outbox=_default_make_outbox,
        make_status_cards=_default_make_status_cards,
        make_supervisor=_default_make_supervisor,
        make_voice=_default_make_voice,
        make_ci=_default_make_ci,
        make_health_monitor=_default_make_health_monitor,
        make_health_server=_default_make_health_server,
        make_app=_default_make_app,
        run_telegram=_run_telegram,
        make_holder=_default_make_holder,
    )


async def build_runtime(
    settings: Settings,
    *,
    factories: RuntimeFactories | None = None,
) -> RuntimeServices:
    """Construct a fully wired runtime, closing partial work on failure."""
    made = factories or production_factories()
    runtime = RuntimeServices(settings=settings, holder=made.make_holder())
    try:
        runtime.db, runtime.system_db = await made.open_databases(settings)

        from ctb.db.connection import set_database
        from ctb.runtime import set_client_pool, set_secret_box, set_system_database
        from ctb.settings import set_settings

        set_database(runtime.db)
        set_system_database(runtime.system_db)
        set_settings(settings)
        # The canary runs before anything can need a key: a bad
        # CTB_MASTER_KEYS must kill the boot, not the first user's first prompt.
        secrets = settings.secret_box()
        secrets.self_check()
        set_secret_box(secrets)
        # Callback payloads that must survive a redeploy are HMAC'd under a key
        # derived from the same master, so every replica agrees and nothing
        # outside the deployment can mint one.
        from ctb.bot.keyboards import set_signing_key

        set_signing_key(secrets.derive("callback-signature"))

        version = await made.verify_schema(runtime.system_db)
        log.info("runtime.database_ready", schema_version=version)

        runtime.clients = made.make_client_pool(settings, runtime.system_db)
        set_client_pool(runtime.clients)

        runtime.bot = made.make_bot(settings)
        runtime.outbox = made.make_outbox(runtime.bot, runtime.system_db)
        runtime.status_cards = made.make_status_cards(runtime.bot, runtime.db)
        runtime.supervisor = made.make_supervisor(
            runtime.clients,
            runtime.db,
            runtime.system_db,
            runtime.bot,
            runtime.outbox,
            runtime.status_cards,
            runtime.holder,
        )
        runtime.voice = made.make_voice(
            settings,
            runtime.db,
            runtime.system_db,
            runtime.bot,
            runtime.clients,
            runtime.supervisor,
        )
        ci_enabled = version >= _CI_SCHEMA_VERSION
        if not ci_enabled:
            log.warning(
                "runtime.ci_disabled",
                schema_version=version,
                required_schema_version=_CI_SCHEMA_VERSION,
            )
        runtime.ci = made.make_ci(runtime.system_db, runtime.outbox, ci_enabled)
        runtime.health_monitor = made.make_health_monitor(
            runtime.system_db,
            runtime.clients,
            runtime.holder,
        )
        runtime.health_server = made.make_health_server(runtime.health_monitor)
        runtime.app = made.make_app(
            settings,
            runtime.db,
            runtime.system_db,
            runtime.bot,
            runtime.clients,
            runtime.outbox,
            runtime.status_cards,
            runtime.supervisor,
            runtime.voice,
            runtime.health_monitor,
        )
        runtime.telegram_runner = made.run_telegram
        return runtime
    except BaseException:
        await runtime.close()
        raise


@contextmanager
def _signal_handlers(
    stop_event: asyncio.Event,
    *,
    watched: tuple[signal.Signals, ...] = _SIGNALS,
) -> Iterator[None]:
    """Translate process signals into the runtime's structured shutdown."""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    fallbacks: list[tuple[signal.Signals, Any]] = []
    for watched_signal in watched:
        try:
            loop.add_signal_handler(watched_signal, stop_event.set)
            installed.append(watched_signal)
        except (NotImplementedError, RuntimeError):
            previous = signal.getsignal(watched_signal)

            def handle(
                _signum: int,
                _frame: FrameType | None,
                *,
                event: asyncio.Event = stop_event,
            ) -> None:
                loop.call_soon_threadsafe(event.set)

            signal.signal(watched_signal, handle)
            fallbacks.append((watched_signal, previous))
    try:
        yield
    finally:
        for watched_signal in installed:
            loop.remove_signal_handler(watched_signal)
        for watched_signal, previous in fallbacks:
            signal.signal(watched_signal, previous)


async def run(
    settings: Settings | None = None,
    *,
    factories: RuntimeFactories | None = None,
    stop_event: asyncio.Event | None = None,
    install_signals: bool = True,
) -> None:
    """Boot, serve, and close the production process."""
    made = factories or production_factories()
    # Configuration is the first operation. Invalid secrets/allowlist fail
    # before a database file or network client exists.
    resolved = settings if settings is not None else made.load_settings()
    made.configure_logging(resolved)

    from ctb.settings import set_settings

    set_settings(resolved)
    shutdown = stop_event or asyncio.Event()
    runtime: RuntimeServices | None = None
    context = _signal_handlers(shutdown) if install_signals else _null_context()
    try:
        with context:
            runtime = await build_runtime(resolved, factories=made)
            log.info(
                "runtime.ready",
                holder=runtime.holder,
            )
            await run_services(runtime, stop_event=shutdown)
    finally:
        if runtime is not None:
            await runtime.close()
        log.info("runtime.stopped")


@contextmanager
def _null_context() -> Iterator[None]:
    yield


def main() -> None:
    """Console/module entry point.

    A misconfiguration is not a crash, so it does not print like one. Raising
    ``SettingsError`` through ``asyncio.run`` buried the one line that matters
    under forty lines of pydantic and asyncio frames — and on a hosting
    platform, where the deploy log is a scrolling box next to a red
    "healthcheck failed", that reads as *no error at all*. Every report of this
    has been "it does not work and there are no logs".

    So: print the reason, unmissably, and exit 1. A real crash still gets its
    traceback, which is what tracebacks are for.
    """
    with suppress(KeyboardInterrupt):
        try:
            asyncio.run(run())
        except SettingsError as exc:
            _report_bad_configuration(exc)
            raise SystemExit(1) from None


def _report_bad_configuration(exc: SettingsError) -> None:
    """One block, on stderr, saying what to fix and where it comes from."""
    rule = "-" * 68
    print(
        f"\n{rule}\n"
        f"  conductor-tg-bot cannot start: configuration\n"
        f"{rule}\n\n"
        f"{exc}\n\n"
        f"{rule}\n"
        f"  Nothing is wrong with the code or the image. Set the variables\n"
        f"  named above and redeploy. docs/SETUP.md says where each one\n"
        f"  comes from.\n"
        f"{rule}\n",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
