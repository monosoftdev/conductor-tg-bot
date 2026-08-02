"""The pure vocabulary of the turn state machine. No logic, no I/O.

``turn/machine.py`` implements ``(context, evidence, now) -> TransitionResult``
against these types. Everything the 23 transitions in ``docs/PLAN.md`` need must
be expressible here — states, the evidence that moves between them, the actions
that fall out, and the context that carries the bookkeeping.

**The state machine never gates delivery.** It drives poll cadence, the typing
indicator, the status card and ``/stop``. Content delivery runs unconditionally
off the transcript cursor on every tick in every state. If the machine is wrong
you get a stale progress line; you never lose or double-see a reply.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final, Self

from ctb import signals
from ctb.conductor.models import SessionStatusValue, WorkspaceStatusValue

__all__ = [
    "AGENTS_MARKING_TURN_END",
    "AbandonPrompt",
    "Action",
    "Boot",
    "CANCEL_CONFIRMS",
    "CADENCE_CURSOR_ONLY_MS",
    "CADENCE_DRAINING_MS",
    "CADENCE_IDLE_DECAY_MS",
    "CADENCE_QUEUED_MS",
    "CADENCE_QUEUED_SLOW_MS",
    "CADENCE_WAKING_MS",
    "CADENCE_WORKING_MS",
    "CADENCE_WORKING_STALLED_MS",
    "Cancel",
    "CancelAck",
    "CardButton",
    "CardKind",
    "DRAIN_CONFIRMS",
    "Delta",
    "E404",
    "EDITED_PATHS_CAP",
    "EditStatusCard",
    "Evidence",
    "Finalize",
    "ForceDrain",
    "JITTER_FRACTION",
    "NO_OUTPUT_SLOW_S",
    "NO_OUTPUT_WARN_S",
    "Notify",
    "NotifyLevel",
    "PROMPT_AGE_OUT_S",
    "PendingPrompt",
    "PostAmbiguous",
    "PostCancel",
    "PostOk",
    "PostStatusCard",
    "QUEUED_SLOW_AFTER_S",
    "CANCEL_TIMEOUT_S",
    "QUEUED_TIMEOUT_S",
    "RePost",
    "RequestStatus",
    "RequestWorkspaceStatus",
    "STATUS_FAILURE_THRESHOLD",
    "TURN_END_AGE_OUT_S",
    "SetCadence",
    "SetTopicMarker",
    "SetTurnCost",
    "StartTyping",
    "Status",
    "StatusUnavailable",
    "StopPolling",
    "StopTyping",
    "TopicMarker",
    "TransitionResult",
    "TurnContext",
    "TurnState",
    "TurnSummary",
    "UnbindTopic",
    "WAKE_TIMEOUT_S",
    "Ws",
]

# ── cadence and timeout constants (PLAN §Poller, §Turn state machine) ────────
#
# The probe measured a trivial warm turn at ~6.3s end to end (n=2) and never
# reproduced the queued-but-idle trap, so DRAIN_CONFIRMS and the QUEUED timings
# remain conservative guesses. They are here, in one place, precisely so a
# cold-workspace re-probe can replace them without touching machine.py.

CADENCE_QUEUED_MS: Final = 3_000
CADENCE_QUEUED_SLOW_MS: Final = 10_000
CADENCE_WAKING_MS: Final = 10_000
CADENCE_WORKING_MS: Final = 6_000
CADENCE_WORKING_STALLED_MS: Final = 30_000
CADENCE_DRAINING_MS: Final = 2_000
CADENCE_CURSOR_ONLY_MS: Final = 8_000
#: IDLE decays 20s → 30s → 60s → 120s while nothing happens.
CADENCE_IDLE_DECAY_MS: Final[tuple[int, ...]] = (20_000, 30_000, 60_000, 120_000)
#: Every scheduled interval is jittered by ±20% so N sessions never align.
JITTER_FRACTION: Final = 0.2

#: Consecutive ``idle`` observations with zero delta before DRAINING finalizes.
DRAIN_CONFIRMS: Final = 3
#: Same, for a cancel.
CANCEL_CONFIRMS: Final = 2
#: QUEUED with no delta this long: the card says "queued behind another turn".
QUEUED_SLOW_AFTER_S: Final = 90.0
#: QUEUED this long without ever starting: give up and offer Retry.
#: How long a requested cancel may go unconfirmed before the card stops
#: claiming to be stopping. `POST /cancel` is asynchronous and settles in
#: seconds when it works; two minutes is generous, and past it the truthful
#: answer is "Conductor never confirmed", not a spinner with no buttons.
CANCEL_TIMEOUT_S: Final = 120.0
QUEUED_TIMEOUT_S: Final = 600.0
#: WAKING this long: wake timeout.
WAKE_TIMEOUT_S: Final = 600.0
#: WORKING with no delta this long: warn, but never kill.
NO_OUTPUT_WARN_S: Final = 1_200.0
#: …and this long: slow the cadence down. Still a watchdog, not a kill.
NO_OUTPUT_SLOW_S: Final = 3_600.0
#: A posted prompt never witnessed in the transcript stops blocking finalize
#: after this long, so one lost echo cannot wedge the session forever.
PROMPT_AGE_OUT_S: Final = 300.0
#: A turn whose end-of-turn record never arrived stops blocking finalize after
#: this long *without a single new message*. Same shape as the prompt age-out
#: and for the same reason: the gate must not be able to wedge a session. It is
#: measured from the last delta, so a talkative agent never approaches it, and
#: it is long because the failure it guards against — declaring a running agent
#: finished — is the one the user actually reported.
TURN_END_AGE_OUT_S: Final = 900.0
#: How many distinct edited paths one turn remembers. A refactor can touch
#: hundreds of files and the receipt shows five of them, so the rest are counted
#: and never carried — an unbounded tuple on a long turn is memory spent on
#: strings nobody will read.
EDITED_PATHS_CAP: Final = 64
#: Agents known to file an end-of-turn record on every turn, so the gate that
#: waits for one can be trusted from a session's *first* turn rather than only
#: after it has watched one arrive. Verified against real transcripts
#: (``tests/fixtures/probe_verified.jsonl``); an agent not listed here teaches
#: the machine at runtime instead.
AGENTS_MARKING_TURN_END: Final = frozenset({"claude"})
#: Consecutive ``/status`` failures before dropping into cursor-only mode.
STATUS_FAILURE_THRESHOLD: Final = 3
#: In cursor-only mode WORKING is inferred from recent deltas, and quiet this
#: long with nothing outstanding finalizes the turn.
CURSOR_ONLY_ACTIVE_WINDOW_S: Final = 45.0
CURSOR_ONLY_QUIET_FINALIZE_S: Final = 45.0


# ── states ───────────────────────────────────────────────────────────────────


class TurnState(StrEnum):
    """Where a session's current turn is, as far as we can tell."""

    #: Nothing outstanding. Cadence decays.
    IDLE = "IDLE"
    #: The prompt row is written but the POST has not been confirmed. Only
    #: reachable across a crash — recovery is a re-POST with the same messageId.
    SUBMIT_PENDING = "SUBMIT_PENDING"
    #: Prompt accepted, turn not yet witnessed starting. ``idle`` is
    #: structurally ignored here.
    QUEUED = "QUEUED"
    #: The workspace is initializing/sleeping/updating.
    WAKING = "WAKING"
    #: The turn is running.
    WORKING = "WORKING"
    #: Status says idle but trailing content may still land. Never declare done
    #: from WORKING — only from here, after repeated confirmation.
    DRAINING = "DRAINING"
    #: A cancel has been posted; waiting for the session to settle.
    CANCELLING = "CANCELLING"
    #: Session status is ``error`` (which persists indefinitely while still
    #: accepting POSTs). Surfaced with errorMessage/lastError and a Retry.
    ERROR = "ERROR"
    #: The session or workspace 404s. Terminal: unbind and stop polling.
    DEAD = "DEAD"

    @property
    def is_terminal(self) -> bool:
        return self is TurnState.DEAD

    @property
    def is_active(self) -> bool:
        """Whether a turn is plausibly in flight (drives typing + fast cadence)."""
        return self in (
            TurnState.QUEUED,
            TurnState.WAKING,
            TurnState.WORKING,
            TurnState.DRAINING,
            TurnState.CANCELLING,
        )


