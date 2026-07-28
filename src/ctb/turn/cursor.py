"""The transcript cursor: the source of truth for content.

Everything in this module runs **unconditionally on every tick, in every state**.
No function here consults :class:`~ctb.turn.state.TurnState`, and nothing in
``machine.py`` can suppress a fetch or a delivery. ``GET /status`` is a cadence
knob; this file is the delivery path.

Four jobs, in order of how badly getting them wrong hurts:

1. **First bind = seek to end, never replay.** There is no count endpoint, so
   :func:`seek_to_end` finds the tail with an exponential ``limit=1`` probe
   (``offset = 0, 1, 2, 4, 8, …``) followed by a binary search of the boundary —
   ``~2·log₂N`` requests, once per session, ever. Getting this wrong dumps an
   entire transcript into a Telegram topic.
2. **Every delta response is validated.** :func:`drain` drops any message whose
   ``sessionIndex`` is ``<=`` the stored cursor. Measured behaviour is a 404 for
   an unknown ``after`` id (no silent replay), so this filter is belt-and-braces
   rather than load-bearing — but it is the only thing standing between a
   server-side regression and re-posting a whole transcript. A 404 on ``after``
   is repaired by offset paging, not by concluding the session is dead.
3. **The advance is atomic.** ``transcript_messages`` + ``deliveries`` + the
   cursor move happen in one transaction, behind a ``SELECT … FOR UPDATE`` on
   the session, inside
   :func:`ctb.db.repo.transcript.advance_cursor`. The cursor never passes an
   unrecorded message (no drops) and a replayed page inserts nothing (no
   duplicates, even with two pollers overlapping across a redeploy).
4. **The prompt POST is idempotent.** :func:`send_prompt` writes the
   ``outbound_prompts`` row *before* any HTTP and re-POSTs the identical
   ``messageId`` on an ambiguous outcome — verified twice against the live API to
   dedupe (one user echo, not two).

Two API asymmetries are respected literally:

* ``POST /v0/workspaces`` has **no** idempotency key. It is never blind-retried;
  a nonce is embedded in the generated name (``tg-<chatid>-<nonce>``) and
  reconciled through ``GET /v0/projects/{id}/workspaces``.
* ``POST /v0/sessions`` **does** take a caller-supplied ``sessionId`` (probe
  verified; ``docs/PLAN.md`` is wrong about this), so :func:`create_session` is
  safely retryable.

``sessionIndex`` is **not** gapless (``0, 2, 3, 4, …`` was observed live).
Nothing here infers message loss from a gap.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from ctb.conductor.client import ConductorClient, TransportFailure
from ctb.conductor.errors import (
    Ambiguous,
    ApiError,
    AuthFatal,
    CircuitOpen,
    NotFound,
)
from ctb.conductor.models import (
    Session,
    TranscriptMessage,
    Workspace,
    WorkspaceStatusValue,
)
from ctb.db import NO_THREAD_ID
from ctb.db.connection import Database, now_ms
from ctb.db.repo import chats, events, prompts, sessions, transcript, workspaces
from ctb.db.repo.prompts import PromptRow
from ctb.db.repo.transcript import AdvanceItem, DeliveryDraft
from ctb.delivery.render.adapters import activity_text, best_effort_text
from ctb.delivery.render.adapters.result import turn_cost
from ctb.delivery.render.adapters.shapes import BlockRole, classify_block, first_str
from ctb.delivery.render.chunk import PartKind, chunk_blocks, chunk_html
from ctb.delivery.render.html import escape
from ctb.delivery.render.registry import RenderResult, render_message
from ctb.delivery.render.types import (
    Block,
    BlockKind,
    RenderContext,
    TextBlock,
    Verbosity,
    chat_blocks,
)
from ctb.logging import get_logger
from ctb.turn.state import Delta, PostAmbiguous, PostOk

__all__ = [
    "MAX_PAGES_PER_TICK",
    "PAGE_LIMIT",
    "Destination",
    "DrainResult",
    "PromptResult",
    "SeekResult",
    "WorkspaceCreation",
    "build_delta",
    "create_session",
    "create_workspace",
    "destination_for",
    "drain",
    "find_last_message",
    "find_workspace_by_nonce",
    "new_nonce",
    "nonce_of",
    "pick_preview",
    "plan_deliveries",
    "preview_text",
    "quick_replies_for",
    "reconcile_workspace",
    "repost_prompt",
    "seek_to_end",
    "send_prompt",
    "stable_nonce",
    "workspace_name",
]

_log = get_logger("ctb.turn.cursor")

#: Messages requested per ``GET /messages`` page during a normal drain.
PAGE_LIMIT: Final = 50
#: PLAN §Cursor: page within a tick while ``hasMore``, bounded. A tick must end.
MAX_PAGES_PER_TICK: Final = 10
#: Sanity bound on the seek/repair probes. 4M messages is far past anything real;
#: reaching it means ``hasMore`` is lying, and we stop rather than probe forever.
MAX_PROBE_OFFSET: Final = 1 << 22
#: How many trailing messages the first-bind preview looks at.
PREVIEW_WINDOW: Final = 12
#: Characters of the "now mirroring: …" preview.
PREVIEW_CHARS: Final = 240

#: POST attempts inside one :func:`send_prompt` call. "Retry forever" is achieved
#: by the machine re-issuing ``RePost`` on the next tick, not by blocking here.
PROMPT_MAX_ATTEMPTS: Final = 4
PROMPT_BACKOFF_BASE_S: Final = 1.0
PROMPT_BACKOFF_CAP_S: Final = 30.0

#: Pages of ``GET /projects/{id}/workspaces`` scanned when reconciling a nonce.
RECONCILE_MAX_PAGES: Final = 10
RECONCILE_PAGE_LIMIT: Final = 100

NONCE_LENGTH: Final = 8
#: No ``l``, ``1``, ``0`` or ``o`` — a nonce ends up in a name a human may read.
_NONCE_ALPHABET: Final = "abcdefghijkmnopqrstuvwxyz23456789"
_WORKSPACE_NAME_RE: Final = re.compile(
    rf"tg-(\d+)-([{_NONCE_ALPHABET}]{{{NONCE_LENGTH}}})"
)
_CHOICES_MARKER_RE: Final = re.compile(r"(?im)^\s*choices:\s*$")
_CHOICE_LINE_RE: Final = re.compile(r"^\s*([1-4])[.)]\s+(.+?)\s*$")

type Sleeper = Callable[[float], Awaitable[None]]


# ── destinations & rendering ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Destination:
    """Where one session's transcript is mirrored, and how loudly."""

    chat_id: int
    thread_id: int = NO_THREAD_ID
    verbosity: Verbosity = Verbosity.NORMAL
    #: Telegram push policy. Content is still delivered when true.
    silent: bool = False


