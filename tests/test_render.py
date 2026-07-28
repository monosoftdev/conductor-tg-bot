"""Adapter registry tests.

Three fixture corpora, three jobs:

* ``probe_verified.jsonl`` — real envelope *shapes* from the Phase 0 live run,
  with every identifier replaced before the repository was made public. These
  pin the shapes we *know*.
* ``synthetic_unverified.jsonl`` — hand-written tool calls, diffs, reasoning
  and failures. The org had no sessions carrying those when the probe ran, so
  they encode the Claude Code block schema as a documented guess.
* ``adversarial_unverified.jsonl`` — hostile input. The registry's contract is
  that a renderer bug never stalls delivery, and the only way to believe that
  is to try to break it.

Every fixture is rendered at every verbosity, and the invariant checked across
all of them is the one from CLAUDE.md: **nothing raises, ever**.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest

from ctb.conductor.models import TranscriptMessage
from ctb.delivery.render.adapters import (
    Adapter,
    AssistantAdapter,
    UnknownAdapter,
    activity_text,
    best_effort_text,
    default_adapters,
    describe_file_edit,
    diff_document,
    shape_signature,
)
from ctb.delivery.render.adapters.base import (
    Segment,
    plain_html,
    split_fenced,
    truncate_text,
)
from ctb.delivery.render.adapters.extract import is_machine_token
from ctb.delivery.render.adapters.result import format_duration
from ctb.delivery.render.adapters.shapes import preamble_span
from ctb.delivery.render.registry import (
    Registry,
    RenderResult,
    default_registry,
    render_message,
    reset_default_registry,
    set_default_registry,
)
from ctb.delivery.render.types import (
    ActivityLine,
    Block,
    BlockKind,
    CodeBlock,
    DocumentBlock,
    RenderContext,
    TextBlock,
    Verbosity,
    utf16_len,
)
from ctb.turn.cursor import preview_text

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VERBOSITIES = (Verbosity.QUIET, Verbosity.NORMAL, Verbosity.VERBOSE)


# ── fixture loading ──────────────────────────────────────────────────────────


def load(name: str) -> dict[str, TranscriptMessage]:
    """Load a fixture file, keyed by its ``_case`` marker (or by index)."""
    path = FIXTURES / name
    messages: dict[str, TranscriptMessage] = {}
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        raw: dict[str, Any] = json.loads(line)
        case = raw.get("_case") or f"{path.stem}-{index}"
        messages[case] = TranscriptMessage.model_validate(raw)
    return messages


PROBE = load("probe_verified.jsonl")
SYNTHETIC = load("synthetic_unverified.jsonl")
ADVERSARIAL = load("adversarial_unverified.jsonl")
ALL_MESSAGES: dict[str, TranscriptMessage] = {
    **{f"probe:{k}": v for k, v in PROBE.items()},
    **{f"synthetic:{k}": v for k, v in SYNTHETIC.items()},
    **{f"adversarial:{k}": v for k, v in ADVERSARIAL.items()},
}


def render(
    message: TranscriptMessage, verbosity: Verbosity = Verbosity.NORMAL
) -> RenderResult:
    return default_registry().render(message, RenderContext(verbosity=verbosity))


def chat_text(result: RenderResult) -> str:
    """Everything the chat would show, concatenated, for substring checks."""
    parts: list[str] = []
    for block in result.chat:
        match block:
            case TextBlock(html=html):
                parts.append(html)
            case CodeBlock(text=text):
                parts.append(text)
            case DocumentBlock(content=content, caption=caption):
                parts.append(f"{caption or ''}\n{content}")
            case _:  # pragma: no cover - ActivityLine is not a chat block
                pass
    return "\n".join(parts)


def kinds(result: RenderResult) -> set[BlockKind]:
    return {block.kind for block in result.chat}


# ── the invariant: nothing raises, for any fixture, at any verbosity ─────────


@pytest.mark.parametrize("case", sorted(ALL_MESSAGES))
@pytest.mark.parametrize("verbosity", VERBOSITIES)
def test_every_fixture_renders_without_raising(case: str, verbosity: Verbosity) -> None:
    result = render(ALL_MESSAGES[case], verbosity)
    assert isinstance(result, RenderResult)
    assert result.adapter
    for block in result.blocks:
        assert isinstance(block, TextBlock | CodeBlock | DocumentBlock | ActivityLine)
        assert block.source_message_id == ALL_MESSAGES[case].id


#: Telegram's supported subset, plus the attribute forms the renderer emits.
_ALLOWED_TAG = re.compile(
    r"</?(?:b|i|u|s|code|pre|blockquote|a|span|tg-spoiler)"
    r'(?: expandable| class="language-[A-Za-z0-9_+#.-]{1,20}"'
    r'| class="tg-spoiler"| href="[^"<>]*")?>'
)
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+);)")


@pytest.mark.parametrize("case", sorted(ALL_MESSAGES))
@pytest.mark.parametrize("verbosity", VERBOSITIES)
def test_text_blocks_are_valid_telegram_html(case: str, verbosity: Verbosity) -> None:
    """No unescaped ``<`` or ``&`` survives, and no tag outside the subset.

    HTML is the only parse mode we use precisely because this check is
    tractable; one unescaped angle bracket is a 400 and a lost reply.
    """
    for block in render(ALL_MESSAGES[case], verbosity).blocks:
        if not isinstance(block, TextBlock):
            continue
        remainder = _ALLOWED_TAG.sub("", block.html)
        assert "<" not in remainder, block.html
        assert ">" not in remainder, block.html
        assert _BARE_AMP.search(remainder) is None, block.html


# ── the verified probe corpus ────────────────────────────────────────────────


def test_probe_user_echo_is_suppressed() -> None:
    result = render(PROBE["probe_verified-0"])
    assert result.adapter == "user_echo"
    assert result.blocks == ()


def test_probe_assistant_answer_is_shown_at_every_verbosity() -> None:
    message = PROBE["probe_verified-4"]
    for verbosity in VERBOSITIES:
        result = render(message, verbosity)
        assert result.adapter == "assistant"
        assert "probe-4c933126" in chat_text(result)
        assert BlockKind.ANSWER in kinds(result)


def test_probe_answer_is_delivered_exactly_once_per_turn() -> None:
    """The ``result`` payload repeats the answer; the chat must not.

    This is the duplicate-reply trap: ``rawPayload.result`` on a successful
    turn is the same string the ``assistant`` message already carried.
    """
    seen = sum(
        chat_text(render(message)).count("probe-4c933126") for message in PROBE.values()
    )
    assert seen == 1


def test_probe_result_never_puts_done_on_a_running_card() -> None:
    """A ``result`` is accounting, not activity.

    Emitting ``✅ done · 45.8s`` as an activity line put it beside a card that
    still read ``working 20s`` and still carried Stop — one card, two states,
    two clocks. The turn is over when the state machine says it is over.
    """
    result = render(PROBE["probe_verified-5"])
    assert result.adapter == "result"
    assert result.chat == ()
    assert result.activity == ()


def test_probe_result_summary_is_verbose_chat_only() -> None:
    result = render(PROBE["probe_verified-5"], Verbosity.VERBOSE)
    assert result.activity == ()
    text = chat_text(result)
    assert "done · 3.7s" in text
    # Phone-sized money: two decimals, never four.
    assert "$0.05" in text


def test_probe_system_and_rate_limit_are_suppressed_below_verbose() -> None:
    for case in ("probe_verified-1", "probe_verified-2", "probe_verified-3"):
        for verbosity in (Verbosity.QUIET, Verbosity.NORMAL):
            assert render(PROBE[case], verbosity).chat == ()
    init = render(PROBE["probe_verified-2"], Verbosity.VERBOSE)
    assert init.adapter == "system"
    assert "claude-sonnet-4-6" in chat_text(init)
    allowed = render(PROBE["probe_verified-3"], Verbosity.VERBOSE)
    assert allowed.adapter == "rate_limit"
    assert "allowed" in chat_text(allowed)


def test_probe_messages_are_never_unknown() -> None:
    """Every shape the live API produced is classified, not guessed at."""
    for case, message in PROBE.items():
        result = render(message)
        assert result.adapter != "unknown", case
        assert result.unknown == ()


# ── the visibility table from PLAN §Adapters ─────────────────────────────────


def test_thinking_is_suppressed_until_verbose_then_collapsed() -> None:
    message = SYNTHETIC["thinking_then_answer"]
    for verbosity in (Verbosity.QUIET, Verbosity.NORMAL):
        assert BlockKind.THINKING not in kinds(render(message, verbosity))
    verbose = render(message, Verbosity.VERBOSE)
    thinking = [b for b in verbose.chat if b.kind is BlockKind.THINKING]
    assert len(thinking) == 1
    assert isinstance(thinking[0], TextBlock)
    assert thinking[0].html.startswith("<blockquote expandable>")
    assert thinking[0].silent is True


def test_answer_splits_fenced_code_into_a_code_block() -> None:
    result = render(SYNTHETIC["thinking_then_answer"])
    code = [b for b in result.chat if isinstance(b, CodeBlock)]
    assert len(code) == 1
    assert code[0].language == "python"
    assert "TelegramBadRequest" in code[0].text
    # Raw, not escaped: the chunker splits it and the outbox escapes at send.
    assert "&lt;" not in code[0].text
    text = [b for b in result.chat if isinstance(b, TextBlock)]
    assert len(text) == 2
    assert "One retry, never two." in text[-1].html


def test_redacted_thinking_is_labelled_not_dumped() -> None:
    verbose = render(SYNTHETIC["redacted_thinking"], Verbosity.VERBOSE)
    assert "[redacted reasoning]" in chat_text(verbose)
    assert "EvgBCkYIARgCKkDd" not in chat_text(verbose)


def test_tool_call_feeds_the_card_and_never_the_chat() -> None:
    result = render(SYNTHETIC["tool_bash"])
    assert result.chat == ()
    assert result.activity == ("Bash · .venv/bin/python -m pytest tests/ -q",)
    # The activity line survives even at quiet: a working turn with no
    # activity looks frozen, and the card is not the chat.
    assert render(SYNTHETIC["tool_bash"], Verbosity.QUIET).activity
    verbose = render(SYNTHETIC["tool_bash"], Verbosity.VERBOSE)
    assert BlockKind.TOOL in kinds(verbose)


def test_read_is_a_tool_call_not_a_file_edit() -> None:
    result = render(SYNTHETIC["tool_read"])
    assert result.chat == ()
    assert result.activity == ("Read · /repo/src/ctb/delivery/outbox.py",)


def test_tool_result_is_suppressed_until_verbose_then_clipped() -> None:
    message = SYNTHETIC["tool_result_text"]
    assert render(message).blocks == ()
    verbose = render(message, Verbosity.VERBOSE)
    assert "<pre>41 passed in 2.13s</pre>" in chat_text(verbose)
    assert BlockKind.TOOL_RESULT in kinds(verbose)


def test_failed_tool_result_is_marked_but_not_promoted_to_error() -> None:
    """Agents retry failed tools constantly; that is not a failed turn."""
    assert render(SYNTHETIC["tool_result_error"]).chat == ()
    verbose = render(SYNTHETIC["tool_result_error"], Verbosity.VERBOSE)
    assert BlockKind.ERROR not in kinds(verbose)
    assert "⚠️" in chat_text(verbose)
    assert "E501" in chat_text(verbose)


@pytest.mark.parametrize(
    ("case", "path", "added", "removed"),
    [
        ("edit_replace", "src/ctb/delivery/outbox.py", 6, 1),
        ("edit_write", "src/ctb/delivery/render/chunk.py", 5, 0),
        ("edit_patch", "docs/ROADMAP.md", 2, 1),
        ("edit_structured_patch", "README.md", 2, 1),
    ],
)
def test_file_edit_is_one_line_with_counts(
    case: str, path: str, added: int, removed: int
) -> None:
    result = render(SYNTHETIC[case])
    assert kinds(result) == {BlockKind.DIFF}
    html = chat_text(result)
    assert path in html
    assert f"+{added}" in html
    assert f"−{removed}" in html  # U+2212, per PLAN's `path +12 −3`


def test_file_edit_body_is_an_attachment_only_when_verbose() -> None:
    normal = render(SYNTHETIC["edit_replace"])
    assert not any(isinstance(b, DocumentBlock) for b in normal.chat)
    verbose = render(SYNTHETIC["edit_replace"], Verbosity.VERBOSE)
    documents = [b for b in verbose.chat if isinstance(b, DocumentBlock)]
    assert len(documents) == 1
    assert documents[0].filename == "outbox.py.diff"
    assert "link_preview_options" in documents[0].content


def test_diff_document_is_reachable_for_the_show_diff_button() -> None:
    """The chat gets the one-liner; the button builds the document on demand."""
    block = SYNTHETIC["edit_replace"].blocks[0]
    edit = describe_file_edit(block)
    assert edit is not None
    document = diff_document(edit, source_message_id="m1")
    assert document is not None
    assert document.filename == "outbox.py.diff"
    assert document.caption == edit.summary
    assert edit.summary == "src/ctb/delivery/outbox.py +6 −1"


def test_file_edit_is_hidden_at_quiet() -> None:
    assert render(SYNTHETIC["edit_replace"], Verbosity.QUIET).chat == ()


def test_multi_edit_counts_every_hunk() -> None:
    edit = describe_file_edit(SYNTHETIC["edit_multi"].blocks[0])
    assert edit is not None
    assert edit.path == "src/ctb/turn/machine.py"
    assert (edit.added, edit.removed) == (2, 2)


def test_one_message_can_narrate_edit_and_act() -> None:
    """``[text, tool_use, tool_use]``: the edit lands, the narration does not.

    The text sits in front of a tool call, so it is preamble — it becomes card
    activity instead of a bubble. The diff one-liner is still the chat's
    "did it work" signal.
    """
    result = render(SYNTHETIC["mixed_blocks"])
    assert kinds(result) == {BlockKind.DIFF}
    assert "Patching the chunker" not in chat_text(result)
    assert "chunk.py" in chat_text(result)
    assert result.activity == (
        "Patching the chunker and re-running:",
        "Bash · pytest tests/test_chunk.py -q",
    )


# ── preamble narration: the six-bubble turn ──────────────────────────────────


def narrating_tool_call(index: int, narration: str) -> TranscriptMessage:
    """One envelope of a Claude turn: a line of narration, then a tool call."""
    return TranscriptMessage.model_validate(
        {
            "id": f"m{index}",
            "sessionId": "s1",
            "sessionIndex": index,
            "type": "agent",
            "content": {
                "rawPayload": {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": narration},
                            {
                                "type": "tool_use",
                                "id": f"toolu_{index}",
                                "name": "Read",
                                "input": {"file_path": f"tests/test_{index}.py"},
                            },
                        ],
                    },
                }
            },
        }
    )


def plain_answer(text: str) -> TranscriptMessage:
    return TranscriptMessage.model_validate(
        {
            "id": "final",
            "sessionId": "s1",
            "sessionIndex": 99,
            "type": "agent",
            "content": {
                "rawPayload": {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            },
        }
    )


NARRATION = (
    "I'll start by looking at the test file and its fixtures.",
    "Let me check how the fixture is scoped.",
    "Now let me run the test a few times to see the flake rate.",
    "The failure looks timing-related. Let me look at the event loop setup.",
    "I'll check the conftest for a shared event loop fixture.",
    "Found it. Let me fix the fixture scope and re-run.",
)


def test_a_six_tool_turn_is_one_bubble() -> None:
    """Six narrated tool calls plus an answer must cost one push, not seven.

    Each tool call arrives as its own envelope carrying a line of narration,
    and each chat block is a separate Telegram message and a separate buzz.
    """
    turn = [narrating_tool_call(i, line) for i, line in enumerate(NARRATION)]
    turn.append(plain_answer("Fixed: the event_loop fixture was function-scoped."))
    chat = [block for message in turn for block in render(message).chat]
    assert len(chat) == 1
    assert "event_loop fixture" in chat_text(render(turn[-1]))


def test_narration_still_reaches_the_status_card() -> None:
    result = render(narrating_tool_call(1, NARRATION[1]))
    assert result.chat == ()
    assert NARRATION[1] in result.activity


def test_narration_is_reachable_at_verbose() -> None:
    """Suppressing noise is fine; losing the agent's words is not."""
    result = render(narrating_tool_call(1, NARRATION[1]), Verbosity.VERBOSE)
    assert kinds(result) == {BlockKind.THINKING, BlockKind.TOOL}
    assert NARRATION[1] in chat_text(result)
    assert all(block.silent for block in result.chat)


