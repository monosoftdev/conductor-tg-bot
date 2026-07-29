"""Pydantic models for the Conductor v0 API surface.

Every model tolerates unknown fields (``extra="allow"``) and accepts either the
camelCase wire name or the snake_case Python name (``populate_by_name=True``).
The API adds fields; a new one must never take the bot down.

Shapes here were verified against the live API during the Phase 0 probe — see
``docs/HANDOFF.md`` and ``probe-out/shape_report.md``. The load-bearing facts:

* A transcript envelope is ``{id, sessionId, sessionIndex, type, content,
  receivedAt}``. The envelope ``id`` is a server-assigned composite
  ``"<sessionId>:<seq>:<sub>"`` — it is **not** the ``messageId`` we POSTed.
* Our POSTed ``messageId`` surfaces as ``content.id`` on the user echo (that is
  "this prompt has been witnessed") and as ``content.turnId`` on every message of
  the turn it triggered (that is exact turn attribution).
* ``sessionIndex`` is **not** gapless. Never infer message loss from a gap.
* ``content`` is untyped in the OpenAPI spec, so it stays a ``dict`` here and is
  classified by *shape* downstream, never by name alone.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from ctb.conductor.errors import PairingError

__all__ = [
    "AGENT_EFFORTS",
    "AGENT_MODELS",
    "Agent",
    "ApiKeyRef",
    "CancelResult",
    "Me",
    "MessagesPage",
    "PostMessageResult",
    "PostState",
    "Project",
    "ProjectsPage",
    "Session",
    "SessionStatus",
    "SessionStatusValue",
    "SessionsPage",
    "SqlResult",
    "TranscriptMessage",
    "Workspace",
    "WorkspaceCreateResult",
    "WorkspaceStatus",
    "WorkspaceStatusValue",
    "WorkspacesPage",
    "default_model_for",
    "efforts_for",
    "is_valid_pairing",
    "models_for",
    "normalize_agent",
    "validate_pairing",
]


class ApiModel(BaseModel):
    """Base for every wire model: camelCase aliases in, snake_case out."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
        frozen=False,
    )


# ── enums (all tolerate an unknown value rather than raising) ────────────────


class SessionStatusValue(StrEnum):
    """``GET /v0/sessions/{id}/status``.

    ``error`` is a **third** status, not an edge case: a session can sit in
    ``error`` indefinitely while still accepting POSTs (observed for 241
    consecutive polls). Any ``while status != "idle"`` loop hangs forever.
    """

    IDLE = "idle"
    WORKING = "working"
    ERROR = "error"
    #: A value the API added after this was written. Treat as "no information":
    #: it must never be read as "the turn finished".
    UNKNOWN = "unknown"


class WorkspaceStatusValue(StrEnum):
    INITIALIZING = "initializing"
    READY = "ready"
    SLEEPING = "sleeping"
    ARCHIVED = "archived"
    DELETED = "deleted"
    UPDATING = "updating"
    UNKNOWN = "unknown"

    @property
    def is_usable(self) -> bool:
        """Whether a prompt POSTed now can be expected to start a turn."""
        return self is WorkspaceStatusValue.READY

    @property
    def is_waking(self) -> bool:
        return self in (
            WorkspaceStatusValue.INITIALIZING,
            WorkspaceStatusValue.SLEEPING,
            WorkspaceStatusValue.UPDATING,
        )

    @property
    def is_gone(self) -> bool:
        return self in (
            WorkspaceStatusValue.ARCHIVED,
            WorkspaceStatusValue.DELETED,
        )


class PostState(StrEnum):
    """``POST /v0/sessions/{id}/messages`` -> ``state``."""

    QUEUED = "queued"
    SENT = "sent"
    UNKNOWN = "unknown"


def _coerce_enum[E: StrEnum](enum_cls: type[E], value: Any, unknown: E) -> E:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value.strip().lower())
        except ValueError:
            return unknown
    return unknown


# ── transcript ───────────────────────────────────────────────────────────────

#: ``rawPayload.type`` values that close a turn. ``error`` is here so a session
#: that fails without an accounting record still finishes rather than hanging.
_TURN_END_TYPES: Final = frozenset({"result", "error"})
#: …and the ones that are always mid-turn. ``system/init`` carries a ``subtype``
#: and would otherwise trip the shape fallback below.
_MID_TURN_TYPES: Final = frozenset({"system", "assistant", "user", "rate_limit_event"})
#: Metrics that describe a whole turn rather than one message.
_TURN_METRICS: Final = ("num_turns", "duration_ms", "total_cost_usd", "usage")


