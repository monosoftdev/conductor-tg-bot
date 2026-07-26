"""Scripted fakes: a Conductor API that replays the sequences that break pollers.

``fake_conductor.py`` lives here — the queued-idle trap, the fast turn that
starts and finishes between two polls, the error mid-turn, the replay attack.
The reliability of this project comes from the pure state machine being tested
against these, offline, before any network or Telegram code exists to hide its
bugs.

Start with :data:`tests.fakes.fake_conductor.SCENARIOS`, or import a named
builder directly::

    from tests.fakes.fake_conductor import FakeConductor, Tick, queued_idle_trap

``tests/fakes/test_fake_selfcheck.py`` pins every property those tests rely on —
a fake that lies would invalidate all of them.
"""
