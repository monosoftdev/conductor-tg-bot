"""The health surface.

The load-bearing assertion in this file is the policy one: a *degraded but
alive* bot — API key rejected, circuit open, pollers stopped — still answers
Railway's healthcheck with 200 and the reason in the body, because restarting
it cannot fix a bad key and would only lose the evidence. A process that cannot
read its own SQLite state answers 503, because that is the one failure a
restart plausibly repairs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import httpx
import pytest

from ctb.conductor.client import ConductorClient
from ctb.conductor.errors import ApiError, AuthFatal, RateLimited
from ctb.conductor.pool import ClientPool
from ctb.db.connection import Database, set_database
from ctb.db.errors import DatabaseError
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo import events as events_repo
from ctb.db.repo import lease as lease_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.health import (
    API_EVENT_LIMIT,
    DEGRADATION_AUTH_FATAL,
    DEGRADATION_CIRCUIT_OPEN,
    DEGRADATION_DATABASE,
    DEGRADATION_DELIVERY_BACKLOG,
    DEGRADATION_DELIVERY_FAILED,
    DEGRADATION_DELIVERY_STALLED,
    DEGRADATION_DELIVERY_STRANDED,
    DEGRADATION_LEASE_LOST,
    DEGRADATION_POLL_LAG,
    DEGRADATION_RATE_LIMITED,
    DEGRADATION_TELEGRAM,
    DELIVERY_BACKLOG,
    DELIVERY_FAILED_WINDOW_MS,
    DELIVERY_STALL_MS,
    DELIVERY_STRANDED_MS,
    HEALTH_PATH,
    MONITOR_KEY,
    TELEGRAM_FAILURE_THRESHOLD,
    Degradation,
    HealthMonitor,
    HealthReport,
    HealthServer,
    HealthStatus,
    TelegramHealth,
    create_app,
    default_database_provider,
    default_pool_provider,
    detail_allowed,
    format_health_html,
    health_port,
    health_token,
    is_local_remote,
    is_loopback_remote,
    serve_health,
    telegram_health,
)
from ctb.settings import Settings, reset_settings
from tests.conftest import FAKE_API_KEY, FakeClock
from tests.pg import as_tenant, worker_dsn

WALL = 1_700_000_000_000  # a fixed epoch-ms "now" for every deterministic test


# ── helpers ──────────────────────────────────────────────────────────────────


class OnePool:
    """A :class:`ClientPool` stand-in holding at most one tenant's client."""

    def __init__(self, client: ConductorClient | None) -> None:
        self._client = client

    def clients(self) -> tuple[tuple[str, ConductorClient], ...]:
        return () if self._client is None else (("tenant-a", self._client),)

    def health(self) -> dict[str, object]:
        return {"clients": 0 if self._client is None else 1, "pinned": 0}


def make_monitor(
    *,
    db: Database | None = None,
    client: ConductorClient | None = None,
    telegram: TelegramHealth | None = None,
    at: int = WALL,
    clock: Callable[[], float] | None = None,
    **kwargs: Any,
) -> HealthMonitor:
    """A monitor wired to explicit providers and a frozen wall clock."""
    record = telegram if telegram is not None else TelegramHealth()
    return HealthMonitor(
        database=lambda: db,
        clients=lambda: None if client is None else cast(ClientPool, OnePool(client)),
        telegram=lambda: record,
        wall_clock=lambda: at,
        clock=clock or FakeClock(1_000.0),
        started_at=1_000.0,
        cache_ttl_s=0.0,
        **kwargs,
    )


async def seed_session(
    system_db: Database,
    session_id: str = "sess-1",
    *,
    bound: bool = True,
    at: int = WALL,
    poll_interval_ms: int = 20_000,
    turn_state: str = "IDLE",
) -> None:
    async with as_tenant():
        await _seed_session(
            system_db,
            session_id,
            bound=bound,
            at=at,
            poll_interval_ms=poll_interval_ms,
            turn_state=turn_state,
        )


async def _seed_session(
    system_db: Database,
    session_id: str,
    *,
    bound: bool,
    at: int,
    poll_interval_ms: int,
    turn_state: str,
) -> None:
    await sessions_repo.upsert(
        system_db, session_id, chat_id=-100123, thread_id=7, is_bound=bound, at=at
    )
    await sessions_repo.update(
        system_db,
        session_id,
        at=at,
        poll_interval_ms=poll_interval_ms,
        turn_state=turn_state,
    )


def transport_returning(
    status: int,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body or {"error": "nope"}, headers=headers)

    return httpx.MockTransport(handler)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
async def running_server() -> AsyncIterator[Callable[..., Any]]:
    servers: list[HealthServer] = []

    async def start(monitor: HealthMonitor, **kwargs: Any) -> HealthServer:
        server = HealthServer(monitor, host="127.0.0.1", port=0, **kwargs)
        await server.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        await server.stop()


