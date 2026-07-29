"""Tests for the Conductor HTTP client.

Everything runs against ``httpx.MockTransport`` — no network, ever. Time is
injected (``clock``/``sleep``/``rng``), so backoff, the token bucket and the
circuit window are asserted exactly rather than slept through.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ctb import USER_AGENT
from ctb.conductor.client import (
    BACKOFF_CAP_S,
    CONNECT_TIMEOUT_S,
    POST_MESSAGE_TIMEOUT_S,
    READ_TIMEOUT_S,
    SQL_TIMEOUT_S,
    ApiEvent,
    CircuitState,
    ConductorClient,
    TokenBucket,
    TransportFailure,
)
from ctb.conductor.errors import (
    Ambiguous,
    ApiError,
    AuthFatal,
    CircuitOpen,
    NotFound,
    PairingError,
    RateLimited,
)
from ctb.conductor.models import PostState, SessionStatusValue, WorkspaceStatusValue
from ctb.settings import Settings
from tests.conftest import FAKE_API_KEY, FakeClock

# ── harness ──────────────────────────────────────────────────────────────────


class FakeSleeper:
    """A ``sleep`` that advances a :class:`FakeClock` instead of waiting."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)
        # Still yield, so concurrency behaves like the real thing.
        await asyncio.sleep(0)

    @property
    def total(self) -> float:
        return sum(self.calls)


class Recorder:
    """Collects every request the client makes and replies from a script."""

    def __init__(
        self,
        responder: Callable[[httpx.Request, int], httpx.Response],
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request, len(self.requests))

    @property
    def count(self) -> int:
        return len(self.requests)

    def body(self, index: int = 0) -> dict[str, Any]:
        raw = self.requests[index].content
        return json.loads(raw) if raw else {}


#: A body wide enough to satisfy several different response models at once, for
#: the tests that hit multiple endpoints with one canned reply. Every model is
#: ``extra="allow"``, so the surplus keys are ignored.
_ANY_SHAPE: dict[str, Any] = {
    "data": [],
    "hasMore": False,
    "messageId": "mid-1",
    "state": "sent",
    "status": "idle",
    "rows": [],
    "rowCount": 0,
}


def always(status: int, payload: Any = None, **headers: str) -> Recorder:
    def respond(_request: httpx.Request, _n: int) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers or None)

    return Recorder(respond)