class TranscriptMessage(ApiModel):
    """One envelope from ``GET /v0/sessions/{id}/messages``."""

    id: str
    session_id: str = ""
    session_index: int = -1
    type: str = ""
    content: dict[str, Any] = Field(default_factory=dict)
    #: The API returns a Postgres-ish timestamp (``"2026-07-26 02:00:37.434+00"``)
    #: which is not strict ISO-8601, so it is kept verbatim as a string. Parse
    #: defensively at the point of use.
    received_at: str = ""

    @field_validator("content", mode="before")
    @classmethod
    def _content_must_be_a_mapping(cls, value: Any) -> dict[str, Any]:
        # ``content`` is `{}` (untyped) in the OpenAPI spec. If it ever arrives
        # as a bare string or a list, wrap it rather than raising — a render
        # miss is recoverable, a validation error stalls the whole cursor.
        if isinstance(value, dict):
            return value
        if value is None:
            return {}
        return {"_raw": value}

    # -- shape-safe accessors -------------------------------------------------

    @property
    def content_type(self) -> str | None:
        """``content.type`` — often mirrors the envelope ``type``, not always."""
        return _get_str(self.content, "type")

    @property
    def turn_id(self) -> str | None:
        """``content.turnId`` — equals the ``messageId`` we POSTed for the turn.

        This is the correct correlation key for "which prompt produced this".
        """
        return _get_str(self.content, "turnId")

    @property
    def content_id(self) -> str | None:
        """``content.id`` — on a user echo this is our POSTed ``messageId``."""
        return _get_str(self.content, "id")

    @property
    def user_message_id(self) -> str | None:
        """``content.userMessageId`` — present on every agent message of a turn."""
        return _get_str(self.content, "userMessageId")

    @property
    def raw_payload(self) -> dict[str, Any]:
        payload = self.content.get("rawPayload")
        return payload if isinstance(payload, dict) else {}

    @property
    def raw_payload_type(self) -> str | None:
        """``system`` | ``rate_limit_event`` | ``assistant`` | ``result`` | …"""
        return _get_str(self.raw_payload, "type")

    @property
    def raw_payload_subtype(self) -> str | None:
        """For ``system``: ``init`` | ``session_state_changed``. For ``result``:
        ``success`` | …"""
        return _get_str(self.raw_payload, "subtype")

    @property
    def is_user_echo(self) -> bool:
        """Our own prompt, echoed back into the transcript."""
        return "userMessage" in (self.type, self.content_type or "")

    @property
    def is_agent(self) -> bool:
        return "agent" in (self.type, self.content_type or "")

    @property
    def is_assistant_text(self) -> bool:
        return self.raw_payload_type == "assistant"

    @property
    def is_result(self) -> bool:
        return self.raw_payload_type == "result"

    @property
    def is_error(self) -> bool:
        """True for a ``result`` payload flagged ``is_error``."""
        payload = self.raw_payload
        return bool(payload.get("is_error")) or payload.get("subtype") == "error"

    @property
    def ends_turn(self) -> bool:
        """The agent's own end-of-turn record — one per prompt.

        This is the only *positive* proof that a turn is over. ``GET /status``
        reporting ``idle`` is not: the agent is idle between tool calls too,
        and the mid-turn quiet stretch of a long tool run reads exactly like a
        finished turn. The record is classified by shape as well as by name,
        because ``type`` is a bare string and a second agent need not spell it
        ``result``.
        """
        payload_type = self.raw_payload_type
        if payload_type in _TURN_END_TYPES:
            return True
        if payload_type in _MID_TURN_TYPES:
            return False
        payload = self.raw_payload
        if not payload:
            return False
        # Shape fallback: an end-of-turn accounting record, whatever it calls
        # itself — a verdict plus at least one whole-turn metric.
        has_verdict = "is_error" in payload or "subtype" in payload
        return has_verdict and any(key in payload for key in _TURN_METRICS)

    @property
    def blocks(self) -> list[dict[str, Any]]:
        """``rawPayload.message.content`` — the assistant's block list.

        Blocks look like ``{"type": "text", "text": "…"}`` or a tool-use block.
        Returns ``[]`` for any other shape.
        """
        message = self.raw_payload.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if not isinstance(content, list):
            return []
        return [b for b in content if isinstance(b, dict)]

    @property
    def prompt_text(self) -> str | None:
        """``content.message`` on a user echo."""
        return _get_str(self.content, "message")

    def belongs_to_turn(self, turn_id: str) -> bool:
        return self.turn_id == turn_id or self.user_message_id == turn_id

    def witnesses_prompt(self, message_id: str) -> bool:
        """True when this envelope proves our POSTed prompt reached the session."""
        return self.is_user_echo and self.content_id == message_id


class _Page(ApiModel):
    offset: int | None = None
    has_more: bool = False