def test_a_plain_answer_with_no_tool_call_is_never_demoted() -> None:
    for verbosity in VERBOSITIES:
        result = render(plain_answer("Done — the flake is gone."), verbosity)
        assert kinds(result) == {BlockKind.ANSWER}
        assert "the flake is gone" in chat_text(result)


def test_text_after_the_last_tool_call_is_kept_as_the_answer() -> None:
    """``[text, tool_use, text]`` — only what precedes the call is preamble.

    Nothing guarantees an agent puts its tool calls last, and losing an answer
    is unrecoverable, so the demotion stops at the last ``tool_use``.
    """
    message = TranscriptMessage.model_validate(
        {
            "id": "m-both",
            "sessionId": "s1",
            "sessionIndex": 5,
            "type": "agent",
            "content": {
                "rawPayload": {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Checking one more thing."},
                            {
                                "type": "tool_use",
                                "id": "toolu_x",
                                "name": "Bash",
                                "input": {"command": "pytest -q"},
                            },
                            {"type": "text", "text": "All 12 tests pass now."},
                        ],
                    },
                }
            },
        }
    )
    result = render(message)
    assert kinds(result) == {BlockKind.ANSWER}
    assert "All 12 tests pass now." in chat_text(result)
    assert "Checking one more thing" not in chat_text(result)