#: Turns one transcript message into the delivery rows it should produce.
type DeliveryPlanner = Callable[
    [TranscriptMessage, Destination], Sequence[DeliveryDraft]
]


async def destination_for(db: Database, session_id: str) -> Destination | None:
    """Resolve a session's chat destination, or ``None`` when it is unbound.

    An unbound session is still *recorded* — only the delivery rows are skipped,
    so re-binding later mirrors from that point rather than replaying.
    """
    row = await sessions.get(db, session_id)
    if row is None or not row.is_bound or row.chat_id is None:
        return None
    verbosity = Verbosity.NORMAL
    silent = False
    chat = await chats.get(db, row.chat_id, row.thread_id)
    if chat is not None:
        try:
            verbosity = Verbosity(chat.verbosity)
        except ValueError:  # pragma: no cover - CHECK constraint prevents this
            verbosity = Verbosity.NORMAL
        # None of these suppress *delivery* — only the buzz.
        #
        # `off` used to be a synonym for `quiet`: both read
        # `notify != "loud" and not is_focused()`, so a topic set to off still
        # pushed for the whole focus window. A setting that does nothing is
        # worse than no setting.
        if chat.notify == "loud":
            silent = False
        elif chat.notify == "off":
            silent = True
        else:
            silent = not chat.is_focused(now_ms())
    return Destination(
        chat_id=row.chat_id,
        thread_id=row.thread_id,
        verbosity=verbosity,
        silent=silent,
    )


