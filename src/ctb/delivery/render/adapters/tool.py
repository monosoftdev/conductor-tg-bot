"""Tool calls and tool results — the noise the status card absorbs.

PLAN §Adapters: a tool call's body is suppressed and becomes a one-line
activity string on the status card; a tool result is suppressed and appears
only at ``verbose``, capped at 500 characters in a ``<pre>``. That is what
keeps the chat a conversation instead of a build log.

:func:`activity_text` returns **plain text, not HTML** — the status card owns
its own escaping, and the block vocabulary marks only ``TextBlock.html`` as
pre-escaped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from ctb.delivery.render.adapters.base import (
    ACTIVITY_LINE_LIMIT,
    one_line,
    plain_html,
    truncate_text,
)
from ctb.delivery.render.adapters.extract import best_effort_text
from ctb.delivery.render.adapters.shapes import first_str, tool_input, tool_name
from ctb.delivery.render.types import (
    ActivityLine,
    Block,
    BlockKind,
    TextBlock,
    Verbosity,
)

__all__ = [
    "TOOL_RESULT_LIMIT",
    "activity_text",
    "tool_call_blocks",
    "tool_result_blocks",
]

#: PLAN: "tool result → suppress; verbose → first 500 chars in <pre>".
TOOL_RESULT_LIMIT: Final = 500

#: Argument keys worth showing next to a tool's name, most specific first.
_DETAIL_KEYS: Final = (
    "command",
    "file_path",
    "filePath",
    "path",
    "notebook_path",
    "url",
    "pattern",
    "query",
    "description",
    "prompt",
    "skill",
    "name",
)
#: Never surface these next to a tool name, whatever the tool called them.
_NOISE_KEYS: Final = frozenset(
    {"content", "contents", "new_string", "old_string", "patch", "diff"}
)

#: Where a shell command stops being the thing it is doing and starts being
#: plumbing: a chained command, a pipeline, a subshell, a heredoc.
_CHAIN: Final = re.compile(r"\s(?:&&|\|\||;|\|)\s|<<|\$\(")
#: Leading segments that say where, not what. Skipped when something follows.
_NAVIGATION: Final = frozenset({".", "cd", "export", "set", "source", "env"})


def activity_text(block: Mapping[str, Any]) -> str:
    """``Bash · git add app/models/org.py`` — one line for the status card.

    One line **by construction**, not by collapsing: a heredoc'd shell command
    flattened into a paragraph is exactly the unreadable soup this exists to
    avoid. The first line, up to the first chain/pipe/subshell, is what a
    person would say they were doing.
    """
    name = tool_name(block) or "tool"
    arguments = tool_input(block)
    detail = first_str(arguments, *_DETAIL_KEYS)
    if detail is None:
        detail = _first_short_value(arguments)
    detail = _headline(detail)
    if not detail:
        return one_line(name)
    return one_line(f"{name} · {detail}", ACTIVITY_LINE_LIMIT)


def _headline(detail: str | None) -> str:
    """The first meaningful line of an argument, without its plumbing."""
    if not detail:
        return ""
    for line in detail.splitlines():
        stripped = line.strip()
        if stripped:
            return _lead_command(stripped)
    return ""


def _lead_command(line: str) -> str:
    segments = [part.strip() for part in _CHAIN.split(line) if part.strip()]
    if not segments:
        return line
    for segment in segments:
        head = segment.split(maxsplit=1)[0]
        if head not in _NAVIGATION:
            return segment
    return segments[0]


def _first_short_value(arguments: Mapping[str, Any]) -> str | None:
    for key, value in arguments.items():
        if key in _NOISE_KEYS or not isinstance(value, str):
            continue
        text = value.strip()
        if text and len(text) <= 200:
            return text
    return None


def tool_call_blocks(
    block: Mapping[str, Any],
    *,
    verbosity: Verbosity = Verbosity.NORMAL,
    source_message_id: str | None = None,
) -> list[Block]:
    """An activity line always; a chat line only at ``verbose``.

    The activity line is emitted at every verbosity because it never reaches
    the chat — the status card is the one place tool traffic belongs, and
    suppressing it there would leave a working turn looking frozen.
    """
    line = activity_text(block)
    blocks: list[Block] = [
        ActivityLine(
            text=line, kind=BlockKind.TOOL, source_message_id=source_message_id
        )
    ]
    if verbosity.at_least(Verbosity.VERBOSE):
        blocks.append(
            TextBlock(
                html=f"🔧 <code>{plain_html(line)}</code>",
                kind=BlockKind.TOOL,
                silent=True,
                source_message_id=source_message_id,
            )
        )
    return blocks


def tool_result_blocks(
    block: Mapping[str, Any],
    *,
    verbosity: Verbosity = Verbosity.NORMAL,
    source_message_id: str | None = None,
) -> list[Block]:
    """Nothing at all below ``verbose``; a clipped ``<pre>`` at ``verbose``.

    A failed tool result is *not* promoted to ``ERROR``: agents retry failed
    tools constantly, and PLAN's "error → always show" is about the turn
    failing, not about a grep that missed.
    """
    if not verbosity.at_least(Verbosity.VERBOSE):
        return []
    text = best_effort_text(block.get("content"), limit=TOOL_RESULT_LIMIT * 2)
    if not text:
        text = best_effort_text(block, limit=TOOL_RESULT_LIMIT * 2)
    if not text.strip():
        return []
    clipped = truncate_text(text, TOOL_RESULT_LIMIT)
    marker = "⚠️ " if block.get("is_error") or block.get("isError") else ""
    return [
        TextBlock(
            html=f"{marker}<pre>{plain_html(clipped)}</pre>",
            kind=BlockKind.TOOL_RESULT,
            silent=True,
            source_message_id=source_message_id,
        )
    ]
