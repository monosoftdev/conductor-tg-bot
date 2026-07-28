"""A scripted, in-memory Conductor API — what Phase 1 correctness is proven against.

``docs/PLAN.md``: *"The pure state machine tested against scripted
``fake_conductor`` sequences is where the reliability actually comes from."*
So this file has to be trustworthy, and every shape in it is copied from the
Phase 0 probe (``probe-out/shape_report.md``, ``probe-out/transcripts.jsonl``)
rather than guessed. ``tests/fakes/test_fake_selfcheck.py`` re-asserts the
load-bearing properties — a lying fake would invalidate every test built on it.

Two ways to drive it
--------------------

**As an httpx transport** — point the real client at it, unmodified::

    fake = FakeConductor()
    session = fake.add_session(script=[Tick(SessionStatusValue.WORKING)])
    async with fake.client() as http:                 # UA + bearer preset
        r = await http.post(
            f"/sessions/{session.session_id}/messages",
            json={"message": "hi", "messageId": "abc"},
        )
        page = MessagesPage.model_validate(
            (await http.get(f"/sessions/{session.session_id}/messages")).json()
        )

    # or wire it into a client you build yourself:
    httpx.AsyncClient(transport=fake.transport(), base_url=fake.base_url)

**As a direct object** — one call per poller tick, returning the exact evidence a
poller would derive, with no HTTP in the way. This is what ``test_machine.py``
wants::

    scenario = queued_idle_trap()
    scenario.session.post("do the thing", message_id=scenario.prompt_ids[0])
    for _ in range(7):
        poll = scenario.session.poll()
        for evidence in poll.evidence():
            result = step(result.context, evidence, clock())

Use one form or the other per session: the HTTP cursor (``after=``) and the
direct-poll cursor are deliberately independent.

The timeline
------------

A session owns a ``script``: a tuple of :class:`Tick`. A tick says what
``GET /status`` reports and which transcript messages become visible at that
moment. The timeline advances per call according to :class:`Advance` (default
:attr:`Advance.ANY`: the first GET of each poll advances, a second GET of the
*same kind* starts the next tick — so ``/messages`` + ``/status`` in either
order is exactly one tick). Polling past the end repeats the final tick without
re-emitting its messages, which is how ``error persists indefinitely`` is
modelled.

Measured behaviours reproduced here (do not "fix" these)
--------------------------------------------------------

* Envelope ``id`` is the server-assigned composite ``"<sessionId>:<seq>:<sub>"``.
  Our POSTed ``messageId`` is **not** it: it surfaces as ``content.id`` on the
  user echo and as ``content.turnId`` on *every* message of the turn.
* ``sessionIndex`` **has gaps** (the probe saw ``0,2,3,4,5,6,7``). A test that
  assumes gapless would pass against a naive fake and fail in production.
* ``after=<garbage>`` and ``after=<id from another session>`` both return
  **HTTP 404 with zero messages** — no silent full replay.
* Re-POSTing the same ``messageId`` **dedupes**: 201 with the same id, and
  exactly one user echo in the transcript.
* ``error`` is a third session status and persists indefinitely while the
  session still accepts POSTs.
* ``GET /me`` is at the API **root**; ``/v0/me`` 404s.
* ``POST /v0/sessions`` accepts a caller-supplied ``sessionId``;
  ``POST /v0/workspaces`` has **no** idempotency key and creates a second
  workspace on a blind retry.
* A missing or default (``python-httpx/…``) ``User-Agent`` is rejected with 403,
  like the proxy in front of the real API.

One field is *not* verified: the ``offset`` in a ``/messages`` response. The
probe never depended on it, so this fake returns the **next** offset
(``start + len(data)``) and callers must page with ``offset += len(data)``
rather than trusting it.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from uuid import UUID, uuid5

import httpx

from ctb import USER_AGENT
from ctb.conductor.errors import RETRYABLE_STATUSES, api_error_for_status
from ctb.conductor.models import (
    CancelResult,
    MessagesPage,
    PostMessageResult,
    PostState,
    Session,
    SessionStatus,
    SessionStatusValue,
    TranscriptMessage,
    WorkspaceCreateResult,
    WorkspaceStatus,
    WorkspaceStatusValue,
)
from ctb.turn.state import Delta, Evidence, Status, StatusUnavailable, Ws

__all__ = [
    "Advance",
    "Call",
    "FakeConductor",
    "FakeSession",
    "FakeWorkspace",
    "Kind",
    "Msg",
    "PollResult",
    "PostFailure",
    "SCENARIOS",
    "Scenario",
    "Tick",
    "assistant",
    "cancelled_turn",
    "delta_for",
    "double_prompt",
    "error_mid_turn",
    "error_result",
    "fast_turn",
    "queued_idle_trap",
    "rate_limit",
    "replay_attack",
    "result",
    "slow_wake",
    "state_changed",
    "status_5xx",
    "status_flapping",
    "system_init",
    "tool_result",
    "tool_use",
    "unknown_event",
    "user_message",
]

#: Deterministic id generation: the same scenario yields the same ids on every
#: run, so a failing test can be pasted into an issue verbatim.
_NS: Final = UUID("6f1d5a3e-0b3a-4a4f-8a0e-2d9d3f0a1b77")

_BASE_URL: Final = "https://api.conductor.build/v0"
_ROOT_URL: Final = "https://api.conductor.build"

#: The probe's first message landed at this instant; receivedAt walks forward
#: from here so timestamps are stable and ordered.
_EPOCH: Final = datetime(2026, 7, 26, 2, 0, 36, 698_000, tzinfo=UTC)
_STAMP_STEP: Final = timedelta(milliseconds=347)

#: User agents the proxy in front of the real API is documented to reject.
_BLOCKED_UA: Final = ("python-httpx", "python-requests", "python-urllib", "urllib")

_MAX_LIMIT: Final = 500
_SQL_MAX_CHARS: Final = 10_000
_SQL_MAX_ROWS: Final = 500


def _uid(*parts: str) -> str:
    return str(uuid5(_NS, ":".join(parts)))


# ── message specs ────────────────────────────────────────────────────────────


class Kind(StrEnum):
    """Which verified envelope shape to emit.

    ``TOOL_RESULT`` and ``UNKNOWN`` were **not** observed in the Phase 0 probe
    (the org had no tool-using transcripts). They are plausible reconstructions
    kept here so the renderer's fallback paths have something to chew on; do not
    treat them as measured.
    """

    USER_MESSAGE = "userMessage"
    ASSISTANT = "assistant"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    ERROR_RESULT = "error_result"
    SYSTEM_INIT = "system_init"
    STATE_CHANGED = "state_changed"
    RATE_LIMIT = "rate_limit"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Msg:
    """One transcript message to emit, before it is given an id and an index.

    ``prompt`` is which POSTed prompt this message belongs to: prompt ``0`` is
    the first prompt POSTed to the session, ``1`` the second. It resolves to
    that prompt's ``messageId`` — which is what ``content.turnId`` carries — so
    a script written before the test knows any ids still produces correct turn
    attribution.
    """

    kind: Kind = Kind.ASSISTANT
    text: str = "ok"
    prompt: int = 0
    tool: str = "Bash"
    tool_input: Mapping[str, Any] = field(default_factory=dict)
    is_error: bool = False
    #: For :attr:`Kind.UNKNOWN`: the ``rawPayload.type`` to claim.
    raw_type: str = "some_future_event"


def user_message(text: str = "do the thing", *, prompt: int = 0) -> Msg:
    """A user echo. Normally emitted automatically by a POST, not scripted."""
    return Msg(kind=Kind.USER_MESSAGE, text=text, prompt=prompt)


def assistant(text: str = "ok", *, prompt: int = 0) -> Msg:
    """Assistant prose — the primary content the chat must show."""
    return Msg(kind=Kind.ASSISTANT, text=text, prompt=prompt)


def tool_use(
    tool: str = "Bash",
    *,
    text: str = "",
    prompt: int = 0,
    tool_input: Mapping[str, Any] | None = None,
) -> Msg:
    """An assistant message whose block list contains a ``tool_use`` block."""
    return Msg(
        kind=Kind.TOOL_USE,
        text=text,
        prompt=prompt,
        tool=tool,
        tool_input=dict(tool_input or {"command": "pytest -q"}),
    )


def tool_result(text: str = "ok", *, prompt: int = 0, is_error: bool = False) -> Msg:
    """SYNTHETIC (not probe-verified): a tool result turn-back."""
    return Msg(kind=Kind.TOOL_RESULT, text=text, prompt=prompt, is_error=is_error)


def result(text: str = "done", *, prompt: int = 0) -> Msg:
    """The terminal ``result`` payload of a successful turn."""
    return Msg(kind=Kind.RESULT, text=text, prompt=prompt)


def error_result(text: str = "the turn failed", *, prompt: int = 0) -> Msg:
    """A ``result`` payload with ``is_error: true``."""
    return Msg(kind=Kind.ERROR_RESULT, text=text, prompt=prompt, is_error=True)


def system_init(*, prompt: int = 0) -> Msg:
    """``rawPayload.type == "system"``, ``subtype == "init"``."""
    return Msg(kind=Kind.SYSTEM_INIT, text="", prompt=prompt)


def state_changed(state: str = "running", *, prompt: int = 0) -> Msg:
    """``system`` / ``session_state_changed`` — the first sign of life."""
    return Msg(kind=Kind.STATE_CHANGED, text=state, prompt=prompt)


def rate_limit(*, prompt: int = 0) -> Msg:
    """The ``rate_limit_event`` the probe saw (``out_of_credits`` overage)."""
    return Msg(kind=Kind.RATE_LIMIT, text="", prompt=prompt)


def unknown_event(raw_type: str = "some_future_event", *, prompt: int = 0) -> Msg:
    """A payload type this codebase has never seen. Must not crash anything."""
    return Msg(kind=Kind.UNKNOWN, text="", prompt=prompt, raw_type=raw_type)


# ── the timeline ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Tick:
    """What one poll of the session observes.

    ``status=None`` carries the previous tick's status forward, so a long script
    only states what changes.
    """

    status: SessionStatusValue | None = None
    error_message: str | None = None
    last_error: str | None = None
    #: When set, ``GET /status`` fails with this HTTP status instead of
    #: answering. This is how cursor-only mode gets exercised.
    status_http: int | None = None
    retry_after: float | None = None
    #: When set, the workspace's status flips to this as the tick begins.
    workspace: WorkspaceStatusValue | None = None
    lifecycle_step: str | None = None
    #: Messages that become visible in the transcript as this tick begins.
    emit: tuple[Msg, ...] = ()
    label: str = ""


class Advance(StrEnum):
    """When the scripted timeline moves on."""

    #: The first GET of a poll advances; a repeat of the *same* endpoint kind
    #: begins the next tick. ``/messages`` + session ``/status`` + workspace
    #: ``/status``, in any order, is one tick. Caveat: a poller that pages
    #: ``/messages`` twice inside one tick will advance twice — use
    #: :attr:`MANUAL` there and call :meth:`FakeSession.tick` yourself.
    ANY = "any"
    #: Every status GET advances — session *and* workspace, so a tick that
    #: fetches both advances twice.
    STATUS = "status"
    #: Only ``GET /messages`` advances.
    MESSAGES = "messages"
    #: Nothing advances except :meth:`FakeSession.tick` / :meth:`FakeSession.poll`.
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class PostFailure:
    """A scripted failure for the next POST.

    ``landed=True`` is the interesting one: the write took effect server-side
    and *then* the response was lost. That is the exact shape of ``Ambiguous``,
    and the reason the idempotency key exists.
    """

    status: int | None = None
    exc: BaseException | None = None
    landed: bool = False


@dataclass(frozen=True, slots=True)
class Call:
    """One request the fake handled, for assertions about call patterns."""

    method: str
    path: str
    params: dict[str, str]
    body: dict[str, Any] | None
    status: int
    error: str | None = None


class _Injected(Exception):
    """Internal: carries a scripted transport exception past the recorder."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(repr(exc))
        self.exc = exc


