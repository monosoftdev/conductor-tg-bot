"""Inline keyboards, and the single-use nonces that make them safe.

Two rules from PLAN §Safety rails:

* **Callback payloads carry ``(action, id, nonce)`` with single-use nonces.**
  Destructive confirmation expires in 60 seconds; safe phone controls remain
  usable for 15 minutes.
* **Confirm buttons contain the name** (``Archive api/fix-flaky``) so a mis-tap
  is visibly wrong before it is fatal.

Telegram caps ``callback_data`` at 64 **bytes**, and a Conductor workspace id is
a 36-character UUID — ``ctb:archive:<uuid>:<nonce>`` does not fit. So the nonce
*is* the handle: the payload is ``ctb:<action>:<nonce>`` and the registry holds
the full target plus the chat, user and label the button was minted for.
:func:`resolve` re-checks the action and the user, which means a payload
hand-crafted by a third party is rejected even before the allow-list is
consulted.

The registry is **in memory on purpose**: a *destructive* button that survives a
redeploy fails closed, and archive/done always require a fresh named confirm.

That rail cost the one control you actually reach for on a phone. PLAN's
headline live test is a redeploy *while a turn is running*, after which the
pinned card's ``Stop`` answered "This button has expired." So the card's
non-destructive controls (:data:`RESTARTABLE_ACTIONS`) mint a **self-describing**
payload instead of a random handle::

    ctb:stop:.<expiry-base36>.<session-id>

It is still registered in the store, so while the process lives it is
single-use and behaves exactly as before. Once the store is gone,
:func:`resolve` re-derives the ticket from the payload itself: the action must
be in :data:`RESTARTABLE_ACTIONS`, the wall-clock expiry must not have passed
(monotonic clocks do not survive a restart; wall clocks do), and the target
must be a bare id. Nothing destructive is reachable this way — the card mints
``archreq``, which only *opens* a named confirm, and that confirm is a fresh
60-second single-use nonce.

The trade is explicit: after a restart a restartable button is no longer
single-use, so a double tap can act twice. Stop, Retry, Transcript and Check
are all safe to repeat; ``clearq`` deletes queued prompts and is deliberately
left store-only.

``wiz`` joined that list for the same reason and pays the same price. The
``/new`` wizard's *state* is already DB-backed (``wizard_state``), so a redeploy
left a live wizard behind dead buttons. Its payload cannot name the choice —
branch names are arbitrary user text and the target charset has no ``:`` — so
the target is a wizard id plus a step letter and an option index
(``.<expiry>.<wid>b1``), and ``ctb.bot.wizards.new_workspace`` resolves that
index against the offered options it persisted in the FSM. The per-user check
lost with the store is *not* lost here: the FSM row is keyed by
``(chat_id, thread_id, user_id)``, so a tap by anyone else reads an empty
wizard and is refused.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ctb.db import NO_THREAD_ID
from ctb.turn.state import CardButton

__all__ = [
    "CALLBACK_PREFIX",
    "CONFIRM_TEMPLATE",
    "CONTROL_TTL_S",
    "MAX_BUTTON_TEXT",
    "NONCE_TTL_S",
    "RESTARTABLE_ACTIONS",
    "Action",
    "CardAction",
    "Cb",
    "NonceError",
    "NonceStore",
    "Ticket",
    "button",
    "card_button_label",
    "choice_keyboard",
    "confirm_keyboard",
    "confirm_label",
    "get_nonce_store",
    "keyboard",
    "parse",
    "quick_reply_keyboard",
    "quick_reply_label",
    "quick_replies_fit",
    "read_stateless",
    "reset_nonce_store",
    "resolve",
    "set_nonce_store",
    "stateless_payload",
    "status_card_keyboard",
    "truncate_label",
    "url_button",
]

#: Buttons expire after this long. PLAN §Safety rails.
NONCE_TTL_S: Final = 60.0
#: Safe controls stay useful when the phone was locked or Telegram was in the
#: background. Destructive confirmation still uses ``NONCE_TTL_S``.
CONTROL_TTL_S: Final = 15 * 60.0

CALLBACK_PREFIX: Final = "ctb"

#: Telegram truncates long button labels on narrow screens; keep the name
#: readable rather than letting the verb push it off-screen.
MAX_BUTTON_TEXT: Final = 48

#: The name is always in a confirm button. Change the shape here, not the rule.
CONFIRM_TEMPLATE: Final = "{verb} {name}"

_NONCE_BYTES: Final = 9  # -> 12 url-safe characters, no ':' to collide with sep
_PURGE_THRESHOLD: Final = 128
_MAX_TICKETS: Final = 4096


class Action(StrEnum):
    """Wire values for callback payloads. Short — 64 bytes is the whole budget.

    Handlers may also pass a raw string for an action this enum does not cover
    (see :func:`button`); it must be lowercase alphanumeric with ``_`` or ``-``
    and must not contain the separator.
    """

    STOP = "stop"
    RETRY = "retry"
    TRANSCRIPT = "tx"
    OPEN = "open"
    CHECK = "check"
    CLEAR_QUEUE = "clearq"
    ARCHIVE_REQUEST = "archreq"
    ARCHIVE = "archive"
    CONFIRM = "ok"
    CANCEL = "cancel"
    JUMP = "jump"
    #: Open a workspace that already exists in Conductor as a topic here.
    ADOPT = "adopt"
    DIFF = "diff"
    CHANGE = "change"
    #: A step of the bare ``/new`` wizard. See the module docstring.
    WIZARD = "wiz"
    DEFAULTS = "defaults"
    PICK = "pick"
    SEND = "send"
    PAGE = "page"


#: The status card's buttons, in the vocabulary the turn machine speaks.
CardAction: Final[dict[CardButton, Action]] = {
    CardButton.STOP: Action.STOP,
    CardButton.RETRY: Action.RETRY,
    CardButton.TRANSCRIPT: Action.TRANSCRIPT,
    CardButton.OPEN: Action.OPEN,
    CardButton.CHECK: Action.CHECK,
    CardButton.CLEAR_QUEUE: Action.CLEAR_QUEUE,
    CardButton.ARCHIVE: Action.ARCHIVE_REQUEST,
}

_CARD_LABELS: Final[dict[CardButton, str]] = {
    CardButton.STOP: "⏹ Stop",
    CardButton.RETRY: "↻ Retry",
    CardButton.TRANSCRIPT: "📄 Transcript",
    CardButton.OPEN: "↗ Open in Conductor",
    CardButton.CHECK: "🔍 Check",
    CardButton.CLEAR_QUEUE: "🧹 Clear queue",
    CardButton.ARCHIVE: "🗄 Archive",
}

_ALLOWED_ACTION_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")

#: Controls whose payload is allowed to outlive the process that minted it.
#: Every one of them is non-destructive: it stops, repeats, reads, picks a
#: wizard option, or *opens a named confirm*. Nothing here deletes anything.
#: ``clearq`` and ``archive`` are deliberately absent — see the module docstring.
RESTARTABLE_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        Action.STOP.value,
        Action.RETRY.value,
        Action.TRANSCRIPT.value,
        Action.CHECK.value,
        Action.ARCHIVE_REQUEST.value,
        Action.WIZARD.value,
    }
)

#: Marks a self-describing payload. Not in the url-safe base64 alphabet
#: ``secrets.token_urlsafe`` draws from, so it can never collide with a nonce.
_STATELESS_MARK: Final = "."
#: Keeps ``ctb:archreq:.<expiry>.<uuid>`` inside Telegram's 64-byte cap.
_MAX_STATELESS_TARGET: Final = 40
_STATELESS_TARGET_CHARS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


class Cb(CallbackData, prefix=CALLBACK_PREFIX):
    """``ctb:<action>:<nonce>`` — everything else lives in :class:`NonceStore`."""

    action: str
    nonce: str


class NonceError(Exception):
    """A callback payload that must not be acted on.

    ``reason`` is one of ``malformed``, ``unknown``, ``expired``, ``used``,
    ``mismatch``, ``wrong_user``. All of them are shown to the user as the same
    thing — the button no longer works — because distinguishing them for a
    caller who tampered with the payload leaks nothing useful.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)

    @property
    def user_message(self) -> str:
        if self.reason == "used":
            return "Already done — that button was single-use."
        return "This button has expired. Run the command again."