async def get_json(
    server: HealthServer, path: str = HEALTH_PATH, **kwargs: Any
) -> tuple[int, dict[str, Any]]:
    url = f"http://127.0.0.1:{server.port}{path}"
    async with httpx.AsyncClient() as http:
        response = await http.get(url, **kwargs)
    body: dict[str, Any] = {}
    if response.headers.get("content-type", "").startswith("application/json"):
        body = json.loads(response.text)
    return response.status_code, body


# ── the happy path and the JSON shape ────────────────────────────────────────


async def test_report_shape_is_complete(system_db: Database) -> None:
    await seed_session(system_db)
    await lease_repo.acquire(system_db, holder="host:1:abc", at=WALL - 1_000)
    report = await make_monitor(db=system_db, holder="host:1:abc").report()

    assert report.status is HealthStatus.OK
    assert report.http_status == 200
    assert report.ok is True

    body = report.as_dict()
    for key in (
        "status",
        "ok",
        "version",
        "uptime_s",
        "uptime",
        "degradations",
        "generated_at",
        "instance",
        "database",
        "conductor",
        "lease",
        "polling",
        "deliveries",
        "api_events",
        "api_event_stats",
        "unknown_content_types",
    ):
        assert key in body, key

    assert body["database"]["ok"] is True
    assert body["database"]["schema_version"] >= 1
    assert body["polling"]["bound_sessions"] == 1
    assert body["polling"]["sessions"][0]["session_id"] == "sess-1"
    assert body["deliveries"]["pending"] == 0
    assert body["conductor"] == {"available": False}
    assert json.loads(json.dumps(body))  # serialisable as-is


async def test_uptime_tracks_the_injected_clock(system_db: Database) -> None:
    clock = FakeClock(1_000.0)
    monitor = make_monitor(db=system_db, clock=clock)
    clock.advance(3_725.0)
    report = await monitor.report()
    assert report.uptime_s == pytest.approx(3_725.0)
    assert report.as_dict()["uptime"] == "1h02m"


async def test_summary_omits_everything_identifying(system_db: Database) -> None:
    await seed_session(system_db)
    report = await make_monitor(db=system_db, holder="host:1:abc").report()
    summary = report.summary()

    assert set(summary) == {
        "status",
        "ok",
        "version",
        "uptime_s",
        "uptime",
        "degradations",
        "generated_at",
    }
    blob = json.dumps(summary)
    assert "sess-1" not in blob
    assert "host:1:abc" not in blob


# ── degraded but alive: the whole point of the policy ────────────────────────


async def test_auth_fatal_is_degraded_but_still_200(
    system_db: Database, settings: Settings
) -> None:
    client = ConductorClient(
        api_key=FAKE_API_KEY,
        api_url=settings.conductor_api_url,
        transport=transport_returning(401),
        sleep=_no_sleep,
        max_attempts=1,
    )
    with pytest.raises(AuthFatal):
        await client.get_session_status("sess-1")
    await client.aclose()
    assert client.auth_failures == 1

    report = await make_monitor(db=system_db, client=client).report()

    # Alive: a restart cannot conjure a valid API key.
    assert report.status is HealthStatus.DEGRADED
    assert report.http_status == 200
    assert report.ok is True
    # Visible: the reason is in the body, not just the logs.
    assert report.has(DEGRADATION_AUTH_FATAL)
    detail = next(
        d.detail for d in report.degradations if d.code == DEGRADATION_AUTH_FATAL
    )
    assert "/key" in detail
    assert "tenant-a" in detail
    assert report.as_dict()["conductor"]["auth_failures"] == 1


async def test_a_stuck_telegram_poller_is_visible_and_still_200(
    system_db: Database,
) -> None:
    """A bot that reaches nothing on Telegram must not report a clean bill.

    ``run_polling`` retries forever with backoff, so without this the deploy is
    green, ``/health`` is ``ok``, and the bot answers nobody.
    """
    record = TelegramHealth()
    for _ in range(TELEGRAM_FAILURE_THRESHOLD):
        record.record_failure("TelegramNetworkError('timeout')")

    report = await make_monitor(db=system_db, telegram=record).report()

    assert report.status is HealthStatus.DEGRADED
    assert report.http_status == 200  # a restart cannot fix Telegram
    assert report.has(DEGRADATION_TELEGRAM)
    body = report.as_dict()
    assert body["telegram"]["polling_failures"] == TELEGRAM_FAILURE_THRESHOLD
    assert "timeout" in body["telegram"]["last_error"]


async def test_telegram_failures_below_the_threshold_are_not_a_degradation(
    system_db: Database,
) -> None:
    """A redeploy overlap costs a conflict or two; that is not an outage."""
    record = TelegramHealth()
    record.record_failure("conflict: terminated by other getUpdates")

    report = await make_monitor(db=system_db, telegram=record).report()

    assert report.status is HealthStatus.OK
    assert not report.has(DEGRADATION_TELEGRAM)


