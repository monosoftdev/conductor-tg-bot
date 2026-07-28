"""File edits: one line on the **card**, the whole diff on demand.

A file-editing tool call carries something a bare tool call does not — *which
file, how much* — so it gets a line of its own (``path +12 −3``) rather than
being folded away entirely. But that line is progress, and progress belongs on
the status card, which is edited in place: as a chat bubble it was one
notification per file, naming paths nobody can act on from a phone, arriving
ahead of the answer that explains them. ``verbose`` moves it back into the chat
along with the patch.

The diff body is an attachment either way, because a 400-line patch in a phone
chat buries the answer that follows it.

Detection is by shape: a tool call whose arguments name a path *and* carry an
edit payload (``old_string``/``new_string``, ``content``, ``patch``,
``edits``…). ``Read`` names a path and is not an edit; ``Bash`` carries a
payload and names no path.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ctb.delivery.render.adapters.base import plain_html, truncate_text
from ctb.delivery.render.adapters.shapes import (
    first_str,
    mapping_of,
    normalize,
    tool_input,
    tool_name,
)
from ctb.delivery.render.types import (
    ActivityLine,
    Block,
    BlockKind,
    DocumentBlock,
    TextBlock,
    Verbosity,
)

__all__ = [
    "FileEdit",
    "describe_file_edit",
    "diff_document",
    "file_edit_blocks",
    "is_file_edit",
]

#: Argument keys that name the file being touched.
_PATH_KEYS: Final = (
    "file_path",
    "filePath",
    "notebook_path",
    "notebookPath",
    "path",
    "filename",
    "file",
    "target_file",
)
#: Argument keys that carry a ready-made unified diff.
_DIFF_KEYS: Final = ("patch", "diff", "unified_diff", "unifiedDiff")
#: Argument keys that carry replacement text.
_OLD_KEYS: Final = ("old_string", "oldString", "old_str", "oldText", "old")
_NEW_KEYS: Final = ("new_string", "newString", "new_str", "newText", "new")
_BODY_KEYS: Final = ("content", "contents", "file_text", "text")
#: Tool names that are edits even when the argument shape is unfamiliar.
_EDIT_TOOL_NAMES: Final = frozenset(
    {
        "applypatch",
        "createfile",
        "edit",
        "multiedit",
        "notebookedit",
        "strreplaceeditor",
        "write",
    }
)

#: Beyond this many characters, skip ``difflib`` and count lines instead. A
#: 200 KB paste must not turn the poller into a diff engine.
_DIFF_BUDGET: Final = 200_000
#: Hard cap on an attached diff. Telegram accepts far more; this is about not
#: holding an unbounded string in memory per delivery row.
_DOCUMENT_LIMIT: Final = 200_000
_ADDED: Final = "+"
_REMOVED: Final = "−"  # U+2212 MINUS SIGN, per PLAN's `path +12 −3`


@dataclass(frozen=True, slots=True)
class FileEdit:
    """A file-editing tool call, reduced to what the chat needs."""

    path: str
    added: int = 0
    removed: int = 0
    #: Unified diff text when one exists or can be reconstructed.
    diff: str | None = None
    tool: str = ""

    @property
    def summary(self) -> str:
        """``src/x.py +12 −3`` — plain text, for logs and captions."""
        return f"{self.path} {_ADDED}{self.added} {_REMOVED}{self.removed}"

    @property
    def document_name(self) -> str:
        """A safe ``.diff`` filename derived from the path's basename."""
        base = self.path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
        return f"{safe.strip('._-') or 'edit'}.diff"


def is_file_edit(block: Mapping[str, Any]) -> bool:
    """True when a ``tool_use`` block is editing a file."""
    return describe_file_edit(block) is not None


def describe_file_edit(block: Mapping[str, Any]) -> FileEdit | None:
    """Reduce a ``tool_use`` block to a :class:`FileEdit`, or ``None``.

    Never raises: a malformed edit degrades to "no counts, no diff" rather
    than to an exception the registry has to catch.
    """
    arguments = tool_input(block)
    if not arguments:
        return None
    path = first_str(arguments, *_PATH_KEYS)
    name = tool_name(block) or ""
    known_tool = normalize(name) in _EDIT_TOOL_NAMES
    if not path:
        return None
    if not known_tool and not _has_edit_payload(arguments):
        return None

    added, removed, diff = _stats(path, arguments)
    return FileEdit(
        path=path, added=added, removed=removed, diff=diff, tool=name or "edit"
    )


