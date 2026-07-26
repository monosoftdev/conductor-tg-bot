"""The UTF-16 chunker.

Two properties are asserted on almost every case, because between them they are
the whole contract with Telegram:

* every part is valid Telegram HTML on its own (``assert_valid_telegram_html``,
  shared with ``test_html.py``), and
* no part exceeds 4096 **UTF-16 code units** — the unit Telegram counts, in
  which one astral emoji is two.

A third property runs on the long cases: concatenating the parts' plain text
reproduces the input's non-whitespace content exactly. A chunker that silently
eats a paragraph is worse than one that 400s, because nobody notices.
"""

from __future__ import annotations

import re

import pytest

from ctb.delivery.render.chunk import (
    CONTINUATION_MARKER,
    MIN_CHUNK_LIMIT,
    MessagePart,
    PartKind,
    chunk_blocks,
    chunk_html,
    document_filename,
)
from ctb.delivery.render.html import markdown_to_html, strip_html
from ctb.delivery.render.types import (
    DOCUMENT_OVERFLOW_LIMIT,
    SINGLE_MESSAGE_SOFT_LIMIT,
    TELEGRAM_TEXT_LIMIT,
    ActivityLine,
    Block,
    BlockKind,
    CodeBlock,
    DocumentBlock,
    TextBlock,
    utf16_len,
)
from tests.test_html import ADVERSARIAL, assert_valid_telegram_html

_WHITESPACE = re.compile(r"\s+")


def assert_deliverable(parts: list[str], *, limit: int = TELEGRAM_TEXT_LIMIT) -> None:
    for part in parts:
        assert_valid_telegram_html(part)
        assert utf16_len(part) <= limit, utf16_len(part)
        assert part.strip()


def dense(text: str) -> str:
    """Content with every whitespace run collapsed away, for loss checks."""
    return _WHITESPACE.sub("", text)


def rejoined(parts: list[str]) -> str:
    plain = "".join(strip_html(part) for part in parts)
    return dense(plain.replace(CONTINUATION_MARKER, ""))


# ── the basics ───────────────────────────────────────────────────────────────


def test_short_text_is_one_part() -> None:
    assert chunk_html("hello <b>world</b>") == ["hello <b>world</b>"]


def test_empty_input_produces_nothing() -> None:
    assert chunk_html("") == []
    assert chunk_html("   \n\n ") == []
    assert chunk_html("<b>  </b>") == []


def test_a_limit_below_the_floor_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        chunk_html("x" * 10_000, limit=MIN_CHUNK_LIMIT - 1)


@pytest.mark.parametrize("raw", ADVERSARIAL)
def test_adversarial_inputs_chunk_into_valid_parts(raw: str) -> None:
    assert_deliverable(chunk_html(markdown_to_html(raw)))


# ── UTF-16 arithmetic ────────────────────────────────────────────────────────


def test_astral_emoji_counts_as_two_units() -> None:
    body = "🚀" * 3000  # 6000 UTF-16 units, 3000 Python characters
    parts = chunk_html(body)
    assert_deliverable(parts)
    assert len(parts) == 2
    assert sum(utf16_len(part) for part in parts) == 6000


def test_emoji_exactly_at_the_boundary_is_never_split() -> None:
    # The emoji would start at unit 4095 and need 4096–4097. It must move
    # whole; a lone surrogate is a 400 and an unreadable reply.
    body = "a" * 4095 + "🚀" + "b" * 100
    parts = chunk_html(body)
    assert_deliverable(parts)
    assert parts[0] == "a" * 4095
    assert parts[1].startswith("🚀")
    assert rejoined(parts) == dense(body)


@pytest.mark.parametrize("offset", range(-4, 5))
def test_no_surrogate_is_ever_orphaned(offset: int) -> None:
    body = "a" * (4096 + offset) + "🚀" * 40
    for part in chunk_html(body):
        # A lone surrogate cannot survive an encode/decode round trip.
        assert part.encode("utf-16-le").decode("utf-16-le") == part


def test_an_entity_is_never_split() -> None:
    body = "x" * 4093 + "&amp;" + "y" * 50
    parts = chunk_html(body)
    assert_deliverable(parts)
    assert parts[0].endswith("x")
    assert parts[1].startswith("&amp;")


# ── split preference ─────────────────────────────────────────────────────────


