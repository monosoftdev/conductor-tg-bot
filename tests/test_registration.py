"""Sign-up, chat binding, and key intake.

The shortest path to working is two private messages — ``/start`` then
``/key`` — and :class:`TestFirstRun` is the test of exactly that. A group is
optional, reached through ``/team``, and :class:`TestTeam` proves it still
works end to end.

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
from ctb.bot.keyboards import Action, NonceStore
from ctb.bot.middleware.tenancy import TenantContext, TenantSettings
from ctb.crypto import SecretBox
from ctb.db.connection import Database, tenant_scope
from ctb.db.repo import sessions as sessions_repo
from ctb.db.repo import tenancy
from ctb.db.repo import workspaces as workspaces_repo
from ctb.db.repo.tenancy import TenantRow
from ctb.runtime import secret_box, system_database
from ctb.settings import Settings, load_settings
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


def voice_settings(*, enabled: bool = True) -> Settings:
    """Platform settings for the key handler, which decides voice on storage."""
    return load_settings(
        telegram_bot_token="123456:AA",
        database_url="postgresql://x/y",
        system_database_url="postgresql://x/y",
        master_keys="v1:" + "A" * 43,
        voice_enabled=enabled,
    )


async def issue_code_for(
    system_db: Database,
    settings: Settings,
    said: list[str],
    *,
    slug: str,
    user_id: int = OWNER,
) -> str:
    """Register, store a key, and return the binding code.

    The code comes from ``/team``, the optional group flow — nothing earlier
    mints one, so its 15-minute clock starts when the owner is ready to make a
    group rather than while they are still hunting for an API key.
    """
    await registration.register(
        dm(f"/register {slug}", user_id=user_id), settings, NullState()
    )
    created = await tenancy.get_by_slug(system_db, slug)
    assert created is not None
    await tenancy.set_conductor_key(
        system_db, created.id, ciphertext=b"sealed", kid="v1", fingerprint=f"fp-{slug}"
    )
    keyed = await tenancy.get(system_db, created.id)
    assert keyed is not None
    await registration.team(
        dm("/team", user_id=user_id),
        context(keyed, user_id=user_id),
        NullState(),
    )
    return said[-1].rsplit("/setup ", 1)[1].split("<", 1)[0].strip()


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


def dm(
    text: str,
    *,
    user_id: int = OWNER,
    bot: FakeBot | None = None,
    username: str | None = None,
    first_name: str | None = None,
) -> Any:
    """A private message. The account fields are what an implicit team is named
    after, so they are part of the fixture rather than an afterthought."""
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=user_id, type="private", title=None),
        message_thread_id=None,
        message_id=11,
        from_user=SimpleNamespace(
            id=user_id,
            username=username,
            first_name=f"User{user_id}" if first_name is None else first_name,
            last_name=None,
        ),
        bot=bot or FakeBot(),
    )


def group(text: str, *, bot: FakeBot | None = None, user_id: int = OWNER) -> Any:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=GROUP, type="supergroup", title="Acme"),
        message_thread_id=None,
        message_id=12,
        from_user=SimpleNamespace(
            id=user_id, username=None, first_name=f"User{user_id}", last_name=None
        ),
        bot=bot or FakeBot(),
    )


def context(
    row: TenantRow, *, role: str = "owner", user_id: int = OWNER
) -> TenantContext:
    return TenantContext(
        tenant_id=row.id,
        slug=row.slug,
        status=row.status,
        role=role,
        user_id=user_id,
        owner_ids=(user_id,),
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
        code = await issue_code_for(system_db, settings, said, slug="acme")
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

    async def test_an_owner_who_runs_it_again_gets_one_team_and_the_next_step(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """Naming a team is optional and re-runnable; it never forks one."""
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        await registration.register(
            dm("/register Again"), settings, NullState(), tenant=context(row)
        )
        assert await tenancy.get_by_slug(system_db, "again") is None
        assert "ready" in said[-1].casefold()
        assert "supergroup" not in said[-1], "a group is not a step any more"

    async def test_a_bare_register_takes_the_default_name(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """A usage line here would be one more round trip before the key."""
        await registration.register(
            dm("/register", username="Chef"), settings, NullState()
        )
        assert await tenancy.get_by_slug(system_db, "chef") is not None

    async def test_a_mere_member_is_told_to_leave_first(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """Being in someone else's workspace is not owning one."""
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        await registration.register(
            dm("/register Mine"),
            settings,
            NullState(),
            tenant=context(row, role="member"),
        )
        assert "/leave" in said[-1]


