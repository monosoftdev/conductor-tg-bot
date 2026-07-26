"""The Block vocabulary: the seam between the renderer and the outbox.

An adapter is ``matches(type, content) -> bool`` / ``render(msg, verbosity) ->
list[Block]``. The outbox consumes ``list[Block]`` and chunks, sends and records
them **without knowing which adapter produced them** — so everything the outbox
needs (what to send where, how big it is, what to hash) is reachable through the
helpers here rather than through ``isinstance`` ladders.

Two invariants:

* ``TextBlock.html`` is already **Telegram HTML** — escaped, tag-balanced, and
  using only Telegram's supported subset. Producing it is ``render/html.py``'s
  job; this module never escapes anything.
* Telegram limits are counted in **UTF-16 code units**, not Python characters.
  Use :func:`utf16_len`. A message full of emoji is half as long as ``len()``
  suggests, and getting this wrong is a 400 and a lost reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

__all__ = [
    "ActivityLine",
    "BaseBlock",
    "Block",
    "BlockKind",
    "CodeBlock",
    "DEFAULT_MIN_VERBOSITY",
    "DOCUMENT_OVERFLOW_LIMIT",
    "DocumentBlock",
    "MAX_INLINE_CODE_LINES",
    "RenderContext",
    "SINGLE_MESSAGE_SOFT_LIMIT",
    "TELEGRAM_CAPTION_LIMIT",
    "TELEGRAM_TEXT_LIMIT",
    "TextBlock",
    "Verbosity",
    "activity_lines",
    "chat_blocks",
    "is_chat_block",
    "is_visible",
    "payload_text",
    "payload_utf16_len",
    "utf16_len",
]

# ── Telegram limits, in UTF-16 code units ────────────────────────────────────

#: Hard limit for ``sendMessage`` text.
TELEGRAM_TEXT_LIMIT: Final = 4096
#: Hard limit for a document/photo caption.
TELEGRAM_CAPTION_LIMIT: Final = 1024
#: At or below this, a turn is one message. Above it, two. Above
#: :data:`DOCUMENT_OVERFLOW_LIMIT`, head + "… +N more" + a ``.md`` document.
SINGLE_MESSAGE_SOFT_LIMIT: Final = 3500
DOCUMENT_OVERFLOW_LIMIT: Final = 7000
#: A code block longer than this goes to the document as
#: ``[code block, N lines →]`` inline.
MAX_INLINE_CODE_LINES: Final = 40


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram actually counts."""
    return len(text.encode("utf-16-le")) // 2


# ── vocabulary ───────────────────────────────────────────────────────────────


class Verbosity(StrEnum):
    """Per-chat output level. ``/mode`` and the chats table carry it."""

    QUIET = "quiet"
    NORMAL = "normal"
    VERBOSE = "verbose"

    @property
    def rank(self) -> int:
        return _VERBOSITY_RANK[self]

    def at_least(self, other: Verbosity) -> bool:
        return self.rank >= other.rank


_VERBOSITY_RANK: Final[dict[Verbosity, int]] = {
    Verbosity.QUIET: 0,
    Verbosity.NORMAL: 1,
    Verbosity.VERBOSE: 2,
}


class BlockKind(StrEnum):
    """What a block *is*, so the outbox can prioritise and the card can filter.

    Classification is by **shape**, never by the transcript's type string alone.
    """

    #: The primary content: an assistant's answer. Always shown.
    ANSWER = "answer"
    #: Reasoning / thinking. Suppressed unless verbose.
    THINKING = "thinking"
    #: A tool invocation. Body suppressed; a one-liner feeds the status card.
    TOOL = "tool"
    #: A tool's output. Suppressed unless verbose.
    TOOL_RESULT = "tool_result"
    #: A file edit or diff. One-line summary in chat, full text on demand.
    DIFF = "diff"
    #: Always shown, at every verbosity.
    ERROR = "error"
    #: System / lifecycle / rate-limit noise. Suppressed.
    META = "meta"
    #: Nothing matched: counted in ``unknown_content_types``, best-effort text.
    UNKNOWN = "unknown"