# ── evidence ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PostOk:
    """``POST /sessions/{id}/messages`` returned 2xx."""

    message_id: str
    state: str = "sent"
    index_at_post: int | None = None


@dataclass(frozen=True, slots=True)
class PostAmbiguous:
    """The POST may or may not have landed. Re-POST the identical messageId."""

    message_id: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Status:
    """``GET /sessions/{id}/status``. A cadence knob and a UX hint. Never truth."""

    value: SessionStatusValue
    error_message: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class StatusUnavailable:
    """``/status`` failed or 429'd. K in a row drops us into cursor-only mode."""

    consecutive_failures: int = 1
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Delta:
    """New transcript messages were fetched this tick.

    Emitted on **every** tick that produced messages, in every state, and always
    after the content has already been recorded and queued for delivery. The
    machine reacts to it for cadence and card purposes only.
    """

    n: int
    max_index: int | None = None
    #: True when at least one new message carried agent output (as opposed to a
    #: user echo or a pure lifecycle event).
    has_agent_content: bool = False
    #: ``content.turnId`` values seen — exact turn attribution.
    turn_ids: frozenset[str] = frozenset()
    #: ``content.id`` values on user echoes: proof our prompts were accepted.
    witnessed_prompt_ids: frozenset[str] = frozenset()
    #: Tool-use blocks seen, for the completed-turn header line.
    tool_calls: int = 0
    #: Paths of files this page's tool calls edited, first-seen order, unique.
    #: A tuple rather than a set because the finish line prints them and the
    #: order an agent touched files in is the order it explains them in.
    edited_paths: tuple[str, ...] = ()
    #: A ``result`` payload flagged ``is_error``.
    has_error_result: bool = False
    #: Turn ids whose end-of-turn record arrived in this page: the agent's own
    #: statement that the turn is over. See ``TranscriptMessage.ends_turn``.
    ended_turn_ids: frozenset[str] = frozenset()
    #: An end-of-turn record that carried no ``turnId`` — it closes whatever
    #: was open rather than nothing.
    has_untagged_turn_end: bool = False