@dataclass(frozen=True, slots=True)
class Ticket:
    """What a button was minted to do."""

    nonce: str
    action: str
    target: str
    label: str = ""
    user_id: int | None = None
    chat_id: int | None = None
    thread_id: int = NO_THREAD_ID
    issued_at: float = 0.0
    expires_at: float = 0.0

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    @property
    def callback_data(self) -> str:
        return Cb(action=self.action, nonce=self.nonce).pack()


class NonceStore:
    """Issue, then consume-exactly-once, the tickets behind inline buttons."""

    def __init__(
        self,
        *,
        ttl: float = NONCE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = _MAX_TICKETS,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._max = max_entries
        self._tickets: dict[str, Ticket] = {}
        #: Nonces already spent, kept until they would have expired anyway so a
        #: double tap says "already done" instead of "expired".
        self._used: dict[str, float] = {}

    # -- issue -----------------------------------------------------------------

    def issue(
        self,
        action: Action | str,
        target: str,
        *,
        label: str = "",
        user_id: int | None = None,
        chat_id: int | None = None,
        thread_id: int = NO_THREAD_ID,
        ttl: float | None = None,
        nonce: str | None = None,
    ) -> Ticket:
        """Mint a single-use ticket. ``target`` is whatever the handler needs.

        ``nonce`` overrides the random handle with a caller-supplied one — used
        only for the self-describing card payloads, which have to be readable
        without this store. It is still registered here, so single-use and the
        monotonic TTL hold for as long as the process does.
        """
        now = self._clock()
        if len(self._tickets) + len(self._used) >= _PURGE_THRESHOLD:
            self.purge()
        if len(self._tickets) >= self._max:  # pragma: no cover - flood guard
            oldest = min(self._tickets, key=lambda n: self._tickets[n].issued_at)
            del self._tickets[oldest]
        lifetime = self._ttl if ttl is None else ttl
        ticket = Ticket(
            nonce=nonce or secrets.token_urlsafe(_NONCE_BYTES),
            action=_action_value(action),
            target=target,
            label=label,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            issued_at=now,
            expires_at=now + lifetime,
        )
        self._tickets[ticket.nonce] = ticket
        return ticket

    # -- consume ---------------------------------------------------------------

    def consume(
        self,
        nonce: str,
        *,
        action: Action | str | None = None,
        user_id: int | None = None,
    ) -> Ticket:
        """Spend a ticket. Raises :class:`NonceError`; never returns twice."""
        now = self._clock()
        ticket = self._tickets.get(nonce)
        if ticket is None:
            raise NonceError("used" if nonce in self._used else "unknown")
        if ticket.is_expired(now):
            del self._tickets[nonce]
            raise NonceError("expired")
        if action is not None and ticket.action != _action_value(action):
            raise NonceError("mismatch")
        if (
            user_id is not None
            and ticket.user_id is not None
            and ticket.user_id != user_id
        ):
            raise NonceError("wrong_user")
        del self._tickets[nonce]
        self._used[nonce] = ticket.expires_at
        return ticket

    def mark_used(self, nonce: str, *, ttl: float | None = None) -> None:
        """Record a spend this store did not mint (a restart-proof payload).

        Stamped with *this* store's clock so :meth:`purge` can reclaim it.
        """
        self._tickets.pop(nonce, None)
        self._used[nonce] = self._clock() + (self._ttl if ttl is None else ttl)

    def peek(self, nonce: str) -> Ticket | None:
        """Look without spending. Returns ``None`` once expired."""
        ticket = self._tickets.get(nonce)
        if ticket is None or ticket.is_expired(self._clock()):
            return None
        return ticket

    # -- housekeeping ----------------------------------------------------------

    def revoke(self, nonce: str) -> bool:
        ticket = self._tickets.pop(nonce, None)
        if ticket is None:
            return False
        # Burned, not forgotten: a forgotten restart-proof payload would fall
        # through to :func:`read_stateless` and come back to life.
        self._used[nonce] = ticket.expires_at
        return True

    def revoke_target(self, target: str, *, action: Action | str | None = None) -> int:
        """Invalidate every live button pointing at ``target``.

        Used when the thing a button acts on is gone — an archived workspace's
        ``Stop`` should not be tappable at all.
        """
        wanted = None if action is None else _action_value(action)
        doomed = [
            nonce
            for nonce, ticket in self._tickets.items()
            if ticket.target == target and (wanted is None or ticket.action == wanted)
        ]
        for nonce in doomed:
            self.revoke(nonce)
        return len(doomed)

    def purge(self) -> int:
        now = self._clock()
        expired = [n for n, t in self._tickets.items() if t.is_expired(now)]
        for nonce in expired:
            del self._tickets[nonce]
        for nonce in [n for n, exp in self._used.items() if now >= exp]:
            del self._used[nonce]
        return len(expired)

    def clear(self) -> None:
        self._tickets.clear()
        self._used.clear()

    def __len__(self) -> int:
        return len(self._tickets)


_store: NonceStore | None = None


def get_nonce_store() -> NonceStore:
    """The process-wide store. ``build_app`` installs the real one at boot."""
    global _store
    if _store is None:
        _store = NonceStore()
    return _store


def set_nonce_store(store: NonceStore) -> None:
    global _store
    _store = store


def reset_nonce_store() -> None:
    global _store
    _store = None


def _action_value(action: Action | str) -> str:
    value = action.value if isinstance(action, Action) else action
    if not value or not set(value) <= _ALLOWED_ACTION_CHARS:
        raise ValueError(f"action {value!r} must be lowercase [a-z0-9_-] and non-empty")
    return value


# -- restart-proof payloads ----------------------------------------------------


def _base36(value: int) -> str:
    if value <= 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out: list[str] = []
    while value:
        value, rest = divmod(value, 36)
        out.append(digits[rest])
    return "".join(reversed(out))


def stateless_payload(action: Action | str, target: str, ttl: float) -> str | None:
    """``.<expiry-base36>.<target>``, or ``None`` when it does not apply.

    Wall clock, not monotonic: the whole point is that a process which did not
    mint this can still read it.
    """
    if _action_value(action) not in RESTARTABLE_ACTIONS:
        return None
    if not target or len(target) > _MAX_STATELESS_TARGET:
        return None
    if not set(target) <= _STATELESS_TARGET_CHARS:
        return None
    expiry = int(time.time() + max(ttl, 0.0))
    return f"{_STATELESS_MARK}{_base36(expiry)}{_STATELESS_MARK}{target}"


def read_stateless(nonce: str, action: str, *, now: float | None = None) -> Ticket:
    """Re-derive a ticket from a payload the store no longer remembers.

    Raises :class:`NonceError` exactly like :meth:`NonceStore.consume`, so a
    tampered or stale payload is indistinguishable from an expired button.
    """
    if not nonce.startswith(_STATELESS_MARK):
        raise NonceError("unknown")
    stamp, mark, target = nonce[len(_STATELESS_MARK) :].partition(_STATELESS_MARK)
    if not stamp or not mark or not target:
        raise NonceError("malformed")
    if action not in RESTARTABLE_ACTIONS:
        raise NonceError("mismatch")
    if not set(target) <= _STATELESS_TARGET_CHARS:
        raise NonceError("malformed")
    try:
        expires_at = float(int(stamp, 36))
    except ValueError as exc:
        raise NonceError("malformed", repr(exc)) from exc
    if (time.time() if now is None else now) >= expires_at:
        raise NonceError("expired")
    return Ticket(nonce=nonce, action=action, target=target, expires_at=expires_at)


# -- builders ------------------------------------------------------------------


def truncate_label(text: str, limit: int = MAX_BUTTON_TEXT) -> str:
    """Keep the tail — the distinguishing part of ``proj/branch`` is the end."""
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :]


