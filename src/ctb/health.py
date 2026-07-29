"""The health surface: ``GET /health`` for Railway, and the ``/health`` command.

``railway.toml`` sets ``healthcheckPath = "/health"``, so this server decides
whether a deploy is promoted and whether a running instance is restarted. That
makes the definition of *unhealthy* a policy decision, not a formality:

**Only a dead database fails the check.** A restart re-runs the boot path —
open both pools, verify the schema, take the lease, spawn pollers — so
a restart is a plausible fix for exactly one class of failure: the process
cannot reach its own state. Everything else is reported in the body at HTTP
200, because a restart loop would make it *worse*:

* **auth fatal (401).** The supervisor stops *that tenant's* pollers and
  keeps the bot alive on purpose — everyone else is unaffected. Cycling the
  process cannot conjure a valid key; it would only lose the in-memory state
  that lets the owner see *why* their workspace went quiet. Reported as the
  ``auth_fatal`` degradation, HTTP 200.
* **circuit open / rate limited.** Conductor is down or throttling us. A
  restart re-opens the circuit against the same unhealthy upstream and drops
  the backoff window. Reported, HTTP 200.
* **poll lag, delivery backlog, lost lease.** All self-healing: the supervisor
  restarts crashed pollers, the outbox drains, the lease expires in 15s. A
  restart that races another instance is strictly worse. Reported, HTTP 200.

The body is the same data the admin ``/health`` command prints (PLAN.md
§Commands): poll lag, circuit state, the last 20 ``api_events``, pending
deliveries, unknown content types, the lease holder, and uptime.

Because a Railway service can also be exposed on a public domain, detail is
gated: full JSON goes to anyone presenting ``HEALTH_TOKEN``, or — when no token
is configured — to a *loopback* peer only. Private addresses do not count:
Railway's edge reaches the container over the project's private IPv6 network,
so "the peer is a private address" would hand the whole body (DB path, session
ids, recent API calls) to the public internet. Everyone else gets a status-only
summary. The status code is identical either way, so the healthcheck never
depends on the gate. Every detail body additionally goes through the log
scrubber.

Nothing here imports the bot, the poller or the outbox — the monitor is fed
providers, so ``__main__`` can start it inside its ``TaskGroup`` before (or
without) any of them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from secrets import compare_digest
from types import TracebackType
from typing import Any, Final, Self

from aiohttp import web

from ctb import __version__
from ctb.conductor.pool import ClientPool
from ctb.db.connection import Database, get_database, now_ms
from ctb.db.migrate import current_schema_version
from ctb.db.repo import deliveries as deliveries_repo
from ctb.db.repo import events as events_repo
from ctb.db.repo import lease as lease_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import voice_inputs as voice_repo
from ctb.delivery.render.html import escape
from ctb.logging import get_logger, scrub_secrets
from ctb.runtime import client_pool, system_database
from ctb.turn.machine import format_duration

log = get_logger(__name__)

# ── knobs ────────────────────────────────────────────────────────────────────

#: Railway injects ``PORT``. 8080 is the fallback for local runs.
DEFAULT_PORT: Final[int] = 8080
HEALTH_PATH: Final[str] = "/health"
PORT_ENV_VARS: Final[tuple[str, ...]] = ("PORT", "HEALTH_PORT")
TOKEN_ENV_VAR: Final[str] = "HEALTH_TOKEN"
TOKEN_HEADER: Final[str] = "X-Health-Token"

#: An exhausted pool must fail the check rather than hang Railway's probe.
DB_TIMEOUT_S: Final[float] = 5.0
#: The endpoint may be public; collecting a report costs ~6 queries.
CACHE_TTL_S: Final[float] = 2.0

API_EVENT_LIMIT: Final[int] = 20
UNKNOWN_TYPE_LIMIT: Final[int] = 10
SESSION_DETAIL_LIMIT: Final[int] = 25
STATS_WINDOW_MS: Final[int] = 15 * 60_000

#: A session is *overdue* when it has been silent for this multiple of its own
#: cadence. The floor absorbs a 3s QUEUED cadence plus one slow round trip.
POLL_LAG_FACTOR: Final[float] = 3.0
POLL_LAG_GRACE_MS: Final[int] = 30_000
#: Deliveries drain in seconds; a standing queue this deep means a stuck outbox.
DELIVERY_BACKLOG: Final[int] = 50


class HealthStatus(StrEnum):
    """``down`` is the only value that fails the Railway healthcheck."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"

    @property
    def http_status(self) -> int:
        return 503 if self is HealthStatus.DOWN else 200

    @property
    def is_serving(self) -> bool:
        """True when the process can still do useful work (possibly reduced)."""
        return self is not HealthStatus.DOWN


