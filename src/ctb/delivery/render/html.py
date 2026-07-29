"""Telegram HTML: escaping, the tag whitelist, markdown, and the plain fallback.

``parse_mode="HTML"``, never MarkdownV2. MarkdownV2 requires 18 characters to be
escaped *including inside code spans*; agent output is nothing but those
characters and one miss is a ``400`` and a lost reply. HTML needs only ``& < >``
and gives us ``<pre><code class="language-python">`` for free.

Three entry points, in order of trust:

``escape``
    Raw text → HTML text. Escapes ``&``, ``<``, ``>`` exactly once each. Feed it
    text that has **never** been escaped; it is deliberately *not* idempotent
    (``&amp;`` typed by a human must survive as ``&amp;amp;`` so Telegram shows
    the five characters the human typed).

``markdown_to_html``
    Agent markdown → Telegram HTML. Escapes once, then re-introduces only the
    tags Telegram supports. Everything else degrades to plain text.

``sanitize_html``
    Arbitrary HTML-ish string → Telegram HTML. The safety net: unknown tags are
    escaped so they stay *visible* (a raw ``<script>`` in agent output is
    content the user wants to read), mismatched tags are balanced, and unsafe
    ``href``\\ s are dropped. It **is** idempotent, so it is cheap to apply
    again at the last moment before a send. Telegram answers an unknown tag
    with a 400, so nothing reaches the wire without passing through here.

``strip_html`` produces the unmarked text for the one ``parse_mode=None`` retry
that any entity-parse ``TelegramBadRequest`` gets.
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from typing import Final

__all__ = [
    "HR_TEXT",
    "MAX_TAG_DEPTH",
    "SAFE_URL_SCHEMES",
    "TABLE_MAX_WIDTH",
    "TELEGRAM_TAGS",
    "blockquote_html",
    "bold",
    "code_block_html",
    "escape",
    "escape_attr",
    "inline_code_html",
    "is_safe_url",
    "italic",
    "link_html",
    "markdown_to_html",
    "normalize_language",
    "sanitize_html",
    "spoiler",
    "strikethrough",
    "strip_html",
    "underline",
]

#: Every tag Telegram's HTML parser accepts. Anything else is a 400.
TELEGRAM_TAGS: Final[frozenset[str]] = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "a",
        "code",
        "pre",
        "blockquote",
        "span",
        "tg-spoiler",
    }
)

#: Synonyms Telegram accepts, folded to one spelling so the chunker's
#: reopen-the-stack logic never has to compare ``<strong>`` with ``</b>``.
_TAG_ALIASES: Final[dict[str, str]] = {
    "strong": "b",
    "em": "i",
    "ins": "u",
    "strike": "s",
    "del": "s",
}

#: Canonical tags after aliasing.
_CANONICAL_TAGS: Final[frozenset[str]] = frozenset(
    {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "span", "tg-spoiler"}
)

#: Deeper than this and the tag is shown as text. Real agent output nests three
#: or four deep; the cap bounds the chunker's reopen cost so a chunk boundary
#: can never cost more than a fraction of the 4096 budget.
MAX_TAG_DEPTH: Final = 8

#: Schemes allowed in ``<a href>``. Anything else (``javascript:``, ``data:``)
#: loses the tag and keeps the text.
SAFE_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "tg", "mailto"})

#: Markdown thematic breaks have no Telegram equivalent.
HR_TEXT: Final = "———"

#: Widest aligned table that still reads on a phone in portrait, in monospace
#: columns. Telegram scrolls a ``<pre>`` block sideways rather than wrapping it,
#: so a wider table is not broken — it is just a table nobody can see at once,
#: which is why anything past this switches to the stanza layout instead.
TABLE_MAX_WIDTH: Final = 46
#: The column separator and the header rule, drawn with box characters. Both
#: are three cells wide so the rule lines up with the separators above it.
_COLUMN: Final = " │ "
_RULE_JOINT: Final = "─┼─"
#: Costs no column: variation selectors, the joiner, and the BOM.
_ZERO_WIDTH: Final = frozenset("︎️​‌‍﻿")

#: A quote longer than this many lines is posted collapsed.
DEFAULT_EXPANDABLE_QUOTE_LINES: Final = 8

_LANG_RE: Final = re.compile(r"^[a-z0-9][a-z0-9+#._-]{0,19}$")
_SCHEME_RE: Final = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ── escaping ─────────────────────────────────────────────────────────────────


def escape(text: str) -> str:
    """Escape raw text for Telegram HTML. Exactly one pass, never re-applied."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(value: str) -> str:
    """Escape a value destined for a double-quoted attribute."""
    return escape(value).replace('"', "&quot;")


