"""conductor-tg-bot — drive Conductor cloud coding agents from Telegram.

The one rule that decides everything: the transcript cursor is the source of
truth for content. ``GET /status`` is only a cadence knob and a UX hint — it
never gates delivery.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: The Conductor API sits behind a proxy that 403s some default client
#: signatures (the docs call out Python's ``urllib``). Every outbound HTTP call
#: must send this explicitly.
USER_AGENT = f"conductor-tg-bot/{__version__}"

__all__ = ["USER_AGENT", "__version__"]