async def test_one_successful_bot_api_call_clears_the_telegram_degradation(
    system_db: Database,
) -> None:
    record = TelegramHealth()
    for _ in range(TELEGRAM_FAILURE_THRESHOLD + 2):
        record.record_failure("conflict")
    monitor = make_monitor(db=system_db, telegram=record)
    assert (await monitor.report(force=True)).has(DEGRADATION_TELEGRAM)

    record.record_ok()

    report = await monitor.report(force=True)
    assert not report.has(DEGRADATION_TELEGRAM)
    assert report.telegram == {
        "available": True,
        "polling_failures": 0,
        "calls_ok": 1,
        "last_error": "",
    }


def test_telegram_health_is_a_process_wide_record() -> None:
    """``bot.app`` writes it without importing anything from the monitor."""
    assert telegram_health() is telegram_health()


async def test_circuit_open_is_degraded_but_still_200(
    system_db: Database, settings: Settings
) -> None:
    client = ConductorClient(
        api_key=FAKE_API_KEY,
        api_url=settings.conductor_api_url,
        transport=transport_returning(500),
    )
    client.circuit.trip(45.0, "GET /sessions/{id}/status")
    report = await make_monitor(db=system_db, client=client).report()
    await client.aclose()

    assert report.status is HealthStatus.DEGRADED
    assert report.http_status == 200
    assert report.has(DEGRADATION_CIRCUIT_OPEN)
    assert report.as_dict()["conductor"]["circuit"]["state"] == "open"


async def test_rate_limiting_is_surfaced_for_tuning(
    system_db: Database, settings: Settings
) -> None:
    client = ConductorClient(
        api_key=FAKE_API_KEY,
        api_url=settings.conductor_api_url,
        transport=transport_returning(429, headers={"Retry-After": "1"}),
        sleep=_no_sleep,
        max_attempts=1,
    )
    with pytest.raises(RateLimited):
        await client.get_session_status("sess-1")
    await client.aclose()

    report = await make_monitor(db=system_db, client=client).report()
    assert report.http_status == 200
    assert report.has(DEGRADATION_RATE_LIMITED)
    assert report.as_dict()["conductor"]["rate_limited_recent"] >= 1


async def test_server_error_leaves_the_check_green(
    system_db: Database, settings: Settings
) -> None:
    """A 500 from Conductor is their outage, not ours: report, do not restart."""
    client = ConductorClient(
        api_key=FAKE_API_KEY,
        api_url=settings.conductor_api_url,
        transport=transport_returning(503),
        sleep=_no_sleep,
        max_attempts=1,
    )
    with pytest.raises(ApiError):
        await client.get_session_status("sess-1")
    await client.aclose()

    report = await make_monitor(db=system_db, client=client).report()
    assert report.http_status == 200
    assert report.as_dict()["conductor"]["failures"] >= 1


async def test_missing_client_is_not_a_failure(system_db: Database) -> None:
    report = await make_monitor(db=system_db).report()
    assert report.status is HealthStatus.OK
    assert report.conductor == {"available": False}


# ── a dead database is the one thing that fails the check ────────────────────


async def test_no_database_handle_is_down() -> None:
    report = await make_monitor().report()
    assert report.status is HealthStatus.DOWN
    assert report.http_status == 503
    assert report.ok is False
    assert report.has(DEGRADATION_DATABASE)
    assert report.database["ok"] is False


async def test_closed_database_is_down(pg_reset: object) -> None:
    db = await Database(worker_dsn(), system=True).connect()
    await db.close()
    report = await make_monitor(db=db).report()
    assert report.status is HealthStatus.DOWN
    assert report.http_status == 503
    assert report.database["ok"] is False
    assert report.database["error"]


async def test_unreadable_database_is_down(pg_reset: object) -> None:
    class Corrupt(Database):
        """Connected, but the server rejects everything we send it."""

        broken = False

        async def fetch_val(
            self, sql: str, params: Any = (), default: Any = None
        ) -> Any:
            if self.broken:
                raise DatabaseError("relation does not exist")
            return await super().fetch_val(sql, params, default)

    db = await Corrupt(worker_dsn(), system=True).connect()
    db.broken = True
    try:
        report = await make_monitor(db=db).report()
    finally:
        await db.close()
    assert report.status is HealthStatus.DOWN
    assert report.http_status == 503
    assert report.has(DEGRADATION_DATABASE)
    assert "DatabaseError" in report.database["error"]


async def test_wedged_database_times_out_rather_than_hanging(
    pg_reset: object,
) -> None:
    class Wedged(Database):
        """Connects normally, then stops answering — an exhausted pool."""

        wedged = False

        async def fetch_val(
            self, sql: str, params: Any = (), default: Any = None
        ) -> Any:
            if self.wedged:
                await asyncio.sleep(30)
            return await super().fetch_val(sql, params, default)

    db = await Wedged(worker_dsn(), system=True).connect()
    db.wedged = True
    try:
        report = await make_monitor(db=db, db_timeout_s=0.05).report()
    finally:
        await db.close()
    assert report.status is HealthStatus.DOWN
    assert report.database["error"] == "timeout"