def _draft(
    destination: Destination,
    *,
    part_index: int,
    kind: str,
    payload: Mapping[str, Any],
) -> DeliveryDraft:
    # `content_hash` is deliberately left for the repo layer to compute from
    # `payload_json`, so every row's hash is produced the same way — the
    # boot-time duplicate guard compares them against each other.
    return DeliveryDraft(
        chat_id=destination.chat_id,
        thread_id=destination.thread_id,
        part_index=part_index,
        kind=kind,
        payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def plan_deliveries(
    message: TranscriptMessage, destination: Destination
) -> tuple[DeliveryDraft, ...]:
    """Render one transcript message into ``deliveries`` rows.

    Never raises. ``render_message`` already guarantees that for adapters; this
    adds the same guarantee for the chunker, because an exception here would
    roll back the advance and the cursor would re-fetch the same page forever —
    a renderer bug must never stall delivery.
    """
    try:
        result = render_message(message, RenderContext(verbosity=destination.verbosity))
        return _deliveries_from_render(message, destination, result)
    except Exception as exc:
        _log.error(
            "cursor.render_failed",
            session_id=message.session_id,
            message_type=message.type,
            error=repr(exc),
        )
        return _fallback_drafts(message, destination)


def _deliveries_from_render(
    message: TranscriptMessage,
    destination: Destination,
    result: RenderResult,
) -> tuple[DeliveryDraft, ...]:
    try:
        choices = quick_replies_for(message)
        blocks = result.chat
        if choices and _quick_replies_fit(choices):
            # The buttons *are* the options. Cut the text before chunking, so
            # the part boundaries are computed on what actually gets sent.
            blocks = _without_choices_text(blocks)
        parts = chunk_blocks(blocks, turn_id=message.turn_id or message.id)
        if choices:
            for index in range(len(parts) - 1, -1, -1):
                if parts[index].kind is not PartKind.DOCUMENT:
                    parts[index] = replace(parts[index], quick_replies=choices)
                    break
        # Errors always push loudly. Normal answers follow /notify and focus.
        has_error = any(block.kind is BlockKind.ERROR for block in result.chat)
        if not has_error:
            # One buzz per reply, not one per chunk. A long answer is split into
            # up to three messages plus a document, and the focus window that
            # promotes them lasts 30 minutes — so a single prompt could vibrate
            # a phone four times, and a busy morning made `notify=quiet` feel
            # like it was doing nothing at all. The first part is the one that
            # says an answer has arrived; the rest are the same answer.
            parts = [
                replace(part, silent=destination.silent or index > 0)
                for index, part in enumerate(parts)
            ]
        return tuple(
            _draft(
                destination,
                part_index=part.index,
                kind=part.kind.value,
                payload=part.payload(),
            )
            for part in parts
        )
    except Exception as exc:
        _log.error(
            "cursor.chunk_failed",
            session_id=message.session_id,
            message_type=message.type,
            error=repr(exc),
        )
        return _fallback_drafts(message, destination)


def quick_replies_for(message: TranscriptMessage) -> tuple[str, ...]:
    """Parse the strict mobile decision contract from an assistant message.

    Requiring the literal ``Choices:`` marker avoids turning numbered build
    steps into executable buttons. If an agent ignores the contract, the text
    is still delivered normally and the user can reply by hand.
    """
    if not message.is_agent:
        return ()
    text = "\n".join(
        str(block.get("text") or "")
        for block in message.blocks
        if str(block.get("type") or "").casefold() == "text"
    )
    markers = list(_CHOICES_MARKER_RE.finditer(text))
    if not markers:
        return ()
    lines = text[markers[-1].end() :].strip().splitlines()
    choices: list[str] = []
    expected = 1
    for line in lines:
        match = _CHOICE_LINE_RE.match(line)
        if match is None:
            if choices and line.strip():
                return ()
            continue
        if int(match.group(1)) != expected:
            return ()
        choice = re.sub(r"[*_`]+", "", match.group(2)).strip()
        if not choice:
            return ()
        choices.append(choice[:160])
        expected += 1
        if len(choices) == 4:
            break
    return tuple(choices) if len(choices) >= 2 else ()


def _quick_replies_fit(choices: Sequence[str]) -> bool:
    """Can every option be read in full on its button?

    Late import: the turn layer does not depend on the bot layer at boot, and
    the answer is the bot layer's label budget, not ours. If it cannot be
    asked, keep the text — a duplicate beats an unreadable option.
    """
    try:
        from ctb.bot.keyboards import quick_replies_fit
    except Exception:  # pragma: no cover - import cycle guard
        return False
    return quick_replies_fit(choices)


def _without_choices_text(blocks: Sequence[Block]) -> list[Block]:
    """Drop the trailing ``Choices:`` block from the chat text.

    It is rendered twice otherwise — once as prose, once as the buttons right
    beneath it, ~8 phone lines for three options. The marker is matched on the
    rendered HTML (plain ASCII on its own line, so escaping cannot touch it)
    and only the *last* one, matching :func:`quick_replies_for`.

    Never returns an empty chat: if cutting would leave nothing to attach the
    buttons to, the original blocks are kept. Losing the reply is not a trade
    we make for eight lines.
    """
    out = list(blocks)
    for index in range(len(out) - 1, -1, -1):
        block = out[index]
        if not isinstance(block, TextBlock):
            continue
        markers = list(_CHOICES_MARKER_RE.finditer(block.html))
        if not markers:
            continue
        head = block.html[: markers[-1].start()].rstrip()
        if head:
            out[index] = replace(block, html=head)
        else:
            del out[index]
        return out if chat_blocks(out) else list(blocks)
    return out


def _fallback_drafts(
    message: TranscriptMessage, destination: Destination
) -> tuple[DeliveryDraft, ...]:
    """Last resort when the renderer itself broke: plain escaped text.

    Losing a reply is worse than an ugly one. If even this fails, the message is
    still recorded and the cursor still advances — one unrendered message beats
    a wedged session.
    """
    try:
        text = best_effort_text(message.content)
        if not text.strip():
            return ()
        chunks = chunk_html(escape(text))
        return tuple(
            _draft(
                destination,
                part_index=index,
                kind="text",
                payload={
                    "kind": "text",
                    "index": index,
                    "html": html,
                    "plain": text,
                    "silent": destination.silent,
                },
            )
            for index, html in enumerate(chunks)
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.error(
            "cursor.fallback_render_failed",
            session_id=message.session_id,
            error=repr(exc),
        )
        return ()


def preview_text(message: TranscriptMessage, *, limit: int = PREVIEW_CHARS) -> str:
    """A one-line plain-text gist of a message, for the first-bind preview.

    Plain text, not HTML — the bot layer escapes it.

    Known shapes are described by the adapters that know them: what the agent
    *said* if it said anything, otherwise what it *did*, phrased the way the
    status card phrases it (``Bash · git add app/models/org.py``).
    :func:`best_effort_text` is the last resort only — it is the shape-blind
    walker, and letting it describe a ``tool_use`` block is how a preview ends
    up reading ``msg_011Cd… message assistant tool_use toolu_01Jz… Bash``.
    """
    text = _preview_source(message)
    if not text:
        text = message.prompt_text or best_effort_text(message.content, limit=limit * 4)
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + "…"


def _preview_source(message: TranscriptMessage) -> str:
    """Said beats did: the first text block, else the first tool call."""
    action = ""
    for block in message.blocks:
        role = classify_block(block)
        if role is BlockRole.TEXT:
            said = first_str(block, "text", "content")
            if said:
                return said
        elif role is BlockRole.TOOL_USE and not action:
            action = activity_text(block)
    return action


# ── evidence ─────────────────────────────────────────────────────────────────


def build_delta(messages: Sequence[TranscriptMessage]) -> Delta | None:
    """The :class:`~ctb.turn.state.Delta` evidence for a page of new messages.

    ``None`` for an empty page — a tick with no new content is not a delta, and
    ``Delta(n=0)`` is a machine no-op anyway.

    Fed to the machine **after** the messages are recorded and queued, never
    before: the machine reacts to a delta for cadence and card purposes only.
    """
    if not messages:
        return None
    turn_ids = {m.turn_id for m in messages if m.turn_id}
    witnessed = {m.content_id for m in messages if m.is_user_echo and m.content_id}
    tool_calls = sum(
        1 for m in messages for block in m.blocks if block.get("type") == "tool_use"
    )
    has_agent_content = any(
        m.is_agent and m.raw_payload_type in ("assistant", "result", "user")
        for m in messages
    )
    return Delta(
        n=len(messages),
        max_index=max(m.session_index for m in messages),
        has_agent_content=has_agent_content,
        turn_ids=frozenset(turn_ids),
        witnessed_prompt_ids=frozenset(witnessed),
        tool_calls=tool_calls,
        has_error_result=any(m.is_result and m.is_error for m in messages),
    )


# ── offset probing (seek-to-end and cursor repair share this) ────────────────


#: ``(message_at_offset, has_more)`` — the two facts a ``limit=1`` probe yields.
type _Probe = tuple[TranscriptMessage | None, bool]
type _Predicate = Callable[[TranscriptMessage | None, bool], bool]


class _Prober:
    """Cached ``limit=1`` offset probes, so a binary search never re-asks."""

    def __init__(self, client: ConductorClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._cache: dict[int, _Probe] = {}
        self.requests = 0

    async def at(self, offset: int) -> _Probe:
        cached = self._cache.get(offset)
        if cached is not None:
            return cached
        page = await self._client.get_messages(self._session_id, limit=1, offset=offset)
        self.requests += 1
        probe: _Probe = (page.data[0] if page.data else None, page.has_more)
        self._cache[offset] = probe
        return probe


async def _first_offset_matching(
    prober: _Prober, predicate: _Predicate, *, cap: int = MAX_PROBE_OFFSET
) -> int:
    """Smallest offset satisfying a **monotone** predicate.

    Exponential probe (``0, 1, 2, 4, 8, …``) to bracket the boundary, then a
    binary search inside the bracket: ``~2·log₂N`` requests instead of ``N``.
    """
    below = -1  # highest offset known *not* to satisfy the predicate
    high = 0
    while True:
        message, has_more = await prober.at(high)
        if predicate(message, has_more):
            break
        below = high
        if high >= cap:
            break
        high = 1 if high == 0 else min(high * 2, cap)

    low = below + 1
    while low < high:
        mid = (low + high) // 2
        message, has_more = await prober.at(mid)
        if predicate(message, has_more):
            high = mid
        else:
            low = mid + 1
    return high


def _at_or_past_end(message: TranscriptMessage | None, has_more: bool) -> bool:
    # `hasMore == False` at `offset=k` with `limit=1` means the transcript ends
    # at k, so the smallest such k *is* the last message. `message is None` is
    # the belt-and-braces branch for a server that stops reporting `hasMore`
    # correctly: it fires one offset later, and the caller steps back one.
    return message is None or not has_more


async def find_last_message(
    client: ConductorClient, session_id: str
) -> tuple[TranscriptMessage | None, int, int]:
    """Locate the newest message without downloading the transcript.

    Returns ``(message, offset_of_message, requests_used)``; ``message`` is
    ``None`` for an empty transcript, with ``offset`` ``-1``.
    """
    prober = _Prober(client, session_id)
    boundary = await _first_offset_matching(prober, _at_or_past_end)
    message, _ = await prober.at(boundary)
    offset = boundary
    if message is None and boundary > 0:
        offset = boundary - 1
        message, _ = await prober.at(offset)
    if message is None:
        offset = -1
    return message, offset, prober.requests


# ── first bind: seek to end ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SeekResult:
    """What :func:`seek_to_end` did. ``skipped`` means it refused, on purpose."""

    session_id: str
    cursor_message_id: str | None = None
    cursor_session_index: int = -1
    message_count: int = 0
    requests: int = 0
    preview: TranscriptMessage | None = None
    preview_text: str = ""
    skipped: bool = False
    #: The trailing messages the preview window already read, oldest first.
    #: **Read-only.** They were fetched to pick a preview line, they are not
    #: recorded and they create no ``deliveries`` rows — the cursor is at the
    #: end, so as far as delivery is concerned they are history.
    tail: tuple[TranscriptMessage, ...] = ()


async def seek_to_end(
    client: ConductorClient,
    db: Database,
    session_id: str,
    *,
    force: bool = False,
    preview_window: int = PREVIEW_WINDOW,
) -> SeekResult:
    """Point the cursor at the newest message. **Never replays history.**

    Refuses to run on an already-seeded session unless ``force`` is set: once
    real content has been recorded, moving the cursor without recording would
    jump over undelivered messages. That is the one thing this module exists to
    prevent, so the guard is not optional.
    """
    row = await sessions.get(db, session_id)
    if row is None:
        raise ValueError(f"seek_to_end: no sessions row for {session_id!r}")
    if row.seeded and not force:
        return SeekResult(
            session_id=session_id,
            cursor_message_id=row.cursor_message_id,
            cursor_session_index=row.cursor_session_index,
            skipped=True,
        )

    message, offset, requests = await find_last_message(client, session_id)
    if message is None:
        # Empty transcript. Still mark it seeded: there is nothing to skip, and
        # everything from here on is new content that must be delivered.
        await sessions.seek_to_end(db, session_id, message_id=None, session_index=-1)
        _log.info("cursor.seeded_empty", session_id=session_id, requests=requests)
        return SeekResult(session_id=session_id, requests=requests)

    await sessions.seek_to_end(
        db,
        session_id,
        message_id=message.id,
        session_index=message.session_index,
    )

    preview = message
    tail: tuple[TranscriptMessage, ...] = ()
    if preview_window > 1:
        window, extra = await _tail_window(
            client, session_id, offset, window=preview_window
        )
        requests += extra
        tail = tuple(window)
        preview = pick_preview(window) or message

    _log.info(
        "cursor.seeded",
        session_id=session_id,
        cursor_session_index=message.session_index,
        message_count=offset + 1,
        requests=requests,
    )
    return SeekResult(
        session_id=session_id,
        cursor_message_id=message.id,
        cursor_session_index=message.session_index,
        message_count=offset + 1,
        requests=requests,
        preview=preview,
        preview_text=preview_text(preview),
        tail=tail,
    )


async def _tail_window(
    client: ConductorClient, session_id: str, last_offset: int, *, window: int
) -> tuple[list[TranscriptMessage], int]:
    """The last ``window`` messages, in one request."""
    start = max(0, last_offset + 1 - window)
    page = await client.get_messages(session_id, offset=start, limit=window)
    return list(page.data), 1


def pick_preview(messages: Sequence[TranscriptMessage]) -> TranscriptMessage | None:
    """The most recent message worth showing as "now mirroring: …".

    Also the answer half of the adoption snapshot card — same question, same
    answer, so it is asked in exactly one place.
    """
    for message in reversed(messages):
        if message.is_assistant_text:
            return message
    for message in reversed(messages):
        if message.is_agent and message.raw_payload_type in ("assistant", "result"):
            return message
    return messages[-1] if messages else None


# ── the drain ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DrainResult:
    """One tick's worth of cursor work."""

    session_id: str
    messages: tuple[TranscriptMessage, ...] = ()
    delta: Delta | None = None
    cursor_message_id: str | None = None
    cursor_session_index: int = -1
    pages: int = 0
    requests: int = 0
    #: Messages the ``sessionIndex`` filter rejected — a replay, or overlap.
    dropped: int = 0
    recorded: int = 0
    duplicates: int = 0
    deliveries_created: int = 0
    #: Prompt ids that moved to ``witnessed`` because their echo arrived.
    witnessed: tuple[str, ...] = ()
    #: The stored ``after`` id was unknown and was rebuilt by offset paging.
    repaired: bool = False
    #: ``hasMore`` was false on the last page: the transcript is fully drained.
    exhausted: bool = True
    #: The page budget ran out with more to fetch. The next tick continues.
    truncated: bool = False
    #: Latest renderer activity line this tick, for the edit-in-place card.
    latest_activity: str | None = None
    #: ``total_cost_usd`` off the newest ``result`` payload this tick. The card
    #: shows it only once the turn is finished, so it never races the clock.
    latest_cost_usd: float | None = None

    @property
    def n(self) -> int:
        """New messages accepted this tick."""
        return len(self.messages)


async def drain(
    client: ConductorClient,
    db: Database,
    session_id: str,
    *,
    destination: Destination | None = None,
    deliver: bool = True,
    planner: DeliveryPlanner = plan_deliveries,
    max_pages: int = MAX_PAGES_PER_TICK,
    page_limit: int = PAGE_LIMIT,
    max_pending: int | None = None,
) -> DrainResult:
    """Fetch, validate, record and queue everything new. Runs every tick.

    This is the whole delivery path. It takes no state-machine input and makes
    no state-machine decision — it returns the :class:`~ctb.turn.state.Delta`
    evidence *after* the content is already durable.

    Raises :class:`~ctb.conductor.errors.NotFound` only when the session itself
    is gone (the poller turns that into ``E404``). A 404 caused by an unknown
    ``after`` id is repaired here, never escalated.
    """
    row = await sessions.get(db, session_id)
    if row is None:
        raise ValueError(f"drain: no sessions row for {session_id!r}")

    if not deliver:
        destination = None
    elif destination is None:
        destination = await destination_for(db, session_id)

    cursor_id = row.cursor_message_id
    cursor_index = row.cursor_session_index
    offset = 0
    requests = 0
    repaired = False

    if cursor_id is None and cursor_index >= 0:
        # A previous repair (or a partial write) left us with an index but no
        # replayable id. Find where that index sits before paging.
        offset, extra = await _repair_offset(client, session_id, cursor_index)
        requests += extra

    accepted: list[TranscriptMessage] = []
    witnessed: list[str] = []
    pages = 0
    dropped = 0
    recorded = 0
    duplicates = 0
    created = 0
    exhausted = True
    truncated = False
    latest_activity: str | None = None
    latest_cost: float | None = None

    while pages < max_pages:
        try:
            if cursor_id is not None:
                page = await client.get_messages(
                    session_id, after=cursor_id, limit=page_limit
                )
            else:
                page = await client.get_messages(
                    session_id, offset=offset, limit=page_limit
                )
            requests += 1
        except NotFound:
            requests += 1
            if cursor_id is None or repaired:
                # Not the cursor's fault: offset paging 404s too, so the session
                # itself is gone. Let the poller conclude DEAD.
                raise
            _log.warning(
                "cursor.after_unknown",
                session_id=session_id,
                cursor_session_index=cursor_index,
            )
            offset, extra = await _repair_offset(client, session_id, cursor_index)
            requests += extra
            cursor_id = None
            repaired = True
            continue

        pages += 1
        fresh = [m for m in page.data if m.session_index > cursor_index]
        dropped += len(page.data) - len(fresh)

        if fresh:
            items: list[AdvanceItem] = []
            unknown_records: list[Any] = []
            for message in fresh:
                drafts: tuple[DeliveryDraft, ...] = ()
                cost = turn_cost(message.content)
                if cost is not None:
                    latest_cost = cost
                if planner is plan_deliveries:
                    rendered = render_message(
                        message,
                        RenderContext(
                            verbosity=(
                                destination.verbosity
                                if destination is not None
                                else Verbosity.NORMAL
                            )
                        ),
                    )
                    if rendered.activity:
                        latest_activity = rendered.activity[-1]
                    unknown_records.extend(rendered.unknown)
                    if destination is not None:
                        drafts = _deliveries_from_render(message, destination, rendered)
                elif destination is not None:
                    drafts = tuple(planner(message, destination))
                items.append(
                    AdvanceItem(
                        message=message,
                        deliveries=drafts,
                    )
                )
            result = await transcript.advance_cursor(
                db, session_id, tuple(items), max_pending=max_pending
            )
            recorded += result.recorded
            duplicates += result.duplicates
            created += result.deliveries_created
            cursor_index = result.cursor_session_index
            cursor_id = result.cursor_message_id
            accepted.extend(fresh)
            if result.content_ids:
                witnessed.extend(
                    await prompts.witness_many(db, session_id, result.content_ids)
                )
            for record in unknown_records:
                try:
                    await events.note_unknown_content_type(
                        db,
                        content_type=record.type,
                        signature=record.shape_signature,
                        session_id=record.session_id or session_id,
                        message_id=record.message_id,
                    )
                except Exception as exc:
                    _log.error(
                        "cursor.unknown_record_failed",
                        session_id=session_id,
                        error=repr(exc),
                    )

        advanced_offset = 0
        if not fresh and cursor_id is None:
            # Offset mode legitimately walks past history we already have.
            advanced_offset = len(page.data)
            offset += advanced_offset

        if not fresh and advanced_offset == 0:
            # No new content and no forward movement: the server ignored our
            # cursor. Another page would return the identical bytes.
            if page.data:
                _log.warning(
                    "cursor.no_progress",
                    session_id=session_id,
                    returned=len(page.data),
                    cursor_session_index=cursor_index,
                )
            exhausted = not page.has_more
            break

        if not page.has_more:
            exhausted = True
            break
        exhausted = False
        truncated = pages >= max_pages

    delta = build_delta(accepted)
    if accepted:
        _log.debug(
            "cursor.delta",
            session_id=session_id,
            n=len(accepted),
            max_index=cursor_index,
            pages=pages,
            dropped=dropped,
            deliveries=created,
        )
    return DrainResult(
        session_id=session_id,
        messages=tuple(accepted),
        delta=delta,
        cursor_message_id=cursor_id,
        cursor_session_index=cursor_index,
        pages=pages,
        requests=requests,
        dropped=dropped,
        recorded=recorded,
        duplicates=duplicates,
        deliveries_created=created,
        witnessed=tuple(witnessed),
        repaired=repaired,
        exhausted=exhausted,
        truncated=truncated,
        latest_activity=latest_activity,
        latest_cost_usd=latest_cost,
    )


async def _repair_offset(
    client: ConductorClient, session_id: str, cursor_index: int
) -> tuple[int, int]:
    """Offset of the first message past ``cursor_index``, and requests used.

    Called when ``after=<stored id>`` 404s — the id disappeared (compaction, a
    session rebuild, our own bad write). ``sessionIndex`` ascends with offset, so
    the boundary is binary-searchable; the cursor index, not the id, is the real
    cursor, which is exactly why this repair is possible at all.
    """
    prober = _Prober(client, session_id)

    def past_cursor(message: TranscriptMessage | None, _has_more: bool) -> bool:
        return message is None or message.session_index > cursor_index

    offset = await _first_offset_matching(prober, past_cursor)
    _log.info(
        "cursor.repaired",
        session_id=session_id,
        cursor_session_index=cursor_index,
        offset=offset,
        requests=prober.requests,
    )
    return offset, prober.requests


# ── idempotent prompt POST ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PromptResult:
    """The outcome of one :func:`send_prompt` / :func:`repost_prompt` call."""

    message_id: str
    prompt: PromptRow
    evidence: PostOk | PostAmbiguous
    attempts: int = 0
    posted: bool = False
    reason: str = ""

    @property
    def ambiguous(self) -> bool:
        return not self.posted


async def send_prompt(
    client: ConductorClient,
    db: Database,
    *,
    session_id: str,
    text: str,
    chat_id: int | None = None,
    thread_id: int = NO_THREAD_ID,
    tg_message_id: int | None = None,
    index_at_post: int | None = None,
    message_id: str | None = None,
    max_attempts: int = PROMPT_MAX_ATTEMPTS,
    sleep: Sleeper = asyncio.sleep,
    rng: random.Random | None = None,
) -> PromptResult:
    """Send a prompt, ledger first.

    The ``outbound_prompts`` row is written **before** the HTTP call, so a crash
    anywhere after this point leaves a recoverable ``pending`` row rather than a
    prompt that may or may not exist. Boot re-POSTs it with the identical
    ``messageId`` (machine transition 3), which the API dedupes.

    Never raises for an ambiguous outcome: the row stays ``pending`` and the
    returned evidence is :class:`~ctb.turn.state.PostAmbiguous`, which the
    machine turns into a ``RePost`` on the next tick. "Retry forever" lives in
    that loop, not in a call that would block a poll tick for minutes.
    """
    if message_id is not None:
        existing = await prompts.get(db, message_id)
        if existing is not None:
            if existing.session_id != session_id or existing.body != text:
                raise ValueError(
                    "send_prompt: caller-supplied message_id belongs to "
                    "different prompt content"
                )
            return await repost_prompt(
                client,
                db,
                message_id,
                max_attempts=max_attempts,
                sleep=sleep,
                rng=rng,
            )
    if index_at_post is None:
        row = await sessions.get(db, session_id)
        index_at_post = row.cursor_session_index if row is not None else None
    prompt = await prompts.create(
        db,
        session_id=session_id,
        body=text,
        chat_id=chat_id,
        thread_id=thread_id,
        tg_message_id=tg_message_id,
        index_at_post=index_at_post,
        message_id=message_id,
    )
    return await _post_prompt(
        client, db, prompt, max_attempts=max_attempts, sleep=sleep, rng=rng
    )


async def repost_prompt(
    client: ConductorClient,
    db: Database,
    message_id: str,
    *,
    max_attempts: int = PROMPT_MAX_ATTEMPTS,
    sleep: Sleeper = asyncio.sleep,
    rng: random.Random | None = None,
) -> PromptResult:
    """Re-POST an unsettled prompt with the **identical** id (transition 3).

    Safe by construction: re-POSTing a known ``messageId`` produces one user
    echo, not two (probe assumption 7, passed twice). A prompt that has already
    been witnessed or abandoned is returned untouched.
    """
    prompt = await prompts.get(db, message_id)
    if prompt is None:
        raise ValueError(f"repost_prompt: no outbound_prompts row for {message_id!r}")
    if prompt.is_settled:
        return PromptResult(
            message_id=message_id,
            prompt=prompt,
            evidence=PostOk(message_id=message_id, state=prompt.post_state or "sent"),
            posted=True,
            reason="already settled",
        )
    return await _post_prompt(
        client, db, prompt, max_attempts=max_attempts, sleep=sleep, rng=rng
    )


async def _post_prompt(
    client: ConductorClient,
    db: Database,
    prompt: PromptRow,
    *,
    max_attempts: int,
    sleep: Sleeper,
    rng: random.Random | None,
) -> PromptResult:
    random_source = rng or random.Random()
    attempts = 0
    reason = ""
    limit = max(1, max_attempts)

    while attempts < limit:
        attempts += 1
        try:
            result = await client.post_message(
                prompt.session_id, prompt.body, prompt.message_id
            )
        except Ambiguous as exc:
            # The write may have landed. The *only* correct response is the same
            # id again — which is why the id was chosen before the first call.
            reason = f"ambiguous: {exc}"
        except (TransportFailure, CircuitOpen) as exc:
            reason = f"unreachable: {type(exc).__name__}"
        except AuthFatal:
            # The key is bad. The row stays pending — "your prompt is saved and
            # will be sent automatically" has to stay true.
            await prompts.record_attempt(db, prompt.message_id, error="auth")
            raise
        except NotFound:
            await prompts.mark_failed(db, prompt.message_id, error="session not found")
            raise
        except ApiError as exc:
            if not exc.retryable:
                await prompts.mark_failed(db, prompt.message_id, error=str(exc))
                raise
            reason = f"retryable: {exc}"
        else:
            updated = await prompts.mark_posted(
                db, prompt.message_id, post_state=result.state.value
            )
            _log.info(
                "cursor.prompt_posted",
                session_id=prompt.session_id,
                attempt=attempts,
                post_state=result.state.value,
            )
            return PromptResult(
                message_id=prompt.message_id,
                prompt=updated or prompt,
                evidence=PostOk(
                    message_id=prompt.message_id,
                    state=result.state.value,
                    index_at_post=prompt.index_at_post,
                ),
                attempts=attempts,
                posted=True,
            )

        await prompts.record_attempt(db, prompt.message_id, error=reason)
        _log.warning(
            "cursor.prompt_retry",
            session_id=prompt.session_id,
            attempt=attempts,
            reason=reason,
        )
        if attempts < limit:
            await sleep(_backoff(attempts, random_source))

    current = await prompts.get(db, prompt.message_id)
    return PromptResult(
        message_id=prompt.message_id,
        prompt=current or prompt,
        evidence=PostAmbiguous(message_id=prompt.message_id, reason=reason),
        attempts=attempts,
        posted=False,
        reason=reason,
    )


def _backoff(attempt: int, rng: random.Random) -> float:
    """Full jitter, same shape as the client's."""
    ceiling = min(PROMPT_BACKOFF_CAP_S, PROMPT_BACKOFF_BASE_S * (2 ** (attempt - 1)))
    return rng.uniform(0.0, ceiling)


# ── workspace / session creation ─────────────────────────────────────────────


def new_nonce(rng: random.Random | None = None) -> str:
    """A short, unambiguous reconciliation token for a workspace name."""
    source = rng or random.Random(uuid.uuid4().int)
    return "".join(source.choice(_NONCE_ALPHABET) for _ in range(NONCE_LENGTH))


def stable_nonce(value: str) -> str:
    """A restart-stable nonce for one externally idempotent create request."""
    digest = hashlib.sha256(value.encode()).digest()
    return "".join(
        _NONCE_ALPHABET[byte % len(_NONCE_ALPHABET)] for byte in digest[:NONCE_LENGTH]
    )


def workspace_name(chat_id: int, nonce: str) -> str:
    """``tg-<chatid>-<nonce>`` — the reconciliation key for a create with no id.

    The nonce lives in the *name* because ``POST /v0/workspaces`` accepts no
    idempotency key: after an ambiguous failure, listing the project's
    workspaces and matching this string is the only way to tell "it landed"
    from "it did not" without risking a second workspace.
    """
    return f"tg-{abs(chat_id)}-{nonce}"


def nonce_of(name: str | None) -> str | None:
    """Extract the nonce from a generated workspace name, if it has one."""
    if not name:
        return None
    match = _WORKSPACE_NAME_RE.search(name)
    return match.group(2) if match else None


@dataclass(frozen=True, slots=True)
class WorkspaceCreation:
    """The outcome of a create that cannot be blind-retried."""

    nonce: str
    name: str
    workspace_id: str | None = None
    session_id: str | None = None
    deep_link: str | None = None
    #: The POST returned 201 to us.
    created: bool = False
    #: The POST failed ambiguously and the workspace was found by its nonce.
    reconciled: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.workspace_id is not None

    @property
    def unresolved(self) -> bool:
        """Ambiguous *and* unreconcilable — a human has to look."""
        return self.workspace_id is None


async def create_workspace(
    client: ConductorClient,
    db: Database,
    *,
    chat_id: int,
    agent: str,
    project_id: str | None = None,
    repository_url: str | None = None,
    branch: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    session_name: str | None = None,
    env: Mapping[str, str] | None = None,
    nonce: str | None = None,
) -> WorkspaceCreation:
    """``POST /v0/workspaces``, exactly once, with nonce reconciliation.

    **Never blind-retried.** Only an :class:`~ctb.conductor.errors.Ambiguous`
    outcome — the write may have landed — triggers reconciliation: the project's
    workspaces are listed and matched on the nonce embedded in ``name``. Found
    means the call landed; absent means it did not. A second POST would leave a
    stranded cloud workspace burning credits.

    Everything else propagates unchanged, because it is *not* ambiguous: a 4xx
    was rejected outright, and the client only raises ``TransportFailure`` /
    ``CircuitOpen`` on a write it can prove never left the process.

    Pass ``nonce`` (persisted by the caller, e.g. in ``wizard_state``) to make
    the reconciliation survive a process restart too.
    """
    token = nonce or new_nonce()
    name = workspace_name(chat_id, token)
    if nonce is not None:
        # A Telegram update can be delivered again after we created the
        # workspace but before acknowledging the update. A stable caller nonce
        # makes that replay a lookup, not a second workspace POST.
        found = await reconcile_workspace(client, db, project_id, token)
        if found is not None:
            await _persist_workspace(
                db,
                workspace_id=found.id,
                name=found.name or name,
                nonce=token,
                project_id=found.project_id or project_id,
                repository_url=repository_url,
                branch=branch or found.branch,
                agent=agent,
                model=model,
                effort=effort,
                deep_link=found.deep_link,
                status=found.status,
            )
            return WorkspaceCreation(
                nonce=token,
                name=found.name or name,
                workspace_id=found.id,
                deep_link=found.deep_link,
                reconciled=True,
                reason="replayed create intent",
            )
    try:
        created = await client.create_workspace(
            agent=agent,
            project_id=project_id,
            repository_url=repository_url,
            branch=branch,
            name=name,
            session_name=session_name,
            model=model,
            effort=effort,
            env=env,
        )
    except Ambiguous as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _log.warning(
            "cursor.workspace_create_ambiguous",
            nonce=token,
            project_id=project_id,
            reason=reason,
        )
        found = await reconcile_workspace(client, db, project_id, token)
        if found is None:
            return WorkspaceCreation(nonce=token, name=name, reason=reason)
        await _persist_workspace(
            db,
            workspace_id=found.id,
            name=found.name or name,
            nonce=token,
            project_id=found.project_id or project_id,
            repository_url=repository_url,
            branch=branch or found.branch,
            agent=agent,
            model=model,
            effort=effort,
            deep_link=found.deep_link,
            status=found.status,
        )
        return WorkspaceCreation(
            nonce=token,
            name=found.name or name,
            workspace_id=found.id,
            deep_link=found.deep_link,
            reconciled=True,
            reason=reason,
        )

    await _persist_workspace(
        db,
        workspace_id=created.workspace_id,
        name=name,
        nonce=token,
        project_id=project_id,
        repository_url=repository_url,
        branch=branch,
        agent=agent,
        model=model,
        effort=effort,
        deep_link=created.deep_link,
        status=WorkspaceStatusValue.INITIALIZING,
    )
    if created.session_id:
        await sessions.upsert(
            db,
            created.session_id,
            workspace_id=created.workspace_id,
            title=session_name,
            agent=agent,
            model=model,
            effort=effort,
        )
    return WorkspaceCreation(
        nonce=token,
        name=name,
        workspace_id=created.workspace_id,
        session_id=created.session_id,
        deep_link=created.deep_link,
        created=True,
    )


async def _persist_workspace(
    db: Database,
    *,
    workspace_id: str,
    name: str,
    nonce: str,
    project_id: str | None,
    repository_url: str | None,
    branch: str | None,
    agent: str,
    model: str | None,
    effort: str | None,
    deep_link: str | None,
    status: WorkspaceStatusValue,
) -> None:
    await workspaces.upsert(
        db,
        workspace_id,
        project_id=project_id,
        name=name,
        repo_url=repository_url,
        branch=branch,
        agent=agent,
        model=model,
        effort=effort,
        deep_link=deep_link,
        status=status.value,
        create_nonce=nonce,
    )


async def find_workspace_by_nonce(
    client: ConductorClient,
    project_id: str | None,
    nonce: str,
    *,
    max_pages: int = RECONCILE_MAX_PAGES,
    page_limit: int = RECONCILE_PAGE_LIMIT,
) -> Workspace | None:
    """Look for ``tg-<chatid>-<nonce>`` in a project's workspaces.

    ``GET /v0/projects/{id}/workspaces`` is the only per-project listing there
    is, so reconciliation is impossible without a project id — a create by
    ``repository_url`` alone reports ``unresolved`` instead of guessing.
    """
    if project_id is None:
        return None
    offset = 0
    for _ in range(max(1, max_pages)):
        page = await client.list_project_workspaces(
            project_id, limit=page_limit, offset=offset
        )
        for workspace in page.data:
            if nonce_of(workspace.name) == nonce:
                return workspace
        if not page.has_more or not page.data:
            return None
        offset += len(page.data)
    return None


async def reconcile_workspace(
    client: ConductorClient,
    db: Database,
    project_id: str | None,
    nonce: str,
) -> Workspace | None:
    """Did the workspace this nonce names get created? Local cache, then API.

    Usable on boot as well as inline: if a previous incarnation persisted the
    row before crashing, the answer costs no request at all.
    """
    cached = await workspaces.get_by_nonce(db, nonce)
    if cached is not None:
        return Workspace(
            id=cached.id,
            name=cached.name,
            project_id=cached.project_id,
            branch=cached.branch,
            agent=cached.agent,
            model=cached.model,
            effort=cached.effort,
            deep_link=cached.deep_link,
            status=cached.status_value,
        )
    return await find_workspace_by_nonce(client, project_id, nonce)


async def create_session(
    client: ConductorClient,
    db: Database,
    *,
    workspace_id: str,
    session_id: str | None = None,
    title: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    fast_mode: bool | None = None,
) -> Session:
    """``POST /v0/sessions`` with a caller-supplied ``sessionId``.

    Unlike ``POST /v0/workspaces`` this **does** take an idempotency key (probe
    verified — ``docs/PLAN.md`` says otherwise and is wrong), so one is always
    supplied and the call is safely retryable. The row is written before the
    HTTP call for the same reason prompts are.
    """
    key = session_id or str(uuid.uuid4())
    # `sessions.workspace_id` is a foreign key: the workspace row has to exist
    # before the session row can point at it.
    await workspaces.upsert(db, workspace_id)
    await sessions.upsert(
        db,
        key,
        workspace_id=workspace_id,
        title=title,
        agent=agent,
        model=model,
        effort=effort,
        is_bound=False,
    )
    created = await client.create_session(
        workspace_id=workspace_id,
        session_id=key,
        title=title,
        agent=agent,
        model=model,
        effort=effort,
        fast_mode=fast_mode,
    )
    if created.id != key:
        # The server ignored our id. Keep both rows straight rather than
        # polling a session that does not exist.
        _log.warning("cursor.session_id_ignored", requested=key, returned=created.id)
        await sessions.delete(db, key)
        await sessions.upsert(
            db,
            created.id,
            workspace_id=workspace_id,
            title=title,
            agent=created.agent or agent,
            model=created.model or model,
            effort=created.effort or effort,
            is_bound=False,
        )
    return created