def _has_edit_payload(arguments: Mapping[str, Any]) -> bool:
    keys = (
        *_DIFF_KEYS,
        *_OLD_KEYS,
        *_NEW_KEYS,
        *_BODY_KEYS,
        "edits",
        "structuredPatch",
    )
    return any(key in arguments for key in keys)


def _stats(path: str, arguments: Mapping[str, Any]) -> tuple[int, int, str | None]:
    """``(added, removed, diff_text)`` from whatever the tool call carries."""
    ready = first_str(arguments, *_DIFF_KEYS)
    if ready:
        added, removed = _count_diff(ready)
        return added, removed, ready

    structured = _from_structured_patch(arguments.get("structuredPatch"))
    if structured is not None:
        added, removed = _count_diff(structured)
        return added, removed, structured

    edits = arguments.get("edits")
    if isinstance(edits, list) and edits:
        old_parts: list[str] = []
        new_parts: list[str] = []
        for edit in edits:
            fields = mapping_of(edit)
            old_parts.append(first_str(fields, *_OLD_KEYS) or "")
            new_parts.append(first_str(fields, *_NEW_KEYS) or "")
        return _diff_texts(path, "\n".join(old_parts), "\n".join(new_parts))

    old = first_str(arguments, *_OLD_KEYS) or ""
    new = first_str(arguments, *_NEW_KEYS)
    if new is None:
        body = first_str(arguments, *_BODY_KEYS)
        if body is None:
            return 0, 0, None
        new = body
    return _diff_texts(path, old, new)


def _from_structured_patch(value: Any) -> str | None:
    """Rebuild diff text from Claude Code's ``structuredPatch`` hunk list."""
    if not isinstance(value, list) or not value:
        return None
    lines: list[str] = []
    for hunk in value:
        fields = mapping_of(hunk)
        hunk_lines = fields.get("lines")
        if isinstance(hunk_lines, list):
            lines.extend(str(line) for line in hunk_lines)
    return "\n".join(lines) if lines else None


def _diff_texts(path: str, old: str, new: str) -> tuple[int, int, str | None]:
    if old == new:
        return 0, 0, None
    if len(old) + len(new) > _DIFF_BUDGET:
        # Too big to diff politely: line counts are honest enough for a
        # one-line summary, and the body goes unattached.
        return _line_count(new), _line_count(old), None
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    added, removed = _count_diff(diff)
    return added, removed, diff or None


def _line_count(text: str) -> int:
    return len(text.splitlines()) if text else 0


def _count_diff(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def diff_document(
    edit: FileEdit, *, source_message_id: str | None = None
) -> DocumentBlock | None:
    """The ``.diff`` attachment for an edit, or ``None`` when there is no body.

    Exposed so the "show diff" button can build the attachment on demand
    without re-deriving the edit — the chat only ever gets the one-liner.
    """
    if not edit.diff:
        return None
    return DocumentBlock(
        filename=edit.document_name,
        content=truncate_text(edit.diff, _DOCUMENT_LIMIT),
        caption=edit.summary,
        kind=BlockKind.DIFF,
        silent=True,
        source_message_id=source_message_id,
    )


def file_edit_blocks(
    edit: FileEdit,
    *,
    verbosity: Verbosity = Verbosity.NORMAL,
    source_message_id: str | None = None,
) -> list[Block]:
    """The card gets the line. The chat gets it only when verbose.

    It used to be a chat bubble per edited file, and that was the single
    largest source of turn noise: a twenty-file turn was twenty notifications
    naming paths you cannot open from a phone, arriving *before* the answer
    that explains them. A file being written is **progress, not content** — so
    it goes where progress goes, the one status card that is edited in place
    rather than re-sent, and the count lands on the finished card
    (``✅ done in 1m32s · 12 tools · 5 files``).

    ``verbose`` still puts the line and the patch itself in the chat, which is
    what that setting is for.
    """
    if not verbosity.at_least(Verbosity.VERBOSE):
        return [
            ActivityLine(
                # Plain text: the card escapes what it is given.
                text=f"📝 {edit.summary}",
                kind=BlockKind.DIFF,
                source_message_id=source_message_id,
            )
        ]
    summary = TextBlock(
        html=(
            f"📝 <code>{plain_html(edit.path)}</code> "
            f"{_ADDED}{edit.added} {_REMOVED}{edit.removed}"
        ),
        kind=BlockKind.DIFF,
        source_message_id=source_message_id,
    )
    blocks: list[Block] = [summary]
    document = diff_document(edit, source_message_id=source_message_id)
    if document is not None:
        blocks.append(document)
    return blocks
