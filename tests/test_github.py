"""The read-only GitHub client and the pull-request link scanner.

Everything runs against ``httpx.MockTransport`` — no network, ever.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ctb import USER_AGENT
from ctb.crypto import SecretBox, SecretError
from ctb.db.repo.tenancy import TenantRow
from ctb.github.client import (
    CheckState,
    GitHubClient,
    GitHubError,
    check_github_token,
)
from ctb.github.links import find_pull_request
from ctb.github.pool import GITHUB_KEY_PURPOSE, GitHubPool
from tests.pg import BOOTSTRAP_TENANT_ID, OTHER_TENANT_ID

# ── links ────────────────────────────────────────────────────────────────────


class TestFindingThePullRequest:
    def test_a_plain_announcement(self) -> None:
        found = find_pull_request(
            "Fixed the flake.\n\nPR: https://github.com/monosoftdev/conductor-tg-bot/pull/24"
        )
        assert found is not None
        assert (found.owner, found.repo, found.number) == (
            "monosoftdev",
            "conductor-tg-bot",
            24,
        )
        assert found.slug == "monosoftdev/conductor-tg-bot#24"

    def test_the_last_one_wins(self) -> None:
        """A turn that cites an older PR before opening its own ends on its own."""
        found = find_pull_request(
            "As in https://github.com/acme/api/pull/7 — opened "
            "https://github.com/acme/api/pull/9 just now"
        )
        assert found is not None and found.number == 9

    def test_it_survives_json_escaping(self) -> None:
        """The scan runs over stored ``content_json``, not rendered text."""
        blob = '{"text": "see https://github.com/a-b/c.d_e/pull/1234/files for more"}'
        found = find_pull_request(blob)
        assert found is not None
        assert (found.owner, found.repo, found.number) == ("a-b", "c.d_e", 1234)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "https://github.com/acme/api/issues/9",
            "https://gitlab.com/acme/api/pull/9",
            "https://github.com/acme/api/pull/",
            "a pull request, number 9, in acme/api",
        ],
    )
    def test_anything_else_is_not_a_guess(self, text: str) -> None:
        assert find_pull_request(text) is None


# ── client ───────────────────────────────────────────────────────────────────


def _pull(sha: str = "abc123", **extra: Any) -> dict[str, Any]:
    return {"head": {"sha": sha}, "state": "open", "merged": False, **extra}


def _runs(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(runs), "check_runs": list(runs)}


def _empty_status() -> dict[str, Any]:
    return {"state": "pending", "statuses": []}


def routed(**bodies: Any) -> httpx.MockTransport:
    """Route by path suffix: ``pulls``, ``check_runs``, ``status``, ``user``."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check-runs"):
            body = bodies.get("check_runs", _runs())
        elif path.endswith("/status"):
            body = bodies.get("status", _empty_status())
        elif "/pulls/" in path:
            body = bodies.get("pulls", _pull())
        else:
            body = bodies.get("user", {"login": "octocat"})
        if isinstance(body, int):
            return httpx.Response(body, json={"message": "nope"})
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


