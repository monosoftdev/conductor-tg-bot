"""Restart-survivable callback payloads carry a MAC, and it is load-bearing.

Buttons that must keep working across a redeploy cannot be random handles in a
process's memory, so they describe themselves: action, expiry, target. That
makes every field attacker-supplied unless it is signed.

Unsigned, the consequences were concrete: the expiry was whatever the tapper
wrote, so "single use" and "expires in 15 minutes" were decorative; and the
target was any id they had ever seen, so a ``stop`` payload could name another
workspace's session.
"""

from __future__ import annotations

import time
from typing import cast

import pytest
from aiogram.types import CallbackQuery

from ctb.bot.keyboards import (
    Action,
    NonceError,
    NonceStore,
    read_stateless,
    resolve,
    stateless_payload,
)
from ctb.crypto import SecretBox, generate_master_key

SESSION = "0d1e6f45-8a11-4b7c-9c3e-2f5a6b7c8d9e"
VICTIM = "ffffffff-8a11-4b7c-9c3e-2f5a6b7c8d9e"


def payload(action: str = "stop", target: str = SESSION, ttl: float = 900.0) -> str:
    minted = stateless_payload(action, target, ttl)
    assert minted is not None
    return minted


class TestSignature:
    def test_a_minted_payload_verifies(self) -> None:
        ticket = read_stateless(payload(), "stop")
        assert ticket.target == SESSION

    def test_the_target_cannot_be_repointed(self) -> None:
        """The whole attack: take a card you saw, aim it at someone else."""
        stamp_and_sig = payload().split(".")[1]
        forged = f".{stamp_and_sig}.{VICTIM}"
        with pytest.raises(NonceError) as exc:
            read_stateless(forged, "stop")
        assert exc.value.reason == "forged"

    def test_the_expiry_cannot_be_extended(self) -> None:
        """Unsigned, a far-future base36 stamp made a button live forever."""
        minted = payload(ttl=1.0)
        signature = minted.split(".")[1][-8:]
        forever = f".{'zzzzzz'}{signature}.{SESSION}"
        with pytest.raises(NonceError) as exc:
            read_stateless(forever, "stop")
        assert exc.value.reason == "forged"

    def test_an_invented_payload_is_refused(self) -> None:
        with pytest.raises(NonceError):
            read_stateless(f".zzzzzz1234567890.{VICTIM}", "stop")

    def test_a_signature_from_another_action_does_not_transfer(self) -> None:
        """Otherwise a `transcript` button becomes a `stop` button."""
        borrowed = payload("tx").split(".")[1]
        with pytest.raises(NonceError) as exc:
            read_stateless(f".{borrowed}.{SESSION}", "stop")
        assert exc.value.reason == "forged"

    def test_a_payload_from_another_deployment_does_not_verify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The key is derived from the master key, so instances do not share."""
        theirs = payload()
        from ctb.bot import keyboards

        keyboards.set_signing_key(
            SecretBox.from_env_value(generate_master_key("other")).derive("callback")
        )
        with pytest.raises(NonceError) as exc:
            read_stateless(theirs, "stop")
        assert exc.value.reason == "forged"

    def test_expiry_is_still_enforced_on_a_genuine_payload(self) -> None:
        genuine = payload(ttl=0.0)
        with pytest.raises(NonceError) as exc:
            read_stateless(genuine, "stop", now=time.time() + 60)
        assert exc.value.reason == "expired"


class TestBudget:
    def test_every_signed_payload_fits_telegrams_64_bytes(self) -> None:
        """Telegram rejects a longer ``callback_data`` outright."""
        store = NonceStore()
        for action in Action:
            ticket = store.issue(action, SESSION)
            assert len(ticket.callback_data.encode()) <= 64, action


class TestStoreStillWins:
    def test_a_ticket_the_store_knows_is_judged_by_the_store(self) -> None:
        """The signature is the fallback, not a way around single-use."""
        store = NonceStore()
        ticket = store.issue(Action.STOP, SESSION)
        query = _Query(ticket.callback_data)

        assert (
            resolve(cast(CallbackQuery, query), expect=Action.STOP, store=store).target
            == SESSION
        )
        with pytest.raises(NonceError):
            resolve(cast(CallbackQuery, query), expect=Action.STOP, store=store)


class _Query:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = None