@dataclass(frozen=True, slots=True)
class Ws:
    """``GET /workspaces/{id}/status``."""

    status: WorkspaceStatusValue
    lifecycle_step: str | None = None


@dataclass(frozen=True, slots=True)
class Timer:
    """A tick with no new information. ``now`` is monotonic seconds."""

    now: float


@dataclass(frozen=True, slots=True)
class Cancel:
    """The user asked for ``/stop``."""

    requested_by: int | None = None


@dataclass(frozen=True, slots=True)
class CancelAck:
    """``POST /sessions/{id}/cancel`` came back."""

    canceled_queued_messages: int = 0
    status: str | None = None


@dataclass(frozen=True, slots=True)
class Boot:
    """The process just started (or the poller was respawned) for this session.

    Forces a full delta + status + workspace-status refresh before any
    conclusion is drawn.
    """

    restored_state: TurnState | None = None


@dataclass(frozen=True, slots=True)
class E404:
    """The session or workspace no longer exists."""

    what: str = "session"


type Evidence = (
    PostOk
    | PostAmbiguous
    | Status
    | StatusUnavailable
    | Delta
    | Ws
    | Timer
    | Cancel
    | CancelAck
    | Boot
    | E404
)


# ── actions ──────────────────────────────────────────────────────────────────


class CardKind(StrEnum):
    """Which face the pinned status card is showing."""

    QUEUED = "queued"
    STARTED = "started"
    WORKING = "working"
    WAKING = "waking"
    STALLED = "stalled"
    DONE = "done"
    ERROR = "error"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    DEAD = "dead"


class CardButton(StrEnum):
    """Buttons the card may carry. The bot layer renders and nonces them."""

    STOP = "stop"
    RETRY = "retry"
    TRANSCRIPT = "transcript"
    OPEN = "open"
    CHECK = "check"
    CLEAR_QUEUE = "clear_queue"
    ARCHIVE = "archive"


class NotifyLevel(StrEnum):
    LOUD = "loud"
    QUIET = "quiet"