class MessagesPage(_Page):
    """``GET /v0/sessions/{id}/messages``.

    ``after=<messageId>`` is an exclusive incremental cursor returning ascending
    ``sessionIndex``; it cannot be combined with ``offset``. An unknown or
    foreign ``after`` id returns HTTP 404 with zero messages — no silent replay.
    """

    data: list[TranscriptMessage] = Field(default_factory=list)

    @property
    def max_session_index(self) -> int | None:
        return max((m.session_index for m in self.data), default=None)


class ProjectsPage(_Page):
    data: list[Project] = Field(default_factory=list)


class WorkspacesPage(_Page):
    data: list[Workspace] = Field(default_factory=list)


class SessionsPage(_Page):
    data: list[Session] = Field(default_factory=list)


# ── status ───────────────────────────────────────────────────────────────────


class SessionStatus(ApiModel):
    status: SessionStatusValue = SessionStatusValue.UNKNOWN
    error_message: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    workspace_id: str | None = None
    session_id: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _tolerate_unknown(cls, value: Any) -> SessionStatusValue:
        return _coerce_enum(SessionStatusValue, value, SessionStatusValue.UNKNOWN)

    @property
    def is_error(self) -> bool:
        return self.status is SessionStatusValue.ERROR

    @property
    def error_text(self) -> str | None:
        """What to actually show a human: ``errorMessage ?? lastError``."""
        return self.error_message or self.last_error


class WorkspaceStatus(ApiModel):
    status: WorkspaceStatusValue = WorkspaceStatusValue.UNKNOWN
    lifecycle_step: str | None = None
    workspace_id: str | None = None
    error_message: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _tolerate_unknown(cls, value: Any) -> WorkspaceStatusValue:
        return _coerce_enum(WorkspaceStatusValue, value, WorkspaceStatusValue.UNKNOWN)


# ── resources ────────────────────────────────────────────────────────────────


class Project(ApiModel):
    id: str
    name: str | None = None
    git_remote: str | None = None


class Workspace(ApiModel):
    id: str
    name: str | None = None
    project_id: str | None = None
    repository_url: str | None = None
    branch: str | None = None
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    deep_link: str | None = None
    status: WorkspaceStatusValue = WorkspaceStatusValue.UNKNOWN
    lifecycle_step: str | None = None
    created_at: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _tolerate_unknown(cls, value: Any) -> WorkspaceStatusValue:
        return _coerce_enum(WorkspaceStatusValue, value, WorkspaceStatusValue.UNKNOWN)


class WorkspaceCreateResult(ApiModel):
    """``POST /v0/workspaces`` -> ``201``.

    **No idempotency key exists for this endpoint.** Never blind-retry it;
    reconcile via the nonce embedded in the generated workspace name.
    """

    workspace_id: str
    session_id: str | None = None
    deep_link: str | None = None


class Session(ApiModel):
    id: str
    workspace_id: str | None = None
    title: str | None = None
    name: str | None = None
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    fast_mode: bool | None = None
    created_at: str | None = None


class PostMessageResult(ApiModel):
    """``POST /v0/sessions/{id}/messages``.

    ``messageId`` is a **caller-supplied idempotency key**. Re-POSTing the same
    id dedupes (verified twice): the second call returns ``201`` with the same
    id and produces exactly one user echo. Write the DB row *before* the HTTP
    call and retry ambiguous failures forever with the same id.
    """

    message_id: str
    state: PostState = PostState.UNKNOWN

    @field_validator("state", mode="before")
    @classmethod
    def _tolerate_unknown(cls, value: Any) -> PostState:
        return _coerce_enum(PostState, value, PostState.UNKNOWN)


class CancelResult(ApiModel):
    """``POST /v0/sessions/{id}/cancel`` — asynchronous; poll until idle."""

    status: str | None = None
    canceled_queued_messages: int = 0


class SqlResult(ApiModel):
    """``POST /v0/sql`` — read-only SELECT over ``session_transcripts_view``.

    Limits: 500 rows, 5s statement timeout, 10 000 char query, single statement,
    no writes, and the literal text ``set_config`` anywhere is rejected. This
    view is also the only cross-org workspace listing.
    """

    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


class ApiKeyRef(ApiModel):
    id: str | None = None
    name: str | None = None


class Me(ApiModel):
    """``GET /me`` — at the API **root**, not under ``/v0``."""

    user_id: str | None = None
    email: str | None = None
    organization_id: str | None = None
    auth_method: str | None = None
    api_key: ApiKeyRef | None = None


# ── agent / model / effort pairing (a mismatch is a 400) ─────────────────────