def test_preamble_span_counts_up_to_the_last_tool_call() -> None:
    assert preamble_span([]) == 0
    assert preamble_span([{"type": "text", "text": "hi"}]) == 0
    assert (
        preamble_span(
            [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"type": "text", "text": "done"},
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    ("case", "needle"),
    [
        ("result_error", "Codex ChatGPT auth not found"),
        ("result_error_traceback", "RuntimeError"),
        ("rate_limit_blocked", "blocked"),
        ("session_error_event", "workspace stopped responding"),
    ],
)
def test_errors_are_shown_at_every_verbosity(case: str, needle: str) -> None:
    for verbosity in VERBOSITIES:
        result = render(SYNTHETIC[case], verbosity)
        assert BlockKind.ERROR in kinds(result), (case, verbosity)
        assert needle in chat_text(result), (case, verbosity)


def test_error_adapter_wins_over_the_result_adapter() -> None:
    assert render(SYNTHETIC["result_error"]).adapter == "error"
    assert render(SYNTHETIC["result_error_traceback"]).adapter == "error"


def test_multiline_error_body_becomes_a_code_block() -> None:
    result = render(SYNTHETIC["result_error_traceback"])
    assert [type(b) for b in result.chat] == [TextBlock, CodeBlock]
    assert "Turn failed" in chat_text(result)


def test_rate_limit_that_does_not_bite_stays_quiet() -> None:
    assert render(PROBE["probe_verified-3"], Verbosity.NORMAL).chat == ()


def test_server_tool_use_is_still_a_tool_call() -> None:
    result = render(SYNTHETIC["tool_web_search"])
    assert result.chat == ()
    assert result.activity == ("web_search · telegram sendMessage entity parse error",)


# ── no machine identifiers, ever ─────────────────────────────────────────────

#: Exhibit A, verbatim in shape: the assistant envelope whose ``tool_use`` block
#: produced ``🤖 claude-opus-5 msg_011Cd… message assistant tool_use toolu_01Jz…
#: Bash git add app/models/org.py && git commit -q -m "$(cat <<'EOF' chore:…``
#: in a live adopt snapshot.
EXHIBIT_A: Final[dict[str, Any]] = {
    "type": "agentMessage",
    "turnId": "msg_011CdRjDXXYG6KcJeuk1oXiu",
    "rawPayload": {
        "type": "assistant",
        "message": {
            "id": "msg_011CdRjDXXYG6KcJeuk1oXiu",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01Jzh4cfPmYAZJLZK4CKTXN1",
                    "name": "Bash",
                    "input": {
                        "command": (
                            "git add app/models/org.py && git commit -q -m "
                            "\"$(cat <<'EOF'\n"
                            "chore: add hello world comment to Org model "
                            "(Conductor)\n"
                            "EOF\n"
                            ')"'
                        ),
                        "description": "Commit the Org model comment",
                    },
                }
            ],
            "usage": {"input_tokens": 4, "output_tokens": 91},
        },
        "session_id": "5b1f0c62-4f7a-4d1e-9b3a-4c9d2e6f8a11",
    },
}