#: The minimum verbosity at which a kind is shown in chat (PLAN §Adapters).
#: An adapter may override this, but this is the default policy and the reason
#: ``ERROR`` is listed at QUIET.
DEFAULT_MIN_VERBOSITY: Final[dict[BlockKind, Verbosity]] = {
    BlockKind.ANSWER: Verbosity.QUIET,
    BlockKind.ERROR: Verbosity.QUIET,
    BlockKind.DIFF: Verbosity.NORMAL,
    BlockKind.TOOL: Verbosity.VERBOSE,
    BlockKind.TOOL_RESULT: Verbosity.VERBOSE,
    BlockKind.THINKING: Verbosity.VERBOSE,
    BlockKind.META: Verbosity.VERBOSE,
    BlockKind.UNKNOWN: Verbosity.NORMAL,
}


def is_visible(kind: BlockKind, verbosity: Verbosity) -> bool:
    """Whether ``kind`` reaches the chat at ``verbosity`` under the default policy."""
    return verbosity.at_least(DEFAULT_MIN_VERBOSITY.get(kind, Verbosity.NORMAL))


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseBlock:
    """Fields every block carries. All keyword-only, so subclasses stay positional."""

    kind: BlockKind = BlockKind.ANSWER
    #: Send with ``disable_notification=True`` regardless of the chat's setting.
    silent: bool = False
    #: The transcript envelope this came from, for the deliveries PK.
    source_message_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TextBlock(BaseBlock):
    """Telegram-ready HTML. Already escaped — the outbox sends it verbatim."""

    html: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CodeBlock(BaseBlock):
    """Raw code. The outbox wraps it in ``<pre><code class="language-…">``.

    Held unescaped so the chunker can split it safely and reopen the fence
    across a boundary; escaping happens at send time.
    """

    text: str
    language: str | None = None
    #: Path this code came from, when it is a file edit.
    filename: str | None = None

    @property
    def line_count(self) -> int:
        return self.text.count("\n") + 1 if self.text else 0


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentBlock(BaseBlock):
    """An attachment: the overflow path, a ``.diff``, or ``/log`` output.

    Telegram previews ``.md`` in a scrollable searchable viewer, which is why
    one tap beats six bubbles for a long turn.
    """

    filename: str
    content: str
    caption: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityLine(BaseBlock):
    """A one-line progress string. Goes to the **status card**, never the chat.

    ``⚙️ working 1m20s · running pytest`` — the card absorbs tool-call noise so
    the chat stays a conversation. Card edits are coalesced to ≤1 per 3s.
    """

    text: str
    kind: BlockKind = BlockKind.TOOL


type Block = TextBlock | CodeBlock | DocumentBlock | ActivityLine


# ── uniform accessors, so the outbox stays type-agnostic ─────────────────────


def is_chat_block(block: Block) -> bool:
    """True when the block belongs in the chat rather than on the status card."""
    return not isinstance(block, ActivityLine)


def chat_blocks(blocks: list[Block]) -> list[Block]:
    return [b for b in blocks if is_chat_block(b)]


def activity_lines(blocks: list[Block]) -> list[str]:
    return [b.text for b in blocks if isinstance(b, ActivityLine)]


def payload_text(block: Block) -> str:
    """The raw text a block carries, for sizing, hashing and logging.

    Not what gets sent — that is the chunker's rendered output — but stable
    enough for ``deliveries.content_hash`` and cheap enough for a size check.
    """
    match block:
        case TextBlock(html=html):
            return html
        case CodeBlock(text=text):
            return text
        case DocumentBlock(content=content, caption=caption):
            return f"{caption or ''}\n{content}"
        case ActivityLine(text=text):
            return text


def payload_utf16_len(block: Block) -> int:
    return utf16_len(payload_text(block))


@dataclass(frozen=True, slots=True)
class RenderContext:
    """What an adapter is told about the chat it is rendering for."""

    verbosity: Verbosity = Verbosity.NORMAL
    #: Session/workspace labels for the completed-turn header line.
    label: str = ""
    #: Extra hints an adapter may consult without changing this signature.
    hints: dict[str, str] = field(default_factory=dict)
