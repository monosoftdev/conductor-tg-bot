"""Two rate budgets and a fair rotor.

With one shared bot serving every workspace, these are what stop a single
customer from being everybody else's outage. The properties worth pinning are
behavioural, not structural: a saturated chat is *skipped* rather than waited
on, a 429 confines itself to the chat that caused it, and the rotor cannot let
one destination monopolise the sender.
"""

from __future__ import annotations

import pytest

from ctb.delivery.pacing import (
    CHAT_RATE_PER_MINUTE,
    DestinationRotor,
    TelegramPacer,
)
from tests.conftest import FakeClock


class NoSleep:
    """Advances the clock instead of waiting, so pacing is deterministic."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.clock.advance(seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(1_000.0)


@pytest.fixture
def pacer(clock: FakeClock) -> TelegramPacer:
    return TelegramPacer(clock=clock, sleep=NoSleep(clock))


class TestChatBudget:
    def test_a_fresh_chat_may_send(self, pacer: TelegramPacer) -> None:
        assert pacer.chat_ready(-100) is True

    async def test_the_burst_is_spent_then_the_chat_is_skipped(
        self, clock: FakeClock
    ) -> None:
        pacer = TelegramPacer(
            chat_rate_per_minute=15.0,
            chat_burst=3.0,
            clock=clock,
            sleep=NoSleep(clock),
        )
        for _ in range(3):
            assert pacer.chat_ready(-100) is True
            await pacer.acquire_chat(-100)
        assert pacer.chat_ready(-100) is False

    async def test_readiness_is_a_peek_and_takes_nothing(
        self, clock: FakeClock
    ) -> None:
        """Otherwise choosing a destination would silently spend its budget."""
        pacer = TelegramPacer(
            chat_rate_per_minute=15.0,
            chat_burst=1.0,
            clock=clock,
            sleep=NoSleep(clock),
        )
        for _ in range(5):
            assert pacer.chat_ready(-100) is True
        await pacer.acquire_chat(-100)
        assert pacer.chat_ready(-100) is False

    async def test_the_budget_refills_over_time(self, clock: FakeClock) -> None:
        pacer = TelegramPacer(
            chat_rate_per_minute=60.0,
            chat_burst=1.0,
            clock=clock,
            sleep=NoSleep(clock),
        )
        await pacer.acquire_chat(-100)
        assert pacer.chat_ready(-100) is False
        clock.advance(1.0)
        assert pacer.chat_ready(-100) is True

    async def test_one_chats_budget_does_not_touch_another(
        self, clock: FakeClock
    ) -> None:
        pacer = TelegramPacer(
            chat_rate_per_minute=15.0,
            chat_burst=1.0,
            clock=clock,
            sleep=NoSleep(clock),
        )
        await pacer.acquire_chat(-100)
        assert pacer.chat_ready(-100) is False
        assert pacer.chat_ready(-200) is True

    async def test_tracked_chats_are_bounded(self, clock: FakeClock) -> None:
        """A scan across many chats must not grow memory without limit."""
        pacer = TelegramPacer(max_chats=8, clock=clock, sleep=NoSleep(clock))
        for chat_id in range(50):
            await pacer.acquire_chat(-chat_id)
        assert pacer.health()["chats_tracked"] == 8


class TestGlobalBudget:
    async def test_it_paces_across_every_chat(self, clock: FakeClock) -> None:
        sleeper = NoSleep(clock)
        pacer = TelegramPacer(
            global_rate=1.0, global_burst=1.0, clock=clock, sleep=sleeper
        )
        await pacer.acquire_global()
        await pacer.acquire_global()
        assert sleeper.delays  # the second one had to wait

    def test_the_default_is_under_telegrams_per_token_ceiling(self) -> None:
        from ctb.delivery.pacing import GLOBAL_RATE_PER_SECOND

        assert GLOBAL_RATE_PER_SECOND <= 30.0
        assert CHAT_RATE_PER_MINUTE <= 20.0


class TestRateLimitBlast:
    def test_a_429_pauses_only_the_offending_chat(self, pacer: TelegramPacer) -> None:
        pacer.pause_chat(-100, 5.0)
        assert pacer.paused_for(-100) == pytest.approx(5.0)
        assert pacer.chat_ready(-100) is False
        assert pacer.chat_ready(-200) is True
        assert pacer.global_paused_for == 0.0

    def test_a_pause_expires(self, pacer: TelegramPacer, clock: FakeClock) -> None:
        pacer.pause_chat(-100, 5.0)
        clock.advance(6.0)
        assert pacer.paused_for(-100) == 0.0
        assert pacer.chat_ready(-100) is True

    def test_the_longer_of_two_pauses_wins(self, pacer: TelegramPacer) -> None:
        pacer.pause_chat(-100, 5.0)
        pacer.pause_chat(-100, 1.0)
        assert pacer.paused_for(-100) == pytest.approx(5.0)

    def test_three_distinct_chats_at_once_escalate_to_a_global_pause(
        self, pacer: TelegramPacer
    ) -> None:
        """Telegram's 429 does not say which limit was hit.

        Several unrelated chats reporting one inside a few seconds is the
        signature of the per-token limit rather than the per-group one, so the
        pacer backs everyone off. Documented as the heuristic it is.
        """
        for chat_id in (-100, -200, -300):
            pacer.pause_chat(chat_id, 5.0)
        assert pacer.global_paused_for > 0

    def test_one_chat_flooding_repeatedly_does_not_escalate(
        self, pacer: TelegramPacer
    ) -> None:
        for _ in range(5):
            pacer.pause_chat(-100, 5.0)
        assert pacer.global_paused_for == 0.0

    def test_chats_spread_over_time_do_not_escalate(
        self, pacer: TelegramPacer, clock: FakeClock
    ) -> None:
        for chat_id in (-100, -200, -300):
            pacer.pause_chat(chat_id, 5.0)
            clock.advance(20.0)
        assert pacer.global_paused_for == 0.0


class TestRotor:
    def test_least_recently_served_goes_first(self, clock: FakeClock) -> None:
        rotor = DestinationRotor(clock=clock)
        rotor.served((-1, 0))
        clock.advance(1.0)
        rotor.served((-2, 0))
        order = rotor.order([(-1, 0), (-2, 0), (-3, 0)], key=lambda item: item)
        assert order == [(-3, 0), (-1, 0), (-2, 0)]  # never served, then oldest

    def test_urgency_beats_fairness(self, clock: FakeClock) -> None:
        """An error must overtake bulk even in a topic that just sent."""
        rotor = DestinationRotor(clock=clock)
        rotor.served((-1, 0))
        order = rotor.order(
            [(-2, 0), (-1, 0)],
            key=lambda item: item,
            urgency=lambda item: 0 if item == (-1, 0) else 20,
        )
        assert order == [(-1, 0), (-2, 0)]

    def test_a_busy_destination_cannot_monopolise_the_rotation(
        self, clock: FakeClock
    ) -> None:
        rotor = DestinationRotor(clock=clock)
        destinations = [(-1, 0), (-2, 0)]
        served: list[tuple[int, int]] = []
        for _ in range(6):
            head = rotor.order(destinations, key=lambda item: item)[0]
            served.append(head)
            rotor.served(head)
            clock.advance(1.0)
        assert served.count((-1, 0)) == served.count((-2, 0)) == 3