#: Substrings that must never survive to a chat surface.
_MACHINE_SUBSTRINGS: Final = (
    "msg_011",
    "toolu_",
    "5b1f0c62",
    "claude-opus-5",
)
#: Protocol vocabulary that must never be read as prose.
_DISCRIMINATOR_SUBSTRINGS: Final = ("tool_use", "assistant", "message", "user")


def _exhibit_a() -> TranscriptMessage:
    return TranscriptMessage(
        id="env-exhibit-a",
        session_id="s-exhibit",
        session_index=7,
        type="agentMessage",
        content=json.loads(json.dumps(EXHIBIT_A)),
    )


def test_exhibit_a_never_reaches_a_preview_as_identifiers() -> None:
    """The adopt snapshot line for a tool call names the tool, not the tokens."""
    line = preview_text(_exhibit_a())

    assert line == "Bash · git add app/models/org.py"
    assert "\n" not in line
    for token in _MACHINE_SUBSTRINGS:
        assert token not in line
    for word in _DISCRIMINATOR_SUBSTRINGS:
        assert word not in line
    # And never the heredoc.
    assert "EOF" not in line and "<<" not in line


def test_exhibit_a_best_effort_text_emits_no_identifiers() -> None:
    """Even the shape-blind walker refuses ids and discriminator words.

    ``best_effort_text`` is the last resort for shapes nobody has seen. Its
    ``interesting`` flag used to be sticky, so entering ``rawPayload.message``
    made every leaf beneath it prose — the model id, ``msg_…``, ``tool_use``,
    ``assistant`` and the raw heredoc, in that order.
    """
    text = best_effort_text(EXHIBIT_A)

    for token in _MACHINE_SUBSTRINGS:
        assert token not in text
    for word in _DISCRIMINATOR_SUBSTRINGS:
        assert word not in text


