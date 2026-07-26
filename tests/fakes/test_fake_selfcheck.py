"""Proof that the fake Conductor is not lying.

``test_machine.py``, ``test_cursor.py`` and ``test_dedup.py`` all take
``fake_conductor`` at its word, so every property they rely on is pinned here:
the envelope shape, the ``sessionIndex`` gaps, the 404-on-bad-cursor, the
messageId dedup, and the advertised behaviour of each named scenario. If this
file goes green under a fake that lies, everything downstream is worthless.

Where a property was measured during the Phase 0 probe, the assertion names the
measurement (see ``docs/HANDOFF.md``).
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from ctb.conductor.errors import NotFound
from ctb.conductor.models import (
    CancelResult,
    MessagesPage,
    PostMessageResult,
    SessionStatus,
    SessionStatusValue,
    TranscriptMessage,
    WorkspaceStatusValue,
)
from tests.fakes.fake_conductor import (
    SCENARIOS,
    Advance,
    FakeConductor,
    PostFailure,
    Scenario,
    Tick,
    assistant,
    cancelled_turn,
    double_prompt,
    error_mid_turn,
    fast_turn,
    queued_idle_trap,
    replay_attack,
    result,
    slow_wake,
    state_changed,
    status_5xx,
    status_flapping,
    system_init,
    tool_use,
)

ENVELOPE_ID = re.compile(r"^[0-9a-f-]{36}:\d+:\d+$")


def _simple(**options: Any) -> Scenario:
    """A one-turn scenario built inline, for the low-level envelope checks."""
    fake = FakeConductor()
    session = fake.add_session(
        script=[
            Tick(SessionStatusValue.WORKING, emit=(state_changed(), assistant("hi"))),
            Tick(SessionStatusValue.IDLE, emit=(result("hi"),)),
            Tick(SessionStatusValue.IDLE),
        ],
        **options,
    )
    return Scenario(name="simple", fake=fake, session=session, expectation="")


# ── envelope shape ───────────────────────────────────────────────────────────


def test_envelope_has_exactly_the_probed_keys() -> None:
    scenario = _simple()
    scenario.start()
    scenario.session.poll()

    for envelope in scenario.session.transcript:
        assert set(envelope) == {
            "id",
            "sessionId",
            "sessionIndex",
            "type",
            "content",
            "receivedAt",
        }
        assert envelope["type"] in ("userMessage", "agent")
        assert ENVELOPE_ID.match(envelope["id"]), envelope["id"]
        assert envelope["id"].startswith(f"{scenario.session_id}:")
        assert envelope["sessionId"] == scenario.session_id
        # Postgres-ish, not strict ISO-8601 — kept verbatim by TranscriptMessage.
        assert envelope["receivedAt"].endswith("+00")


def test_posted_message_id_is_not_the_envelope_id() -> None:
    """Probe 1a: envelope ids are server-assigned composites."""
    scenario = _simple()
    message_id = scenario.start()
    scenario.session.poll()

    ids = {envelope["id"] for envelope in scenario.session.transcript}
    assert message_id not in ids


def test_message_id_surfaces_as_content_id_and_turn_id() -> None:
    """Probe 1b/1c: ``content.id`` on the echo, ``content.turnId`` everywhere."""
    scenario = _simple()
    message_id = scenario.start("do the thing")
    scenario.session.poll()
    scenario.session.poll()

    messages = scenario.session.messages_model()
    echoes = [m for m in messages if m.is_user_echo]
    assert len(echoes) == 1
    assert echoes[0].content_id == message_id
    assert echoes[0].prompt_text == "do the thing"
    assert echoes[0].witnesses_prompt(message_id)

    assert all(m.turn_id == message_id for m in messages)
    assert all(m.belongs_to_turn(message_id) for m in messages)
    agent_messages = [m for m in messages if m.is_agent]
    assert agent_messages
    assert all(m.user_message_id == message_id for m in agent_messages)


def test_session_index_is_monotonic_but_not_gapless() -> None:
    """Probe: the live API produced 0, 2, 3, 4, 5, 6, 7 for one turn."""
    scenario = _simple()
    scenario.start()
    for _ in range(3):
        scenario.session.poll()

    indexes = [m["sessionIndex"] for m in scenario.session.transcript]
    assert len(indexes) >= 4
    assert indexes == sorted(indexes)
    assert len(set(indexes)) == len(indexes)
    assert indexes[-1] > len(indexes) - 1, "no gap — a gapless fake hides real bugs"
    assert indexes[0] == 0


def test_gaps_can_be_switched_off_but_are_on_by_default() -> None:
    scenario = _simple(index_gaps=False)
    scenario.start()
    for _ in range(3):
        scenario.session.poll()
    indexes = [m["sessionIndex"] for m in scenario.session.transcript]
    assert indexes == list(range(len(indexes)))


def test_payload_shapes_classify_the_way_the_models_expect() -> None:
    fake = FakeConductor()
    session = fake.add_session(
        script=[
            Tick(
                SessionStatusValue.WORKING,
                emit=(
                    state_changed(),
                    system_init(),
                    assistant("prose"),
                    tool_use("Bash", tool_input={"command": "pytest -q"}),
                    result("finished"),
                ),
            )
        ]
    )
    session.post()
    poll = session.poll()

    kinds = [m.raw_payload_type for m in poll.messages if m.is_agent]
    assert kinds == ["system", "system", "assistant", "assistant", "result"]
    subtypes = [m.raw_payload_subtype for m in poll.messages if m.is_agent]
    assert subtypes[:2] == ["session_state_changed", "init"]

    prose = [m for m in poll.messages if m.is_assistant_text]
    assert prose[0].blocks == [{"type": "text", "text": "prose"}]

    assert poll.delta is not None
    assert poll.delta.tool_calls == 1
    assert poll.delta.has_agent_content is True
    assert poll.delta.witnessed_prompt_ids == frozenset({session.posted_ids[0]})
    assert poll.delta.max_index == max(m.session_index for m in poll.messages)
    # `texts` is assistant prose only: a `result` payload carries no block list.
    assert poll.texts == ("prose",)


def test_error_result_is_flagged() -> None:
    scenario = error_mid_turn()
    scenario.start()
    seen_error = False
    for _ in range(5):
        poll = scenario.session.poll()
        if poll.delta is not None and poll.delta.has_error_result:
            seen_error = True
    assert seen_error


# ── the messages endpoint ────────────────────────────────────────────────────


def test_after_is_exclusive_and_ascending() -> None:
    """Probe 3a–3c."""
    scenario = _simple()
    scenario.start()
    scenario.session.poll()
    scenario.session.poll()

    everything = scenario.session.messages()
    assert len(everything.data) >= 4
    cursor = everything.data[1]

    page = scenario.session.messages(after=cursor.id)
    assert [m.id for m in page.data] == [m.id for m in everything.data[2:]]
    assert all(m.session_index > cursor.session_index for m in page.data)
    assert [m.session_index for m in page.data] == sorted(
        m.session_index for m in page.data
    )


def test_after_respects_limit_and_sets_has_more() -> None:
    """Probe 3d."""
    scenario = _simple()
    scenario.start()
    scenario.session.poll()
    scenario.session.poll()

    everything = scenario.session.messages()
    page = scenario.session.messages(after=everything.data[0].id, limit=2)
    assert len(page.data) == 2
    assert page.has_more is True

    tail = scenario.session.messages(after=page.data[-1].id, limit=50)
    assert tail.has_more is False


def test_bad_and_foreign_cursors_are_404_with_zero_messages() -> None:
    """Probe 4/5: no silent full replay."""
    fake = FakeConductor()
    first = fake.add_session(script=[Tick(emit=(assistant("a"),))])
    second = fake.add_session(script=[Tick(emit=(assistant("b"),))])
    first.poll()
    second.poll()

    with pytest.raises(NotFound) as garbage:
        first.messages(after="not-a-real-id")
    assert garbage.value.status == 404

    foreign_id = second.transcript[0]["id"]
    with pytest.raises(NotFound):
        first.messages(after=foreign_id)


async def test_bad_cursor_over_http_is_404_with_an_empty_data_list() -> None:
    fake = FakeConductor()
    session = fake.add_session(script=[Tick(emit=(assistant("a"),))])
    session.poll()

    async with fake.client() as http:
        response = await http.get(
            f"/sessions/{session.session_id}/messages",
            params={"after": "garbage"},
        )
    assert response.status_code == 404
    assert MessagesPage.model_validate(response.json()).data == []


def test_after_cannot_be_combined_with_offset() -> None:
    scenario = _simple()
    scenario.start()
    scenario.session.poll()
    cursor = scenario.session.transcript[0]["id"]
    with pytest.raises(Exception) as excinfo:
        scenario.session.messages(after=cursor, offset=0)
    assert getattr(excinfo.value, "status", None) == 400


def test_offset_paging_is_stable() -> None:
    """Probe 6."""
    scenario = _simple()
    scenario.start()
    scenario.session.poll()
    scenario.session.poll()

    first = scenario.session.messages(offset=0, limit=3)
    second = scenario.session.messages(offset=0, limit=3)
    assert [m.id for m in first.data] == [m.id for m in second.data]

    walked: list[str] = []
    offset = 0
    while True:
        page = scenario.session.messages(offset=offset, limit=2)
        walked.extend(m.id for m in page.data)
        offset += len(page.data)
        if not page.has_more or not page.data:
            break
    assert walked == [m.id for m in scenario.session.messages(limit=500).data]


# ── the dedup linchpin ───────────────────────────────────────────────────────


def test_reposting_the_same_message_id_dedupes() -> None:
    """Probe 7 — the linchpin of the whole crash-safety design."""
    scenario = _simple()
    message_id = scenario.session.prompt_ids[0]

    first = scenario.session.post("hello", message_id=message_id)
    second = scenario.session.post("hello", message_id=message_id)

    assert first.message_id == second.message_id == message_id
    assert scenario.session.echo_count(message_id) == 1
    assert scenario.session.posted_ids == (message_id,)
    assert scenario.session.duplicate_posts == 1


async def test_reposting_over_http_dedupes_and_returns_201() -> None:
    fake = FakeConductor()
    session = fake.add_session()
    message_id = session.prompt_ids[0]

    async with fake.client() as http:
        payload = {"message": "hello", "messageId": message_id}
        first = await http.post(
            f"/sessions/{session.session_id}/messages", json=payload
        )
        second = await http.post(
            f"/sessions/{session.session_id}/messages", json=payload
        )

    assert first.status_code == second.status_code == 201
    assert PostMessageResult.model_validate(first.json()).message_id == message_id
    assert PostMessageResult.model_validate(second.json()).state.value == "sent"
    assert session.echo_count(message_id) == 1


def test_an_ambiguous_post_can_land_before_the_response_is_lost() -> None:
    """The exact ``Ambiguous`` shape: the write took, the answer did not."""
    scenario = _simple()
    message_id = scenario.session.prompt_ids[0]
    scenario.session.fail_next_post(
        PostFailure(exc=httpx.ReadTimeout("boom"), landed=True)
    )

    with pytest.raises(httpx.ReadTimeout):
        scenario.session.post("hello", message_id=message_id)

    assert scenario.session.echo_count(message_id) == 1
    # Retrying with the identical id is safe — that is the whole design.
    retried = scenario.session.post("hello", message_id=message_id)
    assert retried.message_id == message_id
    assert scenario.session.echo_count(message_id) == 1


def test_a_failed_post_that_did_not_land_leaves_no_trace() -> None:
    scenario = _simple()
    message_id = scenario.session.prompt_ids[0]
    scenario.session.fail_next_post(PostFailure(status=503))

    with pytest.raises(Exception) as excinfo:
        scenario.session.post("hello", message_id=message_id)
    assert getattr(excinfo.value, "status", None) == 503
    assert scenario.session.echo_count(message_id) == 0
    assert scenario.session.posted_ids == ()


def test_workspace_create_has_no_idempotency_key() -> None:
    """A blind retry really does create a second workspace."""
    fake = FakeConductor()
    fake.add_project("example", project_id="proj-1")
    body = {"projectId": "proj-1", "name": "tg-42-nonce", "agent": "claude"}

    first = fake.create_workspace(**body)
    second = fake.create_workspace(**body)

    assert first.workspace_id != second.workspace_id
    assert fake.created_workspace_names == ["tg-42-nonce", "tg-42-nonce"]


def test_an_ambiguous_workspace_create_may_still_have_landed() -> None:
    """Why the nonce-in-the-name reconciliation exists."""
    fake = FakeConductor()
    fake.add_project("example", project_id="proj-1")
    fake.fail_next_workspace_create(
        PostFailure(exc=httpx.ConnectTimeout("boom"), landed=True)
    )

    with pytest.raises(httpx.ConnectTimeout):
        fake.create_workspace(projectId="proj-1", name="tg-42-nonce")

    assert fake.created_workspace_names == ["tg-42-nonce"]


def test_session_create_does_accept_a_caller_supplied_id() -> None:
    """Corrects PLAN.md — verified against the live API."""
    fake = FakeConductor()
    workspace = fake.add_workspace("api/x")
    body = {"workspaceId": workspace.id, "agent": "claude", "sessionId": "chosen-id"}

    assert fake.create_session(**body).id == "chosen-id"
    assert fake.create_session(**body).id == "chosen-id"
    assert len(fake.sessions) == 1


# ── transport plumbing ───────────────────────────────────────────────────────


async def test_the_transport_serves_a_real_httpx_client() -> None:
    scenario = _simple()
    async with scenario.fake.client() as http:
        await http.post(
            f"/sessions/{scenario.session_id}/messages",
            json={"message": "hi", "messageId": scenario.prompt_ids[0]},
        )
        status = SessionStatus.model_validate(
            (await http.get(f"/sessions/{scenario.session_id}/status")).json()
        )
        page = MessagesPage.model_validate(
            (await http.get(f"/sessions/{scenario.session_id}/messages")).json()
        )
        cancel = CancelResult.model_validate(
            (await http.post(f"/sessions/{scenario.session_id}/cancel")).json()
        )

    assert status.status is SessionStatusValue.WORKING
    assert page.data and isinstance(page.data[0], TranscriptMessage)
    assert cancel.canceled_queued_messages == 0
    assert [c.method for c in scenario.fake.calls] == ["POST", "GET", "GET", "POST"]


async def test_a_default_user_agent_is_rejected_like_the_real_proxy() -> None:
    fake = FakeConductor()
    session = fake.add_session()
    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url=fake.base_url,
        headers={"Authorization": "Bearer k"},
    ) as http:
        response = await http.get(f"/sessions/{session.session_id}/status")
    assert response.status_code == 403


async def test_a_missing_bearer_token_is_401() -> None:
    fake = FakeConductor()
    session = fake.add_session()
    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url=fake.base_url,
        headers={"User-Agent": "conductor-tg-bot/test"},
    ) as http:
        response = await http.get(f"/sessions/{session.session_id}/status")
    assert response.status_code == 401


async def test_me_is_at_the_root_and_v0_me_404s() -> None:
    fake = FakeConductor()
    async with fake.client() as http:
        root = await http.get(fake.me_url)
        versioned = await http.get("/me")
    assert root.status_code == 200
    assert root.json()["apiKey"]["name"] == "fake-api-key"
    assert versioned.status_code == 404


async def test_sql_enforces_the_documented_guard_rails() -> None:
    scenario = _simple()
    scenario.start()
    scenario.session.poll()

    async with scenario.fake.client() as http:
        ok = await http.post("/sql", json={"query": "SELECT * FROM x LIMIT 5"})
        write = await http.post("/sql", json={"query": "DELETE FROM x"})
        multi = await http.post("/sql", json={"query": "SELECT 1; SELECT 2"})
        config = await http.post(
            "/sql", json={"query": "SELECT set_config('a','b',true)"}
        )
        oversize = await http.post("/sql", json={"query": "SELECT " + "x" * 10_001})

    assert ok.status_code == 200
    assert ok.json()["rowCount"] == 1
    assert "transcript" in ok.json()["rows"][0]
    assert (write.status_code, multi.status_code) == (400, 400)
    assert (config.status_code, oversize.status_code) == (400, 400)


async def test_the_timeline_advances_once_per_poll_regardless_of_call_order() -> None:
    """``Advance.ANY``: ``/messages`` + ``/status`` in either order is one tick."""
    scenario = queued_idle_trap()
    async with scenario.fake.client() as http:
        seen: list[str] = []
        for i in range(4):
            path = f"/sessions/{scenario.session_id}"
            if i % 2:
                await http.get(f"{path}/messages")
                status = await http.get(f"{path}/status")
            else:
                status = await http.get(f"{path}/status")
                await http.get(f"{path}/messages")
            seen.append(status.json()["status"])
    assert seen == ["idle", "idle", "idle", "working"]


def test_manual_advance_never_moves_on_its_own() -> None:
    scenario = queued_idle_trap(advance=Advance.MANUAL)
    scenario.start()
    for _ in range(5):
        assert scenario.session.status().status is SessionStatusValue.IDLE
        scenario.session.messages()
    assert scenario.session.tick_index == -1


# ── the named scenarios behave as advertised ─────────────────────────────────


def test_queued_idle_trap_never_shows_working_before_tick_four() -> None:
    scenario = queued_idle_trap()
    scenario.start()

    polls = [scenario.session.poll() for _ in range(7)]
    statuses = [p.status.value if p.status else None for p in polls]
    assert statuses == [
        SessionStatusValue.IDLE,
        SessionStatusValue.IDLE,
        SessionStatusValue.IDLE,
        SessionStatusValue.WORKING,
        SessionStatusValue.IDLE,
        SessionStatusValue.IDLE,
        SessionStatusValue.IDLE,
    ]
    # The whole answer arrives on the first idle *after* working. Finalizing on
    # the first idle would have lost it.
    assert "the answer is 42" in polls[4].texts
    assert polls[0].delta is not None, "the user echo lands immediately"
    assert polls[0].delta.has_agent_content is False


def test_fast_turn_delivers_the_answer_without_ever_reporting_working() -> None:
    scenario = fast_turn()
    scenario.start()

    polls = [scenario.session.poll() for _ in range(5)]
    assert all(
        p.status is not None and p.status.value is SessionStatusValue.IDLE
        for p in polls
    )
    assert "done: 7" in polls[1].texts
    assert any(m.is_result for m in polls[1].messages)


def test_double_prompt_witnesses_both_prompts() -> None:
    scenario = double_prompt()
    first = scenario.start("first")
    witnessed: set[str] = set()
    second = ""

    for i in range(9):
        if i == 2:
            second = scenario.prompt_ids[1]
            scenario.session.post("second", message_id=second)
        poll = scenario.session.poll()
        if poll.delta is not None:
            witnessed |= poll.delta.witnessed_prompt_ids

    assert witnessed == {first, second}
    assert scenario.session.echo_count(first) == 1
    assert scenario.session.echo_count(second) == 1

    turn_ids = {m.turn_id for m in scenario.session.messages_model()}
    assert turn_ids == {first, second}


def test_error_mid_turn_has_content_pending_when_the_error_arrives() -> None:
    scenario = error_mid_turn()
    scenario.start()

    polls = [scenario.session.poll() for _ in range(6)]
    statuses = [p.status.value if p.status else None for p in polls]
    assert statuses[:2] == [SessionStatusValue.WORKING, SessionStatusValue.WORKING]
    assert all(s is SessionStatusValue.ERROR for s in statuses[2:])

    error_poll = polls[2]
    assert error_poll.status is not None
    assert error_poll.status.error_message == "Codex ChatGPT auth not found"
    assert "partial output 3" in error_poll.texts, "content arrives with the error"
    assert "trailing after the error" in polls[3].texts, "and keeps arriving"

    # Persists indefinitely, and still accepts POSTs.
    for _ in range(20):
        poll = scenario.session.poll()
        assert poll.status is not None
        assert poll.status.value is SessionStatusValue.ERROR
    assert scenario.session.post("retry?", message_id="retry-1").message_id == "retry-1"


def test_cancelled_turn_reports_the_dropped_count_then_settles() -> None:
    scenario = cancelled_turn()
    scenario.start()

    scenario.session.poll()
    acknowledgement = scenario.session.cancel()
    assert acknowledgement.canceled_queued_messages == 2
    assert acknowledgement.status == "canceling"

    statuses: list[SessionStatusValue] = []
    trailing: list[str] = []
    for _ in range(5):
        poll = scenario.session.poll()
        assert poll.status is not None
        statuses.append(poll.status.value)
        trailing.extend(poll.texts)

    assert statuses[:2] == [SessionStatusValue.WORKING, SessionStatusValue.WORKING]
    assert statuses[2:] == [SessionStatusValue.IDLE] * 3
    assert "stopped mid-sen" in trailing, "a cancel still has to drain"
    assert scenario.session.cancel_count == 1


def test_replay_attack_returns_the_whole_transcript_for_any_cursor() -> None:
    scenario = replay_attack()
    seeded = scenario.session.messages()
    assert len(seeded.data) == 4, "the seed is a complete earlier turn"
    cursor = seeded.data[-1]

    scenario.start("a new prompt")
    scenario.session.poll()

    replayed = scenario.session.messages(after=cursor.id)
    assert len(replayed.data) > len(seeded.data)
    assert replayed.data[0].id == seeded.data[0].id, "after= was ignored"

    # This is the filter that has to save us.
    fresh = [m for m in replayed.data if m.session_index > cursor.session_index]
    assert all(m.session_index > cursor.session_index for m in fresh)
    assert len(fresh) == len(replayed.data) - len(seeded.data)


def test_slow_wake_walks_the_workspace_to_ready() -> None:
    scenario = slow_wake()
    assert scenario.session.workspace.status is WorkspaceStatusValue.INITIALIZING

    steps: list[str] = []
    statuses: list[WorkspaceStatusValue] = []
    for _ in range(4):
        poll = scenario.session.poll()
        assert poll.ws is not None
        statuses.append(poll.ws.status)
        steps.append(poll.ws.lifecycle_step or "")

    assert statuses == [
        WorkspaceStatusValue.INITIALIZING,
        WorkspaceStatusValue.INITIALIZING,
        WorkspaceStatusValue.INITIALIZING,
        WorkspaceStatusValue.READY,
    ]
    assert steps[0] == "creating sandbox"
    assert steps[-1] == "ready"
    assert scenario.session.poll().status is not None


def test_slow_wake_has_a_sleeping_variant() -> None:
    scenario = slow_wake(initial=WorkspaceStatusValue.SLEEPING)
    poll = scenario.session.poll()
    assert poll.ws is not None
    assert poll.ws.status is WorkspaceStatusValue.SLEEPING
    assert poll.ws.status.is_waking


def test_status_5xx_fails_k_times_then_recovers_while_content_flows() -> None:
    scenario = status_5xx(failures=4)
    scenario.start()

    failures = 0
    delivered: list[str] = []
    results: list[str] = []
    for _ in range(9):
        poll = scenario.session.poll()
        if poll.status_unavailable is not None:
            failures += 1
            assert poll.status_unavailable.consecutive_failures == failures
        delivered.extend(poll.texts)
        results.extend(
            str(m.raw_payload.get("result")) for m in poll.messages if m.is_result
        )

    assert failures == 4
    assert "chunk 1" in delivered, "content flows while /status is down"
    assert "status is back" in delivered
    assert results == ["all done"]


def test_status_flapping_never_reaches_a_run_of_three_failures() -> None:
    scenario = status_flapping()
    scenario.start()

    run = 0
    longest = 0
    for _ in range(9):
        poll = scenario.session.poll()
        if poll.status_unavailable is not None:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    assert longest == 1


async def test_status_flapping_surfaces_retry_after_on_the_429() -> None:
    scenario = status_flapping()
    codes: list[int] = []
    bodies: list[dict[str, Any]] = []
    async with scenario.fake.client() as http:
        for _ in range(3):
            response = await http.get(f"/sessions/{scenario.session_id}/status")
            codes.append(response.status_code)
            bodies.append(response.json())
    assert codes == [500, 200, 429]
    assert bodies[2]["retryAfter"] == 1.5
    assert bodies[2]["retryable"] is True


# ── sweep ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_is_pollable_past_the_end_of_its_script(name: str) -> None:
    """Polling forever must be safe: the last tick sticks and stops emitting."""
    scenario: Scenario = SCENARIOS[name]()
    scenario.start()
    for _ in range(len(scenario.session.script) + 2):
        scenario.session.poll()

    settled = len(scenario.session.transcript)
    for _ in range(20):
        poll = scenario.session.poll()
        assert poll.messages == ()
        assert poll.delta is None
    assert len(scenario.session.transcript) == settled

    indexes = [m["sessionIndex"] for m in scenario.session.transcript]
    assert indexes == sorted(set(indexes))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_attributes_every_message_to_a_posted_turn(name: str) -> None:
    scenario: Scenario = SCENARIOS[name]()
    scenario.start()
    if name == "double_prompt":
        scenario.start("second", prompt=1)
    for _ in range(len(scenario.session.script) + 2):
        scenario.session.poll()

    known = set(scenario.session.posted_ids) | set(scenario.session.prompt_ids)
    for message in scenario.session.messages_model():
        assert message.turn_id in known
        assert message.id.startswith(f"{scenario.session_id}:")