def delta_for(messages: Sequence[TranscriptMessage]) -> Delta | None:
    """Derive the :class:`~ctb.turn.state.Delta` a poller would build.

    ``None`` for an empty page — a tick with no new messages is not a delta.
    """
    if not messages:
        return None
    turn_ids = {m.turn_id for m in messages if m.turn_id}
    witnessed = {m.content_id for m in messages if m.is_user_echo and m.content_id}
    tool_calls = sum(
        1 for m in messages for block in m.blocks if block.get("type") == "tool_use"
    )
    has_agent_content = any(
        m.is_agent and m.raw_payload_type in ("assistant", "result", "user")
        for m in messages
    )
    return Delta(
        n=len(messages),
        max_index=max(m.session_index for m in messages),
        has_agent_content=has_agent_content,
        turn_ids=frozenset(turn_ids),
        witnessed_prompt_ids=frozenset(witnessed),
        tool_calls=tool_calls,
        has_error_result=any(m.is_result and m.is_error for m in messages),
    )


@dataclass(frozen=True, slots=True)
class PollResult:
    """One direct poll: the evidence a poller would hand the state machine."""

    tick_index: int
    label: str
    messages: tuple[TranscriptMessage, ...]
    delta: Delta | None
    status: Status | None
    status_unavailable: StatusUnavailable | None
    ws: Ws | None

    def evidence(self) -> tuple[Evidence, ...]:
        """Delta first — the cursor is the source of truth, status only a hint."""
        out: list[Evidence] = []
        if self.delta is not None:
            out.append(self.delta)
        if self.ws is not None:
            out.append(self.ws)
        if self.status is not None:
            out.append(self.status)
        if self.status_unavailable is not None:
            out.append(self.status_unavailable)
        return tuple(out)

    @property
    def texts(self) -> tuple[str, ...]:
        """Assistant prose delivered on this poll, for readable assertions."""
        out: list[str] = []
        for message in self.messages:
            for block in message.blocks:
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        out.append(text)
        return tuple(out)


# ── workspace ────────────────────────────────────────────────────────────────


class FakeWorkspace:
    """A workspace row. Status is driven by :attr:`Tick.workspace`."""

    def __init__(
        self,
        workspace_id: str,
        *,
        name: str,
        project_id: str,
        status: WorkspaceStatusValue = WorkspaceStatusValue.READY,
        lifecycle_step: str | None = None,
        repo_url: str = "https://github.com/example/repo.git",
        branch: str = "main",
        agent: str = "claude",
        model: str = "opus-5-1m",
        effort: str = "high",
    ) -> None:
        self.id = workspace_id
        self.name = name
        self.project_id = project_id
        self.status = status
        self.lifecycle_step = lifecycle_step
        self.repo_url = repo_url
        self.branch = branch
        self.agent = agent
        self.model = model
        self.effort = effort
        self.deep_link = f"conductor://workspace/{workspace_id}"
        self.rename_count = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "projectId": self.project_id,
            "repositoryUrl": self.repo_url,
            "branch": self.branch,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "deepLink": self.deep_link,
            "status": str(self.status),
            "lifecycleStep": self.lifecycle_step,
            "createdAt": "2026-07-26 01:00:00.000+00",
        }

    def status_json(self) -> dict[str, Any]:
        return {
            "workspaceId": self.id,
            "status": str(self.status),
            "lifecycleStep": self.lifecycle_step,
        }

    def status_model(self) -> WorkspaceStatus:
        """``GET /workspaces/{id}/status`` without HTTP."""
        return WorkspaceStatus.model_validate(self.status_json())


# ── session ──────────────────────────────────────────────────────────────────


