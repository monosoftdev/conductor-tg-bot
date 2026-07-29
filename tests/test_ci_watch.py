"""The CI watch: the ledger, the poll loop, and the button it posts.

The repository half runs on the **app** pool, so row-level security is
exercised for free; the claim half runs on the worker pool, which is where the
watcher lives and where a missing ``tenant_id`` predicate would be a
cross-tenant write.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ctb.bot.keyboards import Action, NonceStore, parse
from ctb.ci.notice import ci_keyboard, ci_text
from ctb.ci.watcher import CiWatcher
from ctb.db.connection import Database, now_ms
from ctb.db.repo import ci as ci_repo
from ctb.db.repo import tenancy as tenancy_repo
from ctb.github.client import ChecksResult, CheckState, GitHubError, PullRequest
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID, as_tenant

pytestmark = pytest.mark.db

CHAT = -100777
THREAD = 12
SESSION = "sess-ci"


async def seed(
    db: Database,
    *,
    owner: str = "acme",
    repo: str = "api",
    number: int = 9,
    session_id: str = SESSION,
    at: int | None = None,
    ttl_ms: int = ci_repo.GIVE_UP_AFTER_MS,
) -> ci_repo.CiWatchRow:
    row = await ci_repo.watch(
        db,
        owner=owner,
        repo=repo,
        pr_number=number,
        session_id=session_id,
        chat_id=CHAT,
        thread_id=THREAD,
        at=at,
        ttl_ms=ttl_ms,
    )
    assert row is not None
    return row


# ── the ledger ───────────────────────────────────────────────────────────────


class TestTheLedger:
    async def test_a_watch_starts_due_immediately(self, db: Database) -> None:
        row = await seed(db, at=1_000)
        assert row.state == "watching"
        assert row.next_poll_at == 1_000
        assert row.expires_at == 1_000 + ci_repo.GIVE_UP_AFTER_MS

    async def test_the_same_pull_request_re_arms_rather_than_duplicating(
        self, db: Database
    ) -> None:
        first = await seed(db, at=1_000)
        await ci_repo.finish(
            db, first, head_sha="aaa", status="failure", notified=True, at=2_000
        )
        again = await seed(db, session_id="sess-second", at=3_000)

        assert again.state == "watching"
        assert again.session_id == "sess-second"
        # The verdict already announced is kept: it is a *new commit* going red
        # that is worth saying again, not the turn ending.
        assert again.notified_sha == "aaa"
        assert len(await ci_repo.list_for_session(db, "sess-second")) == 1

    async def test_a_claim_is_its_own_lease(self, db: Database) -> None:
        await seed(db, at=1_000)
        first = await ci_repo.claim_due(db, at=1_000)
        second = await ci_repo.claim_due(db, at=1_000)

        assert [row.slug for row in first] == ["acme/api#9"]
        assert second == (), "a claimed watch is not due again until its interval"

        later = await ci_repo.claim_due(db, at=1_000 + ci_repo.POLL_INTERVAL_MS)
        assert [row.slug for row in later] == ["acme/api#9"]

    async def test_a_finished_watch_is_never_claimed_again(self, db: Database) -> None:
        row = await seed(db, at=1_000)
        await ci_repo.finish(
            db, row, head_sha="aaa", status="success", notified=True, at=1_100
        )
        assert await ci_repo.claim_due(db, at=9_000_000) == ()

    async def test_a_fatal_failure_gives_up_at_once(self, db: Database) -> None:
        row = await seed(db, at=1_000)
        await ci_repo.fail(db, row, error="404", fatal=True, at=1_100)
        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None and after.state == "gave_up"

    async def test_a_flaky_failure_gives_up_only_after_the_cap(
        self, db: Database
    ) -> None:
        row = await seed(db, at=1_000)
        for index in range(ci_repo.MAX_ERRORS):
            current = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
            assert current is not None
            assert current.state == "watching", "still trying, one blip at a time"
            await ci_repo.fail(db, current, error="502", at=1_000 + index)
        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None and after.state == "gave_up"
        assert after.attempts == ci_repo.MAX_ERRORS
        assert row.attempts == 0

    async def test_pruning_only_takes_finished_ones(self, db: Database) -> None:
        live = await seed(db, number=1, at=1_000)
        done = await seed(db, number=2, at=1_000)
        await ci_repo.finish(
            db, done, head_sha="a", status="success", notified=True, at=1_000
        )
        assert await ci_repo.prune_terminal(db, older_than_ms=0) == 1
        assert await ci_repo.get(db, owner=live.owner, repo=live.repo, pr_number=1)


class TestIsolation:
    async def test_one_tenants_watch_is_invisible_to_another(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db)
        async with as_tenant(OTHER_TENANT_ID):
            assert await ci_repo.get(db, owner="acme", repo="api", pr_number=9) is None

    async def test_a_worker_poll_writes_one_tenants_row_only(
        self, db: Database, system_db: Database
    ) -> None:
        """Both teams watching the same public PR is not hypothetical.

        The worker role bypasses RLS, so without an explicit ``tenant_id``
        predicate one poll would stamp the verdict on both rows.
        """
        await seed(db, at=1_000)
        async with as_tenant(OTHER_TENANT_ID):
            await seed(db, at=1_000)

        claimed = await ci_repo.claim_due(system_db, at=1_000)
        mine = next(row for row in claimed if row.tenant_id == BOOTSTRAP_TENANT_ID)
        await ci_repo.finish(
            system_db, mine, head_sha="aaa", status="failure", notified=True
        )

        async with as_tenant(OTHER_TENANT_ID):
            theirs = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert theirs is not None
        assert theirs.state == "watching"
        assert theirs.notified_status is None


# ── the notice ───────────────────────────────────────────────────────────────


class TestTheNotice:
    def test_a_failure_names_the_red_checks(self) -> None:
        text = ci_text(
            "acme/api#9",
            ChecksResult(CheckState.FAILURE, 4, ("lint", "types")),
        )
        assert text == "⚠️ <b>CI failed</b> · acme/api#9 · lint, types"

    def test_a_pass_is_one_line_with_no_names(self) -> None:
        text = ci_text("acme/api#9", ChecksResult(CheckState.SUCCESS, 1))
        assert text == "✅ <b>CI passed</b> · acme/api#9 · 1 check"

    def test_fix_ci_only_appears_on_a_failure(self) -> None:
        store = NonceStore()
        failed = ci_keyboard(
            state=CheckState.FAILURE,
            session_id=SESSION,
            owner="acme",
            repo="api",
            pr_number=9,
            chat_id=CHAT,
            thread_id=THREAD,
            store=store,
        )
        assert failed is not None
        labels = [b.text for row in failed.inline_keyboard for b in row]
        assert labels == ["🔧 Fix CI"]

        passed = ci_keyboard(
            state=CheckState.SUCCESS,
            session_id=SESSION,
            owner="acme",
            repo="api",
            pr_number=9,
            chat_id=CHAT,
            store=store,
        )
        assert passed is not None
        assert [b.text for row in passed.inline_keyboard for b in row] == [
            "↗ Pull request"
        ]

    def test_fix_ci_survives_the_redeploy_it_will_certainly_outlive(self) -> None:
        """Minted in one process, tapped in the next — hours later."""
        markup = ci_keyboard(
            state=CheckState.FAILURE,
            session_id=SESSION,
            owner="acme",
            repo="api",
            pr_number=9,
            chat_id=CHAT,
            thread_id=THREAD,
            store=NonceStore(),
        )
        assert markup is not None
        data = markup.inline_keyboard[0][0].callback_data
        assert data is not None
        payload = parse(data)
        assert payload.action == Action.FIX_CI.value
        assert payload.nonce.startswith("."), "not a store handle: a signed payload"


# ── the loop ─────────────────────────────────────────────────────────────────


class FakeOutbox:
    def __init__(self, *, accepts: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self._accepts = accepts

    async def send_text(self, html: str, **kwargs: Any) -> list[int]:
        self.sent.append({"html": html, **kwargs})
        return [42] if self._accepts else []


class FakeGitHub:
    """One scripted answer per call, or an error to raise."""

    def __init__(self, *, pull: Any = None, checks: Any = None) -> None:
        self.pull = pull or PullRequest(number=9, head_sha="aaa")
        self.checks = checks or ChecksResult(CheckState.PENDING, 2)
        self.calls = 0

    async def get_pull(self, *_args: Any) -> PullRequest:
        self.calls += 1
        if isinstance(self.pull, Exception):
            raise self.pull
        return self.pull

    async def get_checks(self, *_args: Any) -> ChecksResult:
        if isinstance(self.checks, Exception):
            raise self.checks
        return self.checks


class FakePool:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def get(self, _tenant: Any) -> Any:
        return self.client


def watcher(system_db: Database, outbox: Any, client: Any) -> CiWatcher:
    return CiWatcher(
        system_db=system_db,
        outbox=outbox,
        clients=FakePool(client),  # type: ignore[arg-type]
        nonces=NonceStore(),
    )


async def _give_token(system_db: Database) -> None:
    await tenancy_repo.set_github_key(
        system_db,
        BOOTSTRAP_TENANT_ID,
        ciphertext=b"sealed",
        kid="v2",
        fingerprint="fp-gh",
    )


class TestTheLoop:
    async def test_a_red_run_is_announced_once_with_a_fix_button(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms())
        await _give_token(system_db)
        outbox = FakeOutbox()
        github = FakeGitHub(
            checks=ChecksResult(CheckState.FAILURE, 3, ("types",), "https://x/1")
        )

        assert await watcher(system_db, outbox, github).tick() == 1

        assert len(outbox.sent) == 1
        notice = outbox.sent[0]
        assert "CI failed" in notice["html"]
        assert notice["chat_id"] == CHAT and notice["thread_id"] == THREAD
        assert notice["silent"] is False, "a red run is why you pick the phone up"
        labels = [
            button.text
            for row in notice["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "🔧 Fix CI" in labels

        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None
        assert after.state == "done" and after.notified_sha == "aaa"

        # And a second pass says nothing, because there is nothing left to poll.
        assert await watcher(system_db, outbox, github).tick() == 0
        assert len(outbox.sent) == 1

    async def test_the_same_red_run_is_not_announced_twice(
        self, db: Database, system_db: Database
    ) -> None:
        """The next turn re-arms the watch. Nothing new happened, so: silence.

        Without this, every turn in a topic re-reads the same failed run and
        says so again — the notification equivalent of a stuck key.
        """
        await seed(db, at=now_ms())
        await _give_token(system_db)
        outbox = FakeOutbox()
        github = FakeGitHub(checks=ChecksResult(CheckState.FAILURE, 1, ("types",)))

        await watcher(system_db, outbox, github).tick()
        assert len(outbox.sent) == 1

        # A later turn ends and re-arms the watch, on the same commit.
        await seed(db, at=now_ms())
        await watcher(system_db, outbox, github).tick()

        assert len(outbox.sent) == 1, "same commit, same verdict, already said"
        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None and after.state == "done"

    async def test_a_new_commit_that_fails_again_is_news(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms())
        await _give_token(system_db)
        outbox = FakeOutbox()
        red = ChecksResult(CheckState.FAILURE, 1, ("types",))

        await watcher(
            system_db, outbox, FakeGitHub(pull=PullRequest(9, "aaa"), checks=red)
        ).tick()
        await seed(db, at=now_ms())
        await watcher(
            system_db, outbox, FakeGitHub(pull=PullRequest(9, "bbb"), checks=red)
        ).tick()

        assert len(outbox.sent) == 2, "a push that fails again is worth saying"

    async def test_a_green_run_is_announced_quietly(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms())
        await _give_token(system_db)
        outbox = FakeOutbox()
        github = FakeGitHub(checks=ChecksResult(CheckState.SUCCESS, 3))

        await watcher(system_db, outbox, github).tick()

        assert "CI passed" in outbox.sent[0]["html"]
        assert outbox.sent[0]["silent"] is True

    async def test_a_run_still_going_says_nothing_and_comes_back(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms())
        await _give_token(system_db)
        outbox = FakeOutbox()

        await watcher(
            system_db, outbox, FakeGitHub(checks=ChecksResult(CheckState.PENDING, 2))
        ).tick()

        assert outbox.sent == []
        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None
        assert after.state == "watching" and after.last_status == "pending"

    async def test_a_merged_pull_request_is_dropped_without_a_word(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms())
        await _give_token(system_db)
        outbox = FakeOutbox()
        github = FakeGitHub(
            pull=PullRequest(number=9, head_sha="aaa", state="closed", merged=True),
            checks=ChecksResult(CheckState.FAILURE, 1, ("types",)),
        )

        await watcher(system_db, outbox, github).tick()

        assert outbox.sent == []
        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None and after.state == "done"

    async def test_a_team_with_no_token_is_dropped_not_retried(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms())
        outbox = FakeOutbox()
        watch = CiWatcher(
            system_db=system_db,
            outbox=outbox,  # type: ignore[arg-type]
            clients=FakePool(None),  # type: ignore[arg-type]
        )

        await watch.tick()

        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None and after.state == "gave_up"

    async def test_a_repo_the_token_cannot_see_is_dropped_at_once(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms())
        await _give_token(system_db)
        github = FakeGitHub(pull=GitHubError("gone", status=404))

        await watcher(system_db, FakeOutbox(), github).tick()

        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None and after.state == "gave_up"

    async def test_a_github_blip_keeps_the_watch_alive(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms())
        await _give_token(system_db)
        github = FakeGitHub(pull=GitHubError("bad gateway", status=502))

        await watcher(system_db, FakeOutbox(), github).tick()

        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None
        assert after.state == "watching" and after.attempts == 1

    async def test_a_refused_message_is_not_recorded_as_said(
        self, db: Database, system_db: Database
    ) -> None:
        """Telegram refusing the notice must not lose the verdict."""
        await seed(db, at=now_ms())
        await _give_token(system_db)
        outbox = FakeOutbox(accepts=False)
        github = FakeGitHub(checks=ChecksResult(CheckState.FAILURE, 1, ("types",)))

        await watcher(system_db, outbox, github).tick()

        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None
        assert after.state == "watching", "still owed a message"
        assert after.notified_sha is None

    async def test_an_expired_watch_stops_polling(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, at=now_ms() - 10, ttl_ms=1)
        await _give_token(system_db)
        github = FakeGitHub()

        await watcher(system_db, FakeOutbox(), github).tick()

        assert github.calls == 0, "an expired watch does not spend a request"
        after = await ci_repo.get(db, owner="acme", repo="api", pr_number=9)
        assert after is not None and after.state == "gave_up"

    async def test_a_poll_that_raises_does_not_stop_the_pass(
        self, db: Database, system_db: Database
    ) -> None:
        await seed(db, number=1, at=now_ms())
        await seed(db, number=2, at=now_ms())
        await _give_token(system_db)

        class Exploding(FakeGitHub):
            async def get_pull(self, *args: Any) -> PullRequest:
                if args[-1] == 1:
                    raise RuntimeError("boom")
                return await super().get_pull(*args)

        polled = await watcher(system_db, FakeOutbox(), Exploding()).tick()

        assert polled == 2
        broken = await ci_repo.get(db, owner="acme", repo="api", pr_number=1)
        assert broken is not None and broken.attempts == 1


class TestTheWatchIsCreatedByAFinishedTurn:
    async def test_a_turn_that_opens_a_pull_request_starts_a_watch(
        self, db: Database, system_db: Database
    ) -> None:
        from ctb.bot.actions import BotActionSink

        await _give_token(system_db)
        await _store_transcript(
            db, "PR: https://github.com/acme/api/pull/31 — please review"
        )
        sink = BotActionSink(
            bot=None,  # type: ignore[arg-type]
            db=db,
            system_db=system_db,
            outbox=None,  # type: ignore[arg-type]
            status_cards=None,  # type: ignore[arg-type]
        )

        await sink._watch_ci(session_id=SESSION, chat_id=CHAT, thread_id=THREAD)

        row = await ci_repo.get(db, owner="acme", repo="api", pr_number=31)
        assert row is not None
        assert row.session_id == SESSION and row.chat_id == CHAT

    async def test_a_team_without_a_token_stores_nothing(
        self, db: Database, system_db: Database
    ) -> None:
        from ctb.bot.actions import BotActionSink

        await _store_transcript(db, "PR: https://github.com/acme/api/pull/31")
        sink = BotActionSink(
            bot=None,  # type: ignore[arg-type]
            db=db,
            system_db=system_db,
            outbox=None,  # type: ignore[arg-type]
            status_cards=None,  # type: ignore[arg-type]
        )

        await sink._watch_ci(session_id=SESSION, chat_id=CHAT, thread_id=THREAD)

        assert await ci_repo.get(db, owner="acme", repo="api", pr_number=31) is None

    async def test_a_turn_with_no_pull_request_stores_nothing(
        self, db: Database, system_db: Database
    ) -> None:
        from ctb.bot.actions import BotActionSink

        await _give_token(system_db)
        await _store_transcript(db, "All done, nothing to open.")
        sink = BotActionSink(
            bot=None,  # type: ignore[arg-type]
            db=db,
            system_db=system_db,
            outbox=None,  # type: ignore[arg-type]
            status_cards=None,  # type: ignore[arg-type]
        )

        await sink._watch_ci(session_id=SESSION, chat_id=CHAT, thread_id=THREAD)

        assert await ci_repo.count_watching(db) == 0


async def _store_transcript(db: Database, text: str) -> None:
    """One stored transcript message, the way the cursor writes them."""
    await db.execute("INSERT INTO sessions(id) VALUES (?)", (SESSION,))
    await db.execute(
        "INSERT INTO transcript_messages (session_id, message_id, session_index, "
        "                                 type, content_json, received_at_ms) "
        "VALUES (?, ?, ?, 'assistant', ?, ?)",
        (SESSION, uuid.uuid4().hex, 1, f'{{"text": "{text}"}}', now_ms()),
    )
