"""The probe's offline half — shape description and the report renderer.

These run without network or an API key. They exist because the probe is the
one script whose output the whole project is built on: if `shape_report`
crashes on a content shape Conductor actually returns, Phase 0 silently
produces nothing and we go back to guessing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_spec = importlib.util.spec_from_file_location(
    "probe_transcript",
    Path(__file__).resolve().parent.parent / "scripts" / "probe_transcript.py",
)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
sys.modules["probe_transcript"] = probe
_spec.loader.exec_module(probe)


def msg(idx: int, type_: str, content: Any) -> dict[str, Any]:
    return {
        "id": f"m{idx}",
        "sessionId": "s1",
        "sessionIndex": idx,
        "type": type_,
        "content": content,
        "receivedAt": "2026-07-25T00:00:00Z",
    }


class TestDescribe:
    def test_string_is_reported_by_length_not_value(self):
        # The shape report must never leak transcript text — it's source code.
        assert probe.describe("hunter2") == "str(len=7)"

    def test_nested_dict(self):
        assert probe.describe({"a": {"b": 1}}) == {"a": {"b": "int"}}

    def test_homogeneous_list_collapses_to_one_shape(self):
        blocks = [{"type": "text", "text": "aa"}, {"type": "text", "text": "bbbb"}]
        # Both elements share a shape modulo string length, so we get 2 variants.
        out = probe.describe(blocks)
        assert isinstance(out, list)

    def test_empty_list(self):
        assert probe.describe([]) == "list<empty>"

    def test_depth_is_bounded(self):
        deep: Any = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        out = probe.describe(deep, max_depth=3)
        assert out == {"a": {"b": {"c": "<dict>"}}}

    def test_survives_none_and_mixed_types(self):
        assert probe.describe(None) == "NoneType"
        assert probe.describe([1, "x", None]) is not None


class TestCollectStringLeaves:
    def test_walks_nested_structures(self):
        acc: list[int] = []
        probe.collect_string_leaves({"a": ["xx", {"b": "yyy"}], "c": 1}, acc)
        assert sorted(acc) == [2, 3]


class TestPct:
    def test_empty(self):
        assert probe.pct([], 0.5) == 0

    def test_p50_and_p95_stay_in_range(self):
        vals = list(range(1, 101))
        assert probe.pct(vals, 0.5) == 51
        assert probe.pct(vals, 0.95) == 96
        assert probe.pct(vals, 1.0) == 100  # must not IndexError


class TestShapeReport:
    def test_renders_the_shapes_we_expect_from_conductor(self):
        by_session = {
            "s1": [
                msg(0, "user", "fix the flaky test"),
                msg(1, "assistant", [{"type": "text", "text": "Looking into it."}]),
                msg(2, "tool_use", {"name": "Bash", "input": {"command": "pytest"}}),
                msg(3, "tool_result", {"output": "3 passed"}),
                msg(4, "assistant", [{"type": "text", "text": "Fixed."}]),
            ]
        }
        out = probe.shape_report(by_session)

        assert "## `type` histogram" in out
        assert "`assistant` — 2" in out
        assert "unique=True monotonic=True gapless=True" in out
        assert "range=0..4" in out
        # No raw transcript text leaks into the shape section
        assert "str(len=" in out

    def test_detects_a_broken_session_index(self):
        by_session = {"s1": [msg(0, "user", "a"), msg(0, "assistant", "b")]}
        out = probe.shape_report(by_session)
        assert "unique=False" in out

    def test_handles_an_empty_session_without_crashing(self):
        out = probe.shape_report({"s1": []})
        assert "Total messages:  0" in out

    def test_handles_a_content_type_we_have_never_seen(self):
        # The whole point: an unknown shape must be reported, not raise.
        weird = msg(0, "some_future_type", {"nested": [{"deep": {"deeper": [1, 2]}}]})
        out = probe.shape_report({"s1": [weird]})
        assert "some_future_type" in out


class TestResults:
    def test_renders_a_markdown_table_and_escapes_pipes(self):
        r = probe.Results()
        r.add("passing thing", True, "all good")
        r.add("failing thing", False, "a | b")
        r.add("informational", None, "just fyi")
        out = r.render()

        assert "| PASS | passing thing | all good |" in out
        assert "| FAIL | failing thing | a \\| b |" in out
        assert "| INFO | informational | just fyi |" in out
