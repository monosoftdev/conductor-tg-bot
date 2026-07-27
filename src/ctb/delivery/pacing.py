"""Telegram's two rate limits, kept as two budgets, plus a fair rotor.

One shared bot token means one shared rate budget across every tenant. The old
design had a single process-wide ``15 msg/min`` bucket, which conflated two
different Telegram limits and, at fifty tenants, would have worked out to a
third of a message per minute each — a bot that looks dead.

They are separate here:

* a **global** budget, roughly Telegram's ~30 messages/second per token, and
* a **per-chat** budget, roughly its ~20 messages/minute per group.

:class:`DestinationRotor` then decides *which* destination spends the global
budget next. Fairness is per ``(chat, topic)`` rather than per tenant, which is
strictly stronger: a tenant with forty busy topics cannot starve a tenant with
one, and one runaway topic cannot starve its own siblings.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Final

from ctb.conductor.client import TokenBucket
from ctb.db import NO_THREAD_ID
from ctb.logging import get_logger

__all__ = [
    "CHAT_BURST",
    "CHAT_RATE_PER_MINUTE",
    "GLOBAL_BURST",
    "GLOBAL_RATE_PER_SECOND",
    "DestinationRotor",
    "TelegramPacer",
]

_log = get_logger(__name__)

type Sleeper = Callable[[float], Awaitable[None]]

#: Telegram's documented per-group ceiling is ~20/min; sit under it with room
#: for the status card's edits in the same chat.
CHAT_RATE_PER_MINUTE: Final = 15.0
#: A three-part answer should still arrive as one burst rather than over 12s.
CHAT_BURST: Final = 3.0

#: Telegram's per-token ceiling is ~30/s. Leave headroom for command replies,
#: which do not go through the outbox at all.
GLOBAL_RATE_PER_SECOND: Final = 25.0
GLOBAL_BURST: Final = 20.0

#: Bound on remembered per-chat buckets, so a scan of many chats cannot grow
#: memory without limit. Evicting one only forfeits its saved tokens.
_MAX_CHATS: Final = 4096

#: How many distinct chats must report a 429 inside this window before the
#: pause is treated as token-level rather than group-level. Telegram's error
#: does not distinguish, so this is a heuristic and is documented as one.
_GLOBAL_429_CHATS: Final = 3
_GLOBAL_429_WINDOW_S: Final = 10.0


class TelegramPacer:
    """One per process. Owns the global budget and every per-chat budget."""

    def __init__(
        self,
        *,
        global_rate: float = GLOBAL_RATE_PER_SECOND,
        global_burst: float = GLOBAL_BURST,
        chat_rate_per_minute: float = CHAT_RATE_PER_MINUTE,
        chat_burst: float = CHAT_BURST,
        max_chats: int = _MAX_CHATS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._chat_rate = chat_rate_per_minute / 60.0
        self._chat_burst = chat_burst
        self._max_chats = max(1, max_chats)
        self._global = TokenBucket(
            rate=global_rate, burst=global_burst, clock=clock, sleep=sleep
        )
        self._chats: OrderedDict[int, TokenBucket] = OrderedDict()
        self._paused_until: dict[int, float] = {}
        self._global_paused_until = float("-inf")
        self._recent_429: OrderedDict[int, float] = OrderedDict()

    # -- budgets --------------------------------------------------------------

    def _bucket(self, chat_id: int) -> TokenBucket:
        bucket = self._chats.get(chat_id)
        if bucket is not None:
            self._chats.move_to_end(chat_id)
            return bucket
        while len(self._chats) >= self._max_chats:
            self._chats.popitem(last=False)
        bucket = TokenBucket(
            rate=self._chat_rate,
            burst=self._chat_burst,
            clock=self._clock,
            sleep=self._sleep,
        )
        self._chats[chat_id] = bucket
        return bucket

    def chat_ready(self, chat_id: int) -> bool:
        """Could this chat send right now? Takes nothing.

        Used to *choose* a destination. ``False`` means "saturated or paused" —
        serve someone else rather than wait. The token itself is spent per
        message, in :meth:`acquire_chat`.
        """
        if self._clock() < self._paused_until.get(chat_id, float("-inf")):
            return False
        return self._bucket(chat_id).peek()

    async def acquire_global(self) -> float:
        """Block until the shared per-token budget allows one more send."""
        return await self._global.acquire()

    async def acquire_chat(self, chat_id: int) -> float:
        """Block until *this* chat may send again.

        The fair path is :meth:`chat_ready`, which skips a saturated chat and
        serves someone else. This is the fallback for when there *is* nobody
        else: one busy topic should still drain at its own rate rather than
        spin.
        """
        return await self._bucket(chat_id).acquire()

    @property
    def global_paused_for(self) -> float:
        return max(0.0, self._global_paused_until - self._clock())

    def paused_for(self, chat_id: int) -> float:
        return max(0.0, self._paused_until.get(chat_id, float("-inf")) - self._clock())

    # -- 429 handling ---------------------------------------------------------

    def pause_chat(self, chat_id: int, seconds: float) -> None:
        """One group told us to slow down. Only that group waits."""
        now = self._clock()
        self._paused_until[chat_id] = max(
            self._paused_until.get(chat_id, float("-inf")), now + max(0.0, seconds)
        )
        self._bucket(chat_id).drain()
        self._note_429(chat_id, now)

    def pause_global(self, seconds: float) -> None:
        """Everyone waits. Only for a limit that is genuinely token-level."""
        self._global_paused_until = max(
            self._global_paused_until, self._clock() + max(0.0, seconds)
        )

    def _note_429(self, chat_id: int, now: float) -> None:
        """Escalate to a global pause when several *distinct* chats 429 at once.

        Telegram's 429 does not say whether the limit was per-chat or
        per-token. Several unrelated chats hitting it inside a few seconds is
        the signature of the latter.
        """
        self._recent_429[chat_id] = now
        self._recent_429.move_to_end(chat_id)
        for stale in [
            key
            for key, at in self._recent_429.items()
            if now - at > _GLOBAL_429_WINDOW_S
        ]:
            del self._recent_429[stale]
        if len(self._recent_429) >= _GLOBAL_429_CHATS:
            _log.warning("pacer.global_backoff", chats=len(self._recent_429))
            self.pause_global(_GLOBAL_429_WINDOW_S)
            self._recent_429.clear()

    def health(self) -> dict[str, float | int]:
        return {
            "chats_tracked": len(self._chats),
            "chats_paused": sum(
                1 for chat_id in self._paused_until if self.paused_for(chat_id) > 0
            ),
            "global_paused_for": round(self.global_paused_for, 3),
            "global_tokens": round(self._global.tokens, 3),
        }


class DestinationRotor:
    """Round-robin over ``(chat, topic)`` pairs, least recently served first.

    The queue is claimed per destination rather than globally-oldest-first, so
    ordering *within* a topic is preserved while several topics drain in
    parallel — and no single backlog can monopolise the sender.
    """

    __slots__ = ("_clock", "_served")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._served: dict[tuple[int, int], float] = {}

    def order[T](
        self,
        destinations: Sequence[T],
        *,
        key: Callable[[T], tuple[int, int]],
        urgency: Callable[[T], int] | None = None,
    ) -> list[T]:
        """Most urgent first, then least recently served.

        ``urgency`` is the destination's best pending priority, with the topic
        the user's thumb is in promoted to the front. Errors therefore still
        overtake bulk *across* topics, which per-destination claiming would
        otherwise have lost.
        """

        def sort_key(item: T) -> tuple[int, float]:
            rank = 0 if urgency is None else urgency(item)
            return (rank, self._served.get(key(item), 0.0))

        return sorted(destinations, key=sort_key)

    def served(self, destination_key: tuple[int, int]) -> None:
        self._served[destination_key] = self._clock()
        if len(self._served) > _MAX_CHATS:
            oldest = min(self._served, key=lambda k: self._served[k])
            del self._served[oldest]

    def forget(self, keys: Iterable[tuple[int, int]] = ()) -> None:
        for item in keys:
            self._served.pop(item, None)


def destination_key(chat_id: int, thread_id: int = NO_THREAD_ID) -> tuple[int, int]:
    return (chat_id, thread_id)
