"""Every owner-only command, checked against a plain member.

Written as one table rather than ten test functions because the risk here is
*omission*, not subtlety. An adversarial review deleted all ten `is_owner`
gates at once and eight of them killed no test — a `member` could have revoked
the workspace's Conductor key, run arbitrary SQL against the organisation's
transcripts, or dumped every member's Telegram id, and the suite stayed green.

The rule these encode: ``member`` may drive sessions. Everything that changes
who is in the workspace, what it costs, or what leaves it, is ``is_owner``
(which admins also pass — see :data:`ctb.db.repo.tenancy.ROLE_ORDER`).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from ctb.bot.handlers import admin, power, registration
from ctb.bot.middleware.tenancy import TenantContext, TenantSettings
from ctb.db.connection import Database
from ctb.db.repo import tenancy
from ctb.db.repo.tenancy import TenantRow
from tests.pg import BOOTSTRAP_TENANT_ID

pytestmark = pytest.mark.db

MEMBER = 5150
CHAT = -1_002_000_000_777


class NullState:
    async def get_state(self) -> None:
        return None

    async def clear(self) -> None:
        return None


def message(text: str, *, private: bool = False) -> Any:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(
            id=MEMBER if private else CHAT,
            type="private" if private else "supergroup",
            title=None,
        ),
        message_thread_id=None,
        message_id=1,
        from_user=SimpleNamespace(id=MEMBER),
        bot=SimpleNamespace(),
    )


def member_context(row: TenantRow) -> TenantContext:
    """A seated user with the lowest role. Not an owner, not a stranger."""
    return TenantContext(
        tenant_id=row.id,
        slug=row.slug,
        status=row.status,
        role="member",
        user_id=MEMBER,
        owner_ids=(1,),
        primary_chat_id=None,
        settings=TenantSettings(),
        row=row,
    )


@pytest.fixture
async def tenant_row(system_db: Database) -> TenantRow:
    row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
    assert row is not None
    return row


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []

    async def fake_tell(_message: Any, text: str, **_kwargs: Any) -> None:
        lines.append(text)

    for module in (admin, power, registration):
        monkeypatch.setattr(module, "tell", fake_tell, raising=False)
    return lines


#: ``(label, coroutine factory)``. Each entry is one command a member must not
#: be able to run. The *observable* assertion is shared: it answers with a
#: refusal, and nothing it would have changed was changed.
GATED: dict[str, Any] = {
    "invite": lambda t, db: admin.invite(message("/invite 99"), t, NullState()),
    "remove": lambda t, db: admin.remove(message("/remove 99"), t, NullState()),
    "members": lambda t, db: admin.members(message("/members"), t, NullState()),
    "health": lambda t, db: admin.health(message("/health"), t, NullState()),
    "export": lambda t, db: admin.export(message("/export"), t, NullState()),
    "revoke": lambda t, db: registration.revoke(
        message("/revoke", private=True), t, NullState()
    ),
    "tidy": lambda t, db: power.tidy(message("/tidy"), t, NullState(), db=db),
    "sql": lambda t, db: power.sql_command(
        message("/sql SELECT 1"), t, NullState(), is_owner=False
    ),
}


#: The two refusal strings the gates actually emit. Matched exactly rather than
#: by substring: `/members` ungated prints every member's *role*, which contains
#: "owner", and `/remove` ungated says "…or the last owner" — so a loose
#: `"owner" in said` passes with the gate deleted, which is precisely the
#: mistake this file exists to stop.
REFUSALS = frozenset({"Owners only.", "Owner only."})


@pytest.mark.parametrize("name", sorted(GATED))
async def test_a_member_is_refused(
    name: str,
    db: Database,
    system_db: Database,
    tenant_row: TenantRow,
    said: list[str],
) -> None:
    await GATED[name](member_context(tenant_row), db)

    assert said, f"/{name} answered a non-owner with silence"
    assert said[-1] in REFUSALS, f"/{name} did not refuse a member: {said[-1]!r}"


async def test_revoke_by_a_member_leaves_the_key_in_place(
    db: Database, system_db: Database, tenant_row: TenantRow, said: list[str]
) -> None:
    """The refusal has to *not act*, not merely say no."""
    before = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
    assert before is not None and before.has_conductor_key

    await registration.revoke(
        message("/revoke", private=True), member_context(tenant_row), NullState()
    )

    after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
    assert after is not None
    assert after.conductor_key_ct == before.conductor_key_ct
    assert after.status == before.status


async def test_invite_by_a_member_seats_nobody(
    db: Database, system_db: Database, tenant_row: TenantRow, said: list[str]
) -> None:
    await admin.invite(message("/invite 99"), member_context(tenant_row), NullState())

    assert await tenancy.member(system_db, BOOTSTRAP_TENANT_ID, 99) is None


class TestLastOwner:
    """A workspace with zero owners is unadministrable *and* undeletable.

    Every command that could fix it is gated on ``is_owner``, and ``/forget``
    needs the role exactly — so the row, its sealed key and its transcripts
    would persist with nobody able to touch them.
    """

    async def test_the_only_owner_cannot_demote_themselves(
        self, system_db: Database, tenant_row: TenantRow
    ) -> None:
        owners = await tenancy.list_owner_ids(system_db, BOOTSTRAP_TENANT_ID)
        assert len(owners) == 1
        sole = owners[0]

        with pytest.raises(tenancy.RoleError, match="only owner"):
            await tenancy.add_member(
                system_db,
                BOOTSTRAP_TENANT_ID,
                sole,
                role="member",
                added_by=sole,
            )

        assert await tenancy.list_owner_ids(system_db, BOOTSTRAP_TENANT_ID) == owners

    async def test_demotion_is_allowed_once_a_second_owner_exists(
        self, system_db: Database, tenant_row: TenantRow
    ) -> None:
        sole = (await tenancy.list_owner_ids(system_db, BOOTSTRAP_TENANT_ID))[0]
        await tenancy.add_member(
            system_db, BOOTSTRAP_TENANT_ID, 4242, role="owner", added_by=sole
        )

        await tenancy.add_member(
            system_db, BOOTSTRAP_TENANT_ID, sole, role="member", added_by=4242
        )

        assert await tenancy.list_owner_ids(system_db, BOOTSTRAP_TENANT_ID) == (4242,)

    async def test_an_admin_still_cannot_demote_an_owner(
        self, system_db: Database, tenant_row: TenantRow
    ) -> None:
        """Admins pass `is_owner`, so without this they could seize a workspace."""
        sole = (await tenancy.list_owner_ids(system_db, BOOTSTRAP_TENANT_ID))[0]
        await tenancy.add_member(
            system_db, BOOTSTRAP_TENANT_ID, 7777, role="admin", added_by=sole
        )

        with pytest.raises(tenancy.RoleError):
            await tenancy.add_member(
                system_db, BOOTSTRAP_TENANT_ID, sole, role="member", added_by=7777
            )


class TestUseRebinding:
    """`/use` picks which workspace a DM means. It has to work more than once."""

    async def test_a_dm_can_be_pointed_back_at_the_first_workspace(
        self, system_db: Database
    ) -> None:
        other = uuid.UUID("00000000-0000-4000-8000-000000000002")
        dm_chat = 90_001

        await tenancy.rebind_chat(system_db, dm_chat, BOOTSTRAP_TENANT_ID)
        await tenancy.rebind_chat(system_db, dm_chat, other)
        await tenancy.rebind_chat(system_db, dm_chat, BOOTSTRAP_TENANT_ID)

        binding = await tenancy.chat_for(system_db, dm_chat)
        assert binding is not None and binding.tenant_id == BOOTSTRAP_TENANT_ID

    async def test_a_group_is_never_re_homed_by_one_person(
        self, system_db: Database
    ) -> None:
        with pytest.raises(ValueError, match="private chats only"):
            await tenancy.rebind_chat(
                system_db, CHAT, BOOTSTRAP_TENANT_ID, kind="group"
            )
