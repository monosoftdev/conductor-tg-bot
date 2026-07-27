"""One reliable polling loop per bound Conductor session.

The ordering in :meth:`SessionPoller.tick` is intentional:

1. reconcile prompts written by Telegram handlers;
2. drain the transcript and queue deliveries, in every state;
3. feed that already-durable delta (or a timer) to the state machine;
4. fetch status only for cadence/UX.

Status can therefore be stale, unavailable, or permanently ``error`` without
ever suppressing transcript delivery.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from ctb.conductor.client import ConductorClient, TransportFailure
from ctb.conductor.errors import (
    ApiError,
    AuthFatal,
    CircuitOpen,
    ConductorError,
    NotFound,
)
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, now_ms
from ctb.db.repo import prompts, sessions, workspaces
from ctb.logging import get_logger
from ctb.turn import cursor
from ctb.turn.machine import step
from ctb.turn.state import (
    E404,
    JITTER_FRACTION,
    AbandonPrompt,
    Action,
    Boot,
    CancelAck,
    Evidence,
    ForceDrain,
    Notify,
    NotifyLevel,
    PendingPrompt,
    PostCancel,
    PostOk,
    RePost,
    RequestStatus,
    RequestWorkspaceStatus,
    SetCadence,
    SetTopicMarker,
    SetTurnCost,
    Status,
    StatusUnavailable,
    StopPolling,
    Timer,
    TurnContext,
    TurnState,
    UnbindTopic,
    UpdateActivity,
    Ws,
)

__all__ = [
    "FORCE_DRAIN_MAX_PASSES",
    "ActionSink",
    "SessionPoller",
    "TickReport",
]

_log = get_logger(__name__)

FORCE_DRAIN_MAX_PASSES: Final = 10
_STATUS_ERROR_TYPES: Final = (ApiError, CircuitOpen, TransportFailure, OSError)

type Clock = Callable[[], float]
type Sleeper = Callable[[float], Awaitable[None]]


class ActionSink(Protocol):
    """Bot-facing side effects.

    The signature matches
    :meth:`ctb.delivery.status_card.StatusCards.handle`. A runtime composite may
    also consume ``Notify``, ``SetTopicMarker`` and ``UnbindTopic``.
    """

    async def handle(
        self,
        actions: Sequence[Action],
        *,
        session_id: str,
        chat_id: int,
        thread_id: int = NO_THREAD_ID,
        deep_link: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TickReport:
    """Useful, stable observability for tests and health reporting."""

    session_id: str
    state: TurnState
    messages: int = 0
    deliveries_created: int = 0
    status_requested: bool = False
    workspace_status_requested: bool = False
    cursor_only: bool = False
    stopped: bool = False


class SessionPoller:
    """Adaptive, crash-recoverable poller for exactly one session."""

    def __init__(
        self,
        client: ConductorClient,
        db: Database,
        session_id: str,
        *,
        action_sink: ActionSink | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.client = client
        self.db = db
        self.session_id = session_id
        self.action_sink = action_sink
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._context: TurnContext | None = None
        self._lock = asyncio.Lock()
        self._booted = False
        self._stop_requested = False
        self._working_ticks = 0
        self._tick_messages = 0
        self._tick_deliveries = 0
        self._tick_status = False
        self._tick_workspace_status = False
        self._reposted_this_tick: set[str] = set()

    @property
    def context(self) -> TurnContext | None:
        return self._context

    @property
    def stopped(self) -> bool:
        return self._stop_requested

    def request_stop(self) -> None:
        self._stop_requested = True

    async def run(self) -> None:
        """Poll until unbound, dead, cancelled by the supervisor, or auth-fatal."""
        while not self._stop_requested:
            report = await self.tick()
            if report.stopped:
                return
            context = self._require_context()
            interval = max(0, context.cadence_ms)
            if interval == 0:
                return
            factor = self._rng.uniform(1.0 - JITTER_FRACTION, 1.0 + JITTER_FRACTION)
            await self._sleep((interval / 1000.0) * factor)

    async def tick(self) -> TickReport:
        """Perform one complete tick.

        The transcript drain is unconditional after first-bind seeding. No
        branch based on turn state or status can bypass it.
        """
        async with self._lock:
            return await self._tick_unlocked()

    async def dispatch(self, evidence: Evidence) -> TurnState:
        """Serialize user-driven evidence (notably ``Cancel``) with polling."""
        async with self._lock:
            await self._ensure_context()
            await self._apply_evidence(evidence, reposted=set())
            return self._require_context().state

    async def _tick_unlocked(self) -> TickReport:
        self._reset_tick_counters()
        row = await sessions.get(self.db, self.session_id)
        if row is None or not row.is_bound or row.state is TurnState.DEAD:
            self._stop_requested = True
            state = TurnState.DEAD if row is None else row.state
            return self._report(state)

        if not row.seeded:
            try:
                seek = await cursor.seek_to_end(self.client, self.db, self.session_id)
            except NotFound:
                await self._ensure_context()
                await self._apply_evidence(E404("session"), reposted=set())
                return self._report(self._require_context().state)
            if seek.preview_text:
                await self._emit_external(
                    (
                        Notify(
                            f"Now mirroring: {seek.preview_text}",
                            NotifyLevel.QUIET,
                            once_key="first-bind-preview",
                        ),
                    )
                )

        await self._ensure_context()
        reposted: set[str] = set()
        self._reposted_this_tick = reposted

        if not self._booted:
            self._booted = True
            # The durable prompt ledger is the authority for recovery, not the
            # cached machine state. A crash can leave a pending row while the
            # session still says IDLE or already says QUEUED.
            workspace_status = self._require_context().workspace_status
            if workspace_status is None or workspace_status.is_usable:
                for prompt in await prompts.list_recoverable(
                    self.db, session_id=self.session_id
                ):
                    if prompt.message_id in reposted:
                        continue
                    reposted.add(prompt.message_id)
                    result = await cursor.repost_prompt(
                        self.client,
                        self.db,
                        prompt.message_id,
                        max_attempts=1,
                        sleep=self._sleep,
                        rng=self._rng,
                    )
                    await self._apply_evidence(result.evidence, reposted=reposted)
            await self._apply_evidence(
                Boot(self._require_context().state), reposted=reposted
            )
        else:
            await self._reconcile_new_prompts(reposted)
            if not self._stop_requested:
                drained = await self._drain_once()
                if drained is not None:
                    if drained.delta is not None:
                        await self._apply_evidence(drained.delta, reposted=reposted)
                    else:
                        await self._apply_evidence(
                            Timer(self._clock()), reposted=reposted
                        )
                    await self._emit_card_updates(drained)
            if not self._stop_requested:
                await self._poll_regular_status(reposted)

        return self._report(self._require_context().state)

    async def _ensure_context(self) -> None:
        if self._context is not None:
            return
        now = self._clock()
        loaded = await sessions.load_turn_context(self.db, self.session_id, now=now)
        if loaded is None:
            self._context = TurnContext(
                state=TurnState.DEAD, entered_state_at=now, cadence_ms=0
            )
            self._stop_requested = True
            return
        self._context = loaded

    def _require_context(self) -> TurnContext:
        if self._context is None:  # pragma: no cover - internal invariant
            raise RuntimeError("poller context has not been loaded")
        return self._context

    async def _reconcile_new_prompts(self, reposted: set[str]) -> None:
        """Adopt rows written by bot handlers while this task was sleeping."""
        unsettled = await prompts.list_unsettled(self.db, self.session_id)
        known = {
            prompt.message_id for prompt in self._require_context().pending_prompts
        }
        wall = now_ms()
        now = self._clock()
        for prompt in unsettled:
            if prompt.message_id in known:
                continue
            stamp = prompt.posted_at or prompt.created_at
            pending = PendingPrompt(
                prompt.message_id,
                now - max(0.0, (wall - stamp) / 1000.0),
                prompt.index_at_post,
            )
            self._context = self._require_context().with_prompt(pending)
            await sessions.save_turn_context(
                self.db, self.session_id, self._require_context(), now=now
            )
            evidence: Evidence
            if prompt.state == "posted":
                evidence = PostOk(
                    prompt.message_id,
                    prompt.post_state or "sent",
                    prompt.index_at_post,
                )
            else:
                # Let the machine issue RePost, preserving its transition-3
                # semantics. The per-tick set prevents an ambiguous result from
                # recursively retrying forever in one tick.
                from ctb.turn.state import PostAmbiguous

                evidence = PostAmbiguous(
                    prompt.message_id, prompt.last_error or "recovering saved prompt"
                )
            await self._apply_evidence(evidence, reposted=reposted)
            known.add(prompt.message_id)

    async def _drain_once(self) -> cursor.DrainResult | None:
        try:
            result = await cursor.drain(self.client, self.db, self.session_id)
        except NotFound:
            await self._apply_evidence(
                E404("session"), reposted=self._reposted_this_tick
            )
            return None
        except AuthFatal:
            raise
        except (ConductorError, OSError) as exc:
            _log.warning(
                "poller.messages_unavailable",
                session_id=self.session_id,
                error=type(exc).__name__,
            )
            return None
        self._tick_messages += result.n
        self._tick_deliveries += result.deliveries_created
        return result

    async def _force_drain(self, reposted: set[str]) -> None:
        """Drain to exhaustion before a boot/error conclusion."""
        for _ in range(FORCE_DRAIN_MAX_PASSES):
            result = await self._drain_once()
            if result is None or self._stop_requested:
                return
            if result.delta is not None:
                await self._apply_evidence(result.delta, reposted=reposted)
            await self._emit_card_updates(result)
            if result.exhausted:
                return
        _log.warning("poller.force_drain_budget_exhausted", session_id=self.session_id)

    async def _emit_card_updates(self, drained: cursor.DrainResult) -> None:
        """Chrome the drain produced: what it is doing, and what it has cost.

        One batch, so a tool call and its turn's price cost one card edit.
        Neither can gate anything — the content is already durable and queued.
        """
        updates: list[Action] = []
        if drained.latest_activity:
            updates.append(UpdateActivity(drained.latest_activity))
        if drained.latest_cost_usd is not None:
            updates.append(SetTurnCost(drained.latest_cost_usd))
        if updates:
            await self._emit_external(tuple(updates))

    async def _poll_regular_status(self, reposted: set[str]) -> None:
        context = self._require_context()
        state = context.state
        session_due = False
        workspace_due = False

        if context.cursor_only or state in (
            TurnState.QUEUED,
            TurnState.DRAINING,
            TurnState.CANCELLING,
            TurnState.ERROR,
        ):
            session_due = True
        elif state is TurnState.WORKING:
            self._working_ticks += 1
            session_due = self._working_ticks % 2 == 0
        else:
            self._working_ticks = 0

        # BOOT always asks for workspace state. After that, the workspace
        # endpoint replaces session status only while WAKING.
        workspace_due = state is TurnState.WAKING

        if session_due:
            await self._request_status(reposted)
        if workspace_due and not self._stop_requested:
            await self._request_workspace_status(reposted)

    async def _request_status(self, reposted: set[str]) -> None:
        self._tick_status = True
        try:
            status = await self.client.get_session_status(self.session_id)
        except NotFound:
            await self._apply_evidence(E404("session"), reposted=reposted)
        except AuthFatal:
            raise
        except _STATUS_ERROR_TYPES as exc:
            await self._apply_evidence(
                StatusUnavailable(
                    self._require_context().consecutive_status_failures + 1,
                    type(exc).__name__,
                ),
                reposted=reposted,
            )
        else:
            await sessions.record_status(
                self.db,
                self.session_id,
                status=status.status,
                error_message=status.error_message,
                last_error=status.last_error,
            )
            await self._apply_evidence(
                Status(status.status, status.error_message, status.last_error),
                reposted=reposted,
            )

    async def _request_workspace_status(self, reposted: set[str]) -> None:
        row = await sessions.get(self.db, self.session_id)
        if row is None or row.workspace_id is None:
            return
        self._tick_workspace_status = True
        try:
            status = await self.client.get_workspace_status(row.workspace_id)
        except NotFound:
            await self._apply_evidence(E404("workspace"), reposted=reposted)
        except AuthFatal:
            raise
        except _STATUS_ERROR_TYPES as exc:
            _log.warning(
                "poller.workspace_status_unavailable",
                session_id=self.session_id,
                error=type(exc).__name__,
            )
        else:
            await workspaces.update_status(
                self.db,
                row.workspace_id,
                status=status.status,
                lifecycle_step=status.lifecycle_step,
            )
            await self._apply_evidence(
                Ws(status.status, status.lifecycle_step), reposted=reposted
            )

    async def _apply_evidence(self, evidence: Evidence, *, reposted: set[str]) -> None:
        if self._stop_requested and not isinstance(evidence, E404):
            return
        now = self._clock()
        result = step(self._require_context(), evidence, now)
        self._context = result.context
        # DEAD includes UnbindTopic. Keep routing intact until the composite
        # sink has renamed/detached the actual Telegram topic.
        delayed_save = any(isinstance(action, UnbindTopic) for action in result.actions)
        if not delayed_save:
            await sessions.save_turn_context(
                self.db, self.session_id, result.context, now=now
            )
        if result.transition is not None:
            _log.info(
                "poller.transition",
                session_id=self.session_id,
                transition=result.transition,
                state=result.state.value,
                reason=result.reason,
            )
        await self._execute_actions(result.actions, reposted=reposted)
        if delayed_save:
            await sessions.save_turn_context(
                self.db, self.session_id, self._require_context(), now=now
            )

    async def _execute_actions(
        self, actions: Sequence[Action], *, reposted: set[str]
    ) -> None:
        external: list[Action] = []

        async def flush_external() -> None:
            if not external:
                return
            batch = tuple(external)
            external.clear()
            await self._emit_external(batch)

        for action in actions:
            if isinstance(
                action,
                (ForceDrain, RequestStatus, RequestWorkspaceStatus, RePost, PostCancel),
            ):
                await flush_external()

            match action:
                case ForceDrain():
                    await self._force_drain(reposted)
                case RequestStatus():
                    await self._request_status(reposted)
                case RequestWorkspaceStatus():
                    await self._request_workspace_status(reposted)
                case RePost(message_id=message_id):
                    if message_id in reposted:
                        continue
                    reposted.add(message_id)
                    result = await cursor.repost_prompt(
                        self.client,
                        self.db,
                        message_id,
                        sleep=self._sleep,
                        rng=self._rng,
                    )
                    await self._apply_evidence(result.evidence, reposted=reposted)
                case PostCancel(requested_by=_):
                    await self._post_cancel(reposted)
                case AbandonPrompt(message_id=message_id, reason=reason):
                    await prompts.abandon(self.db, message_id, reason=reason)
                case SetTopicMarker():
                    external.append(action)
                case UnbindTopic():
                    # The composite sink needs the existing workspace/topic
                    # routing to rename/detach Telegram first.
                    external.append(action)
                    await flush_external()
                    await sessions.unbind(self.db, self.session_id)
                    row = await sessions.get(self.db, self.session_id)
                    if row is not None and row.workspace_id is not None:
                        await workspaces.unbind_topic(self.db, row.workspace_id)
                case StopPolling():
                    self._stop_requested = True
                case SetCadence():
                    # The new cadence is already in TurnContext and was saved
                    # before actions began.
                    pass
                case _:
                    external.append(action)
        await flush_external()

    async def _post_cancel(self, reposted: set[str]) -> None:
        try:
            result = await self.client.cancel_session(self.session_id)
        except NotFound:
            await self._apply_evidence(E404("session"), reposted=reposted)
        except AuthFatal:
            raise
        except _STATUS_ERROR_TYPES as exc:
            _log.warning(
                "poller.cancel_unavailable",
                session_id=self.session_id,
                error=type(exc).__name__,
            )
        else:
            await self._apply_evidence(
                CancelAck(result.canceled_queued_messages, result.status),
                reposted=reposted,
            )

    async def _emit_external(self, actions: Sequence[Action]) -> None:
        if not actions or self.action_sink is None:
            return
        row = await sessions.get(self.db, self.session_id)
        if row is None or row.chat_id is None:
            return
        deep_link: str | None = None
        if row.workspace_id is not None:
            workspace = await workspaces.get(self.db, row.workspace_id)
            if workspace is not None:
                deep_link = workspace.deep_link
        try:
            await self.action_sink.handle(
                actions,
                session_id=self.session_id,
                chat_id=row.chat_id,
                thread_id=row.thread_id,
                deep_link=deep_link,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Card/topic/notice failures never get to stall delivery.
            _log.error(
                "poller.action_sink_failed",
                session_id=self.session_id,
                error=repr(exc),
            )
        await self._sync_status_card_id()

    async def _sync_status_card_id(self) -> None:
        row = await sessions.get(self.db, self.session_id)
        if row is not None and self._context is not None:
            self._context = self._context.evolve(
                status_card_msg_id=row.status_card_msg_id
            )

    def _reset_tick_counters(self) -> None:
        self._tick_messages = 0
        self._tick_deliveries = 0
        self._tick_status = False
        self._tick_workspace_status = False

    def _report(self, state: TurnState) -> TickReport:
        context = self._context
        return TickReport(
            session_id=self.session_id,
            state=state,
            messages=self._tick_messages,
            deliveries_created=self._tick_deliveries,
            status_requested=self._tick_status,
            workspace_status_requested=self._tick_workspace_status,
            cursor_only=False if context is None else context.cursor_only,
            stopped=self._stop_requested,
        )
