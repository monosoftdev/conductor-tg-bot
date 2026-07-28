"""The pure turn state machine — every row of ``docs/PLAN.md`` §Turn state machine.

``step(context, evidence, now) -> TransitionResult`` is a total function of its
arguments: no I/O, no ``await``, no clock reads, no randomness, no globals. The
poller feeds it evidence it has already gathered and executes the returned
actions **in order** — the order is load-bearing (rule 17 emits ``ForceDrain``
before it shows the error, because partial output may still be pending).

**This machine can never suppress a delivery.** It emits no delivery action at
all: content reaches Telegram off the transcript cursor, unconditionally, on
every tick in every state. The only thing the machine can do to the fetch path
is ask for *more* of it (:class:`ForceDrain`, :class:`RequestStatus`). If the
machine is wrong you get a stale progress line — never a lost or doubled reply.

The transition table, keyed by the rule numbers used in ``TransitionResult``:

===  ===========================  ==============================  ===========
#    From                         Evidence                        To
===  ===========================  ==============================  ===========
1    IDLE                         PostOk                          QUEUED
2    IDLE                         Delta(agent content)            WORKING
3    SUBMIT_PENDING               Boot | PostAmbiguous            QUEUED
4    QUEUED                       Status(working)                 WORKING
5    QUEUED                       Delta(agent/matching turnId)    WORKING
6    QUEUED                       Status(idle)                    QUEUED
7    QUEUED                       Timer(90s, no delta)            QUEUED
8    QUEUED                       Timer(10m, never started)       ERROR
9    QUEUED | IDLE                Ws(initializing|sleeping|…)     WAKING
10   WAKING                       Ws(ready)                       QUEUED | IDLE
11   WAKING                       Timer(10m)                      ERROR
12   WORKING                      Delta(n>0)                      WORKING
13   WORKING                      Status(idle)                    DRAINING
14   DRAINING                     Delta(n>0)                      WORKING
15   DRAINING                     Status(idle) ×3, no delta       IDLE
16   DRAINING                     Status(working)                 WORKING
17   any                          Status(error)                   ERROR
18   any                          Cancel                          CANCELLING
19   CANCELLING                   Status(idle) ×2 + drain         IDLE
20   any                          E404 | Ws(archived|deleted)     DEAD
21   WORKING                      Timer(20m no delta)             WORKING
22   QUEUED|WORKING|DRAINING|…    Boot                            same
23   IDLE                         Timer                           IDLE
===  ===========================  ==============================  ===========

Rules with no number in the table (``transition=None`` and a ``reason``) are the
status-free fallback from PLAN §"Status-free fallback" and the small number of
book-keeping no-ops that keep the function total over every (state, evidence)
pair.

Two things the machine deliberately refuses to do:

* ``Status(idle)`` while ``start_witnessed`` is false is *structurally* ignored
  (rule 6). A queued prompt reports ``idle`` until its turn starts, so believing
  it is the single most dangerous mistake available here.
* ``DRAINING`` never finalizes while a POSTed prompt is still unwitnessed and
  younger than ``PROMPT_AGE_OUT_S``. Two prompts in flight means one finalize,
  after both are accounted for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from ctb.conductor.models import SessionStatusValue, WorkspaceStatusValue
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
    QUEUED_SLOW_AFTER_S,
    QUEUED_TIMEOUT_S,
    STATUS_FAILURE_THRESHOLD,
    WAKE_TIMEOUT_S,
    AbandonPrompt,
    Action,
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
    TurnSummary,
    UnbindTopic,
    Ws,
)

__all__ = ["cadence_for", "format_duration", "step"]


# ── small helpers ────────────────────────────────────────────────────────────


def format_duration(ms: int) -> str:
    """``92_000 -> "1m32s"``. Used in the status card and the header line."""
    seconds = max(0, ms) // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


_ACTIVE_BUTTONS: Final[tuple[CardButton, ...]] = (CardButton.STOP,)
_QUEUE_BUTTONS: Final[tuple[CardButton, ...]] = (
    CardButton.STOP,
    CardButton.CLEAR_QUEUE,
)
_DONE_BUTTONS: Final[tuple[CardButton, ...]] = (
    CardButton.TRANSCRIPT,
    CardButton.RETRY,
    CardButton.OPEN,
)
_ERROR_BUTTONS: Final[tuple[CardButton, ...]] = (
    CardButton.RETRY,
    CardButton.TRANSCRIPT,
    CardButton.OPEN,
)
_TIMEOUT_BUTTONS: Final[tuple[CardButton, ...]] = (
    CardButton.RETRY,
    CardButton.ARCHIVE,
)
_STALLED_BUTTONS: Final[tuple[CardButton, ...]] = (
    CardButton.CHECK,
    CardButton.STOP,
)

#: States in which a turn is plausibly in flight, so a new prompt joins the
#: current turn instead of starting a new one.
_ACTIVE_STATES: Final[frozenset[TurnState]] = frozenset(
    {
        TurnState.QUEUED,
        TurnState.WAKING,
        TurnState.WORKING,
        TurnState.DRAINING,
        TurnState.CANCELLING,
    }
)


def cadence_for(context: TurnContext, now: float) -> int:
    """The poll interval this context should be running at, in milliseconds.

    Jitter (``JITTER_FRACTION``) is applied by the poller, not here — this
    function stays pure and deterministic so the tests can assert on it.
    """
    if context.state is TurnState.DEAD:
        return 0
    if context.cursor_only and context.state in _ACTIVE_STATES:
        return CADENCE_CURSOR_ONLY_MS
    match context.state:
        case TurnState.IDLE:
            step_index = min(context.idle_decay_step, len(CADENCE_IDLE_DECAY_MS) - 1)
            return CADENCE_IDLE_DECAY_MS[step_index]
        case TurnState.SUBMIT_PENDING | TurnState.QUEUED:
            if context.elapsed_in_state(now) >= QUEUED_SLOW_AFTER_S:
                return CADENCE_QUEUED_SLOW_MS
            return CADENCE_QUEUED_MS
        case TurnState.WAKING:
            return CADENCE_WAKING_MS
        case TurnState.WORKING:
            if context.quiet_for(now) >= NO_OUTPUT_SLOW_S:
                return CADENCE_WORKING_STALLED_MS
            return CADENCE_WORKING_MS
        case TurnState.DRAINING | TurnState.CANCELLING:
            return CADENCE_DRAINING_MS
        case TurnState.ERROR:
            return CADENCE_IDLE_DECAY_MS[0]
        case _:  # pragma: no cover - TurnState.DEAD handled above
            return CADENCE_IDLE_DECAY_MS[0]


class _Tick:
    """Mutable accumulator for one call to :func:`step`.

    Keeps the handlers readable without giving up the immutability of
    :class:`TurnContext` — every mutation here rebuilds the frozen context.
    """

    __slots__ = ("actions", "ctx")

    def __init__(self, context: TurnContext) -> None:
        self.ctx: TurnContext = context
        self.actions: list[Action] = []

    def do(self, *actions: Action) -> None:
        self.actions.extend(actions)

    def evolve(self, **changes: Any) -> None:
        self.ctx = self.ctx.evolve(**changes)

    def enter(self, state: TurnState, now: float, **changes: Any) -> None:
        self.ctx = self.ctx.enter(state, now, **changes)

    def cadence(self, interval_ms: int, reason: str = "") -> None:
        """Record a cadence change, emitting ``SetCadence`` only when it moves."""
        if interval_ms != self.ctx.cadence_ms:
            self.actions.append(SetCadence(interval_ms, reason))
        self.ctx = self.ctx.evolve(cadence_ms=interval_ms)

    def retune(self, now: float, reason: str = "") -> None:
        """Re-derive the cadence from the (possibly just changed) state."""
        self.cadence(cadence_for(self.ctx, now), reason)

    def card(
        self,
        kind: CardKind,
        text: str = "",
        buttons: tuple[CardButton, ...] = (),
        activity: str | None = None,
    ) -> None:
        """Post the card if this turn has none yet, otherwise edit it in place."""
        if self.ctx.has_card:
            self.actions.append(EditStatusCard(kind, text, buttons, activity))
        else:
            self.actions.append(PostStatusCard(kind, text, buttons))

    def result(
        self, transition: int | None = None, reason: str = ""
    ) -> TransitionResult:
        return TransitionResult(self.ctx, tuple(self.actions), transition, reason)


# ── card copy ────────────────────────────────────────────────────────────────


def _queued_text(context: TurnContext) -> str:
    pending = context.outstanding
    return "queued" if pending <= 1 else f"queued ({pending} pending)"


def _queue_buttons(context: TurnContext) -> tuple[CardButton, ...]:
    return _QUEUE_BUTTONS if context.outstanding > 1 else _ACTIVE_BUTTONS


def _working_text(context: TurnContext, now: float) -> str:
    text = f"working {format_duration(context.turn_duration_ms(now))}"
    if context.outstanding > 1:
        text += f" · {context.outstanding} pending"
    return text


def _done_text(context: TurnContext, now: float) -> str:
    text = f"done in {format_duration(context.turn_duration_ms(now))}"
    if context.tool_calls:
        text += f" · {context.tool_calls} tools"
    return text


def _waking_text(context: TurnContext) -> str:
    if context.lifecycle_step:
        return f"waking · {context.lifecycle_step}"
    return "waking"


#: An error card is one phone line. The full text is the ``Notify`` bubble.
_ERROR_HEADLINE_CHARS: Final = 90


def _error_headline(message: str) -> str:
    """The agent's own first line, short enough to read at a glance.

    The card is where the owner looks to answer "is it broken?", so a bare
    ``error`` is not an answer. The full message still goes out exactly once as
    the ``Notify`` bubble and is persisted on the session row for ``/health``.
    """
    line = " ".join(message.split())
    if not line:
        return "error"
    if len(line) <= _ERROR_HEADLINE_CHARS:
        return line
    return line[: _ERROR_HEADLINE_CHARS - 1].rstrip() + "…"


def _cancelled_text(context: TurnContext) -> str:
    dropped = context.canceled_queued_messages
    if dropped:
        return f"stopped · {dropped} queued dropped"
    return "stopped"


# ── prompt bookkeeping ───────────────────────────────────────────────────────


def _track_prompt(
    tick: _Tick, message_id: str, now: float, index_at_post: int | None
) -> None:
    """Add (or refresh) a POSTed-but-unwitnessed prompt.

    A re-POST of the same ``messageId`` keeps the *original* ``posted_at`` so the
    5-minute age-out is measured from the first attempt, not the last retry.
    """
    for prompt in tick.ctx.pending_prompts:
        if prompt.message_id == message_id:
            merged = PendingPrompt(
                message_id,
                prompt.posted_at,
                prompt.index_at_post
                if prompt.index_at_post is not None
                else index_at_post,
            )
            tick.evolve(
                pending_prompts=tuple(
                    merged if p.message_id == message_id else p
                    for p in tick.ctx.pending_prompts
                )
            )
            return
    tick.evolve(
        pending_prompts=(
            *tick.ctx.pending_prompts,
            PendingPrompt(message_id, now, index_at_post),
        )
    )


def _drop_prompts(tick: _Tick, message_ids: frozenset[str], reason: str) -> None:
    """Forget prompts, emitting :class:`AbandonPrompt` for each."""
    if not message_ids:
        return
    for prompt in tick.ctx.pending_prompts:
        if prompt.message_id in message_ids:
            tick.do(AbandonPrompt(prompt.message_id, reason))
    tick.evolve(
        pending_prompts=tuple(
            p for p in tick.ctx.pending_prompts if p.message_id not in message_ids
        )
    )


def _age_out_prompts(tick: _Tick, now: float) -> None:
    """A prompt whose echo never arrived stops blocking finalize after 5 min."""
    stale = frozenset(p.message_id for p in tick.ctx.pending_prompts if p.aged_out(now))
    _drop_prompts(tick, stale, "aged out — never witnessed in the transcript")


# ── turn lifecycle ───────────────────────────────────────────────────────────


def _start_turn(tick: _Tick, now: float, index_at_post: int | None) -> None:
    """Reset the per-turn counters and enter QUEUED with a fresh status card."""
    tick.enter(
        TurnState.QUEUED,
        now,
        start_witnessed=False,
        index_at_post=index_at_post,
        turn_started_at=now,
        last_delta_at=None,
        consecutive_idle=0,
        idle_decay_step=0,
        tool_calls=0,
        delivered=0,
        turn_ids=frozenset(),
        error_message=None,
        warned_no_output_at=None,
        waking_notified=False,
        cancel_requested_at=None,
        canceled_queued_messages=0,
        status_card_msg_id=None,
    )
    tick.card(CardKind.QUEUED, _queued_text(tick.ctx), _queue_buttons(tick.ctx))
    # The topic list is the only surface you can see without opening anything,
    # and it used to keep saying ✅ from the *previous* turn until the agent
    # produced its first output. Sending a prompt and watching the list say
    # "finished" is the one wrong answer; ⏳ from the moment it is accepted.
    tick.do(SetTopicMarker(TopicMarker.INITIALIZING))
    tick.retune(now, "queued")


def _witness_start(tick: _Tick, now: float, reason: str) -> None:
    """The turn is confirmed running: card, typing indicator, fast cadence."""
    tick.enter(
        TurnState.WORKING,
        now,
        start_witnessed=True,
        consecutive_idle=0,
        turn_started_at=tick.ctx.turn_started_at
        if tick.ctx.turn_started_at is not None
        else now,
    )
    # WORKING, not STARTED: only WORKING re-renders on every card tick, so this
    # is what gives the card a clock from second zero. A turn that produces no
    # output would otherwise sit frozen on "started" until the 20m watchdog.
    tick.card(CardKind.WORKING, _working_text(tick.ctx, now), _queue_buttons(tick.ctx))
    tick.do(StartTyping(), SetTopicMarker(TopicMarker.WORKING))
    tick.retune(now, reason)


def _finalize(
    tick: _Tick,
    now: float,
    *,
    kind: CardKind,
    text: str,
    buttons: tuple[CardButton, ...],
    ok: bool,
    error: str | None,
    canceled_queued_messages: int,
    reason: str,
) -> None:
    """End the turn: settle the card, emit the header line, go quiet."""
    context = tick.ctx
    _drop_prompts(
        tick,
        frozenset(p.message_id for p in context.pending_prompts),
        reason,
    )
    summary = TurnSummary(
        duration_ms=context.turn_duration_ms(now),
        tool_calls=context.tool_calls,
        files_changed=0,
        prompts=max(1, len(context.turn_ids)),
        ok=ok,
        error=error,
        canceled_queued_messages=canceled_queued_messages,
    )
    tick.card(kind, text, buttons)
    # DONE, not IDLE: a finished turn has something waiting to be read, and a
    # blank prefix made that indistinguishable from a topic with nothing in it.
    # It falls back to IDLE on the next prompt, when the marker follows the
    # session to WORKING.
    tick.do(Finalize(summary), StopTyping(), SetTopicMarker(TopicMarker.DONE))
    tick.enter(
        TurnState.IDLE,
        now,
        start_witnessed=False,
        pending_prompts=(),
        index_at_post=None,
        turn_started_at=None,
        last_delta_at=None,
        consecutive_idle=0,
        idle_decay_step=0,
        tool_calls=0,
        turn_ids=frozenset(),
        error_message=None,
        warned_no_output_at=None,
        waking_notified=False,
        cancel_requested_at=None,
        canceled_queued_messages=0,
        status_card_msg_id=None,
    )
    tick.retune(now, "idle")


def _finalize_done(tick: _Tick, now: float) -> None:
    error = tick.ctx.error_message
    _finalize(
        tick,
        now,
        kind=CardKind.DONE,
        text=_done_text(tick.ctx, now),
        buttons=_DONE_BUTTONS,
        ok=error is None,
        error=error,
        canceled_queued_messages=0,
        reason="turn finished",
    )


def _enter_error(
    tick: _Tick,
    now: float,
    message: str,
    *,
    kind: CardKind = CardKind.ERROR,
    buttons: tuple[CardButton, ...] = _ERROR_BUTTONS,
    card_text: str = "",
    drain: bool = True,
) -> None:
    """Surface an error. ``drain`` first — partial output may still be pending.

    The full ``message`` goes out exactly once, as the ``Notify`` bubble — that
    is what pushes and what the user reads. The card carries ``card_text`` — a
    caller-supplied phrase, or the first 90 characters of the agent's own words
    — plus the Retry button, because printing the same 300 characters twice in
    one topic costs a screen and buys nothing. The detail stays reachable: it is
    persisted as ``error_message`` on the session row and surfaced by
    ``/health``.

    The session status ``error`` persists indefinitely while the session still
    accepts POSTs (measured: 241 consecutive polls), so this is not a terminal
    state and it must not re-announce itself on every poll. Re-entering ERROR
    with an unchanged message is handled by the caller as a no-op.
    """
    if drain:
        tick.do(ForceDrain("error may have partial output"))
    tick.enter(
        TurnState.ERROR,
        now,
        error_message=message,
        start_witnessed=False,
        consecutive_idle=0,
        idle_decay_step=0,
    )
    tick.card(kind, card_text or _error_headline(message), buttons)
    tick.do(
        StopTyping(),
        SetTopicMarker(TopicMarker.ERROR),
        Notify(message, NotifyLevel.LOUD, once_key=f"session-error:{message[:80]}"),
    )
    tick.retune(now, "error")


# ── PostOk / PostAmbiguous — rules 1 and 3 ───────────────────────────────────


def _accept_prompt(
    tick: _Tick,
    now: float,
    *,
    index_at_post: int | None,
    transition: int,
    reason: str,
) -> TransitionResult:
    """A prompt is now in flight. Either it joins the turn or it starts one."""
    state = tick.ctx.state
    if state is TurnState.WORKING:
        # Second prompt while working: Conductor queues it server-side. Rule 15
        # will not finalize until this one is witnessed too (or ages out).
        tick.card(
            CardKind.WORKING,
            _working_text(tick.ctx, now),
            _queue_buttons(tick.ctx),
        )
        return tick.result(transition, f"{reason}:queued behind the running turn")
    if state is TurnState.QUEUED:
        tick.card(CardKind.QUEUED, _queued_text(tick.ctx), _queue_buttons(tick.ctx))
        return tick.result(transition, f"{reason}:another prompt queued")
    if state is TurnState.WAKING:
        tick.card(CardKind.WAKING, _waking_text(tick.ctx), _ACTIVE_BUTTONS)
        return tick.result(transition, f"{reason}:queued while the workspace wakes")
    if state is TurnState.CANCELLING:
        return tick.result(transition, f"{reason}:posted during a cancel")
    if state is TurnState.DRAINING:
        # The previous turn is still settling; this prompt has its own start to
        # wait for, so start_witnessed goes back to false.
        tick.enter(
            TurnState.QUEUED,
            now,
            start_witnessed=False,
            consecutive_idle=0,
            index_at_post=index_at_post,
        )
        tick.card(CardKind.QUEUED, _queued_text(tick.ctx), _queue_buttons(tick.ctx))
        tick.retune(now, "queued")
        return tick.result(transition, f"{reason}:new prompt while draining")
    _start_turn(tick, now, index_at_post)
    return tick.result(transition, reason)


def _on_post_ok(tick: _Tick, evidence: PostOk, now: float) -> TransitionResult:
    _track_prompt(tick, evidence.message_id, now, evidence.index_at_post)
    return _accept_prompt(
        tick,
        now,
        index_at_post=evidence.index_at_post,
        transition=1,
        reason="post accepted",
    )


def _on_post_ambiguous(
    tick: _Tick, evidence: PostAmbiguous, now: float
) -> TransitionResult:
    # The POST may or may not have landed. Re-POSTing the identical messageId is
    # verified to dedupe server-side, so the safe move is always to re-POST.
    _track_prompt(tick, evidence.message_id, now, None)
    tick.do(RePost(evidence.message_id))
    return _accept_prompt(
        tick,
        now,
        index_at_post=None,
        transition=3,
        reason=evidence.reason or "post outcome unknown",
    )


# ── Boot — rules 3 and 22 ────────────────────────────────────────────────────


def _rebase_clock(tick: _Tick, now: float) -> None:
    """Move every timestamp into the current monotonic frame.

    Persisted timestamps came from a previous process's monotonic clock, which
    has no meaning here. Anything else risks an instant bogus timeout or a
    watchdog that never fires. The cost is that a turn spanning a restart
    reports its duration from the restart.
    """
    tick.evolve(
        entered_state_at=now,
        last_delta_at=None,
        warned_no_output_at=None,
        turn_started_at=now if tick.ctx.turn_started_at is not None else None,
        pending_prompts=tuple(
            PendingPrompt(p.message_id, now, p.index_at_post)
            for p in tick.ctx.pending_prompts
        ),
    )


def _on_boot(tick: _Tick, evidence: Boot, now: float) -> TransitionResult:
    if evidence.restored_state is not None:
        tick.evolve(state=evidence.restored_state)
    _rebase_clock(tick, now)
    # Boot draws no conclusion from stale counters.
    tick.evolve(
        consecutive_idle=0,
        consecutive_status_failures=0,
        cursor_only=False,
    )
    state = tick.ctx.state

    if state is TurnState.DEAD:
        tick.do(StopPolling("session is gone"))
        return tick.result(20, "boot into a dead session")

    if state is TurnState.SUBMIT_PENDING:
        # Rule 3: we crashed between writing the prompt row and confirming the
        # POST. Re-POST the identical messageId — it dedupes.
        pending = tick.ctx.pending_prompts
        tick.do(ForceDrain("boot"), RequestStatus(), RequestWorkspaceStatus())
        if not pending:
            tick.enter(TurnState.IDLE, now, start_witnessed=False, idle_decay_step=0)
            tick.retune(now, "idle")
            return tick.result(3, "boot: submit pending with no prompt to re-post")
        for prompt in pending:
            tick.do(RePost(prompt.message_id))
        tick.enter(TurnState.QUEUED, now, start_witnessed=False, consecutive_idle=0)
        tick.card(CardKind.QUEUED, _queued_text(tick.ctx), _queue_buttons(tick.ctx))
        tick.retune(now, "queued")
        return tick.result(3, "boot: re-posting the identical messageId")

    # Rule 22: a forced delta + status + workspace status before any conclusion.
    tick.do(ForceDrain("boot"), RequestStatus(), RequestWorkspaceStatus())
    if state in (
        TurnState.QUEUED,
        TurnState.WORKING,
        TurnState.DRAINING,
        TurnState.WAKING,
    ):
        tick.retune(now, "boot")
        return tick.result(22, "boot: refresh everything before concluding")
    tick.retune(now, "boot")
    return tick.result(None, "boot: refresh everything before concluding")


# ── Status ───────────────────────────────────────────────────────────────────

_StateFn = Callable[[_Tick, float], TransitionResult]


def _noop(tick: _Tick, now: float) -> TransitionResult:
    return tick.result(None, "no-op")


# Status(idle), per state ─────────────────────────────────────────────────────


def _idle_in_queued(tick: _Tick, now: float) -> TransitionResult:
    # RULE 6 — the queued-but-idle trap. A queued prompt has not started a turn
    # yet and the session reports `idle` until it does. While start_witnessed is
    # false this observation carries no information at all, so it is not even
    # counted towards the drain confirmations.
    return tick.result(6, "idle ignored: the turn has not started yet")


def _idle_in_working(tick: _Tick, now: float) -> TransitionResult:
    # RULE 13 — never declare done here. Trailing content is common.
    tick.enter(TurnState.DRAINING, now, consecutive_idle=1)
    tick.retune(now, "draining")
    return tick.result(13, "status idle: draining, not done")


def _drain_check(tick: _Tick, now: float, *, transition: int) -> TransitionResult:
    """Shared tail of rules 15 and 19."""
    context = tick.ctx
    confirms = DRAIN_CONFIRMS if transition == 15 else CANCEL_CONFIRMS
    if context.consecutive_idle < confirms:
        return tick.result(transition, f"idle {context.consecutive_idle}/{confirms}")
    _age_out_prompts(tick, now)
    if transition == 15:
        # Rule 15 will not call a turn finished while a prompt we POSTed has
        # never been seen in the transcript. A cancel (rule 19) deliberately
        # does not wait: the user asked for everything to stop, so the
        # unwitnessed prompts are abandoned instead.
        live = tick.ctx.live_outstanding(now)
        if live > 0:
            return tick.result(transition, f"holding: {live} prompt(s) not witnessed")
        _finalize_done(tick, now)
        return tick.result(15, "turn finished")
    _finalize(
        tick,
        now,
        kind=CardKind.CANCELLED,
        text=_cancelled_text(tick.ctx),
        buttons=_DONE_BUTTONS,
        ok=False,
        error=None,
        canceled_queued_messages=tick.ctx.canceled_queued_messages,
        reason="canceled",
    )
    return tick.result(19, "cancel confirmed")


def _idle_in_draining(tick: _Tick, now: float) -> TransitionResult:
    # RULE 15 — three consecutive idles, zero delta, nothing outstanding.
    tick.evolve(consecutive_idle=tick.ctx.consecutive_idle + 1)
    return _drain_check(tick, now, transition=15)


def _idle_in_cancelling(tick: _Tick, now: float) -> TransitionResult:
    # RULE 19 — two consecutive idles plus a drain.
    tick.evolve(consecutive_idle=tick.ctx.consecutive_idle + 1)
    return _drain_check(tick, now, transition=19)


def _idle_in_error(tick: _Tick, now: float) -> TransitionResult:
    # The error cleared. It usually does not, so this is worth acting on.
    if tick.ctx.live_outstanding(now) > 0:
        tick.enter(TurnState.QUEUED, now, error_message=None, consecutive_idle=0)
        tick.card(CardKind.QUEUED, _queued_text(tick.ctx), _queue_buttons(tick.ctx))
        tick.retune(now, "queued")
        return tick.result(None, "error cleared with a prompt still in flight")
    tick.enter(
        TurnState.IDLE, now, error_message=None, consecutive_idle=0, idle_decay_step=0
    )
    tick.do(SetTopicMarker(TopicMarker.IDLE))
    tick.retune(now, "idle")
    return tick.result(None, "error cleared")


def _idle_in_idle(tick: _Tick, now: float) -> TransitionResult:
    return tick.result(None, "idle confirmed")


_STATUS_IDLE: Final[dict[TurnState, _StateFn]] = {
    TurnState.IDLE: _idle_in_idle,
    TurnState.SUBMIT_PENDING: _idle_in_queued,
    TurnState.QUEUED: _idle_in_queued,
    TurnState.WAKING: _noop,
    TurnState.WORKING: _idle_in_working,
    TurnState.DRAINING: _idle_in_draining,
    TurnState.CANCELLING: _idle_in_cancelling,
    TurnState.ERROR: _idle_in_error,
}


# Status(working), per state ──────────────────────────────────────────────────


def _working_from_queued(tick: _Tick, now: float) -> TransitionResult:
    # RULE 4 — the only observation that makes a later `idle` meaningful.
    _witness_start(tick, now, "working")
    return tick.result(4, "status working: the turn started")


def _working_from_idle(tick: _Tick, now: float) -> TransitionResult:
    # RULE 2 (status flavour) — someone drove this session from the Mac.
    _witness_start(tick, now, "working")
    return tick.result(2, "out-of-band turn started")


def _working_from_draining(tick: _Tick, now: float) -> TransitionResult:
    # RULE 16 — DRAINING <-> WORKING ping-pong is expected, not a bug.
    tick.enter(TurnState.WORKING, now, consecutive_idle=0)
    tick.card(CardKind.WORKING, _working_text(tick.ctx, now), _queue_buttons(tick.ctx))
    tick.retune(now, "working")
    return tick.result(16, "status working: back from draining")


def _working_from_working(tick: _Tick, now: float) -> TransitionResult:
    tick.evolve(consecutive_idle=0)
    return tick.result(None, "still working")


def _working_from_error(tick: _Tick, now: float) -> TransitionResult:
    tick.evolve(error_message=None)
    _witness_start(tick, now, "working")
    return tick.result(None, "recovered from error into a running turn")


_STATUS_WORKING: Final[dict[TurnState, _StateFn]] = {
    TurnState.IDLE: _working_from_idle,
    TurnState.SUBMIT_PENDING: _working_from_queued,
    TurnState.QUEUED: _working_from_queued,
    TurnState.WAKING: _working_from_queued,
    TurnState.WORKING: _working_from_working,
    TurnState.DRAINING: _working_from_draining,
    TurnState.CANCELLING: _working_from_working,
    TurnState.ERROR: _working_from_error,
}


def _on_status(tick: _Tick, evidence: Status, now: float) -> TransitionResult:
    # A successful /status call always clears the status-free fallback.
    recovered = tick.ctx.cursor_only
    tick.evolve(consecutive_status_failures=0, cursor_only=False)

    if evidence.value is SessionStatusValue.UNKNOWN:
        # "No information" — never "the turn finished".
        if recovered:
            tick.retune(now, "status recovered")
        return tick.result(None, "unknown status ignored")

    tick.evolve(last_status=evidence.value)

    if evidence.value is SessionStatusValue.ERROR:
        # RULE 17 — drain first, then surface errorMessage ?? lastError.
        message = (
            evidence.error_message or evidence.last_error or "the session is in error"
        )
        if tick.ctx.state is TurnState.ERROR and tick.ctx.error_message == message:
            # `error` persists indefinitely; do not re-announce it every poll.
            if recovered:
                tick.retune(now, "status recovered")
            return tick.result(None, "error unchanged")
        _enter_error(tick, now, message)
        return tick.result(17, "status error")

    table = (
        _STATUS_IDLE if evidence.value is SessionStatusValue.IDLE else _STATUS_WORKING
    )
    result = table.get(tick.ctx.state, _noop)(tick, now)
    if recovered and not any(isinstance(a, SetCadence) for a in result.actions):
        tick.retune(now, "status recovered")
        return tick.result(result.transition, result.reason)
    return result


def _on_status_unavailable(
    tick: _Tick, evidence: StatusUnavailable, now: float
) -> TransitionResult:
    """PLAN §Status-free fallback: K failures in a row and we stop asking."""
    failures = max(
        tick.ctx.consecutive_status_failures + 1, evidence.consecutive_failures
    )
    tick.evolve(consecutive_status_failures=failures)
    if failures < STATUS_FAILURE_THRESHOLD or tick.ctx.cursor_only:
        return tick.result(None, f"status unavailable ({failures})")
    tick.evolve(cursor_only=True)
    tick.do(
        # One quiet line, not four. The mechanism (transcript-only tracking, a
        # laggy card) is bot internals the owner cannot act on; the only fact
        # worth a bubble is that replies keep arriving.
        Notify(
            "Status API down · replies still arrive.",
            NotifyLevel.QUIET,
            once_key="cursor-only",
        )
    )
    tick.retune(now, "cursor-only")
    return tick.result(None, "entered cursor-only mode")


# ── Delta — rules 2, 5, 12, 14 ───────────────────────────────────────────────


def _delta_started(evidence: Delta) -> bool:
    """Whether this delta proves the queued turn actually started.

    Turn attribution runs on ``content.turnId`` (verified 6/6 and 7/7 against the
    live API), so agent content or a matching turn id is proof. Do not fall back
    to ``sessionIndex``: it is not gapless, and an unrelated later event is not
    evidence that this prompt started.
    """
    if evidence.n <= 0:
        return False
    return evidence.has_agent_content or bool(evidence.turn_ids)


def _delta_in_queued(tick: _Tick, evidence: Delta, now: float) -> TransitionResult:
    # RULE 5 — covers the fast turn that starts *and* finishes between two polls.
    if not _delta_started(evidence):
        return tick.result(None, "delta predates this prompt")
    _witness_start(tick, now, "working")
    return tick.result(5, "delta: the turn started")


def _delta_in_working(tick: _Tick, evidence: Delta, now: float) -> TransitionResult:
    # RULE 12 — content is already delivered by the time we see this.
    tick.evolve(warned_no_output_at=None)
    tick.card(CardKind.WORKING, _working_text(tick.ctx, now), _queue_buttons(tick.ctx))
    tick.retune(now, "working")
    return tick.result(12, "delta while working")


def _delta_in_draining(tick: _Tick, evidence: Delta, now: float) -> TransitionResult:
    # RULE 14 — trailing content. The ping-pong back to WORKING is by design.
    tick.enter(TurnState.WORKING, now, consecutive_idle=0, warned_no_output_at=None)
    tick.card(CardKind.WORKING, _working_text(tick.ctx, now), _queue_buttons(tick.ctx))
    tick.retune(now, "working")
    return tick.result(14, "trailing content while draining")


def _delta_in_idle(tick: _Tick, evidence: Delta, now: float) -> TransitionResult:
    # RULE 2 — out-of-band activity: you drove the session from the Mac.
    if evidence.has_agent_content:
        tick.evolve(turn_started_at=now, tool_calls=0, status_card_msg_id=None)
        _witness_start(tick, now, "working")
        return tick.result(2, "out-of-band agent output")
    # Something happened but it is not agent output (a user echo posted from the
    # Mac, a lifecycle event). Poll fast again rather than concluding anything.
    tick.evolve(idle_decay_step=0)
    tick.retune(now, "idle")
    return tick.result(None, "non-agent delta while idle")


def _delta_in_waking(tick: _Tick, evidence: Delta, now: float) -> TransitionResult:
    if evidence.has_agent_content:
        _witness_start(tick, now, "working")
        return tick.result(12, "agent output while the workspace was waking")
    return tick.result(None, "delta while waking")


def _delta_passive(tick: _Tick, evidence: Delta, now: float) -> TransitionResult:
    """CANCELLING / ERROR: record it, deliver it, conclude nothing."""
    return tick.result(None, f"delta while {tick.ctx.state.value.lower()}")


_DeltaFn = Callable[[_Tick, Delta, float], TransitionResult]

_DELTA: Final[dict[TurnState, _DeltaFn]] = {
    TurnState.IDLE: _delta_in_idle,
    TurnState.SUBMIT_PENDING: _delta_in_queued,
    TurnState.QUEUED: _delta_in_queued,
    TurnState.WAKING: _delta_in_waking,
    TurnState.WORKING: _delta_in_working,
    TurnState.DRAINING: _delta_in_draining,
    TurnState.CANCELLING: _delta_passive,
    TurnState.ERROR: _delta_passive,
}


def _on_delta(tick: _Tick, evidence: Delta, now: float) -> TransitionResult:
    if evidence.n <= 0:
        return tick.result(None, "empty delta")
    context = tick.ctx
    # Bookkeeping first: this runs identically in every state, because the
    # content it describes has already been recorded and queued for delivery.
    pending = {p.message_id for p in context.pending_prompts}
    witnessed = frozenset(pending & (evidence.witnessed_prompt_ids | evidence.turn_ids))
    tick.evolve(
        last_delta_at=now,
        consecutive_idle=0,
        delivered=context.delivered + evidence.n,
        tool_calls=context.tool_calls + evidence.tool_calls,
        turn_ids=context.turn_ids | evidence.turn_ids,
    )
    if witnessed:
        tick.evolve(
            pending_prompts=tuple(
                p for p in tick.ctx.pending_prompts if p.message_id not in witnessed
            )
        )
    if evidence.has_error_result and tick.ctx.error_message is None:
        # The turn's own result payload says it failed. Remember it so the
        # finalized card is honest, without flapping into ERROR.
        tick.evolve(error_message="the agent reported an error result")
    return _DELTA.get(context.state, _delta_passive)(tick, evidence, now)


# ── Ws — rules 9, 10, 20 ─────────────────────────────────────────────────────


def _on_ws(tick: _Tick, evidence: Ws, now: float) -> TransitionResult:
    tick.evolve(
        workspace_status=evidence.status, lifecycle_step=evidence.lifecycle_step
    )
    state = tick.ctx.state

    if evidence.status.is_gone:
        # RULE 20 (workspace flavour) — archived or deleted is terminal.
        return _die(tick, now, f"workspace {evidence.status.value}")

    if evidence.status.is_waking:
        # RULE 9 — show the waking card, with the lifecycle step.
        if state in (TurnState.IDLE, TurnState.QUEUED, TurnState.SUBMIT_PENDING):
            # No Notify here. The card already reads "⏳ waking · cloning_repo"
            # and carries Stop; a silent bubble restating it in three lines is
            # pure scroll. ``waking_notified`` still records that the waking
            # face has been shown, so rule 10 can reset it.
            tick.enter(TurnState.WAKING, now, consecutive_idle=0, waking_notified=True)
            tick.card(CardKind.WAKING, _waking_text(tick.ctx), _ACTIVE_BUTTONS)
            # 💤 means "nothing is happening here". With a prompt outstanding
            # something *is* happening — the machine is waiting on infrastructure
            # — so the topic says ⏳ whether the workspace was asleep or is
            # cloning. Which of the two it was is our problem, not the reader's,
            # and pretending otherwise costs a rename to correct it a second later.
            tick.do(
                SetTopicMarker(
                    TopicMarker.SLEEPING
                    if evidence.status is WorkspaceStatusValue.SLEEPING
                    and tick.ctx.outstanding == 0
                    else TopicMarker.INITIALIZING
                )
            )
            tick.retune(now, "waking")
            return tick.result(9, "workspace is waking")
        if state is TurnState.WAKING:
            tick.card(CardKind.WAKING, _waking_text(tick.ctx), _ACTIVE_BUTTONS)
            return tick.result(9, "still waking")
        return tick.result(None, "workspace waking, turn already in flight")

    if state is TurnState.WAKING and evidence.status.is_usable:
        # RULE 10 — resume the fast cadence.
        tick.evolve(waking_notified=False)
        if tick.ctx.live_outstanding(now) > 0 or tick.ctx.outstanding > 0:
            tick.enter(TurnState.QUEUED, now, consecutive_idle=0, start_witnessed=False)
            tick.card(CardKind.QUEUED, _queued_text(tick.ctx), _queue_buttons(tick.ctx))
            for prompt in tick.ctx.pending_prompts:
                tick.do(RePost(prompt.message_id))
            # Still ⏳, not blank. The workspace is up but the turn has not
            # started, and a blank prefix here meant the topic flickered
            # ⏳ → nothing → ⚙️ inside two polls: two renames, two permanent
            # "changed the topic name" lines, to say nothing that lasted.
            tick.do(SetTopicMarker(TopicMarker.INITIALIZING))
            tick.retune(now, "queued")
            return tick.result(10, "workspace ready")
        tick.enter(TurnState.IDLE, now, consecutive_idle=0, idle_decay_step=0)
        tick.do(SetTopicMarker(TopicMarker.IDLE))
        tick.retune(now, "idle")
        return tick.result(10, "workspace ready, nothing outstanding")

    return tick.result(None, f"workspace {evidence.status.value}")


# ── Cancel / CancelAck — rule 18 ─────────────────────────────────────────────


def _on_cancel(tick: _Tick, evidence: Cancel, now: float) -> TransitionResult:
    # RULE 18 — /stop is never confirmed; friction on cancel is worse than an
    # accidental cancel.
    tick.enter(
        TurnState.CANCELLING,
        now,
        consecutive_idle=0,
        cancel_requested_at=now,
    )
    tick.do(PostCancel(evidence.requested_by))
    # Not `()`. A cancel that never settles used to leave a spinner with
    # nothing to press; Check asks Conductor directly what the session is
    # actually doing, which is the question the reader now has.
    tick.card(CardKind.CANCELLING, "stopping…", (CardButton.CHECK,))
    tick.retune(now, "cancelling")
    return tick.result(18, "cancel requested")


def _on_cancel_ack(tick: _Tick, evidence: CancelAck, now: float) -> TransitionResult:
    tick.evolve(canceled_queued_messages=evidence.canceled_queued_messages)
    if tick.ctx.state is not TurnState.CANCELLING:
        tick.enter(
            TurnState.CANCELLING, now, consecutive_idle=0, cancel_requested_at=now
        )
        tick.retune(now, "cancelling")
    tick.card(CardKind.CANCELLING, _cancelled_text(tick.ctx), ())
    return tick.result(18, "cancel acknowledged")


# ── E404 — rule 20 ───────────────────────────────────────────────────────────


def _die(tick: _Tick, now: float, what: str) -> TransitionResult:
    """RULE 20 — terminal. Unbind the topic, stop the task, say so once."""
    tick.enter(
        TurnState.DEAD,
        now,
        pending_prompts=(),
        consecutive_idle=0,
        start_witnessed=False,
    )
    tick.card(CardKind.DEAD, what, ())
    tick.do(
        StopTyping(),
        SetTopicMarker(TopicMarker.ARCHIVED),
        # The topic is already renamed with the archived prefix and closed,
        # and the card
        # says the same thing. One line is enough to push.
        Notify(f"Gone · {what}", NotifyLevel.LOUD, once_key="dead"),
        UnbindTopic(what),
        StopPolling(what),
    )
    tick.evolve(cadence_ms=0)
    return tick.result(20, what)


def _on_404(tick: _Tick, evidence: E404, now: float) -> TransitionResult:
    return _die(tick, now, f"{evidence.what} is gone (404)")


# ── Timer — rules 7, 8, 11, 21, 23 and the cursor-only fallback ──────────────


def _cursor_only_finalize(tick: _Tick, now: float) -> bool:
    """Status-free fallback: 45s of quiet with nothing outstanding ends the turn."""
    if not tick.ctx.cursor_only:
        return False
    if tick.ctx.quiet_for(now) < CURSOR_ONLY_QUIET_FINALIZE_S:
        return False
    if tick.ctx.live_outstanding(now) > 0:
        return False
    _finalize_done(tick, now)
    return True


def _timer_in_idle(tick: _Tick, now: float) -> TransitionResult:
    # RULE 23 — cadence decay 20 → 30 → 60 → 120s.
    step_index = min(tick.ctx.idle_decay_step + 1, len(CADENCE_IDLE_DECAY_MS) - 1)
    tick.evolve(idle_decay_step=step_index)
    tick.retune(now, "idle decay")
    return tick.result(23, f"idle decay step {step_index}")


def _timer_in_queued(tick: _Tick, now: float) -> TransitionResult:
    context = tick.ctx
    elapsed = context.elapsed_in_state(now)
    if elapsed >= QUEUED_TIMEOUT_S and not context.start_witnessed:
        # RULE 8 — it never started. Offer Retry, which mints a new messageId.
        _drop_prompts(
            tick,
            frozenset(p.message_id for p in context.pending_prompts),
            "prompt never started",
        )
        _enter_error(
            tick,
            now,
            "The prompt never started after 10 minutes.",
            buttons=_TIMEOUT_BUTTONS,
            card_text="never started",
            drain=False,
        )
        return tick.result(8, "queued timeout")
    workspace_ok = (
        context.workspace_status is None or context.workspace_status.is_usable
    )
    if (
        elapsed >= QUEUED_SLOW_AFTER_S
        and context.last_delta_at is None
        and workspace_ok
    ):
        # RULE 7 — most likely queued behind another turn. Back off.
        if context.cadence_ms != CADENCE_QUEUED_SLOW_MS:
            tick.card(
                CardKind.QUEUED,
                "queued behind another turn",
                _queue_buttons(context),
            )
        tick.retune(now, "queued slow")
        return tick.result(7, "queued for a while")
    return tick.result(None, "queued")


def _timer_in_waking(tick: _Tick, now: float) -> TransitionResult:
    if tick.ctx.elapsed_in_state(now) >= WAKE_TIMEOUT_S:
        # RULE 11 — wake timeout.
        _enter_error(
            tick,
            now,
            "The workspace did not become ready within 10 minutes.",
            buttons=_TIMEOUT_BUTTONS,
            card_text="wake timed out",
            drain=False,
        )
        return tick.result(11, "wake timeout")
    return tick.result(None, "waking")


def _timer_in_working(tick: _Tick, now: float) -> TransitionResult:
    if _cursor_only_finalize(tick, now):
        return tick.result(15, "cursor-only: quiet long enough to finalize")
    context = tick.ctx
    quiet = context.quiet_for(now)
    # RULE 21 — a watchdog, never a kill.
    if quiet >= NO_OUTPUT_WARN_S and context.warned_no_output_at is None:
        tick.evolve(warned_no_output_at=now)
        tick.card(
            CardKind.STALLED,
            f"{_working_text(context, now)} · stalled?",
            _STALLED_BUTTONS,
        )
        tick.do(
            # "· still polling" carries the reassurance the old two sentences
            # spelled out; the card's own "stalled?" + Check button carry
            # the rest.
            Notify(
                f"Quiet {format_duration(int(quiet * 1000))} · still polling",
                NotifyLevel.QUIET,
                once_key="no-output",
            )
        )
        tick.retune(now, "stalled")
        return tick.result(21, "no output warning")
    if quiet >= NO_OUTPUT_SLOW_S:
        tick.retune(now, "stalled")
        return tick.result(21, "no output, slow cadence")
    return tick.result(None, "working")


def _timer_in_draining(tick: _Tick, now: float) -> TransitionResult:
    if _cursor_only_finalize(tick, now):
        return tick.result(15, "cursor-only: quiet long enough to finalize")
    # A prompt may have just aged out; if the confirmations are already in, the
    # turn can finalize without waiting for another /status.
    before = tick.ctx.outstanding
    _age_out_prompts(tick, now)
    if (
        tick.ctx.consecutive_idle >= DRAIN_CONFIRMS
        and tick.ctx.live_outstanding(now) == 0
    ):
        _finalize_done(tick, now)
        return tick.result(15, "turn finished after the prompt aged out")
    if before != tick.ctx.outstanding:
        return tick.result(None, "prompt aged out while draining")
    return tick.result(None, "draining")


def _timer_in_cancelling(tick: _Tick, now: float) -> TransitionResult:
    if tick.ctx.cursor_only and tick.ctx.quiet_for(now) >= CURSOR_ONLY_QUIET_FINALIZE_S:
        _finalize(
            tick,
            now,
            kind=CardKind.CANCELLED,
            text=_cancelled_text(tick.ctx),
            buttons=_DONE_BUTTONS,
            ok=False,
            error=None,
            canceled_queued_messages=tick.ctx.canceled_queued_messages,
            reason="canceled",
        )
        return tick.result(19, "cursor-only: cancel settled")
    requested_at = tick.ctx.cancel_requested_at
    if requested_at is not None and (now - requested_at) >= CANCEL_TIMEOUT_S:
        # RULE 23 — Conductor never acknowledged the cancel.
        #
        # `PostCancel` is issued once and its transport errors are swallowed, so
        # this state used to be absorbing: "🛑 stopping…" with no buttons, a
        # typing indicator that never stopped, and a 2s poll, forever. Do not
        # finalize — the turn may well still be running, and claiming "stopped"
        # would be the one lie the card must never tell. Go back to reporting
        # what is observable and hand back the controls.
        tick.enter(
            TurnState.WORKING,
            now,
            consecutive_idle=0,
            cancel_requested_at=None,
        )
        tick.card(
            CardKind.WORKING,
            f"{_working_text(tick.ctx, now)} · stop not confirmed",
            _STALLED_BUTTONS,
        )
        tick.do(
            Notify(
                "Conductor did not confirm the stop. The turn may still be "
                "running — tap Check to ask, or Stop to try again.",
                NotifyLevel.QUIET,
                once_key=f"cancel-unconfirmed:{int(requested_at)}",
            )
        )
        tick.retune(now, "cancel not confirmed")
        return tick.result(23, "cancel was never confirmed")
    return tick.result(None, "cancelling")


def _timer_passive(tick: _Tick, now: float) -> TransitionResult:
    return tick.result(None, tick.ctx.state.value.lower())


_TIMER: Final[dict[TurnState, _StateFn]] = {
    TurnState.IDLE: _timer_in_idle,
    TurnState.SUBMIT_PENDING: _timer_passive,
    TurnState.QUEUED: _timer_in_queued,
    TurnState.WAKING: _timer_in_waking,
    TurnState.WORKING: _timer_in_working,
    TurnState.DRAINING: _timer_in_draining,
    TurnState.CANCELLING: _timer_in_cancelling,
    TurnState.ERROR: _timer_passive,
}


def _on_timer(tick: _Tick, evidence: Timer, now: float) -> TransitionResult:
    _age_out_prompts(tick, now)
    return _TIMER.get(tick.ctx.state, _timer_passive)(tick, now)


# ── dispatch ─────────────────────────────────────────────────────────────────

_Handler = Callable[[_Tick, Any, float], TransitionResult]

_HANDLERS: Final[dict[type, _Handler]] = {
    PostOk: _on_post_ok,
    PostAmbiguous: _on_post_ambiguous,
    Status: _on_status,
    StatusUnavailable: _on_status_unavailable,
    Delta: _on_delta,
    Ws: _on_ws,
    Timer: _on_timer,
    Cancel: _on_cancel,
    CancelAck: _on_cancel_ack,
    Boot: _on_boot,
    E404: _on_404,
}


def step(context: TurnContext, evidence: Evidence, now: float) -> TransitionResult:
    """Advance one session's turn state machine by one piece of evidence.

    ``now`` is monotonic seconds, supplied by the caller so this stays pure.
    The returned actions must be executed **in order**.
    """
    if context.state is TurnState.DEAD:
        if isinstance(evidence, Boot):
            return TransitionResult(
                context,
                (StopPolling("session is gone"),),
                20,
                "boot into a dead session",
            )
        return TransitionResult(context, (), None, "dead: evidence ignored")
    handler = _HANDLERS.get(type(evidence))
    if handler is None:
        # Timer is the only evidence with no state of its own; anything truly
        # unknown must not raise inside a poller.
        return TransitionResult(context, (), None, "unhandled evidence")
    return handler(_Tick(context), evidence, now)