def confirm_label(verb: str, name: str, limit: int = MAX_BUTTON_TEXT) -> str:
    """``Archive api/fix-flaky`` — the name is never optional."""
    room = max(1, limit - len(verb) - 1)
    return CONFIRM_TEMPLATE.format(verb=verb, name=truncate_label(name, room))


def card_button_label(kind: CardButton) -> str:
    return _CARD_LABELS.get(kind, str(kind.value).replace("_", " ").title())


def button(
    text: str,
    action: Action | str,
    target: str,
    *,
    store: NonceStore | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    thread_id: int = NO_THREAD_ID,
    ttl: float | None = None,
    style: str | None = None,
    restartable: bool = False,
) -> InlineKeyboardButton:
    """A nonce-backed button. The ticket is minted here and nowhere else.

    ``restartable`` asks for a self-describing payload so the button still
    works after a redeploy. It is honoured only for
    :data:`RESTARTABLE_ACTIONS`; anything else silently gets the usual random
    single-use handle, which is the safe direction to fail.
    """
    registry = store if store is not None else get_nonce_store()
    label = truncate_label(text)
    lifetime = NONCE_TTL_S if ttl is None else ttl
    payload = stateless_payload(action, target, lifetime) if restartable else None
    ticket = registry.issue(
        action,
        target,
        label=label,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        ttl=ttl,
        nonce=payload,
    )
    return InlineKeyboardButton(
        text=label,
        callback_data=ticket.callback_data,
        style=style or _style_for(action),
    )