def normalize_language(language: str | None) -> str | None:
    """Return a safe ``language-…`` class suffix, or ``None``.

    Telegram renders the class only for languages it knows and 400s on a
    malformed attribute, so anything outside a conservative charset is dropped.
    """
    if not language:
        return None
    lang = language.strip().lower()
    return lang if _LANG_RE.match(lang) else None


def is_safe_url(url: str) -> bool:
    """Whether ``url`` may appear in an ``href``.

    Relative URLs are rejected: there is no document base in a Telegram
    message, so they can only be a mis-parse.
    """
    if not url or any(ch in url for ch in "\r\n\t "):
        return False
    match = _SCHEME_RE.match(url)
    return bool(match) and match.group(1).lower() in SAFE_URL_SCHEMES


# ── tag constructors ─────────────────────────────────────────────────────────


def bold(text: str) -> str:
    return f"<b>{escape(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{escape(text)}</i>"


def underline(text: str) -> str:
    return f"<u>{escape(text)}</u>"


def strikethrough(text: str) -> str:
    return f"<s>{escape(text)}</s>"


def spoiler(text: str) -> str:
    return f'<span class="tg-spoiler">{escape(text)}</span>'


def inline_code_html(text: str) -> str:
    return f"<code>{escape(text)}</code>"


def code_block_html(text: str, language: str | None = None) -> str:
    """A fenced block as ``<pre><code class="language-…">``.

    ``text`` is raw code: it is escaped here, exactly once.
    """
    body = escape(text.strip("\n"))
    lang = normalize_language(language)
    if lang:
        return f'<pre><code class="language-{lang}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def link_html(text: str, url: str) -> str:
    """A link, or — when the URL is not safe — ``text (url)`` as plain text."""
    if not is_safe_url(url):
        return f"{escape(text)} ({escape(url)})" if text else escape(url)
    return f'<a href="{escape_attr(url)}">{escape(text) or escape(url)}</a>'


def blockquote_html(inner_html: str, *, expandable: bool = False) -> str:
    """Wrap **already-escaped** HTML in a quote. ``expandable`` = collapsed."""
    open_tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{open_tag}{inner_html}</blockquote>"


# ── sanitiser ────────────────────────────────────────────────────────────────


