"""Why a session that ought to be polling is not — and what to tell its owner.

``sessions.list_silent`` answers *whether* the bot has stopped doing its job.
This module answers *why*, and the distinction is load-bearing twice over:

* **It decides whether a restart is worth trying.** Railway restarts on a failed
  healthcheck, so a 503 must mean "cycling this process is a plausible fix".
  A rejected key is not — a restart cannot conjure a valid one. Conductor being
  down is not — a restart re-opens the circuit against the same dead upstream
  and throws away the backoff. But a process that is *silently not trying*, with
  no rejection and no upstream failure to point at, is the wedge a restart does
  fix, and it is exactly the shape of the four-day outage.

* **It decides what the owner is told.** "Your workspace has gone quiet" is not
  actionable. "Conductor rejected this team's API key — send /key" is.

The discriminator is ``api_events``, not a live client, and that is deliberate.
When polling stops the client pool is swept, so by the time anybody asks there
is no client left to interrogate — the same blindness that let the outage hide.
The event log survives the workers that wrote it:

=============================  ===========================================
observation                    reading
=============================  ===========================================
no API calls at all            nothing is even trying — *this* is the wedge
calls fail, stamp set          the key was rejected; only a new one helps
calls fail, no stamp           the upstream is down; waiting is the fix
=============================  ===========================================

The order matters and was once the other way round. See :func:`attribute`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "SILENCE_LOOKBACK_MS",
    "SilenceReason",
    "attribute",
    "notice_html",
    "recovered_html",
]

#: How far back to look for evidence that the bot was trying. Comfortably wider
#: than the silence threshold itself, so a tenant that has been quiet for eleven
#: minutes is judged on a window that could actually contain its last attempt.
SILENCE_LOOKBACK_MS: Final[int] = 30 * 60_000


class SilenceReason(StrEnum):
    """Why nothing is polling. Stable strings — ``/health`` reports them."""

    AUTH_REJECTED = "auth_rejected"
    CONDUCTOR_UNREACHABLE = "conductor_unreachable"
    UNEXPLAINED = "unexplained"

    @property
    def is_explained(self) -> bool:
        """Whether something other than this process is the cause.

        An explained silence must never fail the healthcheck: restarting into
        a rejected key or a dead upstream is a restart loop that replaces one
        outage with a worse one.
        """
        return self is not SilenceReason.UNEXPLAINED


def attribute(*, auth_failed: bool, api_calls: int, api_ok: int) -> SilenceReason:
    """Read the cause off the tenant row and its recent API events.

    **Zero calls outranks everything, including the tenant row.** ``auth_failed``
    used to win outright, on the reasoning that a latched tenant makes no calls
    *because* it was latched and so cannot be told apart from the wedge. That
    reasoning stopped being true when the latch got a clock: ``list_bound``
    readmits one poller every :data:`~ctb.db.repo.tenancy.AUTH_RETRY_AFTER_MS` to
    ask again, and that poller's request lands in ``api_events`` whatever answer
    it gets. A genuinely rejected key therefore shows up as *calls that all fail*
    — never as no calls at all, not across a window twice the retry interval.

    So "a stamp is set and nothing has been tried for half an hour" does not
    describe a rejected key. It describes a process that has stopped polling,
    which is the one thing a restart fixes — and reading it as *explained* is
    precisely what let a wedged circuit breaker run for sixteen days behind a
    rejection that had stopped happening weeks earlier.
    """
    if api_calls == 0:
        return SilenceReason.UNEXPLAINED
    if api_ok == 0:
        return (
            SilenceReason.AUTH_REJECTED
            if auth_failed
            else SilenceReason.CONDUCTOR_UNREACHABLE
        )
    return SilenceReason.UNEXPLAINED


def notice_html(reason: SilenceReason, *, sessions: int, silent_for_ms: int) -> str:
    """The message the owner actually receives. HTML, per the parse-mode rule."""
    minutes = max(1, silent_for_ms // 60_000)
    room = "task" if sessions == 1 else "tasks"
    head = f"⚠️ Nothing has been checking {sessions} {room} for {minutes} minutes."
    if reason is SilenceReason.AUTH_REJECTED:
        return (
            f"{head}\n\nConductor rejected this team's API key. Send "
            "<code>/key</code> in a private message to set a new one — or wait, "
            "and the bot will try the stored key again by itself shortly."
        )
    if reason is SilenceReason.CONDUCTOR_UNREACHABLE:
        return (
            f"{head}\n\nEvery recent call to Conductor has failed, so this is "
            "almost certainly Conductor rather than the bot. Nothing to do — "
            "polling resumes on its own, and no message is lost while it waits."
        )
    return (
        f"{head}\n\nNo rejected key and no failing upstream to blame, so this "
        "one is the bot's fault. It has been reported and the process will "
        "recycle itself. Nothing queued is lost; replies arrive when it returns."
    )


def recovered_html(*, sessions: int) -> str:
    room = "task" if sessions == 1 else "tasks"
    return f"✅ Back to normal — {sessions} {room} are being watched again."
