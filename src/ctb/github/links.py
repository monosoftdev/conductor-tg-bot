"""Find the pull request a turn opened, in the text it said so in.

The agent announces its PR as a URL — ``docs/GETTING_STARTED.md`` and this
repo's own ``CLAUDE.md`` both ask for exactly that, on its own last line. So the
link *is* the announcement, and parsing it needs no cooperation from the agent
beyond what it already does.

Deliberately strict: owner and repo match GitHub's own character set, the host
must be ``github.com``, and a trailing ``/files``-style path is ignored rather
than making the whole match fail. Anything else is not a pull request URL and a
watch is not created — never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["PullRequestRef", "find_pull_request", "pull_request_url"]

#: GitHub's own rules: owners are alphanumeric with single hyphens, repository
#: names additionally allow ``.`` and ``_``. Bounded lengths so a pathological
#: line cannot make this scan quadratic.
_PR_RE: Final = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/pull/"
    r"(?P<number>\d{1,9})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        """``owner/repo#12`` — how a human names it, and how the card does."""
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def url(self) -> str:
        return pull_request_url(self.owner, self.repo, self.number)


def pull_request_url(owner: str, repo: str, number: int) -> str:
    return f"https://github.com/{owner}/{repo}/pull/{number}"


def find_pull_request(text: str) -> PullRequestRef | None:
    """The **last** pull request URL in ``text``, or ``None``.

    Last, not first: a turn that discusses an older PR before opening its own
    ends on the one it opened, and that is the one worth watching.
    """
    found: PullRequestRef | None = None
    for match in _PR_RE.finditer(text):
        number = int(match.group("number"))
        if number <= 0:
            continue
        found = PullRequestRef(
            owner=match.group("owner"),
            repo=match.group("repo").removesuffix(".git"),
            number=number,
        )
    return found