async def test_a_raising_provider_is_down_not_an_exception() -> None:
    def boom() -> Database | None:
        raise RuntimeError("no database installed")

    monitor = HealthMonitor(database=boom, clients=lambda: None, cache_ttl_s=0.0)
    report = await monitor.report()
    assert report.status is HealthStatus.DOWN
    assert report.has(DEGRADATION_DATABASE)


# ── poll lag ─────────────────────────────────────────────────────────────────


async def test_fresh_poller_is_not_overdue(system_db: Database) -> None:
    await seed_session(system_db, at=WALL - 5_000, poll_interval_ms=20_000)
    report = await make_monitor(db=system_db).report()
    assert report.polling["overdue"] == 0
    assert report.polling["max_lag_ms"] == 5_000
    assert report.status is HealthStatus.OK


async def test_silent_poller_is_overdue(system_db: Database) -> None:
    await seed_session(system_db, at=WALL - 400_000, poll_interval_ms=120_000)
    report = await make_monitor(db=system_db).report()
    assert report.polling["overdue"] == 1
    assert report.polling["max_lag_session_id"] == "sess-1"
    assert report.has(DEGRADATION_POLL_LAG)
    assert report.http_status == 200  # the supervisor restarts pollers itself


async def test_fast_cadence_uses_the_grace_floor(system_db: Database) -> None:
    """A 3s QUEUED cadence must not go overdue after 9s — the floor is 30s."""
    await seed_session(system_db, at=WALL - 10_000, poll_interval_ms=3_000)
    report = await make_monitor(db=system_db).report()
    assert report.polling["overdue"] == 0


async def test_unbound_sessions_are_not_polled_and_not_counted(
    system_db: Database,
) -> None:
    await seed_session(system_db, "sess-idle", bound=False, at=WALL - 10_000_000)
    report = await make_monitor(db=system_db).report()
    assert report.polling["bound_sessions"] == 0
    assert report.polling["overdue"] == 0
    assert report.status is HealthStatus.OK


async def test_a_workspace_that_stopped_being_watched_is_counted(
    system_db: Database,
) -> None:
    """The number this report could not produce: *how many did I lose?*

    ``bound_sessions`` only ever says how many are watched. A session whose
    topic was deleted is unbound, still live, still costing money, and still
    something somebody is waiting on — and it left the report entirely. The
    sessions ``/attach`` upserts unbound are deliberately not counted: they
    were never watched, because a room is opened the first time it is used.
    """
    async with as_tenant():
        # One room each — `uq_sessions_one_per_room` is the reason that is not
        # a detail: two bound sessions cannot share a thread.
        for index, session_id in enumerate(("watched", "dropped", "never-opened")):
            await sessions_repo.upsert(
                system_db, session_id, chat_id=-100123, thread_id=7 + index
            )
        # Seeded means its cursor was placed: this one *was* being read.
        await sessions_repo.seek_to_end(
            system_db, "dropped", message_id="m-9", session_index=9
        )
        await sessions_repo.unbind(system_db, "dropped")
        await sessions_repo.unbind(system_db, "never-opened")

        report = await make_monitor(db=system_db).report()

    assert report.polling["bound_sessions"] == 1
    assert report.polling["unwatched"] == 1
    assert report.polling["unwatched_sessions"] == ["dropped"]


# ── deliveries ───────────────────────────────────────────────────────────────


async def test_delivery_counts_and_backlog(system_db: Database) -> None:
    async with as_tenant():
        await seed_session(system_db)
        for index in range(DELIVERY_BACKLOG + 2):
            await deliveries_repo.enqueue(
                system_db,
                session_id="sess-1",
                message_id=f"msg-{index}",
                chat_id=-100123,
                thread_id=7,
                session_index=index,
                payload_json=json.dumps({"html": "hi"}),
            )
        report = await make_monitor(db=system_db).report()
        assert report.deliveries["pending"] == DELIVERY_BACKLOG + 2
        assert report.deliveries["by_state"]["pending"] == DELIVERY_BACKLOG + 2
        assert report.has(DEGRADATION_DELIVERY_BACKLOG)
        assert report.http_status == 200


async def test_failed_deliveries_are_degradations(system_db: Database) -> None:
    async with as_tenant():
        await seed_session(system_db)
        await deliveries_repo.enqueue(
            system_db, session_id="sess-1", message_id="m1", chat_id=-100123
        )
        await deliveries_repo.mark_failed(
            system_db, ("sess-1", "m1", 0, -100123), error="entity parse", retry=False
        )
        report = await make_monitor(db=system_db).report()
        assert report.deliveries["failed"] == 1
        assert report.has(DEGRADATION_DELIVERY_FAILED)


