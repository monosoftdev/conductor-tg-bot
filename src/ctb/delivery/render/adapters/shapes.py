"""Shape probes: what a piece of transcript content *is*, structurally.

Every predicate here answers "does this mapping have the shape of X", and only
then consults a name. The API's ``type`` is a bare string and ``content`` is
``{}`` in the spec, so a name alone is never enough — ``rawPayload.type`` was
``"assistant"``, ``"result"``, ``"system"`` and ``"rate_limit_event"`` in the
Phase 0 probe, and nothing promises that list is closed.

Normalisation folds case, underscores, hyphens and spaces
(``tool_use`` / ``toolUse`` / ``TOOL-USE`` → ``tooluse``) so a name check
survives the three agents' differing conventions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "BlockRole",
    "TOOL_INPUT_KEYS",
    "calls_tool",
    "classify_block",
    "first_str",
    "is_error_signal",
    "mapping_of",
    "normalize",
    "payload_blocks",
    "preamble_span",
    "raw_payload",
    "tool_input",
    "tool_name",
]

_SEPARATORS: Final = re.compile(r"[\s_\-.]+")

#: Where the three agents put a tool call's arguments.
TOOL_INPUT_KEYS: Final = ("input", "parameters", "arguments", "args", "params")

_THINKING_KEYS: Final = ("thinking", "reasoning", "reasoning_content", "thought")
_ERROR_TEXT_KEYS: Final = (
    "error",
    "errorMessage",
    "error_message",
    "lastError",
    "last_error",
    "errorText",
)


def normalize(value: Any) -> str:
    """Fold a type/name string to a comparable token. Non-strings → ``""``."""
    if not isinstance(value, str):
        return ""
    return _SEPARATORS.sub("", value).strip().lower()


def mapping_of(value: Any) -> dict[str, Any]:
    """``value`` as a plain dict, or ``{}``. Never raises, never mutates."""
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def first_str(source: Mapping[str, Any], *keys: str, strip: bool = True) -> str | None:
    """The first key whose value is a non-empty string."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str):
            text = value.strip() if strip else value
            if text:
                return text
    return None


def raw_payload(content: Mapping[str, Any]) -> dict[str, Any]:
    """``content.rawPayload`` — where every agent message keeps its event."""
    return mapping_of(content.get("rawPayload") or content.get("raw_payload"))


def payload_blocks(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Anthropic-style block list carried by a message, or ``[]``.

    Looks in ``rawPayload.message.content`` (observed), then
    ``rawPayload.content`` and ``content.content`` (defensive: a variant that
    flattens the message wrapper would otherwise fall to the unknown path).
    A bare string ``content`` is promoted to a single text block, which is
    legal in the Anthropic schema.
    """
    payload = raw_payload(content)
    candidates: list[Any] = [
        mapping_of(payload.get("message")).get("content"),
        payload.get("content"),
        mapping_of(content.get("message")).get("content"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str):
            if candidate.strip():
                return [{"type": "text", "text": candidate}]
            continue
        if isinstance(candidate, list):
            blocks = [dict(b) for b in candidate if isinstance(b, Mapping)]
            if blocks:
                return blocks
    return []


def tool_name(block: Mapping[str, Any]) -> str | None:
    """A tool call's display name, from wherever the agent put it."""
    return first_str(block, "name", "toolName", "tool_name", "tool")


def tool_input(block: Mapping[str, Any]) -> dict[str, Any]:
    """A tool call's arguments mapping, from wherever the agent put it."""
    for key in TOOL_INPUT_KEYS:
        candidate = mapping_of(block.get(key))
        if candidate:
            return candidate
    return {}


def is_error_signal(source: Mapping[str, Any]) -> bool:
    """True when a payload announces failure by shape.

    ``is_error: false`` and ``api_error_status: null`` are present on *every*
    successful result, so truthiness is the test, never presence.
    """
    if source.get("is_error") or source.get("isError"):
        return True
    if source.get("api_error_status") or source.get("apiErrorStatus"):
        return True
    if normalize(source.get("subtype")) == "error":
        return True
    if normalize(source.get("type")) in {"error", "apierror", "errorevent"}:
        return True
    return first_str(source, *_ERROR_TEXT_KEYS) is not None


class BlockRole(StrEnum):
    """What one block inside a message's block list is."""

    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    OTHER = "other"


def classify_block(block: Mapping[str, Any]) -> BlockRole:
    """Classify one content block by shape, using ``type`` only as a tiebreak.

    Order matters: a ``tool_result`` block carries ``content``, a ``tool_use``
    block carries ``name`` + ``input``, and a thinking block carries its text
    under its own key — so the discriminating structure is checked before the
    generic "has some text" case.
    """
    kind = normalize(block.get("type"))

    if kind in {"toolresult", "toolcallresult", "functionresult"}:
        return BlockRole.TOOL_RESULT
    if kind in {"tooluse", "servertooluse", "toolcall", "functioncall", "mcptooluse"}:
        return BlockRole.TOOL_USE
    if kind in {"thinking", "redactedthinking", "reasoning"}:
        return BlockRole.THINKING
    if kind == "text":
        return BlockRole.TEXT

    # No usable type string: fall back to pure structure.
    has_tool_ref = bool(first_str(block, "tool_use_id", "toolUseId", "tool_call_id"))
    if has_tool_ref and "content" in block and not tool_name(block):
        return BlockRole.TOOL_RESULT
    if tool_name(block) and (tool_input(block) or has_tool_ref):
        return BlockRole.TOOL_USE
    if first_str(block, *_THINKING_KEYS):
        return BlockRole.THINKING
    if first_str(block, "text"):
        return BlockRole.TEXT
    return BlockRole.OTHER


def calls_tool(content: Mapping[str, Any]) -> bool:
    """Does this whole message invoke a tool?

    The cross-message half of :func:`preamble_span`'s argument. Shape-only,
    like everything else here: a ``tool_use`` block anywhere in the payload's
    block list, whatever the envelope calls itself.
    """
    return any(
        classify_block(block) is BlockRole.TOOL_USE for block in payload_blocks(content)
    )


def preamble_span(blocks: Sequence[Mapping[str, Any]]) -> int:
    """How many leading blocks of a message are preamble to a tool call.

    A message that calls a tool cannot be the end of a turn: the protocol
    requires a ``tool_result`` to come back before the agent speaks again. So
    text sitting *before* a tool call in the same message is narration —
    "Let me check how the fixture is scoped" — and never the final answer.
    A six-tool turn is six such messages, and at one Telegram bubble and one
    push notification each that is the whole cost of the turn.

    Text *after* the last tool call is not covered by that argument (nothing
    guarantees the agent puts its tool calls last) and is left alone, because
    losing an answer is unrecoverable and showing one line of narration is not.

    Returns the index just past the last ``tool_use`` block, or ``0`` when the
    message calls no tools.
    """
    for index in range(len(blocks) - 1, -1, -1):
        if classify_block(blocks[index]) is BlockRole.TOOL_USE:
            return index + 1
    return 0
