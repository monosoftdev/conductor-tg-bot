"""The adapter protocol and the text helpers every adapter shares.

An adapter is ``matches(msg_type, content) -> bool`` /
``render(message, context) -> list[Block]``. The registry tries them in order
and wraps both calls, so an adapter is allowed to be wrong — it is never
allowed to be trusted blindly.

Two things live here rather than in an adapter: the ABC, and the conversion
from *agent text* to blocks. Fenced code has to become a :class:`CodeBlock`
before the chunker sees it (the chunker reopens fences across a 4096-unit
boundary and cannot do that for a fence buried inside escaped HTML), and only
an adapter ever sees the raw text.

``TextBlock.html`` must be Telegram-ready HTML, and there are two ways to get
there. A :data:`ProseRenderer` — in production ``html.markdown_to_html`` — is
for **agent prose**, where ``**bold**`` should become ``<b>bold</b>``.
:func:`plain_html` is for everything an adapter interpolates *into* a tag: a
path, a tool name, a status word. Those must never go through the markdown
renderer, which would read ``src/__init__.py`` as a request for italics.
"""

from __future__ import annotations

import html as std_html
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.types import (
    Block,
    BlockKind,
    CodeBlock,
    RenderContext,
    TextBlock,
)

__all__ = [
    "ACTIVITY_LINE_LIMIT",
    "Adapter",
    "ELLIPSIS",
    "ProseRenderer",
    "Segment",
    "SUPPRESS",
    "one_line",
    "plain_html",
    "split_fenced",
    "text_to_blocks",
    "truncate_text",
]

#: Turn agent prose into Telegram-ready HTML. Injected at construction time so
#: ``render/html.py`` owns the markdown whitelist and this module owns none of
#: it. Only ever applied to text the *agent* wrote.
type ProseRenderer = Callable[[str], str]

ELLIPSIS: Final = "…"
#: A status-card activity line is one line on a phone. Longer is noise.
ACTIVITY_LINE_LIMIT: Final = 120
#: The empty render: "this message is deliberately not shown".
SUPPRESS: Final[list[Block]] = []

#: Fence info strings are interpolated into ``class="language-…"``. Anything
#: outside this is dropped rather than escaped — a language is never load-bearing.
_LANGUAGE_RE: Final = re.compile(r"^[A-Za-z0-9_+#.-]{1,20}$")
_FENCE_RE: Final = re.compile(r"^\s*(?:`{3,}|~{3,})")


def plain_html(text: str) -> str:
    """Escape ``& < >``. The minimum that is always valid Telegram HTML.

    ``quote=False`` deliberately: ``"`` and ``'`` are legal text content and
    escaping them just makes agent output look wrong.
    """
    return std_html.escape(text, quote=False)


def truncate_text(text: str, limit: int) -> str:
    """Clip to ``limit`` UTF-16 code units, appending an ellipsis.

    Telegram counts UTF-16 code units, not Python characters, so an emoji
    costs two. Slicing happens on whole characters, never inside a surrogate
    pair.
    """
    if limit <= 0:
        return ""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit - 1:
            return text[:index].rstrip() + ELLIPSIS
    return text


def one_line(text: str, limit: int = ACTIVITY_LINE_LIMIT) -> str:
    """Collapse to a single whitespace-normalised line, clipped to ``limit``."""
    return truncate_text(" ".join(text.split()), limit)


@dataclass(frozen=True, slots=True)
class Segment:
    """A run of agent text that is either prose or one fenced code block."""

    text: str
    is_code: bool = False
    language: str | None = None


def split_fenced(text: str) -> list[Segment]:
    """Split markdown-fenced text into prose and code segments.

    Tolerant by construction: an unterminated fence closes at end of text, a
    fence with a junk info string loses only its language, and text with no
    fences comes back as a single prose segment. Adversarial backticks are the
    normal case in agent output, so this never raises.
    """
    segments: list[Segment] = []
    buffer: list[str] = []
    language: str | None = None
    in_code = False

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer)
        if body.strip():
            segments.append(
                Segment(
                    text=body.strip("\n") if in_code else body.strip(),
                    is_code=in_code,
                    language=language if in_code else None,
                )
            )
        buffer = []

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            flush()
            if in_code:
                in_code = False
                language = None
            else:
                in_code = True
                info = line.strip().lstrip("`~").strip()
                language = info if _LANGUAGE_RE.match(info) else None
            continue
        buffer.append(line)
    flush()
    return segments


def text_to_blocks(
    text: str,
    *,
    prose: ProseRenderer = plain_html,
    kind: BlockKind = BlockKind.ANSWER,
    source_message_id: str | None = None,
    silent: bool = False,
) -> list[Block]:
    """Agent text → blocks, with fenced code lifted into :class:`CodeBlock`."""
    blocks: list[Block] = []
    for segment in split_fenced(text):
        if segment.is_code:
            blocks.append(
                CodeBlock(
                    text=segment.text,
                    language=segment.language,
                    kind=kind,
                    silent=silent,
                    source_message_id=source_message_id,
                )
            )
        else:
            rendered = prose(segment.text)
            if rendered.strip():
                blocks.append(
                    TextBlock(
                        html=rendered,
                        kind=kind,
                        silent=silent,
                        source_message_id=source_message_id,
                    )
                )
    return blocks


class Adapter(ABC):
    """One classification rule plus its renderer.

    :meth:`matches` gets the envelope ``type`` and the raw ``content`` mapping
    because that is all classification is allowed to use, and ``type`` is only
    ever a *hint*: it is a bare string in the OpenAPI spec and ``content`` is
    untyped, so every implementation here checks structure first.
    """

    #: Stable identifier, logged and reported in ``RenderResult.adapter``.
    name: ClassVar[str] = "adapter"

    @abstractmethod
    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        """True when this adapter claims the message. Must not mutate."""

    @abstractmethod
    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        """Blocks for the chat and the status card. May return ``[]``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"
