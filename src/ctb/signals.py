"""One vocabulary for "what is this session doing", used by every surface.

The topic title, the pinned status card and ``/board`` all answer the same
question, and they used to answer it with different glyphs: a working session
was ``●`` in the topic list and ``⚙️`` on the card, and a finished one was
*nothing at all* in the topic list but ``✅`` on the card. Three vocabularies
for one fact is three things to learn, and the disagreement is invisible until
you see two of them side by side.

So the glyphs live here, once, and each surface renders from this table.

Two Telegram constraints shaped it, both verified rather than assumed:

* **Not every emoji is a valid reaction.** ``setMessageReaction`` accepts a
  fixed set, and ``✅`` and ``⏳`` are *not* in it — the obvious "done" and
  "waiting" reactions both fail with ``REACTION_INVALID``. They are fine here,
  because a title prefix and a card are ordinary text. :data:`REACTION_SAFE`
  is the subset that may be used as an actual reaction.
* **A topic's ``icon_color`` cannot be changed after creation**, so colour can
  only ever carry *identity* (which workspace), never *state*. State goes on
  the title prefix, and on ``icon_custom_emoji_id``, which *can* be changed on
  every rename — see :mod:`ctb.bot.handlers.topics`.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ARCHIVED",
    "CANCELLED",
    "DONE",
    "ERROR",
    "IDLE",
    "REACTION_SAFE",
    "SLEEPING",
    "UNREACHABLE",
    "WORKING",
    "WAITING",
]

#: Queued, waking, initializing — the bot has the work but nothing is running.
WAITING: Final = "⏳"
#: A turn is running right now.
WORKING: Final = "⚙️"
#: A turn finished and produced something you have not acted on yet.
DONE: Final = "✅"
#: Bound and quiet: nothing running, nothing new to read.
IDLE: Final = ""
#: The session reported an error. Persistent — it can sit here indefinitely.
ERROR: Final = "⚠️"
#: Stopped on request.
CANCELLED: Final = "🛑"
#: The workspace is asleep; a prompt may wake it.
SLEEPING: Final = "💤"
#: Archived or deleted in Conductor. The topic is closed too.
ARCHIVED: Final = "🗄"
#: Conductor 404s for this session — it is gone, not merely quiet.
UNREACHABLE: Final = "🚫"

#: The emoji ``setMessageReaction`` actually accepts, filtered to the ones this
#: bot has a use for. Reacting with anything outside Telegram's fixed list
#: raises ``REACTION_INVALID``, which is why the receipt vocabulary cannot
#: simply reuse the glyphs above: ``✅`` is not a legal reaction.
REACTION_SAFE: Final[frozenset[str]] = frozenset(
    {"👀", "👍", "👌", "😴", "🔥", "💯", "⚡", "🎉", "🤝", "🙏", "😢", "🤔"}
)