# Degradation codes. Stable strings — alerts and the /health command match on
# them, so they are part of the public API.
DEGRADATION_DATABASE: Final[str] = "database_unavailable"
DEGRADATION_AUTH_FATAL: Final[str] = "auth_fatal"
DEGRADATION_CIRCUIT_OPEN: Final[str] = "circuit_open"
DEGRADATION_RATE_LIMITED: Final[str] = "rate_limited"
DEGRADATION_POLL_LAG: Final[str] = "poll_lag"
DEGRADATION_DELIVERY_BACKLOG: Final[str] = "delivery_backlog"
DEGRADATION_DELIVERY_FAILED: Final[str] = "delivery_failed"
DEGRADATION_LEASE_LOST: Final[str] = "lease_lost"
DEGRADATION_TELEGRAM: Final[str] = "telegram_polling"

#: Consecutive long-poll restarts before the poller counts as *stuck*. aiogram
#: retries inside its own reader, so anything that reaches this counter already
#: escaped that; three in a row is no longer a redeploy overlap.
TELEGRAM_FAILURE_THRESHOLD: Final[int] = 3


@dataclass(frozen=True, slots=True)
class Degradation:
    """One named thing that is wrong. ``detail`` is human-facing prose."""

    code: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthReport:
    """One snapshot. Immutable so it can be cached and shared."""

    status: HealthStatus = HealthStatus.OK
    generated_at: int = 0
    uptime_s: float = 0.0
    version: str = __version__
    instance: str = ""
    degradations: tuple[Degradation, ...] = ()
    database: dict[str, Any] = field(default_factory=dict)
    conductor: dict[str, Any] = field(default_factory=dict)
    telegram: dict[str, Any] = field(default_factory=dict)
    lease: dict[str, Any] = field(default_factory=dict)
    polling: dict[str, Any] = field(default_factory=dict)
    deliveries: dict[str, Any] = field(default_factory=dict)
    voice: dict[str, Any] = field(default_factory=dict)
    api_events: tuple[dict[str, Any], ...] = ()
    api_event_stats: dict[str, Any] = field(default_factory=dict)
    unknown_content_types: tuple[dict[str, Any], ...] = ()

    @property
    def http_status(self) -> int:
        return self.status.http_status

    @property
    def ok(self) -> bool:
        """The healthcheck verdict — *not* "nothing is wrong"."""
        return self.status.is_serving

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(d.code for d in self.degradations)

    def has(self, code: str) -> bool:
        return any(d.code == code for d in self.degradations)

    def summary(self) -> dict[str, Any]:
        """The unauthenticated body: verdict and nothing identifying."""
        return {
            "status": str(self.status),
            "ok": self.ok,
            "version": self.version,
            "uptime_s": round(self.uptime_s, 3),
            "uptime": format_duration(int(self.uptime_s * 1000)),
            "degradations": list(self.codes),
            "generated_at": self.generated_at,
        }

    def as_dict(self, *, detail: bool = True) -> dict[str, Any]:
        """Full JSON body. Always scrubbed — this can be served over the wire."""
        if not detail:
            return self.summary()
        body: dict[str, Any] = self.summary()
        body["degradations"] = [d.as_dict() for d in self.degradations]
        body["instance"] = self.instance
        body["database"] = self.database
        body["conductor"] = self.conductor
        body["telegram"] = self.telegram
        body["lease"] = self.lease
        body["polling"] = self.polling
        body["deliveries"] = self.deliveries
        body["voice"] = self.voice
        body["api_events"] = list(self.api_events)
        body["api_event_stats"] = self.api_event_stats
        body["unknown_content_types"] = list(self.unknown_content_types)
        return dict(scrub_secrets(event_dict=body))


# ── Telegram liveness ────────────────────────────────────────────────────────


@dataclass(slots=True)
class TelegramHealth:
    """Bot API liveness, written by ``ctb.bot.app`` and read here.

    Without it a broken Telegram side is invisible: ``run_polling`` retries
    forever with backoff, so a deploy whose bot answers nothing still reports
    ``ok``. ``failures`` counts polling restarts that escaped aiogram's own
    reader; *any* successful Bot API call (a ``getUpdates`` that returns
    included) clears it, so a redeploy conflict self-heals.

    Kept here, not in the bot, so ``health.py`` still imports nothing from it.
    """

    failures: int = 0
    calls_ok: int = 0
    last_error: str = ""

    def record_ok(self) -> None:
        self.calls_ok += 1
        self.failures = 0
        self.last_error = ""

    def record_failure(self, error: str) -> None:
        self.failures += 1
        self.last_error = error

    def snapshot(self) -> dict[str, Any]:
        return {
            "polling_failures": self.failures,
            "calls_ok": self.calls_ok,
            "last_error": self.last_error,
        }


_telegram_health = TelegramHealth()


def telegram_health() -> TelegramHealth:
    """The process-wide record. ``app.py`` writes it; the monitor reads it."""
    return _telegram_health


