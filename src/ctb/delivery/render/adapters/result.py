"""The turn-completed event. Card material, not chat material.

A successful ``result`` payload repeats the final answer in ``result`` — the
same text the preceding ``assistant`` message already delivered. Rendering it
would double every reply in the chat, so the text is dropped and only the
*shape of the turn* survives: duration, turns, cost, on the status card.

Failed results never reach here; :class:`~.error.ErrorAdapter` is ordered
ahead of this one and claims anything with a truthy error signal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.adapters.base import Adapter, one_line, plain_html
from ctb.delivery.render.adapters.shapes import normalize, raw_payload
from ctb.delivery.render.types import (
    ActivityLine,
    Block,
    BlockKind,
    RenderContext,
    TextBlock,
    Verbosity,
)

__all__ = ["ResultAdapter", "format_duration"]


def format_duration(ms: float) -> str:
    """``820ms`` · ``3.7s`` · ``1m20s`` — compact enough for a card line."""
    if ms < 1000:
        return f"{int(ms)}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _number(source: Mapping[str, Any], key: str) -> float | None:
    value = source.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


class ResultAdapter(Adapter):
    """Matches a successful end-of-turn summary."""

    name: ClassVar[str] = "result"

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        payload = raw_payload(content)
        if not payload:
            return False
        if normalize(payload.get("type")) == "result":
            return True
        # Shape fallback: an end-of-turn accounting record, whatever it calls
        # itself — an error/success verdict plus at least one turn metric.
        has_verdict = "is_error" in payload or "subtype" in payload
        metrics = ("num_turns", "duration_ms", "total_cost_usd", "usage")
        return has_verdict and any(key in payload for key in metrics)

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        payload = raw_payload(message.content)
        parts: list[str] = ["done"]

        duration = _number(payload, "duration_ms")
        if duration is not None:
            parts.append(format_duration(duration))
        turns = _number(payload, "num_turns")
        if turns is not None and turns > 1:
            parts.append(f"{int(turns)} turns")
        cost = _number(payload, "total_cost_usd")
        if cost is not None and cost > 0:
            parts.append(f"${cost:.4f}".rstrip("0").rstrip("."))

        summary = one_line(" · ".join(parts))
        blocks: list[Block] = [
            ActivityLine(
                text=f"✅ {summary}",
                kind=BlockKind.META,
                source_message_id=message.id,
            )
        ]
        if context.verbosity.at_least(Verbosity.VERBOSE):
            blocks.append(
                TextBlock(
                    html=f"✅ <i>{plain_html(summary)}</i>",
                    kind=BlockKind.META,
                    silent=True,
                    source_message_id=message.id,
                )
            )
        return blocks