class _Sanitizer(HTMLParser):
    """Rewrite arbitrary markup into the Telegram subset.

    ``convert_charrefs=True`` means every entity arrives as text and is
    re-escaped on the way out, which is what makes the whole pass idempotent:
    ``&amp;`` → ``&`` → ``&amp;``, and ``&#60;`` → ``<`` → ``&lt;``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        # (canonical name, whether a closing tag should be emitted)
        self.stack: list[tuple[str, bool]] = []

    # -- state ------------------------------------------------------------

    @property
    def _in_code(self) -> bool:
        """Inside ``<pre>``/``<code>``, where no nested markup is allowed."""
        return any(name in ("pre", "code") for name, _ in self.stack)

    def _open_names(self) -> list[str]:
        return [name for name, _ in self.stack]

    # -- handlers ---------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or f"<{tag}>"
        canon = _TAG_ALIASES.get(tag.lower(), tag.lower())
        if not self._start_allowed(canon):
            self.out.append(escape(raw))
            return
        rendered = _render_start(canon, attrs)
        if rendered is None:
            # A known-but-unusable tag (bad href, styled span): drop the
            # markup, keep the words.
            self.stack.append((canon, False))
            return
        self.stack.append((canon, True))
        self.out.append(rendered)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        canon = _TAG_ALIASES.get(tag.lower(), tag.lower())
        if canon not in _CANONICAL_TAGS or self._in_code:
            self.out.append(escape(self.get_starttag_text() or f"<{tag}/>"))
        # An empty whitelisted element carries nothing; drop it silently.

    def handle_endtag(self, tag: str) -> None:
        canon = _TAG_ALIASES.get(tag.lower(), tag.lower())
        if canon in self._open_names():
            while self.stack:
                name, emit = self.stack.pop()
                if emit:
                    self.out.append(f"</{name}>")
                if name == canon:
                    break
            return
        if canon in _CANONICAL_TAGS and not self._in_code:
            # A stray close of a markup tag is markup noise, not content.
            return
        # Inside <pre>/<code>, or for a tag Telegram never had, it *is* content.
        self.out.append(escape(f"</{tag}>"))

    def handle_data(self, data: str) -> None:
        self.out.append(escape(data))

    def handle_comment(self, data: str) -> None:
        self.out.append(escape(f"<!--{data}-->"))

    def handle_decl(self, decl: str) -> None:
        self.out.append(escape(f"<!{decl}>"))

    def unknown_decl(self, data: str) -> None:
        self.out.append(escape(f"<![{data}]>"))

    def handle_pi(self, data: str) -> None:
        self.out.append(escape(f"<?{data}>"))

    # -- helpers ----------------------------------------------------------

    def _start_allowed(self, canon: str) -> bool:
        if canon not in _CANONICAL_TAGS:
            return False
        if len(self.stack) >= MAX_TAG_DEPTH:
            return False
        if canon == "code":
            # The one legal nesting: <code> directly inside <pre>.
            return not self._in_code or (
                self.stack[-1][0] == "pre" and "code" not in self._open_names()
            )
        if self._in_code:
            return False
        return not (canon == "a" and "a" in self._open_names())

    def result(self) -> str:
        while self.stack:
            name, emit = self.stack.pop()
            if emit:
                self.out.append(f"</{name}>")
        return "".join(self.out)


def _render_start(canon: str, attrs: list[tuple[str, str | None]]) -> str | None:
    """The opening tag to emit, or ``None`` to drop the tag but keep content."""
    values = {key.lower(): (value or "") for key, value in attrs}
    if canon == "a":
        href = values.get("href", "").strip()
        return f'<a href="{escape_attr(href)}">' if is_safe_url(href) else None
    if canon == "code":
        lang = normalize_language(_language_from_class(values.get("class", "")))
        return f'<code class="language-{lang}">' if lang else "<code>"
    if canon == "blockquote":
        return "<blockquote expandable>" if "expandable" in values else "<blockquote>"
    if canon == "span":
        classes = values.get("class", "").split()
        return '<span class="tg-spoiler">' if "tg-spoiler" in classes else None
    return f"<{canon}>"


def _language_from_class(value: str) -> str | None:
    for token in value.split():
        if token.startswith("language-"):
            return token[len("language-") :]
    return value.strip() or None


def sanitize_html(html: str) -> str:
    """Coerce ``html`` into valid, balanced, Telegram-supported HTML.

    Idempotent: sanitising sanitised output is a no-op, so callers may apply it
    defensively without corrupting entities.
    """
    if not html:
        return ""
    parser = _Sanitizer()
    parser.feed(_CONTROL_RE.sub("", html.replace("\r\n", "\n").replace("\r", "\n")))
    parser.close()
    return parser.result()


class _Stripper(HTMLParser):
    """Collect text, discard markup — the ``parse_mode=None`` retry payload."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_data(self, data: str) -> None:
        self.out.append(data)


def strip_html(html: str) -> str:
    """Plain text for the one retry with ``parse_mode=None``.

    A reply may look ugly. It is never dropped.
    """
    if not html:
        return ""
    parser = _Stripper()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


# ── markdown ─────────────────────────────────────────────────────────────────