def reset_telegram_health() -> None:
    """Drop the recorded state (tests, and a fresh boot in the same process)."""
    _telegram_health.failures = 0
    _telegram_health.calls_ok = 0
    _telegram_health.last_error = ""


# ── providers ────────────────────────────────────────────────────────────────

type DatabaseProvider = Callable[[], Database | None]
type PoolProvider = Callable[[], ClientPool | None]
type TelegramProvider = Callable[[], TelegramHealth | None]


def _pool_stats(db: Database) -> dict[str, Any]:
    """Pool counters. Exhaustion is the new deadlock class; make it visible."""
    try:
        stats = db.stats()
    except Exception:  # pragma: no cover - stats are best effort
        return {}
    return {
        "pool_size": stats.get("pool_size", 0),
        "pool_available": stats.get("pool_available", 0),
        "pool_waiting": stats.get("requests_waiting", 0),
    }


def default_database_provider() -> Database | None:
    """The process-wide database, or ``None`` before boot installed one."""
    try:
        db = get_database()
    except RuntimeError:
        return None
    return db if db.is_connected else None


def default_database_provider_system() -> Database | None:
    """The worker pool, which is what the cross-tenant sections read."""
    try:
        db = system_database()
    except RuntimeError:
        return None
    return db if db.is_connected else None


def default_pool_provider() -> ClientPool | None:
    """The process-wide client pool, or ``None`` before boot installed one."""
    try:
        return client_pool()
    except RuntimeError:
        return None


# ── the monitor ──────────────────────────────────────────────────────────────


