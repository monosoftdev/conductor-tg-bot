"""Sign-up, chat binding, and key intake.

Three properties carry the security of the whole self-serve story, and each
has a test that fails loudly if it stops holding:

* A group is bound only by someone holding a **single-use code** issued in a
  private chat. A shared bot can be added to any group by anyone; being added
  is not consent.
* An API key **never stays in Telegram**. Sent to a group it is refused and the
  message is deleted; sent privately it is validated, sealed, stored, and the
  message that carried it is deleted.
* A stored key is **sealed**, and the plaintext never appears in the row.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ctb.bot.handlers import registration
from ctb.bot.middleware.tenancy import TenantContext, TenantSettings
from ctb.crypto import SecretBox
from ctb.db.connection import Database
from ctb.db.repo import tenancy
from ctb.db.repo.tenancy import TenantRow
from ctb.runtime import secret_box
from ctb.settings import Settings
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID

pytestmark = pytest.mark.db

OWNER = 4242
GROUP = -1_002_000_000_777


class FakeBot:
    """Records what the handler asked Telegram to do."""

    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []
        self.topics: list[int] = []

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True


class NullState:
    async def get_state(self) -> None:
        return None

    async def clear(self) -> None:
        return None


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture what the handler replied, instead of calling Telegram."""
    lines: list[str] = []

    async def fake_tell(_message: Any, text: str, **_kwargs: Any) -> None:
        lines.append(text)

    monkeypatch.setattr(registration, "tell", fake_tell)
    return lines


def dm(text: str, *, user_id: int = OWNER, bot: FakeBot | None = None) -> Any:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=user_id, type="private", title=None),
        message_thread_id=None,
        message_id=11,
        from_user=SimpleNamespace(id=user_id),
        bot=bot or FakeBot(),
    )


def group(text: str, *, bot: FakeBot | None = None) -> Any:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=GROUP, type="supergroup", title="Acme"),
        message_thread_id=None,
        message_id=12,
        from_user=SimpleNamespace(id=OWNER),
        bot=bot or FakeBot(),
    )


def context(row: TenantRow, *, role: str = "owner") -> TenantContext:
    return TenantContext(
        tenant_id=row.id,
        slug=row.slug,
        status=row.status,
        role=role,
        user_id=OWNER,
        owner_ids=(OWNER,),
        primary_chat_id=None,
        settings=TenantSettings(),
        row=row,
    )


class TestSlugs:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Acme Corp", "acme-corp"),
            ("  spaced  out  ", "spaced-out"),
            ("Ünïcödé!!", "n-c-d"),
            ("!!!", "workspace"),
            ("x" * 80, "x" * 24),
        ],
    )
    def test_a_name_becomes_a_boring_handle(self, name: str, expected: str) -> None:
        """Slugs are logged and shown; they must not carry arbitrary text."""
        assert registration.slugify(name) == expected


