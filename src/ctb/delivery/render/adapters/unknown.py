"""The last adapter. Matches everything, so the registry always terminates.

PLAN §Adapters: *record in ``unknown_content_types``; best-effort text
extraction, else silent-but-counted*. Both halves matter. The extraction means
a new payload wrapper costs formatting, not content. The record means the
gap is visible in ``/health`` instead of being discovered by someone
wondering why their agent went quiet.

:class:`UnknownRecord` is deliberately a *pointer* — type, shape digest and the
ids — never content. Transcript content is the user's source code and the
``unknown_content_types`` table stores none of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.adapters.base import (
    Adapter,
    ProseRenderer,
    text_to_blocks,
)
from ctb.delivery.render.adapters.extract import best_effort_text, shape_signature
from ctb.delivery.render.html import markdown_to_html
from ctb.delivery.render.types import Block, BlockKind, RenderContext

__all__ = ["UNKNOWN_TEXT_LIMIT", "UnknownAdapter", "UnknownRecord"]

UNKNOWN_TEXT_LIMIT: Final = 2000


@dataclass(frozen=True, slots=True)
class UnknownRecord:
    """One row's worth of ``unknown_content_types``, ready to upsert."""

    #: The envelope ``type`` verbatim — ``""`` becomes ``"<empty>"`` so the
    #: primary key stays meaningful.
    type: str
    shape_signature: str
    session_id: str = ""
    message_id: str = ""
    #: Set when an adapter raised and the message fell through to here, so a
    #: renderer bug is distinguishable from a genuinely new shape.
    reason: str = ""


class UnknownAdapter(Adapter):
    """Matches anything. Renders whatever text it can find, or nothing."""

    name: ClassVar[str] = "unknown"

    def __init__(self, prose: ProseRenderer = markdown_to_html) -> None:
        self._prose = prose

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        return True

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        text = best_effort_text(message.content, limit=UNKNOWN_TEXT_LIMIT)
        if not text.strip():
            return []
        return text_to_blocks(
            text,
            prose=self._prose,
            kind=BlockKind.UNKNOWN,
            source_message_id=message.id,
        )

    def record(self, message: TranscriptMessage, *, reason: str = "") -> UnknownRecord:
        """Describe the message for ``unknown_content_types``. Content-free."""
        return UnknownRecord(
            type=message.type or "<empty>",
            shape_signature=shape_signature(message.content),
            session_id=message.session_id,
            message_id=message.id,
            reason=reason,
        )
