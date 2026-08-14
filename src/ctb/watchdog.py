"""The service that notices the bot has stopped working, and says so.

Everything else in this process reports *into* ``/health``, where somebody has
to go and look. Nothing pushed. So the four-day outage was found by a person
trying to use the product, which is the worst way to find one — the bot held an
open Telegram channel to the exact human who cared and never used it to say it
was broken.

Three decisions shape this module.

**It does not live inside the supervisor.** The supervisor is a thing that can
wedge, and a watchdog running on the wedged loop is not a watchdog. It gets its
own task, reads the database directly, and needs nothing from the component it
is watching.

**Deduplication is a database key, not a flag in memory.** The alarm is keyed on
when the silence *started* — the oldest silent session's ``updated_at``, which
by definition is not moving while the silence lasts. Repeated ticks therefore
collide on ``deliveries``' primary key and are dropped by PostgreSQL, exactly
the way the turn receipt and every ``once_key`` notice already work. One message
per episode, across restarts, with no state of our own to get wrong; a genuinely
new episode has a different start and gets a new message.

**A missed alarm is a bug; a missed all-clear is not.** So the alarm is durable
and the "back to normal" note is best effort, tracked in memory. A redeploy
mid-outage costs at most one reassurance, never a warning.

It is an :data:`OPTIONAL_SERVICE`: a watchdog that could take the bot down with
it would be worse than no watchdog at all.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from ctb.db.connection import Database, now_ms
from ctb.db.repo import events as events_repo
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import tenancy
from ctb.db.repo.sessions import SessionRow
from ctb.logging import get_logger
from ctb.silence import (
    SILENCE_LOOKBACK_MS,
    SilenceReason,
    attribute,
    notice_html,
    recovered_html,
)

__all__ = ["CHECK_INTERVAL_S", "SILENT_AFTER_MS", "Silence", "SilenceSink", "Watchdog"]

log = get_logger(__name__)

#: Matches ``health.POLL_SILENT_MS``: the two must agree, or ``/health`` and the
#: owner's phone tell different stories about the same minute.
SILENT_AFTER_MS: Final[int] = 10 * 60_000

#: Slow on purpose. The condition takes ten minutes to arise, so checking every
#: half minute buys nothing and costs two queries a tick forever.
CHECK_INTERVAL_S: Final[float] = 60.0

type Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Silence:
    """One tenant's outage, as the watchdog sees it."""

    tenant_id: uuid.UUID
    slug: str
    reason: SilenceReason
    sessions: tuple[str, ...]
    #: ``updated_at`` of the longest-silent session: when this episode began.
    since: int
    silent_for_ms: int

    @property
    def key(self) -> str:
        """Stable for one episode, different for the next. The dedup identity."""
        return f"silence:{self.tenant_id}:{self.since}"


class SilenceSink(Protocol):
    """What the watchdog needs from the bot. Implemented by ``BotActionSink``."""

    async def silence_detected(self, silence: Silence, *, html: str) -> None: ...

    async def silence_cleared(self, tenant_id: uuid.UUID, *, html: str) -> None: ...


class Watchdog:
    """Poll for sessions nobody is watching and tell their owners."""

    def __init__(
        self,
        system_db: Database,
        *,
        sink: SilenceSink | None = None,
        silent_after_ms: int = SILENT_AFTER_MS,
        interval_s: float = CHECK_INTERVAL_S,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        #: BYPASSRLS: silence is a cross-tenant census, like the reconcile query.
        self.system_db = system_db
        self._sink = sink
        self._silent_after_ms = silent_after_ms
        self._interval_s = interval_s
        self._sleep = sleep
        self._clock = clock
        self._stop = asyncio.Event()
        #: Tenants alarmed since this process started, for the all-clear only.
        self._alarmed: set[uuid.UUID] = set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A watchdog that dies on a transient read is the failure it
                # exists to catch. Log and try again next tick.
                log.warning("watchdog.check_failed", error=repr(exc))
            await self._pause(self._interval_s)

    async def stop(self) -> None:
        self._stop.set()

    async def _pause(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    async def check_once(self) -> tuple[Silence, ...]:
        """One pass. Returns what is currently silent, having reported it."""
        at = self._clock()
        rows = await sessions_repo.list_silent(
            self.system_db, silent_after_ms=self._silent_after_ms, at=at
        )
        episodes = await self._episodes(rows, at=at)
        for episode in episodes:
            await self._alarm(episode)
        await self._all_clear({episode.tenant_id for episode in episodes})
        return episodes

    async def _episodes(
        self, rows: Sequence[SessionRow], *, at: int
    ) -> tuple[Silence, ...]:
        by_tenant: dict[uuid.UUID, list[SessionRow]] = {}
        for row in rows:
            if row.tenant_id is not None:
                by_tenant.setdefault(row.tenant_id, []).append(row)

        episodes: list[Silence] = []
        for tenant_id, group in by_tenant.items():
            tenant = await tenancy.get(self.system_db, tenant_id)
            if tenant is None:  # pragma: no cover - the FK guarantees one
                continue
            stats = await events_repo.stats(
                self.system_db,
                since_ms=at - SILENCE_LOOKBACK_MS,
                tenant_id=tenant_id,
            )
            since = min(row.updated_at for row in group)
            episodes.append(
                Silence(
                    tenant_id=tenant_id,
                    slug=tenant.slug,
                    reason=attribute(
                        auth_failed=tenant.auth_failed_at is not None,
                        api_calls=stats.total,
                        api_ok=stats.ok,
                    ),
                    sessions=tuple(sorted(row.id for row in group)),
                    since=since,
                    silent_for_ms=max(0, at - since),
                )
            )
        return tuple(sorted(episodes, key=lambda e: e.slug))

    async def _alarm(self, episode: Silence) -> None:
        log.warning(
            "watchdog.silent",
            tenant=episode.slug,
            reason=str(episode.reason),
            sessions=len(episode.sessions),
            silent_for_ms=episode.silent_for_ms,
        )
        self._alarmed.add(episode.tenant_id)
        if self._sink is None:
            return
        html = notice_html(
            episode.reason,
            sessions=len(episode.sessions),
            silent_for_ms=episode.silent_for_ms,
        )
        try:
            await self._sink.silence_detected(episode, html=html)
        except Exception as exc:
            # The enqueue is the durable part; if it fails the key was never
            # written, so the next tick retries the same episode and the same
            # key. Nothing is lost by not raising here.
            log.warning("watchdog.notice_failed", tenant=episode.slug, error=repr(exc))

    async def _all_clear(self, still_silent: set[uuid.UUID]) -> None:
        recovered = self._alarmed - still_silent
        if not recovered:
            return
        self._alarmed -= recovered
        if self._sink is None:
            return
        for tenant_id in recovered:
            log.info("watchdog.recovered", tenant=str(tenant_id))
            watched = await sessions_repo.list_bound(self.system_db)
            count = sum(1 for row in watched if row.tenant_id == tenant_id)
            try:
                await self._sink.silence_cleared(
                    tenant_id, html=recovered_html(sessions=count)
                )
            except Exception as exc:  # pragma: no cover - best effort by design
                log.warning(
                    "watchdog.recovery_notice_failed",
                    tenant=str(tenant_id),
                    error=repr(exc),
                )
