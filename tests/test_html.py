"""Telegram HTML: escaping, the whitelist, markdown, and the plain fallback.

The validator at the top of this file is the contract the rest of the delivery
path depends on, so ``test_chunk.py`` imports it: *anything* we hand to
``sendMessage`` with ``parse_mode="HTML"`` must parse as balanced markup made
only of tags Telegram accepts. Telegram answers everything else with a 400, and
a 400 on a reply is a lost reply.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from ctb.delivery.render.html import (
    HR_TEXT,
    MAX_TAG_DEPTH,
    blockquote_html,
    bold,
    code_block_html,
    escape,
    escape_attr,
    inline_code_html,
    is_safe_url,
    italic,
    link_html,
    markdown_to_html,
    normalize_language,
    sanitize_html,
    spoiler,
    strikethrough,
    strip_html,
    underline,
)

#: Exactly what Telegram's HTML parser accepts, in the one spelling we emit.
ALLOWED_TAGS = frozenset(
    {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "span", "tg-spoiler"}
)
ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href"}),
    "code": frozenset({"class"}),
    "span": frozenset({"class"}),
    "blockquote": frozenset({"expandable"}),
}


class _Validator(HTMLParser):
    """Assert balance, whitelist membership and attribute legality."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
            return
        allowed = ALLOWED_ATTRS.get(tag, frozenset())
        for name, _ in attrs:
            if name not in allowed:
                self.errors.append(f"unsupported attribute {name} on <{tag}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.errors.append(f"self-closing <{tag}/> is not Telegram HTML")

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.errors.append(f"</{tag}> with nothing open")
            return
        if self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1]}>")
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def handle_entityref(self, name: str) -> None:
        if name not in ("amp", "lt", "gt", "quot"):
            self.errors.append(f"entity &{name}; is not in Telegram's set")

    def handle_charref(self, name: str) -> None:
        self.errors.append(f"numeric entity &#{name}; is not in Telegram's set")


def assert_valid_telegram_html(html: str) -> None:
    """Round-trip through an HTML parser; fail on anything Telegram would 400."""
    validator = _Validator()
    validator.feed(html)
    validator.close()
    assert not validator.errors, f"{validator.errors} in {html[:400]!r}"
    assert not validator.stack, f"unclosed {validator.stack} in {html[:400]!r}"
    # The sanitiser is idempotent, so valid output is a fixed point of it.
    assert sanitize_html(html) == html


#: Everything the plan's Phase 2 verification list names, plus the shapes the
#: probe showed us agents actually emit.
ADVERSARIAL: tuple[str, ...] = (
    "<script>alert('xss')</script>",
    "raw & ampersand < less > greater",
    "&amp; &lt; &gt; &#60; &nbsp; &bogus;",
    "unbalanced ``` backticks ` here",
    "`unclosed code span",
    "**bold *nested italic** trailing*",
    "<b><i>mismatched</b></i>",
    "<b>unclosed",
    "</i>stray close",
    '<a href="javascript:alert(1)">click</a>',
    "<img src=x onerror=alert(1)>",
    "```python\ndef f(x):\n    return x < 3 & 4\n```",
    "```\nno language\n",
    "text with 🚀 astral emoji and ✅ BMP one",
    "_snake_case_name_ and __dunder__ and a_b_c",
    "[link](https://example.com/a_b_c?x=1&y=2)",
    "> quoted\n> lines\n\nplain",
    "# Heading\n- bullet\n* star bullet\n\n---\n",
    "\x00\x07 control \x1b chars",
    "<blockquote expandable>quote</blockquote>",
    '<pre><code class="language-python">x = 1</code></pre>',
    "<b>" * 40 + "deep" + "</b>" * 40,
    "| a | b |\n|---|---|\n| <b>x</b> | `y` |",
    "| a | b |\n|:-:|--:|\n| \\| | **&** |\n| 🚀 | 日本語 |",
    "| a | b | c |\n|---|---|---|\n| only |",
    "|---|---|\n| orphan | delimiter |",
    "| " + "wide " * 40 + " | b |\n|---|---|\n| x | y |",
    "",
    "   \n\n  ",
)


