"""Anything that announces failure. Shown at every verbosity, no exceptions.

This adapter sits ahead of the result and system adapters precisely so a
failed turn cannot be classified as a routine event and suppressed. The probe
found a session that sat in ``error`` for 241 consecutive polls while still
accepting POSTs — the one thing the chat must never do with that is stay quiet.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Final

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.adapters.base import Adapter, plain_html, truncate_text
from ctb.delivery.render.adapters.extract import best_effort_text
from ctb.delivery.render.adapters.shapes import (
    first_str,
    is_error_signal,
    normalize,
    raw_payload,
)
from ctb.delivery.render.types import (
    Block,
    BlockKind,
    CodeBlock,
    RenderContext,
    TextBlock,
)

__all__ = ["ErrorAdapter"]

#: Beyond this the message body becomes its own block so the chunker can split
#: it; below it, header and body ride in one bubble.
_INLINE_LIMIT: Final = 300
_BODY_LIMIT: Final = 3000

_MESSAGE_KEYS: Final = (
    "error",
    "errorMessage",
    "error_message",
    "lastError",
    "last_error",
    "result",
    "message",
    "detail",
)


class ErrorAdapter(Adapter):
    """Matches a truthy failure signal anywhere in the envelope."""

    name: ClassVar[str] = "error"

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        if normalize(msg_type) in {"error", "errorevent"}:
            return True
        payload = raw_payload(content)
        return is_error_signal(payload) or is_error_signal(content)

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        payload = raw_payload(message.content)
        source = payload or dict(message.content)
        body = first_str(source, *_MESSAGE_KEYS)
        if body is None:
            body = best_effort_text(source, limit=_BODY_LIMIT)
        body = truncate_text(body.strip(), _BODY_LIMIT)

        reason = first_str(source, "stop_reason", "terminal_reason", "subtype")
        header = "⚠️ <b>Turn failed</b>"
        if reason and normalize(reason) != "error":
            header = f"{header} · <code>{plain_html(reason)}</code>"

        if not body:
            return [
                TextBlock(
                    html=header,
                    kind=BlockKind.ERROR,
                    source_message_id=message.id,
                )
            ]
        if len(body) <= _INLINE_LIMIT and "\n" not in body:
            return [
                TextBlock(
                    html=f"{header}\n{plain_html(body)}",
                    kind=BlockKind.ERROR,
                    source_message_id=message.id,
                )
            ]
        # Multi-line failures are tracebacks and stderr. Monospace reads
        # better and lets the chunker split the body without touching HTML.
        return [
            TextBlock(html=header, kind=BlockKind.ERROR, source_message_id=message.id),
            CodeBlock(text=body, kind=BlockKind.ERROR, source_message_id=message.id),
        ]