def test_exhibit_a_renders_as_one_activity_line() -> None:
    result = render(_exhibit_a())
    assert result.chat == ()
    assert result.activity == ("Bash · git add app/models/org.py",)


@pytest.mark.parametrize(
    "token",
    [
        "msg_011CdRjDXXYG6KcJeuk1oXiu",
        "toolu_01Jzh4cfPmYAZJLZK4CKTXN1",
        "sevt_01ABCdefGHIjklMNO234",
        "5b1f0c62-4f7a-4d1e-9b3a-4c9d2e6f8a11",
        "a3f91c02be77d4e15b8c",
        "deadbeefdeadbeefdead",
    ],
)
def test_identifier_shapes_are_recognised(token: str) -> None:
    assert is_machine_token(token)
    assert best_effort_text({"text": token}) == ""


@pytest.mark.parametrize(
    "prose",
    [
        "chore_addhelloworld",
        "Deployed to production at last",
        "src/ctb/delivery/render.py",
        "1200",
        "ValueError: expected 3 arguments",
    ],
)
def test_prose_is_not_mistaken_for_an_identifier(prose: str) -> None:
    assert not is_machine_token(prose)
    assert best_effort_text({"text": prose}) == prose


def test_meta_keys_never_carry_prose_but_still_yield_nested_text() -> None:
    assert best_effort_text({"type": "widget", "role": "user"}) == ""
    # Interest is switched off for the subtree, not the subtree skipped.
    assert best_effort_text({"metadata": {"text": "still readable"}}) == (
        "still readable"
    )


# ── unknown content: counted, never crashed ──────────────────────────────────


def test_unknown_type_with_text_is_extracted_and_recorded() -> None:
    result = render(SYNTHETIC["unknown_with_text"])
    assert result.adapter == "unknown"
    assert "Workspace woke from sleep in 42s." in chat_text(result)
    assert len(result.unknown) == 1
    record = result.unknown[0]
    assert record.type == "workspaceEvent"
    assert record.shape_signature
    assert record.session_id == SYNTHETIC["unknown_with_text"].session_id
    assert record.message_id == SYNTHETIC["unknown_with_text"].id
    assert record.reason == ""


