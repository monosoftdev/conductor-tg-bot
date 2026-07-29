"""Our own prompt, echoed back by the transcript. Always suppressed.

The echo is load-bearing *elsewhere*: ``content.id`` equalling the ``messageId``
we POSTed is the proof a prompt was accepted (see
``TranscriptMessage.witnesses_prompt``). It is just not something to send back
to the person who typed it thirty seconds ago.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.adapters.base import SUPPRESS, Adapter
from ctb.delivery.render.adapters.shapes import first_str, normalize, raw_payload
from ctb.delivery.render.types import Block, RenderContext

__all__ = ["UserEchoAdapter"]

_CONTRACT_MARK = "OUTPUT CONTRACT"


class UserEchoAdapter(Adapter):
    """Matches a user-authored envelope. Renders nothing."""

    name: ClassVar[str] = "user_echo"

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        if normalize(msg_type) == "usermessage":
            return True
        if normalize(content.get("type")) == "usermessage":
            return True
        if _raw_user_prompt_echo(content):
            return True
        # Shape fallback: a prompt with a sender and a delivery state, and no
        # agent event underneath it.
        has_sender = first_str(content, "senderId", "senderApiKeyName", "sender")
        return bool(
            has_sender
            and first_str(content, "message", "text")
            and not raw_payload(content)
        )

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        return list(SUPPRESS)


def _raw_user_prompt_echo(content: Mapping[str, Any]) -> bool:
    payload = raw_payload(content)
    if normalize(payload.get("type")) != "user":
        return False
    message = payload.get("message")
    if not isinstance(message, Mapping):
        return False
    if normalize(message.get("role")) != "user":
        return False
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return False
    return any(
        isinstance(block, Mapping)
        and normalize(block.get("type")) == "text"
        and _CONTRACT_MARK in str(block.get("text") or block.get("content") or "")
        for block in blocks
    )
