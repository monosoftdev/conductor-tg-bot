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

**This is the last resort, and it is chrome, not the reply.** It runs for
shapes nobody has seen, so it has to assume the payload is mostly bookkeeping.
Three filters keep machine noise out of the chat, in ascending order of cost:

1. :data:`META_KEYS` (and any key whose folded name ends in ``id``) never
   carry prose. Entering one turns interest **off** for that subtree, so a
   prose-y container like ``message`` cannot make its own ``id``/``type``/
   ``role``/``model`` leaves read as prose. That single stickiness bug is what
   put ``msg_…``, ``toolu_…``, ``assistant`` and ``tool_use`` in a chat card.
2. A value that *looks* like an identifier (``msg_…``, ``toolu_…``, a UUID, a
   long hex or base62 blob) is dropped whatever its key is called — unknown
   payloads use key names we cannot enumerate.
3. A value that is nothing but a discriminator word (``assistant``,
   ``tool_use``, ``message``, ``result``) is dropped for the same reason.

Losing a word here is fine. Showing the owner a token is not.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "DISCRIMINATOR_WORDS",
    "META_KEYS",
    "TEXT_KEYS",
    "best_effort_text",
    "is_machine_token",
    "is_meta_key",
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

#: Folded key names that are bookkeeping by definition. Anything under one of
#: these is metadata, not prose. Every ``*_id`` / ``*Id`` / ``uuid`` key is
#: covered by the suffix rule in :func:`is_meta_key` instead of being listed.
META_KEYS: Final = frozenset(
    {
        "agent",
        "apikeysource",
        "cachecreationinputtokens",
        "cachereadinputtokens",
        "createdat",
        "cwd",
        "durationapims",
        "durationms",
        "effort",
        "gitbranch",
        "index",
        "inputtokens",
        "kind",
        "mcpservers",
        "meta",
        "metadata",
        "model",
        "name",
        "numturns",
        "outputtokens",
        "permissiondenials",
        "permissionmode",
        "provider",
        "receivedat",
        "role",
        "sessionindex",
        "signature",
        "slashcommands",
        "stopreason",
        "stopsequence",
        "subtype",
        "timestamp",
        "tools",
        "totalcostusd",
        "type",
        "updatedat",
        "usage",
        "version",
    }
)

#: Values that are pure protocol vocabulary. A leaf that is exactly one of
#: these is a discriminator someone forgot to name ``type``.
DISCRIMINATOR_WORDS: Final = frozenset(
    {
        "agentmessage",
        "assistant",
        "content",
        "event",
        "functioncall",
        "init",
        "mcptooluse",
        "message",
        "object",
        "ratelimitevent",
        "redactedthinking",
        "result",
        "servertooluse",
        "system",
        "text",
        "thinking",
        "toolcall",
        "toolresult",
        "tooluse",
        "user",
        "usermessage",
    }
)

_MAX_DEPTH: Final = 6
_MAX_PARTS: Final = 40
_PART_LIMIT: Final = 500
_DEFAULT_LIMIT: Final = 1500
_MAX_SIGNATURE_PATHS: Final = 400

_SEPARATORS: Final = re.compile(r"[\s_\-.]+")
#: ``msg_011Cd…``, ``toolu_01Jz…``, ``sevt_…`` — a short lowercase prefix and a
#: long opaque suffix. The suffix must mix digits and letters, so
#: ``chore_addhelloworld`` stays prose.
_PREFIXED_ID: Final = re.compile(r"^[a-z]{2,8}_[A-Za-z0-9]{10,}$")
_UUID: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_BLOB: Final = re.compile(r"^[0-9a-fA-F]{16,}$")
_OPAQUE_BLOB: Final = re.compile(r"^[A-Za-z0-9]{20,}$")
#: Shortest string worth testing at all — below this nothing is an id.
_MIN_TOKEN_CHARS: Final = 12


def fold(name: str) -> str:
    """``session_id`` / ``sessionId`` / ``Session-Id`` → ``sessionid``."""
    return _SEPARATORS.sub("", name).strip().lower()


def is_meta_key(name: str) -> bool:
    """True when a key name can only ever hold bookkeeping, never prose."""
    folded = fold(name)
    return folded in META_KEYS or folded.endswith("id")


def is_machine_token(text: str) -> bool:
    """True when a value looks like an identifier rather than something said.

    Shape only, no key names: unknown payloads invent their own key names, and
    a token is recognisable without one. Cheap by construction — one length
    test rejects almost every real string before a regex runs.
    """
    if len(text) < _MIN_TOKEN_CHARS or any(c.isspace() for c in text):
        return False
    if _UUID.match(text) or _HEX_BLOB.match(text):
        return True
    has_digit = any(c.isdigit() for c in text)
    if not has_digit:
        return False
    if _PREFIXED_ID.match(text):
        return _mixed(text.split("_", 1)[1])
    return bool(_OPAQUE_BLOB.match(text)) and _mixed(text)


def _mixed(text: str) -> bool:
    return any(c.isdigit() for c in text) and any(c.isalpha() for c in text)


def _is_noise(text: str) -> bool:
    """A leaf that is an identifier or a bare discriminator word."""
    if " " in text or "\n" in text:
        return False
    return fold(text) in DISCRIMINATOR_WORDS or is_machine_token(text)


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
            if interesting and text and text not in seen and not _is_noise(text):
                seen.add(text)
                parts.append(text[:_PART_LIMIT])
            return
        if isinstance(node, Mapping):
            for key, child in node.items():
                if len(parts) >= _MAX_PARTS:
                    return
                key_name = key if isinstance(key, str) else ""
                if is_meta_key(key_name):
                    # Not skipped — descended into with interest *off*, so a
                    # ``text`` nested under ``metadata`` still survives.
                    visit(child, depth + 1, False)
                    continue
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