class TestFirstRun:
    """DM the bot, paste a key, go. No team name, no group, no `/register`."""

    @pytest.fixture(autouse=True)
    def _key_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ok(_key: str, _url: str) -> None:
            return None

        monkeypatch.setattr(registration, "_check_conductor_key", ok)

    async def _only_seat(self, system_db: Database) -> TenantRow:
        seats = await tenancy.memberships_for_user(system_db, OWNER)
        assert len(seats) == 1, "one /start, one team"
        row = await tenancy.get(system_db, seats[0].tenant_id)
        assert row is not None
        return row

    async def test_start_alone_creates_the_team_and_asks_only_for_a_key(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        await registration.start(dm("/start"), settings, NullState())

        row = await self._only_seat(system_db)
        assert row.status == "pending"  # no key yet
        assert "/key" in said[-1]
        assert "/register" not in said[-1], "no team name is asked for"
        assert "supergroup" not in said[-1], "no group is asked for"

    async def test_start_twice_makes_one_team(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """Tapping the button twice is the most ordinary thing a user does.

        Both calls run *unresolved*, exactly as the middleware delivers them
        before any chat is bound, so the guard under test is the membership
        lookup and not a cached tenant.
        """
        await registration.start(dm("/start"), settings, NullState())
        await registration.start(dm("/start"), settings, NullState())

        await self._only_seat(system_db)
        assert "/key" in said[-1], "the second one still says what to do"

    async def test_the_key_finishes_it_and_points_at_new(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """`/start` + `/key` is the whole sign-up."""
        await registration.start(dm("/start"), settings, NullState())
        row = await self._only_seat(system_db)

        await registration.set_key(
            dm("/key cndk_live_first_run_0001"),
            context(row),
            voice_settings(),
            NullState(),
        )

        after = await tenancy.get(system_db, row.id)
        assert after is not None and after.status == "active"
        assert "ready" in said[-1].casefold()
        assert "/new" in said[-1]
        assert "supergroup" not in said[-1], "the group must not read as step 2"
        assert "optional" in said[-1], "and where it is mentioned, it is optional"

    async def test_the_private_chat_is_bound_to_the_new_team(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """Otherwise a second team makes the first one's DM ambiguous, and
        `/use` — the fix — needs a resolved tenant itself."""
        await registration.start(dm("/start"), settings, NullState())

        row = await self._only_seat(system_db)
        binding = await tenancy.chat_for(system_db, OWNER)
        assert binding is not None
        assert binding.tenant_id == row.id
        assert binding.kind == "dm"

    async def test_the_team_is_named_after_the_account(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        await registration.start(dm("/start", username="Chef"), settings, NullState())
        assert await tenancy.get_by_slug(system_db, "chef") is not None

    async def test_a_name_that_slugifies_to_nothing_still_gets_a_handle(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """Slugs are logged and shown, so they cannot be empty or shared."""
        await registration.start(
            dm("/start", first_name="Борис"), settings, NullState()
        )
        assert await tenancy.get_by_slug(system_db, f"team-{OWNER}") is not None

    async def test_two_accounts_with_the_same_name_stay_separate(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        await registration.start(dm("/start", username="Chef"), settings, NullState())
        await registration.start(
            dm("/start", user_id=OWNER + 1, username="Chef"), settings, NullState()
        )
        rows = await system_db.fetch_all(
            "SELECT slug FROM tenants WHERE slug LIKE 'chef%'"
        )
        assert len(rows) == 2

    async def test_a_closed_instance_creates_nothing(
        self,
        db: Database,
        system_db: Database,
        settings_factory: Any,
        said: list[str],
    ) -> None:
        """`/start` is the sign-up path now, so it is also the gate."""
        await registration.start(
            dm("/start"), settings_factory(registration_open=False), NullState()
        )

        assert await tenancy.memberships_for_user(system_db, OWNER) == []
        assert "closed" in said[-1]

    async def test_the_rate_limit_applies_to_the_implicit_path(
        self,
        db: Database,
        system_db: Database,
        settings_factory: Any,
        said: list[str],
    ) -> None:
        """An unauthenticated INSERT per /start, and nothing prunes it."""
        await registration.start(
            dm("/start"),
            settings_factory(registration_rate_per_hour=1),
            NullState(),
        )

        assert await tenancy.memberships_for_user(system_db, OWNER) == []
        assert "busy" in said[-1]

    async def test_a_group_is_never_the_entry_point(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """Sign-up ends in a key, and a key in a group is a key to rotate."""
        await registration.start(group("/start"), settings, NullState())

        assert await tenancy.memberships_for_user(system_db, OWNER) == []
        assert "private chat" in said[-1]

    async def test_start_after_the_key_says_ready(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None

        await registration.start(
            dm("/start"), settings, NullState(), tenant=context(row)
        )

        assert "ready" in said[-1].casefold()
        assert "/new" in said[-1]

    async def test_a_member_of_a_keyless_team_is_told_who_can_fix_it(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """A member cannot store the key, so `/key` is not their instruction."""
        await tenancy.set_conductor_key(
            system_db, BOOTSTRAP_TENANT_ID, ciphertext=None, kid=None, fingerprint=None
        )
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None

        await registration.start(
            dm("/start"), settings, NullState(), tenant=context(row, role="member")
        )

        assert "owner" in said[-1]


class TestTeam:
    """The group flow, now optional and reached on purpose."""

    @pytest.fixture(autouse=True)
    def _forum_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ok(*_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(degraded=False, detail=None)

        async def topic(*_args: Any, **_kwargs: Any) -> int:
            return 99

        async def discard(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def key_ok(_key: str, _url: str) -> None:
            return None

        monkeypatch.setattr(registration, "forum_support", ok)
        monkeypatch.setattr(registration, "require_topic", topic)
        monkeypatch.setattr(registration, "discard_topic", discard)
        monkeypatch.setattr(registration, "_check_conductor_key", key_ok)

    async def test_it_explains_what_a_group_adds_and_issues_a_code(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None

        await registration.team(dm("/team"), context(row), NullState())

        assert "optional" in said[-1].casefold()
        assert "/setup " in said[-1]
        code = said[-1].rsplit("/setup ", 1)[1].split("<", 1)[0].strip()
        redeemed = await tenancy.consume_enrollment_token(
            system_db, token_hash=registration.hash_code(code), purpose="bind_chat"
        )
        assert redeemed is not None and redeemed[0] == BOOTSTRAP_TENANT_ID

    async def test_the_whole_group_flow_still_works(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """`/start` → `/key` → `/team` → `/setup` in the supergroup."""
        await registration.start(dm("/start"), settings, NullState())
        seats = await tenancy.memberships_for_user(system_db, OWNER)
        assert len(seats) == 1
        row = await tenancy.get(system_db, seats[0].tenant_id)
        assert row is not None
        await registration.set_key(
            dm("/key cndk_live_group_flow"), context(row), voice_settings(), NullState()
        )
        keyed = await tenancy.get(system_db, row.id)
        assert keyed is not None

        await registration.team(dm("/team"), context(keyed), NullState())
        code = said[-1].rsplit("/setup ", 1)[1].split("<", 1)[0].strip()
        await registration.setup(group(f"/setup {code}"), NullState(), db=db)

        binding = await tenancy.chat_for(system_db, GROUP)
        assert binding is not None and binding.tenant_id == row.id
        assert binding.is_primary is True, "the group takes over owner notices"
        assert "Ready" in said[-1]

    async def test_only_owners_mint_one(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None

        await registration.team(dm("/team"), context(row, role="member"), NullState())

        assert said[-1] == "Owners only."
        assert await system_db.fetch_val("SELECT COUNT(*) FROM enrollment_tokens") == 0

    async def test_it_is_refused_in_a_group(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        """The code would be a bearer token in front of everyone who can read."""
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None

        await registration.team(group("/team"), context(row), NullState())

        assert "private chat" in said[-1]
        assert await system_db.fetch_val("SELECT COUNT(*) FROM enrollment_tokens") == 0


class TestBinding:
    async def _register(
        self, settings: Settings, said: list[str], system_db: Database | None = None
    ) -> str:
        """Walk the private half and return the binding code.

        `/team` is the only thing that mints one — the group is optional, so
        nothing before it mentions a supergroup at all.
        """
        await registration.register(dm("/register Acme"), settings, NullState())
        system = system_db if system_db is not None else system_database()
        created = await tenancy.get_by_slug(system, "acme")
        assert created is not None
        await tenancy.set_conductor_key(
            system,
            created.id,
            ciphertext=b"sealed",
            kid="v1",
            fingerprint="fp-acme",
        )
        # Re-fetch: the context carries the row, and the row is what decides
        # which step you are on.
        keyed = await tenancy.get(system, created.id)
        assert keyed is not None
        await registration.team(dm("/team"), context(keyed), NullState())
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
        assert "/team" in said[-1]

    async def test_a_group_already_owned_cannot_be_stolen(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """The attack this whole dance exists to prevent."""
        code = await self._register(settings, said)
        await registration.setup(group(f"/setup {code}"), NullState(), db=db)
        first = await tenancy.chat_for(system_db, GROUP)
        assert first is not None

        # Somebody else registers and tries to point the same group at theirs.
        rival_code = await issue_code_for(
            system_db, settings, said, slug="rival", user_id=OWNER + 5
        )
        await registration.setup(group(f"/setup {rival_code}"), NullState(), db=db)

        still = await tenancy.chat_for(system_db, GROUP)
        assert still is not None and still.tenant_id == first.tenant_id

    async def test_a_code_only_works_for_the_person_it_was_issued_to(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """A code is otherwise a bearer token for somebody else's workspace.

        Anyone who sees one — a screenshot, a forward, a paste into the wrong
        chat — could bind a group *they* control to the victim's workspace,
        which then becomes its primary chat and receives its owner notices.
        """
        code = await self._register(settings, said)

        await registration.setup(
            group(f"/setup {code}", user_id=OWNER + 99), NullState(), db=db
        )

        assert await tenancy.chat_for(system_db, GROUP) is None
        assert "issued to someone else" in said[-1]

    async def test_the_routing_row_belongs_to_the_bound_tenant(
        self, db: Database, system_db: Database, settings: Settings, said: list[str]
    ) -> None:
        """`/setup` runs with no tenant in scope, so it must set one itself.

        Without the explicit `tenant_scope`, the `chats` row lands under
        whatever the ambient scope happens to be — nothing raises, nothing
        fails, and the group routes to the wrong workspace.
        """
        code = await self._register(settings, said)
        await registration.setup(group(f"/setup {code}"), NullState(), db=db)

        binding = await tenancy.chat_for(system_db, GROUP)
        assert binding is not None
        owner = await system_db.fetch_val(
            "SELECT tenant_id FROM chats WHERE chat_id = ? AND thread_id = 0",
            (GROUP,),
        )
        assert owner == binding.tenant_id

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
            group("/key cndk_leaked_0001", bot=bot),
            context(row),
            voice_settings(),
            NullState(),
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
            dm(f"/key {secret}", bot=bot), context(row), voice_settings(), NullState()
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

        await registration.set_key(
            dm(f"/key {secret}"), context(row), voice_settings(), NullState()
        )

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

        await registration.set_key(
            dm("/key typo"), context(row), voice_settings(), NullState()
        )

        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.conductor_key_fp == before
        assert "rejected" in said[-1]

    async def test_resending_the_same_key_changes_nothing(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        secret = "cndk_live_real_key_0003"
        row = await self._tenant(system_db)
        await registration.set_key(
            dm(f"/key {secret}"), context(row), voice_settings(), NullState()
        )
        stored = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert stored is not None

        await registration.set_key(
            dm(f"/key {secret}"), context(stored), voice_settings(), NullState()
        )

        assert "already the stored key" in said[-1]

    async def test_a_key_sent_before_start_is_still_deleted(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        """The message carries a live credential whether or not we know who
        sent it. Deletion first; the instruction after."""
        bot = FakeBot()

        await registration.set_key(
            dm("/key cndk_live_too_early", bot=bot),
            None,
            voice_settings(),
            NullState(),
        )

        assert bot.deleted == [(OWNER, 11)]
        assert "/start" in said[-1]

    async def test_only_owners_may_set_a_key(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await self._tenant(system_db)
        await registration.set_key(
            dm("/key whatever"),
            context(row, role="member"),
            voice_settings(),
            NullState(),
        )
        assert "owners can store its key" in said[-1]

    async def test_revoking_deletes_the_key_and_stops_polling(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await self._tenant(system_db)
        await registration.set_key(
            dm("/key cndk_x"), context(row), voice_settings(), NullState()
        )
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


class TestUse:
    """A DM has to belong to exactly one workspace, and you say which."""

    async def test_it_binds_this_chat_to_a_named_workspace(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        other = await tenancy.create(
            system_db, slug="second", name="Second", owner_user_id=OWNER
        )
        row = await self._tenant(system_db)

        await registration.use(dm("/use second"), context(row), NullState())

        binding = await tenancy.chat_for(system_db, OWNER)
        assert binding is not None and binding.tenant_id == other.id

    async def test_it_refuses_a_workspace_you_are_not_in(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        """Otherwise it is a way to point your DM at somebody else's."""
        await tenancy.create(
            system_db, slug="theirs", name="Theirs", owner_user_id=OWNER + 99
        )
        row = await self._tenant(system_db)

        await registration.use(dm("/use theirs"), context(row), NullState())

        assert await tenancy.chat_for(system_db, OWNER) is None
        assert "not in a team" in said[-1]

    async def test_with_no_argument_it_lists_what_you_are_in(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await self._tenant(system_db)
        await registration.use(dm("/use"), context(row), NullState())
        assert "test" in said[-1]

    async def _tenant(self, system_db: Database) -> TenantRow:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        await tenancy.add_member(system_db, BOOTSTRAP_TENANT_ID, OWNER, role="owner")
        return row


class TestForget:
    """``/privacy`` promises deletion, so deletion has to exist and work."""

    async def _owned(self, system_db: Database) -> TenantRow:
        await tenancy.add_member(system_db, BOOTSTRAP_TENANT_ID, OWNER, role="owner")
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        return row

    async def test_it_takes_two_taps(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await self._owned(system_db)
        await registration.forget(
            dm("/forget"), context(row), NullState(), NonceStore()
        )

        assert await tenancy.get(system_db, BOOTSTRAP_TENANT_ID) is not None
        assert "cannot be undone" in said[-1]

    async def test_the_second_tap_deletes_everything(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        row = await self._owned(system_db)
        async with tenant_scope(BOOTSTRAP_TENANT_ID):
            await workspaces_repo.upsert(db, "ws-gone", name="gone")
            await sessions_repo.upsert(db, "sess-gone", workspace_id="ws-gone")

        store = NonceStore()
        ticket = store.issue(Action.FORGET, str(BOOTSTRAP_TENANT_ID), user_id=OWNER)
        await registration.confirm_forget(
            _Tap(ticket.callback_data), context(row), store
        )

        assert await tenancy.get(system_db, BOOTSTRAP_TENANT_ID) is None
        assert await system_db.fetch_val("SELECT COUNT(*) FROM sessions") == 0

    async def test_a_payload_naming_another_workspace_is_refused(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        """The guard that stops a leaked payload deleting the wrong customer."""
        row = await self._owned(system_db)
        store = NonceStore()
        ticket = store.issue(Action.FORGET, str(OTHER_TENANT_ID), user_id=OWNER)

        tap = _Tap(ticket.callback_data)
        await registration.confirm_forget(tap, context(row), store)

        assert await tenancy.get(system_db, OTHER_TENANT_ID) is not None
        assert tap.answers == ["Not yours to delete."]

    async def test_only_an_owner_can_start_it(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        """Admins pass the ordinary owner gate; deletion needs more than that."""
        row = await self._owned(system_db)
        await registration.forget(
            dm("/forget"), context(row, role="admin"), NullState(), NonceStore()
        )
        assert "Only an owner" in said[-1]


class _Tap:
    """A CallbackQuery stand-in that records what it was answered with."""

    def __init__(self, data: str, user_id: int = OWNER) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_kwargs: Any) -> None:
        self.answers.append(text)


class TestVoiceToggle:
    """``tenants.voice_enabled`` gated the feature and nothing ever set it."""

    async def _tenant(self, system_db: Database) -> TenantRow:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        return row

    async def test_a_workspace_with_a_speech_key_can_turn_voice_on(
        self, db: Database, system_db: Database, settings_factory: Any, said: list[str]
    ) -> None:
        await tenancy.set_elevenlabs_key(
            system_db,
            BOOTSTRAP_TENANT_ID,
            ciphertext=b"sealed",
            kid="v1",
            fingerprint="fp",
        )
        row = await self._tenant(system_db)

        await registration.voice(
            dm("/voice on"),
            context(row),
            settings_factory(voice_enabled=True),
            NullState(),
        )

        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.voice_enabled is True

    async def test_without_a_speech_key_it_says_so_and_stays_off(
        self, db: Database, system_db: Database, settings_factory: Any, said: list[str]
    ) -> None:
        """There is deliberately no shared key to fall back to."""
        row = await self._tenant(system_db)

        await registration.voice(
            dm("/voice on"),
            context(row),
            settings_factory(voice_enabled=True),
            NullState(),
        )

        assert "/voicekey" in said[-1]
        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.voice_enabled is False

    async def test_the_platform_kill_switch_wins(
        self, db: Database, system_db: Database, settings_factory: Any, said: list[str]
    ) -> None:
        await tenancy.set_elevenlabs_key(
            system_db,
            BOOTSTRAP_TENANT_ID,
            ciphertext=b"sealed",
            kid="v1",
            fingerprint="fp",
        )
        row = await self._tenant(system_db)

        await registration.voice(
            dm("/voice on"),
            context(row),
            settings_factory(voice_enabled=False),
            NullState(),
        )

        assert "whole instance" in said[-1]
        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.voice_enabled is False

    async def test_a_member_cannot_toggle_it(
        self, db: Database, system_db: Database, settings_factory: Any, said: list[str]
    ) -> None:
        row = await self._tenant(system_db)

        await registration.voice(
            dm("/voice on"),
            context(row, role="member"),
            settings_factory(voice_enabled=True),
            NullState(),
        )

        assert said[-1] == "Owners only."


class TestGitHubTokenIntake:
    """`/gitkey` is what turns CI watching on for a team."""

    async def _tenant(self, system_db: Database) -> TenantRow:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        return row

    async def test_a_good_token_is_sealed_and_stored(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def accept(_token: str, **_kw: Any) -> None:
            return None

        monkeypatch.setattr(registration, "check_github_token", accept)
        row = await self._tenant(system_db)

        await registration.set_key(
            dm("/gitkey ghp_good"), context(row), voice_settings(), NullState()
        )

        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.github_key_ct is not None
        # Sealed, not stored: the row never holds the token itself.
        assert b"ghp_good" not in bytes(after.github_key_ct)
        assert "GitHub token stored" in said[-1]
        # The other two credentials are untouched by the third.
        assert after.conductor_key_ct is not None
        assert after.elevenlabs_key_ct is None

    async def test_a_rejected_token_is_not_stored_but_is_still_deleted(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def refuse(_token: str, **_kw: Any) -> str:
            return "GitHub rejected that token."

        monkeypatch.setattr(registration, "check_github_token", refuse)
        bot = FakeBot()
        row = await self._tenant(system_db)

        await registration.set_key(
            dm("/gitkey ghp_typo", bot=bot),
            context(row),
            voice_settings(),
            NullState(),
        )

        assert "GitHub rejected" in said[-1]
        assert bot.deleted, "a refused token is still a live token in history"
        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.github_key_ct is None

    async def test_a_token_sent_to_a_group_is_refused_and_deleted(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
    ) -> None:
        bot = FakeBot()
        message = SimpleNamespace(
            text="/gitkey ghp_oops",
            chat=SimpleNamespace(id=-100999, type="supergroup", title="Team"),
            message_thread_id=None,
            message_id=11,
            from_user=SimpleNamespace(
                id=OWNER, username=None, first_name="U", last_name=None
            ),
            bot=bot,
        )
        row = await self._tenant(system_db)

        await registration.set_key(message, context(row), voice_settings(), NullState())

        assert "Never send an API key to a group" in said[-1]
        assert bot.deleted
        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.github_key_ct is None

    async def test_only_owners_may_store_it(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
    ) -> None:
        row = await self._tenant(system_db)

        await registration.set_key(
            dm("/gitkey ghp_good"),
            context(row, role="member"),
            voice_settings(),
            NullState(),
        )

        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.github_key_ct is None

    async def test_revoke_takes_the_github_token_too(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def accept(_token: str, **_kw: Any) -> None:
            return None

        monkeypatch.setattr(registration, "check_github_token", accept)
        row = await self._tenant(system_db)
        await registration.set_key(
            dm("/gitkey ghp_good"), context(row), voice_settings(), NullState()
        )

        await registration.revoke(dm("/revoke"), context(row), NullState())

        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None
        assert after.github_key_ct is None and after.github_key_fp is None


class TestSpeechKeyIntake:
    """`/voicekey` used to accept anything and fail at the first voice note."""

    async def _tenant(self, system_db: Database) -> TenantRow:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        return row

    async def test_a_rejected_key_is_not_stored(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def refuse(_key: str, **_kw: Any) -> str:
            return "invalid_api_key"

        monkeypatch.setattr(registration, "check_elevenlabs_key", refuse)
        row = await self._tenant(system_db)

        await registration.set_key(
            dm("/voicekey sk_typo"), context(row), voice_settings(), NullState()
        )

        assert "ElevenLabs rejected" in said[-1]
        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.elevenlabs_key_ct is None

    async def test_a_good_key_is_stored_and_points_at_the_next_step(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def accept(_key: str, **_kw: Any) -> None:
            return None

        monkeypatch.setattr(registration, "check_elevenlabs_key", accept)
        row = await self._tenant(system_db)

        await registration.set_key(
            dm("/voicekey sk_good"), context(row), voice_settings(), NullState()
        )

        assert "voice is on" in said[-1].casefold(), "storing a key enables it"
        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.elevenlabs_key_ct is not None

    async def test_the_message_carrying_it_is_still_deleted(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Validation must not come before getting the key out of Telegram."""

        async def refuse(_key: str, **_kw: Any) -> str:
            return "invalid_api_key"

        monkeypatch.setattr(registration, "check_elevenlabs_key", refuse)
        bot = FakeBot()
        row = await self._tenant(system_db)

        await registration.set_key(
            dm("/voicekey sk_typo", bot=bot),
            context(row),
            voice_settings(),
            NullState(),
        )

        assert bot.deleted, "a refused key must still be deleted"


class TestAKeyOnTheWrongCommand:
    """The live failure: `/voice sk_...` printed a status line and moved on.

    The key was left sitting in Telegram history in plaintext, and the reply
    said nothing was wrong. A credential is a credential whatever command
    carried it.
    """

    async def _tenant(self, system_db: Database) -> TenantRow:
        row = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert row is not None
        return row

    async def test_it_is_deleted_and_stored_rather_than_ignored(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def accept(_key: str, **_kw: Any) -> None:
            return None

        monkeypatch.setattr(registration, "check_elevenlabs_key", accept)
        bot = FakeBot()
        row = await self._tenant(system_db)
        secret = "sk_9aa0285a88a625df7d3fecb4ad46911151724b75899bb51b"

        await registration.voice(
            dm(f"/voice {secret}", bot=bot),
            context(row),
            voice_settings(),
            NullState(),
        )

        assert bot.deleted, "a key on the wrong command is still a key"
        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.elevenlabs_key_ct is not None
        assert after.voice_enabled is True, "storing a key turns it on"

    async def test_a_refused_key_is_still_deleted(
        self,
        db: Database,
        system_db: Database,
        said: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def refuse(_key: str, **_kw: Any) -> str:
            return "invalid_api_key"

        monkeypatch.setattr(registration, "check_elevenlabs_key", refuse)
        bot = FakeBot()
        row = await self._tenant(system_db)

        await registration.voice(
            dm(f"/voice {'x' * 40}", bot=bot),
            context(row),
            voice_settings(),
            NullState(),
        )

        assert bot.deleted, "deletion must not wait on validation"
        after = await tenancy.get(system_db, BOOTSTRAP_TENANT_ID)
        assert after is not None and after.elevenlabs_key_ct is None

    async def test_on_and_off_are_not_mistaken_for_keys(
        self, db: Database, system_db: Database, said: list[str]
    ) -> None:
        bot = FakeBot()
        row = await self._tenant(system_db)

        await registration.voice(
            dm("/voice off", bot=bot), context(row), voice_settings(), NullState()
        )

        assert not bot.deleted
        assert "off" in said[-1].casefold()

    @pytest.mark.parametrize(
        "text",
        ["on", "off", "", "please turn it on", "yes"],
        ids=lambda t: t or "empty",
    )
    def test_ordinary_words_are_not_secrets(self, text: str) -> None:
        assert not registration.looks_like_secret(text)

    @pytest.mark.parametrize(
        "text",
        [
            "sk_9aa0285a88a625df7d3fecb4ad46911151724b75899bb51b",
            "cnd_live_abcdefghijklmnopqrstuvwxyz012345",
        ],
    )
    def test_real_keys_are_secrets(self, text: str) -> None:
        assert registration.looks_like_secret(text)