def test_unknown_type_without_text_is_silent_but_counted() -> None:
    result = render(SYNTHETIC["unknown_silent"])
    assert result.adapter == "unknown"
    assert result.blocks == ()
    assert len(result.unknown) == 1
    assert result.unknown[0].type == "telemetry"


def test_a_wholly_unknown_shape_degrades_without_raising_or_leaking_ids() -> None:
    """The last resort stays a last resort: no soup, no tokens, still counted.

    Nothing here may raise. Losing an unrecognised payload's wording is fine;
    printing its ids at the owner is not.
    """
    message = TranscriptMessage(
        id="env-mystery",
        session_id="s-mystery",
        session_index=11,
        type="quantumEvent",
        content={
            "type": "quantumEvent",
            "eventId": "sevt_01ABCdefGHIjklMNO234",
            "requestId": "5b1f0c62-4f7a-4d1e-9b3a-4c9d2e6f8a11",
            "payload": {"role": "assistant", "kind": "tool_use"},
            "summary": "The workspace finished updating.",
        },
    )

    for verbosity in Verbosity:
        result = render(message, verbosity)
        assert result.adapter == "unknown"
        assert len(result.unknown) == 1
        assert result.unknown[0].type == "quantumEvent"
        text = chat_text(result)
        assert "sevt_" not in text
        assert "5b1f0c62" not in text
        assert "tool_use" not in text
        assert "assistant" not in text
    assert "The workspace finished updating." in chat_text(render(message))


def test_unknown_record_carries_no_content() -> None:
    """``unknown_content_types`` stores a pointer, never the user's code."""
    record = render(SYNTHETIC["unknown_with_text"]).unknown[0]
    assert "Workspace woke" not in json.dumps(record.__getstate__() or {}, default=str)
    assert "Workspace woke" not in repr(record)


def test_shape_signature_is_stable_and_discriminating() -> None:
    a = SYNTHETIC["unknown_with_text"].content
    b = SYNTHETIC["unknown_silent"].content
    assert shape_signature(a) == shape_signature(dict(a))
    assert shape_signature(a) != shape_signature(b)
    # Values never participate: only the key paths do.
    mutated = json.loads(
        json.dumps(a).replace("Workspace woke from sleep in 42s.", "x")
    )
    assert shape_signature(mutated) == shape_signature(a)


# ── a raising adapter degrades; it never propagates ──────────────────────────


class BoomOnRender(Adapter):
    name = "boom_render"

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        return True

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        raise RuntimeError("adapter is broken")


class BoomOnMatch(Adapter):
    name = "boom_match"

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        raise ValueError("probe is broken")

    def render(
        self, message: TranscriptMessage, context: RenderContext
    ) -> list[Block]:  # pragma: no cover - unreachable, matches() raises first
        return []


class JunkOutput(Adapter):
    name = "junk"

    def matches(self, msg_type: str, content: Mapping[str, Any]) -> bool:
        return True

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        return ["not a block", 42, None]  # pyright: ignore[reportReturnType]


class BrokenFallback(UnknownAdapter):
    name = "unknown"

    def render(self, message: TranscriptMessage, context: RenderContext) -> list[Block]:
        raise RuntimeError("even the safety net is broken")

    def record(self, message: TranscriptMessage, *, reason: str = "") -> Any:
        raise RuntimeError("and so is the recorder")


def test_raising_render_degrades_to_the_unknown_adapter() -> None:
    registry = Registry([BoomOnRender(), *default_adapters()])
    message = SYNTHETIC["plain_answer"]
    result = registry.render(message, RenderContext())
    assert result.degraded is True
    assert result.adapter == "unknown"
    assert "RuntimeError" in result.error
    assert result.unknown[0].reason == "boom_render raised"
    # The safety net still finds the answer.
    assert "All 41 tests pass." in chat_text(result)


def test_raising_matches_is_skipped_not_fatal() -> None:
    registry = Registry([BoomOnMatch(), *default_adapters()])
    result = registry.render(SYNTHETIC["plain_answer"], RenderContext())
    assert result.adapter == "assistant"
    assert result.degraded is False
    assert "All 41 tests pass." in chat_text(result)
    assert registry.select(SYNTHETIC["plain_answer"]).name == "assistant"


def test_adapter_returning_junk_yields_no_blocks() -> None:
    registry = Registry([JunkOutput()])
    result = registry.render(SYNTHETIC["plain_answer"], RenderContext())
    assert result.blocks == ()
    assert result.adapter == "junk"


def test_a_broken_safety_net_still_returns_a_result() -> None:
    registry = Registry([BoomOnRender(), BrokenFallback()])
    result = registry.render(SYNTHETIC["plain_answer"], RenderContext())
    assert result.blocks == ()
    assert result.degraded is True
    assert result.unknown[0].shape_signature == "unavailable"


def test_registry_always_has_a_terminating_adapter() -> None:
    registry = Registry([])
    assert isinstance(registry.adapters[-1], UnknownAdapter)
    assert registry.render(SYNTHETIC["plain_answer"]).adapter == "unknown"