class TopicMarker(StrEnum):
    """Topic-name prefix per Conductor state. Renamed on transitions only.

    ``DONE`` and ``IDLE`` are deliberately separate. They used to be one blank
    prefix, which made "finished, you have not read it" and "quiet, nothing to
    do" identical in the topic list — and telling those two apart is the most
    common reason to look at the list at all.

    There is no read receipt for bots, so ``DONE`` cannot clear when you *look*
    at a topic. It clears when you *act* in it: the next prompt moves the
    session to ``WORKING`` and the marker follows.
    """

    INITIALIZING = "initializing"
    IDLE = "idle"
    DONE = "done"
    WORKING = "working"
    ERROR = "error"
    SLEEPING = "sleeping"
    ARCHIVED = "archived"

    @property
    def prefix(self) -> str:
        return _TOPIC_PREFIXES[self]

    @property
    def icons(self) -> tuple[str, ...]:
        """Emoji this state would accept as the topic's icon, best first.

        ``icon_color`` is fixed at creation and can only ever mean *which
        workspace*; ``icon_custom_emoji_id`` can change on every rename, so
        state goes here.

        **A tuple, not one emoji.** Telegram serves bots a fixed pack
        (``getForumTopicIconStickers``) and refuses anything outside it, and an
        unresolvable request is not an error — aiogram omits the field and
        Telegram keeps whatever icon the topic already had. One wanted emoji
        the pack happens not to carry therefore looked exactly like "the icon
        never changes", which is how every topic ended up wearing the same one.
        Naming alternatives costs nothing and fails visibly instead of
        silently. Empty means "leave the icon alone".
        """
        return _TOPIC_ICONS[self]


#: Prefixes come from :mod:`ctb.signals` so the topic list, the pinned card and
#: ``/board`` cannot drift apart — they disagreed on every single state.
_TOPIC_PREFIXES: Final[dict[TopicMarker, str]] = {
    TopicMarker.INITIALIZING: f"{signals.WAITING} ",
    TopicMarker.IDLE: signals.IDLE,
    TopicMarker.DONE: f"{signals.DONE} ",
    TopicMarker.WORKING: f"{signals.WORKING} ",
    TopicMarker.ERROR: f"{signals.ERROR} ",
    TopicMarker.SLEEPING: f"{signals.SLEEPING} ",
    TopicMarker.ARCHIVED: f"{signals.ARCHIVED} ",
}

#: Wanted icons per state, **best first**. Telegram only serves a fixed pack to
#: bots, so these are *requests*: :func:`ctb.bot.handlers.topics.topic_icon_id`
#: takes the first the pack actually carries, and leaves the icon unchanged
#: rather than failing the rename if it carries none of them.
#:
#: ``IDLE`` and ``SLEEPING`` deliberately no longer share ``💤``. They are
#: different facts — "bound and quiet" against "the workspace is asleep" — and
#: since a topic spends most of its life idle, one sleep icon on everything was
#: most of what "all the icons are the same" actually looked like.
_TOPIC_ICONS: Final[dict[TopicMarker, tuple[str, ...]]] = {
    TopicMarker.INITIALIZING: ("⌛", "⏳", "🕒", "🔄"),
    TopicMarker.IDLE: ("💭", "📝", "✏", "💬"),
    TopicMarker.DONE: ("✅", "☑", "🎉", "👍"),
    TopicMarker.WORKING: ("⚡", "🛠", "🔧", "⚙", "🔥"),
    TopicMarker.ERROR: ("❗", "⚠", "❌", "🆘"),
    TopicMarker.SLEEPING: ("💤", "🌙", "😴"),
    TopicMarker.ARCHIVED: ("🏁", "📦", "🗂", "🔒"),
}


@dataclass(frozen=True, slots=True)
class TurnSummary:
    """What the finalized card and the completed-turn header line need."""

    duration_ms: int = 0
    tool_calls: int = 0
    files_changed: int = 0
    #: The paths behind :attr:`files_changed`, in the order the agent touched
    #: them, capped at :data:`EDITED_PATHS_CAP`. ``files_changed`` may exceed
    #: ``len(files)`` only if the cap bit — the count is the honest one.
    files: tuple[str, ...] = ()
    prompts: int = 1
    ok: bool = True
    error: str | None = None
    canceled_queued_messages: int = 0


@dataclass(frozen=True, slots=True)
class PostStatusCard:
    """Create the pinned status card (no card exists yet for this turn)."""

    kind: CardKind
    text: str = ""
    buttons: tuple[CardButton, ...] = ()


@dataclass(frozen=True, slots=True)
class EditStatusCard:
    """Edit the existing card in place. Coalesced: queued edits send only the last."""

    kind: CardKind
    text: str = ""
    buttons: tuple[CardButton, ...] = ()
    #: Activity line from the renderer (a tool call), rate-limited to 1 edit/3s.
    activity: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateActivity:
    """Replace only the status card's latest tool/activity line."""

    activity: str


