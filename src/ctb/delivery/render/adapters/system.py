"""System lifecycle events and rate-limit events. Meta by default.

``system/init``, ``system/session_state_changed`` and ``rate_limit_event`` are
the three the Phase 0 probe observed, and all three are plumbing: the turn
machine already drives the card from ``GET /status``, so repeating "running"
in the chat says nothing.

The one exception is a rate-limit event whose ``status`` is not ``allowed``.
That is the difference between "the agent is thinking" and "the agent will
never answer", and PLAN's error rule applies: show it at every verbosity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Final

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.adapters.base import Adapter, one_line, plain_html
from ctb.delivery.render.adapters.extract import best_effort_text
from ctb.delivery.render.adapters.shapes import (
    first_str,
    mapping_of,
    normalize,
    raw_payload,
)
from ctb.delivery.render.types import (
    Block,
    BlockKind,
    RenderContext,
    TextBlock,
    Verbosity,
)

__all__ = ["RateLimitAdapter", "SystemAdapter"]

_META_LIMIT: Final = 300
#: Rate-limit statuses that mean "carry on".
_OK_STATUSES: Final = frozenset({"allowed", "ok", "available", "none"})


class SystemAdapter(Adapter):
    """Matches session lifecycle noise: ``init``, state changes, and friends."""

    name: ClassVar[str] = "system"

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        payload = raw_payload(content)
        if not payload:
            return False
        kind = normalize(payload.get("type"))
        if kind in {"system", "systemevent", "sessionstatechanged", "lifecycle"}:
            return True
        # Shape fallback: a session-scoped event with a lifecycle subtype and
        # no message body of any kind.
        subtype = normalize(payload.get("subtype"))
        return subtype in {"init", "sessionstatechanged"} and "message" not in payload

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        if not context.verbosity.at_least(Verbosity.VERBOSE):
            return []
        payload = raw_payload(message.content)
        subtype = normalize(payload.get("subtype"))

        if subtype == "init":
            model = first_str(payload, "model") or "?"
            tools = payload.get("tools")
            tool_count = len(tools) if isinstance(tools, list) else 0
            detail = f"session ready · {model} · {tool_count} tools"
        elif subtype == "sessionstatechanged":
            state = first_str(payload, "state") or "?"
            detail = f"session {state}"
        else:
            label = first_str(payload, "subtype", "type") or "system"
            body = best_effort_text(payload, limit=_META_LIMIT)
            detail = f"{label} {body}".strip()

        text = one_line(detail, _META_LIMIT)
        if not text:
            return []
        return [
            TextBlock(
                html=f"⚙️ <i>{plain_html(text)}</i>",
                kind=BlockKind.META,
                silent=True,
                source_message_id=message.id,
            )
        ]


class RateLimitAdapter(Adapter):
    """Matches a rate-limit event. Loud only when the limit actually bites."""

    name: ClassVar[str] = "rate_limit"

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        payload = raw_payload(content)
        if not payload:
            return False
        if normalize(payload.get("type")) in {"ratelimitevent", "ratelimit"}:
            return True
        return bool(
            mapping_of(payload.get("rate_limit_info") or payload.get("rateLimitInfo"))
        )

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        payload = raw_payload(message.content)
        info = mapping_of(
            payload.get("rate_limit_info") or payload.get("rateLimitInfo")
        )
        status = first_str(info, "status") or "unknown"
        limit_type = first_str(info, "rateLimitType", "rate_limit_type")

        if normalize(status) in _OK_STATUSES:
            if not context.verbosity.at_least(Verbosity.VERBOSE):
                return []
            detail = f"rate limit {status}"
            if limit_type:
                detail = f"{detail} · {limit_type}"
            return [
                TextBlock(
                    html=f"⚙️ <i>{plain_html(one_line(detail, _META_LIMIT))}</i>",
                    kind=BlockKind.META,
                    silent=True,
                    source_message_id=message.id,
                )
            ]

        parts = [f"rate limit: {status}"]
        if limit_type:
            parts.append(limit_type)
        reason = first_str(
            info, "overageDisabledReason", "reason", "message", "overageStatus"
        )
        if reason:
            parts.append(reason)
        warning = plain_html(one_line(" · ".join(parts), _META_LIMIT))
        return [
            TextBlock(
                html=f"⚠️ <b>{warning}</b>",
                kind=BlockKind.ERROR,
                source_message_id=message.id,
            )
        ]