async def test_one_message_stuck_for_an_hour_is_a_degradation(
    system_db: Database,
) -> None:
    """The signal a depth threshold cannot give.

    A backlog of 50 was the only alarm the report had, and the failure people
    actually hit is *one* row: an answer behind a wedged hold, a single pending
    delivery against a threshold of fifty. To the person waiting it is the bot
    going quiet, and to this report it was ``ok``.
    """
    async with as_tenant():
        await seed_session(system_db)
        await deliveries_repo.enqueue(
            system_db,
            session_id="sess-1",
            message_id="m1",
            chat_id=-100123,
            payload_json=json.dumps({"html": "hi"}),
            at=WALL - DELIVERY_STALL_MS - 1_000,
        )
        report = await make_monitor(db=system_db).report()

        assert report.deliveries["pending"] == 1
        assert not report.has(DEGRADATION_DELIVERY_BACKLOG), "one row is not a backlog"
        assert report.has(DEGRADATION_DELIVERY_STALLED)
        assert report.deliveries["oldest_pending_ms"] >= DELIVERY_STALL_MS


async def test_a_claim_nobody_resolved_is_reported_separately(
    system_db: Database,
) -> None:
    """A stranded claim is a different fault from a queue that will not drain.

    ``sending`` rows are invisible to the pending count, so a worker that died
    between Telegram and the database took its row out of every number this
    report had. Recovery should have re-sent it; saying so names the thing that
    did not happen.
    """
    async with as_tenant():
        await seed_session(system_db)
        await deliveries_repo.enqueue(
            system_db, session_id="sess-1", message_id="m1", chat_id=-100123
        )
        await deliveries_repo.claim(
            system_db, claim_id="dead-worker", at=WALL - DELIVERY_STRANDED_MS - 1_000
        )
        report = await make_monitor(db=system_db).report()

        assert report.deliveries["sending"] == 1
        assert report.has(DEGRADATION_DELIVERY_STRANDED)


async def test_an_old_terminal_failure_stops_being_news(system_db: Database) -> None:
    """A permanent refusal is real once, not for a week.

    One bot kicked from one group pinned the whole report to ``degraded`` until
    retention dropped the row — and a health check that is always amber is a
    health check nobody reads. The count stays in the body; only the alarm is
    windowed.
    """
    async with as_tenant():
        await seed_session(system_db)
        await deliveries_repo.enqueue(
            system_db, session_id="sess-1", message_id="m1", chat_id=-100123
        )
        await deliveries_repo.mark_failed(
            system_db,
            ("sess-1", "m1", 0, -100123),
            error="bot was kicked",
            retry=False,
            at=WALL - DELIVERY_FAILED_WINDOW_MS - 1_000,
        )
        report = await make_monitor(db=system_db).report()

        assert report.deliveries["failed"] == 1
        assert report.deliveries["failed_recent"] == 0
        assert not report.has(DEGRADATION_DELIVERY_FAILED)
        assert report.status is HealthStatus.OK


# ── the singleton lease ──────────────────────────────────────────────────────


async def test_lease_held_by_this_instance(system_db: Database) -> None:
    holder = "host:1:abc"
    await lease_repo.acquire(system_db, holder=holder, at=WALL - 1_000)
    report = await make_monitor(db=system_db, holder=holder).report()
    assert report.lease["holder"] == holder
    assert report.lease["is_me"] is True
    assert report.lease["held"] is True
    assert not report.has(DEGRADATION_LEASE_LOST)


async def test_lease_held_by_another_instance_is_degraded(system_db: Database) -> None:
    await lease_repo.acquire(system_db, holder="other:9:zzz", at=WALL - 1_000)
    report = await make_monitor(db=system_db, holder="host:1:abc").report()
    assert report.lease["is_me"] is False
    assert report.has(DEGRADATION_LEASE_LOST)
    assert report.http_status == 200  # racing a restart would be worse


async def test_unheld_lease_without_a_holder_is_not_a_degradation(
    system_db: Database,
) -> None:
    report = await make_monitor(db=system_db).report()
    assert report.lease == {
        "name": lease_repo.SUPERVISOR,
        "holder": None,
        "held": False,
    }
    assert report.status is HealthStatus.OK


async def test_set_holder_starts_reporting_the_lease(system_db: Database) -> None:
    monitor = make_monitor(db=system_db)
    assert monitor.holder is None
    monitor.set_holder("host:1:abc")
    report = await monitor.report()
    assert monitor.holder == "host:1:abc"
    assert report.has(DEGRADATION_LEASE_LOST)


# ── api_events and unknown content types ─────────────────────────────────────


async def test_last_twenty_api_events_newest_first(system_db: Database) -> None:
    for index in range(API_EVENT_LIMIT + 5):
        await events_repo.record_api_event(
            system_db,
            method="GET",
            endpoint="/sessions/{id}/messages",
            status_code=200,
            duration_ms=index,
            ok=True,
            at=WALL - 1_000 + index,
        )
    report = await make_monitor(db=system_db).report()
    assert len(report.api_events) == API_EVENT_LIMIT
    assert report.api_events[0]["duration_ms"] == API_EVENT_LIMIT + 4
    assert report.api_events[0]["endpoint"] == "/sessions/{id}/messages"
    assert report.api_events[0]["ms_ago"] >= 0
    assert report.as_dict()["api_event_stats"]["total"] == API_EVENT_LIMIT + 5


