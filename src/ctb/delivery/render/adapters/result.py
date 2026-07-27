"""The turn-completed event. Accounting, not chat material.

A successful ``result`` payload repeats the final answer in ``result`` — the
same text the preceding ``assistant`` message already delivered. Rendering it
would double every reply in the chat, so the text is dropped and only the
*shape of the turn* survives: duration, turns, cost.

**It is deliberately not an activity line.** An activity line is "what the
agent is doing right now", and the status card prints it next to the live
state. A ``result`` saying ``done · 45.8s`` therefore landed on a card that
still (correctly) read ``working 20s`` and still carried Stop — one card
claiming two states and two durations. The turn is over when the state machine
says it is over; the only thing this payload knows that the machine does not
is the money, and that reaches the card as
:class:`~ctb.turn.state.SetTurnCost`.

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
    Block,
    BlockKind,
    RenderContext,
    TextBlock,
    Verbosity,
)

__all__ = ["ResultAdapter", "format_cost", "format_duration", "turn_cost"]


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


def format_cost(usd: float) -> str:
    """``$0.29`` — phone-sized. Sub-cent turns round up so it is never ``$0.00``."""
    if usd <= 0:
        return ""
    return f"${max(usd, 0.01):.2f}"


def turn_cost(content: Mapping[str, Any]) -> float | None:
    """``total_cost_usd`` off a ``result`` payload, or ``None``.

    Read straight off the envelope rather than through a block, because the
    money is the one fact the state machine cannot derive for itself.
    """
    payload = raw_payload(content)
    if not payload:
        return None
    if normalize(payload.get("type")) != "result" and "total_cost_usd" not in payload:
        return None
    cost = _number(payload, "total_cost_usd")
    return cost if cost is not None and cost > 0 else None


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
        """Nothing at all below ``verbose``. The status card says "done"."""
        if not context.verbosity.at_least(Verbosity.VERBOSE):
            return []
        payload = raw_payload(message.content)
        parts: list[str] = ["done"]

        duration = _number(payload, "duration_ms")
        if duration is not None:
            parts.append(format_duration(duration))
        turns = _number(payload, "num_turns")
        if turns is not None and turns > 1:
            parts.append(f"{int(turns)} agent turns")
        cost = _number(payload, "total_cost_usd")
        if cost is not None and cost > 0:
            parts.append(format_cost(cost))

        summary = one_line(" · ".join(parts))
        return [
            TextBlock(
                html=f"✅ <i>{plain_html(summary)}</i>",
                kind=BlockKind.META,
                silent=True,
                source_message_id=message.id,
            )
        ]
