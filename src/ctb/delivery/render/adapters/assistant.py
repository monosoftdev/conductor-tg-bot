"""Messages carrying a block list — the agent actually saying something.

Matched by shape (``rawPayload.message.content`` is a list), not by
``rawPayload.type == "assistant"``, because the same envelope shape carries
tool results back with ``role: "user"``, and a mixed block list
(``[thinking, text, tool_use]``) is the common case rather than the exception.
A message-level adapter that could only handle one of those would drop the
rest of the turn's content on the floor.

Each block is then classified on its own, so one message can legitimately
produce an answer in the chat, a diff line, and an activity string for the
card.

Position matters for exactly one thing: text that precedes a tool call in the
same message is narration, not an answer (see
:func:`~ctb.delivery.render.adapters.shapes.preamble_span`), and is rendered as
card activity rather than as a chat bubble.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Final

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.adapters.base import (
    Adapter,
    ProseRenderer,
    one_line,
    plain_html,
    text_to_blocks,
    truncate_text,
)
from ctb.delivery.render.adapters.diff import describe_file_edit, file_edit_blocks
from ctb.delivery.render.adapters.extract import best_effort_text
from ctb.delivery.render.adapters.shapes import (
    BlockRole,
    classify_block,
    first_str,
    normalize,
    payload_blocks,
    preamble_span,
)
from ctb.delivery.render.adapters.tool import tool_call_blocks, tool_result_blocks
from ctb.delivery.render.html import markdown_to_html
from ctb.delivery.render.types import (
    ActivityLine,
    Block,
    BlockKind,
    RenderContext,
    TextBlock,
    Verbosity,
)

__all__ = ["AssistantAdapter"]

#: Reasoning is shown collapsed at ``verbose``; a whole chain of thought in a
#: <blockquote> is still a wall, so it is clipped rather than chunked.
THINKING_LIMIT: Final = 3000
UNKNOWN_BLOCK_LIMIT: Final = 1000

_THINKING_KEYS: Final = (
    "thinking",
    "reasoning",
    "reasoning_content",
    "thought",
    "text",
)


class AssistantAdapter(Adapter):
    """Renders every block of a message that carries a block list."""

    name: ClassVar[str] = "assistant"

    def __init__(self, prose: ProseRenderer = markdown_to_html) -> None:
        self._prose = prose

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        return bool(payload_blocks(content))

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        raw = payload_blocks(message.content)
        span = preamble_span(raw)
        blocks: list[Block] = []
        for index, block in enumerate(raw):
            blocks.extend(
                self._render_block(block, message, context, preamble=index < span)
            )
        return blocks

    def _render_block(
        self,
        block: Mapping[str, Any],
        message: TranscriptMessage,
        context: RenderContext,
        *,
        preamble: bool,
    ) -> list[Block]:
        role = classify_block(block)
        match role:
            case BlockRole.TEXT:
                return self._text(block, message, preamble=preamble)
            case BlockRole.THINKING:
                return self._thinking(block, message, context)
            case BlockRole.TOOL_USE:
                return self._tool_use(block, message, context)
            case BlockRole.TOOL_RESULT:
                return tool_result_blocks(
                    block,
                    verbosity=context.verbosity,
                    source_message_id=message.id,
                )
            case BlockRole.OTHER:
                return self._other(block, message)

    def _text(
        self, block: Mapping[str, Any], message: TranscriptMessage, *, preamble: bool
    ) -> list[Block]:
        text = first_str(block, "text", "content", strip=False)
        if not text or not text.strip():
            return []
        if not preamble:
            return text_to_blocks(
                text,
                prose=self._prose,
                kind=BlockKind.ANSWER,
                source_message_id=message.id,
            )
        # Narration in front of a tool call. It reads as progress, not as an
        # answer, so it goes where progress goes — the status card — and stays
        # out of the chat until ``verbose``. The transcript keeps the full
        # text either way, so ``/log`` still has it.
        blocks: list[Block] = [
            ActivityLine(
                text=one_line(text.strip()),
                kind=BlockKind.TOOL,
                source_message_id=message.id,
            )
        ]
        blocks.extend(
            text_to_blocks(
                text,
                prose=self._prose,
                kind=BlockKind.THINKING,
                source_message_id=message.id,
                silent=True,
            )
        )
        return blocks

    def _thinking(
        self,
        block: Mapping[str, Any],
        message: TranscriptMessage,
        context: RenderContext,
    ) -> list[Block]:
        if not context.verbosity.at_least(Verbosity.VERBOSE):
            return []
        if normalize(block.get("type")) == "redactedthinking":
            text = "[redacted reasoning]"
        else:
            text = first_str(block, *_THINKING_KEYS, strip=False) or ""
        if not text.strip():
            return []
        clipped = truncate_text(text.strip(), THINKING_LIMIT)
        return [
            TextBlock(
                html=(f"<blockquote expandable>💭 {plain_html(clipped)}</blockquote>"),
                kind=BlockKind.THINKING,
                silent=True,
                source_message_id=message.id,
            )
        ]

    def _tool_use(
        self,
        block: Mapping[str, Any],
        message: TranscriptMessage,
        context: RenderContext,
    ) -> list[Block]:
        edit = describe_file_edit(block)
        if edit is not None:
            return file_edit_blocks(
                edit,
                verbosity=context.verbosity,
                source_message_id=message.id,
            )
        return tool_call_blocks(
            block,
            verbosity=context.verbosity,
            source_message_id=message.id,
        )

    def _other(
        self, block: Mapping[str, Any], message: TranscriptMessage
    ) -> list[Block]:
        """A block shape nobody planned for, inside a message we do understand.

        Best-effort text at ``UNKNOWN`` visibility — losing an image block is
        fine, losing a paragraph because it arrived in a new wrapper is not.
        """
        text = best_effort_text(block, limit=UNKNOWN_BLOCK_LIMIT)
        if not text.strip():
            return []
        return text_to_blocks(
            text,
            prose=self._prose,
            kind=BlockKind.UNKNOWN,
            source_message_id=message.id,
        )
