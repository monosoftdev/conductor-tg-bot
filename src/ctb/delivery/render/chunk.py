"""The UTF-16 chunker: blocks in, independently deliverable message parts out.

Telegram counts **UTF-16 code units**, not Python characters. An emoji outside
the BMP is two units, so ``len(text)`` is a lie on exactly the messages that
matter most — the long ones, with the emoji the agent put in its summary. Every
measurement here goes through :func:`~ctb.delivery.render.types.utf16_len`.

Two jobs:

:func:`chunk_html`
    Split Telegram HTML at a unit budget without ever cutting inside a tag or
    an entity, preferring paragraph → line → whitespace → hard cut, and closing
    and reopening the open-tag stack across the boundary (``<pre><code>`` gets a
    ``…(cont.)`` marker) so **each part is valid HTML on its own**.

:func:`chunk_blocks`
    Apply the size policy from ``PLAN.md``: ≤3500 units one message, 3500–7000
    two, above that a head message plus a ``turn-<id>.md`` document. A single
    code block over 40 lines never goes inline — it becomes
    ``[code block, 120 lines →]`` with the full text in the document.

Every part is its own ``deliveries`` row (``part_index``), so a crash halfway
through a turn resumes rather than restarts.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from ctb.delivery.render.html import (
    code_block_html,
    escape,
    sanitize_html,
    strip_html,
)
from ctb.delivery.render.types import (
    DOCUMENT_OVERFLOW_LIMIT,
    MAX_INLINE_CODE_LINES,
    SINGLE_MESSAGE_SOFT_LIMIT,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_TEXT_LIMIT,
    Block,
    CodeBlock,
    DocumentBlock,
    TextBlock,
    chat_blocks,
    utf16_len,
)

__all__ = [
    "CONTINUATION_MARKER",
    "MIN_CHUNK_LIMIT",
    "MessagePart",
    "PartKind",
    "chunk_blocks",
    "chunk_html",
    "document_filename",
]

#: Inserted after the reopened ``<pre><code>`` on the far side of a boundary.
CONTINUATION_MARKER: Final = "…(cont.)\n"

#: Below this the reopen-the-stack overhead stops being a rounding error.
#: Callers pass 4096; tests pass smaller values, but not absurd ones.
MIN_CHUNK_LIMIT: Final = 512

#: Accept a paragraph/line break only if it fills at least this much of the
#: budget. Without it one early ``\n\n`` shreds a long message into confetti.
_MIN_FILL: Final = 0.5

#: A hard cut never lands inside ``&amp;``; entities are short.
_MAX_ENTITY_LEN: Final = 12

_TAG_RE: Final = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)([^<>]*)>")
_FILENAME_UNSAFE_RE: Final = re.compile(r"[^A-Za-z0-9]+")
_LITERAL_TAGS: Final = frozenset({"pre", "code"})


class PartKind(StrEnum):
    """Mirrors ``deliveries.kind``."""

    TEXT = "text"
    CODE = "code"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True, kw_only=True)
class MessagePart:
    """One ``deliveries`` row: exactly one Telegram API call.

    ``html`` is sent with ``parse_mode="HTML"``; ``plain`` is the payload for
    the single ``parse_mode=None`` retry an entity-parse ``TelegramBadRequest``
    is allowed. Both are pre-computed so the retry needs no re-render.
    """

    kind: PartKind = PartKind.TEXT
    index: int = 0
    html: str = ""
    plain: str = ""
    filename: str | None = None
    content: str | None = None
    caption: str | None = None
    source_message_id: str | None = None
    silent: bool = False
    #: Agent-provided decision options. The outbox turns these into inline
    #: buttons only on the final text part.
    quick_replies: tuple[str, ...] = ()
    #: Bot-owned controls attached to durable notices, by CardButton value.
    control_buttons: tuple[str, ...] = ()

    @property
    def utf16_length(self) -> int:
        """Length of the outgoing text, in the units Telegram counts."""
        return utf16_len(self.html)

    @property
    def content_hash(self) -> str:
        """Stable digest for the ``deliveries.content_hash`` duplicate guard."""
        digest = hashlib.sha256()
        digest.update(self.kind.value.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((self.filename or "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update((self.html or self.content or "").encode("utf-8"))
        for reply in self.quick_replies:
            digest.update(b"\x00")
            digest.update(reply.encode("utf-8"))
        for button in self.control_buttons:
            digest.update(b"\x00")
            digest.update(button.encode("utf-8"))
        return digest.hexdigest()

    def payload(self) -> dict[str, Any]:
        """The ``deliveries.payload_json`` body — everything a resend needs."""
        data: dict[str, Any] = {"kind": self.kind.value, "index": self.index}
        if self.html:
            data["html"] = self.html
        if self.plain:
            data["plain"] = self.plain
        if self.filename is not None:
            data["filename"] = self.filename
        if self.content is not None:
            data["content"] = self.content
        if self.caption is not None:
            data["caption"] = self.caption
        if self.source_message_id is not None:
            data["source_message_id"] = self.source_message_id
        if self.silent:
            data["silent"] = True
        if self.quick_replies:
            data["quick_replies"] = list(self.quick_replies)
        if self.control_buttons:
            data["control_buttons"] = list(self.control_buttons)
        return data

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> MessagePart:
        """Rebuild a part from ``deliveries.payload_json`` after a restart."""
        raw_kind = str(data.get("kind", PartKind.TEXT.value))
        known = {member.value for member in PartKind}
        kind = PartKind(raw_kind) if raw_kind in known else PartKind.TEXT
        return cls(
            kind=kind,
            index=int(data.get("index", 0)),
            html=str(data.get("html", "")),
            plain=str(data.get("plain", "")),
            filename=data.get("filename"),
            content=data.get("content"),
            caption=data.get("caption"),
            source_message_id=data.get("source_message_id"),
            silent=bool(data.get("silent", False)),
            quick_replies=tuple(
                str(value)
                for value in data.get("quick_replies", ())
                if isinstance(value, str) and value.strip()
            )[:4],
            control_buttons=tuple(
                str(value)
                for value in data.get("control_buttons", ())
                if isinstance(value, str) and value.strip()
            )[:4],
        )


# ── HTML chunking ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Tag:
    raw: str
    name: str


def chunk_html(html: str, *, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split Telegram HTML into parts of at most ``limit`` UTF-16 units.

    The input is sanitised first (the pass is idempotent), so the output is
    valid Telegram HTML even if the caller handed us something that was not.
    """
    if limit < MIN_CHUNK_LIMIT:
        raise ValueError(f"limit must be >= {MIN_CHUNK_LIMIT}, got {limit}")
    cleaned = sanitize_html(html)
    if not cleaned.strip():
        return []
    chunker = _Chunker(limit)
    for token in _tokenize(cleaned):
        if isinstance(token, _Tag):
            chunker.add_tag(token)
        else:
            chunker.add_text(token)
    return chunker.finish()