def test_paragraph_breaks_are_preferred() -> None:
    # Three 1860-unit paragraphs: two fit in the first part, the third starts
    # the second part. A split mid-sentence here would be a chunker bug.
    body = "\n\n".join(f"START{i} " + "word " * 370 + f"END{i}" for i in range(3))
    parts = chunk_html(body)
    assert_deliverable(parts)
    assert len(parts) == 2
    for part in parts:
        assert part.startswith("START")
        assert re.fullmatch(r"END\d", part.rsplit(" ", 1)[-1])
    assert rejoined(parts) == dense(body)


def test_line_breaks_are_preferred_when_there_is_no_paragraph() -> None:
    body = "\n".join(f"line {index}" for index in range(1000))
    parts = chunk_html(body)
    assert_deliverable(parts)
    for part in parts[:-1]:
        assert re.fullmatch(r"line \d+", part.rsplit("\n", 1)[-1])


def test_whitespace_is_preferred_over_a_hard_cut() -> None:
    body = ("token " * 2000).strip()
    parts = chunk_html(body)
    assert_deliverable(parts)
    for part in parts:
        assert part.startswith("token")
        assert part.endswith("token")


def test_a_hard_cut_is_the_last_resort() -> None:
    body = "z" * 9000
    parts = chunk_html(body)
    assert_deliverable(parts)
    assert [utf16_len(part) for part in parts] == [4096, 4096, 808]
    assert rejoined(parts) == body


def test_nothing_is_lost_across_a_split() -> None:
    body = "\n\n".join(f"paragraph {index} " + "filler " * 40 for index in range(200))
    parts = chunk_html(body)
    assert_deliverable(parts)
    assert rejoined(parts) == dense(body)


# ── tags across boundaries ───────────────────────────────────────────────────


def test_a_tag_is_never_split() -> None:
    body = "".join(f'<a href="https://x.dev/{i}">link {i}</a> ' for i in range(400))
    parts = chunk_html(body)
    assert_deliverable(parts)
    for part in parts:
        assert part.count("<a ") == part.count("</a>")


def test_a_code_fence_spanning_a_boundary_is_closed_and_reopened() -> None:
    source = "\n".join(f"line {index} = {index} * 2" for index in range(600))
    html = markdown_to_html(f"```python\n{source}\n```")
    parts = chunk_html(html)
    assert_deliverable(parts)
    assert len(parts) > 2
    for part in parts:
        assert part.startswith('<pre><code class="language-python">')
        assert part.endswith("</code></pre>")
    for part in parts[1:]:
        assert CONTINUATION_MARKER in part
    assert rejoined(parts) == dense(source)


def test_a_blockquote_spanning_a_boundary_is_reopened() -> None:
    html = markdown_to_html("\n".join(f"> quoted line {i}" for i in range(600)))
    parts = chunk_html(html)
    assert_deliverable(parts)
    assert len(parts) > 1
    for part in parts:
        assert part.startswith("<blockquote")
        assert part.endswith("</blockquote>")
    # A prose split gets no marker; only <pre>/<code> needs one.
    assert CONTINUATION_MARKER not in "".join(parts)


def test_nested_inline_tags_are_reopened_in_order() -> None:
    html = "<b><i>" + ("deep text " * 900).strip() + "</i></b>"
    parts = chunk_html(html)
    assert_deliverable(parts)
    assert len(parts) > 1
    for part in parts:
        assert part.startswith("<b><i>")
        assert part.endswith("</i></b>")


def test_mismatched_input_still_yields_valid_parts() -> None:
    html = "<b><i>" + ("x " * 5000) + "</b></i>"
    assert_deliverable(chunk_html(html))


def test_a_tag_too_large_to_fit_loses_its_markup_not_its_words() -> None:
    # A kilobyte-long href cannot be reopened on every part without either
    # blowing the limit or emitting one character per message.
    url = "https://x.dev/" + "u" * 4300
    body = ("word " * 2000).strip()
    parts = chunk_html(f'<a href="{url}">{body}</a>')
    assert_deliverable(parts)
    assert len(parts) < 10
    assert "<a " not in "".join(parts)
    assert rejoined(parts) == dense(body)


def test_an_unaffordable_reopen_degrades_to_plain_text() -> None:
    url = "https://x.dev/" + "u" * 2100
    body = ("word " * 2000).strip()
    parts = chunk_html(f'<a href="{url}">{body}</a>')
    assert_deliverable(parts)
    assert len(parts) < 10
    assert parts[0].startswith("<a ")
    assert "<a " not in "".join(parts[1:])
    assert rejoined(parts) == dense(body)