@dataclass(frozen=True, slots=True)
class SetTurnCost:
    """What this turn has cost so far, in USD.

    The one number the machine cannot derive: it lives in the agent's own
    ``result`` payload. The card shows it only on a finished turn, so it can
    never contradict the live state.
    """

    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class StartTyping:
    """``sendChatAction("typing")`` every 4s until stopped."""


@dataclass(frozen=True, slots=True)
class StopTyping:
    pass


@dataclass(frozen=True, slots=True)
class SetCadence:
    """Set this session's poll interval. Jitter is applied by the poller."""

    interval_ms: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RePost:
    """Re-POST a prompt with the **identical** messageId. Verified to dedupe."""

    message_id: str


@dataclass(frozen=True, slots=True)
class PostCancel:
    """``POST /sessions/{id}/cancel``."""

    requested_by: int | None = None


@dataclass(frozen=True, slots=True)
class AbandonPrompt:
    """Stop waiting for this prompt; it never started or was dropped."""

    message_id: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Notify:
    """A chat message that is not transcript content."""

    text: str
    level: NotifyLevel = NotifyLevel.QUIET
    #: When set, the bot sends this at most once per key per session.
    once_key: str | None = None


@dataclass(frozen=True, slots=True)
class Finalize:
    """The turn is over. Flip the card to done and emit the header line."""

    summary: TurnSummary


@dataclass(frozen=True, slots=True)
class ForceDrain:
    """Page the cursor to exhaustion *before* drawing any conclusion.

    Required by transition 17 (an error may still have partial output pending)
    and transition 22 (boot).
    """

    reason: str = ""


@dataclass(frozen=True, slots=True)
class RequestStatus:
    """Fetch ``/sessions/{id}/status`` on this tick regardless of cadence."""


@dataclass(frozen=True, slots=True)
class RequestWorkspaceStatus:
    """Fetch ``/workspaces/{id}/status`` on this tick regardless of cadence."""


@dataclass(frozen=True, slots=True)
class SetTopicMarker:
    """Rename the topic. Only ever emitted on a real state transition."""

    marker: TopicMarker


@dataclass(frozen=True, slots=True)
class UnbindTopic:
    """The session is gone: detach the topic from it."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class StopPolling:
    """Terminal. The supervisor drops this session's task."""

    reason: str = ""


type Action = (
    PostStatusCard
    | EditStatusCard
    | UpdateActivity
    | SetTurnCost
    | StartTyping
    | StopTyping
    | SetCadence
    | RePost
    | PostCancel
    | AbandonPrompt
    | Notify
    | Finalize
    | ForceDrain
    | RequestStatus
    | RequestWorkspaceStatus
    | SetTopicMarker
    | UnbindTopic
    | StopPolling
)


