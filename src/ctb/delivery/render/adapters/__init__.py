"""The adapter set, in the order the registry tries it.

Order encodes precedence, and precedence encodes the visibility table in
PLAN §Adapters:

1. :class:`UserEchoAdapter` — our own prompt. Cheapest match, always suppressed.
2. :class:`ErrorAdapter` — ahead of everything that could otherwise classify a
   failed turn as routine and hide it.
3. :class:`ResultAdapter` — successful end-of-turn accounting.
4. :class:`RateLimitAdapter` / :class:`SystemAdapter` — lifecycle noise.
5. :class:`AssistantAdapter` — anything carrying a block list: answers,
   reasoning, tool calls, tool results, file edits.
6. :class:`UnknownAdapter` — matches everything, so the loop always terminates.
"""

from __future__ import annotations

from ctb.delivery.render.adapters.assistant import AssistantAdapter
from ctb.delivery.render.adapters.base import (
    Adapter,
    ProseRenderer,
    plain_html,
    text_to_blocks,
)
from ctb.delivery.render.adapters.diff import (
    FileEdit,
    describe_file_edit,
    diff_document,
    is_file_edit,
)
from ctb.delivery.render.adapters.error import ErrorAdapter
from ctb.delivery.render.adapters.extract import (
    best_effort_text,
    shape_paths,
    shape_signature,
)
from ctb.delivery.render.adapters.result import ResultAdapter
from ctb.delivery.render.adapters.system import RateLimitAdapter, SystemAdapter
from ctb.delivery.render.adapters.tool import activity_text
from ctb.delivery.render.adapters.unknown import UnknownAdapter, UnknownRecord
from ctb.delivery.render.adapters.user_echo import UserEchoAdapter
from ctb.delivery.render.html import markdown_to_html

__all__ = [
    "Adapter",
    "AssistantAdapter",
    "ErrorAdapter",
    "FileEdit",
    "ProseRenderer",
    "RateLimitAdapter",
    "ResultAdapter",
    "SystemAdapter",
    "UnknownAdapter",
    "UnknownRecord",
    "UserEchoAdapter",
    "activity_text",
    "best_effort_text",
    "default_adapters",
    "describe_file_edit",
    "diff_document",
    "is_file_edit",
    "plain_html",
    "shape_paths",
    "shape_signature",
    "text_to_blocks",
]


def default_adapters(*, prose: ProseRenderer = markdown_to_html) -> tuple[Adapter, ...]:
    """The production chain. ``UnknownAdapter`` is last, by construction.

    ``prose`` reaches only the two adapters that render text the *agent* wrote.
    The rest interpolate paths, tool names and status words into tags and
    escape them with :func:`plain_html`, because markdown rendering would read
    ``src/__init__.py`` as italics.
    """
    return (
        UserEchoAdapter(),
        ErrorAdapter(),
        ResultAdapter(),
        RateLimitAdapter(),
        SystemAdapter(),
        AssistantAdapter(prose),
        UnknownAdapter(prose),
    )
