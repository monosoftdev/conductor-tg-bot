"""The Conductor v0 HTTP client — one ``httpx.AsyncClient``, guarded four ways.

Every outbound call passes through the same pipeline:

    Semaphore(8) -> circuit breaker -> token bucket (5 req/s, burst 10)
        -> httpx (per-endpoint timeouts) -> retry/backoff -> api_events hook

The rules that are not negotiable, and why:

* **An explicit ``User-Agent`` on every request.** The API sits behind a proxy
  that 403s some default client signatures (the docs call out Python's
  ``urllib``). This is a hard requirement, not politeness.
* **401/403 is never retried.** Retrying a bad key risks a lockout and the
  failure will not fix itself. :class:`~ctb.conductor.errors.AuthFatal` reaches
  the caller on the first response; the caller stops the pollers and DMs the
  owner once.
* **A write whose fate is unknown raises
  :class:`~ctb.conductor.errors.Ambiguous`, never a plain error.** For
  ``POST /sessions/{id}/messages`` the caller retries forever with the *same*
  ``messageId`` — re-POSTing an identical id dedupes (verified twice against the
  live API). ``POST /workspaces`` has **no** idempotency key, so it is never
  retried after the request may have been delivered; the caller reconciles via
  the nonce embedded in the generated workspace name.
* **``POST /sessions`` does take a caller-supplied ``sessionId``** (probe-verified
  — ``docs/PLAN.md`` is wrong about this), so :meth:`ConductorClient.create_session`
  accepts one and becomes safely retryable when given it.
* **``GET /me`` lives at the API root**, not under ``/v0``.

Everything that touches the wall clock is injectable (``clock``, ``sleep``,
``rng``) so the whole retry/backoff/circuit surface is testable without a real
network and without real waiting.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, Self

import httpx
from pydantic import BaseModel, ValidationError

from ctb import USER_AGENT
from ctb.conductor.errors import (
    Ambiguous,
    ApiError,
    AuthFatal,
    CircuitOpen,
    ConductorError,
    NotFound,
    RateLimited,
    api_error_for_status,
)
from ctb.conductor.models import (
    CancelResult,
    Me,
    MessagesPage,
    PostMessageResult,
    ProjectsPage,
    Session,
    SessionsPage,
    SessionStatus,
    SqlResult,
    TranscriptMessage,
    Workspace,
    WorkspaceCreateResult,
    WorkspacesPage,
    WorkspaceStatus,
    validate_pairing,
)
from ctb.logging import get_logger, register_secret
from ctb.settings import Settings, get_settings

__all__ = [
    "ApiEvent",
    "ApiEventHook",
    "CircuitBreaker",
    "CircuitState",
    "ConductorClient",
    "TokenBucket",
    "TransportFailure",
    "close_client",
    "get_client",
    "set_client",
]

# ── tuning (PLAN §Client) ────────────────────────────────────────────────────

RATE_LIMIT_PER_SECOND: Final = 5.0
RATE_LIMIT_BURST: Final = 10.0
MAX_CONCURRENCY: Final = 8

CIRCUIT_FAILURE_THRESHOLD: Final = 3
CIRCUIT_OPEN_SECONDS: Final = 60.0
CIRCUIT_JITTER: Final = 0.2
#: How long a caller is told to wait when another task already holds the
#: half-open probe slot.
HALF_OPEN_PROBE_RETRY_S: Final = 1.0

MAX_ATTEMPTS: Final = 5
BACKOFF_BASE_S: Final = 0.5
BACKOFF_CAP_S: Final = 30.0
#: A ``Retry-After`` longer than this is handed back to the caller instead of
#: being slept through inside one request — a poll tick must not block for
#: minutes.
MAX_RETRY_AFTER_SLEEP_S: Final = 30.0

CONNECT_TIMEOUT_S: Final = 5.0
READ_TIMEOUT_S: Final = 20.0
POST_MESSAGE_TIMEOUT_S: Final = 30.0
SQL_TIMEOUT_S: Final = 60.0

#: ``POST /v0/sql`` limits, enforced client-side so a bad query fails locally
#: instead of burning a request.
SQL_MAX_QUERY_CHARS: Final = 10_000
SQL_FORBIDDEN_TEXT: Final = "set_config"

EVENT_BUFFER: Final = 200

type Clock = Callable[[], float]
type Sleeper = Callable[[float], Awaitable[None]]

_WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _timeout(read: float) -> httpx.Timeout:
    """Per-endpoint timeout: a short connect, a generous read."""
    return httpx.Timeout(read, connect=CONNECT_TIMEOUT_S)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── api_events ring buffer (the /health surface) ─────────────────────────────


@dataclass(frozen=True, slots=True)
class ApiEvent:
    """One HTTP attempt. Column names match the ``api_events`` table."""

    at: int
    method: str
    endpoint: str
    status_code: int | None
    duration_ms: int
    attempt: int
    ok: bool
    error: str | None
    circuit_state: str
    request_id: str | None
    session_id: str | None

    def as_row(self) -> dict[str, Any]:
        """Kwargs for an ``INSERT INTO api_events`` in the repo layer."""
        return {
            "at": self.at,
            "method": self.method,
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "attempt": self.attempt,
            "ok": 1 if self.ok else 0,
            "error": self.error,
            "circuit_state": self.circuit_state,
            "request_id": self.request_id,
            "session_id": self.session_id,
        }


#: Injected by the boot path so the client never imports the repo layer. May be
#: sync or async; anything it raises is swallowed — telemetry never breaks a call.
type ApiEventHook = Callable[[ApiEvent], Awaitable[None] | None]


class TransportFailure(ConductorError):
    """No HTTP response at all: DNS, connect, TLS, reset, or a read timeout.

    Raised for reads, and for writes that provably never reached the server (a
    connect failure). A write whose fate is genuinely unknown raises
    :class:`~ctb.conductor.errors.Ambiguous` instead, because the two demand
    different recovery.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str = "GET",
        path: str = "",
        attempts: int = 1,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.attempts = attempts
        self.cause = cause

    @property
    def retryable(self) -> bool:
        return True