def test_a_smaller_limit_is_respected() -> None:
    parts = chunk_html(markdown_to_html("word " * 3000), limit=MIN_CHUNK_LIMIT)
    assert_deliverable(parts, limit=MIN_CHUNK_LIMIT)
    assert len(parts) > 20


# ── the size policy ──────────────────────────────────────────────────────────


def _paragraphs(units: int) -> str:
    """Roughly ``units`` UTF-16 units of paragraph-shaped prose."""
    paragraph = ("word " * 100).strip() + "\n\n"
    return (paragraph * (units // utf16_len(paragraph) + 1)).strip()


def test_a_short_turn_is_one_message() -> None:
    parts = chunk_blocks([TextBlock(html=_paragraphs(2000))])
    assert len(parts) == 1
    assert parts[0].kind is PartKind.TEXT
    assert parts[0].utf16_length <= SINGLE_MESSAGE_SOFT_LIMIT
    assert_deliverable([parts[0].html])


def test_a_medium_turn_is_two_messages() -> None:
    body = _paragraphs(5000)
    assert SINGLE_MESSAGE_SOFT_LIMIT < utf16_len(body) <= DOCUMENT_OVERFLOW_LIMIT
    parts = chunk_blocks([TextBlock(html=body)])
    assert len(parts) == 2
    assert [part.index for part in parts] == [0, 1]
    assert_deliverable([part.html for part in parts])
    # Balanced, not 4096 + a stub.
    first, second = (part.utf16_length for part in parts)
    assert abs(first - second) < first


def test_a_long_turn_is_a_head_plus_a_document() -> None:
    body = _paragraphs(20_000)
    parts = chunk_blocks([TextBlock(html=body)], turn_id="abc:12:0")
    assert [part.kind for part in parts] == [PartKind.TEXT, PartKind.DOCUMENT]
    head, document = parts
    assert head.utf16_length <= TELEGRAM_TEXT_LIMIT
    assert re.search(r"<i>… \+\d+ more</i>$", head.html)
    assert document.filename == "turn-abc-12-0.md"
    assert dense(document.content or "") == dense(strip_html(body))
    assert_deliverable([head.html])


def test_document_filename_is_sanitised() -> None:
    assert document_filename("") == "turn.md"
    assert document_filename("../../etc/passwd") == "turn-etc-passwd.md"
    assert document_filename("a" * 200).endswith(".md")
    assert len(document_filename("a" * 200)) <= 60


# ── code blocks ──────────────────────────────────────────────────────────────


def test_a_short_code_block_stays_inline() -> None:
    code = "\n".join(f"x{index} = {index}" for index in range(10))
    parts = chunk_blocks([CodeBlock(text=code, language="python")])
    assert len(parts) == 1
    assert parts[0].kind is PartKind.CODE
    assert 'class="language-python"' in parts[0].html
    assert_deliverable([parts[0].html])


def test_a_long_code_block_becomes_a_placeholder_plus_a_document() -> None:
    code = "\n".join(f"x{index} = {index}" for index in range(120))
    parts = chunk_blocks(
        [TextBlock(html="Here you go:"), CodeBlock(text=code, language="python")],
        turn_id="t1",
    )
    assert [part.kind for part in parts] == [PartKind.TEXT, PartKind.DOCUMENT]
    assert "[code block, 120 lines →]" in strip_html(parts[0].html)
    assert code in (parts[1].content or "")
    assert "```python" in (parts[1].content or "")
    assert_deliverable([parts[0].html])


def test_a_named_file_edit_names_itself_in_the_placeholder() -> None:
    code = "\n".join("+ added" for _ in range(60))
    parts = chunk_blocks([CodeBlock(text=code, filename="src/app.py", language="diff")])
    assert "[src/app.py, 60 lines →]" in strip_html(parts[0].html)


def test_the_document_fence_escapes_a_code_block_containing_backticks() -> None:
    code = "\n".join(["```", "nested fence"] + [f"line {i}" for i in range(60)])
    parts = chunk_blocks([CodeBlock(text=code)])
    content = parts[-1].content or ""
    assert content.startswith("````")
    assert content.endswith("````")


def test_a_two_hundred_kilobyte_diff_survives() -> None:
    diff = "\n".join(
        f'+ line {index} with <tag> & "quotes" and 🚀 ' + "payload " * 4
        for index in range(4000)
    )
    assert len(diff.encode("utf-8")) > 200_000
    parts = chunk_blocks(
        [TextBlock(html="Diff:"), CodeBlock(text=diff, language="diff")],
        turn_id="big",
    )
    assert [part.kind for part in parts] == [PartKind.TEXT, PartKind.DOCUMENT]
    assert_deliverable([parts[0].html])
    assert diff in (parts[1].content or "")


def test_a_long_code_block_that_is_shown_inline_is_chunked_safely() -> None:
    # 40 lines, but each one enormous: under the line cap, over the size cap.
    code = "\n".join("x" * 900 for _ in range(40))
    parts = chunk_blocks([CodeBlock(text=code, language="python")], turn_id="t")
    assert_deliverable([part.html for part in parts if part.html])
    assert parts[-1].kind is PartKind.DOCUMENT
    assert re.search(r"… \+\d+ more", parts[0].html)


# ── block plumbing ───────────────────────────────────────────────────────────


def test_activity_lines_never_reach_the_chat() -> None:
    blocks: list[Block] = [
        ActivityLine(text="running pytest"),
        TextBlock(html="done"),
    ]
    parts = chunk_blocks(blocks)
    assert [part.html for part in parts] == ["done"]


def test_only_activity_lines_produce_nothing() -> None:
    assert chunk_blocks([ActivityLine(text="running pytest")]) == []
    assert chunk_blocks([]) == []


def test_document_blocks_pass_through_in_order() -> None:
    blocks: list[Block] = [
        TextBlock(html="see attached"),
        DocumentBlock(filename="a.diff", content="--- a\n+++ b", caption="the diff"),
    ]
    parts = chunk_blocks(blocks)
    assert [part.kind for part in parts] == [PartKind.TEXT, PartKind.DOCUMENT]
    assert parts[1].filename == "a.diff"
    assert parts[1].caption == "the diff"


def test_an_oversized_caption_is_truncated_to_the_caption_limit() -> None:
    blocks: list[Block] = [
        DocumentBlock(filename="a.md", content="x", caption="🚀" * 900)
    ]
    caption = chunk_blocks(blocks)[0].caption or ""
    assert utf16_len(caption) <= 1024
    assert caption.endswith("…")


def test_part_indices_are_contiguous_from_zero() -> None:
    parts = chunk_blocks(
        [
            TextBlock(html=_paragraphs(20_000)),
            DocumentBlock(filename="a.md", content="x"),
        ],
        turn_id="t",
    )
    assert [part.index for part in parts] == list(range(len(parts)))


def test_source_message_id_and_silence_carry_through() -> None:
    parts = chunk_blocks([TextBlock(html="hi", source_message_id="s:1:0", silent=True)])
    assert parts[0].source_message_id == "s:1:0"
    assert parts[0].silent is True


def test_every_part_carries_its_own_plain_fallback() -> None:
    parts = chunk_blocks([TextBlock(html=markdown_to_html("**b** and `c` and <x>"))])
    assert parts[0].plain == "b and c and <x>"


def test_payload_round_trips_through_the_deliveries_row() -> None:
    original = chunk_blocks(
        [TextBlock(html=_paragraphs(20_000), source_message_id="s:9:0")],
        turn_id="t9",
    )
    for part in original:
        assert MessagePart.from_payload(part.payload()) == part


def test_from_payload_tolerates_a_broken_row() -> None:
    part = MessagePart.from_payload({"kind": "nonsense", "html": "hi"})
    assert part.kind is PartKind.TEXT
    assert part.html == "hi"


def test_content_hash_is_stable_and_discriminating() -> None:
    first = MessagePart(html="a", kind=PartKind.TEXT)
    same = MessagePart(html="a", kind=PartKind.TEXT, index=3)
    other = MessagePart(html="b", kind=PartKind.TEXT)
    assert first.content_hash == same.content_hash
    assert first.content_hash != other.content_hash


def test_error_blocks_are_delivered_like_any_other_text() -> None:
    # The renderer decides visibility; the chunker never drops a block.
    block = TextBlock(html="<b>boom</b>", kind=BlockKind.ERROR)
    assert chunk_blocks([block])[0].html == "<b>boom</b>"
