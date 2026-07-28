"""The launcher: the two things a chat with nothing open can do.

Telegram's threaded DM has no addressable root. The seat the client calls *New
Chat* is a composer — anything sent there, command or not, makes it open a
thread named after that first line — so there is no message the bot can pin
there, no inline keyboard it can leave standing, and no room it can answer in.

A **reply keyboard** is the one control surface that survives that, because it
belongs to the chat rather than to a message or a thread. It is visible from
New Chat and from every workspace room of that private chat, without the bot
having to keep a card alive in any of them.

**A private chat only.** Telegram shows a reply keyboard to *everyone* in a
group, and this is a personal control — a group's General is a real, readable
room with none of the problem above, so there is nothing for it to solve there.

That constrains what may go on it, and the constraint is the design:

* Both entries **consume** the thread their press creates. Pressing *New
  workspace* from New Chat opens a thread and that thread becomes the
  workspace; pressing *Attach existing* opens one and the adopted workspace
  moves into it. Nothing is left behind.
* A ``/board`` button was drafted here and cut. It answers and then abandons
  the room it was pressed in, and in a threaded DM the topic list already *is*
  the board — with live state icons the bot could not draw in a message.

The router sits at order 4, ahead of the wizard, so a press is never swallowed
as an answer to a half-finished form: somebody who taps *New workspace* in the
middle of ``/new`` means start over, not "the branch is called ➕ New
workspace".
"""

from __future__ import annotations

from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ctb.bot.app import register_router
from ctb.bot.handlers.common import abandon_wizard, tell
from ctb.bot.handlers.core import attach_workspace
from ctb.bot.keyboards import (
    HOME_ATTACH,
    HOME_LABELS,
    NonceStore,
    home_keyboard,
)
from ctb.bot.middleware.routing import Route
from ctb.bot.middleware.tenancy import TenantContext
from ctb.db.connection import Database

router = Router(name=__name__)
register_router(router, order=4)

#: Shown with the keyboard, once, by ``/home``. It says the one thing that is
#: not obvious from the two buttons: that typing works too, and where it lands.
HOME_TEXT: Final = (
    "<b>Ready.</b>\n"
    "Describe a task and it becomes a workspace in its own thread.\n"
    "<code>/board</code> lists what is running · <code>/help</code> for the rest."
)


#: Said instead of a keyboard in a group, where one would be everybody's.
GROUP_TEXT: Final = (
    "The launcher is for your private chat with me · "
    "here, use <code>/new</code> and <code>/attach</code>."
)


@router.message(Command("home", "menu"))
async def home(message: Message, state: FSMContext) -> None:
    """Put the launcher back under the composer.

    Needed because a reply keyboard is not permanent state the bot can query —
    Telegram shows the last one it was sent, whatever that was, and a person who
    hid it has no other way to ask for it again.
    """
    await abandon_wizard(state)
    if message.chat.type != "private":
        await tell(message, GROUP_TEXT)
        return
    await tell(message, HOME_TEXT, reply_markup=home_keyboard())


@router.message(F.text.in_(HOME_LABELS))
async def launcher(
    message: Message,
    route: Route,
    tenant: TenantContext,
    state: FSMContext,
    nonces: NonceStore,
    db: Database | None = None,
) -> None:
    """One handler for the whole reserved vocabulary.

    Registered against :data:`~ctb.bot.keyboards.HOME_LABELS` itself rather than
    against the two strings, so the set that *documents* the reservation is the
    set that *enforces* it — a label added to the keyboard and forgotten here
    would otherwise be sent to an agent as a prompt.
    """
    from ctb.bot.wizards.new_workspace import start_wizard

    await abandon_wizard(state)
    if message.text == HOME_ATTACH:
        # `query=""`, never the default: this message's text is the button's own
        # label, and `command_text` would spend it as a search term.
        await attach_workspace(message, tenant, state, nonces, db=db, query="")
        return
    await start_wizard(message, route=route, tenant=tenant, state=state, db=db)