# ── rate limiting ────────────────────────────────────────────────────────────


#: Rounding slack when comparing accumulated tokens against the ask. A monotonic
#: clock reading is a large float, so ``(t1 - t0) * rate`` lands a few ULPs under
#: the exact answer; without this the bucket can ask for a delay so small that it
#: does not move the clock at all, and spin forever.
_TOKEN_EPSILON: Final = 1e-9
#: Never schedule a wait shorter than this — same starvation guard, from the
#: other end.
_MIN_WAIT_S: Final = 0.001


class TokenBucket:
    """Classic token bucket: ``rate`` tokens/second, at most ``burst`` saved up.

    Serialized by a lock so waiters are admitted in arrival order — a poller that
    has been waiting must not be starved by a fresh command handler.
    """

    def __init__(
        self,
        rate: float = RATE_LIMIT_PER_SECOND,
        burst: float = RATE_LIMIT_BURST,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._rate = rate
        self._capacity = burst
        self._tokens = burst
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> float:
        return self._tokens

    async def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available. Returns the seconds waited."""
        waited = 0.0
        async with self._lock:
            while True:
                now = self._clock()
                elapsed = max(0.0, now - self._updated)
                self._updated = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens - _TOKEN_EPSILON:
                    self._tokens -= tokens
                    return waited
                delay = max((tokens - self._tokens) / self._rate, _MIN_WAIT_S)
                await self._sleep(delay)
                waited += delay


# ── circuit breaker ──────────────────────────────────────────────────────────


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Three consecutive failures open the circuit for a jittered window.

    A *failure* is a 5xx or a transport error — both mean "the API is unhealthy".
    A 4xx is a healthy server saying no, so it resets the streak.

    When the window expires the next caller becomes the single half-open probe;
    everyone else keeps failing fast. The probe closes the circuit on success and
    re-opens it on failure.

    A success does **not** clear an open window. That matters for 429: the window
    is set to ``Retry-After``, and the in-flight retry that then succeeds must not
    unleash every other poller early.
    """

    def __init__(
        self,
        *,
        threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        open_seconds: float = CIRCUIT_OPEN_SECONDS,
        jitter: float = CIRCUIT_JITTER,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._jitter = jitter
        self._clock = clock
        self._rng = rng or random.Random()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_until = 0.0
        self._opened_by: str | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def opened_by(self) -> str | None:
        return self._opened_by

    def retry_after(self) -> float:
        return max(0.0, self._opened_until - self._clock())

    def check(self) -> None:
        """Gate one logical request. Raises :class:`CircuitOpen` to fail fast.

        Synchronous on purpose: it must not yield between the state test and the
        probe claim, or two tasks would both become the probe.
        """
        if self._state is CircuitState.OPEN:
            remaining = self.retry_after()
            if remaining > 0:
                raise CircuitOpen(retry_after=remaining, opened_by=self._opened_by)
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = False
        if self._state is CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                raise CircuitOpen(
                    retry_after=HALF_OPEN_PROBE_RETRY_S, opened_by=self._opened_by
                )
            self._probe_in_flight = True

    def record_ok(self) -> None:
        """A healthy round trip — any response the server actually produced."""
        self._probe_in_flight = False
        self._consecutive_failures = 0
        if self._state is CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._opened_by = None

    def record_failure(self, reason: str) -> None:
        """A 5xx or a transport error."""
        was_probing = self._state is CircuitState.HALF_OPEN
        self._probe_in_flight = False
        self._consecutive_failures += 1
        if was_probing or self._consecutive_failures >= self._threshold:
            self.trip(None, reason)

    def trip(self, seconds: float | None, reason: str) -> None:
        """Open the circuit explicitly (429 passes its ``Retry-After`` here)."""
        window = self._window() if seconds is None else max(0.0, seconds)
        self._state = CircuitState.OPEN
        self._opened_until = self._clock() + window
        self._opened_by = reason
        self._probe_in_flight = False

    def _window(self) -> float:
        spread = self._jitter * (2.0 * self._rng.random() - 1.0)
        return self._open_seconds * (1.0 + spread)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "retry_after": round(self.retry_after(), 3),
            "opened_by": self._opened_by,
        }


# ── the client ───────────────────────────────────────────────────────────────


class ConductorClient:
    """Async client for the Conductor v0 API.

    One instance per process (see :func:`get_client`). Construct it with a
    ``transport`` for tests; construct it with ``clock``/``sleep``/``rng`` to make
    backoff and the circuit window deterministic.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        on_event: ApiEventHook | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        event_buffer: int = EVENT_BUFFER,
    ) -> None:
        self._settings = settings or get_settings()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._max_attempts = max(1, max_attempts)
        self._on_event = on_event
        self._log = get_logger("ctb.conductor.client")

        api_key = self._settings.conductor_api_key.get_secret_value()
        # Belt and braces: even if a third-party library echoes a header, the log
        # scrubber will have seen this value.
        register_secret(api_key)

        self._http = http_client or httpx.AsyncClient(
            base_url=self._settings.conductor_api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                # Not optional: the proxy 403s some default client signatures.
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=_timeout(READ_TIMEOUT_S),
            transport=transport,
            limits=httpx.Limits(
                max_connections=MAX_CONCURRENCY,
                max_keepalive_connections=MAX_CONCURRENCY,
            ),
            follow_redirects=False,
        )
        self._owns_http = http_client is None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._bucket = TokenBucket(clock=clock, sleep=sleep)
        self._circuit = CircuitBreaker(clock=clock, rng=self._rng)
        self._events: deque[ApiEvent] = deque(maxlen=max(1, event_buffer))
        #: Strong refs to in-flight async event-hook tasks; without these the
        #: event loop may garbage-collect a task mid-await.
        self._hook_tasks: set[asyncio.Task[None]] = set()
        self._requests = 0
        self._retries = 0
        self._failures = 0
        self._auth_failures = 0
        self._closed = False

    # -- lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
        if self._hook_tasks:
            await asyncio.gather(*tuple(self._hook_tasks), return_exceptions=True)
        if self._owns_http and not self._closed:
            await self._http.aclose()
        self._closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- introspection --------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def circuit(self) -> CircuitBreaker:
        return self._circuit

    @property
    def auth_failures(self) -> int:
        """Non-zero means the API key was rejected. Pollers should be stopped."""
        return self._auth_failures

    def recent_events(self, limit: int | None = None) -> tuple[ApiEvent, ...]:
        events = tuple(self._events)
        return events if limit is None else events[-limit:]

    def health(self) -> dict[str, Any]:
        """The ``/health`` payload for this subsystem."""
        last = self._events[-1] if self._events else None
        return {
            "requests": self._requests,
            "retries": self._retries,
            "failures": self._failures,
            "auth_failures": self._auth_failures,
            "buffered_events": len(self._events),
            "circuit": self._circuit.snapshot(),
            "last_call": None
            if last is None
            else {
                "at": last.at,
                "method": last.method,
                "endpoint": last.endpoint,
                "status_code": last.status_code,
                "duration_ms": last.duration_ms,
                "ok": last.ok,
            },
        }

    # -- the request pipeline -------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        read_timeout: float = READ_TIMEOUT_S,
        idempotent: bool = False,
        write: bool | None = None,
        message_id: str | None = None,
        session_id: str | None = None,
        max_attempts: int | None = None,
    ) -> Any:
        """Issue one logical request, retries included, and return parsed JSON.

        ``idempotent`` means "replaying this cannot create a second side effect".
        ``write`` (default: any non-GET) decides whether an unknown outcome is
        reported as :class:`Ambiguous` rather than :class:`TransportFailure`.
        """
        method = method.upper()
        is_write = (method in _WRITE_METHODS) if write is None else write
        limit = self._max_attempts if max_attempts is None else max(1, max_attempts)
        query = {k: v for k, v in (params or {}).items() if v is not None}

        async with self._semaphore:
            # Gated once per logical request: an in-flight retry has already been
            # budgeted, and re-checking would turn a 5xx on a write into
            # CircuitOpen, destroying the "this may have landed" information.
            self._circuit.check()
            attempt = 0
            while True:
                attempt += 1
                await self._bucket.acquire()
                started = self._clock()
                error: ApiError | None = None
                payload: Any = None
                status: int | None = None
                request_id: str | None = None
                try:
                    response = await self._http.request(
                        method,
                        path,
                        params=query or None,
                        json=dict(json_body) if json_body is not None else None,
                        timeout=_timeout(read_timeout),
                    )
                except httpx.HTTPError as exc:
                    self._finish(
                        endpoint=endpoint,
                        method=method,
                        status=None,
                        started=started,
                        attempt=attempt,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        request_id=None,
                        session_id=session_id,
                    )
                    self._circuit.record_failure(f"{method} {endpoint}: transport")
                    never_sent = isinstance(
                        exc, (httpx.ConnectError, httpx.ConnectTimeout)
                    )
                    if (idempotent or never_sent) and attempt < limit:
                        self._retries += 1
                        await self._backoff(attempt)
                        continue
                    if is_write and not never_sent:
                        raise Ambiguous(
                            f"{method} {endpoint} failed without a response "
                            f"after {attempt} attempt(s); the write may have landed",
                            method=method,
                            path=endpoint,
                            message_id=message_id,
                            idempotent=idempotent,
                            cause=exc,
                        ) from exc
                    raise TransportFailure(
                        f"{method} {endpoint} failed without a response "
                        f"after {attempt} attempt(s): {type(exc).__name__}",
                        method=method,
                        path=endpoint,
                        attempts=attempt,
                        cause=exc,
                    ) from exc

                status = response.status_code
                request_id = _request_id_of(response)
                if status < 400:
                    try:
                        payload = _decode(response)
                    except ValueError:
                        # A 2xx that is not JSON is a proxy error page, not an
                        # answer. Treat it as retryable rather than crashing a
                        # poller on a decode error.
                        error = ApiError(
                            status,
                            {
                                "userMessage": "response body was not JSON",
                                "retryable": True,
                            },
                            method=method,
                            path=endpoint,
                            request_id=request_id,
                        )
                    if error is None:
                        self._circuit.record_ok()
                        # A 2xx proves the key works, so a *transient* 403 (the
                        # proxy in front of the API rejects some client
                        # signatures) must not latch the bot dead: the
                        # supervisor treats ``auth_failures > 0`` as fatal and
                        # cancels every poller for the life of the process. A
                        # genuinely bad key never reaches this line.
                        if self._auth_failures:
                            self._log.info(
                                "conductor.auth_recovered",
                                endpoint=endpoint,
                                method=method,
                                auth_failures=self._auth_failures,
                            )
                            self._auth_failures = 0
                        self._finish(
                            endpoint=endpoint,
                            method=method,
                            status=status,
                            started=started,
                            attempt=attempt,
                            ok=True,
                            error=None,
                            request_id=request_id,
                            session_id=session_id,
                        )
                        return payload
                else:
                    error = api_error_for_status(
                        status,
                        _decode_safely(response),
                        method=method,
                        path=endpoint,
                        retry_after=_parse_retry_after(
                            response.headers.get("retry-after")
                        ),
                        request_id=request_id,
                    )

                self._finish(
                    endpoint=endpoint,
                    method=method,
                    status=status,
                    started=started,
                    attempt=attempt,
                    ok=False,
                    error=str(error),
                    request_id=request_id,
                    session_id=session_id,
                )

                if isinstance(error, RateLimited):
                    # Honour Retry-After *and* open the circuit: every other
                    # caller backs off too, not just this one.
                    self._circuit.trip(
                        error.retry_after, f"{method} {endpoint}: 429 rate limited"
                    )
                elif error.status >= 500:
                    self._circuit.record_failure(f"{method} {endpoint}: {error.status}")
                else:
                    self._circuit.record_ok()

                if isinstance(error, AuthFatal):
                    self._auth_failures += 1
                    self._log.error(
                        "conductor.auth_fatal",
                        endpoint=endpoint,
                        method=method,
                        status_code=error.status,
                        detail=error.user_message,
                    )
                    raise error
                if isinstance(error, NotFound):
                    raise error

                # A 4xx (429 included) was rejected without a side effect, so it
                # is safe to replay even for a non-idempotent write. A 5xx is not.
                safe_to_retry = idempotent or error.status < 500
                if error.retryable and safe_to_retry and attempt < limit:
                    delay = self._backoff_delay(attempt)
                    if isinstance(error, RateLimited) and error.retry_after is not None:
                        if error.retry_after > MAX_RETRY_AFTER_SLEEP_S:
                            raise error
                        delay = error.retry_after
                    self._retries += 1
                    self._log.warning(
                        "conductor.retry",
                        endpoint=endpoint,
                        method=method,
                        status_code=error.status,
                        attempt=attempt,
                        delay_s=round(delay, 3),
                    )
                    await self._sleep(delay)
                    continue

                if is_write and error.status >= 500:
                    raise Ambiguous(
                        f"{method} {endpoint} -> {error.status} "
                        f"after {attempt} attempt(s); the write may have landed",
                        method=method,
                        path=endpoint,
                        message_id=message_id,
                        idempotent=idempotent,
                        cause=error,
                    ) from error
                raise error

    def _backoff_delay(self, attempt: int) -> float:
        """Full jitter: ``uniform(0, min(cap, base * 2**(attempt-1)))``."""
        ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempt - 1)))
        return self._rng.uniform(0.0, ceiling)

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(self._backoff_delay(attempt))

    def _finish(
        self,
        *,
        endpoint: str,
        method: str,
        status: int | None,
        started: float,
        attempt: int,
        ok: bool,
        error: str | None,
        request_id: str | None,
        session_id: str | None,
    ) -> None:
        duration_ms = max(0, int((self._clock() - started) * 1000))
        self._requests += 1
        if not ok:
            self._failures += 1
        event = ApiEvent(
            at=_now_ms(),
            method=method,
            endpoint=endpoint,
            status_code=status,
            duration_ms=duration_ms,
            attempt=attempt,
            ok=ok,
            error=error,
            circuit_state=self._circuit.state.value,
            request_id=request_id,
            session_id=session_id,
        )
        self._events.append(event)
        self._log.debug(
            "conductor.call",
            endpoint=endpoint,
            method=method,
            status_code=status,
            duration_ms=duration_ms,
            attempt=attempt,
            ok=ok,
            error=error,
        )
        if self._on_event is None:
            return
        try:
            result = self._on_event(event)
        except Exception:  # pragma: no cover - telemetry must never break a call
            self._log.warning("conductor.event_hook_failed", endpoint=endpoint)
            return
        if result is not None:
            # Fire-and-forget: an async sink must not add latency to a poll tick.
            task = asyncio.ensure_future(_swallow(result))
            self._hook_tasks.add(task)
            task.add_done_callback(self._hook_tasks.discard)

    def _model[M: BaseModel](self, model: type[M], payload: Any, endpoint: str) -> M:
        """Validate a response body, converting a shape surprise into an ApiError.

        Never quotes the offending values: a response body can contain transcript
        content, which is the user's source code.
        """
        if not isinstance(payload, dict):
            payload = {} if payload is None else {"_raw": payload}
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            fields = ", ".join(
                ".".join(str(part) for part in err["loc"]) for err in exc.errors()[:5]
            )
            raise ApiError(
                200,
                {
                    "userMessage": (
                        f"unexpected {model.__name__} shape from {endpoint}"
                        + (f" (fields: {fields})" if fields else "")
                    )
                },
                method="GET",
                path=endpoint,
            ) from exc

    # -- identity -------------------------------------------------------------

    async def get_me(self) -> Me:
        """``GET /me`` — at the API **root**; ``/v0/me`` 404s (probe-verified)."""
        payload = await self._request(
            "GET", self._settings.me_url, endpoint="/me", idempotent=True
        )
        return self._model(Me, payload, "/me")

    # -- projects & workspaces ------------------------------------------------

    async def list_projects(
        self, *, limit: int | None = None, offset: int | None = None
    ) -> ProjectsPage:
        payload = await self._request(
            "GET",
            "/projects",
            endpoint="/projects",
            params={"limit": limit, "offset": offset},
            idempotent=True,
        )
        return self._model(ProjectsPage, payload, "/projects")

    async def list_project_workspaces(
        self,
        project_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> WorkspacesPage:
        """The only per-project workspace listing. There is no global one —
        cross-org listing goes through :meth:`sql`."""
        payload = await self._request(
            "GET",
            f"/projects/{project_id}/workspaces",
            endpoint="/projects/{id}/workspaces",
            params={"limit": limit, "offset": offset},
            idempotent=True,
        )
        return self._model(WorkspacesPage, payload, "/projects/{id}/workspaces")

    async def create_workspace(
        self,
        *,
        agent: str,
        project_id: str | None = None,
        repository_url: str | None = None,
        branch: str | None = None,
        name: str | None = None,
        session_name: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> WorkspaceCreateResult:
        """``POST /v0/workspaces`` — **no idempotency key exists**.

        This is the one call that is never retried once the request may have been
        delivered: an :class:`Ambiguous` here must be reconciled by listing the
        project's workspaces and looking for the nonce embedded in ``name``.
        """
        if (project_id is None) == (repository_url is None):
            raise ValueError(
                "create_workspace needs exactly one of project_id or repository_url"
            )
        resolved_agent, resolved_model, resolved_effort = validate_pairing(
            agent, model, effort
        )
        body: dict[str, Any] = {"agent": resolved_agent.value}
        if project_id is not None:
            body["projectId"] = project_id
        if repository_url is not None:
            body["repositoryUrl"] = repository_url
        if branch is not None:
            body["branch"] = branch
        if name is not None:
            body["name"] = name
        if session_name is not None:
            body["sessionName"] = session_name
        if resolved_model is not None:
            body["model"] = resolved_model
        if resolved_effort is not None:
            body["effort"] = resolved_effort
        if env:
            body["env"] = dict(env)
        payload = await self._request(
            "POST",
            "/workspaces",
            endpoint="/workspaces",
            json_body=body,
            idempotent=False,
        )
        return self._model(WorkspaceCreateResult, payload, "/workspaces")

    async def get_workspace(self, workspace_id: str) -> Workspace:
        payload = await self._request(
            "GET",
            f"/workspaces/{workspace_id}",
            endpoint="/workspaces/{id}",
            idempotent=True,
        )
        return self._model(Workspace, payload, "/workspaces/{id}")

    async def rename_workspace(self, workspace_id: str, name: str) -> Workspace | None:
        """Rename is idempotent — replaying it sets the same name."""
        payload = await self._request(
            "POST",
            f"/workspaces/{workspace_id}/rename",
            endpoint="/workspaces/{id}/rename",
            json_body={"name": name},
            idempotent=True,
        )
        return self._maybe_workspace(payload, "/workspaces/{id}/rename")

    async def archive_workspace(self, workspace_id: str) -> Workspace | None:
        """Archive is idempotent and restorable (per the API docs)."""
        payload = await self._request(
            "POST",
            f"/workspaces/{workspace_id}/archive",
            endpoint="/workspaces/{id}/archive",
            idempotent=True,
        )
        return self._maybe_workspace(payload, "/workspaces/{id}/archive")

    async def get_workspace_status(self, workspace_id: str) -> WorkspaceStatus:
        payload = await self._request(
            "GET",
            f"/workspaces/{workspace_id}/status",
            endpoint="/workspaces/{id}/status",
            idempotent=True,
        )
        return self._model(WorkspaceStatus, payload, "/workspaces/{id}/status")

    async def list_workspace_sessions(
        self,
        workspace_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> SessionsPage:
        payload = await self._request(
            "GET",
            f"/workspaces/{workspace_id}/sessions",
            endpoint="/workspaces/{id}/sessions",
            params={"limit": limit, "offset": offset},
            idempotent=True,
        )
        return self._model(SessionsPage, payload, "/workspaces/{id}/sessions")

    def _maybe_workspace(self, payload: Any, endpoint: str) -> Workspace | None:
        """These endpoints' response bodies are undocumented; parse if there is
        a workspace in there, otherwise report success as ``None``."""
        if isinstance(payload, dict) and payload.get("id"):
            return self._model(Workspace, payload, endpoint)
        return None

    # -- sessions -------------------------------------------------------------

    async def create_session(
        self,
        *,
        workspace_id: str,
        session_id: str | None = None,
        title: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        fast_mode: bool | None = None,
    ) -> Session:
        """``POST /v0/sessions``.

        ``session_id`` is a caller-supplied idempotency key — probe-verified: the
        returned session has exactly that id. Passing one makes this call safely
        retryable; without one it is never retried after delivery.
        """
        if agent is not None:
            resolved_agent, resolved_model, resolved_effort = validate_pairing(
                agent, model, effort
            )
            agent_value: str | None = resolved_agent.value
        else:
            if model is not None or effort is not None:
                raise ValueError(
                    "model/effort need an explicit agent to validate against"
                )
            agent_value, resolved_model, resolved_effort = None, None, None
        body: dict[str, Any] = {"workspaceId": workspace_id}
        if session_id is not None:
            body["sessionId"] = session_id
        if title is not None:
            body["title"] = title
        if agent_value is not None:
            body["agent"] = agent_value
        if resolved_model is not None:
            body["model"] = resolved_model
        if resolved_effort is not None:
            body["effort"] = resolved_effort
        if fast_mode is not None:
            body["fastMode"] = fast_mode
        payload = await self._request(
            "POST",
            "/sessions",
            endpoint="/sessions",
            json_body=body,
            idempotent=session_id is not None,
            message_id=session_id,
        )
        return self._model(Session, payload, "/sessions")

    async def get_session(self, session_id: str) -> Session:
        payload = await self._request(
            "GET",
            f"/sessions/{session_id}",
            endpoint="/sessions/{id}",
            idempotent=True,
            session_id=session_id,
        )
        return self._model(Session, payload, "/sessions/{id}")

    async def rename_session(self, session_id: str, title: str) -> Session | None:
        payload = await self._request(
            "POST",
            f"/sessions/{session_id}/rename",
            endpoint="/sessions/{id}/rename",
            json_body={"title": title},
            idempotent=True,
            session_id=session_id,
        )
        if isinstance(payload, dict) and payload.get("id"):
            return self._model(Session, payload, "/sessions/{id}/rename")
        return None

    async def archive_session(self, session_id: str) -> Session | None:
        payload = await self._request(
            "POST",
            f"/sessions/{session_id}/archive",
            endpoint="/sessions/{id}/archive",
            idempotent=True,
            session_id=session_id,
        )
        if isinstance(payload, dict) and payload.get("id"):
            return self._model(Session, payload, "/sessions/{id}/archive")
        return None

    async def post_message(
        self,
        session_id: str,
        text: str,
        message_id: str,
    ) -> PostMessageResult:
        """Send a prompt. ``message_id`` is the caller's idempotency key.

        Write the ``outbound_prompts`` row *before* calling this, and retry an
        :class:`Ambiguous` forever with the identical ``message_id`` — re-POSTing
        the same id dedupes (verified twice: one user echo, not two).

        The id also comes back as ``content.id`` on the user echo and as
        ``content.turnId`` on every message of the turn it triggers, which is how
        the poller attributes output to a prompt.
        """
        if not message_id:
            raise ValueError("post_message requires a caller-supplied message_id")
        payload = await self._request(
            "POST",
            f"/sessions/{session_id}/messages",
            endpoint="/sessions/{id}/messages",
            json_body={"message": text, "messageId": message_id},
            read_timeout=POST_MESSAGE_TIMEOUT_S,
            idempotent=True,
            message_id=message_id,
            session_id=session_id,
        )
        if isinstance(payload, dict) and not payload.get("messageId"):
            # The server echoes our id; if it ever doesn't, keep ours. It is the
            # key everything downstream correlates on.
            payload = {**payload, "messageId": message_id}
        return self._model(PostMessageResult, payload, "/sessions/{id}/messages")

    async def get_messages(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        after: str | None = None,
    ) -> MessagesPage:
        """The delivery path's only source of truth.

        ``after=<messageId>`` is an exclusive cursor returning ascending
        ``sessionIndex``; it **cannot** be combined with ``offset``.

        A 404 here is ambiguous by nature: an unknown or foreign ``after`` id also
        returns 404 with zero messages (probe-verified). Before concluding the
        session is ``DEAD``, re-check without the cursor.
        """
        if after is not None and offset is not None:
            raise ValueError("get_messages: `after` cannot be combined with `offset`")
        payload = await self._request(
            "GET",
            f"/sessions/{session_id}/messages",
            endpoint="/sessions/{id}/messages",
            params={"limit": limit, "offset": offset, "after": after},
            idempotent=True,
            session_id=session_id,
        )
        return self._model(MessagesPage, payload, "/sessions/{id}/messages")

    async def get_message(self, message_id: str) -> TranscriptMessage:
        """``GET /v0/messages/{messageId}`` — one envelope by its server id."""
        payload = await self._request(
            "GET",
            f"/messages/{message_id}",
            endpoint="/messages/{id}",
            idempotent=True,
        )
        return self._model(TranscriptMessage, payload, "/messages/{id}")

    async def get_session_status(self, session_id: str) -> SessionStatus:
        """A cadence knob and a UX hint. **Never** a gate on delivery.

        ``error`` is a third status alongside ``idle``/``working`` and persists
        indefinitely while the session still accepts POSTs.
        """
        payload = await self._request(
            "GET",
            f"/sessions/{session_id}/status",
            endpoint="/sessions/{id}/status",
            idempotent=True,
            session_id=session_id,
        )
        return self._model(SessionStatus, payload, "/sessions/{id}/status")

    async def cancel_session(self, session_id: str) -> CancelResult:
        """Asynchronous — poll the transcript, not the status, for the outcome."""
        payload = await self._request(
            "POST",
            f"/sessions/{session_id}/cancel",
            endpoint="/sessions/{id}/cancel",
            idempotent=True,
            session_id=session_id,
        )
        return self._model(CancelResult, payload, "/sessions/{id}/cancel")

    # -- SQL ------------------------------------------------------------------

    async def sql(self, query: str) -> SqlResult:
        """Read-only SELECT over the single view ``session_transcripts_view``.

        Callers must never concatenate user text into ``query`` — the blast radius
        is every workspace's transcripts. Use a fixed template with an escaped
        literal.
        """
        text = query.strip()
        if not text:
            raise ValueError("sql: empty query")
        if len(text) > SQL_MAX_QUERY_CHARS:
            raise ValueError(
                f"sql: query is {len(text)} chars, the API limit is "
                f"{SQL_MAX_QUERY_CHARS}"
            )
        if SQL_FORBIDDEN_TEXT in text.lower():
            raise ValueError(
                f"sql: the literal text {SQL_FORBIDDEN_TEXT!r} is rejected"
            )
        payload = await self._request(
            "POST",
            "/sql",
            endpoint="/sql",
            json_body={"query": text},
            read_timeout=SQL_TIMEOUT_S,
            idempotent=True,
            # Read-only: an unknown outcome is a failed read, not a lost write.
            write=False,
        )
        return self._model(SqlResult, payload, "/sql")


# ── helpers ──────────────────────────────────────────────────────────────────


async def _swallow(awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except Exception:  # pragma: no cover - telemetry must never break a call
        get_logger("ctb.conductor.client").warning("conductor.event_hook_failed")


def _decode(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _decode_safely(response: httpx.Response) -> Any:
    """Error bodies are best-effort: a proxy may return HTML."""
    try:
        return _decode(response)
    except ValueError:
        return {"userMessage": response.text[:500]} if response.text else None


def _request_id_of(response: httpx.Response) -> str | None:
    for header in ("x-request-id", "request-id", "cf-ray"):
        value = response.headers.get(header)
        if value:
            return value
    return None


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` is either delta-seconds or an HTTP date."""
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


# ── process-wide singleton ───────────────────────────────────────────────────

_client: ConductorClient | None = None


def get_client() -> ConductorClient:
    """The process-wide client, built on first use from the process settings."""
    global _client
    if _client is None:
        _client = ConductorClient()
    return _client


def set_client(client: ConductorClient | None) -> None:
    """Install a client explicitly (boot path and tests)."""
    global _client
    _client = client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