async def test_unknown_content_types_are_reported(system_db: Database) -> None:
    async with as_tenant():
        await events_repo.note_unknown_content_type(
            system_db,
            content_type="futureEvent",
            signature="deadbeefdeadbeef",
            session_id="sess-1",
            message_id="sess-1:9:0",
            at=WALL - 500,
        )
        report = await make_monitor(db=system_db).report()
        assert report.unknown_content_types[0]["type"] == "futureEvent"
        assert report.unknown_content_types[0]["count"] == 1
        # An unknown shape is data to look at, not a reason to restart.
        assert report.status is HealthStatus.OK


async def test_the_body_is_scrubbed(system_db: Database) -> None:
    await events_repo.record_api_event(
        system_db,
        method="POST",
        endpoint="/sessions/{id}/messages",
        status_code=401,
        ok=False,
        error="401 for Authorization: Bearer sk-live-abcdef0123456789",
        at=WALL - 10,
    )
    body = (await make_monitor(db=system_db).report()).as_dict()
    assert "sk-live-abcdef0123456789" not in json.dumps(body)


# ── caching ──────────────────────────────────────────────────────────────────


async def test_reports_are_cached_within_the_ttl(system_db: Database) -> None:
    clock = FakeClock(1_000.0)
    monitor = HealthMonitor(
        database=lambda: system_db,
        clients=lambda: None,
        clock=clock,
        wall_clock=lambda: WALL,
        cache_ttl_s=2.0,
    )
    first = await monitor.report()
    assert await monitor.report() is first
    assert await monitor.report(force=True) is not first
    clock.advance(3.0)
    assert await monitor.report() is not first


# ── the HTTP surface ─────────────────────────────────────────────────────────


async def test_http_health_returns_200_with_detail_on_loopback(
    system_db: Database, running_server: Callable[..., Any]
) -> None:
    await seed_session(system_db)
    server = await running_server(make_monitor(db=system_db))
    status, body = await get_json(server)
    assert status == 200
    assert body["status"] == "ok"
    assert body["polling"]["bound_sessions"] == 1  # loopback gets the detail


async def test_http_health_head_is_allowed(
    system_db: Database, running_server: Callable[..., Any]
) -> None:
    server = await running_server(make_monitor(db=system_db))
    async with httpx.AsyncClient() as http:
        response = await http.head(f"http://127.0.0.1:{server.port}{HEALTH_PATH}")
    assert response.status_code == 200


async def test_http_health_503_when_the_database_is_gone(
    running_server: Callable[..., Any],
) -> None:
    server = await running_server(make_monitor())
    status, body = await get_json(server)
    assert status == 503
    assert body["ok"] is False
    assert DEGRADATION_DATABASE in json.dumps(body)


async def test_http_degraded_state_is_visible_at_200(
    system_db: Database, settings: Settings, running_server: Callable[..., Any]
) -> None:
    client = ConductorClient(
        api_key=FAKE_API_KEY,
        api_url=settings.conductor_api_url,
        transport=transport_returning(401),
        sleep=_no_sleep,
        max_attempts=1,
    )
    with pytest.raises(AuthFatal):
        await client.get_session_status("sess-1")
    server = await running_server(make_monitor(db=system_db, client=client))
    status, body = await get_json(server)
    await client.aclose()

    assert status == 200
    assert body["status"] == "degraded"
    assert DEGRADATION_AUTH_FATAL in [d["code"] for d in body["degradations"]]


async def test_http_token_gate_hides_detail(
    system_db: Database, running_server: Callable[..., Any]
) -> None:
    await seed_session(system_db)
    server = await running_server(make_monitor(db=system_db), token="s3cret-token")

    status, body = await get_json(server)
    assert status == 200
    assert "polling" not in body  # loopback is not enough once a token is set
    assert body["degradations"] == []

    status, body = await get_json(server, params={"token": "s3cret-token"})
    assert status == 200
    assert body["polling"]["bound_sessions"] == 1

    status, body = await get_json(
        server, headers={"Authorization": "Bearer s3cret-token"}
    )
    assert body["polling"]["bound_sessions"] == 1

    status, body = await get_json(server, params={"token": "wrong"})
    assert status == 200
    assert "polling" not in body


async def test_http_unknown_path_is_404(
    system_db: Database, running_server: Callable[..., Any]
) -> None:
    server = await running_server(make_monitor(db=system_db))
    status, _ = await get_json(server, "/")
    assert status == 404


async def test_custom_path_is_honoured(
    system_db: Database, running_server: Callable[..., Any]
) -> None:
    server = await running_server(make_monitor(db=system_db), path="/healthz")
    status, body = await get_json(server, "/healthz")
    assert status == 200
    assert body["status"] == "ok"


async def test_app_exposes_the_monitor(system_db: Database) -> None:
    monitor = make_monitor(db=system_db)
    app = create_app(monitor)
    assert app[MONITOR_KEY] is monitor


async def test_handler_survives_a_broken_monitor(
    running_server: Callable[..., Any],
) -> None:
    class Exploding(HealthMonitor):
        async def report(self, *, force: bool = False) -> HealthReport:
            raise RuntimeError("collector is broken")

    server = await running_server(
        Exploding(database=lambda: None, clients=lambda: None, cache_ttl_s=0.0)
    )
    status, body = await get_json(server)
    assert status == 503
    assert body["ok"] is False