def sequence(*responses: httpx.Response | Exception) -> Recorder:
    def respond(_request: httpx.Request, n: int) -> httpx.Response:
        item = responses[min(n, len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return Recorder(respond)


def make_client(
    recorder: Recorder,
    settings: Settings,
    *,
    clock: FakeClock | None = None,
    sleeper: FakeSleeper | None = None,
    max_attempts: int = 5,
    on_event: Any = None,
) -> tuple[ConductorClient, FakeClock, FakeSleeper]:
    the_clock = clock or FakeClock()
    the_sleeper = sleeper or FakeSleeper(the_clock)
    client = ConductorClient(
        api_key=FAKE_API_KEY,
        api_url=settings.conductor_api_url,
        transport=httpx.MockTransport(recorder),
        clock=the_clock,
        sleep=the_sleeper,
        rng=random.Random(1234),
        max_attempts=max_attempts,
        on_event=on_event,
    )
    return client, the_clock, the_sleeper


# ── headers, URLs, timeouts ──────────────────────────────────────────────────


async def test_every_request_sends_an_explicit_user_agent(settings: Settings) -> None:
    """The proxy 403s some default client signatures — the UA is load-bearing."""
    recorder = always(200, _ANY_SHAPE)
    client, _, _ = make_client(recorder, settings)
    async with client:
        await client.list_projects()
        await client.get_session_status("sess-1")
        await client.post_message("sess-1", "hi", "mid-1")

    assert recorder.count == 3
    for request in recorder.requests:
        assert request.headers["user-agent"] == USER_AGENT
        assert request.headers["authorization"] == f"Bearer {FAKE_API_KEY}"
        assert request.headers["accept"] == "application/json"


async def test_get_me_uses_the_api_root_not_v0(settings: Settings) -> None:
    recorder = always(200, {"userId": "u-1", "email": "a@b.c"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        me = await client.get_me()

    assert str(recorder.requests[0].url) == "https://api.conductor.build/me"
    assert me.user_id == "u-1"


async def test_per_endpoint_timeouts(settings: Settings) -> None:
    recorder = always(200, _ANY_SHAPE)
    client, _, _ = make_client(recorder, settings)
    async with client:
        await client.get_session_status("sess-1")
        await client.post_message("sess-1", "hi", "mid-1")
        await client.sql("SELECT 1 FROM session_transcripts_view")

    reads = [r.extensions["timeout"]["read"] for r in recorder.requests]
    connects = [r.extensions["timeout"]["connect"] for r in recorder.requests]
    assert reads == [READ_TIMEOUT_S, POST_MESSAGE_TIMEOUT_S, SQL_TIMEOUT_S]
    assert connects == [CONNECT_TIMEOUT_S] * 3


# ── token bucket ─────────────────────────────────────────────────────────────


async def test_token_bucket_refills_at_the_configured_rate() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    bucket = TokenBucket(rate=5.0, burst=10.0, clock=clock, sleep=sleeper)

    for _ in range(10):
        assert await bucket.acquire() == 0.0  # burst is free
    assert sleeper.total == 0.0

    waited = await bucket.acquire()  # 11th must wait 1/5s
    assert waited == pytest.approx(0.2)
    assert clock.now == pytest.approx(1_000.2)


async def test_client_paces_requests_at_five_per_second(settings: Settings) -> None:
    recorder = always(200, {"data": [], "hasMore": False})
    client, clock, sleeper = make_client(recorder, settings)
    start = clock.now
    async with client:
        for _ in range(15):
            await client.list_projects()

    # 10 free from the burst, then 5 more at 5/s.
    assert recorder.count == 15
    assert clock.now - start == pytest.approx(1.0)
    assert sleeper.total == pytest.approx(1.0)


async def test_semaphore_caps_in_flight_requests(settings: Settings) -> None:
    in_flight = 0
    peak = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        for _ in range(3):
            await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(200, json={"data": [], "hasMore": False})

    clock = FakeClock()
    client = ConductorClient(
        api_key=FAKE_API_KEY,
        api_url=settings.conductor_api_url,
        transport=httpx.MockTransport(respond),
        clock=clock,
        sleep=FakeSleeper(clock),
        rng=random.Random(0),
    )
    async with client:
        await asyncio.gather(*(client.list_projects() for _ in range(24)))

    assert peak <= 8


# ── circuit breaker ──────────────────────────────────────────────────────────


async def test_circuit_opens_after_three_5xx_then_half_opens(
    settings: Settings,
) -> None:
    healthy = {"data": [], "hasMore": False}
    recorder = sequence(
        httpx.Response(500, json={"userMessage": "boom"}),
        httpx.Response(500, json={"userMessage": "boom"}),
        httpx.Response(500, json={"userMessage": "boom"}),
        httpx.Response(200, json=healthy),
    )
    client, clock, _ = make_client(recorder, settings, max_attempts=1)
    async with client:
        for _ in range(3):
            with pytest.raises(ApiError):
                await client.list_projects()
        assert client.circuit.state is CircuitState.OPEN
        assert client.circuit.consecutive_failures == 3

        # Fails fast: no fourth request is made.
        with pytest.raises(CircuitOpen) as caught:
            await client.list_projects()
        assert recorder.count == 3
        assert 0 < caught.value.retry_after <= 72.0

        # After the (jittered 60s) window the next caller is the half-open probe.
        clock.advance(caught.value.retry_after + 0.001)
        await client.list_projects()
        assert client.circuit.state is CircuitState.CLOSED
        assert recorder.count == 4


async def test_half_open_probe_failure_reopens_the_circuit(
    settings: Settings,
) -> None:
    recorder = always(503, {"userMessage": "down"})
    client, clock, _ = make_client(recorder, settings, max_attempts=1)
    async with client:
        for _ in range(3):
            with pytest.raises(ApiError):
                await client.list_projects()
        opened_at = client.circuit.retry_after()
        clock.advance(opened_at + 0.001)

        with pytest.raises(ApiError):  # the probe itself fails
            await client.list_projects()
        assert client.circuit.state is CircuitState.OPEN
        assert client.circuit.retry_after() > 0

        with pytest.raises(CircuitOpen):
            await client.list_projects()
        assert recorder.count == 4


def sick(*paths: str) -> Recorder:
    """500 for the named paths, a healthy reply for everything else."""

    def respond(request: httpx.Request, _n: int) -> httpx.Response:
        if request.url.path.endswith(paths):
            return httpx.Response(500, json={"userMessage": "boom"})
        return httpx.Response(200, json=_ANY_SHAPE | {"status": "ready"})

    return Recorder(respond)


async def test_one_sick_resource_is_isolated_not_the_whole_api(
    settings: Settings,
) -> None:
    """The bug: a workspace whose status 500s forever froze its whole tenant.

    Its poller wins every half-open probe slot, so the window never closes and
    the archive that would retire it fails fast behind it.
    """
    recorder = sick("/workspaces/ws-sick/status")
    client, _, _ = make_client(recorder, settings, max_attempts=1)
    async with client:
        for _ in range(3):
            with pytest.raises(ApiError):
                await client.get_workspace_status("ws-sick")
        assert client.circuit.state is CircuitState.CLOSED
        assert "GET /workspaces/ws-sick/status" in client.circuit.isolated

        # That one path now fails fast…
        with pytest.raises(CircuitOpen):
            await client.get_workspace_status("ws-sick")
        assert recorder.count == 3

        # …and everything else, the archive included, still goes out.
        await client.archive_workspace("ws-sick")
        await client.list_projects()
        await client.get_workspace_status("ws-well")
        assert recorder.count == 6


async def test_two_sick_resources_still_open_the_whole_circuit(
    settings: Settings,
) -> None:
    recorder = sick("/status")
    client, _, _ = make_client(recorder, settings, max_attempts=1)
    async with client:
        for workspace in ("ws-a", "ws-b", "ws-a"):
            with pytest.raises(ApiError):
                await client.get_workspace_status(workspace)
        assert client.circuit.state is CircuitState.OPEN
        with pytest.raises(CircuitOpen):
            await client.list_projects()
    assert recorder.count == 3


async def test_a_still_sick_resource_re_isolates_on_its_first_retry(
    settings: Settings,
) -> None:
    recorder = sick("/workspaces/ws-sick/status")
    client, clock, _ = make_client(recorder, settings, max_attempts=1)
    async with client:
        for _ in range(3):
            with pytest.raises(ApiError):
                await client.get_workspace_status("ws-sick")
        held = client.circuit.isolated["GET /workspaces/ws-sick/status"]
        clock.advance(held.until - clock() + 0.001)

        with pytest.raises(ApiError):  # the one call let through still fails
            await client.get_workspace_status("ws-sick")
        assert client.circuit.state is CircuitState.CLOSED
        with pytest.raises(CircuitOpen):
            await client.get_workspace_status("ws-sick")
        assert recorder.count == 4


async def test_a_recovered_resource_leaves_isolation(settings: Settings) -> None:
    recorder = sequence(
        *(httpx.Response(500, json={"userMessage": "boom"}) for _ in range(3)),
        httpx.Response(200, json=_ANY_SHAPE | {"status": "ready"}),
    )
    client, clock, _ = make_client(recorder, settings, max_attempts=1)
    async with client:
        for _ in range(3):
            with pytest.raises(ApiError):
                await client.get_workspace_status("ws-flaky")
        held = client.circuit.isolated["GET /workspaces/ws-flaky/status"]
        clock.advance(held.until - clock() + 0.001)

        status = await client.get_workspace_status("ws-flaky")
        assert status.status is WorkspaceStatusValue.READY
        assert client.circuit.isolated == {}


async def test_a_4xx_does_not_open_the_circuit(settings: Settings) -> None:
    recorder = always(400, {"userMessage": "bad model"})
    client, _, _ = make_client(recorder, settings, max_attempts=1)
    async with client:
        for _ in range(4):
            with pytest.raises(ApiError):
                await client.list_projects()
    assert client.circuit.state is CircuitState.CLOSED
    assert recorder.count == 4


# ── rate limiting ────────────────────────────────────────────────────────────


async def test_429_honours_retry_after_and_opens_the_circuit(
    settings: Settings,
) -> None:
    recorder = sequence(
        httpx.Response(
            429, json={"userMessage": "slow down"}, headers={"retry-after": "7"}
        ),
        httpx.Response(200, json={"data": [], "hasMore": False}),
    )
    client, _, sleeper = make_client(recorder, settings)
    async with client:
        page = await client.list_projects()

    assert page.data == []
    # Slept exactly Retry-After, not the backoff curve.
    assert sleeper.calls == [7.0]
    # Everyone else backs off too, until the window this call already waited out.
    assert client.circuit.opened_by is not None
    assert "429" in client.circuit.opened_by


async def test_429_with_a_long_retry_after_is_handed_back(settings: Settings) -> None:
    recorder = always(429, {"userMessage": "much later"}, **{"retry-after": "600"})
    client, _, sleeper = make_client(recorder, settings)
    async with client:
        with pytest.raises(RateLimited) as caught:
            await client.get_session_status("sess-1")

    assert caught.value.retry_after == 600.0
    assert sleeper.calls == []  # a poll tick must not block for ten minutes
    assert recorder.count == 1


# ── auth / not found ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_fatal_and_never_retried(
    settings: Settings, status: int
) -> None:
    recorder = always(status, {"userMessage": "bad key"})
    client, _, sleeper = make_client(recorder, settings)
    async with client:
        with pytest.raises(AuthFatal) as caught:
            await client.get_messages("sess-1")

    assert caught.value.status == status
    assert caught.value.retryable is False
    assert recorder.count == 1  # retrying a bad key risks a lockout
    assert sleeper.calls == []
    assert client.auth_failures == 1


async def test_a_transient_403_does_not_latch_the_bot_dead(
    settings: Settings,
) -> None:
    """A 2xx clears ``auth_failures``; the supervisor treats it as fatal.

    CLAUDE.md: the API sits behind a proxy that 403s some client signatures. If
    a hiccup were permanent, ``supervisor.auth_fatal`` would cancel every
    poller for the life of the process while commands kept answering — the bot
    would look alive and never deliver another reply.
    """
    recorder = sequence(
        httpx.Response(403, json={"userMessage": "proxy said no"}),
        httpx.Response(200, json=_ANY_SHAPE),
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(AuthFatal):
            await client.get_messages("sess-1")
        assert client.auth_failures == 1

        await client.get_session_status("sess-1")
        assert client.auth_failures == 0
        assert client.health()["auth_failures"] == 0


async def test_a_genuinely_bad_key_keeps_the_fatal_latch(settings: Settings) -> None:
    """No 2xx ever arrives, so nothing resets the counter."""
    recorder = always(401, {"userMessage": "bad key"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        for _ in range(3):
            with pytest.raises(AuthFatal):
                await client.get_messages("sess-1")
    assert client.auth_failures == 3


async def test_auth_failure_on_a_post_is_not_ambiguous(settings: Settings) -> None:
    recorder = always(401, {"userMessage": "bad key"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(AuthFatal):
            await client.post_message("sess-1", "hi", "mid-1")
    assert recorder.count == 1


async def test_404_raises_not_found_and_is_not_retried(settings: Settings) -> None:
    recorder = always(404, {"userMessage": "no such session"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(NotFound) as caught:
            await client.get_session_status("gone")

    assert caught.value.retryable is False
    assert recorder.count == 1


# ── retry policy ─────────────────────────────────────────────────────────────


async def test_backoff_is_full_jitter_and_bounded(settings: Settings) -> None:
    recorder = always(503, {"userMessage": "down"})
    client, _, sleeper = make_client(recorder, settings, max_attempts=5)
    async with client:
        with pytest.raises(ApiError):
            await client.get_messages("sess-1")

    assert recorder.count == 5  # attempts, not retries
    assert len(sleeper.calls) == 4
    for attempt, delay in enumerate(sleeper.calls, start=1):
        assert 0.0 <= delay <= min(BACKOFF_CAP_S, 0.5 * 2 ** (attempt - 1))
    assert max(sleeper.calls) <= BACKOFF_CAP_S


async def test_retryable_false_in_the_body_is_never_retried(
    settings: Settings,
) -> None:
    recorder = always(503, {"userMessage": "permanent", "retryable": False})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(ApiError) as caught:
            await client.get_messages("sess-1")

    assert caught.value.retryable is False
    assert recorder.count == 1


async def test_retryable_true_in_the_body_is_retried_on_a_4xx(
    settings: Settings,
) -> None:
    recorder = sequence(
        httpx.Response(400, json={"userMessage": "transient", "retryable": True}),
        httpx.Response(200, json={"status": "idle"}),
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        status = await client.get_session_status("sess-1")

    assert status.status is SessionStatusValue.IDLE
    assert recorder.count == 2


async def test_transport_failure_on_a_read_is_retried_then_surfaced(
    settings: Settings,
) -> None:
    recorder = sequence(httpx.ReadTimeout("timed out"))
    client, _, sleeper = make_client(recorder, settings, max_attempts=3)
    async with client:
        with pytest.raises(TransportFailure) as caught:
            await client.get_messages("sess-1")

    assert caught.value.attempts == 3
    assert caught.value.retryable is True
    assert recorder.count == 3
    assert len(sleeper.calls) == 2


async def test_a_2xx_that_is_not_json_is_retried(settings: Settings) -> None:
    recorder = sequence(
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json={"status": "working"}),
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        status = await client.get_session_status("sess-1")

    assert status.status is SessionStatusValue.WORKING
    assert recorder.count == 2


# ── writes: ambiguity ────────────────────────────────────────────────────────


async def test_post_message_retries_then_raises_ambiguous(settings: Settings) -> None:
    recorder = sequence(httpx.ReadTimeout("no answer"))
    client, _, _ = make_client(recorder, settings, max_attempts=3)
    async with client:
        with pytest.raises(Ambiguous) as caught:
            await client.post_message("sess-1", "hello", "mid-42")

    assert caught.value.message_id == "mid-42"
    assert caught.value.idempotent is True  # retry with the SAME id, forever
    assert recorder.count == 3


async def test_post_message_5xx_is_ambiguous_not_a_server_error(
    settings: Settings,
) -> None:
    recorder = always(502, {"userMessage": "bad gateway"})
    client, _, _ = make_client(recorder, settings, max_attempts=2)
    async with client:
        with pytest.raises(Ambiguous) as caught:
            await client.post_message("sess-1", "hello", "mid-7")

    assert caught.value.message_id == "mid-7"
    assert recorder.count == 2


async def test_create_workspace_is_never_blind_retried(settings: Settings) -> None:
    """No idempotency key exists for POST /workspaces — one shot, then reconcile."""
    recorder = sequence(httpx.ReadTimeout("no answer"))
    client, _, _ = make_client(recorder, settings, max_attempts=5)
    async with client:
        with pytest.raises(Ambiguous) as caught:
            await client.create_workspace(project_id="proj-1", agent="claude")

    assert recorder.count == 1
    assert caught.value.idempotent is False
    assert caught.value.message_id is None


async def test_create_workspace_retries_when_the_request_never_left(
    settings: Settings,
) -> None:
    """A connect failure provably created no side effect, so a replay is safe."""
    recorder = sequence(
        httpx.ConnectError("dns"),
        httpx.Response(201, json={"workspaceId": "ws-1", "sessionId": "sess-1"}),
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        created = await client.create_workspace(
            repository_url="https://github.com/x/y", agent="claude", model="opus-5-1m"
        )

    assert created.workspace_id == "ws-1"
    assert recorder.count == 2


async def test_create_workspace_5xx_is_ambiguous_immediately(
    settings: Settings,
) -> None:
    recorder = always(500, {"userMessage": "boom"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(Ambiguous):
            await client.create_workspace(project_id="proj-1", agent="claude")
    assert recorder.count == 1


async def test_sql_transport_failure_is_a_read_not_a_write(settings: Settings) -> None:
    recorder = sequence(httpx.ReadTimeout("slow view"))
    client, _, _ = make_client(recorder, settings, max_attempts=2)
    async with client:
        with pytest.raises(TransportFailure):
            await client.sql("SELECT session_id FROM session_transcripts_view")
    assert recorder.count == 2


# ── request shapes ───────────────────────────────────────────────────────────


async def test_get_messages_rejects_after_with_offset(settings: Settings) -> None:
    recorder = always(200, {"data": []})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(ValueError, match="after"):
            await client.get_messages("sess-1", after="m-1", offset=10)
    assert recorder.count == 0


async def test_get_messages_sends_the_cursor(settings: Settings) -> None:
    recorder = always(
        200,
        {
            "data": [
                {
                    "id": "sess-1:3:0",
                    "sessionId": "sess-1",
                    "sessionIndex": 3,
                    "type": "agent",
                    "content": {"turnId": "mid-1", "rawPayload": {"type": "assistant"}},
                    "receivedAt": "2026-07-26 02:00:37.434+00",
                }
            ],
            "hasMore": False,
        },
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        page = await client.get_messages("sess-1", after="sess-1:2:0", limit=100)

    url = recorder.requests[0].url
    assert url.path == "/v0/sessions/sess-1/messages"
    assert dict(url.params) == {"limit": "100", "after": "sess-1:2:0"}
    assert page.data[0].turn_id == "mid-1"
    assert page.max_session_index == 3


async def test_post_message_sends_the_idempotency_key(settings: Settings) -> None:
    recorder = always(201, {"messageId": "mid-9", "state": "queued"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        result = await client.post_message("sess-1", "do the thing", "mid-9")

    assert recorder.body() == {"message": "do the thing", "messageId": "mid-9"}
    assert result.message_id == "mid-9"
    assert result.state is PostState.QUEUED


async def test_post_message_requires_a_message_id(settings: Settings) -> None:
    recorder = always(201, {})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(ValueError, match="message_id"):
            await client.post_message("sess-1", "hi", "")
    assert recorder.count == 0


async def test_create_session_accepts_a_caller_supplied_session_id(
    settings: Settings,
) -> None:
    """Probe-verified: POST /v0/sessions DOES take a sessionId idempotency key."""
    recorder = sequence(
        httpx.Response(500, json={"userMessage": "boom"}),
        httpx.Response(201, json={"id": "sess-abc", "workspaceId": "ws-1"}),
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        session = await client.create_session(
            workspace_id="ws-1",
            session_id="sess-abc",
            agent="claude",
            model="opus-5-1m",
            effort="high",
        )

    assert recorder.body() == {
        "workspaceId": "ws-1",
        "sessionId": "sess-abc",
        "agent": "claude",
        "model": "opus-5-1m",
        "effort": "high",
    }
    assert session.id == "sess-abc"
    assert recorder.count == 2  # safe to replay, because we chose the id


async def test_create_session_without_an_id_is_not_retried(settings: Settings) -> None:
    recorder = always(500, {"userMessage": "boom"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(Ambiguous):
            await client.create_session(workspace_id="ws-1")
    assert recorder.count == 1


async def test_pairing_is_validated_before_any_http(settings: Settings) -> None:
    recorder = always(201, {"workspaceId": "ws-1"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(PairingError):
            await client.create_workspace(
                project_id="p-1", agent="claude", model="gpt-5.5"
            )
    assert recorder.count == 0


async def test_create_workspace_needs_exactly_one_source(settings: Settings) -> None:
    recorder = always(201, {"workspaceId": "ws-1"})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(ValueError):
            await client.create_workspace(agent="claude")
        with pytest.raises(ValueError):
            await client.create_workspace(
                agent="claude", project_id="p-1", repository_url="https://x/y"
            )
    assert recorder.count == 0


async def test_workspace_status_and_sessions(settings: Settings) -> None:
    recorder = sequence(
        httpx.Response(200, json={"status": "sleeping", "lifecycleStep": "hibernated"}),
        httpx.Response(200, json={"data": [{"id": "s-1", "workspaceId": "ws-1"}]}),
        httpx.Response(200, json={"status": "canceling", "canceledQueuedMessages": 2}),
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        status = await client.get_workspace_status("ws-1")
        sessions = await client.list_workspace_sessions("ws-1")
        canceled = await client.cancel_session("s-1")

    assert status.status is WorkspaceStatusValue.SLEEPING
    assert status.status.is_waking is True
    assert [s.id for s in sessions.data] == ["s-1"]
    assert canceled.canceled_queued_messages == 2


async def test_error_status_is_surfaced_verbatim(settings: Settings) -> None:
    recorder = always(
        200,
        {"status": "error", "errorMessage": "Codex ChatGPT auth not found"},
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        status = await client.get_session_status("sess-1")

    assert status.is_error is True
    assert status.error_text == "Codex ChatGPT auth not found"


async def test_rename_and_archive_tolerate_an_empty_body(settings: Settings) -> None:
    recorder = always(204)
    client, _, _ = make_client(recorder, settings)
    async with client:
        assert await client.rename_workspace("ws-1", "tg-42-abc") is None
        assert await client.archive_workspace("ws-1") is None
        assert await client.rename_session("s-1", "Title") is None
        assert await client.archive_session("s-1") is None
    assert recorder.count == 4
    assert recorder.body(0) == {"name": "tg-42-abc"}


async def test_get_message_by_id(settings: Settings) -> None:
    recorder = always(
        200,
        {
            "id": "sess-1:7:0",
            "sessionId": "sess-1",
            "sessionIndex": 7,
            "type": "userMessage",
            "content": {"id": "mid-1", "type": "userMessage", "message": "hi"},
            "receivedAt": "2026-07-26 02:00:37.434+00",
        },
    )
    client, _, _ = make_client(recorder, settings)
    async with client:
        message = await client.get_message("sess-1:7:0")

    assert recorder.requests[0].url.path == "/v0/messages/sess-1:7:0"
    assert message.witnesses_prompt("mid-1") is True


# ── SQL guardrails ───────────────────────────────────────────────────────────


async def test_sql_rejects_oversized_and_forbidden_queries(settings: Settings) -> None:
    recorder = always(200, {"rows": [], "rowCount": 0})
    client, _, _ = make_client(recorder, settings)
    async with client:
        with pytest.raises(ValueError):
            await client.sql("   ")
        with pytest.raises(ValueError):
            await client.sql("SELECT 1 FROM t WHERE x = 'SET_CONFIG'")
        with pytest.raises(ValueError):
            await client.sql("SELECT " + "a" * 10_001)
        result = await client.sql("SELECT session_id FROM session_transcripts_view")

    assert recorder.count == 1
    assert result.row_count == 0
    assert recorder.body() == {
        "query": "SELECT session_id FROM session_transcripts_view"
    }


# ── telemetry ────────────────────────────────────────────────────────────────


async def test_every_attempt_is_recorded_for_health(settings: Settings) -> None:
    seen: list[ApiEvent] = []
    recorder = sequence(
        httpx.Response(503, json={"userMessage": "down"}),
        httpx.Response(200, json={"status": "idle"}),
    )
    client, _, _ = make_client(recorder, settings, max_attempts=2, on_event=seen.append)
    async with client:
        await client.get_session_status("sess-1")

    assert [(e.attempt, e.status_code, e.ok) for e in seen] == [
        (1, 503, False),
        (2, 200, True),
    ]
    assert {e.endpoint for e in seen} == {"/sessions/{id}/status"}
    assert {e.session_id for e in seen} == {"sess-1"}
    row = seen[0].as_row()
    assert row["ok"] == 0 and row["endpoint"] == "/sessions/{id}/status"

    health = client.health()
    assert health["requests"] == 2
    assert health["retries"] == 1
    assert health["circuit"]["state"] == "closed"
    assert len(client.recent_events()) == 2


async def test_an_exploding_event_hook_never_breaks_a_call(
    settings: Settings,
) -> None:
    def boom(_event: ApiEvent) -> None:
        raise RuntimeError("sink is down")

    recorder = always(200, {"status": "idle"})
    client, _, _ = make_client(recorder, settings, on_event=boom)
    async with client:
        status = await client.get_session_status("sess-1")
    assert status.status is SessionStatusValue.IDLE


async def test_an_async_event_hook_is_awaited(settings: Settings) -> None:
    seen: list[ApiEvent] = []

    async def sink(event: ApiEvent) -> None:
        await asyncio.sleep(0)
        seen.append(event)

    recorder = always(200, {"status": "idle"})
    client, _, _ = make_client(recorder, settings, on_event=sink)
    async with client:
        await client.get_session_status("sess-1")
    # aclose() drains in-flight hook tasks.
    assert len(seen) == 1


# ── no singleton ─────────────────────────────────────────────────────────────


def test_the_module_exposes_no_process_wide_client() -> None:
    """Deleting the global is the whole cross-tenant safety argument.

    While ``get_client()`` existed, a handler that forgot its tenant would
    silently read another organisation's data. Now it raises by name.
    """
    import ctb.conductor.client as module

    assert not hasattr(module, "get_client")
    assert not hasattr(module, "set_client")
    assert not hasattr(module, "close_client")


async def test_a_client_is_bound_to_the_key_it_was_given(
    settings: Settings,
) -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"data": [], "hasMore": False})

    async with ConductorClient(
        api_key="tenant-a-key-0001",
        api_url=settings.conductor_api_url,
        transport=httpx.MockTransport(record),
    ) as client:
        await client.list_projects()

    assert seen == ["Bearer tenant-a-key-0001"]