class FakeSession:
    """One scripted session: a transcript, a status timeline, and a POST inbox."""

    def __init__(
        self,
        session_id: str,
        *,
        workspace: FakeWorkspace,
        script: Sequence[Tick] = (),
        advance: Advance = Advance.ANY,
        seed: Sequence[Msg] = (),
        title: str = "fake session",
        agent: str = "claude",
        model: str = "sonnet",
        effort: str = "high",
        model_full: str = "claude-sonnet-4-6",
        auto_echo: bool = True,
        post_state: PostState = PostState.SENT,
        replay_after: bool = False,
        index_gaps: bool = True,
        default_limit: int = 50,
        cancel_queued: int = 0,
        cancel_status: str = "canceling",
        idle_ticks_after_cancel: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.workspace = workspace
        self.workspace_id = workspace.id
        self.script: tuple[Tick, ...] = tuple(script)
        self.advance = advance
        self.title = title
        self.agent = agent
        self.model = model
        self.effort = effort
        self.model_full = model_full
        #: Emit a user echo into the transcript on the first POST of an id.
        self.auto_echo = auto_echo
        self.post_state = post_state
        #: The replay attack: ``after=`` is ignored and the whole transcript
        #: comes back. The ``sessionIndex`` filter must drop all of it.
        self.replay_after = replay_after
        self.index_gaps = index_gaps
        self.default_limit = default_limit
        self.cancel_queued = cancel_queued
        self.cancel_status = cancel_status
        #: After a cancel, keep reporting ``working`` for this many *further*
        #: ticks and then ``idle``, overriding the script — cancel is
        #: asynchronous. ``None`` leaves the script in charge.
        self.idle_ticks_after_cancel = idle_ticks_after_cancel

        self._messages: list[dict[str, Any]] = []
        self._index_of: dict[str, int] = {}
        self._next_free_index = 0
        self._assigned = 0
        self._stamps = 0

        self._tick_index = 0
        self._started = False
        self._applied: set[int] = set()
        self._seen_kinds: set[str] = set()

        self._posted: dict[str, str] = {}
        self._posted_order: list[str] = []
        self._post_failures: deque[PostFailure] = deque()
        self._extra_prompt_ids: dict[int, str] = {}
        self._direct_pos = 0
        self._status_failures = 0
        self._canceled_at_tick: int | None = None

        #: Duplicate POSTs of an already-known ``messageId``. The dedup test
        #: asserts this went up while the echo count did not.
        self.duplicate_posts = 0
        self.cancel_count = 0

        n_prompts = 1 + max(
            (m.prompt for tick in self.script for m in tick.emit),
            default=0,
        )
        n_prompts = max(n_prompts, 1 + max((m.prompt for m in seed), default=0))
        #: The ``messageId`` values a test should POST, in order. Preassigned so
        #: a script can reference turns before anything has been posted; if a
        #: test POSTs a different id, that id wins for that slot.
        self.prompt_ids: tuple[str, ...] = tuple(
            _uid(session_id, "prompt", str(i)) for i in range(n_prompts)
        )

        for spec in seed:
            self._emit(spec)

    # -- identity -------------------------------------------------------------

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "workspaceId": self.workspace_id,
            "title": self.title,
            "name": self.title,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "fastMode": False,
            "createdAt": "2026-07-26 01:00:00.000+00",
        }

    def turn_id_for(self, prompt_index: int) -> str:
        """The ``messageId`` that owns prompt slot ``prompt_index``.

        The actually-POSTed id if there is one, else the preassigned fallback.
        """
        if 0 <= prompt_index < len(self._posted_order):
            return self._posted_order[prompt_index]
        if 0 <= prompt_index < len(self.prompt_ids):
            return self.prompt_ids[prompt_index]
        return self._extra_prompt_ids.setdefault(
            prompt_index, _uid(self.session_id, "prompt", str(prompt_index))
        )

    @property
    def posted_ids(self) -> tuple[str, ...]:
        """Distinct ``messageId`` values accepted, in POST order."""
        return tuple(self._posted_order)

    # -- transcript -----------------------------------------------------------

    @property
    def transcript(self) -> tuple[dict[str, Any], ...]:
        """Every visible envelope, as the wire JSON."""
        return tuple(self._messages)

    def messages_model(self) -> tuple[TranscriptMessage, ...]:
        return tuple(TranscriptMessage.model_validate(m) for m in self._messages)

    def echo_count(self, message_id: str) -> int:
        """User echoes carrying ``content.id == message_id``. Dedup asserts 1."""
        return sum(
            1
            for m in self._messages
            if m["type"] == "userMessage" and m["content"].get("id") == message_id
        )

    def _stamp(self) -> str:
        moment = _EPOCH + _STAMP_STEP * self._stamps
        self._stamps += 1
        return f"{moment:%Y-%m-%d %H:%M:%S}.{moment.microsecond // 1000:03d}+00"

    def _next_index(self) -> int:
        """Allocate a ``sessionIndex``. **Deliberately not gapless.**

        The live API produced ``0, 2, 3, 4, 5, 6, 7`` for one turn. Anything
        that infers message loss from a gap is wrong, and this reproduces the
        gap so such code fails here rather than in production.
        """
        index = self._next_free_index
        self._assigned += 1
        self._next_free_index = index + 1
        if self.index_gaps and self._assigned % 5 == 1:
            self._next_free_index += 1
        return index

    def _emit(self, spec: Msg, turn_id: str | None = None) -> dict[str, Any]:
        index = self._next_index()
        seq = index + 1
        envelope_id = f"{self.session_id}:{seq}:0"
        turn = turn_id if turn_id is not None else self.turn_id_for(spec.prompt)
        envelope_type, content = self._content_for(spec, turn, envelope_id, seq)
        envelope: dict[str, Any] = {
            "id": envelope_id,
            "sessionId": self.session_id,
            "sessionIndex": index,
            "type": envelope_type,
            "content": content,
            "receivedAt": self._stamp(),
        }
        self._index_of[envelope_id] = len(self._messages)
        self._messages.append(envelope)
        return envelope

    def _content_for(
        self, spec: Msg, turn_id: str, envelope_id: str, seq: int
    ) -> tuple[str, dict[str, Any]]:
        if spec.kind is Kind.USER_MESSAGE:
            return "userMessage", {
                "eventId": envelope_id,
                "type": "userMessage",
                "senderId": _uid(self.session_id, "sender"),
                "senderApiKeyName": "fake-api-key",
                "id": turn_id,
                "message": spec.text,
                "state": "sent",
                "turnId": turn_id,
                "config": {
                    "collaborationMode": "default",
                    "model": self.model,
                    "thinkingLevel": "none",
                },
            }
        return "agent", {
            "eventId": envelope_id,
            "type": "agent",
            "rawPayload": self._raw_payload(spec, seq),
            "userMessageId": turn_id,
            "turnId": turn_id,
        }

    def _raw_payload(self, spec: Msg, seq: int) -> dict[str, Any]:
        uuid_value = _uid(self.session_id, "event", str(seq))
        base: dict[str, Any] = {
            "session_id": self.session_id,
            "uuid": uuid_value,
        }
        match spec.kind:
            case Kind.ASSISTANT | Kind.TOOL_USE:
                blocks: list[dict[str, Any]] = []
                if spec.text:
                    blocks.append({"type": "text", "text": spec.text})
                if spec.kind is Kind.TOOL_USE:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": f"toolu_{seq:04d}",
                            "name": spec.tool,
                            "input": dict(spec.tool_input),
                        }
                    )
                return base | {
                    "type": "assistant",
                    "message": {
                        "id": f"msg_{seq:04d}",
                        "type": "message",
                        "role": "assistant",
                        "model": self.model_full,
                        "content": blocks,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "stop_details": None,
                        "context_management": None,
                        "diagnostics": None,
                        "usage": {"input_tokens": 12, "output_tokens": 34},
                    },
                    "parent_tool_use_id": None,
                    "request_id": f"req_{seq:04d}",
                }
            case Kind.TOOL_RESULT:
                return base | {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"toolu_{seq:04d}",
                                "content": spec.text,
                                "is_error": spec.is_error,
                            }
                        ],
                    },
                    "parent_tool_use_id": None,
                }
            case Kind.RESULT | Kind.ERROR_RESULT:
                is_error = spec.kind is Kind.ERROR_RESULT or spec.is_error
                return base | {
                    "type": "result",
                    "subtype": "error" if is_error else "success",
                    "is_error": is_error,
                    "result": spec.text,
                    "stop_reason": "error" if is_error else "end_turn",
                    "num_turns": 1,
                    "duration_ms": 6_320,
                    "duration_api_ms": 6_100,
                    "total_cost_usd": 0.0123,
                    "api_error_status": None,
                    "permission_denials": [],
                    "fast_mode_state": "off",
                    "terminal_reason": "completed",
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 45,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "service_tier": "standard",
                    },
                }
            case Kind.SYSTEM_INIT:
                return base | {
                    "type": "system",
                    "subtype": "init",
                    "cwd": "/home/agent/repo",
                    "tools": ["Bash", "Read", "Edit", "Write"],
                    "mcp_servers": [{"name": "conductor", "status": "connected"}],
                    "model": self.model_full,
                    "permissionMode": "bypassPermissions",
                    "slash_commands": [],
                    "apiKeySource": "none",
                    "claude_code_version": "2.1.201",
                    "output_style": "default",
                    "agents": ["claude"],
                    "skills": [],
                    "plugins": [],
                    "analytics_disabled": False,
                    "product_feedback_disabled": False,
                    "memory_paths": {"auto": "/home/vercel-sandbox/.claude/memory/"},
                    "fast_mode_state": "off",
                }
            case Kind.STATE_CHANGED:
                return base | {
                    "type": "system",
                    "subtype": "session_state_changed",
                    "state": spec.text or "running",
                }
            case Kind.RATE_LIMIT:
                return base | {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "allowed",
                        "resetsAt": 1_785_039_000,
                        "rateLimitType": "five_hour",
                        "overageStatus": "rejected",
                        "overageDisabledReason": "out_of_credits",
                        "isUsingOverage": False,
                    },
                }
            case _:
                return base | {"type": spec.raw_type, "payload": spec.text}

    # -- timeline -------------------------------------------------------------

    @property
    def tick_index(self) -> int:
        """Which tick is current. ``-1`` before the first advance."""
        return self._tick_index if self._started else -1

    @property
    def current_tick(self) -> Tick:
        if not self.script:
            return Tick()
        if not self._started:
            return Tick()
        return self.script[self._tick_index]

    def tick(self) -> Tick:
        """Advance the timeline one step and apply that tick's emissions.

        Past the end of the script the final tick repeats without re-emitting,
        which is how "the error status persists indefinitely" is modelled.
        """
        if not self.script:
            self._started = True
            self._seen_kinds.clear()
            return Tick()
        if not self._started:
            self._started = True
            self._tick_index = 0
        else:
            self._tick_index = min(self._tick_index + 1, len(self.script) - 1)
        self._seen_kinds.clear()
        current = self.script[self._tick_index]
        if self._tick_index not in self._applied:
            self._applied.add(self._tick_index)
            for spec in current.emit:
                self._emit(spec)
            if current.workspace is not None:
                self.workspace.status = current.workspace
            if current.lifecycle_step is not None:
                self.workspace.lifecycle_step = current.lifecycle_step
        return current

    def _maybe_advance(self, kind: str) -> None:
        """Advance per :class:`Advance` for an incoming request of ``kind``."""
        match self.advance:
            case Advance.MANUAL:
                return
            case Advance.STATUS if kind not in ("status", "ws"):
                return
            case Advance.MESSAGES if kind != "messages":
                return
            case Advance.ANY if self._started and kind not in self._seen_kinds:
                self._seen_kinds.add(kind)
                return
        self.tick()
        self._seen_kinds.add(kind)

    def _status_value(self) -> SessionStatusValue:
        """The scripted status, carrying the last stated value forward."""
        if not self._started or not self.script:
            return SessionStatusValue.IDLE
        for i in range(self._tick_index, -1, -1):
            value = self.script[i].status
            if value is not None:
                return value
        return SessionStatusValue.IDLE

    def _cancel_override(self) -> SessionStatusValue | None:
        if self._canceled_at_tick is None or self.idle_ticks_after_cancel is None:
            return None
        elapsed = self._tick_index - self._canceled_at_tick
        if elapsed > self.idle_ticks_after_cancel:
            return SessionStatusValue.IDLE
        return SessionStatusValue.WORKING

    def status_json(self) -> dict[str, Any]:
        tick = self.current_tick
        value = self._cancel_override() or self._status_value()
        return {
            "sessionId": self.session_id,
            "workspaceId": self.workspace_id,
            "status": str(value),
            "errorMessage": tick.error_message,
            "lastError": tick.last_error or tick.error_message,
            "lastErrorAt": "2026-07-26 02:01:00.000+00" if tick.error_message else None,
        }

    # -- writes ---------------------------------------------------------------

    def fail_next_post(self, failure: PostFailure) -> None:
        """Queue a scripted failure for the next ``POST .../messages``."""
        self._post_failures.append(failure)

    def _register_post(self, message_id: str, text: str) -> bool:
        """Record a prompt. Returns ``True`` if it was new (and echoed)."""
        if message_id in self._posted:
            self.duplicate_posts += 1
            return False
        self._posted[message_id] = text
        self._posted_order.append(message_id)
        if self.auto_echo:
            self._emit(Msg(kind=Kind.USER_MESSAGE, text=text), turn_id=message_id)
        return True

    def _post_json(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        raw_id = body.get("messageId")
        message_id = (
            raw_id
            if isinstance(raw_id, str) and raw_id
            else _uid(self.session_id, "server-generated", str(len(self._posted_order)))
        )
        text = body.get("message")
        text = text if isinstance(text, str) else ""
        if self._post_failures:
            failure = self._post_failures.popleft()
            if failure.landed:
                self._register_post(message_id, text)
            if failure.exc is not None:
                raise _Injected(failure.exc)
            status = failure.status or 500
            return status, {
                "code": "server_error",
                "userMessage": "scripted POST failure",
                "retryable": status in RETRYABLE_STATUSES,
            }
        self._register_post(message_id, text)
        return 201, {"messageId": message_id, "state": str(self.post_state)}

    def _cancel_json(self) -> dict[str, Any]:
        self.cancel_count += 1
        self._canceled_at_tick = self._tick_index
        return {
            "status": self.cancel_status,
            "canceledQueuedMessages": self.cancel_queued,
        }

    # -- reads ----------------------------------------------------------------

    def _messages_json(
        self,
        *,
        after: str | None,
        offset: int | None,
        limit: int | None,
    ) -> tuple[int, dict[str, Any]]:
        if after is not None and offset is not None:
            return 400, _error(
                "invalid_request", "after cannot be combined with offset"
            )
        page_size = self.default_limit if limit is None else limit
        page_size = max(1, min(page_size, _MAX_LIMIT))

        start = 0
        if after is not None and not self.replay_after:
            position = self._index_of.get(after)
            if position is None:
                # Measured: an unknown or foreign `after` is a 404 with zero
                # messages. There is no silent full replay.
                return 404, _error(
                    "not_found",
                    f"message {after} not found in session {self.session_id}",
                ) | {"data": [], "offset": None, "hasMore": False}
            start = position + 1
        elif after is None and offset is not None:
            start = max(0, offset)

        window = self._messages[start : start + page_size]
        return 200, {
            "data": [json.loads(json.dumps(m)) for m in window],
            # NOT verified against the live API — page with `offset += len(data)`.
            "offset": start + len(window),
            "hasMore": start + len(window) < len(self._messages),
        }

    # -- direct (no-HTTP) driving --------------------------------------------

    def post(
        self, message: str = "do the thing", *, message_id: str | None = None
    ) -> PostMessageResult:
        """``POST /sessions/{id}/messages`` without HTTP.

        Raises the scripted exception, or an :class:`~ctb.conductor.errors.ApiError`,
        when a failure is queued.
        """
        try:
            status, payload = self._post_json(
                {"message": message, "messageId": message_id}
            )
        except _Injected as injected:
            raise injected.exc from None
        if status >= 400:
            raise api_error_for_status(
                status,
                payload,
                method="POST",
                path=f"/sessions/{self.session_id}/messages",
            )
        return PostMessageResult.model_validate(payload)

    def cancel(self) -> CancelResult:
        """``POST /sessions/{id}/cancel`` without HTTP."""
        return CancelResult.model_validate(self._cancel_json())

    def status(self) -> SessionStatus:
        """The current scripted status. Does **not** advance the timeline."""
        return SessionStatus.model_validate(self.status_json())

    def messages(
        self,
        *,
        after: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> MessagesPage:
        """``GET /sessions/{id}/messages`` without HTTP. 404 raises ``NotFound``."""
        status, payload = self._messages_json(after=after, offset=offset, limit=limit)
        if status >= 400:
            raise api_error_for_status(
                status,
                payload,
                method="GET",
                path=f"/sessions/{self.session_id}/messages",
            )
        return MessagesPage.model_validate(payload)

    def poll(self) -> PollResult:
        """Advance one tick and return the evidence a poller would derive.

        Uses its own cursor, independent of the HTTP ``after=`` cursor: drive a
        session either directly or over HTTP, not both.
        """
        current = self.tick()
        fresh = tuple(
            TranscriptMessage.model_validate(m)
            for m in self._messages[self._direct_pos :]
        )
        self._direct_pos = len(self._messages)

        status: Status | None = None
        unavailable: StatusUnavailable | None = None
        if current.status_http is not None:
            self._status_failures += 1
            unavailable = StatusUnavailable(
                consecutive_failures=self._status_failures,
                reason=f"HTTP {current.status_http}",
            )
        else:
            self._status_failures = 0
            payload = self.status_json()
            status = Status(
                value=SessionStatusValue(payload["status"]),
                error_message=payload["errorMessage"],
                last_error=payload["lastError"],
            )
        ws = (
            Ws(
                status=self.workspace.status,
                lifecycle_step=self.workspace.lifecycle_step,
            )
            if current.workspace is not None
            else None
        )
        return PollResult(
            tick_index=self._tick_index,
            label=current.label,
            messages=fresh,
            delta=delta_for(fresh),
            status=status,
            status_unavailable=unavailable,
            ws=ws,
        )


# ── the server ───────────────────────────────────────────────────────────────

_RE_PROJECT_WS = re.compile(r"^/projects/([^/]+)/workspaces$")
_RE_WORKSPACE = re.compile(r"^/workspaces/([^/]+)$")
_RE_WORKSPACE_STATUS = re.compile(r"^/workspaces/([^/]+)/status$")
_RE_WORKSPACE_SESSIONS = re.compile(r"^/workspaces/([^/]+)/sessions$")
_RE_WORKSPACE_RENAME = re.compile(r"^/workspaces/([^/]+)/rename$")
_RE_WORKSPACE_ARCHIVE = re.compile(r"^/workspaces/([^/]+)/archive$")
_RE_SESSION_MESSAGES = re.compile(r"^/sessions/([^/]+)/messages$")
_RE_SESSION_STATUS = re.compile(r"^/sessions/([^/]+)/status$")
_RE_SESSION_CANCEL = re.compile(r"^/sessions/([^/]+)/cancel$")
_RE_MESSAGE = re.compile(r"^/messages/(.+)$")


def _error(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "code": code,
        "userMessage": message,
        "debugMessage": message,
        "retryable": retryable,
        "source": "fake",
    }


class FakeConductor:
    """An in-memory Conductor API. Drive it over httpx or object-to-object."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        require_auth: bool = True,
        require_user_agent: bool = True,
        base_url: str = _BASE_URL,
        root_url: str = _ROOT_URL,
    ) -> None:
        #: When set, the bearer token must match exactly.
        self.api_key = api_key
        self.require_auth = require_auth
        #: The real API is behind a proxy that 403s default client signatures.
        self.require_user_agent = require_user_agent
        self.base_url = base_url
        #: ``GET /me`` is at the root, not under ``/v0`` — verified.
        self.root_url = root_url
        self.projects: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[str, FakeWorkspace] = {}
        self.sessions: dict[str, FakeSession] = {}
        self.calls: list[Call] = []
        #: ``POST /v0/workspaces`` has no idempotency key, so every accepted
        #: call appends here — including a blind retry's duplicate.
        self.created_workspace_names: list[str] = []
        self._create_failures: deque[PostFailure] = deque()
        self._counter = 0

    # -- construction ---------------------------------------------------------

    def add_project(
        self,
        name: str = "example",
        *,
        project_id: str | None = None,
        git_remote: str = "https://github.com/example/repo.git",
    ) -> str:
        pid = project_id or _uid("project", name)
        self.projects[pid] = {"id": pid, "name": name, "gitRemote": git_remote}
        return pid

    def add_workspace(
        self,
        name: str = "api/fix-flaky",
        *,
        workspace_id: str | None = None,
        project_id: str | None = None,
        status: WorkspaceStatusValue = WorkspaceStatusValue.READY,
        lifecycle_step: str | None = None,
    ) -> FakeWorkspace:
        pid = project_id or next(iter(self.projects), None) or self.add_project()
        wid = workspace_id or _uid("workspace", name, str(len(self.workspaces)))
        workspace = FakeWorkspace(
            wid,
            name=name,
            project_id=pid,
            status=status,
            lifecycle_step=lifecycle_step,
        )
        self.workspaces[wid] = workspace
        return workspace

    def add_session(
        self,
        *,
        session_id: str | None = None,
        workspace: FakeWorkspace | None = None,
        script: Sequence[Tick] = (),
        **options: Any,
    ) -> FakeSession:
        """Create a scripted session (and a workspace for it if needed)."""
        target = workspace or (
            next(iter(self.workspaces.values()), None) or self.add_workspace()
        )
        sid = session_id or _uid("session", target.id, str(len(self.sessions)))
        session = FakeSession(sid, workspace=target, script=script, **options)
        self.sessions[sid] = session
        return session

    def fail_next_workspace_create(self, failure: PostFailure) -> None:
        """Queue a failure for ``POST /v0/workspaces`` (no idempotency key)."""
        self._create_failures.append(failure)

    # -- direct (no-HTTP) writes ----------------------------------------------

    def create_workspace(self, **payload: Any) -> WorkspaceCreateResult:
        """``POST /v0/workspaces`` without HTTP.

        **There is no idempotency key here.** Calling this twice with identical
        arguments creates two workspaces — which is exactly why the bot has to
        reconcile on a nonce in the generated name instead of blind-retrying.
        """
        try:
            status, body = self._create_workspace(payload)
        except _Injected as injected:
            raise injected.exc from None
        if status >= 400:
            raise api_error_for_status(status, body, method="POST", path="/workspaces")
        return WorkspaceCreateResult.model_validate(body)

    def create_session(self, **payload: Any) -> Session:
        """``POST /v0/sessions`` without HTTP.

        A caller-supplied ``sessionId`` **is** honoured — verified against the
        live API, contra ``docs/PLAN.md``.
        """
        status, body = self._create_session(payload)
        if status >= 400:
            raise api_error_for_status(status, body, method="POST", path="/sessions")
        return Session.model_validate(body)

    # -- wiring ---------------------------------------------------------------

    @property
    def me_url(self) -> str:
        """The absolute ``GET /me`` URL. ``{base_url}/me`` would 404."""
        return f"{self.root_url}/me"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        """An ``AsyncClient`` wired to this fake, with UA and bearer preset."""
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.api_key or 'fake-key'}",
        }
        headers.update(kwargs.pop("headers", {}))
        return httpx.AsyncClient(
            transport=self.transport(),
            base_url=kwargs.pop("base_url", self.base_url),
            headers=headers,
            **kwargs,
        )

    def calls_to(self, needle: str, *, method: str | None = None) -> list[Call]:
        return [
            c
            for c in self.calls
            if needle in c.path and (method is None or c.method == method)
        ]

    def reset_calls(self) -> None:
        self.calls.clear()

    # -- request handling -----------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = {k: v for k, v in request.url.params.items()}
        body = _json_body(request)
        try:
            status, payload = self._route(request, params, body)
        except _Injected as injected:
            self.calls.append(
                Call(
                    request.method,
                    request.url.path,
                    params,
                    body,
                    0,
                    error=repr(injected.exc),
                )
            )
            raise injected.exc from None
        self.calls.append(Call(request.method, request.url.path, params, body, status))
        return httpx.Response(status, json=payload)

    def _route(
        self,
        request: httpx.Request,
        params: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        guard = self._check_headers(request)
        if guard is not None:
            return guard

        method = request.method.upper()
        raw_path = request.url.path.rstrip("/") or "/"

        # `GET /me` lives at the API root. `/v0/me` genuinely 404s — verified.
        if raw_path == "/me":
            if method != "GET":
                return 405, _error("method_not_allowed", f"{method} /me")
            return 200, {
                "userId": _uid("me", "user"),
                "email": "owner@example.com",
                "organizationId": _uid("me", "org"),
                "authMethod": "apiKey",
                "apiKey": {"id": _uid("me", "key"), "name": "fake-api-key"},
            }
        if not raw_path.startswith("/v0/"):
            return 404, _error("not_found", f"no route for {raw_path}")
        path = raw_path[3:]

        return self._dispatch(method, path, params, body)

    def _dispatch(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        payload: Mapping[str, Any] = body or {}

        if path == "/projects" and method == "GET":
            return 200, _page(list(self.projects.values()))

        match = _RE_PROJECT_WS.match(path)
        if match and method == "GET":
            pid = match.group(1)
            if pid not in self.projects:
                return 404, _error("not_found", f"project {pid} not found")
            return 200, _page(
                [w.as_json() for w in self.workspaces.values() if w.project_id == pid]
            )

        if path == "/workspaces" and method == "POST":
            return self._create_workspace(payload)

        match = _RE_WORKSPACE_STATUS.match(path)
        if match and method == "GET":
            workspace = self.workspaces.get(match.group(1))
            if workspace is None:
                return 404, _error("not_found", "workspace not found")
            self._advance_for_workspace(workspace.id, "ws")
            return 200, workspace.status_json()

        match = _RE_WORKSPACE_SESSIONS.match(path)
        if match and method == "GET":
            wid = match.group(1)
            if wid not in self.workspaces:
                return 404, _error("not_found", "workspace not found")
            return 200, _page(
                [s.as_json() for s in self.sessions.values() if s.workspace_id == wid]
            )

        match = _RE_WORKSPACE_RENAME.match(path)
        if match and method == "POST":
            workspace = self.workspaces.get(match.group(1))
            if workspace is None:
                return 404, _error("not_found", "workspace not found")
            new_name = payload.get("name")
            if isinstance(new_name, str) and new_name:
                workspace.name = new_name
            workspace.rename_count += 1
            return 200, workspace.as_json()

        match = _RE_WORKSPACE_ARCHIVE.match(path)
        if match and method == "POST":
            workspace = self.workspaces.get(match.group(1))
            if workspace is None:
                return 404, _error("not_found", "workspace not found")
            # Archive is idempotent and restorable.
            workspace.status = WorkspaceStatusValue.ARCHIVED
            return 200, workspace.as_json()

        match = _RE_WORKSPACE.match(path)
        if match and method == "GET":
            workspace = self.workspaces.get(match.group(1))
            if workspace is None:
                return 404, _error("not_found", "workspace not found")
            return 200, workspace.as_json()

        if path == "/sessions" and method == "POST":
            return self._create_session(payload)

        match = _RE_SESSION_MESSAGES.match(path)
        if match:
            session = self.sessions.get(match.group(1))
            if session is None:
                return 404, _error("not_found", "session not found")
            if method == "POST":
                return session._post_json(payload)
            if method == "GET":
                session._maybe_advance("messages")
                limit = _int_param(params, "limit")
                if limit is _INVALID:
                    return 400, _error("invalid_request", "limit must be an integer")
                offset = _int_param(params, "offset")
                if offset is _INVALID:
                    return 400, _error("invalid_request", "offset must be an integer")
                return session._messages_json(
                    after=params.get("after"),
                    offset=offset,
                    limit=limit,
                )

        match = _RE_SESSION_STATUS.match(path)
        if match and method == "GET":
            session = self.sessions.get(match.group(1))
            if session is None:
                return 404, _error("not_found", "session not found")
            session._maybe_advance("status")
            tick = session.current_tick
            if tick.status_http is not None:
                extra: dict[str, Any] = {}
                if tick.retry_after is not None:
                    extra["retryAfter"] = tick.retry_after
                return tick.status_http, (
                    _error(
                        "upstream_error",
                        "scripted /status failure",
                        retryable=True,
                    )
                    | extra
                )
            return 200, session.status_json()

        match = _RE_SESSION_CANCEL.match(path)
        if match and method == "POST":
            session = self.sessions.get(match.group(1))
            if session is None:
                return 404, _error("not_found", "session not found")
            return 200, session._cancel_json()

        match = _RE_MESSAGE.match(path)
        if match and method == "GET":
            envelope_id = match.group(1)
            for session in self.sessions.values():
                position = session._index_of.get(envelope_id)
                if position is not None:
                    return 200, session.transcript[position]
            return 404, _error("not_found", f"message {envelope_id} not found")

        if path == "/sql" and method == "POST":
            return self._sql(payload)

        return 404, _error("not_found", f"no route for {path}")

    def _check_headers(
        self, request: httpx.Request
    ) -> tuple[int, dict[str, Any]] | None:
        agent = request.headers.get("user-agent", "")
        if self.require_user_agent:
            lowered = agent.lower()
            if not agent or any(lowered.startswith(b) for b in _BLOCKED_UA):
                return 403, _error(
                    "forbidden",
                    "blocked user agent; send an explicit User-Agent header",
                )
        if self.require_auth:
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer "):
                return 401, _error("unauthorized", "missing bearer token")
            token = authorization.removeprefix("Bearer ").strip()
            if not token or (self.api_key is not None and token != self.api_key):
                return 401, _error("unauthorized", "bad api key")
        return None

    def _advance_for_workspace(self, workspace_id: str, kind: str) -> None:
        for session in self.sessions.values():
            if session.workspace_id == workspace_id:
                session._maybe_advance(kind)

    def _create_workspace(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """**No idempotency key.** A blind retry really does create a second one."""
        self._counter += 1
        name = payload.get("name")
        name = name if isinstance(name, str) and name else f"workspace-{self._counter}"
        if self._create_failures:
            failure = self._create_failures.popleft()
            if failure.landed:
                self._really_create_workspace(payload, name)
            if failure.exc is not None:
                raise _Injected(failure.exc)
            status = failure.status or 500
            return status, _error(
                "server_error", "scripted create failure", retryable=True
            )
        workspace, session = self._really_create_workspace(payload, name)
        return 201, {
            "workspaceId": workspace.id,
            "sessionId": session.session_id,
            "deepLink": workspace.deep_link,
        }

    def _really_create_workspace(
        self, payload: Mapping[str, Any], name: str
    ) -> tuple[FakeWorkspace, FakeSession]:
        project_id = payload.get("projectId")
        pid = (
            project_id
            if isinstance(project_id, str) and project_id
            else (next(iter(self.projects), None) or self.add_project())
        )
        workspace = self.add_workspace(
            name,
            workspace_id=_uid("created", name, str(self._counter)),
            project_id=pid,
            status=WorkspaceStatusValue.INITIALIZING,
            lifecycle_step="creating sandbox",
        )
        self.created_workspace_names.append(name)
        session = self.add_session(workspace=workspace)
        return workspace, session

    def _create_session(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """``sessionId`` **is** accepted here — verified, contra PLAN.md."""
        raw_id = payload.get("sessionId")
        session_id = raw_id if isinstance(raw_id, str) and raw_id else None
        if session_id is not None and session_id in self.sessions:
            return 201, self.sessions[session_id].as_json()
        workspace_id = payload.get("workspaceId")
        workspace = (
            self.workspaces.get(workspace_id)
            if (isinstance(workspace_id, str))
            else None
        )
        if workspace is None:
            return 404, _error("not_found", "workspace not found")
        options: dict[str, Any] = {}
        for key in ("agent", "model", "effort"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                options[key] = value
        session = self.add_session(
            session_id=session_id, workspace=workspace, **options
        )
        return 201, session.as_json()

    def _sql(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """``POST /v0/sql`` over the one view, with the documented guard rails."""
        raw = payload.get("query") or payload.get("sql")
        query = raw if isinstance(raw, str) else ""
        stripped = query.strip().rstrip(";").strip()
        if not stripped:
            return 400, _error("invalid_request", "empty query")
        if len(query) > _SQL_MAX_CHARS:
            return 400, _error("invalid_request", "query too long")
        if ";" in stripped:
            return 400, _error("invalid_request", "only a single statement is allowed")
        if "set_config" in query.lower():
            return 400, _error("invalid_request", "set_config is not permitted")
        if not stripped.lower().startswith("select"):
            return 400, _error("invalid_request", "only SELECT is permitted")
        rows = self.sql_rows()
        return 200, {
            "rows": rows[:_SQL_MAX_ROWS],
            "rowCount": min(len(rows), _SQL_MAX_ROWS),
            "truncated": len(rows) > _SQL_MAX_ROWS,
        }

    def sql_rows(self) -> list[dict[str, Any]]:
        """``session_transcripts_view`` rows built from current fake state.

        The query text is validated but not interpreted — every accepted query
        returns the whole view. Tests that care about filtering should assert on
        the query the bot *sent*, which is the security-relevant part.
        """
        rows: list[dict[str, Any]] = []
        for session in self.sessions.values():
            transcript_parts: list[str] = []
            for message in session.messages_model():
                if message.is_user_echo and message.prompt_text:
                    transcript_parts.append(f"user: {message.prompt_text}")
                for block in message.blocks:
                    if block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            transcript_parts.append(f"assistant: {text}")
            rows.append(
                {
                    "session_id": session.session_id,
                    "workspace_id": session.workspace_id,
                    "transcript": "\n".join(transcript_parts),
                    "session_title": session.title,
                    "agent_type": session.agent,
                    "model": session.model,
                    "workspace_name": session.workspace.name,
                    "workspace_state": str(session.workspace.status),
                    "repo_url": session.workspace.repo_url,
                    "session_created_at": "2026-07-26 01:00:00.000+00",
                    "transcript_updated_at": "2026-07-26 02:00:41.267+00",
                }
            )
        return rows


_INVALID: Final = object()


def _int_param(params: Mapping[str, str], name: str) -> Any:
    raw = params.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return _INVALID


def _json_body(request: httpx.Request) -> dict[str, Any] | None:
    if not request.content:
        return None
    try:
        parsed = json.loads(request.content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _page(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    data = list(rows)
    return {"data": data, "offset": len(data), "hasMore": False}


# ── scenarios ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named, pre-wired fake plus the ids a test needs to drive it."""

    name: str
    fake: FakeConductor
    session: FakeSession
    expectation: str

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def workspace_id(self) -> str:
        return self.session.workspace_id

    @property
    def prompt_ids(self) -> tuple[str, ...]:
        """The ``messageId`` values to POST, in order."""
        return self.session.prompt_ids

    def start(self, text: str = "do the thing", *, prompt: int = 0) -> str:
        """POST prompt ``prompt`` with its preassigned id. Returns that id."""
        message_id = self.session.prompt_ids[prompt]
        self.session.post(text, message_id=message_id)
        return message_id


def _scenario(
    name: str,
    script: Sequence[Tick],
    expectation: str,
    *,
    advance: Advance = Advance.ANY,
    workspace_status: WorkspaceStatusValue = WorkspaceStatusValue.READY,
    lifecycle_step: str | None = None,
    **options: Any,
) -> Scenario:
    fake = FakeConductor()
    workspace = fake.add_workspace(
        f"fake/{name}",
        workspace_id=_uid("workspace", name),
        status=workspace_status,
        lifecycle_step=lifecycle_step,
    )
    session = fake.add_session(
        session_id=_uid("session", name),
        workspace=workspace,
        script=script,
        advance=advance,
        **options,
    )
    return Scenario(name=name, fake=fake, session=session, expectation=expectation)


_IDLE: Final = SessionStatusValue.IDLE
_WORKING: Final = SessionStatusValue.WORKING
_ERROR: Final = SessionStatusValue.ERROR


def queued_idle_trap(*, advance: Advance = Advance.ANY) -> Scenario:
    """POST -> ``idle, idle, idle, working, idle x3``.

    The documented trap: *"a queued prompt hasn't started a turn yet, and the
    session reports idle until it does."* Anything that finalizes on the first
    ``idle`` declares the turn done before it started and then loses the whole
    answer, which arrives on tick 5.
    """
    return _scenario(
        "queued_idle_trap",
        [
            Tick(_IDLE, label="queued 1"),
            Tick(_IDLE, label="queued 2"),
            Tick(_IDLE, label="queued 3"),
            Tick(
                _WORKING,
                emit=(state_changed(), system_init(), assistant("on it")),
                label="working at last",
            ),
            Tick(
                _IDLE,
                emit=(assistant("the answer is 42"), result("the answer is 42")),
                label="answer lands on the first idle",
            ),
            Tick(_IDLE, label="drain 2"),
            Tick(_IDLE, label="drain 3"),
        ],
        "must not finalize before the `working` observation; the answer on "
        "tick 5 must be delivered",
        advance=advance,
    )


def fast_turn(*, advance: Advance = Advance.ANY) -> Scenario:
    """POST -> ``idle, [delta with the final answer], idle x3``.

    The turn starts *and* finishes between two polls, so ``working`` is never
    observed. Delivery must not depend on having seen it; finalize must still
    happen.
    """
    return _scenario(
        "fast_turn",
        [
            Tick(_IDLE, label="pre"),
            Tick(
                _IDLE,
                emit=(
                    state_changed(),
                    system_init(),
                    assistant("done: 7"),
                    result("done: 7"),
                ),
                label="whole turn between two polls",
            ),
            Tick(_IDLE, label="drain 1"),
            Tick(_IDLE, label="drain 2"),
            Tick(_IDLE, label="drain 3"),
        ],
        "must deliver and finalize despite never observing `working`",
        advance=advance,
    )


def double_prompt(*, advance: Advance = Advance.ANY) -> Scenario:
    """Two POSTs while working: both witnessed, exactly one finalize.

    POST ``prompt_ids[0]`` before the first poll and ``prompt_ids[1]`` around
    tick 2. Conductor queues server-side; replies are not attributed to
    individual prompts, so the chat mirrors the stream and finalize waits for
    both echoes.
    """
    return _scenario(
        "double_prompt",
        [
            Tick(_IDLE, label="p1 queued"),
            Tick(
                _WORKING,
                emit=(state_changed(), assistant("first answer")),
                label="p1 working",
            ),
            Tick(_WORKING, label="POST prompt_ids[1] here"),
            Tick(
                _WORKING,
                emit=(assistant("still going"), result("first done")),
                label="p1 result",
            ),
            Tick(
                _WORKING,
                emit=(assistant("second answer", prompt=1),),
                label="p2 working",
            ),
            Tick(
                _IDLE,
                emit=(result("second done", prompt=1),),
                label="p2 result on the idle tick",
            ),
            Tick(_IDLE, label="drain 1"),
            Tick(_IDLE, label="drain 2"),
            Tick(_IDLE, label="drain 3"),
        ],
        "both prompts witnessed, one finalize, no finalize while a prompt is "
        "still outstanding",
        advance=advance,
    )


def error_mid_turn(*, advance: Advance = Advance.ANY) -> Scenario:
    """Partial output, then ``status=error`` — with content still undelivered.

    Two things must hold. The drain has to happen *before* the error is
    surfaced (transition 17 forces a full delta), and the error must not be
    treated as a terminal poll-stop: content keeps arriving after it and the
    status persists indefinitely (observed for 241 consecutive polls).
    """
    detail = "Codex ChatGPT auth not found"
    return _scenario(
        "error_mid_turn",
        [
            Tick(
                _WORKING,
                emit=(state_changed(), assistant("partial output 1")),
                label="partial",
            ),
            Tick(_WORKING, emit=(assistant("partial output 2"),), label="more partial"),
            Tick(
                _ERROR,
                error_message=detail,
                last_error=detail,
                emit=(assistant("partial output 3"), error_result(detail)),
                label="error arrives with content still pending",
            ),
            Tick(
                _ERROR,
                error_message=detail,
                last_error=detail,
                emit=(assistant("trailing after the error"),),
                label="content still arriving after the error",
            ),
            Tick(_ERROR, error_message=detail, last_error=detail, label="error sticks"),
        ],
        "drain the partial output before surfacing errorMessage; keep polling "
        "— `error` persists indefinitely while still accepting POSTs",
        advance=advance,
    )


def cancelled_turn(*, advance: Advance = Advance.ANY) -> Scenario:
    """``/stop``: cancel returns ``{status, canceledQueuedMessages}``, async.

    Call ``scenario.session.cancel()`` (or POST ``/cancel``) around tick 1. The
    session keeps reporting ``working`` for two more ticks, emits a trailing
    fragment, and only then goes ``idle`` — so a cancel that stops draining
    drops content.
    """
    return _scenario(
        "cancelled_turn",
        [
            Tick(
                _WORKING,
                emit=(state_changed(), assistant("working on it")),
                label="working",
            ),
            Tick(_WORKING, label="cancel here"),
            Tick(
                _WORKING,
                emit=(assistant("stopped mid-sen"),),
                label="trailing fragment after the cancel",
            ),
            Tick(_WORKING, label="settling"),
            Tick(_IDLE, label="idle 1"),
            Tick(_IDLE, label="idle 2"),
            Tick(_IDLE, label="idle 3"),
        ],
        "report canceledQueuedMessages, keep draining, finalize as cancelled "
        "only after the session settles",
        advance=advance,
        cancel_queued=2,
        idle_ticks_after_cancel=2,
    )


def replay_attack(*, advance: Advance = Advance.ANY) -> Scenario:
    """``after=`` is ignored and the **entire** transcript comes back.

    Measured behaviour is a 404 for a bad cursor, so this cannot actually
    happen today — which is exactly why it is worth pinning: the
    ``sessionIndex > cursor`` filter is the only thing standing between a
    server-side regression and re-posting a whole transcript to Telegram.
    """
    return _scenario(
        "replay_attack",
        [
            Tick(_WORKING, emit=(state_changed(), assistant("new answer"))),
            Tick(_IDLE, emit=(result("new answer"),)),
            Tick(_IDLE),
            Tick(_IDLE),
        ],
        "every message with sessionIndex <= cursor must be dropped; exactly "
        "the new ones are delivered",
        advance=advance,
        replay_after=True,
        seed=(
            user_message("an older prompt"),
            system_init(),
            assistant("an older answer"),
            result("an older answer"),
        ),
    )


def slow_wake(
    *,
    advance: Advance = Advance.ANY,
    initial: WorkspaceStatusValue = WorkspaceStatusValue.INITIALIZING,
) -> Scenario:
    """The 30–90s init wait: workspace ``initializing`` (or ``sleeping``) → ``ready``.

    ``lifecycleStep`` changes on each tick, which is what the init card shows.
    Pass ``initial=WorkspaceStatusValue.SLEEPING`` for the sleeping-workspace
    variant (probe assumption 8, still untested against the live API).
    """
    return _scenario(
        "slow_wake",
        [
            Tick(_IDLE, workspace=initial, lifecycle_step="creating sandbox"),
            Tick(_IDLE, workspace=initial, lifecycle_step="cloning repository"),
            Tick(_IDLE, workspace=initial, lifecycle_step="installing dependencies"),
            Tick(
                _IDLE,
                workspace=WorkspaceStatusValue.READY,
                lifecycle_step="ready",
                label="ready — release the queued prompt",
            ),
            Tick(
                _WORKING,
                emit=(state_changed(), assistant("hello from a cold start")),
            ),
            Tick(_IDLE, emit=(result("hello from a cold start"),)),
            Tick(_IDLE),
            Tick(_IDLE),
        ],
        "WAKING until the workspace is ready, then the queued prompt runs; no "
        "wake timeout, one waking notification",
        advance=advance,
        workspace_status=initial,
        lifecycle_step="creating sandbox",
    )


def status_5xx(*, failures: int = 4, advance: Advance = Advance.ANY) -> Scenario:
    """``GET /status`` 500s for K polls while content keeps flowing.

    After ``STATUS_FAILURE_THRESHOLD`` consecutive failures the poller must drop
    into cursor-only mode — fixed cadence, ``WORKING`` inferred from recent
    deltas — and still deliver every message. UX degrades; delivery does not.
    """
    script: list[Tick] = [
        Tick(
            _WORKING,
            status_http=500,
            emit=(state_changed(),) if i == 0 else (assistant(f"chunk {i}"),),
            label=f"status 500 #{i + 1}",
        )
        for i in range(failures)
    ]
    script += [
        Tick(_WORKING, emit=(assistant("status is back"),), label="status recovers"),
        Tick(_IDLE, emit=(result("all done"),), label="result"),
        Tick(_IDLE),
        Tick(_IDLE),
        Tick(_IDLE),
    ]
    return _scenario(
        "status_5xx",
        script,
        "enter cursor-only mode after K failures, deliver everything, recover "
        "when /status comes back",
        advance=advance,
    )


def status_flapping(*, advance: Advance = Advance.ANY) -> Scenario:
    """``/status`` alternates 500 / 429 / 200, never K failures in a row.

    Cursor-only mode must **not** latch here — the failure counter has to reset
    on every success — and a 429 must surface its ``Retry-After``.
    """
    return _scenario(
        "status_flapping",
        [
            Tick(_WORKING, emit=(state_changed(),), status_http=500, label="500"),
            Tick(_WORKING, emit=(assistant("chunk 1"),), label="ok"),
            Tick(_WORKING, status_http=429, retry_after=1.5, label="429"),
            Tick(_WORKING, emit=(assistant("chunk 2"),), label="ok"),
            Tick(_WORKING, status_http=503, label="503"),
            Tick(_IDLE, emit=(result("done"),), label="ok + result"),
            Tick(_IDLE),
            Tick(_IDLE),
            Tick(_IDLE),
        ],
        "never latches cursor-only (the failure run never reaches K); honours "
        "Retry-After on the 429",
        advance=advance,
    )


#: Every named scenario, so a test can sweep all of them.
SCENARIOS: Final[dict[str, Callable[[], Scenario]]] = {
    "queued_idle_trap": queued_idle_trap,
    "fast_turn": fast_turn,
    "double_prompt": double_prompt,
    "error_mid_turn": error_mid_turn,
    "cancelled_turn": cancelled_turn,
    "replay_attack": replay_attack,
    "slow_wake": slow_wake,
    "status_5xx": status_5xx,
    "status_flapping": status_flapping,
}