class TestRegister:
    async def test_it_creates_a_pending_workspace_and_seats_the_caller(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        await registration.register(dm("/register Acme"), settings, NullState())

        created = await tenancy.get_by_slug(system_db, "acme")
        assert created is not None
        assert created.status == "pending"  # no key yet, so not active
        member = await tenancy.member(system_db, created.id, OWNER)
        assert member is not None and member.role == "owner"
        assert "acme" in said[0]

    async def test_it_issues_a_code_that_is_stored_only_as_a_digest(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        await registration.register(dm("/register Acme"), settings, NullState())

        code = said[0].rsplit("/setup ", 1)[1].split("<", 1)[0].strip()
        stored = await system_db.fetch_val("SELECT token_hash FROM enrollment_tokens")
        assert stored == registration.hash_code(code)
        assert code not in str(stored)

    async def test_a_duplicate_name_gets_its_own_workspace(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """Two customers picking the same name must not be merged."""
        await registration.register(dm("/register Acme"), settings, NullState())
        await registration.register(
            dm("/register Acme", user_id=OWNER + 1), settings, NullState()
        )
        slugs = await system_db.fetch_all("SELECT slug FROM tenants ORDER BY slug")
        assert len([row for row in slugs if str(row["slug"]).startswith("acme")]) == 2

    async def test_it_is_refused_in_a_group(
        self, db: Database, settings: Settings, said: list[str]
    ) -> None:
        await registration.register(group("/register Acme"), settings, NullState())
        assert "private chat" in said[0]

    async def test_a_closed_instance_says_so(
        self,
        db: Database,
        system_db: Database,
        settings_factory: Any,
        said: list[str],
    ) -> None:
        await registration.register(
            dm("/register Acme"), settings_factory(registration_open=False), NullState()
        )
        assert await tenancy.get_by_slug(system_db, "acme") is None
        assert "closed" in said[0]

    async def test_an_existing_member_is_pointed_at_what_they_have(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        await registration.register(
            dm("/register Again"), settings, NullState(), tenant=context(row)
        )
        assert "already have" in said[0]


class TestBinding:
    async def _register(self, settings: Settings, said: list[str]) -> str:
        await registration.register(dm("/register Acme"), settings, NullState())
        return said[-1].rsplit("/setup ", 1)[1].split("<", 1)[0].strip()

    @pytest.fixture(autouse=True)
    def _forum_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The capability probe succeeds; its own behaviour is tested elsewhere."""

        async def ok(*_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(degraded=False, detail=None)

        async def topic(*_args: Any, **_kwargs: Any) -> int:
            return 99

        async def discard(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(registration, "forum_support", ok)
        monkeypatch.setattr(registration, "require_topic", topic)
        monkeypatch.setattr(registration, "discard_topic", discard)

    async def test_a_valid_code_binds_the_group(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        code = await self._register(settings, said)

        await registration.setup(group(f"/setup {code}"), NullState(), db=db)

        binding = await tenancy.chat_for(system_db, GROUP)
        assert binding is not None
        assert binding.is_primary is True
        assert binding.verified_at is not None
        assert "Ready" in said[-1]

    async def test_a_code_works_exactly_once(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """Otherwise a screenshot of the code is a standing invitation."""
        code = await self._register(settings, said)
        await registration.setup(group(f"/setup {code}"), NullState(), db=db)
        await tenancy.unbind_chat(system_db, GROUP)

        await registration.setup(group(f"/setup {code}"), NullState(), db=db)

        assert await tenancy.chat_for(system_db, GROUP) is None
        assert "not valid" in said[-1]

    async def test_an_unknown_code_binds_nothing(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        await registration.setup(group("/setup guessed-it"), NullState(), db=db)
        assert await tenancy.chat_for(system_db, GROUP) is None
        assert "not valid" in said[-1]

    async def test_a_group_with_no_code_is_told_how_to_start(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        await registration.setup(group("/setup"), NullState(), db=db)
        assert await tenancy.chat_for(system_db, GROUP) is None
        assert "/register" in said[-1]

    async def test_a_group_already_owned_cannot_be_stolen(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """The attack this whole dance exists to prevent."""
        code = await self._register(settings, said)
        await registration.setup(group(f"/setup {code}"), NullState(), db=db)
        first = await tenancy.chat_for(system_db, GROUP)
        assert first is not None

        # Somebody else registers and tries to point the same group at theirs.
        await registration.register(
            dm("/register Rival", user_id=OWNER + 5), settings, NullState()
        )
        rival_code = said[-1].rsplit("/setup ", 1)[1].split("<", 1)[0].strip()
        await registration.setup(group(f"/setup {rival_code}"), NullState(), db=db)

        still = await tenancy.chat_for(system_db, GROUP)
        assert still is not None and still.tenant_id == first.tenant_id

    async def test_rebinding_a_bound_group_is_owners_only(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        code = await self._register(settings, said)
        await registration.setup(group(f"/setup {code}"), NullState(), db=db)
        bound = await tenancy.chat_for(system_db, GROUP)
        assert bound is not None
        row = await tenancy.get(system_db, bound.tenant_id)
        assert row is not None

        await registration.setup(
            group("/setup"), NullState(), tenant=context(row, role="member"), db=db
        )
        assert said[-1] == "Owners only."

    async def test_a_blocked_capability_check_refuses_to_bind(
        self,
        db: Database,
        system_db: Database,
        settings: Settings,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A /setup that says Ready while /new fails is worse than a refusal."""

        async def blocked(*_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(degraded=True, detail="manage topics is off")

        monkeypatch.setattr(registration, "forum_support", blocked)
        code = await self._register(settings, said)

        await registration.setup(group(f"/setup {code}"), NullState(), db=db)

        assert await tenancy.chat_for(system_db, GROUP) is None
        assert "Setup blocked" in said[-1]


class TestKeyIntake:
    @pytest.fixture(autouse=True)
    def _key_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ok(_key: str, _url: str) -> None:
            return None

        monkeypatch.setattr(registration, "_check_conductor_key", ok)

    async def _tenant(self, system_db: Database) -> TenantRow:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        return row

    async def test_a_key_sent_to_a_group_is_refused_and_deleted(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        """It is already in that group's history; say so and clean up."""
        bot = FakeBot()
        row = await self._tenant(system_db)

        await registration.set_key(
            group("/key cndk_leaked_0001", bot=bot), context(row), NullState()
        )

        assert bot.deleted == [(GROUP, 12)]
        assert "rotate" in said[-1].casefold()

    async def test_a_key_sent_privately_is_sealed_stored_and_deleted(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        bot = FakeBot()
        row = await self._tenant(system_db)
        secret = "cndk_live_real_key_0001"

        await registration.set_key(
            dm(f"/key {secret}", bot=bot), context(row), NullState()
        )

        assert bot.deleted == [(OWNER, 11)]
        stored = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert stored is not None
        assert stored.conductor_key_ct is not None
        assert secret.encode() not in stored.conductor_key_ct
        assert stored.conductor_key_fp == secret_box().fingerprint_of(
            secret, tenant_id=BOOTSTRAP_TENANT_ID
        )
        assert stored.status == "active"

    async def test_the_fingerprint_is_keyed_not_a_bare_digest(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        """``conductor_key_fp`` is readable by the app role.

        An unsalted hash of an API key is offline-guessable for any key with
        predictable structure, so the handle is an HMAC under the master key and
        scoped to the tenant — the same key in two workspaces does not
        correlate.
        """
        secret = "cndk_live_real_key_0009"
        assert secret_box().fingerprint_of(
            secret, tenant_id=BOOTSTRAP_TENANT_ID
        ) != SecretBox.fingerprint(secret)
        assert secret_box().fingerprint_of(
            secret, tenant_id=BOOTSTRAP_TENANT_ID
        ) != secret_box().fingerprint_of(secret, tenant_id=OTHER_TENANT_ID)

    async def test_the_stored_key_round_trips(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await self._tenant(system_db)
        secret = "cndk_live_real_key_0002"

        await registration.set_key(dm(f"/key {secret}"), context(row), NullState())

        stored = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert stored is not None
        assert (
            secret_box().open(
                stored.conductor_key_ct,
                tenant_id=BOOTSTRAP_TENANT_ID,
                purpose="conductor_api_key",
            )
            == secret
        )

    async def test_a_rejected_key_is_not_stored(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A typo should be an answer now, not an auth failure in an hour."""

        async def rejected(_key: str, _url: str) -> str:
            return "401 Unauthorized"

        monkeypatch.setattr(registration, "_check_conductor_key", rejected)
        row = await self._tenant(system_db)
        before = row.conductor_key_fp

        await registration.set_key(dm("/key typo"), context(row), NullState())

        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.conductor_key_fp == before
        assert "rejected" in said[-1]

    async def test_resending_the_same_key_changes_nothing(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        secret = "cndk_live_real_key_0003"
        row = await self._tenant(system_db)
        await registration.set_key(dm(f"/key {secret}"), context(row), NullState())
        stored = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert stored is not None

        await registration.set_key(dm(f"/key {secret}"), context(stored), NullState())

        assert "already the stored key" in said[-1]

    async def test_only_owners_may_set_a_key(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await self._tenant(system_db)
        await registration.set_key(
            dm("/key whatever"), context(row, role="member"), NullState()
        )
        assert said[-1] == "Owners only."

    async def test_revoking_deletes_the_key_and_stops_polling(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await self._tenant(system_db)
        await registration.set_key(dm("/key cndk_x"), context(row), NullState())
        keyed = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert keyed is not None and keyed.has_conductor_key

        await registration.revoke(dm("/revoke"), context(keyed), NullState())

        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None
        assert after.conductor_key_ct is None
        assert after.status == "pending"  # list_bound stops polling it


class TestDisclosure:
    async def test_privacy_names_what_is_stored_and_what_leaves(
        self, db: Database, said: list[str]
    ) -> None:
        """Holding someone else's API key deserves a plain statement."""
        await registration.privacy(dm("/privacy"), NullState())
        text = said[0]
        assert "encrypted" in text
        assert "/revoke" in text
        assert "30 days" in text