class Agent(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"


AGENT_MODELS: dict[Agent, tuple[str, ...]] = {
    Agent.CLAUDE: (
        "fable-5",
        "opus-5-1m",
        "opus-4-8-1m",
        "opus-4-8",
        "opus-4-7-1m",
        "opus-4-7",
        "opus-1m",
        "opus",
        "opus-4-6-1m",
        "sonnet-5-1m",
        "sonnet-4-6-1m",
        "sonnet",
        "haiku",
    ),
    Agent.CODEX: (
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.3-codex-spark",
        "gpt-5.3-codex",
        "gpt-5.2-codex",
    ),
    Agent.CURSOR: (
        "auto",
        "composer-2.5",
        "grok-4.5",
    ),
}

#: An empty tuple means "the API documents no effort levels for this agent" —
#: effort is then passed through unvalidated rather than guessed at.
AGENT_EFFORTS: dict[Agent, tuple[str, ...]] = {
    Agent.CLAUDE: ("low", "medium", "high", "xhigh", "max"),
    Agent.CODEX: ("none", "low", "medium", "high", "xhigh", "max", "ultra"),
    Agent.CURSOR: (),
}

#: Extra codex-only constraints from the API docs: ``max`` needs a 5.6 model and
#: ``ultra`` needs Sol or Terra.
_CODEX_EFFORT_MODELS: dict[str, tuple[str, ...]] = {
    "max": ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
    "ultra": ("gpt-5.6-sol", "gpt-5.6-terra"),
}

DEFAULT_MODEL_BY_AGENT: dict[Agent, str] = {
    Agent.CLAUDE: "opus-5-1m",
    Agent.CODEX: "gpt-5.5",
    Agent.CURSOR: "auto",
}


def normalize_agent(agent: str | Agent) -> Agent:
    """Coerce a user-supplied agent name to an :class:`Agent`.

    Raises :class:`~ctb.conductor.errors.PairingError` for anything unknown.
    """
    if isinstance(agent, Agent):
        return agent
    try:
        return Agent(agent.strip().lower())
    except ValueError:
        raise PairingError(
            f"unknown agent {agent!r}; valid agents: "
            + ", ".join(a.value for a in Agent),
            agent=str(agent),
            valid=tuple(a.value for a in Agent),
        ) from None


def models_for(agent: str | Agent) -> tuple[str, ...]:
    return AGENT_MODELS[normalize_agent(agent)]


def efforts_for(agent: str | Agent) -> tuple[str, ...]:
    return AGENT_EFFORTS[normalize_agent(agent)]


def default_model_for(agent: str | Agent) -> str:
    return DEFAULT_MODEL_BY_AGENT[normalize_agent(agent)]


def validate_pairing(
    agent: str | Agent,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[Agent, str | None, str | None]:
    """Reject an agent/model/effort combination the API would 400 on.

    Returns the normalized ``(agent, model, effort)``. ``None`` for model or
    effort means "let the server pick" and is always allowed.
    """
    resolved = normalize_agent(agent)

    normalized_model: str | None = None
    if model is not None:
        normalized_model = model.strip().lower()
        valid_models = AGENT_MODELS[resolved]
        if normalized_model not in valid_models:
            raise PairingError(
                f"model {model!r} is not valid for agent {resolved.value!r}; "
                f"valid models: {', '.join(valid_models)}",
                agent=resolved.value,
                model=model,
                valid=valid_models,
            )

    normalized_effort: str | None = None
    if effort is not None:
        normalized_effort = effort.strip().lower()
        valid_efforts = AGENT_EFFORTS[resolved]
        if valid_efforts and normalized_effort not in valid_efforts:
            raise PairingError(
                f"effort {effort!r} is not valid for agent {resolved.value!r}; "
                f"valid efforts: {', '.join(valid_efforts)}",
                agent=resolved.value,
                model=model,
                effort=effort,
                valid=valid_efforts,
            )
        required = (
            _CODEX_EFFORT_MODELS.get(normalized_effort, ())
            if resolved is Agent.CODEX
            else ()
        )
        if (
            required
            and normalized_model is not None
            and normalized_model not in required
        ):
            raise PairingError(
                f"effort {normalized_effort!r} requires one of "
                f"{', '.join(required)} (got model {model!r})",
                agent=resolved.value,
                model=model,
                effort=effort,
                valid=required,
            )

    return resolved, normalized_model, normalized_effort


def is_valid_pairing(
    agent: str | Agent,
    model: str | None = None,
    effort: str | None = None,
) -> bool:
    try:
        validate_pairing(agent, model, effort)
    except PairingError:
        return False
    return True


def _get_str(source: dict[str, Any], key: str) -> str | None:
    value = source.get(key)
    return value if isinstance(value, str) and value else None
