"""The safety net: get *something* readable out of a shape nobody planned for.

Two jobs, both for content that no adapter claimed:

* :func:`best_effort_text` — a bounded recursive walk that collects strings
  living under the key names the probe actually saw (``text``, ``content``,
  ``message``, ``output``, ``body``, ``value``, ``result``), plus bare strings
  and block lists. Anything else (ids, uuids, token counts, timestamps) is
  structurally uninteresting and stays out.
* :func:`shape_signature` — a stable digest of a content mapping's key paths,
  the ``unknown_content_types`` primary key alongside ``type``. It is derived
  from *shape only*, never from values, so it is safe to store and safe to log
  while transcript content is not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "TEXT_KEYS",
    "best_effort_text",
    "shape_paths",
    "shape_signature",
]

#: Key names observed to carry human-readable text. ``_raw`` is included
#: because ``TranscriptMessage`` wraps a non-mapping ``content`` as
#: ``{"_raw": value}`` rather than raising.
TEXT_KEYS: Final = frozenset(
    {
        "_raw",
        "body",
        "content",
        "message",
        "output",
        "result",
        "stdout",
        "summary",
        "text",
        "value",
    }
)

_MAX_DEPTH: Final = 6
_MAX_PARTS: Final = 40
_PART_LIMIT: Final = 500
_DEFAULT_LIMIT: Final = 1500
_MAX_SIGNATURE_PATHS: Final = 400


def best_effort_text(value: Any, *, limit: int = _DEFAULT_LIMIT) -> str:
    """Collect readable text out of an arbitrary structure. Never raises.

    Depth-, width- and length-bounded: a pathological payload costs a fixed
    amount of work, because this runs on the poller's thread for content that
    is by definition not understood.
    """
    if isinstance(value, str):
        return _clip(value.strip(), limit)

    parts: list[str] = []
    seen: set[str] = set()

    def visit(node: Any, depth: int, interesting: bool) -> None:
        if depth > _MAX_DEPTH or len(parts) >= _MAX_PARTS:
            return
        if isinstance(node, str):
            text = node.strip()
            if interesting and text and text not in seen:
                seen.add(text)
                parts.append(text[:_PART_LIMIT])
            return
        if isinstance(node, Mapping):
            for key, child in node.items():
                if len(parts) >= _MAX_PARTS:
                    return
                key_name = key if isinstance(key, str) else ""
                visit(child, depth + 1, interesting or key_name in TEXT_KEYS)
            return
        if isinstance(node, Sequence) and not isinstance(node, str | bytes):
            for child in node:
                if len(parts) >= _MAX_PARTS:
                    return
                visit(child, depth + 1, interesting)

    visit(value, 0, False)
    return _clip("\n".join(parts), limit)


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def shape_paths(value: Any, *, max_depth: int = 4) -> tuple[str, ...]:
    """Sorted, de-duplicated dotted key paths. Values are never inspected.

    A list contributes ``key[]`` and recurses into its first mapping element
    only — a hundred identical blocks are one shape, not a hundred.
    """
    paths: set[str] = set()

    def visit(node: Any, prefix: str, depth: int) -> None:
        if depth > max_depth or len(paths) >= _MAX_SIGNATURE_PATHS:
            return
        if isinstance(node, Mapping):
            for key, child in node.items():
                key_name = key if isinstance(key, str) else "?"
                path = f"{prefix}.{key_name}" if prefix else key_name
                paths.add(path)
                visit(child, path, depth + 1)
            return
        if isinstance(node, Sequence) and not isinstance(node, str | bytes):
            paths.add(f"{prefix}[]")
            for child in node:
                if isinstance(child, Mapping):
                    visit(child, f"{prefix}[]", depth + 1)
                    return

    visit(value, "", 0)
    return tuple(sorted(paths))


def shape_signature(value: Any, *, max_depth: int = 4) -> str:
    """A short stable digest of :func:`shape_paths` — shape only, no values."""
    joined = "\n".join(shape_paths(value, max_depth=max_depth))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
