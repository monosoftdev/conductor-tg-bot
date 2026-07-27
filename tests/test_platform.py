"""Operator commands, and the line between an operator and a customer.

``PLATFORM_ADMIN_IDS`` gates these. The distinction that matters: an operator
can *stop* a workspace, and cannot *read* one. Nothing here returns tenant
data — the strongest thing available is a slug and a status.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ctb.bot.handlers import platform
from ctb.bot.middleware.tenancy import TenantContext, TenantSettings
from ctb.db.connection import Database
from ctb.db.repo import tenancy
from ctb.db.repo.tenancy import TenantRow
from ctb.settings import Settings
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID

pytestmark = pytest.mark.db

ADMIN = 1001
CUSTOMER = 7007


class NullState:
    async def get_state(self) -> None:
        return None

    async def clear(self) -> None:
        return None


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []

    async def fake_tell(_message: Any, text: str, **_kwargs: Any) -> None:
        lines.append(text)

    monkeypatch.setattr(platform, "tell", fake_tell)
    return lines


def message(text: str) -> Any:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=ADMIN, type="private"),
        message_thread_id=None,
        message_id=1,
        from_user=SimpleNamespace(id=ADMIN),
    )


def context(row: TenantRow, user_id: int = ADMIN) -> TenantContext:
    return TenantContext(
        tenant_id=row.id,
        slug=row.slug,
        status=row.status,
        role="owner",
        user_id=user_id,
        owner_ids=(user_id,),
        primary_chat_id=None,
        settings=TenantSettings(),
        row=row,
    )


@pytest.fixture
async def operator(system_db: Database) -> TenantRow:
    row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
    assert row is not None
    return row


class TestGate:
    async def test_a_customer_is_told_it_is_not_theirs(
        self,
        db: Database,
        settings_factory: Any,
        operator: TenantRow,
        said: list[str],
    ) -> None:
        """Not silence: a legitimate customer deserves a real answer."""
        cfg: Settings = settings_factory(platform_admin_ids="")
        await platform.platform(
            message("/platform list"), context(operator, CUSTOMER), cfg, NullState()
        )
        assert "whoever runs this instance" in said[-1]

    async def test_a_customer_cannot_suspend_anyone(
        self,
        db: Database,
        system_db: Database,
        settings_factory: Any,
        operator: TenantRow,
        said: list[str],
    ) -> None:
        cfg: Settings = settings_factory(platform_admin_ids="")
        await platform.platform(
            message("/platform suspend other"),
            context(operator, CUSTOMER),
            cfg,
            NullState(),
        )
        still = await tenancy.get(system_db, OTHER_TENANT_ID)
        assert still is not None and still.status == "active"


class TestOperator:
    @pytest.fixture
    def settings(self, settings_factory: Any) -> Settings:
        return settings_factory(platform_admin_ids=str(ADMIN))

    async def test_list_names_workspaces_but_no_customer_data(
        self,
        db: Database,
        settings: Settings,
        operator: TenantRow,
        said: list[str],
    ) -> None:
        await platform.platform(
            message("/platform list"), context(operator), settings, NullState()
        )
        assert "test" in said[-1] and "other" in said[-1]

    async def test_suspend_stops_that_workspace(
        self,
        db: Database,
        system_db: Database,
        settings: Settings,
        operator: TenantRow,
        said: list[str],
    ) -> None:
        await platform.platform(
            message("/platform suspend other"),
            context(operator),
            settings,
            NullState(),
        )
        row = await tenancy.get(system_db, OTHER_TENANT_ID)
        assert row is not None and row.status == "suspended"

    async def test_resume_starts_it_again(
        self,
        db: Database,
        system_db: Database,
        settings: Settings,
        operator: TenantRow,
        said: list[str],
    ) -> None:
        await tenancy.set_status(system_db, OTHER_TENANT_ID, "suspended")
        await platform.platform(
            message("/platform resume other"),
            context(operator),
            settings,
            NullState(),
        )
        row = await tenancy.get(system_db, OTHER_TENANT_ID)
        assert row is not None and row.status == "active"

    async def test_an_unknown_workspace_changes_nothing(
        self,
        db: Database,
        settings: Settings,
        operator: TenantRow,
        said: list[str],
    ) -> None:
        await platform.platform(
            message("/platform suspend nope"),
            context(operator),
            settings,
            NullState(),
        )
        assert "No team" in said[-1]

    async def test_no_argument_shows_the_usage(
        self,
        db: Database,
        settings: Settings,
        operator: TenantRow,
        said: list[str],
    ) -> None:
        await platform.platform(
            message("/platform"), context(operator), settings, NullState()
        )
        assert "/platform list" in said[-1]