def test_supplied_unknown_adapter_is_reused_not_duplicated() -> None:
    fallback = UnknownAdapter()
    registry = Registry([fallback, AssistantAdapter()])
    assert registry.fallback is fallback
    assert sum(isinstance(a, UnknownAdapter) for a in registry.adapters) == 1
    # It is the fallback, so it does not shadow the adapters behind it.
    assert registry.render(SYNTHETIC["plain_answer"]).adapter == "assistant"


# ── entry points ─────────────────────────────────────────────────────────────


def test_render_message_accepts_a_bare_verbosity() -> None:
    message = SYNTHETIC["thinking_then_answer"]
    assert render_message(message, Verbosity.VERBOSE).chat
    assert BlockKind.THINKING in {
        b.kind for b in render_message(message, Verbosity.VERBOSE).chat
    }
    assert BlockKind.THINKING not in {b.kind for b in render_message(message).chat}


def test_default_registry_is_shared_and_replaceable() -> None:
    original = default_registry()
    assert default_registry() is original
    replacement = Registry([UnknownAdapter()])
    set_default_registry(replacement)
    try:
        assert render_message(SYNTHETIC["plain_answer"]).adapter == "unknown"
    finally:
        reset_default_registry()
    assert default_registry() is not replacement


# ── helpers ──────────────────────────────────────────────────────────────────


def test_split_fenced_handles_an_unterminated_fence() -> None:
    segments = split_fenced(
        "before\n```python\ncode line\n\nstill code, fence never closed"
    )
    assert [s.is_code for s in segments] == [False, True]
    assert segments[0] == Segment(text="before", is_code=False)
    assert segments[1].language == "python"
    assert "still code" in segments[1].text


def test_split_fenced_drops_a_junk_language() -> None:
    segments = split_fenced("````not a language at all; <b>x</b>\nbody\n````")
    assert len(segments) == 1
    assert segments[0].is_code is True
    assert segments[0].language is None
    assert segments[0].text == "body"


def test_split_fenced_leaves_plain_prose_alone() -> None:
    assert split_fenced("just prose") == [Segment(text="just prose")]
    assert split_fenced("   \n\n  ") == []


def test_truncate_text_counts_utf16_code_units() -> None:
    emoji = "🚀" * 10
    assert utf16_len(emoji) == 20
    clipped = truncate_text(emoji, 10)
    assert utf16_len(clipped) <= 10
    assert clipped.endswith("…")
    # Never splits a surrogate pair.
    clipped.encode("utf-16-le").decode("utf-16-le")
    assert truncate_text("short", 100) == "short"
    assert truncate_text("anything", 0) == ""


def test_plain_html_escapes_only_what_telegram_needs() -> None:
    assert plain_html("<b>a & b</b>") == "&lt;b&gt;a &amp; b&lt;/b&gt;"
    assert plain_html('it\'s "quoted"') == 'it\'s "quoted"'


# ── the prose renderer reaches agent text, and only agent text ───────────────


def test_agent_markdown_becomes_telegram_html() -> None:
    html = chat_text(render(SYNTHETIC["markdown_answer"]))
    assert "<b>Fixed</b>" in html
    assert "<code>parse_mode</code>" in html
    assert "<i>no</i>" in html
    assert '<a href="https://example.com/plan">the plan</a>' in html
    assert "**" not in html


def test_a_path_is_never_markdown_rendered() -> None:
    """``src/ctb/__init__.py`` is a path, not a request for italics.

    This is why the markdown renderer is applied to agent prose only, and
    every structural interpolation escapes with ``plain_html`` instead.
    """
    html = chat_text(render(SYNTHETIC["edit_underscore_path"]))
    assert "<code>src/ctb/__init__.py</code>" in html
    assert "<b>init</b>" not in html
    assert "<i>" not in html


def test_tool_output_is_never_markdown_rendered() -> None:
    """A tool's own output is data; ``*`` in a shell command is a glob."""
    message = SYNTHETIC["tool_result_text"]
    verbose = render(message, Verbosity.VERBOSE)
    assert "<pre>41 passed in 2.13s</pre>" in chat_text(verbose)
    call = render(SYNTHETIC["tool_bash"], Verbosity.VERBOSE)
    assert "<code>" in chat_text(call)
    assert "<b>" not in chat_text(call)


def test_a_custom_prose_renderer_can_be_injected() -> None:
    registry = Registry(prose=lambda text: f"[{plain_html(text)}]")
    result = registry.render(SYNTHETIC["plain_answer"])
    assert chat_text(result) == "[All 41 tests pass.]"


def test_best_effort_text_walks_to_the_interesting_keys_only() -> None:
    payload = {
        "sessionId": "1111-2222",
        "uuid": "3333-4444",
        "detail": {"body": "the readable part"},
        "counters": {"tokens": 12},
    }
    found = best_effort_text(payload)
    assert found == "the readable part"
    assert best_effort_text({"counters": {"tokens": 12}}) == ""
    assert best_effort_text("a bare string") == "a bare string"


