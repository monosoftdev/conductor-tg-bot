"""Pure, conservative voice-command parser.

Only a wake phrase at the beginning plus an exact command alias can create a
command intent. Everything else is an ordinary prompt or an explicit
``ambiguous`` result that requires the user to type/tap.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "VoiceCommand",
    "VoiceIntent",
    "VoiceIntentKind",
    "parse_intent",
]


class VoiceIntentKind(StrEnum):
    PROMPT = "prompt"
    COMMAND = "command"
    AMBIGUOUS = "ambiguous"
    EMPTY = "empty"


class VoiceCommand(StrEnum):
    NEW = "new"
    BOARD = "board"
    STOP = "stop"
    FIND = "find"
    MODE = "mode"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class VoiceIntent:
    kind: VoiceIntentKind
    text: str
    command: VoiceCommand | None = None
    argument: str = ""
    reason: str = ""

    @property
    def requires_confirmation(self) -> bool:
        return self.command is VoiceCommand.DONE

    def to_json(self) -> str:
        payload = asdict(self)
        payload["kind"] = str(self.kind)
        payload["command"] = str(self.command) if self.command is not None else None
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> VoiceIntent:
        data = json.loads(value)
        return cls(
            kind=VoiceIntentKind(str(data["kind"])),
            text=str(data.get("text") or ""),
            command=(
                VoiceCommand(str(data["command"])) if data.get("command") else None
            ),
            argument=str(data.get("argument") or ""),
            reason=str(data.get("reason") or ""),
        )


_ALIASES: Final[dict[str, VoiceCommand]] = {
    # English
    "new": VoiceCommand.NEW,
    "board": VoiceCommand.BOARD,
    "stop": VoiceCommand.STOP,
    "find": VoiceCommand.FIND,
    "mode": VoiceCommand.MODE,
    "done": VoiceCommand.DONE,
    # Ukrainian
    "новий": VoiceCommand.NEW,
    "нова": VoiceCommand.NEW,
    "дошка": VoiceCommand.BOARD,
    "зупини": VoiceCommand.STOP,
    "стоп": VoiceCommand.STOP,
    "знайди": VoiceCommand.FIND,
    "режим": VoiceCommand.MODE,
    "готово": VoiceCommand.DONE,
    "завершити": VoiceCommand.DONE,
    # Russian
    "создать": VoiceCommand.NEW,
    "новый": VoiceCommand.NEW,
    "доска": VoiceCommand.BOARD,
    "останови": VoiceCommand.STOP,
    "найти": VoiceCommand.FIND,
    "завершить": VoiceCommand.DONE,
}

_BOUNDARY = r"(?=$|[\s,:;.!?—–-])"
_SEPARATORS = " \t\r\n,:;.!?—–-"
_VERB = re.compile(r"^([^\s,:;.!?—–-]+)(.*)$", re.S)


def _clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def parse_intent(transcript: str, wake_phrases: list[str]) -> VoiceIntent:
    """Parse without fuzzy matching, translation, or identifier rewriting."""
    text = _clean(transcript)
    if not text:
        return VoiceIntent(VoiceIntentKind.EMPTY, "", reason="empty transcript")

    folded = text.casefold()
    matched_end: int | None = None
    for phrase in sorted(wake_phrases, key=len, reverse=True):
        normalized = _clean(phrase).casefold()
        if not normalized:
            continue
        match = re.match(rf"^{re.escape(normalized)}{_BOUNDARY}", folded)
        if match is not None:
            matched_end = match.end()
            break

    if matched_end is None:
        return VoiceIntent(VoiceIntentKind.PROMPT, text)

    remainder = text[matched_end:].lstrip(_SEPARATORS)
    if not remainder:
        return VoiceIntent(
            VoiceIntentKind.AMBIGUOUS,
            text,
            reason="wake phrase without a command",
        )
    match = _VERB.match(remainder)
    if match is None:  # pragma: no cover - lstrip + non-empty guarantees a verb
        return VoiceIntent(VoiceIntentKind.AMBIGUOUS, text, reason="missing command")
    spoken_verb = match.group(1).casefold()
    command = _ALIASES.get(spoken_verb)
    if command is None:
        return VoiceIntent(
            VoiceIntentKind.AMBIGUOUS,
            text,
            reason=f"unknown command: {match.group(1)[:40]}",
        )
    argument = match.group(2).lstrip(_SEPARATORS)
    return VoiceIntent(
        VoiceIntentKind.COMMAND,
        text,
        command=command,
        argument=argument,
    )