async def test_server_lifecycle_frees_the_port(system_db: Database) -> None:
    monitor = make_monitor(db=system_db)
    server = HealthServer(monitor, host="127.0.0.1", port=0)
    port = await server.start()
    assert port > 0 and server.port == port
    with pytest.raises(RuntimeError):
        await server.start()
    await server.stop()
    assert server.port is None

    # The port is genuinely released: a second server can take it.
    async with HealthServer(monitor, host="127.0.0.1", port=port) as second:
        assert second.port == port


async def test_serve_health_runs_until_cancelled(system_db: Database) -> None:
    monitor = make_monitor(db=system_db)
    task = asyncio.create_task(serve_health(monitor, host="127.0.0.1", port=0))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── the detail gate, as pure functions ───────────────────────────────────────


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.1:54321", True),
        ("[::1]", True),
        ("::1", True),
        ("10.0.0.4", True),
        ("192.168.1.9", True),
        ("169.254.1.1", True),
        ("8.8.8.8", False),
        ("2606:4700::1111", False),
        ("not-an-ip", False),
        (None, False),
        ("", False),
    ],
)
def test_is_local_remote(remote: str | None, expected: bool) -> None:
    assert is_local_remote(remote) is expected


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.1:54321", True),
        ("[::1]", True),
        ("::1", True),
        ("10.0.0.4", False),
        ("fd12::1", False),
        ("8.8.8.8", False),
        (None, False),
    ],
)
def test_is_loopback_remote(remote: str | None, expected: bool) -> None:
    assert is_loopback_remote(remote) is expected


def test_detail_allowed_policy() -> None:
    # No token configured: loopback only. A private peer is NOT trusted —
    # Railway's edge reaches the container over the project's private network,
    # so `is_local_remote` would publish session ids on a public domain.
    assert detail_allowed(remote="127.0.0.1", expected_token=None, provided_token=None)
    assert not detail_allowed(
        remote="10.0.1.5", expected_token=None, provided_token=None
    )
    assert not detail_allowed(
        remote="fd12::1", expected_token=None, provided_token=None
    )
    assert not detail_allowed(
        remote="8.8.8.8", expected_token=None, provided_token=None
    )
    # Token configured: the token is the only key, even from loopback.
    assert not detail_allowed(
        remote="127.0.0.1", expected_token="abc", provided_token=None
    )
    assert not detail_allowed(
        remote="127.0.0.1", expected_token="abc", provided_token="ab"
    )
    assert detail_allowed(remote="8.8.8.8", expected_token="abc", provided_token="abc")


def test_health_port_reads_railways_port_var() -> None:
    assert health_port({"PORT": "8123"}) == 8123
    assert health_port({"HEALTH_PORT": "9000"}) == 9000
    assert health_port({"PORT": "", "HEALTH_PORT": "9000"}) == 9000
    assert health_port({"PORT": "not-a-port"}) == 8080
    assert health_port({"PORT": "70000"}) == 8080
    assert health_port({}) == 8080


def test_health_token_reads_the_env() -> None:
    assert health_token({"HEALTH_TOKEN": " abc "}) == "abc"
    assert health_token({"HEALTH_TOKEN": ""}) is None
    assert health_token({}) is None


async def test_default_database_provider_follows_the_installed_handle(
    db: Database,
) -> None:
    assert default_database_provider() is db
    set_database(None)
    assert default_database_provider() is None
    set_database(db)


def test_default_pool_provider_degrades_instead_of_raising() -> None:
    """Before boot there is no pool; the health surface must still answer."""
    from ctb.runtime import set_client_pool

    set_client_pool(None)
    reset_settings()
    assert default_pool_provider() is None


# ── the /health command rendering ────────────────────────────────────────────


async def test_format_health_html_is_telegram_safe(
    system_db: Database, settings: Settings
) -> None:
    async with as_tenant():
        await seed_session(system_db, at=WALL - 400_000, poll_interval_ms=120_000)
        await events_repo.record_api_event(
            system_db,
            method="GET",
            endpoint="/sessions/{id}/status",
            status_code=200,
            duration_ms=42,
            at=WALL - 100,
        )
        await events_repo.note_unknown_content_type(
            system_db, content_type="a<b>c", signature="ffff0000ffff0000", at=WALL - 100
        )
        client = ConductorClient(
            api_key=FAKE_API_KEY,
            api_url=settings.conductor_api_url,
            transport=transport_returning(401),
            sleep=_no_sleep,
            max_attempts=1,
        )
        with pytest.raises(AuthFatal):
            await client.get_session_status("sess-1")
        await client.aclose()

        text = format_health_html(
            await make_monitor(db=system_db, client=client).report()
        )

        assert "<b>degraded</b>" in text
        assert DEGRADATION_AUTH_FATAL in text
        assert "circuit <code>closed</code>" in text
        assert "1 bound · 1 overdue" in text
        assert "/sessions/{id}/status" in text
        assert "a&lt;b&gt;c" in text and "<b>c" not in text
        assert "MarkdownV2" not in text