def test_best_effort_text_is_bounded() -> None:
    deep: dict[str, Any] = {"text": "top"}
    node = deep
    for _ in range(50):
        node["content"] = {"text": "x" * 10_000}
        node = node["content"]
    assert len(best_effort_text(deep, limit=100)) <= 100


def test_activity_text_is_one_plain_line() -> None:
    """One line by construction: the first line, not the whole thing flattened.

    A multi-line command collapsed into a paragraph is still a paragraph. The
    activity line says what the tool is doing, so it stops where the command
    stops being that and starts being plumbing.
    """
    line = activity_text(
        {"name": "Bash", "input": {"command": "pytest -q\n--maxfail=1"}}
    )
    assert line == "Bash · pytest -q"
    assert activity_text({"name": "Task"}) == "Task"
    assert activity_text({}) == "tool"
    long = activity_text({"name": "Bash", "input": {"command": "x" * 500}})
    assert utf16_len(long) <= 120


def test_activity_text_drops_shell_plumbing() -> None:
    heredoc = activity_text(
        {
            "name": "Bash",
            "input": {
                "command": (
                    "git add app/models/org.py && git commit -q -m \"$(cat <<'EOF'\n"
                    "chore: add hello world comment to Org model\n"
                    "EOF\n"
                    ')"'
                )
            },
        }
    )
    assert heredoc == "Bash · git add app/models/org.py"
    # A leading `cd` says where, not what.
    assert (
        activity_text({"name": "Bash", "input": {"command": "cd /srv && make test"}})
        == "Bash · make test"
    )


@pytest.mark.parametrize(
    ("ms", "expected"),
    [(820, "820ms"), (3663, "3.7s"), (80_000, "1m20s"), (7_200_000, "2h00m")],
)
def test_format_duration(ms: int, expected: str) -> None:
    assert format_duration(ms) == expected


# ── adversarial input ────────────────────────────────────────────────────────


def test_raw_html_in_an_answer_is_escaped_not_executed() -> None:
    html = chat_text(render(ADVERSARIAL["raw_html"]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;&amp;" in html


def test_astral_text_survives_and_is_measured_in_utf16() -> None:
    result = render(ADVERSARIAL["astral_emoji"])
    assert "🚀" in chat_text(result)
    for block in result.chat:
        if isinstance(block, TextBlock):
            assert utf16_len(block.html) > len(block.html)


def test_a_bare_string_content_is_not_lost() -> None:
    result = render(ADVERSARIAL["content_bare_string"])
    assert "the whole content was a string" in chat_text(result)
    assert result.unknown


def test_an_empty_envelope_is_silent_but_counted() -> None:
    result = render(ADVERSARIAL["content_empty"])
    assert result.blocks == ()
    assert result.unknown and result.unknown[0].type == "agent"


def test_blocks_delivered_as_a_bare_string_still_render() -> None:
    result = render(ADVERSARIAL["blocks_as_string"])
    assert result.adapter == "assistant"
    assert "just a string, not a list" in chat_text(result)


def test_untyped_and_malformed_blocks_degrade_per_block() -> None:
    result = render(ADVERSARIAL["untyped_blocks"])
    assert result.adapter == "assistant"
    text = chat_text(result)
    assert "no type field here" in text
    assert "buried but readable" in text


def test_null_fields_produce_nothing_rather_than_crashing() -> None:
    result = render(ADVERSARIAL["null_fields"], Verbosity.VERBOSE)
    assert result.adapter == "assistant"
    assert not any(isinstance(b, DocumentBlock) for b in result.blocks)


def test_an_edit_that_changes_nothing_reports_zeroes() -> None:
    edit = describe_file_edit(ADVERSARIAL["edit_no_change"].blocks[0])
    assert edit is not None
    assert (edit.added, edit.removed, edit.diff) == (0, 0, None)
    assert diff_document(edit) is None


def test_a_tool_call_without_arguments_is_still_an_activity_line() -> None:
    result = render(ADVERSARIAL["tool_use_no_input"])
    assert result.activity == ("Bash",)
    assert result.chat == ()


def test_a_huge_code_block_is_kept_whole_for_the_chunker() -> None:
    """The chunker owns the 40-line rule; the adapter must not pre-truncate."""
    result = render(ADVERSARIAL["huge_code_block"])
    code = [b for b in result.chat if isinstance(b, CodeBlock)]
    assert len(code) == 1
    assert code[0].line_count == 120


def test_a_huge_write_still_produces_a_one_line_summary() -> None:
    result = render(ADVERSARIAL["huge_write"])
    assert kinds(result) == {BlockKind.DIFF}
    assert "generated/big.sql" in chat_text(result)
    assert "+400" in chat_text(result)


def test_deeply_nested_tool_result_does_not_hang() -> None:
    result = render(ADVERSARIAL["tool_result_nested"], Verbosity.VERBOSE)
    assert isinstance(result, RenderResult)


def test_an_empty_envelope_type_still_yields_a_usable_record() -> None:
    result = render(ADVERSARIAL["type_empty"])
    assert result.unknown and result.unknown[0].type == "<empty>"
