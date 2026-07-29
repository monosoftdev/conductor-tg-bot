"""A very small, read-only GitHub client: "did CI pass on this PR?".

Scope is the point. This client can read a pull request, read the checks on a
commit, and identify its own token — nothing else. It cannot write, so a stolen
or over-scoped token still cannot be made to push, merge or comment *through
this code*, and a review of what the bot does to a customer's repository is
three methods long.

Two APIs answer the question, and both are asked:

* ``/commits/{sha}/check-runs`` — GitHub Actions and anything else that
  registers a check run.
* ``/commits/{sha}/status`` — the older commit-status API, still how several
  external CI providers report.

A repository using only the second would otherwise look like "no CI at all"
forever. The results are merged, and *pending wins*: a green check-run beside a
running status is not a pass yet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, Self

import httpx

from ctb import USER_AGENT

__all__ = [
    "CheckState",
    "ChecksResult",
    "GitHubClient",
    "GitHubError",
    "PullRequest",
    "check_github_token",
]

DEFAULT_GITHUB_API_URL: Final = "https://api.github.com"
CONNECT_TIMEOUT_S: Final = 5.0
READ_TIMEOUT_S: Final = 15.0
MAX_CHECKS: Final = 100

#: Sent on every request. GitHub rejects requests without a User-Agent outright,
#: and pins behaviour to a dated API version rather than "whatever is live".
_HEADERS: Final[dict[str, str]] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": USER_AGENT,
}

#: Conclusions that mean the check ran and did not pass. ``None`` while running.
_BAD_CONCLUSIONS: Final[frozenset[str]] = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
#: Everything else GitHub can conclude with is a pass for our purposes:
#: ``success``, ``neutral``, ``skipped``, ``stale``.
_RUNNING_STATUSES: Final[frozenset[str]] = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested"}
)


class CheckState(StrEnum):
    """What the commit's checks add up to."""

    NONE = "none"
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"

    @property
    def is_terminal(self) -> bool:
        return self in (CheckState.SUCCESS, CheckState.FAILURE)


class GitHubError(Exception):
    """Any non-answer from GitHub. ``status`` is ``None`` for a transport error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_auth(self) -> bool:
        """A token problem, not a transient one: stop asking until it changes."""
        return self.status in (401, 403)

    @property
    def is_missing(self) -> bool:
        """404 also covers "the token cannot see this repository" — same fix."""
        return self.status == 404


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    head_sha: str
    state: str = "open"
    merged: bool = False
    draft: bool = False
    title: str = ""

    @property
    def is_closed(self) -> bool:
        return self.state != "open"


@dataclass(frozen=True, slots=True)
class ChecksResult:
    state: CheckState = CheckState.NONE
    total: int = 0
    failed: tuple[str, ...] = field(default_factory=tuple)
    #: A link to one failing run, so the notice can point at the log.
    failed_url: str | None = None


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


class GitHubClient:
    """One instance per tenant token, built by :class:`ctb.github.pool.GitHubPool`.

    There is no process-wide client and no token read from settings, for the
    reason ``CLAUDE.md`` gives about the Conductor client: a caller that forgets
    to pass a tenant must fail by name, not quietly read another customer's
    repositories.
    """

    def __init__(
        self,
        *,
        token: str,
        api_url: str = DEFAULT_GITHUB_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._http = http_client or httpx.AsyncClient(
            base_url=self.api_url,
            headers={**_HEADERS, "Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            transport=transport,
            follow_redirects=False,
        )
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- the three reads ------------------------------------------------------

    async def get_login(self) -> str:
        """Who this token is. Used to validate one at intake."""
        payload = await self._get("/user")
        if not isinstance(payload, dict):
            raise GitHubError("GET /user did not return an object")
        return _as_str(payload.get("login"), "?")

    async def get_pull(self, owner: str, repo: str, number: int) -> PullRequest:
        payload = await self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubError("pull request payload was not an object")
        head = payload.get("head")
        return PullRequest(
            number=number,
            head_sha=_as_str(head.get("sha")) if isinstance(head, dict) else "",
            state=_as_str(payload.get("state"), "open"),
            merged=payload.get("merged") is True,
            draft=payload.get("draft") is True,
            title=_as_str(payload.get("title")),
        )

    async def get_checks(self, owner: str, repo: str, sha: str) -> ChecksResult:
        """Merge check-runs and commit statuses for one commit."""
        runs, statuses = await asyncio.gather(
            self._get(
                f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
                params={"per_page": MAX_CHECKS},
            ),
            self._get(f"/repos/{owner}/{repo}/commits/{sha}/status"),
        )
        return _merge(runs, statuses)

    # -- the wire -------------------------------------------------------------

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise GitHubError(
                f"GET {path} -> {response.status_code}: {_message_of(response)}"[:200],
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError(f"GET {path} returned non-JSON") from exc


def _message_of(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(payload, dict):
        return _as_str(payload.get("message"), "error")
    return "error"


def _merge(runs: Any, statuses: Any) -> ChecksResult:
    """Fold both payloads into one verdict. Pending beats success, always."""
    pending = False
    failed: list[str] = []
    failed_url: str | None = None
    total = 0

    entries = runs.get("check_runs") if isinstance(runs, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict):
            continue
        total += 1
        name = _as_str(entry.get("name"), "check")
        status = _as_str(entry.get("status")).lower()
        conclusion = _as_str(entry.get("conclusion")).lower()
        if status in _RUNNING_STATUSES or not conclusion:
            pending = True
            continue
        if conclusion in _BAD_CONCLUSIONS:
            failed.append(name)
            failed_url = failed_url or _as_str(entry.get("html_url")) or None

    combined = _as_str(statuses.get("state") if isinstance(statuses, dict) else None)
    contexts = statuses.get("statuses") if isinstance(statuses, dict) else None
    seen_statuses = len(contexts) if isinstance(contexts, list) else 0
    total += seen_statuses
    if seen_statuses:
        if combined == "pending":
            pending = True
        elif combined in ("failure", "error"):
            for entry in contexts if isinstance(contexts, list) else ():
                if not isinstance(entry, dict):
                    continue
                if _as_str(entry.get("state")).lower() in ("failure", "error"):
                    failed.append(_as_str(entry.get("context"), "status"))
                    failed_url = failed_url or _as_str(entry.get("target_url")) or None

    if total == 0:
        return ChecksResult(CheckState.NONE)
    # Failure is reported even while other checks run: a red required check is
    # not going to turn green, and waiting for the slow ones only delays the fix.
    if failed:
        return ChecksResult(
            CheckState.FAILURE, total, tuple(failed[:8]), failed_url=failed_url
        )
    if pending:
        return ChecksResult(CheckState.PENDING, total)
    return ChecksResult(CheckState.SUCCESS, total)


async def check_github_token(
    token: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """``None`` when the token works, else a short reason to show the owner."""
    client = GitHubClient(token=token, transport=transport)
    try:
        await client.get_login()
    except GitHubError as exc:
        if exc.is_auth:
            return "GitHub rejected that token."
        return f"GitHub could not be reached ({exc})."[:120]
    finally:
        await client.aclose()
    return None