def url_button(
    text: str, url: str, *, style: str | None = "primary"
) -> InlineKeyboardButton:
    """A link button. No nonce: a deep link cannot go stale or do damage."""
    return InlineKeyboardButton(text=truncate_label(text), url=url, style=style)


def keyboard(
    rows: Iterable[Sequence[InlineKeyboardButton]],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def confirm_keyboard(
    action: Action | str,
    target: str,
    name: str,
    *,
    verb: str = "Confirm",
    cancel_text: str = "Cancel",
    cancel_action: Action | str = Action.CANCEL,
    store: NonceStore | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    thread_id: int = NO_THREAD_ID,
) -> InlineKeyboardMarkup:
    """Two-tap confirm. The destructive button carries the name it will hit."""
    registry = store if store is not None else get_nonce_store()
    return keyboard(
        [
            [
                button(
                    confirm_label(verb, name),
                    action,
                    target,
                    store=registry,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    ttl=NONCE_TTL_S,
                    style="danger",
                )
            ],
            [
                button(
                    cancel_text,
                    cancel_action,
                    target,
                    store=registry,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    ttl=NONCE_TTL_S,
                )
            ],
        ]
    )


def choice_keyboard(
    options: Sequence[tuple[str, str]],
    action: Action | str,
    *,
    columns: int = 1,
    store: NonceStore | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    thread_id: int = NO_THREAD_ID,
    extra: Sequence[InlineKeyboardButton] = (),
) -> InlineKeyboardMarkup:
    """``[(label, target), …]`` → a grid. Model pickers, ``/board``, wizards."""
    registry = store if store is not None else get_nonce_store()
    buttons = [
        button(
            label,
            action,
            target,
            store=registry,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        for label, target in options
    ]
    width = max(1, columns)
    rows: list[list[InlineKeyboardButton]] = [
        buttons[i : i + width] for i in range(0, len(buttons), width)
    ]
    if extra:
        rows.append(list(extra))
    return keyboard(rows)


def status_card_keyboard(
    buttons: Sequence[CardButton],
    target: str,
    *,
    deep_link: str | None = None,
    columns: int = 2,
    store: NonceStore | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    thread_id: int = NO_THREAD_ID,
) -> InlineKeyboardMarkup | None:
    """The pinned card's buttons, straight from ``EditStatusCard.buttons``.

    ``CardButton.OPEN`` becomes a URL button when a ``deepLink`` is known and is
    dropped when it is not — an "Open in Conductor" that cannot open is worse
    than no button.
    """
    registry = store if store is not None else get_nonce_store()
    row: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []
    for kind in buttons:
        if kind is CardButton.OPEN:
            if not deep_link:
                continue
            item = url_button(card_button_label(kind), deep_link)
        else:
            item = button(
                card_button_label(kind),
                CardAction[kind],
                target,
                store=registry,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                ttl=CONTROL_TTL_S,
                # The card is the control surface you reach for mid-turn; it
                # has to survive the redeploy that happens mid-turn.
                restartable=True,
            )
        row.append(item)
        if len(row) >= max(1, columns):
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return keyboard(rows) if rows else None


def quick_reply_label(index: int, choice: str) -> str:
    """The exact label :func:`quick_reply_keyboard` will put on the button."""
    return f"{'✓ ' if index == 1 else ''}{index} · {' '.join(choice.split())[:160]}"


def quick_replies_fit(choices: Sequence[str]) -> bool:
    """True when every option is readable *in full* on its button.

    The caller strips the ``Choices:`` text from the message only when this
    holds: an option the user cannot finish reading is worse than a duplicate.
    """
    if not choices:
        return False
    return all(
        len(quick_reply_label(index, choice)) <= MAX_BUTTON_TEXT
        for index, choice in enumerate(choices[:4], start=1)
    )


def quick_reply_keyboard(
    choices: Sequence[str],
    session_id: str,
    *,
    store: NonceStore | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    thread_id: int = NO_THREAD_ID,
) -> InlineKeyboardMarkup | None:
    """One-tap replies for a final ``Choices:`` block from the agent."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, choice in enumerate(choices[:4], start=1):
        cleaned = " ".join(choice.split())[:160]
        if not cleaned:
            continue
        rows.append(
            [
                button(
                    quick_reply_label(index, cleaned),
                    Action.SEND,
                    f"{session_id}\nChoose option {index}: {cleaned}",
                    store=store,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    ttl=CONTROL_TTL_S,
                    style="success" if index == 1 else "primary",
                )
            ]
        )
    return keyboard(rows) if rows else None


# -- resolve -------------------------------------------------------------------


def parse(data: str | None) -> Cb:
    """Unpack a payload. Raises :class:`NonceError` rather than aiogram's."""
    if not data:
        raise NonceError("malformed")
    try:
        return Cb.unpack(data)
    except Exception as exc:
        raise NonceError("malformed", repr(exc)) from exc


def resolve(
    query: CallbackQuery,
    *,
    expect: Action | str | None = None,
    store: NonceStore | None = None,
    enforce_user: bool = True,
) -> Ticket:
    """Validate a tapped button and spend its nonce.

    Raises :class:`NonceError`; handlers answer ``exc.user_message``. There is
    no "just this once" path — a payload that does not resolve is not acted on.

    A :data:`RESTARTABLE_ACTIONS` payload the store has never heard of is
    re-derived from the payload itself — that, and only that, is how a card or
    wizard button minted before a redeploy still works after it. It is not a
    hole in the allow-list: this runs behind ``AuthMiddleware``, so anyone who
    can present a payload at all is already allow-listed. The per-user check is
    the one thing lost with the store; ``wiz`` gets it back from the FSM row,
    which is keyed by ``(chat_id, thread_id, user_id)``.

    Order matters: the store is asked first, so a ticket it *does* know — spent,
    expired, revoked, or minted for someone else — is refused as before. Only
    ``unknown`` falls through.
    """
    registry = store if store is not None else get_nonce_store()
    payload = parse(query.data)
    if expect is not None and payload.action != _action_value(expect):
        raise NonceError("mismatch")
    user_id = query.from_user.id if enforce_user and query.from_user else None
    try:
        return registry.consume(payload.nonce, action=payload.action, user_id=user_id)
    except NonceError as exc:
        if exc.reason != "unknown":
            raise
        ticket = read_stateless(payload.nonce, payload.action)
        # Record the spend so a double tap inside this process still says
        # "already done"; after the next restart it is single-use no more.
        # Held for exactly as long as the payload itself would have lived —
        # a 30-minute wizard button must not become re-tappable at 15.
        registry.mark_used(ticket.nonce, ttl=max(0.0, ticket.expires_at - time.time()))
        return ticket


def _style_for(action: Action | str) -> str | None:
    value = _action_value(action)
    if value in {
        Action.STOP.value,
        Action.CLEAR_QUEUE.value,
        Action.ARCHIVE_REQUEST.value,
        Action.ARCHIVE.value,
    }:
        return "danger"
    if value in {Action.RETRY.value, Action.CONFIRM.value}:
        return "success"
    if value in {
        Action.TRANSCRIPT.value,
        Action.CHECK.value,
        Action.SEND.value,
        Action.PICK.value,
        Action.PAGE.value,
        Action.ADOPT.value,
    }:
        return "primary"
    return None
