"""GitHub, read-only: whether CI passed on the pull request a turn opened."""

from __future__ import annotations

from ctb.github.client import ChecksResult, CheckState, GitHubClient, GitHubError
from ctb.github.links import PullRequestRef, find_pull_request
from ctb.github.pool import GITHUB_KEY_PURPOSE, GitHubPool

__all__ = [
    "GITHUB_KEY_PURPOSE",
    "CheckState",
    "ChecksResult",
    "GitHubClient",
    "GitHubError",
    "GitHubPool",
    "PullRequestRef",
    "find_pull_request",
]