_FENCE_RE: Final = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([^\s`~]*)[ \t]*$")
_QUOTE_RE: Final = re.compile(r"^[ \t]{0,3}>[ \t]?")
_HEADING_RE: Final = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_BULLET_RE: Final = re.compile(r"^([ \t]*)([-*+])[ \t]+(.*)$")
_HR_RE: Final = re.compile(
    r"^[ \t]{0,3}((?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
)

#: A pipe that is not escaped as ``\|`` ends a table cell.
_CELL_SPLIT_RE: Final = re.compile(r"(?<!\\)\|")
_DELIM_CELL_RE: Final = re.compile(r"^:?-+:?$")
#: GFM's one blessed tag inside a cell, and the only way to write two lines
#: into one. Both layouts flatten it to a space.
_BREAK_RE: Final = re.compile(r"<br\s*/?>", re.I)

_CODE_SPAN_RE: Final = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK_RE: Final = re.compile(
    r"\[([^\[\]]*)\]\([ \t]*<?([^\s>()]*)>?(?:[ \t]+\"[^\"]*\")?[ \t]*\)"
)
_PLACEHOLDER_RE: Final = re.compile("\x00([cl])(\\d+)\x01")

_BOLD_STAR: Final = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_BOLD_UNDER: Final = re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?!\w)", re.S)
_STRIKE_RE: Final = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_ITALIC_STAR: Final = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*]*?)(?<=\S)\*(?!\*)", re.S)
_ITALIC_UNDER: Final = re.compile(r"(?<![\w_])_(?=\S)([^_]*?)(?<=\S)_(?!\w)", re.S)


def markdown_to_html(
    text: str,
    *,
    expandable_quote_lines: int = DEFAULT_EXPANDABLE_QUOTE_LINES,
) -> str:
    """Agent markdown → Telegram HTML.

    A deliberately small subset — bold, italic, underline, strike, inline code,
    fenced code, links, quotes, headings and bullets. Everything else survives
    as the literal characters the agent wrote, because a lost reply is worse
    than an unstyled one.
    """
    if not text:
        return ""
    codes: list[str] = []
    links: list[tuple[str, str]] = []
    pieces: list[str] = []
    source = _CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    for is_code, body, language in _split_fences(source):
        if is_code:
            if body.strip():
                pieces.append(code_block_html(body, language))
        elif body:
            pieces.append(
                _render_text_segment(body, codes, links, expandable_quote_lines)
            )
    return sanitize_html(_restore(_join_pieces(pieces), codes, links))


def _split_fences(text: str) -> list[tuple[bool, str, str]]:
    """Split into ``(is_code, body, language)`` runs.

    An unterminated fence swallows the rest of the message — the same choice
    every markdown renderer makes, and the one that keeps stray backticks out
    of the prose.
    """
    segments: list[tuple[bool, str, str]] = []
    lines = text.split("\n")
    buffer: list[str] = []
    index = 0
    while index < len(lines):
        match = _FENCE_RE.match(lines[index])
        if not match:
            buffer.append(lines[index])
            index += 1
            continue
        segments.append((False, "\n".join(buffer), ""))
        buffer = []
        fence, language = match.group(1), match.group(2)
        index += 1
        body: list[str] = []
        while index < len(lines):
            closing = _FENCE_RE.match(lines[index])
            if _closes(closing, fence):
                index += 1
                break
            body.append(lines[index])
            index += 1
        segments.append((True, "\n".join(body), language))
    segments.append((False, "\n".join(buffer), ""))
    return segments


def _closes(closing: re.Match[str] | None, fence: str) -> bool:
    """Whether ``closing`` ends a fence opened with ``fence``.

    Same character, at least as long, and no info string — CommonMark's rule.
    """
    if closing is None or closing.group(2):
        return False
    return closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence)


def _join_pieces(pieces: list[str]) -> str:
    """One newline per segment boundary.

    The line break that ended the paragraph before a fence was consumed as the
    boundary itself, so re-adding exactly one preserves a blank line where the
    author left one and adds none where they did not.
    """
    return "\n".join(pieces)


def _render_text_segment(
    text: str,
    codes: list[str],
    links: list[tuple[str, str]],
    expandable_quote_lines: int,
) -> str:
    """One prose run → HTML, with GFM pipe tables lifted out first.

    Tables are found on the *raw* lines, before :func:`_protect`, because the
    two layouts want opposite things from a cell: the aligned one needs plain
    characters it can measure, and the list one wants the ordinary inline
    pipeline. Everything between two tables goes through the unchanged path.
    """
    raw = text.split("\n")
    out: list[str] = []
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        if buffer:
            out.append(_render_prose(buffer, codes, links, expandable_quote_lines))
            buffer.clear()

    while index < len(raw):
        table = _take_table(raw, index, codes, links)
        if table is None:
            buffer.append(raw[index])
            index += 1
            continue
        flush()
        html, index = table
        out.append(html)
    flush()
    return "\n".join(out)


def _render_prose(
    raw_lines: list[str],
    codes: list[str],
    links: list[tuple[str, str]],
    expandable_quote_lines: int,
) -> str:
    lines = [_protect(line, codes, links) for line in raw_lines]
    out: list[str] = []
    index = 0
    while index < len(lines):
        if _QUOTE_RE.match(lines[index]):
            quoted: list[str] = []
            while index < len(lines) and _QUOTE_RE.match(lines[index]):
                quoted.append(_QUOTE_RE.sub("", lines[index], count=1))
                index += 1
            body = "\n".join(_line_html(line) for line in quoted)
            out.append(
                blockquote_html(body, expandable=len(quoted) > expandable_quote_lines)
            )
            continue
        out.append(_line_html(lines[index]))
        index += 1
    return "\n".join(out)


# ── tables ───────────────────────────────────────────────────────────────────
#
# A GFM pipe table left as prose is the worst thing Telegram renders: a
# proportional font turns the columns into ragged noise and a wide row is cut
# mid-cell by the chunker, because a row has no whitespace worth breaking on.
#
# So a table becomes one of two things, chosen by how wide it actually is:
#
#   narrow   an aligned monospace grid inside <pre>, drawn with box characters.
#            Monospace is the only way columns line up on a phone at all, and
#            <pre> is also what keeps the chunker's row structure intact.
#
#   wide     one stanza per row — the first column bolded as the row's name,
#            the rest as ``label: value`` lines. Nothing lines up, but nothing
#            needs to: each line wraps on its own and reads top to bottom, which
#            is the only shape a five-column table can have in a 40-character
#            viewport.


def _split_row(line: str) -> list[str]:
    """A pipe row → its cells, honouring ``\\|``. Leading/trailing pipe optional."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT_RE.split(body)]


def _alignments(delimiter_cells: list[str]) -> list[str]:
    out: list[str] = []
    for cell in delimiter_cells:
        left, right = cell.startswith(":"), cell.endswith(":")
        out.append("center" if left and right else "right" if right else "left")
    return out


def _table_bounds(lines: list[str], start: int) -> tuple[list[str], int] | None:
    """The delimiter row's cells and the index past the table, or ``None``.

    A header line and a delimiter line with the *same* column count is what
    makes a table unambiguous. Without that check any prose sentence containing
    a pipe would start one.
    """
    if start + 1 >= len(lines):
        return None
    header, delimiter = lines[start], lines[start + 1]
    if _QUOTE_RE.match(header) or "|" not in header:
        return None
    delimiter_cells = _split_row(delimiter)
    if len(delimiter_cells) < 2 or not all(
        _DELIM_CELL_RE.match(cell) for cell in delimiter_cells
    ):
        return None
    if len(_split_row(header)) != len(delimiter_cells):
        return None
    end = start + 2
    while end < len(lines) and "|" in lines[end] and lines[end].strip():
        if _QUOTE_RE.match(lines[end]):
            break
        end += 1
    return delimiter_cells, end


def _take_table(
    lines: list[str],
    start: int,
    codes: list[str],
    links: list[tuple[str, str]],
) -> tuple[str, int] | None:
    """Render the table starting at ``start``, or ``None`` if there isn't one."""
    bounds = _table_bounds(lines, start)
    if bounds is None:
        return None
    delimiter_cells, end = bounds
    align = _alignments(delimiter_cells)
    header = _split_row(lines[start])
    body = [_split_row(line) for line in lines[start + 2 : end]]
    width = max(len(header), *(len(row) for row in body)) if body else len(header)
    header = _pad_row(header, width)
    body = [_pad_row(row, width) for row in body]
    align += ["left"] * (width - len(align))

    grid = _table_grid(header, body, align)
    if grid is not None:
        return grid, end
    return _table_stanzas(header, body, codes, links), end


def _pad_row(row: list[str], width: int) -> list[str]:
    return (row + [""] * width)[:width]


def _cell_plain(cell: str) -> str:
    """A cell as measurable characters: markdown markers stripped, not styled."""
    text = _BREAK_RE.sub(" ", cell)
    text = _CODE_SPAN_RE.sub(lambda m: m.group(2), text)
    text = _LINK_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _BOLD_STAR.sub(r"\1", text)
    text = _BOLD_UNDER.sub(r"\1", text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    text = _ITALIC_UNDER.sub(r"\1", text)
    return " ".join(text.split())


def _cell_width(text: str) -> int:
    """Columns the terminal-ish font will actually spend on ``text``.

    Combining marks and variation selectors cost nothing; CJK and most emoji
    cost two. Getting this wrong only skews a column by a space, which is why
    an approximation is enough and a full grapheme segmenter is not worth it.
    """
    width = 0
    for char in text:
        if unicodedata.combining(char) or char in _ZERO_WIDTH:
            continue
        width += 2 if unicodedata.east_asian_width(char) in "WF" else 1
    return width


def _pad(text: str, width: int, align: str) -> str:
    slack = max(0, width - _cell_width(text))
    if align == "right":
        return " " * slack + text
    if align == "center":
        left = slack // 2
        return " " * left + text + " " * (slack - left)
    return text + " " * slack


def _table_grid(
    header: list[str], body: list[list[str]], align: list[str]
) -> str | None:
    """The aligned monospace table, or ``None`` when it is too wide to align."""
    cells = [[_cell_plain(cell) for cell in row] for row in [header, *body]]
    widths = [max(_cell_width(row[i]) for row in cells) for i in range(len(header))]
    if sum(widths) + 3 * (len(widths) - 1) > TABLE_MAX_WIDTH:
        return None

    def row_text(row: list[str]) -> str:
        # Trailing empty cells earn neither padding nor a separator: a ragged
        # row should end at its last value, not at a run of bare pipes.
        last = max((i for i, cell in enumerate(row) if cell), default=0)
        return _COLUMN.join(
            _pad(cell, width, how)
            for cell, width, how in zip(row[: last + 1], widths, align, strict=False)
        ).rstrip()

    rule = _RULE_JOINT.join("─" * width for width in widths)
    rows = [row_text(cells[0]), rule, *(row_text(row) for row in cells[1:])]
    return code_block_html("\n".join(rows))


def _table_stanzas(
    header: list[str],
    body: list[list[str]],
    codes: list[str],
    links: list[tuple[str, str]],
) -> str:
    """One stanza per row: a bold name, then ``label: value`` lines."""

    def inline(text: str) -> str:
        return _emphasis(escape(_protect(_BREAK_RE.sub(" ", text), codes, links)))

    stanzas: list[str] = []
    for row in body:
        lines: list[str] = []
        if row[0]:
            lines.append(f"<b>{inline(row[0])}</b>")
        elif header[0]:
            lines.append(f"<b>{inline(header[0])}</b>")
        for label, value in zip(header[1:], row[1:], strict=True):
            if not value:
                continue
            rendered = inline(value)
            lines.append(f"{inline(label)}: {rendered}" if label else rendered)
        if lines:
            stanzas.append("\n".join(lines))
    return "\n\n".join(stanzas)


def _protect(line: str, codes: list[str], links: list[tuple[str, str]]) -> str:
    """Replace code spans and links with placeholders before anything else.

    Code beats links beats emphasis, and neither a URL's underscores nor a code
    span's asterisks may reach the emphasis pass.
    """

    def take_code(match: re.Match[str]) -> str:
        codes.append(match.group(2))
        return f"\x00c{len(codes) - 1}\x01"

    def take_link(match: re.Match[str]) -> str:
        links.append((match.group(1), match.group(2)))
        return f"\x00l{len(links) - 1}\x01"

    return _LINK_RE.sub(take_link, _CODE_SPAN_RE.sub(take_code, line))


def _line_html(line: str) -> str:
    if _HR_RE.match(line):
        return HR_TEXT
    heading = _HEADING_RE.match(line)
    if heading:
        return f"<b>{_emphasis(escape(heading.group(2)))}</b>"
    bullet = _BULLET_RE.match(line)
    if bullet:
        return f"{bullet.group(1)}• {_emphasis(escape(bullet.group(3)))}"
    return _emphasis(escape(line))


def _emphasis(escaped: str) -> str:
    """Apply inline emphasis to **already-escaped** text.

    Longest delimiters first, and underscore emphasis needs word boundaries —
    ``session_index_at_post`` is an identifier, not three italics.
    """
    out = _BOLD_STAR.sub(r"<b>\1</b>", escaped)
    out = _BOLD_UNDER.sub(r"<b>\1</b>", out)
    out = _STRIKE_RE.sub(r"<s>\1</s>", out)
    out = _ITALIC_STAR.sub(r"<i>\1</i>", out)
    return _ITALIC_UNDER.sub(r"<i>\1</i>", out)


def _restore(html: str, codes: list[str], links: list[tuple[str, str]]) -> str:
    def expand(match: re.Match[str]) -> str:
        index = int(match.group(2))
        if match.group(1) == "c":
            return inline_code_html(codes[index]) if index < len(codes) else ""
        if index >= len(links):
            return ""
        label, url = links[index]
        inner = _restore(_emphasis(escape(label)), codes, links)
        if not is_safe_url(url):
            return f"{inner} ({escape(url)})" if inner else escape(url)
        return f'<a href="{escape_attr(url)}">{inner or escape(url)}</a>'

    return _PLACEHOLDER_RE.sub(expand, html)