async def test_format_health_html_mentions_telegram_only_when_it_is_failing(
    system_db: Database,
) -> None:
    """One phone screen: the healthy case says nothing about Telegram."""
    quiet = format_health_html(await make_monitor(db=system_db).report())
    assert "telegram" not in quiet

    record = TelegramHealth()
    for _ in range(TELEGRAM_FAILURE_THRESHOLD):
        record.record_failure("conflict: <other> instance")
    text = format_health_html(
        await make_monitor(db=system_db, telegram=record).report()
    )

    assert f"telegram: {TELEGRAM_FAILURE_THRESHOLD} failed polls" in text
    assert "&lt;other&gt;" in text and "<other>" not in text


def test_format_health_html_of_an_empty_report() -> None:
    text = format_health_html(HealthReport())
    assert text.startswith("✅ <b>ok</b>")
    assert "unheld" in text


def test_a_degraded_health_line_always_says_why() -> None:
    """The live report read "degraded" above five lines that were all fine.

    ``Degradation`` has carried human-facing ``detail`` all along; the compact
    ``/health`` printed the verdict and dropped the reason, which is the one
    part the owner can act on.
    """
    from ctb.bot.handlers.admin import _voice_line

    report = HealthReport(
        status=HealthStatus.DEGRADED,
        degradations=(
            Degradation(code=DEGRADATION_POLL_LAG, detail="2 sessions overdue"),
            Degradation(code=DEGRADATION_CIRCUIT_OPEN),
        ),
    )

    why = " · ".join(
        item.detail or item.code.replace("_", " ") for item in report.degradations
    )

    assert why == "2 sessions overdue · circuit open"
    assert "_" not in why, "codes are prose here, not identifiers"
    # And a healthy report contributes no reason line at all.
    assert HealthReport(status=HealthStatus.OK).degradations == ()
    # Voice stays silent until it has something to report.
    assert _voice_line({}, retention_days=7) == ""
    assert _voice_line({"pending": 1, "transcribing": 1}, retention_days=7) == (
        "🎙 voice · 1 waiting · 1 transcribing"
    )


def test_health_answers_what_where_when_and_what_next() -> None:
    """The old report named a fault and answered none of the questions.

    "2 deliveries exhausted their retries" did not say which, when, why, or
    whether it would ever clear — and because failed rows had no retention, it
    would have said it forever.
    """
    from ctb.bot.handlers.admin import _ago, _delivery_line, _verdict

    now = 1_000_000_000
    line = _delivery_line(
        {
            "count": 2,
            "newest_ms": now - 11 * 60 * 1000,
            "reason": "Forbidden: bot was blocked by the user",
        },
        now_ms_=now,
    )

    assert "2 replies never sent" in line  # what
    assert "11m ago" in line  # when
    assert "blocked by the user" in line  # why
    assert "clears itself" in line  # what next
    assert "exhausted" not in line, "jargon for a thing that already happened"

    # The verdict answers "can I still use it", not "what is the process state".
    assert _verdict("ok", False, False).startswith("✅")
    assert "nothing needs you" in _verdict("ok", False, False)
    assert "clearing on their own" in _verdict("degraded", True, False)
    assert "needs you" in _verdict("degraded", True, True)
    assert _verdict("down", True, False).startswith("🚫")
    # Silence when there is nothing to report.
    assert _delivery_line({"count": 0}, now_ms_=now) == ""
    assert _ago(0, now_ms_=now) == "unknown"


class TestHealthTokenComparison:
    """The token arrives off the wire, so every byte sequence must be a no."""

    TOKEN = "ae4da669cb67edde9845797ffdd71638"

    def test_the_right_token_opens_the_detailed_body(self) -> None:
        assert detail_allowed(
            remote="1.2.3.4", expected_token=self.TOKEN, provided_token=self.TOKEN
        )

    @pytest.mark.parametrize(
        "provided",
        ["", "wrong", "é", "🔑", "ae4da669cb67edde9845797ffdd7163", "\udcff"],
        ids=["empty", "wrong", "accent", "emoji", "prefix", "lone-surrogate"],
    )
    def test_anything_else_is_refused_without_raising(self, provided: str) -> None:
        """`compare_digest` raises TypeError on non-ASCII `str`.

        That made `GET /health?token=é` a 500 from any unauthenticated caller —
        the gate is evaluated before the handler's try/except, so it took the
        whole response with it rather than falling back to the summary.
        """
        assert not detail_allowed(
            remote="1.2.3.4", expected_token=self.TOKEN, provided_token=provided
        )

    def test_no_token_configured_falls_back_to_loopback(self) -> None:
        assert detail_allowed(
            remote="127.0.0.1", expected_token=None, provided_token=None
        )
        assert not detail_allowed(
            remote="8.8.8.8", expected_token=None, provided_token=None
        )