def _tokenize(html: str) -> list[_Tag | str]:
    """Tags and text runs. Sanitised input has no ``<`` outside a real tag."""
    tokens: list[_Tag | str] = []
    position = 0
    for match in _TAG_RE.finditer(html):
        if match.start() > position:
            tokens.append(html[position : match.start()])
        closing = bool(match.group(1))
        name = match.group(2).lower()
        tokens.append(_Tag(raw=match.group(0), name=f"/{name}" if closing else name))
        position = match.end()
    if position < len(html):
        tokens.append(html[position:])
    return tokens


class _Chunker:
    """Streaming splitter that keeps the open-tag stack balanced per part."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.buffer: list[str] = []
        self.length = 0
        self.stack: list[_Tag] = []
        self.has_text = False

    # -- geometry ---------------------------------------------------------

    @property
    def in_literal(self) -> bool:
        return any(tag.name in _LITERAL_TAGS for tag in self.stack)

    def close_cost(self) -> int:
        return sum(len(tag.name) + 3 for tag in self.stack)

    def _append(self, text: str) -> None:
        self.buffer.append(text)
        self.length += utf16_len(text)

    # -- input ------------------------------------------------------------

    def add_tag(self, tag: _Tag) -> None:
        if tag.name.startswith("/"):
            # Sanitised input is balanced, so the top always matches. If it
            # somehow does not, dropping the close beats emitting a stray one.
            if self.stack and self.stack[-1].name == tag.name[1:]:
                self.stack.pop()
                self._append(tag.raw)
            return
        # An opening tag costs its own text *plus* the close it commits us to.
        cost = utf16_len(tag.raw) + len(tag.name) + 3
        if self.has_text and self.length + self.close_cost() + cost > self.limit:
            self._seal()
        if self.length + self.close_cost() + cost > self.limit:
            # A pathological tag (a kilobyte-long href). Drop the markup and
            # keep the words: the alternative is a part over the limit.
            return
        self.stack.append(tag)
        self._append(tag.raw)

    def add_text(self, text: str) -> None:
        position = 0
        total = len(text)
        while position < total:
            budget = self.limit - self.length - self.close_cost()
            if budget < _MAX_ENTITY_LEN and self.has_text:
                self._seal()
                continue
            end = _advance(text, position, max(budget, 1))
            if end >= total:
                self._append(text[position:])
                self.has_text = True
                return
            cut = _break_point(text, position, end)
            self._append(text[position:cut])
            self.has_text = True
            position = cut
            self._seal()
            if not self.in_literal:
                while position < total and text[position] == "\n":
                    position += 1

    # -- output -----------------------------------------------------------

    def _body(self) -> str:
        body = "".join(self.buffer)
        return body.rstrip("\n") if self.in_literal else body.rstrip()

    def _emit(self) -> None:
        body = self._body()
        closes = "".join(f"</{tag.name}>" for tag in reversed(self.stack))
        candidate = body + closes
        if _has_visible_text(candidate):
            self.parts.append(candidate)

    def _reopen_cost(self) -> int:
        marker = utf16_len(CONTINUATION_MARKER) if self.in_literal else 0
        return sum(utf16_len(tag.raw) for tag in self.stack) + marker

    def _seal(self) -> None:
        """Close the open tags, start a new part, reopen them."""
        self._emit()
        self.buffer = []
        self.length = 0
        self.has_text = False
        if self._reopen_cost() + self.close_cost() > self.limit // 2:
            # Reopening would leave no room for content, and a part that holds
            # one character each is a message storm. Drop to plain text: the
            # text is already escaped, so it is still valid and still complete.
            self.stack = []
            return
        for tag in self.stack:
            self._append(tag.raw)
        if self.in_literal:
            self._append(CONTINUATION_MARKER)

    def finish(self) -> list[str]:
        self._emit()
        self.buffer = []
        self.length = 0
        return self.parts


def _advance(text: str, start: int, budget: int) -> int:
    """Largest ``end`` with ``utf16_len(text[start:end]) <= budget``.

    Walks characters, so a surrogate pair is never split — the emoji sitting
    exactly on the 4096 boundary moves to the next part whole.
    """
    used = 0
    index = start
    total = len(text)
    while index < total:
        width = 2 if ord(text[index]) > 0xFFFF else 1
        if used + width > budget:
            break
        used += width
        index += 1
    if index == start and start < total:
        index = start + 1
    return index


def _break_point(text: str, start: int, end: int) -> int:
    """Paragraph → line → whitespace → hard cut, never inside an entity."""
    window = text[start:end]
    floor = int(len(window) * _MIN_FILL)
    paragraph = window.rfind("\n\n")
    if paragraph >= floor:
        return start + paragraph + 2
    line = window.rfind("\n")
    if line >= floor:
        return start + line + 1
    space = max(window.rfind(" "), window.rfind("\t"))
    if space >= floor:
        return start + space + 1
    return _entity_safe(text, start, end)


def _entity_safe(text: str, start: int, end: int) -> int:
    """Back a hard cut off the middle of ``&amp;``."""
    window_start = max(start, end - _MAX_ENTITY_LEN)
    ampersand = text.rfind("&", window_start, end)
    if ampersand > start and ";" not in text[ampersand:end]:
        return ampersand
    return end


def _has_visible_text(html: str) -> bool:
    stripped = strip_html(html).replace(CONTINUATION_MARKER, "")
    return bool(stripped.strip())


# ── block → parts ────────────────────────────────────────────────────────────


def document_filename(turn_id: str) -> str:
    """``turn-<id>.md`` — Telegram previews ``.md`` in a searchable viewer.

    Turn ids come from the API, not from us, so everything outside
    ``[A-Za-z0-9]`` becomes a dash: no dots, no slashes, no traversal.
    """
    safe = _FILENAME_UNSAFE_RE.sub("-", turn_id).strip("-")[:48].strip("-")
    return f"turn-{safe}.md" if safe else "turn.md"


def chunk_blocks(
    blocks: Sequence[Block],
    *,
    turn_id: str = "",
    limit: int = TELEGRAM_TEXT_LIMIT,
    soft_limit: int = SINGLE_MESSAGE_SOFT_LIMIT,
    overflow_limit: int = DOCUMENT_OVERFLOW_LIMIT,
    max_code_lines: int = MAX_INLINE_CODE_LINES,
) -> list[MessagePart]:
    """Turn one transcript message's blocks into ``deliveries`` rows.

    Size policy (``PLAN.md`` §Telegram formatting):

    * ``<= soft_limit`` → one message.
    * ``soft_limit..overflow_limit`` → two balanced messages.
    * ``> overflow_limit`` → a head message ending ``… +N more``, plus the
      whole turn as a ``turn-<id>.md`` document.
    * any code block over ``max_code_lines`` → ``[code block, N lines →]``
      inline and the full text in the document, whatever the total size.
    """
    chat = chat_blocks(list(blocks))
    if not chat:
        return []
    source_id = next(
        (b.source_message_id for b in chat if b.source_message_id is not None), None
    )
    silent = all(b.silent for b in chat)

    html_pieces: list[str] = []
    document_pieces: list[str] = []
    attachments: list[DocumentBlock] = []
    deferred = False

    for block in chat:
        match block:
            case TextBlock(html=html):
                clean = sanitize_html(html)
                if clean.strip():
                    html_pieces.append(clean)
                    document_pieces.append(strip_html(clean))
            case CodeBlock(text=text, language=language, filename=filename):
                if not text.strip():
                    continue
                document_pieces.append(_code_markdown(text, language, filename))
                lines = block.line_count
                if lines > max_code_lines:
                    deferred = True
                    html_pieces.append(_deferred_code_html(lines, filename))
                else:
                    html_pieces.append(code_block_html(text, language))
            case DocumentBlock():
                attachments.append(block)

    parts: list[MessagePart] = []
    body = "\n\n".join(html_pieces)
    total = utf16_len(body)
    document_body = "\n\n".join(document_pieces)

    if total > overflow_limit:
        # One head message; the rest of the turn lives in the document, which
        # Telegram previews in a scrollable searchable viewer.
        every = chunk_html(body, limit=max(soft_limit, MIN_CHUNK_LIMIT))
        chunks = (
            [f"{every[0]}\n\n<i>… +{len(every) - 1} more</i>"]
            if len(every) > 1
            else every
        )
    elif total > soft_limit:
        chunks = _two_way_split(body, total=total, limit=limit)
    else:
        chunks = chunk_html(body, limit=limit)
    for chunk in chunks:
        parts.append(_text_part(chunk, len(parts), source_id, silent))

    if document_body.strip() and (total > overflow_limit or deferred):
        parts.append(
            MessagePart(
                kind=PartKind.DOCUMENT,
                index=len(parts),
                filename=document_filename(turn_id),
                content=document_body,
                caption=_caption(turn_id),
                source_message_id=source_id,
                silent=silent,
            )
        )

    for attachment in attachments:
        parts.append(
            MessagePart(
                kind=PartKind.DOCUMENT,
                index=len(parts),
                filename=attachment.filename,
                content=attachment.content,
                caption=_caption_text(attachment.caption),
                source_message_id=attachment.source_message_id or source_id,
                silent=attachment.silent,
            )
        )
    return parts


def _two_way_split(body: str, *, total: int, limit: int) -> list[str]:
    """Two balanced messages for a 3500–7000 unit turn.

    A balanced target is tried first so the two bubbles look alike; if the
    paragraph structure is too coarse for that, the full limit is tried, and
    the fewest-parts result wins. Coarse block structure can still force three.
    """
    ladder = (
        max(MIN_CHUNK_LIMIT, min(limit, total // 2 + 64)),
        max(MIN_CHUNK_LIMIT, min(limit, total * 3 // 5)),
        limit,
    )
    best: list[str] | None = None
    for target in ladder:
        chunks = chunk_html(body, limit=target)
        if len(chunks) <= 2:
            return chunks
        if best is None or len(chunks) < len(best):
            best = chunks
    return best if best is not None else chunk_html(body, limit=limit)


def _text_part(
    html: str, index: int, source_id: str | None, silent: bool
) -> MessagePart:
    is_code = html.startswith("<pre>") or html.startswith("<pre><code")
    kind = PartKind.CODE if is_code else PartKind.TEXT
    return MessagePart(
        kind=kind,
        index=index,
        html=html,
        plain=strip_html(html),
        source_message_id=source_id,
        silent=silent,
    )


def _deferred_code_html(lines: int, filename: str | None) -> str:
    label = f"{filename}, {lines} lines" if filename else f"code block, {lines} lines"
    return f"<i>[{escape(label)} →]</i>"


def _code_markdown(text: str, language: str | None, filename: str | None) -> str:
    header = f"`{filename}`\n" if filename else ""
    fence = "```"
    while fence in text:
        fence += "`"
    body = text.strip("\n")
    return f"{header}{fence}{language or ''}\n{body}\n{fence}"


def _caption(turn_id: str) -> str:
    label = f"Full output for turn {turn_id}" if turn_id else "Full output"
    return _truncate_caption(label)


def _caption_text(caption: str | None) -> str | None:
    return None if caption is None else _truncate_caption(caption)


def _truncate_caption(caption: str) -> str:
    """Telegram caps a caption at 1024 units and 400s on the 1025th."""
    if utf16_len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption
    return _advance_slice(caption, TELEGRAM_CAPTION_LIMIT - 1) + "…"


def _advance_slice(text: str, budget: int) -> str:
    return text[: _advance(text, 0, budget)]