def client(transport: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(token="ghp_x", transport=transport)


class TestChecks:
    async def test_all_green_is_a_pass(self) -> None:
        transport = routed(
            check_runs=_runs(
                {"name": "tests", "status": "completed", "conclusion": "success"},
                {"name": "lint", "status": "completed", "conclusion": "skipped"},
            )
        )
        async with client(transport) as api:
            result = await api.get_checks("acme", "api", "abc123")
        assert result.state is CheckState.SUCCESS
        assert result.total == 2

    async def test_one_red_check_is_a_failure_and_is_named(self) -> None:
        transport = routed(
            check_runs=_runs(
                {"name": "tests", "status": "completed", "conclusion": "success"},
                {
                    "name": "types",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/acme/api/runs/5",
                },
            )
        )
        async with client(transport) as api:
            result = await api.get_checks("acme", "api", "abc123")
        assert result.state is CheckState.FAILURE
        assert result.failed == ("types",)
        assert result.failed_url == "https://github.com/acme/api/runs/5"

    async def test_a_running_check_is_not_a_verdict_yet(self) -> None:
        transport = routed(
            check_runs=_runs(
                {"name": "tests", "status": "completed", "conclusion": "success"},
                {"name": "e2e", "status": "in_progress", "conclusion": None},
            )
        )
        async with client(transport) as api:
            result = await api.get_checks("acme", "api", "abc123")
        assert result.state is CheckState.PENDING
        assert not result.state.is_terminal

    async def test_a_repo_with_no_checks_says_so_rather_than_passing(self) -> None:
        async with client(routed()) as api:
            result = await api.get_checks("acme", "api", "abc123")
        assert result.state is CheckState.NONE
        assert not result.state.is_terminal

    async def test_the_old_commit_status_api_still_counts(self) -> None:
        """A repo whose CI predates check runs must not read as "no CI"."""
        transport = routed(
            status={
                "state": "failure",
                "statuses": [
                    {
                        "context": "buildkite",
                        "state": "failure",
                        "target_url": "https://buildkite.com/x",
                    }
                ],
            }
        )
        async with client(transport) as api:
            result = await api.get_checks("acme", "api", "abc123")
        assert result.state is CheckState.FAILURE
        assert result.failed == ("buildkite",)

    async def test_a_pending_status_beats_a_green_check_run(self) -> None:
        transport = routed(
            check_runs=_runs(
                {"name": "tests", "status": "completed", "conclusion": "success"}
            ),
            status={"state": "pending", "statuses": [{"context": "deploy"}]},
        )
        async with client(transport) as api:
            result = await api.get_checks("acme", "api", "abc123")
        assert result.state is CheckState.PENDING


class TestPullAndAuth:
    async def test_the_pull_request_carries_its_head_commit(self) -> None:
        transport = routed(pulls=_pull("deadbee", merged=False, title="Fix flake"))
        async with client(transport) as api:
            pull = await api.get_pull("acme", "api", 9)
        assert pull.head_sha == "deadbee"
        assert not pull.is_closed and not pull.merged

    async def test_a_merged_pull_request_says_so(self) -> None:
        transport = routed(pulls=_pull(state="closed", merged=True))
        async with client(transport) as api:
            pull = await api.get_pull("acme", "api", 9)
        assert pull.merged and pull.is_closed

    async def test_every_request_carries_a_user_agent_and_the_token(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"login": "octocat"})

        async with client(httpx.MockTransport(handle)) as api:
            assert await api.get_login() == "octocat"
        assert seen[0].headers["user-agent"] == USER_AGENT
        assert seen[0].headers["authorization"] == "Bearer ghp_x"
        assert seen[0].headers["x-github-api-version"] == "2022-11-28"

    async def test_a_401_is_an_auth_error_and_a_404_is_a_missing_one(self) -> None:
        async with client(routed(user=401)) as api:
            with pytest.raises(GitHubError) as caught:
                await api.get_login()
        assert caught.value.is_auth and not caught.value.is_missing

        async with client(routed(pulls=404)) as api:
            with pytest.raises(GitHubError) as second:
                await api.get_pull("acme", "api", 9)
        assert second.value.is_missing

    async def test_a_bad_token_is_refused_at_intake(self) -> None:
        assert await check_github_token("ghp_x", transport=routed(user=401)) == (
            "GitHub rejected that token."
        )
        assert await check_github_token("ghp_x", transport=routed()) is None


# ── pool ─────────────────────────────────────────────────────────────────────


def _tenant(tenant_id: Any, box: SecretBox, token: str | None) -> TenantRow:
    sealed = (
        None
        if token is None
        else box.seal(token, tenant_id=tenant_id, purpose=GITHUB_KEY_PURPOSE)
    )
    return TenantRow(
        id=tenant_id,
        slug="acme",
        name="Acme",
        status="active",
        github_key_ct=sealed,
        github_key_fp=None if token is None else "fp-" + token[-3:],
    )


class TestPool:
    async def test_a_tenant_without_a_token_gets_no_client(
        self, secret_box: SecretBox
    ) -> None:
        pool = GitHubPool(secret_box)
        assert await pool.get(_tenant(BOOTSTRAP_TENANT_ID, secret_box, None)) is None

    async def test_each_tenant_opens_its_own_token(self, secret_box: SecretBox) -> None:
        opened: list[str] = []
        pool = GitHubPool(
            secret_box,
            factory=lambda token: (
                opened.append(token) or GitHubClient(token=token, transport=routed())
            ),
        )
        first = await pool.get(_tenant(BOOTSTRAP_TENANT_ID, secret_box, "ghp_aaa"))
        second = await pool.get(_tenant(OTHER_TENANT_ID, secret_box, "ghp_bbb"))
        assert opened == ["ghp_aaa", "ghp_bbb"]
        assert first is not second
        # Cached: the same tenant and fingerprint is not decrypted twice.
        assert await pool.get(_tenant(BOOTSTRAP_TENANT_ID, secret_box, "ghp_aaa")) is (
            first
        )
        assert opened == ["ghp_aaa", "ghp_bbb"]
        await pool.aclose()

    async def test_another_tenants_row_cannot_open_a_borrowed_token(
        self, secret_box: SecretBox
    ) -> None:
        """The AAD binding, from the pool's side: a swapped blob fails shut."""
        stolen = _tenant(BOOTSTRAP_TENANT_ID, secret_box, "ghp_aaa")
        impostor = TenantRow(
            id=OTHER_TENANT_ID,
            slug="other",
            name="Other",
            status="active",
            github_key_ct=stolen.github_key_ct,
            github_key_fp=stolen.github_key_fp,
        )
        pool = GitHubPool(secret_box)
        with pytest.raises(SecretError):
            await pool.get(impostor)

    async def test_revoking_evicts_the_decrypted_token(
        self, secret_box: SecretBox
    ) -> None:
        pool = GitHubPool(
            secret_box,
            factory=lambda token: GitHubClient(token=token, transport=routed()),
        )
        await pool.get(_tenant(BOOTSTRAP_TENANT_ID, secret_box, "ghp_aaa"))
        assert await pool.forget(BOOTSTRAP_TENANT_ID) == 1
        assert await pool.forget(BOOTSTRAP_TENANT_ID) == 0