class HealthMonitor:
    """Collects a :class:`HealthReport`. Never raises; failures become fields.

    Everything it reads is injected, so it neither imports nor starts the
    subsystems it reports on. ``holder`` is this process's singleton-lease
    token (``repo.lease.instance_id()``); pass it once the supervisor has one
    and the report will flag a lease stolen by another instance.
    """

    __slots__ = (
        "_cache",
        "_cache_ttl",
        "_cached_at",
        "_pool_provider",
        "_clock",
        "_db_provider",
        "_db_timeout",
        "_holder",
        "_instance",
        "_lock",
        "_started_at",
        "_telegram_provider",
        "_wall_clock",
    )

    def __init__(
        self,
        *,
        database: DatabaseProvider = default_database_provider,
        clients: PoolProvider = default_pool_provider,
        telegram: TelegramProvider = telegram_health,
        holder: str | None = None,
        instance: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], int] = now_ms,
        started_at: float | None = None,
        cache_ttl_s: float = CACHE_TTL_S,
        db_timeout_s: float = DB_TIMEOUT_S,
    ) -> None:
        self._db_provider = database
        self._pool_provider = clients
        self._telegram_provider = telegram
        self._holder = holder
        self._instance = instance if instance is not None else (holder or "")
        self._clock = clock
        self._wall_clock = wall_clock
        self._started_at = clock() if started_at is None else started_at
        self._cache_ttl = max(0.0, cache_ttl_s)
        self._db_timeout = db_timeout_s
        self._cache: HealthReport | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    # -- identity -------------------------------------------------------------

    @property
    def holder(self) -> str | None:
        return self._holder

    def set_holder(self, holder: str | None) -> None:
        """Called once the supervisor wins (or releases) the singleton lease."""
        self._holder = holder
        if holder and not self._instance:
            self._instance = holder

    @property
    def uptime_s(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    # -- collection -----------------------------------------------------------

    async def report(self, *, force: bool = False) -> HealthReport:
        """A report, at most ``cache_ttl_s`` old. Concurrent callers share one."""
        async with self._lock:
            cached = self._cache
            if (
                not force
                and cached is not None
                and self._clock() - self._cached_at < self._cache_ttl
            ):
                return cached
            report = await self._collect()
            self._cache = report
            self._cached_at = self._clock()
            return report

    async def _collect(self) -> HealthReport:
        degradations: list[Degradation] = []
        conductor = self._collect_conductor(degradations)
        telegram = self._collect_telegram(degradations)
        db_section, sections = await self._collect_database(degradations)
        status = (
            HealthStatus.DOWN
            if not db_section.get("ok", False)
            else HealthStatus.DEGRADED
            if degradations
            else HealthStatus.OK
        )
        return HealthReport(
            status=status,
            generated_at=self._wall_clock(),
            uptime_s=self.uptime_s,
            instance=self._instance,
            degradations=tuple(degradations),
            database=db_section,
            conductor=conductor,
            telegram=telegram,
            lease=sections.get("lease", {}),
            polling=sections.get("polling", {}),
            deliveries=sections.get("deliveries", {}),
            voice=sections.get("voice", {}),
            api_events=tuple(sections.get("api_events", ())),
            api_event_stats=sections.get("api_event_stats", {}),
            unknown_content_types=tuple(sections.get("unknown_content_types", ())),
        )

    # -- Conductor client (in-memory, cannot block) ---------------------------

    def _collect_conductor(self, degradations: list[Degradation]) -> dict[str, Any]:
        """Aggregate across every live tenant client.

        There is no single Conductor client any more, so this reports the pool
        plus the *worst* state any tenant is in. Naming which tenant is
        deliberate — an operator needs to know who to contact — and safe,
        because a slug is not identifying the way a name or a key would be.
        """
        pool = _call_safely(self._pool_provider)
        if pool is None:
            return {"available": False}
        try:
            section: dict[str, Any] = dict(pool.health())
            clients = pool.clients()
        except Exception as exc:  # pragma: no cover - health() is pure reads
            return {"available": False, "error": _reason(exc)}
        section["available"] = True

        throttled = 0
        recent_total = 0
        auth_failures = 0
        failures_total = 0
        requests_total = 0
        retries_total = 0
        open_circuits: list[str] = []
        rejected: list[str] = []
        for slug, client in clients:
            try:
                stats: Mapping[str, Any] = client.health()
                recent: tuple[Any, ...] = client.recent_events()
            except Exception:  # pragma: no cover - pure reads
                continue
            recent_total += len(recent)
            throttled += sum(1 for event in recent if event.status_code == 429)
            failures_total += int(stats.get("failures", 0) or 0)
            requests_total += int(stats.get("requests", 0) or 0)
            retries_total += int(stats.get("retries", 0) or 0)
            failures = int(stats.get("auth_failures", 0) or 0)
            if failures:
                auth_failures += failures
                rejected.append(slug)
            circuit = stats.get("circuit")
            if isinstance(circuit, Mapping) and circuit.get("state") != "closed":
                open_circuits.append(slug)
        section["rate_limited_recent"] = throttled
        section["auth_failures"] = auth_failures
        section["failures"] = failures_total
        section["requests"] = requests_total
        section["retries"] = retries_total
        section["auth_rejected_tenants"] = tuple(sorted(rejected))
        section["open_circuit_tenants"] = tuple(sorted(open_circuits))
        # One summary line for the operator: the worst breaker anyone is in.
        section["circuit"] = {
            "state": "open" if open_circuits else "closed",
            "open_tenants": len(open_circuits),
        }

        if auth_failures:
            degradations.append(
                Degradation(
                    DEGRADATION_AUTH_FATAL,
                    "Conductor rejected an API key (401) for "
                    f"{', '.join(sorted(rejected))}; those tenants' pollers are "
                    "stopped. They must set a new key with /key — restarting "
                    "will not fix it, so this does not fail the healthcheck.",
                )
            )
        if open_circuits:
            degradations.append(
                Degradation(
                    DEGRADATION_CIRCUIT_OPEN,
                    "Conductor API circuit is open for "
                    f"{', '.join(open_circuits)}. Each tenant has its own "
                    "breaker, so this affects only them.",
                )
            )
        if throttled:
            degradations.append(
                Degradation(
                    DEGRADATION_RATE_LIMITED,
                    f"{throttled} of the last {recent_total} API attempts were 429s; "
                    "the token bucket may need tuning.",
                )
            )
        return section

    # -- Telegram (in-memory, cannot block) -----------------------------------

    def _collect_telegram(self, degradations: list[Degradation]) -> dict[str, Any]:
        record = _call_safely(self._telegram_provider)
        if record is None:
            return {"available": False}
        section: dict[str, Any] = {"available": True, **record.snapshot()}
        if record.failures >= TELEGRAM_FAILURE_THRESHOLD:
            degradations.append(
                Degradation(
                    DEGRADATION_TELEGRAM,
                    f"Telegram long polling has failed {record.failures} times in "
                    f"a row and no Bot API call has succeeded since "
                    f"({record.last_error or 'no detail'}); the bot is not "
                    "receiving updates.",
                )
            )
        return section

    # -- database-backed sections --------------------------------------------

    async def _collect_database(
        self, degradations: list[Degradation]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        db = _call_safely(self._db_provider)
        if db is None:
            degradations.append(
                Degradation(
                    DEGRADATION_DATABASE,
                    "No database handle is installed; the process has not booted.",
                )
            )
            return {"ok": False, "error": "not connected"}, {}

        started = self._clock()
        try:
            async with asyncio.timeout(self._db_timeout):
                schema_version = await current_schema_version(db)
                sections = await self._collect_sections(db, degradations)
        except TimeoutError:
            degradations.append(
                Degradation(
                    DEGRADATION_DATABASE,
                    f"PostgreSQL did not answer within {self._db_timeout:g}s.",
                )
            )
            return ({"ok": False, "error": "timeout", **_pool_stats(db)}, {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            degradations.append(
                Degradation(
                    DEGRADATION_DATABASE,
                    f"PostgreSQL is unreadable: {_reason(exc)}",
                )
            )
            return {"ok": False, "error": _reason(exc), **_pool_stats(db)}, {}

        db_section = {
            "ok": True,
            "schema_version": schema_version,
            "query_ms": int((self._clock() - started) * 1000),
            **_pool_stats(db),
        }
        return db_section, sections

    async def _collect_sections(
        self, db: Database, degradations: list[Degradation]
    ) -> dict[str, Any]:
        at = self._wall_clock()
        bound = await sessions_repo.list_bound(db)
        polling = self._polling_section(bound, at=at)
        if polling["overdue"]:
            degradations.append(
                Degradation(
                    DEGRADATION_POLL_LAG,
                    f"{polling['overdue']} of {polling['bound_sessions']} bound "
                    f"sessions have not polled in {polling['max_lag_ms']}ms.",
                )
            )

        counts = await deliveries_repo.counts_by_state(db)
        pending = counts.get("pending", 0) + counts.get("sending", 0)
        failed = counts.get("failed", 0)
        deliveries_section = {
            "pending": counts.get("pending", 0),
            "sending": counts.get("sending", 0),
            "failed": failed,
            "by_state": counts,
        }
        if pending > DELIVERY_BACKLOG:
            degradations.append(
                Degradation(
                    DEGRADATION_DELIVERY_BACKLOG,
                    f"{pending} deliveries are queued (threshold "
                    f"{DELIVERY_BACKLOG}); the outbox may be stuck.",
                )
            )
        if failed:
            degradations.append(
                Degradation(
                    DEGRADATION_DELIVERY_FAILED,
                    f"{failed} deliveries exhausted their retries.",
                )
            )

        lease_section = await self._lease_section(db, at=at, degradations=degradations)

        recent = await events_repo.recent_api_events(db, limit=API_EVENT_LIMIT)
        stats = await events_repo.stats(db, since_ms=at - STATS_WINDOW_MS)
        unknown = await events_repo.list_unknown_content_types(
            db, limit=UNKNOWN_TYPE_LIMIT
        )
        voice = await voice_repo.health_stats(db)

        return {
            "polling": polling,
            "deliveries": deliveries_section,
            "voice": voice,
            "lease": lease_section,
            "api_events": [_event_dict(event, at=at) for event in recent],
            # Cross-tenant on purpose: this is renderer coverage, not customer
            # data. The rows' `sample_session_id`/`sample_message_id` *are* a
            # pointer into one tenant's transcripts, so they are deliberately
            # not surfaced here — only the shape and the count.
            "unknown_content_types": [
                {
                    "type": row.type,
                    "shape_signature": row.shape_signature,
                    "count": row.count,
                    "last_seen_ms_ago": max(0, at - row.last_seen_at),
                }
                for row in unknown
            ],
            "api_event_stats": {
                "window_ms": STATS_WINDOW_MS,
                "total": stats.total,
                "ok": stats.ok,
                "failed": stats.failed,
                "error_rate": round(stats.error_rate, 4),
                "avg_duration_ms": stats.avg_duration_ms,
                "max_duration_ms": stats.max_duration_ms,
                "last_error": stats.last_error,
            },
        }

    def _polling_section(
        self, bound: list[sessions_repo.SessionRow], *, at: int
    ) -> dict[str, Any]:
        """Poll lag per bound session.

        ``sessions.updated_at`` is stamped by every ``save_turn_context``, i.e.
        once per tick of the session's poller, so ``now - updated_at`` is that
        poller's silence. A session is overdue at ``POLL_LAG_FACTOR`` times its
        own cadence plus a grace floor — the cadence is adaptive, so a fixed
        threshold would either cry wolf at 3s QUEUED or miss a dead 120s IDLE
        poller entirely.
        """
        rows: list[dict[str, Any]] = []
        overdue = 0
        max_lag = 0
        worst: str | None = None
        for row in bound:
            stamp = row.updated_at or row.created_at
            lag = max(0, at - stamp) if stamp else 0
            interval = max(1, row.poll_interval_ms)
            limit = max(int(interval * POLL_LAG_FACTOR), POLL_LAG_GRACE_MS)
            is_overdue = bool(stamp) and lag > limit
            if is_overdue:
                overdue += 1
            if lag > max_lag:
                max_lag, worst = lag, row.id
            if len(rows) < SESSION_DETAIL_LIMIT:
                rows.append(
                    {
                        "session_id": row.id,
                        "turn_state": row.turn_state,
                        "lag_ms": lag,
                        "interval_ms": interval,
                        "overdue": is_overdue,
                        "cursor_session_index": row.cursor_session_index,
                        "last_status": row.last_status,
                    }
                )
        return {
            "bound_sessions": len(bound),
            "overdue": overdue,
            "max_lag_ms": max_lag,
            "max_lag_session_id": worst,
            "sessions": rows,
        }

    async def _lease_section(
        self, db: Database, *, at: int, degradations: list[Degradation]
    ) -> dict[str, Any]:
        held = await lease_repo.get(db, lease_repo.SUPERVISOR)
        if held is None:
            section: dict[str, Any] = {
                "name": lease_repo.SUPERVISOR,
                "holder": None,
                "held": False,
            }
        else:
            section = {
                "name": held.name,
                "holder": held.holder,
                "held": not held.is_expired(at),
                "is_me": self._holder is not None and held.holder == self._holder,
                "heartbeat_ms_ago": max(0, at - held.heartbeat_at),
                "expires_in_ms": held.expires_at - at,
            }
        if self._holder is None:
            return section
        section.setdefault("is_me", False)
        if not (held is not None and held.held_by(self._holder, at)):
            degradations.append(
                Degradation(
                    DEGRADATION_LEASE_LOST,
                    "This instance does not hold the supervisor lease "
                    f"(holder={held.holder if held else 'none'}); "
                    "its pollers are stopped until it wins it back.",
                )
            )
        return section


def _event_dict(event: events_repo.ApiEvent, *, at: int) -> dict[str, Any]:
    return {
        "at": event.at,
        "ms_ago": max(0, at - event.at),
        "method": event.method,
        "endpoint": event.endpoint,
        "status_code": event.status_code,
        "duration_ms": event.duration_ms,
        "attempt": event.attempt,
        "ok": event.ok,
        "error": event.error,
        "circuit_state": event.circuit_state,
        "session_id": event.session_id,
    }


def _reason(exc: BaseException) -> str:
    """A short, content-free description of a failure."""
    text = str(exc).strip()
    label = type(exc).__name__
    return f"{label}: {text}" if text else label


def _call_safely[T](provider: Callable[[], T | None]) -> T | None:
    try:
        return provider()
    except Exception:
        return None


# ── the HTTP surface ─────────────────────────────────────────────────────────


def health_port(env: Mapping[str, str] | None = None) -> int:
    """Railway injects ``PORT``; ``HEALTH_PORT`` overrides it locally."""
    source = os.environ if env is None else env
    for name in PORT_ENV_VARS:
        raw = (source.get(name) or "").strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            log.warning("health.port_invalid", var=name)
            continue
        if 0 <= port <= 65535:
            return port
        log.warning("health.port_out_of_range", var=name)
    return DEFAULT_PORT


def health_token(env: Mapping[str, str] | None = None) -> str | None:
    """Optional shared secret that unlocks the detailed body from anywhere."""
    source = os.environ if env is None else env
    token = (source.get(TOKEN_ENV_VAR) or "").strip()
    return token or None


def _remote_ip(
    remote: str | None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not remote:
        return None
    host = remote.strip()
    if host.startswith("[") and "]" in host:  # [::1]:1234
        host = host[1 : host.index("]")]
    elif host.count(":") == 1 and "." in host:  # 127.0.0.1:1234
        host = host.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_local_remote(remote: str | None) -> bool:
    """True for loopback, private and link-local peers. Unknown peers are not.

    Informational only — *not* the detail gate. A reverse proxy (Railway's edge
    included) reaches the container from a private address, so this answers
    "same network", never "trusted".
    """
    ip = _remote_ip(remote)
    return ip is not None and (ip.is_loopback or ip.is_private or ip.is_link_local)


def is_loopback_remote(remote: str | None) -> bool:
    """True only for a peer inside this container. Nothing routed gets here."""
    ip = _remote_ip(remote)
    return ip is not None and ip.is_loopback


def detail_allowed(
    *, remote: str | None, expected_token: str | None, provided_token: str | None
) -> bool:
    """Gate on the *full* body only. The status code never depends on this.

    Without ``HEALTH_TOKEN`` the body is loopback-only: a Railway service with a
    generated domain is reachable by anyone, and its edge arrives on a private
    address, so private peers cannot be trusted with session ids and API logs.

    Compared as **bytes**. ``compare_digest`` raises ``TypeError`` on two
    ``str`` when either holds a non-ASCII character, and this one comes
    straight off the wire — so ``GET /health?token=e`` with any accented letter,
    from anybody, turned a public endpoint into a 500. Encoding first keeps the
    comparison constant-time and makes every possible input a plain no.
    """
    if expected_token:
        if provided_token is None:
            return False
        return compare_digest(
            provided_token.encode("utf-8", "surrogatepass"),
            expected_token.encode("utf-8", "surrogatepass"),
        )
    return is_loopback_remote(remote)


def request_token(request: web.Request) -> str | None:
    """``?token=`` · ``X-Health-Token:`` · ``Authorization: Bearer``."""
    query = request.query.get("token")
    if query:
        return query
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


type DetailPolicy = Callable[[web.Request], bool]


def default_detail_policy(*, token: str | None) -> DetailPolicy:
    def policy(request: web.Request) -> bool:
        return detail_allowed(
            remote=request.remote,
            expected_token=token,
            provided_token=request_token(request),
        )

    return policy


#: Lets a caller reach the monitor from the app (``app[MONITOR_KEY]``).
MONITOR_KEY: Final[web.AppKey[HealthMonitor]] = web.AppKey(
    "health_monitor", HealthMonitor
)


def _dumps(payload: Any) -> str:
    # ``default=str`` so a stray Path or enum can never turn a health check into
    # a 500. The body is diagnostics, not a contract with a parser.
    return json.dumps(payload, default=str)


def create_app(
    monitor: HealthMonitor,
    *,
    token: str | None = None,
    detail_policy: DetailPolicy | None = None,
    path: str = HEALTH_PATH,
) -> web.Application:
    """An aiohttp app serving ``GET``/``HEAD`` on ``path`` and nothing else."""
    policy = detail_policy or default_detail_policy(token=token)

    async def handle(request: web.Request) -> web.StreamResponse:
        try:
            report = await monitor.report()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - _collect swallows its own
            log.error("health.collect_failed", error=_reason(exc))
            return web.json_response(
                {"status": str(HealthStatus.DOWN), "ok": False, "error": _reason(exc)},
                status=HealthStatus.DOWN.http_status,
                dumps=_dumps,
            )
        body = report.as_dict(detail=policy(request))
        return web.json_response(body, status=report.http_status, dumps=_dumps)

    app = web.Application()
    app.router.add_get(path, handle, allow_head=True)
    app[MONITOR_KEY] = monitor
    return app


class HealthServer:
    """The ``/health`` listener, shaped for ``__main__``'s ``TaskGroup``.

    ``await server.run()`` blocks until cancelled and always unbinds the port on
    the way out, so a redeploy cannot leave the socket held.
    """

    __slots__ = ("_app", "_host", "_port", "_requested_port", "_runner")

    def __init__(
        self,
        monitor: HealthMonitor,
        *,
        host: str = "0.0.0.0",  # noqa: S104 - Railway routes to the container IP
        port: int | None = None,
        token: str | None = None,
        detail_policy: DetailPolicy | None = None,
        path: str = HEALTH_PATH,
    ) -> None:
        self._app = create_app(
            monitor,
            token=token if token is not None else health_token(),
            detail_policy=detail_policy,
            path=path,
        )
        self._host = host
        self._requested_port = port
        self._port: int | None = None
        self._runner: web.AppRunner | None = None

    @property
    def app(self) -> web.Application:
        return self._app

    @property
    def port(self) -> int | None:
        """The bound port — resolved only after :meth:`start`."""
        return self._port

    async def start(self) -> int:
        """Bind and serve. Returns the actual port (``port=0`` picks one)."""
        if self._runner is not None:
            raise RuntimeError("health server already started")
        port = self._requested_port
        if port is None:
            port = health_port()
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        self._runner = runner
        try:
            site = web.TCPSite(runner, self._host, port)
            await site.start()
        except Exception:
            self._runner = None
            await runner.cleanup()
            raise
        self._port = _bound_port(runner, port)
        log.info("health.listening", port=self._port, path=HEALTH_PATH)
        return self._port

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        self._port = None
        if runner is not None:
            await runner.cleanup()

    async def run(self) -> None:
        """Serve until cancelled. This is the TaskGroup entry point."""
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.shield(self.stop())

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()


def _bound_port(runner: web.AppRunner, requested: int) -> int:
    for address in runner.addresses:
        if isinstance(address, tuple) and len(address) >= 2:
            port = address[1]
            if isinstance(port, int):
                return port
    return requested


async def serve_health(
    monitor: HealthMonitor,
    *,
    host: str = "0.0.0.0",  # noqa: S104 - Railway routes to the container IP
    port: int | None = None,
    token: str | None = None,
    path: str = HEALTH_PATH,
) -> None:
    """``tg.create_task(serve_health(monitor))`` — runs until cancelled."""
    await HealthServer(monitor, host=host, port=port, token=token, path=path).run()


# ── the admin /health command ────────────────────────────────────────────────

_STATUS_ICON: Final[dict[HealthStatus, str]] = {
    HealthStatus.OK: "✅",
    HealthStatus.DEGRADED: "⚠️",
    HealthStatus.DOWN: "🛑",
}


def lease_line(lease: Mapping[str, Any]) -> str:
    """Who is driving, in words rather than in a token.

    The holder is a random instance id. Pasting it into a chat answers nothing
    the owner can act on — there is no second token to compare it against. The
    only actionable fact is whether *this* process is the one polling, which is
    exactly what ``is_me`` says.
    """
    if not lease.get("holder"):
        return "<i>unheld</i>"
    if not lease.get("held", True):
        return "expired · a new instance can take it"
    return "this instance" if lease.get("is_me") else "another instance"


def format_health_html(report: HealthReport, *, events: int = 5) -> str:
    """The ``/health`` command body. Telegram HTML, never MarkdownV2."""
    icon = _STATUS_ICON.get(report.status, "")
    uptime = format_duration(int(report.uptime_s * 1000))
    lines = [f"{icon} <b>{escape(str(report.status))}</b> · up {escape(uptime)}"]

    for degradation in report.degradations:
        lines.append(
            f"⚠️ <b>{escape(degradation.code)}</b> — {escape(degradation.detail)}"
        )

    circuit = report.conductor.get("circuit")
    circuit_state = (
        str(circuit.get("state")) if isinstance(circuit, Mapping) else "unknown"
    )
    lines.append(
        "🔌 circuit <code>{}</code> · {} requests · {} failures · {} 429s".format(
            escape(circuit_state),
            report.conductor.get("requests", 0),
            report.conductor.get("failures", 0),
            report.conductor.get("rate_limited_recent", 0),
        )
    )

    # Silence is the healthy case, so this line only appears when it says
    # something: a bot that is answering /health is obviously reaching Telegram.
    telegram_failures = int(report.telegram.get("polling_failures", 0) or 0)
    if telegram_failures:
        lines.append(
            "📶 telegram: {} failed polls · {}".format(
                telegram_failures,
                escape(str(report.telegram.get("last_error") or "no detail")),
            )
        )

    polling = report.polling
    lines.append(
        "📡 {} bound · {} overdue · max lag {}".format(
            polling.get("bound_sessions", 0),
            polling.get("overdue", 0),
            escape(format_duration(int(polling.get("max_lag_ms", 0) or 0))),
        )
    )
    lines.append(
        "📬 deliveries: {} pending · {} sending · {} failed".format(
            report.deliveries.get("pending", 0),
            report.deliveries.get("sending", 0),
            report.deliveries.get("failed", 0),
        )
    )
    # ``transcribing`` earns its own number: a note stuck mid-flight is what a
    # hang looks like, and it is invisible folded into ``pending``.
    transcribing = int(report.voice.get("transcribing", 0) or 0)
    lines.append(
        "🎙 voice: {} pending · {} failed · {} clarify{}".format(
            report.voice.get("pending", 0),
            report.voice.get("failed", 0),
            report.voice.get("waiting_for_user", 0),
            f" · {transcribing} transcribing" if transcribing else "",
        )
    )

    lines.append(f"🔑 lease: {lease_line(report.lease)}")

    unknown = report.unknown_content_types
    if unknown:
        summary = ", ".join(
            f"{escape(str(row.get('type')))}×{row.get('count', 0)}"
            for row in unknown[:5]
        )
        lines.append(f"❓ unknown types: {summary}")

    recent = report.api_events[:events]
    if recent:
        lines.append("<b>Recent API calls</b>")
        for event in recent:
            mark = "ok" if event.get("ok") else "ERR"
            lines.append(
                "<code>{} {} {} {} {}ms</code>".format(
                    escape(str(event.get("method") or "?")),
                    escape(str(event.get("endpoint") or "?")),
                    escape(str(event.get("status_code") or "-")),
                    mark,
                    event.get("duration_ms") or 0,
                )
            )
    return "\n".join(lines)


__all__ = [
    "API_EVENT_LIMIT",
    "CACHE_TTL_S",
    "DB_TIMEOUT_S",
    "DEFAULT_PORT",
    "DEGRADATION_AUTH_FATAL",
    "DEGRADATION_CIRCUIT_OPEN",
    "DEGRADATION_DATABASE",
    "DEGRADATION_DELIVERY_BACKLOG",
    "DEGRADATION_DELIVERY_FAILED",
    "DEGRADATION_LEASE_LOST",
    "DEGRADATION_POLL_LAG",
    "DEGRADATION_RATE_LIMITED",
    "DEGRADATION_TELEGRAM",
    "DELIVERY_BACKLOG",
    "HEALTH_PATH",
    "POLL_LAG_FACTOR",
    "POLL_LAG_GRACE_MS",
    "TELEGRAM_FAILURE_THRESHOLD",
    "PoolProvider",
    "DatabaseProvider",
    "Degradation",
    "DetailPolicy",
    "HealthMonitor",
    "HealthReport",
    "HealthServer",
    "HealthStatus",
    "TelegramHealth",
    "TelegramProvider",
    "create_app",
    "default_pool_provider",
    "default_database_provider",
    "default_detail_policy",
    "detail_allowed",
    "format_health_html",
    "lease_line",
    "health_port",
    "health_token",
    "is_local_remote",
    "is_loopback_remote",
    "request_token",
    "reset_telegram_health",
    "serve_health",
    "telegram_health",
]