@pytest.mark.parametrize("raw", ADVERSARIAL)
def test_markdown_output_is_always_valid_telegram_html(raw: str) -> None:
    assert_valid_telegram_html(markdown_to_html(raw))


@pytest.mark.parametrize("raw", ADVERSARIAL)
def test_sanitize_output_is_always_valid_telegram_html(raw: str) -> None:
    assert_valid_telegram_html(sanitize_html(raw))


@pytest.mark.parametrize("raw", ADVERSARIAL)
def test_sanitize_is_idempotent(raw: str) -> None:
    once = sanitize_html(raw)
    assert sanitize_html(once) == once


# ── escaping ─────────────────────────────────────────────────────────────────


def test_escape_covers_exactly_the_three_characters() -> None:
    assert escape("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    assert escape("'quotes\" stay") == "'quotes\" stay"


def test_escape_is_one_pass_over_raw_text() -> None:
    # A human who typed "&amp;" must see "&amp;", so raw escaping is *not*
    # idempotent — that is what sanitize_html is for.
    assert escape("&amp;") == "&amp;amp;"
    assert strip_html(escape("&amp;")) == "&amp;"


def test_escape_attr_also_escapes_quotes() -> None:
    assert escape_attr('https://x/?q="a"&b') == "https://x/?q=&quot;a&quot;&amp;b"


def test_no_double_escaping_through_the_markdown_pipeline() -> None:
    assert markdown_to_html("a & b") == "a &amp; b"
    assert markdown_to_html("if a < b && b > c") == "if a &lt; b &amp;&amp; b &gt; c"
    assert strip_html(markdown_to_html("a & b < c")) == "a & b < c"


# ── tag constructors ─────────────────────────────────────────────────────────


def test_tag_constructors_escape_their_argument() -> None:
    assert bold("a<b") == "<b>a&lt;b</b>"
    assert italic("x") == "<i>x</i>"
    assert underline("x") == "<u>x</u>"
    assert strikethrough("x") == "<s>x</s>"
    assert spoiler("x") == '<span class="tg-spoiler">x</span>'
    assert inline_code_html("<b>") == "<code>&lt;b&gt;</code>"
    assert (
        blockquote_html("hi", expandable=True)
        == "<blockquote expandable>hi</blockquote>"
    )


def test_code_block_html_uses_the_language_class() -> None:
    assert code_block_html("x = 1", "Python") == (
        '<pre><code class="language-python">x = 1</code></pre>'
    )
    assert code_block_html("x = 1") == "<pre>x = 1</pre>"


def test_code_block_html_drops_a_hostile_language() -> None:
    assert code_block_html("x", '"><script>') == "<pre>x</pre>"
    assert normalize_language("c++") == "c++"
    assert normalize_language("a" * 40) is None
    assert normalize_language(None) is None


def test_link_html_degrades_an_unsafe_url_to_text() -> None:
    assert link_html("go", "https://x.dev") == '<a href="https://x.dev">go</a>'
    assert link_html("go", "javascript:alert(1)") == "go (javascript:alert(1))"
    assert is_safe_url("mailto:a@b.c")
    assert not is_safe_url("/relative")
    assert not is_safe_url("https://x.dev/ has space")


# ── markdown ─────────────────────────────────────────────────────────────────


def test_inline_emphasis() -> None:
    assert markdown_to_html("**bold**") == "<b>bold</b>"
    assert markdown_to_html("__bold__") == "<b>bold</b>"
    assert markdown_to_html("*it*") == "<i>it</i>"
    assert markdown_to_html("_it_") == "<i>it</i>"
    assert markdown_to_html("~~gone~~") == "<s>gone</s>"


def test_identifiers_are_not_italics() -> None:
    # Agent output is full of snake_case; one stray <i> ruins a code reference.
    assert markdown_to_html("cursor_message_id") == "cursor_message_id"
    assert markdown_to_html("a_b and c_d") == "a_b and c_d"
    assert markdown_to_html("2 * 3 * 4") == "2 * 3 * 4"


def test_inline_code_beats_emphasis_and_markup() -> None:
    assert markdown_to_html("`a * b * c`") == "<code>a * b * c</code>"
    assert markdown_to_html("`<b>x</b>`") == "<code>&lt;b&gt;x&lt;/b&gt;</code>"


def test_unbalanced_backticks_stay_literal() -> None:
    assert markdown_to_html("a ` b") == "a ` b"
    assert markdown_to_html("`unclosed") == "`unclosed"


def test_fenced_code_block() -> None:
    html = markdown_to_html("before\n```py\nx < 1\n```\nafter")
    assert '<pre><code class="language-py">x &lt; 1</code></pre>' in html
    assert html.startswith("before")
    assert html.endswith("after")


def test_blank_lines_around_a_fence_are_preserved_exactly() -> None:
    assert markdown_to_html("one\n\n```py\nx\n```\n\ntwo") == (
        'one\n\n<pre><code class="language-py">x</code></pre>\n\ntwo'
    )
    assert markdown_to_html("one\n```py\nx\n```\ntwo") == (
        'one\n<pre><code class="language-py">x</code></pre>\ntwo'
    )


def test_unterminated_fence_swallows_the_rest() -> None:
    html = markdown_to_html("intro\n```\nx = 1\nmore text")
    assert html == "intro\n<pre>x = 1\nmore text</pre>"


def test_fence_content_is_not_treated_as_markdown() -> None:
    html = markdown_to_html("```\n**not bold** and _not italic_\n```")
    assert html == "<pre>**not bold** and _not italic_</pre>"


def test_links() -> None:
    assert markdown_to_html("[t](https://a.dev/x_y?a=1&b=2)") == (
        '<a href="https://a.dev/x_y?a=1&amp;b=2">t</a>'
    )
    # A URL's underscores must never reach the emphasis pass.
    assert "<i>" not in markdown_to_html("[t](https://a.dev/a_b_c_d)")


def test_unsafe_link_keeps_its_words() -> None:
    html = markdown_to_html("[click](javascript:alert)")
    assert_valid_telegram_html(html)
    assert "<a" not in html
    assert "click" in html


def test_headings_bullets_and_rules() -> None:
    html = markdown_to_html("## Title\n- one\n* two\n\n---")
    assert "<b>Title</b>" in html
    assert "• one" in html
    assert "• two" in html
    assert HR_TEXT in html


def test_blockquote_grouping_and_expansion() -> None:
    short = markdown_to_html("> a\n> b\nplain")
    assert short == "<blockquote>a\nb</blockquote>\nplain"
    long_quote = markdown_to_html("\n".join(f"> line {i}" for i in range(20)))
    assert long_quote.startswith("<blockquote expandable>")
    assert_valid_telegram_html(long_quote)


def test_control_characters_are_removed() -> None:
    assert markdown_to_html("a\x00b\x07c") == "abc"
    assert "\r" not in markdown_to_html("a\r\nb")


def test_script_is_shown_not_executed_and_not_dropped() -> None:
    html = markdown_to_html("<script>alert('x')</script>")
    assert html == "&lt;script&gt;alert('x')&lt;/script&gt;"
    assert strip_html(html) == "<script>alert('x')</script>"


# ── tables ───────────────────────────────────────────────────────────────────


def table_lines(html: str) -> list[str]:
    """The rendered table, as the reader's eye sees it: markup gone, rows kept."""
    return strip_html(html).strip().splitlines()


def test_a_narrow_table_becomes_an_aligned_monospace_grid() -> None:
    html = markdown_to_html(
        "| Name | Status | Time |\n"
        "|------|--------|------|\n"
        "| alpha | done | 1.2s |\n"
        "| beta-service | failed | 0.4s |"
    )
    assert html.startswith("<pre>")
    assert_valid_telegram_html(html)
    lines = table_lines(html)
    # Every separator sits in the same column on every row — the whole point.
    columns = {
        tuple(i for i, ch in enumerate(line) if ch == "│") for line in lines[::2]
    }
    assert len(columns) == 1
    assert lines[1] == "─────────────┼────────┼─────"
    assert lines[2] == "alpha        │ done   │ 1.2s"


def test_alignment_markers_are_honoured() -> None:
    html = markdown_to_html("| L | C | R |\n|:--|:-:|--:|\n| a | b | c |")
    assert table_lines(html)[2] == "a │ b │ c"
    wide = markdown_to_html("| L | C | R |\n|:--|:-:|--:|\n| aaa | bbb | ccc |")
    assert table_lines(wide)[2] == "aaa │ bbb │ ccc"
    assert table_lines(markdown_to_html("| L |  R |\n|:--|---:|\n| a | bb |"))[2] == (
        "a │ bb"
    )


def test_a_table_too_wide_to_align_becomes_one_stanza_per_row() -> None:
    """A phone cannot show eight columns. It can show eight lines."""
    html = markdown_to_html(
        "| Service | Region | Status | Latency | Owner |\n"
        "|---|---|---|---|---|\n"
        "| checkout-api | us-east-1 | healthy | 240ms | payments |\n"
        "| search | eu-west-2 | degraded | 1.9s | discovery |"
    )
    assert "<pre>" not in html
    assert_valid_telegram_html(html)
    assert html.startswith("<b>checkout-api</b>\nRegion: us-east-1\n")
    assert "\n\n<b>search</b>" in html


def test_cell_markup_survives_in_the_layout_that_can_show_it() -> None:
    stanzas = markdown_to_html(
        "| Service | Note | Region | Owner | Age |\n"
        "|---|---|---|---|---|\n"
        "| api | **hot** and `cached` | us-east-1 | payments | 3d |"
    )
    assert "<b>hot</b>" in stanzas
    assert "<code>cached</code>" in stanzas
    # The grid is monospace, so a cell's markers are stripped rather than styled:
    # nothing may open a tag inside <pre>.
    grid = markdown_to_html("| A | B |\n|---|---|\n| **x** | `y` |")
    assert grid == "<pre>A │ B\n──┼──\nx │ y</pre>"


def test_escaped_pipes_and_html_in_cells_are_content_not_structure() -> None:
    grid = markdown_to_html("| A | B |\n|---|---|\n| a \\| b | <i> |")
    assert table_lines(grid)[2] == "a | b │ <i>"
    assert_valid_telegram_html(grid)


def test_a_line_break_tag_flattens_rather_than_splitting_a_row() -> None:
    grid = markdown_to_html("| A | B |\n|---|---|\n| one<br>two | x |")
    assert table_lines(grid)[2] == "one two │ x"


def test_wide_characters_are_measured_at_the_width_they_occupy() -> None:
    lines = table_lines(
        markdown_to_html("| A | B |\n|---|---|\n| 日本 | x |\n| ab | y |")
    )
    assert lines[2] == "日本 │ x"
    assert lines[3] == "ab   │ y"


def test_a_ragged_row_neither_raises_nor_grows_bare_pipes() -> None:
    lines = table_lines(markdown_to_html("| a | b |\n|---|---|\n| only |\n| x | y |"))
    assert lines[2] == "only"
    assert lines[3] == "x    │ y"


def test_prose_containing_a_pipe_is_still_prose() -> None:
    """The delimiter row is what makes a table; a pipe alone is punctuation."""
    assert markdown_to_html("a | b\nc | d") == "a | b\nc | d"
    assert markdown_to_html("| a | b |") == "| a | b |"
    # Column counts that disagree are not a table either.
    assert markdown_to_html("| a | b |\n|---|") == "| a | b |\n|---|"


def test_a_table_inside_a_fence_is_left_exactly_as_written() -> None:
    html = markdown_to_html("```\n| a | b |\n|---|---|\n```")
    assert html == "<pre>| a | b |\n|---|---|</pre>"


def test_text_around_a_table_keeps_its_place() -> None:
    html = markdown_to_html("before\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nafter")
    assert html.startswith("before\n\n<pre>")
    assert html.endswith("</pre>\n\nafter")


# ── sanitiser ────────────────────────────────────────────────────────────────


def test_sanitize_balances_mismatched_nesting() -> None:
    assert sanitize_html("<b><i>x</b></i>") == "<b><i>x</i></b>"


def test_sanitize_closes_unclosed_tags() -> None:
    assert sanitize_html("<b>x") == "<b>x</b>"


def test_sanitize_drops_stray_markup_closes() -> None:
    assert sanitize_html("</b>x") == "x"


def test_sanitize_escapes_unknown_tags() -> None:
    assert sanitize_html("<div>x</div>") == "&lt;div&gt;x&lt;/div&gt;"
    assert sanitize_html("<img src=x>") == "&lt;img src=x&gt;"


def test_sanitize_normalizes_synonyms() -> None:
    assert sanitize_html("<strong>a</strong><em>b</em><del>c</del>") == (
        "<b>a</b><i>b</i><s>c</s>"
    )


def test_sanitize_keeps_markup_out_of_code() -> None:
    assert sanitize_html("<code><b>x</b></code>") == "<code>&lt;b&gt;x&lt;/b&gt;</code>"
    assert sanitize_html('<pre><code class="language-py">y</code></pre>') == (
        '<pre><code class="language-py">y</code></pre>'
    )


def test_sanitize_filters_attributes() -> None:
    assert sanitize_html('<a href="https://x.dev" onclick="x">t</a>') == (
        '<a href="https://x.dev">t</a>'
    )
    assert sanitize_html('<a href="javascript:x">t</a>') == "t"
    assert sanitize_html('<span style="color:red">t</span>') == "t"
    assert sanitize_html('<span class="tg-spoiler">t</span>') == (
        '<span class="tg-spoiler">t</span>'
    )
    assert sanitize_html('<code class="evil quoted">t</code>') == "<code>t</code>"


def test_sanitize_refuses_nested_links() -> None:
    html = sanitize_html('<a href="https://a.dev"><a href="https://b.dev">t</a></a>')
    assert html.count("<a ") == 1
    assert_valid_telegram_html(html)


def test_sanitize_caps_nesting_depth() -> None:
    html = sanitize_html(
        "<b>" * (MAX_TAG_DEPTH + 5) + "x" + "</b>" * (MAX_TAG_DEPTH + 5)
    )
    assert html.count("<b>") == MAX_TAG_DEPTH
    assert_valid_telegram_html(html)


def test_sanitize_normalizes_entities_without_double_escaping() -> None:
    assert sanitize_html("a &amp; b") == "a &amp; b"
    assert sanitize_html("&#60;tag&#62;") == "&lt;tag&gt;"
    assert sanitize_html("&bogus;") == "&amp;bogus;"


def test_sanitize_escapes_comments_and_declarations() -> None:
    assert sanitize_html("<!-- hi -->") == "&lt;!-- hi --&gt;"
    assert_valid_telegram_html(sanitize_html("<!DOCTYPE html>"))


# ── plain fallback ───────────────────────────────────────────────────────────


def test_strip_html_removes_all_markup() -> None:
    html = markdown_to_html("**bee** `cee` [tee](https://x.dev)\n```\ncode\n```")
    assert strip_html(html) == "bee cee tee\ncode"


def test_strip_html_unescapes_entities_for_parse_mode_none() -> None:
    assert strip_html(markdown_to_html("a < b & c > d")) == "a < b & c > d"


def test_strip_html_removes_the_tags_the_renderer_added() -> None:
    # Tags the *user* typed survive as literal text — that is the point of the
    # fallback. Tags the renderer added must not.
    plain = strip_html(markdown_to_html("**b** _i_ `c` > q\n```\nx\n```"))
    for tag in ALLOWED_TAGS:
        assert f"<{tag}>" not in plain
        assert f"</{tag}>" not in plain


@pytest.mark.parametrize("raw", ADVERSARIAL)
def test_plain_fallback_is_never_longer_than_the_html(raw: str) -> None:
    html = markdown_to_html(raw)
    assert len(strip_html(html)) <= len(html)