# ── context ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PendingPrompt:
    """A prompt we POSTed and have not yet seen echoed in the transcript."""

    message_id: str
    posted_at: float
    index_at_post: int | None = None

    def aged_out(self, now: float, ttl: float = PROMPT_AGE_OUT_S) -> bool:
        return (now - self.posted_at) >= ttl


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Everything the machine carries between ticks for one session.

    Frozen: a transition returns a new context via :meth:`evolve`. The poller
    persists the fields it cares about to ``sessions`` so a restart resumes.
    """

    state: TurnState = TurnState.IDLE
    #: True once ``working`` or attributed agent output has been seen.
    #: for the current turn. **While this is false, ``idle`` means nothing.**
    start_witnessed: bool = False
    #: Prompts POSTed but not yet witnessed in the transcript. Finalize is
    #: blocked while any of these is younger than PROMPT_AGE_OUT_S.
    pending_prompts: tuple[PendingPrompt, ...] = ()
    #: Historical snapshot retained for schema/backward compatibility only.
    #: Turn attribution uses ``content.turnId``; sessionIndex is not gapless.
    index_at_post: int | None = None

    last_delta_at: float | None = None
    entered_state_at: float = 0.0
    turn_started_at: float | None = None
    #: Consecutive ``idle`` observations with no intervening delta.
    consecutive_idle: int = 0
    consecutive_status_failures: int = 0
    #: True while ``/status`` is unusable: WORKING is inferred from deltas.
    cursor_only: bool = False

    cadence_ms: int = CADENCE_IDLE_DECAY_MS[0]
    #: Index into CADENCE_IDLE_DECAY_MS while IDLE.
    idle_decay_step: int = 0

    last_status: SessionStatusValue | None = None
    workspace_status: WorkspaceStatusValue | None = None
    lifecycle_step: str | None = None
    waking_notified: bool = False

    error_message: str | None = None
    #: Set when the no-output watchdog has already warned, so it warns once.
    warned_no_output_at: float | None = None

    status_card_msg_id: int | None = None
    tool_calls: int = 0
    #: Files this turn has edited, first-seen order, capped at
    #: :data:`EDITED_PATHS_CAP`. Deliberately **not** persisted: it is receipt
    #: decoration, and a redeploy mid-turn degrades it to the empty tuple —
    #: which is exactly what every turn showed before it existed. ``tool_calls``
    #: has a column and survives; this does not, and must not be read as if it
    #: were authoritative.
    edited_paths: tuple[str, ...] = ()
    delivered: int = 0
    cancel_requested_at: float | None = None
    canceled_queued_messages: int = 0
    #: Turn ids seen this turn — used to attribute output to prompts.
    turn_ids: frozenset[str] = field(default_factory=frozenset)
    #: Turn ids that started and whose end-of-turn record has not arrived.
    #: Non-empty means the agent has not said it is finished, whatever
    #: ``/status`` says.
    open_turn_ids: frozenset[str] = field(default_factory=frozenset)
    #: True once this session has been seen to emit an end-of-turn record.
    #: Sticky, because the gate may only be trusted for an agent that has
    #: demonstrated it: for one that never emits them every turn would look
    #: permanently open and every finalize would wait out the age-out.
    marks_turn_end: bool = False

    def turn_end_pending(self, now: float) -> bool:
        """True while the agent still owes an end-of-turn record for a live turn.

        Measured from the last delta so a lost record ages out instead of
        wedging the session — the same escape hatch ``PROMPT_AGE_OUT_S`` gives
        an unwitnessed prompt.
        """
        if not self.marks_turn_end or not self.open_turn_ids:
            return False
        return self.quiet_for(now) < TURN_END_AGE_OUT_S

    @property
    def outstanding(self) -> int:
        """How many POSTed prompts have not been witnessed yet."""
        return len(self.pending_prompts)

    def live_outstanding(self, now: float) -> int:
        """``outstanding`` ignoring prompts old enough to have aged out."""
        return sum(1 for p in self.pending_prompts if not p.aged_out(now))

    @property
    def has_card(self) -> bool:
        return self.status_card_msg_id is not None

    def elapsed_in_state(self, now: float) -> float:
        return max(0.0, now - self.entered_state_at)

    def quiet_for(self, now: float) -> float:
        """Seconds since the last delta (since entering the state if none yet)."""
        reference = (
            self.last_delta_at
            if self.last_delta_at is not None
            else (self.entered_state_at)
        )
        return max(0.0, now - reference)

    def turn_duration_ms(self, now: float) -> int:
        if self.turn_started_at is None:
            return 0
        return max(0, int((now - self.turn_started_at) * 1000))

    def evolve(self, **changes: object) -> Self:
        """A new context with ``changes`` applied. Never mutates."""
        return replace(self, **changes)  # pyright: ignore[reportArgumentType]

    def enter(self, state: TurnState, now: float, **changes: object) -> Self:
        """Move to ``state``, stamping ``entered_state_at``."""
        return replace(  # pyright: ignore[reportArgumentType]
            self, state=state, entered_state_at=now, **changes
        )

    def with_prompt(self, prompt: PendingPrompt) -> Self:
        return replace(self, pending_prompts=(*self.pending_prompts, prompt))

    def without_prompts(self, message_ids: frozenset[str] | set[str]) -> Self:
        if not message_ids:
            return self
        return replace(
            self,
            pending_prompts=tuple(
                p for p in self.pending_prompts if p.message_id not in message_ids
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """Flat mapping for persistence and for structured log lines."""
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """What ``machine.step()`` returns.

    ``transition`` is the row number from the PLAN table, so a log line and a
    test can both name the rule that fired.
    """

    context: TurnContext
    actions: tuple[Action, ...] = ()
    transition: int | None = None
    reason: str = ""

    @property
    def state(self) -> TurnState:
        return self.context.state
