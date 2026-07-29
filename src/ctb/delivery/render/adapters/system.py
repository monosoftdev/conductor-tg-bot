"""System lifecycle events and rate-limit events. Meta by default.

``system/init``, ``system/session_state_changed`` and ``rate_limit_event`` are
the three the Phase 0 probe observed, and all three are plumbing: the turn
machine already drives the card from ``GET /status``, so repeating "running"
in the chat says nothing.

Rate limits are the one exception, and they come in three flavours rather than
two. ``allowed`` is plumbing. ``blocked`` is the difference between "the agent
is thinking" and "the agent will never answer", so PLAN's error rule applies
and it is shown at every verbosity. In between sits ``allowed_warning``:
requests are still going through, you are just near the cap. That is worth
knowing and is *not* worth a red bubble — and because the event repeats on
every turn once the warning latches, a chat line would be one bubble per turn
for the rest of the week. So it goes to the status card, which coalesces, and
reaches the chat only at ``verbose``.
"""

from __future__ import annotations

import time
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
    ActivityLine,
    Block,
    BlockKind,
    RenderContext,
    TextBlock,
    Verbosity,
)

__all__ = ["RateLimitAdapter", "SystemAdapter"]

_META_LIMIT: Final = 300

#: Rate-limit statuses that mean "carry on". ``allowed_warning`` is deliberately
#: *not* here: it is allowed, but it has something to say.
_OK_STATUSES: Final = frozenset({"allowed", "ok", "available", "none", "active"})
#: Statuses that mean "allowed, but you are near the cap".
_WARN_STATUSES: Final = frozenset(
    {"allowedwarning", "warning", "warn", "approaching", "nearing", "allowednearing"}
)

#: ``rateLimitType`` in the customer's words. Anything unlisted is de-snaked.
_LIMIT_LABELS: Final[dict[str, str]] = {
    "fivehour": "5-hour limit",
    "sevenday": "weekly limit",
    "sevendayopus": "weekly Opus limit",
    "sevendayoauth": "weekly limit",
    "monthly": "monthly limit",
}


def _limit_label(limit_type: str | None, provider: str | None = None) -> str:
    """``seven_day`` → ``weekly limit``. Never an API token in a chat bubble."""
    if not limit_type:
        label = "usage limit"
    else:
        known = _LIMIT_LABELS.get(normalize(limit_type))
        label = (
            known
            or limit_type.replace("_", " ").replace("-", " ").strip()
            or "usage limit"
        )
    # Claude's weekly Opus type already names the model.  Codex events may use
    # the same generic five-hour/weekly windows, with the provider beside them.
    if "codex" in normalize(provider) and "codex" not in normalize(label):
        return f"Codex {label}"
    return label


def _epoch_seconds(value: Any) -> float | None:
    """``resetsAt`` as epoch seconds. Tolerates milliseconds and strings."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number <= 0:
        return None
    # Anything past the year 33658 in seconds is milliseconds in disguise.
    return number / 1000 if number > 1e12 else number


def _reset_phrase(info: Mapping[str, Any], now: float) -> str | None:
    """``resets in 2h 15m`` — the only number the user can act on."""
    resets_at = _epoch_seconds(info.get("resetsAt") or info.get("resets_at"))
    if resets_at is None:
        return None
    remaining = int(resets_at - now)
    if remaining <= 60:
        return "resets shortly"
    hours, minutes = divmod(remaining // 60, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"resets in {days}d {hours}h" if hours else f"resets in {days}d"
    if hours:
        return f"resets in {hours}h {minutes}m" if minutes else f"resets in {hours}h"
    return f"resets in {minutes}m"


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
    """Matches a rate-limit event. Loud only when the limit actually bites.

    Three outcomes, one emoji each, so the state is readable at a glance
    without reading the words:

    ``⚙️``  allowed — plumbing, verbose only.
    ``⏳``  allowed but near the cap — the status card, and the chat at verbose.
    ``⛔``  blocked — an error bubble at every verbosity.
    """

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
        info = (
            mapping_of(payload.get("rate_limit_info") or payload.get("rateLimitInfo"))
            or payload
        )
        status = normalize(first_str(info, "status") or "unknown")
        label = _limit_label(
            first_str(info, "rateLimitType", "rate_limit_type", "limitType"),
            first_str(info, "provider", "agent", "model"),
        )
        resets = _reset_phrase(info, context.now_epoch_s or time.time())

        if status in _OK_STATUSES:
            return self._meta(message, context, f"⚙️ rate limit ok · {label}")

        if status in _WARN_STATUSES:
            detail = f"approaching your {label}"
            if resets:
                detail = f"{detail} · {resets}"
            line = f"⏳ {detail}"
            blocks: list[Block] = [
                ActivityLine(
                    text=one_line(line),
                    kind=BlockKind.META,
                    source_message_id=message.id,
                )
            ]
            blocks.extend(self._meta(message, context, line))
            return blocks

        # Anything else is "the agent will never answer" until the reset.
        parts = [f"{label} reached"]
        reason = first_str(info, "overageDisabledReason", "reason", "message")
        if reason:
            parts.append(reason.replace("_", " "))
        if resets:
            parts.append(resets)
        warning = plain_html(one_line(" · ".join(parts), _META_LIMIT))
        return [
            TextBlock(
                html=f"⛔ <b>{warning}</b>",
                kind=BlockKind.ERROR,
                source_message_id=message.id,
            )
        ]

    def _meta(
        self, message: TranscriptMessage, context: RenderContext, line: str
    ) -> list[Block]:
        """The verbose-only chat copy. Empty below ``verbose``."""
        if not context.verbosity.at_least(Verbosity.VERBOSE):
            return []
        emoji, _, rest = line.partition(" ")
        return [
            TextBlock(
                html=f"{emoji} <i>{plain_html(one_line(rest, _META_LIMIT))}</i>",
                kind=BlockKind.META,
                silent=True,
                source_message_id=message.id,
            )
        ]
