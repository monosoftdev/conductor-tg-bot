"""The ``/help`` card, which had no test at all until DM topics landed.

Two things can rot here and nothing else would notice: the card can name a
command that no longer exists, and it can describe a room the reader does not
have. The default install is now a private chat with no group, so "General" —
the group's cockpit topic — is a place a DM-only user cannot go.
"""

from __future__ import annotations

import re

from ctb.bot import app as bot_app
from ctb.bot.app import clear_routers
from ctb.bot.handlers.power import _HELP

#: Commands the card advertises, e.g. ``<code>/use name</code>`` → ``use``.
_ADVERTISED = re.compile(r"<code>/([a-z]+)")


def test_help_names_only_commands_that_exist() -> None:
    """A card entry with no handler is a dead instruction on a phone."""
    clear_routers()
    try:
        routers = bot_app.discover_routers()
    finally:
        clear_routers()

    handled: set[str] = set()
    for router in routers:
        for handler in router.message.handlers:
            for flt in handler.filters or ():
                callback = getattr(flt, "callback", None)
                handled.update(getattr(callback, "commands", ()) or ())

    advertised = set(_ADVERTISED.findall(_HELP))
    assert advertised, "the help card advertises nothing at all"
    assert advertised <= handled, advertised - handled


def test_help_offers_the_optional_group_and_never_assumes_one() -> None:
    """The default install is a DM. A group is something you may add.

    ``General`` only exists inside a supergroup with Topics on, so naming it as
    *the* place to run ``/new`` is wrong for the majority path.
    """
    assert "General" not in _HELP
    assert "/team" in _HELP
    assert "optional" in _HELP


def test_help_stays_a_phone_sized_card() -> None:
    """It is read on a phone, one thumb, between other messages."""
    lines = _HELP.splitlines()
    assert len(lines) <= 20, len(lines)
    longest = max(len(re.sub(r"<[^>]+>", "", line)) for line in lines)
    assert longest <= 60, longest
