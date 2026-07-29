"""What a CI verdict looks like on a phone.

One line, the same vocabulary the status card uses, and at most two buttons.
The failure names the checks that went red because that is the part a person
can act on without opening anything; the pass says nothing but that it passed.
"""

from __future__ import annotations

from typing import Final

from aiogram.types import InlineKeyboardMarkup

from ctb.bot.keyboards import Action, NonceStore, button, keyboard, url_button
from ctb.db import NO_THREAD_ID
from ctb.delivery.render.html import escape
from ctb.github.client import ChecksResult, CheckState
from ctb.github.links import pull_request_url

__all__ = ["CI_BUTTON_TTL_S", "ci_keyboard", "ci_text"]

#: A CI notice is read when the phone is picked up, which is not when it was
#: posted. Six hours, against the fifteen minutes a mid-turn control gets:
#: "Fix CI" is not destructive, and a button that has to answer "expired" is
#: worse than no button at all.
CI_BUTTON_TTL_S: Final = 6 * 60 * 60.0

#: How many red check names fit on one phone line before it stops being one.
_MAX_NAMED: Final = 3


def ci_text(slug: str, checks: ChecksResult) -> str:
    """``⚠️ <b>CI failed</b> · owner/repo#12 · lint, types`` and its twin."""
    if checks.state is CheckState.SUCCESS:
        count = f" · {checks.total} check{'' if checks.total == 1 else 's'}"
        return f"✅ <b>CI passed</b> · {escape(slug)}{escape(count)}"
    named = ", ".join(checks.failed[:_MAX_NAMED])
    extra = len(checks.failed) - _MAX_NAMED
    if extra > 0:
        named += f" +{extra}"
    tail = f" · {named}" if named else ""
    return f"⚠️ <b>CI failed</b> · {escape(slug)}{escape(tail)}"


def ci_keyboard(
    *,
    state: CheckState,
    session_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    chat_id: int,
    thread_id: int = NO_THREAD_ID,
    store: NonceStore | None = None,
    failed_url: str | None = None,
) -> InlineKeyboardMarkup | None:
    """``🔧 Fix CI`` on a failure, a link to the run either way.

    The Fix CI payload is restartable, so the button still works after the
    redeploy that will almost certainly happen before anyone taps it.
    """
    row = []
    if state is CheckState.FAILURE:
        row.append(
            button(
                "🔧 Fix CI",
                Action.FIX_CI,
                session_id,
                store=store,
                chat_id=chat_id,
                thread_id=thread_id,
                ttl=CI_BUTTON_TTL_S,
                restartable=True,
            )
        )
        if failed_url:
            row.append(url_button("↗ Failing job", failed_url))
    else:
        row.append(
            url_button("↗ Pull request", pull_request_url(owner, repo, pr_number))
        )
    return keyboard([row]) if row else None
