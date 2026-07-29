"""Every row of the ``docs/PLAN.md`` §Turn state machine table, plus the named
scenarios from §Verification Phase 1.

This file is the project's proof of correctness. The machine is a pure function,
so the scenarios are scripted evidence sequences — no network, no clock, no
database, no Telegram. If a change here needs a "well, in practice…" to justify
it, the change is wrong.

The four scenarios that matter most, and what each one is defending:

``queued-idle trap``
    A queued prompt reports ``idle`` until its turn starts. Believing it would
    announce "done" before the agent has typed a character.
``fast turn``
    A turn can start *and* finish between two polls, so ``working`` may never be
    observed. The cursor still has the answer.
``double prompt``
    Two prompts in flight must produce exactly one finalize, and not before both
    are accounted for.
``error mid-turn``
    ``error`` may arrive with partial output still unfetched. The drain happens
    first, always.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from ctb.conductor.models import SessionStatusValue, WorkspaceStatusValue
from ctb.turn.machine import cadence_for, format_duration, step
from ctb.turn.state import (
    CADENCE_CURSOR_ONLY_MS,
    CADENCE_DRAINING_MS,
    CADENCE_IDLE_DECAY_MS,
    CADENCE_QUEUED_MS,
    CADENCE_QUEUED_SLOW_MS,
    CADENCE_WAKING_MS,
    CADENCE_WORKING_MS,
    CADENCE_WORKING_STALLED_MS,
    CANCEL_CONFIRMS,
    CANCEL_TIMEOUT_S,
    CURSOR_ONLY_QUIET_FINALIZE_S,
    DRAIN_CONFIRMS,
    E404,
    NO_OUTPUT_SLOW_S,
    NO_OUTPUT_WARN_S,
    PROMPT_AGE_OUT_S,
    QUEUED_SLOW_AFTER_S,
    QUEUED_TIMEOUT_S,
    STATUS_FAILURE_THRESHOLD,
    TURN_END_AGE_OUT_S,
    WAKE_TIMEOUT_S,
    AbandonPrompt,
    Boot,
    Cancel,
    CancelAck,
    CardButton,
    CardKind,
    Delta,
    EditStatusCard,
    Evidence,
    Finalize,
    ForceDrain,
    Notify,
    NotifyLevel,
    PendingPrompt,
    PostAmbiguous,
    PostCancel,
    PostOk,
    PostStatusCard,
    RePost,
    RequestStatus,
    RequestWorkspaceStatus,
    SetCadence,
    SetTopicMarker,
    StartTyping,
    Status,
    StatusUnavailable,
    StopPolling,
    StopTyping,
    Timer,
    TopicMarker,
    TransitionResult,
    TurnContext,
    TurnState,
    UnbindTopic,
    Ws,
)

T0 = 1_000.0
MID = "11111111-1111-4111-8111-111111111111"
MID2 = "22222222-2222-4222-8222-222222222222"
CARD = 4242

IDLE_STATUS = Status(SessionStatusValue.IDLE)
WORKING_STATUS = Status(SessionStatusValue.WORKING)

#: Every action class the machine is allowed to emit. Nothing in this list can
#: stop the poller fetching transcript messages — that is the whole point.
ACTION_TYPES: tuple[type, ...] = (
    PostStatusCard,
    EditStatusCard,
    StartTyping,
    StopTyping,
    SetCadence,
    RePost,
    PostCancel,
    AbandonPrompt,
    Notify,
    Finalize,
    ForceDrain,
    RequestStatus,
    RequestWorkspaceStatus,
    SetTopicMarker,
    UnbindTopic,
    StopPolling,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def actions_of[T](result: TransitionResult, cls: type[T]) -> list[T]:
    return [a for a in result.actions if isinstance(a, cls)]


def has_action(result: TransitionResult, cls: type) -> bool:
    return any(isinstance(a, cls) for a in result.actions)


def cadence_set(result: TransitionResult) -> int | None:
    found = actions_of(result, SetCadence)
    return found[-1].interval_ms if found else None


def prompt(message_id: str = MID, posted_at: float = T0) -> PendingPrompt:
    return PendingPrompt(message_id, posted_at)


def ctx(state: TurnState, **overrides: Any) -> TurnContext:
    """A plausible context for ``state`` — the poller would have one like it."""
    base: dict[str, Any] = {
        "state": state,
        "entered_state_at": T0,
        "status_card_msg_id": CARD,
        "cadence_ms": CADENCE_IDLE_DECAY_MS[0],
    }
    match state:
        case TurnState.IDLE:
            pass
        case TurnState.SUBMIT_PENDING | TurnState.QUEUED:
            base |= {
                "pending_prompts": (prompt(),),
                "index_at_post": 4,
                "turn_started_at": T0,
                "cadence_ms": CADENCE_QUEUED_MS,
            }
        case TurnState.WAKING:
            base |= {
                "pending_prompts": (prompt(),),
                "turn_started_at": T0,
                "workspace_status": WorkspaceStatusValue.INITIALIZING,
                "lifecycle_step": "cloning",
                "cadence_ms": CADENCE_WAKING_MS,
            }
        case TurnState.WORKING:
            base |= {
                "start_witnessed": True,
                "turn_started_at": T0,
                "last_delta_at": T0,
                "cadence_ms": CADENCE_WORKING_MS,
            }
        case TurnState.DRAINING:
            base |= {
                "start_witnessed": True,
                "turn_started_at": T0,
                "last_delta_at": T0,
                "consecutive_idle": 1,
                "cadence_ms": CADENCE_DRAINING_MS,
            }
        case TurnState.CANCELLING:
            base |= {
                "start_witnessed": True,
                "turn_started_at": T0,
                "cancel_requested_at": T0,
                "canceled_queued_messages": 2,
                "cadence_ms": CADENCE_DRAINING_MS,
            }
        case TurnState.ERROR:
            base |= {"error_message": "boom"}
        case TurnState.DEAD:
            pass
    base |= overrides
    return TurnContext(**base)


def drive(
    context: TurnContext, script: list[tuple[Evidence, float]]
) -> list[TransitionResult]:
    """Replay a scripted evidence sequence, threading the context through."""
    results: list[TransitionResult] = []
    for evidence, at in script:
        result = step(context, evidence, at)
        results.append(result)
        context = result.context
    return results


# ══ the transition table, row by row ═════════════════════════════════════════


def test_rule_01_idle_post_ok_queues() -> None:
    result = step(
        ctx(TurnState.IDLE, status_card_msg_id=None), PostOk(MID, index_at_post=7), T0
    )

    assert result.transition == 1
    assert result.state is TurnState.QUEUED
    assert result.context.start_witnessed is False
    assert result.context.outstanding == 1
    assert result.context.pending_prompts[0].message_id == MID
    assert result.context.index_at_post == 7
    assert result.context.turn_started_at == T0
    card = actions_of(result, PostStatusCard)[0]
    assert card.kind is CardKind.QUEUED
    assert CardButton.STOP in card.buttons
    assert cadence_set(result) == CADENCE_QUEUED_MS
    assert not has_action(result, Finalize)


def test_rule_01_post_ok_starts_a_fresh_card_after_a_previous_turn() -> None:
    # The finished turn's card stays in the chat; the new turn gets its own.
    finished = ctx(
        TurnState.IDLE,
        status_card_msg_id=None,
        tool_calls=9,
        turn_ids=frozenset({"old"}),
    )
    result = step(finished, PostOk(MID), T0)

    assert has_action(result, PostStatusCard)
    assert result.context.tool_calls == 0
    assert result.context.turn_ids == frozenset()


def test_rule_02_idle_delta_with_agent_content_mirrors_out_of_band_work() -> None:
    # You drove this session from the Mac. Mirror it rather than ignore it.
    result = step(
        ctx(TurnState.IDLE, status_card_msg_id=None, idle_decay_step=3),
        Delta(n=2, max_index=11, has_agent_content=True, turn_ids=frozenset({"t-9"})),
        T0 + 5,
    )

    assert result.transition == 2
    assert result.state is TurnState.WORKING
    assert result.context.start_witnessed is True
    assert result.context.last_delta_at == T0 + 5
    assert has_action(result, StartTyping)
    assert actions_of(result, SetTopicMarker)[0].marker is TopicMarker.WORKING
    assert cadence_set(result) == CADENCE_WORKING_MS


def test_rule_02_idle_delta_without_agent_content_only_speeds_polling() -> None:
    result = step(
        ctx(TurnState.IDLE, idle_decay_step=3, cadence_ms=CADENCE_IDLE_DECAY_MS[3]),
        Delta(n=1, max_index=11),
        T0 + 5,
    )

    assert result.state is TurnState.IDLE
    assert result.context.idle_decay_step == 0
    assert cadence_set(result) == CADENCE_IDLE_DECAY_MS[0]
    assert not has_action(result, Finalize)


def test_rule_03_submit_pending_boot_reposts_the_identical_message_id() -> None:
    result = step(ctx(TurnState.SUBMIT_PENDING), Boot(), T0 + 60)

    assert result.transition == 3
    assert result.state is TurnState.QUEUED
    assert [a.message_id for a in actions_of(result, RePost)] == [MID]
    # Rule 22 still applies: nothing is concluded before a full refresh.
    assert has_action(result, ForceDrain)
    assert has_action(result, RequestStatus)
    assert has_action(result, RequestWorkspaceStatus)
    assert not has_action(result, Finalize)


def test_rule_03_submit_pending_boot_without_a_prompt_falls_back_to_idle() -> None:
    result = step(ctx(TurnState.SUBMIT_PENDING, pending_prompts=()), Boot(), T0 + 60)

    assert result.transition == 3
    assert result.state is TurnState.IDLE
    assert not has_action(result, RePost)


def test_rule_03_post_ambiguous_reposts_the_identical_message_id() -> None:
    result = step(
        ctx(TurnState.SUBMIT_PENDING), PostAmbiguous(MID, "read timeout"), T0 + 1
    )

    assert result.transition == 3
    assert result.state is TurnState.QUEUED
    assert actions_of(result, RePost) == [RePost(MID)]
    # Re-POSTing keeps the original posted_at, so the age-out clock does not
    # restart on every retry.
    assert result.context.pending_prompts[0].posted_at == T0
    assert result.context.outstanding == 1


def test_rule_04_queued_status_working_starts_the_turn() -> None:
    result = step(ctx(TurnState.QUEUED), WORKING_STATUS, T0 + 3)

    assert result.transition == 4
    assert result.state is TurnState.WORKING
    assert result.context.start_witnessed is True
    # WORKING, never STARTED: only WORKING ticks its clock, so the card answers
    # "is it still alive?" from second zero even on a turn that emits nothing.
    card = actions_of(result, EditStatusCard)[0]
    assert card.kind is CardKind.WORKING
    assert card.text == "working 3s"  # already counting, not a frozen "started"
    assert has_action(result, StartTyping)
    assert actions_of(result, SetTopicMarker)[0].marker is TopicMarker.WORKING
    assert cadence_set(result) == CADENCE_WORKING_MS


def test_rule_05_queued_attributed_delta_starts_the_turn() -> None:
    result = step(
        ctx(TurnState.QUEUED, index_at_post=4),
        Delta(n=1, max_index=5, turn_ids=frozenset({MID})),
        T0 + 2,
    )

    assert result.transition == 5
    assert result.state is TurnState.WORKING
    assert result.context.start_witnessed is True


def test_rule_05_unattributed_delta_is_not_a_start_regardless_of_index() -> None:
    # A later unrelated event must not fake a start.
    result = step(
        ctx(TurnState.QUEUED, index_at_post=9),
        Delta(n=1, max_index=99),
        T0 + 2,
    )

    assert result.state is TurnState.QUEUED
    assert result.context.start_witnessed is False
    # It is still recorded — the content itself was delivered regardless.
    assert result.context.last_delta_at == T0 + 2


def test_rule_05_turn_id_attribution_starts_the_turn_without_an_index() -> None:
    # content.turnId == our messageId on every message of the turn (probe: 6/6).
    result = step(
        ctx(TurnState.QUEUED, index_at_post=None),
        Delta(n=1, max_index=None, turn_ids=frozenset({MID})),
        T0 + 2,
    )

    assert result.transition == 5
    assert result.state is TurnState.WORKING
    assert result.context.outstanding == 0  # the echo witnessed the prompt


def test_rule_06_queued_status_idle_is_structurally_ignored() -> None:
    # THE trap. `idle` while start_witnessed is false carries no information.
    context = ctx(TurnState.QUEUED)
    result = step(context, IDLE_STATUS, T0 + 3)

    assert result.transition == 6
    assert result.state is TurnState.QUEUED
    assert result.context.consecutive_idle == 0
    assert result.actions == ()
    assert not has_action(result, Finalize)


def test_rule_07_queued_ninety_seconds_backs_the_cadence_off() -> None:
    result = step(
        ctx(TurnState.QUEUED),
        Timer(T0 + QUEUED_SLOW_AFTER_S + 1),
        T0 + QUEUED_SLOW_AFTER_S + 1,
    )

    assert result.transition == 7
    assert result.state is TurnState.QUEUED
    assert cadence_set(result) == CADENCE_QUEUED_SLOW_MS
    assert actions_of(result, EditStatusCard)[0].text == "queued behind another turn"


def test_rule_07_does_not_fire_once_a_delta_has_landed() -> None:
    result = step(
        ctx(TurnState.QUEUED, last_delta_at=T0 + 10),
        Timer(T0 + QUEUED_SLOW_AFTER_S + 1),
        T0 + QUEUED_SLOW_AFTER_S + 1,
    )

    assert result.transition is None
    assert not has_action(result, EditStatusCard)


def test_rule_08_queued_ten_minutes_never_started_is_an_error() -> None:
    at = T0 + QUEUED_TIMEOUT_S + 1
    result = step(ctx(TurnState.QUEUED), Timer(at), at)

    assert result.transition == 8
    assert result.state is TurnState.ERROR
    assert actions_of(result, AbandonPrompt)[0].message_id == MID
    card = actions_of(result, EditStatusCard)[0]
    assert card.kind is CardKind.ERROR
    assert card.text == "never started"
    assert CardButton.RETRY in card.buttons
    notify = actions_of(result, Notify)[0]
    assert notify.level is NotifyLevel.LOUD
    assert notify.text == "The prompt never started after 10 minutes."
    assert result.context.error_message == notify.text
    assert has_action(result, StopTyping)


def test_rule_08_does_not_fire_once_the_turn_has_started() -> None:
    at = T0 + QUEUED_TIMEOUT_S + 1
    result = step(ctx(TurnState.QUEUED, start_witnessed=True), Timer(at), at)

    assert result.state is TurnState.QUEUED


@pytest.mark.parametrize(
    ("workspace", "marker"),
    [
        (WorkspaceStatusValue.INITIALIZING, TopicMarker.INITIALIZING),
        # QUEUED carries an outstanding prompt, so 💤 would be a lie: the reader
        # is waiting on us, not looking at a parked workspace. IDLE has none.
        (WorkspaceStatusValue.SLEEPING, TopicMarker.INITIALIZING),
        (WorkspaceStatusValue.UPDATING, TopicMarker.INITIALIZING),
    ],
)
@pytest.mark.parametrize("start", [TurnState.QUEUED])
def test_rule_09_workspace_waking(
    workspace: WorkspaceStatusValue, marker: TopicMarker, start: TurnState
) -> None:
    result = step(ctx(start), Ws(workspace, lifecycle_step="cloning repo"), T0 + 1)

    assert result.transition == 9
    assert result.state is TurnState.WAKING
    assert result.context.waking_notified is True
    assert actions_of(result, SetTopicMarker)[0].marker is marker
    # The waking card carries the lifecycle step; there is no Notify restating
    # it in three lines underneath.
    card = actions_of(result, EditStatusCard)[0]
    assert card.kind is CardKind.WAKING
    assert card.text == "waking · cloning repo"
    assert actions_of(result, Notify) == []
    assert cadence_set(result) == CADENCE_WAKING_MS


def test_rule_09_says_asleep_only_when_nothing_is_waiting() -> None:
    """💤 is "parked", not "starting up". With nothing outstanding it is true."""
    parked = step(ctx(TurnState.IDLE), Ws(WorkspaceStatusValue.SLEEPING), T0 + 1)
    assert actions_of(parked, SetTopicMarker)[0].marker is TopicMarker.SLEEPING


def test_a_prompt_marks_the_topic_waiting_before_anything_runs() -> None:
    """The topic list must stop saying ✅ the moment a new prompt is accepted."""
    result = step(ctx(TurnState.IDLE), PostOk("m-new", "queued", 7), T0 + 1)

    assert result.state is TurnState.QUEUED
    assert actions_of(result, SetTopicMarker)[0].marker is TopicMarker.INITIALIZING


def test_a_waking_turn_never_flickers_through_a_blank_prefix() -> None:
    """⏳ → blank → ⚙️ inside two polls was two renames saying nothing.

    Every rename is a permanent "changed the topic name to …" line in the
    topic, so a prefix that lives for one poll interval is pure scroll.
    """
    waking = step(ctx(TurnState.QUEUED), Ws(WorkspaceStatusValue.INITIALIZING), T0 + 1)
    ready = step(waking.context, Ws(WorkspaceStatusValue.READY), T0 + 40)

    assert actions_of(waking, SetTopicMarker)[0].marker is TopicMarker.INITIALIZING
    assert actions_of(ready, SetTopicMarker)[0].marker is TopicMarker.INITIALIZING


def test_rule_09_never_notifies() -> None:
    first = step(ctx(TurnState.QUEUED), Ws(WorkspaceStatusValue.INITIALIZING), T0 + 1)
    second = step(
        first.context, Ws(WorkspaceStatusValue.INITIALIZING, "building"), T0 + 11
    )

    assert actions_of(first, Notify) == []
    assert actions_of(second, Notify) == []
    assert second.state is TurnState.WAKING


def test_rule_10_waking_ready_resumes_the_queue() -> None:
    result = step(ctx(TurnState.WAKING), Ws(WorkspaceStatusValue.READY), T0 + 40)

    assert result.transition == 10
    assert result.state is TurnState.QUEUED
    assert result.context.start_witnessed is False
    assert cadence_set(result) == CADENCE_QUEUED_MS
    assert [action.message_id for action in actions_of(result, RePost)] == [MID]
    # Still ⏳: the workspace is up, the turn is not. A blank prefix here lived
    # one poll interval and cost a permanent rename line to say it.
    assert actions_of(result, SetTopicMarker)[0].marker is TopicMarker.INITIALIZING


def test_rule_10_waking_ready_with_nothing_outstanding_goes_idle() -> None:
    result = step(
        ctx(TurnState.WAKING, pending_prompts=()),
        Ws(WorkspaceStatusValue.READY),
        T0 + 40,
    )

    assert result.transition == 10
    assert result.state is TurnState.IDLE
    assert cadence_set(result) == CADENCE_IDLE_DECAY_MS[0]


def test_rule_11_wake_timeout() -> None:
    at = T0 + WAKE_TIMEOUT_S + 1
    result = step(ctx(TurnState.WAKING), Timer(at), at)

    assert result.transition == 11
    assert result.state is TurnState.ERROR
    card = actions_of(result, EditStatusCard)[0]
    assert card.text == "wake timed out"
    assert CardButton.RETRY in card.buttons
    assert CardButton.ARCHIVE in card.buttons
    assert (
        actions_of(result, Notify)[0].text
        == "The workspace did not become ready within 10 minutes."
    )


def test_rule_12_working_delta_keeps_working() -> None:
    result = step(
        ctx(TurnState.WORKING, tool_calls=1),
        Delta(n=4, max_index=20, has_agent_content=True, tool_calls=3),
        T0 + 30,
    )

    assert result.transition == 12
    assert result.state is TurnState.WORKING
    assert result.context.last_delta_at == T0 + 30
    assert result.context.tool_calls == 4
    assert result.context.delivered == 4
    assert actions_of(result, EditStatusCard)[0].kind is CardKind.WORKING


def test_rule_13_working_status_idle_drains_and_never_declares_done() -> None:
    result = step(ctx(TurnState.WORKING), IDLE_STATUS, T0 + 30)

    assert result.transition == 13
    assert result.state is TurnState.DRAINING
    assert result.context.consecutive_idle == 1
    assert not has_action(result, Finalize)
    assert not has_action(result, StopTyping)
    assert cadence_set(result) == CADENCE_DRAINING_MS


def test_rule_14_draining_delta_goes_back_to_working() -> None:
    # Ping-pong is expected, not a bug.
    result = step(
        ctx(TurnState.DRAINING, consecutive_idle=2),
        Delta(n=1, max_index=21, has_agent_content=True),
        T0 + 31,
    )

    assert result.transition == 14
    assert result.state is TurnState.WORKING
    assert result.context.consecutive_idle == 0
    assert cadence_set(result) == CADENCE_WORKING_MS


def test_rule_15_draining_finalizes_after_three_idles() -> None:
    context = ctx(
        TurnState.DRAINING, consecutive_idle=0, tool_calls=12, turn_ids=frozenset({MID})
    )
    results = drive(
        context, [(IDLE_STATUS, T0 + 30 + i) for i in range(DRAIN_CONFIRMS)]
    )

    for early in results[:-1]:
        assert early.state is TurnState.DRAINING
        assert not has_action(early, Finalize)

    last = results[-1]
    assert last.transition == 15
    assert last.state is TurnState.IDLE
    summary = actions_of(last, Finalize)[0].summary
    assert summary.ok is True
    assert summary.tool_calls == 12
    assert summary.prompts == 1
    assert summary.duration_ms == int((30 + DRAIN_CONFIRMS - 1) * 1000)
    assert actions_of(last, EditStatusCard)[0].kind is CardKind.DONE
    # The room is most likely finished with exactly here, so the way out is on
    # the card that says so — no /done needed.
    assert CardButton.ARCHIVE in actions_of(last, EditStatusCard)[0].buttons
    assert has_action(last, StopTyping)
    # DONE, not IDLE: the turn produced something to read, and a blank prefix
    # made that indistinguishable from a topic with nothing in it. The state is
    # still IDLE — only the topic marker distinguishes "finished" from "quiet".
    assert actions_of(last, SetTopicMarker)[0].marker is TopicMarker.DONE
    assert cadence_set(last) == CADENCE_IDLE_DECAY_MS[0]
    # A new turn gets a new card.
    assert last.context.status_card_msg_id is None


def test_rule_15_will_not_finalize_while_a_prompt_is_unwitnessed() -> None:
    context = ctx(
        TurnState.DRAINING, consecutive_idle=0, pending_prompts=(prompt(MID2, T0 + 25),)
    )
    results = drive(context, [(IDLE_STATUS, T0 + 30 + i) for i in range(6)])

    assert all(not has_action(r, Finalize) for r in results)
    assert results[-1].state is TurnState.DRAINING
    assert results[-1].context.consecutive_idle == 6


def test_rule_15_will_not_finalize_before_the_agent_reports_the_turn_over() -> None:
    """The reported bug: ✅ mid-turn, during the quiet of a long tool call.

    `idle` between two tool calls is indistinguishable from `idle` after the
    last one. The agent's own end-of-turn record is the difference.
    """
    context = ctx(
        TurnState.DRAINING,
        consecutive_idle=0,
        marks_turn_end=True,
        turn_ids=frozenset({MID}),
        open_turn_ids=frozenset({MID}),
    )
    results = drive(context, [(IDLE_STATUS, T0 + 30 + i) for i in range(6)])

    assert all(not has_action(r, Finalize) for r in results)
    assert results[-1].state is TurnState.DRAINING
    # …and once the confirmations are in it stops polling every 2s to wait.
    assert cadence_set(results[DRAIN_CONFIRMS - 1]) == CADENCE_WORKING_MS


def test_the_end_of_turn_record_releases_the_finalize() -> None:
    context = ctx(
        TurnState.DRAINING,
        consecutive_idle=0,
        marks_turn_end=True,
        turn_ids=frozenset({MID}),
        open_turn_ids=frozenset({MID}),
    )
    held = drive(context, [(IDLE_STATUS, T0 + 30 + i) for i in range(4)])[-1]
    assert not has_action(held, Finalize)

    closed = step(
        held.context,
        Delta(n=1, has_agent_content=True, ended_turn_ids=frozenset({MID})),
        T0 + 40,
    )
    assert closed.context.open_turn_ids == frozenset()
    finished = drive(
        closed.context, [(IDLE_STATUS, T0 + 41 + i) for i in range(DRAIN_CONFIRMS)]
    )[-1]

    assert finished.transition == 15
    assert finished.state is TurnState.IDLE
    assert has_action(finished, Finalize)


def test_an_agent_that_never_marks_the_turn_end_finalizes_as_before() -> None:
    """No demonstrated record ⇒ no gate. Otherwise every turn waits it out."""
    context = ctx(
        TurnState.DRAINING, consecutive_idle=0, open_turn_ids=frozenset({MID})
    )
    results = drive(
        context, [(IDLE_STATUS, T0 + 30 + i) for i in range(DRAIN_CONFIRMS)]
    )

    assert results[-1].transition == 15
    assert has_action(results[-1], Finalize)


def test_a_missing_end_of_turn_record_ages_out_rather_than_wedging() -> None:
    context = ctx(
        TurnState.DRAINING,
        consecutive_idle=DRAIN_CONFIRMS,
        marks_turn_end=True,
        last_delta_at=T0,
        open_turn_ids=frozenset({MID}),
    )
    at = T0 + TURN_END_AGE_OUT_S + 1
    result = step(context, Timer(at), at)

    assert result.transition == 15
    assert result.state is TurnState.IDLE
    assert has_action(result, Finalize)


def test_a_delta_opens_a_turn_and_its_record_closes_it() -> None:
    opened = step(
        ctx(TurnState.WORKING),
        Delta(n=2, has_agent_content=True, turn_ids=frozenset({MID})),
        T0 + 5,
    )
    assert opened.context.open_turn_ids == frozenset({MID})
    assert opened.context.marks_turn_end is False

    # The same page may carry the last message and the record together.
    both = step(
        ctx(TurnState.WORKING),
        Delta(
            n=2,
            has_agent_content=True,
            turn_ids=frozenset({MID}),
            ended_turn_ids=frozenset({MID}),
        ),
        T0 + 5,
    )
    assert both.context.open_turn_ids == frozenset()
    assert both.context.marks_turn_end is True


def test_an_end_of_turn_record_without_a_turn_id_closes_everything() -> None:
    context = ctx(
        TurnState.WORKING, marks_turn_end=True, open_turn_ids=frozenset({MID, MID2})
    )
    result = step(context, Delta(n=1, has_untagged_turn_end=True), T0 + 5)

    assert result.context.open_turn_ids == frozenset()
    assert result.context.turn_end_pending(T0 + 5) is False


def test_rule_16_draining_status_working_returns_to_working() -> None:
    result = step(ctx(TurnState.DRAINING, consecutive_idle=2), WORKING_STATUS, T0 + 31)

    assert result.transition == 16
    assert result.state is TurnState.WORKING
    assert result.context.consecutive_idle == 0


@pytest.mark.parametrize(
    "start",
    [
        TurnState.IDLE,
        TurnState.QUEUED,
        TurnState.WAKING,
        TurnState.WORKING,
        TurnState.DRAINING,
        TurnState.CANCELLING,
    ],
)
def test_rule_17_status_error_drains_before_surfacing(start: TurnState) -> None:
    result = step(
        ctx(start),
        Status(SessionStatusValue.ERROR, error_message="Codex ChatGPT auth not found"),
        T0 + 12,
    )

    assert result.transition == 17
    assert result.state is TurnState.ERROR
    # THE ordering that matters: partial output is fetched before the error card.
    assert isinstance(result.actions[0], ForceDrain)
    assert result.context.error_message == "Codex ChatGPT auth not found"
    card = [
        a for a in result.actions if isinstance(a, PostStatusCard | EditStatusCard)
    ][0]
    assert card.kind is CardKind.ERROR
    # The card says what failed in the agent's own words — it is the surface the
    # owner reads to answer "is it broken?", and "error" is not an answer. It
    # stays one clipped line; the full text pushes once, in the Notify bubble,
    # and stays on the session row for /health.
    assert card.text == "Codex ChatGPT auth not found"
    assert "\n" not in card.text
    assert len(card.text) <= 90
    assert CardButton.RETRY in card.buttons
    notify = actions_of(result, Notify)[0]
    assert notify.level is NotifyLevel.LOUD
    assert notify.text == "Codex ChatGPT auth not found"
    assert has_action(result, StopTyping)
    assert actions_of(result, SetTopicMarker)[0].marker is TopicMarker.ERROR


def test_rule_17_falls_back_to_last_error() -> None:
    result = step(
        ctx(TurnState.WORKING),
        Status(SessionStatusValue.ERROR, error_message=None, last_error="rate limited"),
        T0 + 12,
    )

    assert result.context.error_message == "rate limited"


def test_rule_17_does_not_re_announce_a_persistent_error() -> None:
    # Measured: `error` persisted for 241 consecutive polls while the session
    # still accepted POSTs. One announcement, not 241.
    evidence = Status(SessionStatusValue.ERROR, error_message="auth not found")
    first = step(ctx(TurnState.WORKING), evidence, T0 + 12)
    repeats = drive(first.context, [(evidence, T0 + 12 + i) for i in range(1, 4)])

    assert all(r.actions == () for r in repeats)
    assert all(r.state is TurnState.ERROR for r in repeats)
    assert all(r.transition is None for r in repeats)


def test_error_state_still_accepts_a_retry_prompt() -> None:
    result = step(ctx(TurnState.ERROR, status_card_msg_id=None), PostOk(MID2), T0 + 60)

    assert result.transition == 1
    assert result.state is TurnState.QUEUED
    assert result.context.error_message is None
    assert result.context.outstanding == 1


@pytest.mark.parametrize(
    "start",
    [
        TurnState.IDLE,
        TurnState.QUEUED,
        TurnState.WAKING,
        TurnState.WORKING,
        TurnState.DRAINING,
        TurnState.ERROR,
    ],
)
def test_rule_18_cancel_from_any_state(start: TurnState) -> None:
    result = step(ctx(start), Cancel(requested_by=1001), T0 + 20)

    assert result.transition == 18
    assert result.state is TurnState.CANCELLING
    assert actions_of(result, PostCancel) == [PostCancel(1001)]
    assert result.context.cancel_requested_at == T0 + 20
    assert result.context.cadence_ms == CADENCE_DRAINING_MS


def test_cancel_ack_records_the_dropped_count() -> None:
    result = step(
        ctx(TurnState.CANCELLING, canceled_queued_messages=0), CancelAck(3), T0 + 21
    )

    assert result.context.canceled_queued_messages == 3
    assert actions_of(result, EditStatusCard)[0].text == "stopped · 3 queued dropped"


def test_rule_19_cancelling_finalizes_after_two_idles() -> None:
    context = ctx(TurnState.CANCELLING, consecutive_idle=0, canceled_queued_messages=2)
    results = drive(
        context, [(IDLE_STATUS, T0 + 30 + i) for i in range(CANCEL_CONFIRMS)]
    )

    assert not has_action(results[0], Finalize)
    last = results[-1]
    assert last.transition == 19
    assert last.state is TurnState.IDLE
    summary = actions_of(last, Finalize)[0].summary
    assert summary.ok is False
    assert summary.canceled_queued_messages == 2
    assert actions_of(last, EditStatusCard)[0].kind is CardKind.CANCELLED
    assert actions_of(last, EditStatusCard)[0].text == "stopped · 2 queued dropped"


def test_rule_19_cancel_still_drains_trailing_content_first() -> None:
    context = ctx(TurnState.CANCELLING, consecutive_idle=1)
    result = step(context, Delta(n=1, max_index=30, has_agent_content=True), T0 + 25)

    assert result.state is TurnState.CANCELLING
    assert result.context.consecutive_idle == 0  # the two confirms restart
    assert result.context.delivered == 1


@pytest.mark.parametrize("start", [s for s in TurnState if s is not TurnState.DEAD])
def test_rule_20_e404_is_terminal(start: TurnState) -> None:
    result = step(ctx(start), E404("session"), T0 + 5)

    assert result.transition == 20
    assert result.state is TurnState.DEAD
    assert has_action(result, UnbindTopic)
    assert has_action(result, StopPolling)
    assert has_action(result, StopTyping)
    notify = actions_of(result, Notify)[0]
    assert notify.level is NotifyLevel.LOUD
    # One line. The topic is already renamed and closed and the card says it.
    assert notify.text == "Gone · session is gone (404)"
    assert actions_of(result, SetTopicMarker)[0].marker is TopicMarker.ARCHIVED


@pytest.mark.parametrize(
    "workspace", [WorkspaceStatusValue.ARCHIVED, WorkspaceStatusValue.DELETED]
)
def test_rule_20_archived_or_deleted_workspace_is_terminal(
    workspace: WorkspaceStatusValue,
) -> None:
    result = step(ctx(TurnState.WORKING), Ws(workspace), T0 + 5)

    assert result.transition == 20
    assert result.state is TurnState.DEAD
    assert has_action(result, StopPolling)


@pytest.mark.parametrize(
    "evidence",
    [
        IDLE_STATUS,
        WORKING_STATUS,
        Delta(n=3, max_index=99, has_agent_content=True),
        Timer(T0 + 5),
        Cancel(),
        PostOk(MID),
        Ws(WorkspaceStatusValue.READY),
    ],
)
def test_dead_is_absorbing(evidence: Evidence) -> None:
    dead = ctx(TurnState.DEAD)
    result = step(dead, evidence, T0 + 5)

    assert result.state is TurnState.DEAD
    assert result.actions == ()
    assert result.context == dead


def test_dead_boot_stops_the_poller_again() -> None:
    result = step(ctx(TurnState.DEAD), Boot(), T0 + 5)

    assert result.state is TurnState.DEAD
    assert has_action(result, StopPolling)


def test_rule_21_no_output_warns_once_and_never_kills() -> None:
    at = T0 + NO_OUTPUT_WARN_S + 1
    first = step(ctx(TurnState.WORKING), Timer(at), at)

    assert first.transition == 21
    assert first.state is TurnState.WORKING  # a watchdog, not a kill
    card = actions_of(first, EditStatusCard)[0]
    assert card.kind is CardKind.STALLED
    assert CardButton.CHECK in card.buttons
    notify = actions_of(first, Notify)[0]
    assert notify.once_key == "no-output"
    # One line: the card already carries "stalled?" and a Check button.
    assert (
        notify.text == f"Quiet {format_duration(int((at - T0) * 1000))} · still polling"
    )
    assert first.context.warned_no_output_at == at

    second = step(first.context, Timer(at + 5), at + 5)
    assert actions_of(second, Notify) == []


def test_rule_21_sixty_minutes_slows_the_cadence() -> None:
    at = T0 + NO_OUTPUT_SLOW_S + 1
    result = step(
        ctx(TurnState.WORKING, warned_no_output_at=T0 + NO_OUTPUT_WARN_S), Timer(at), at
    )

    assert result.transition == 21
    assert result.state is TurnState.WORKING
    assert cadence_set(result) == CADENCE_WORKING_STALLED_MS


def test_rule_21_warning_clears_when_output_resumes() -> None:
    at = T0 + NO_OUTPUT_WARN_S + 1
    warned = step(ctx(TurnState.WORKING), Timer(at), at)
    resumed = step(
        warned.context, Delta(n=1, max_index=40, has_agent_content=True), at + 1
    )

    assert resumed.context.warned_no_output_at is None


@pytest.mark.parametrize(
    "start",
    [TurnState.QUEUED, TurnState.WORKING, TurnState.DRAINING, TurnState.WAKING],
)
def test_rule_22_boot_forces_a_full_refresh_before_any_conclusion(
    start: TurnState,
) -> None:
    result = step(
        ctx(start, consecutive_idle=2, consecutive_status_failures=5), Boot(), T0 + 900
    )

    assert result.transition == 22
    assert result.state is start
    assert has_action(result, ForceDrain)
    assert has_action(result, RequestStatus)
    assert has_action(result, RequestWorkspaceStatus)
    assert not has_action(result, Finalize)
    # Stale counters from before the restart cannot conclude anything.
    assert result.context.consecutive_idle == 0
    assert result.context.consecutive_status_failures == 0
    assert result.context.cursor_only is False


def test_rule_22_boot_rebases_timestamps_into_the_new_monotonic_frame() -> None:
    # Persisted timestamps came from a previous process's monotonic clock.
    stale = ctx(
        TurnState.WORKING,
        entered_state_at=0.0,
        turn_started_at=0.0,
        last_delta_at=0.0,
        pending_prompts=(prompt(MID, 0.0),),
    )
    result = step(stale, Boot(), 90_000.0)

    assert result.context.entered_state_at == 90_000.0
    assert result.context.pending_prompts[0].posted_at == 90_000.0
    assert result.context.quiet_for(90_000.0) == 0.0
    # …so the very next timer does not instantly fire the 20-minute watchdog.
    after = step(result.context, Timer(90_001.0), 90_001.0)
    assert after.transition is None


def test_rule_22_boot_honours_a_restored_state() -> None:
    result = step(TurnContext(), Boot(restored_state=TurnState.DRAINING), T0)

    assert result.state is TurnState.DRAINING
    assert result.transition == 22


def test_rule_23_idle_cadence_decays_and_stops_at_the_floor() -> None:
    context = ctx(TurnState.IDLE)
    intervals: list[int | None] = []
    for i in range(6):
        result = step(context, Timer(T0 + i), T0 + i)
        assert result.transition == 23
        assert result.state is TurnState.IDLE
        intervals.append(cadence_set(result))
        context = result.context

    assert intervals[:3] == list(CADENCE_IDLE_DECAY_MS[1:])
    assert intervals[3:] == [None, None, None]  # already at the floor
    assert context.cadence_ms == CADENCE_IDLE_DECAY_MS[-1]


# ══ named scenarios — PLAN §Verification, Phase 1 ════════════════════════════


def test_scenario_queued_idle_trap() -> None:
    """POST → idle, idle, idle, working, idle×3.

    The three leading ``idle`` observations are the documented trap: a queued
    prompt reports ``idle`` until its turn starts. Nothing may finalize before
    the ``working`` observation.
    """
    posted = step(TurnContext(), PostOk(MID, index_at_post=4), T0)
    assert posted.state is TurnState.QUEUED

    script: list[tuple[Evidence, float]] = [
        (IDLE_STATUS, T0 + 3),
        (IDLE_STATUS, T0 + 6),
        (IDLE_STATUS, T0 + 9),
    ]
    trap = drive(posted.context, script)

    assert [r.state for r in trap] == [TurnState.QUEUED] * 3
    assert [r.transition for r in trap] == [6, 6, 6]
    assert all(not has_action(r, Finalize) for r in trap)
    assert all(r.context.consecutive_idle == 0 for r in trap)

    started = step(trap[-1].context, WORKING_STATUS, T0 + 12)
    assert started.transition == 4
    assert started.state is TurnState.WORKING
    assert started.context.start_witnessed is True

    # The prompt's own echo lands, then the turn really does go quiet.
    echoed = step(
        started.context,
        Delta(
            n=2,
            max_index=6,
            has_agent_content=True,
            witnessed_prompt_ids=frozenset({MID}),
        ),
        T0 + 14,
    )
    assert echoed.context.outstanding == 0

    tail = drive(echoed.context, [(IDLE_STATUS, T0 + 16 + i * 2) for i in range(3)])
    assert [r.state for r in tail] == [
        TurnState.DRAINING,
        TurnState.DRAINING,
        TurnState.IDLE,
    ]
    assert sum(len(actions_of(r, Finalize)) for r in tail) == 1


def test_scenario_fast_turn() -> None:
    """POST → idle, [delta with the final answer], idle×3.

    ``working`` is never observed — the turn started and finished between two
    polls. The cursor still carries the answer, so the machine must both start
    and finalize the turn without a single ``working`` status.
    """
    posted = step(TurnContext(), PostOk(MID, index_at_post=4), T0)
    ignored = step(posted.context, IDLE_STATUS, T0 + 3)
    assert ignored.transition == 6
    assert ignored.state is TurnState.QUEUED

    answered = step(
        ignored.context,
        Delta(
            n=4,
            max_index=8,
            has_agent_content=True,
            turn_ids=frozenset({MID}),
            witnessed_prompt_ids=frozenset({MID}),
            tool_calls=2,
        ),
        T0 + 6,
    )
    assert answered.transition == 5
    assert answered.state is TurnState.WORKING
    assert answered.context.outstanding == 0

    tail = drive(answered.context, [(IDLE_STATUS, T0 + 8 + i * 2) for i in range(3)])
    final = tail[-1]
    assert final.state is TurnState.IDLE
    assert final.transition == 15
    summary = actions_of(final, Finalize)[0].summary
    assert summary.ok is True
    assert summary.tool_calls == 2
    assert summary.prompts == 1
    # No status ever said "working".
    assert all(r.context.last_status is not SessionStatusValue.WORKING for r in tail)


def test_scenario_double_prompt() -> None:
    """Two POSTs while working → exactly two witnessed, exactly one finalize."""
    first = step(TurnContext(), PostOk(MID, index_at_post=4), T0)
    working = step(
        first.context,
        Delta(
            n=2,
            max_index=6,
            has_agent_content=True,
            turn_ids=frozenset({MID}),
            witnessed_prompt_ids=frozenset({MID}),
        ),
        T0 + 4,
    )
    assert working.state is TurnState.WORKING

    second = step(working.context, PostOk(MID2, index_at_post=6), T0 + 10)
    assert second.state is TurnState.WORKING  # Conductor queues it server-side
    assert second.context.outstanding == 1
    assert second.context.turn_started_at == T0  # still the same turn's clock

    # Three idles with the second prompt still unwitnessed: no finalize.
    blocked = drive(second.context, [(IDLE_STATUS, T0 + 12 + i * 2) for i in range(3)])
    assert all(not has_action(r, Finalize) for r in blocked)
    assert blocked[-1].state is TurnState.DRAINING

    witnessed = step(
        blocked[-1].context,
        Delta(
            n=3,
            max_index=10,
            has_agent_content=True,
            turn_ids=frozenset({MID2}),
            witnessed_prompt_ids=frozenset({MID2}),
        ),
        T0 + 20,
    )
    assert witnessed.transition == 14
    assert witnessed.context.outstanding == 0

    tail = drive(witnessed.context, [(IDLE_STATUS, T0 + 22 + i * 2) for i in range(3)])
    finalizes = [
        f for r in [*blocked, witnessed, *tail] for f in actions_of(r, Finalize)
    ]
    assert len(finalizes) == 1
    assert finalizes[0].summary.prompts == 2
    assert tail[-1].state is TurnState.IDLE


def test_scenario_error_mid_turn_drains_partial_output_first() -> None:
    """The error card must never be posted before the partial output is fetched."""
    working = ctx(TurnState.WORKING, delivered=3)
    errored = step(
        working,
        Status(SessionStatusValue.ERROR, error_message="Codex ChatGPT auth not found"),
        T0 + 30,
    )

    order = [type(a).__name__ for a in errored.actions]
    assert order.index("ForceDrain") == 0
    assert order.index("ForceDrain") < order.index("EditStatusCard")
    assert order.index("ForceDrain") < order.index("Notify")
    assert errored.state is TurnState.ERROR

    # Partial output that arrives after the error is still recorded, and the
    # machine does not flap back and forth.
    trailing = step(
        errored.context, Delta(n=2, max_index=12, has_agent_content=True), T0 + 32
    )
    assert trailing.state is TurnState.ERROR
    assert trailing.context.delivered == 5  # 3 before the error, 2 after

    # And the error keeps persisting without spamming.
    persisted = step(
        trailing.context,
        Status(SessionStatusValue.ERROR, error_message="Codex ChatGPT auth not found"),
        T0 + 34,
    )
    assert persisted.actions == ()


def test_scenario_error_result_in_the_transcript_is_reported_at_finalize() -> None:
    working = ctx(TurnState.WORKING)
    failed = step(working, Delta(n=1, max_index=12, has_error_result=True), T0 + 30)
    tail = drive(failed.context, [(IDLE_STATUS, T0 + 32 + i * 2) for i in range(3)])

    summary = actions_of(tail[-1], Finalize)[0].summary
    assert summary.ok is False
    assert summary.error == "the agent reported an error result"


def test_scenario_status_free_fallback() -> None:
    """K consecutive ``/status`` failures → cursor-only mode. Delivery unaffected."""
    context = ctx(TurnState.WORKING)
    degrading = drive(
        context,
        [
            (StatusUnavailable(reason="429"), T0 + i)
            for i in range(STATUS_FAILURE_THRESHOLD)
        ],
    )

    assert all(not r.context.cursor_only for r in degrading[:-1])
    entered = degrading[-1]
    assert entered.context.cursor_only is True
    assert cadence_set(entered) == CADENCE_CURSOR_ONLY_MS
    notice = actions_of(entered, Notify)[0]
    assert notice.once_key == "cursor-only"
    assert notice.text == "Status API down · replies still arrive."

    # WORKING is inferred from recent deltas…
    quiet_but_recent = step(entered.context, Timer(T0 + 20), T0 + 20)
    assert quiet_but_recent.state is TurnState.WORKING
    assert not has_action(quiet_but_recent, Finalize)

    # …and 45s of quiet with nothing outstanding finalizes the turn.
    at = T0 + CURSOR_ONLY_QUIET_FINALIZE_S + 1
    settled = step(quiet_but_recent.context, Timer(at), at)
    assert settled.state is TurnState.IDLE
    assert settled.transition == 15
    assert has_action(settled, Finalize)


def test_cursor_only_will_not_finalize_with_a_prompt_outstanding() -> None:
    context = ctx(
        TurnState.WORKING, cursor_only=True, pending_prompts=(prompt(MID2, T0 + 10),)
    )
    at = T0 + CURSOR_ONLY_QUIET_FINALIZE_S + 1
    result = step(context, Timer(at), at)

    assert result.state is TurnState.WORKING
    assert not has_action(result, Finalize)


def test_status_recovery_leaves_cursor_only_mode() -> None:
    context = ctx(
        TurnState.WORKING, cursor_only=True, cadence_ms=CADENCE_CURSOR_ONLY_MS
    )
    result = step(context, WORKING_STATUS, T0 + 5)

    assert result.context.cursor_only is False
    assert result.context.consecutive_status_failures == 0
    assert cadence_set(result) == CADENCE_WORKING_MS


def test_unknown_status_is_never_read_as_done() -> None:
    for start in (TurnState.QUEUED, TurnState.WORKING, TurnState.DRAINING):
        result = step(ctx(start), Status(SessionStatusValue.UNKNOWN), T0 + 5)
        assert result.state is start
        assert not has_action(result, Finalize)
        assert result.context.consecutive_idle == ctx(start).consecutive_idle


def test_scenario_prompt_age_out_unblocks_a_wedged_drain() -> None:
    """One lost echo must not wedge a session forever."""
    context = ctx(
        TurnState.DRAINING,
        consecutive_idle=DRAIN_CONFIRMS,
        pending_prompts=(prompt(MID2, T0),),
    )
    at = T0 + PROMPT_AGE_OUT_S + 1
    result = step(context, Timer(at), at)

    assert actions_of(result, AbandonPrompt)[0].message_id == MID2
    assert result.transition == 15
    assert result.state is TurnState.IDLE
    assert has_action(result, Finalize)


def test_second_prompt_while_queued_shows_the_pending_count() -> None:
    first = step(TurnContext(), PostOk(MID), T0)
    second = step(first.context, PostOk(MID2), T0 + 1)

    assert second.state is TurnState.QUEUED
    assert second.context.outstanding == 2
    card = actions_of(second, PostStatusCard)[0]
    assert card.text == "queued (2 pending)"
    assert CardButton.CLEAR_QUEUE in card.buttons


def test_prompt_while_draining_starts_a_new_wait() -> None:
    result = step(ctx(TurnState.DRAINING, consecutive_idle=2), PostOk(MID2), T0 + 40)

    assert result.state is TurnState.QUEUED
    assert result.context.start_witnessed is False
    assert result.context.consecutive_idle == 0


# ══ invariants ═══════════════════════════════════════════════════════════════

ALL_EVIDENCE: list[Evidence] = [
    PostOk(MID, index_at_post=4),
    PostAmbiguous(MID, "timeout"),
    Status(SessionStatusValue.IDLE),
    Status(SessionStatusValue.WORKING),
    Status(SessionStatusValue.ERROR, error_message="nope"),
    Status(SessionStatusValue.UNKNOWN),
    StatusUnavailable(consecutive_failures=9),
    Delta(n=2, max_index=99, has_agent_content=True, tool_calls=1),
    Delta(n=1, max_index=1),
    Ws(WorkspaceStatusValue.READY),
    Ws(WorkspaceStatusValue.SLEEPING),
    Ws(WorkspaceStatusValue.DELETED),
    Timer(T0 + 5),
    Cancel(requested_by=7),
    CancelAck(canceled_queued_messages=1),
    Boot(),
    E404("session"),
]


@pytest.mark.parametrize("state", list(TurnState))
@pytest.mark.parametrize("evidence", ALL_EVIDENCE, ids=lambda e: type(e).__name__)
def test_step_is_total_and_pure(state: TurnState, evidence: Evidence) -> None:
    """Every (state, evidence) pair returns; the input context never mutates."""
    context = ctx(state)
    snapshot = dataclasses.replace(context)

    result = step(context, evidence, T0 + 7)

    assert isinstance(result, TransitionResult)
    assert isinstance(result.state, TurnState)
    assert context == snapshot
    assert all(isinstance(a, ACTION_TYPES) for a in result.actions)


@pytest.mark.parametrize("state", list(TurnState))
@pytest.mark.parametrize("evidence", ALL_EVIDENCE, ids=lambda e: type(e).__name__)
def test_the_machine_can_never_gate_delivery(
    state: TurnState, evidence: Evidence
) -> None:
    """No action reachable from any (state, evidence) pair can stop a fetch.

    Delivery runs off the transcript cursor unconditionally. The machine's only
    influence on the fetch path is ``SetCadence`` (which stays positive except
    for the terminal DEAD state), and the two actions that ask for *more*
    fetching. ``StopPolling`` is only ever reachable together with DEAD.
    """
    result = step(ctx(state), evidence, T0 + 7)

    for action in result.actions:
        if isinstance(action, SetCadence):
            assert action.interval_ms > 0
            assert action.interval_ms <= CADENCE_IDLE_DECAY_MS[-1]
        if isinstance(action, StopPolling | UnbindTopic):
            assert result.state is TurnState.DEAD


@pytest.mark.parametrize(
    "state", [s for s in TurnState if s is not TurnState.CANCELLING]
)
def test_finalize_requires_nothing_outstanding(state: TurnState) -> None:
    """No evidence may finalize a turn while a live prompt is unwitnessed.

    ``CANCELLING`` is the deliberate exception: ``/stop`` means stop, so the
    unwitnessed prompts are abandoned rather than waited on.
    """
    context = ctx(state, pending_prompts=(prompt(MID2, T0 + 6),), consecutive_idle=9)
    for evidence in ALL_EVIDENCE:
        result = step(context, evidence, T0 + 7)
        assert not has_action(result, Finalize), (state, evidence)


def test_cancel_abandons_unwitnessed_prompts_rather_than_waiting() -> None:
    context = ctx(
        TurnState.CANCELLING,
        consecutive_idle=CANCEL_CONFIRMS - 1,
        pending_prompts=(prompt(MID2, T0 + 6),),
    )
    result = step(context, IDLE_STATUS, T0 + 7)

    assert result.transition == 19
    assert result.state is TurnState.IDLE
    assert actions_of(result, AbandonPrompt)[0].message_id == MID2
    assert has_action(result, Finalize)


def test_finalize_only_ever_lands_in_idle() -> None:
    seen = 0
    for state in TurnState:
        for evidence in ALL_EVIDENCE:
            result = step(ctx(state, consecutive_idle=9), evidence, T0 + 7)
            if has_action(result, Finalize):
                seen += 1
                assert result.state is TurnState.IDLE
                assert has_action(result, StopTyping)
                assert result.context.pending_prompts == ()
    assert seen  # the sweep actually exercised a finalize


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (TurnState.IDLE, CADENCE_IDLE_DECAY_MS[0]),
        (TurnState.SUBMIT_PENDING, CADENCE_QUEUED_MS),
        (TurnState.QUEUED, CADENCE_QUEUED_MS),
        (TurnState.WAKING, CADENCE_WAKING_MS),
        (TurnState.WORKING, CADENCE_WORKING_MS),
        (TurnState.DRAINING, CADENCE_DRAINING_MS),
        (TurnState.CANCELLING, CADENCE_DRAINING_MS),
        (TurnState.ERROR, CADENCE_IDLE_DECAY_MS[0]),
        (TurnState.DEAD, 0),
    ],
)
def test_cadence_table(state: TurnState, expected: int) -> None:
    assert cadence_for(ctx(state), T0 + 1) == expected


def test_cadence_cursor_only_overrides_active_states() -> None:
    assert (
        cadence_for(ctx(TurnState.WORKING, cursor_only=True), T0)
        == CADENCE_CURSOR_ONLY_MS
    )
    # …but IDLE keeps decaying normally.
    assert (
        cadence_for(ctx(TurnState.IDLE, cursor_only=True), T0)
        == CADENCE_IDLE_DECAY_MS[0]
    )


@pytest.mark.parametrize(
    ("ms", "text"),
    [
        (0, "0s"),
        (6_300, "6s"),
        (92_000, "1m32s"),
        (62_000, "1m02s"),
        (3_600_000, "1h00m"),
        (-5, "0s"),
    ],
)
def test_format_duration(ms: int, text: str) -> None:
    assert format_duration(ms) == text


def test_rule_23_an_unconfirmed_cancel_hands_the_controls_back() -> None:
    """CANCELLING used to be absorbing: a spinner with no buttons, forever.

    ``PostCancel`` is issued once and its transport errors are swallowed, and
    the only exit was ``Status(idle)``. A session genuinely wedged in a tool
    call never sent one, so the user who pressed Stop got "🛑 stopping…", a
    perpetual typing indicator and a 2s poll until the process died.
    """
    cancelling = ctx(TurnState.CANCELLING, cancel_requested_at=T0)
    at = T0 + CANCEL_TIMEOUT_S + 1

    result = step(cancelling, Timer(at), at)

    assert result.transition == 23
    # Not finalized: the turn may still be running, and "stopped" would be the
    # one thing the card must never say falsely.
    assert result.state is TurnState.WORKING
    card = actions_of(result, EditStatusCard)[0]
    assert "stop not confirmed" in card.text
    assert CardButton.CHECK in card.buttons and CardButton.STOP in card.buttons
    assert result.context.cancel_requested_at is None


def test_a_cancel_still_settles_normally_well_inside_the_timeout() -> None:
    cancelling = ctx(TurnState.CANCELLING, cancel_requested_at=T0)
    at = T0 + CANCEL_TIMEOUT_S - 1

    result = step(cancelling, Timer(at), at)

    assert result.state is TurnState.CANCELLING
    assert result.transition is None


def test_the_stopping_card_always_offers_a_way_to_ask() -> None:
    result = step(ctx(TurnState.WORKING), Cancel(requested_by=1), T0 + 1)

    card = actions_of(result, EditStatusCard)[0]
    assert card.kind is CardKind.CANCELLING
    assert CardButton.CHECK in card.buttons, "a spinner with no controls is a dead end"
