from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

MentionKind = Literal["issue", "pr", "review"]


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    head: str
    base: str
    state: str


@dataclass(frozen=True, slots=True)
class Mention:
    id: str  # the comment's id, used to dedupe
    repo: str
    kind: MentionKind
    number: int  # issue or PR number
    author: str
    body: str  # untrusted
    url: str
    title: str  # untrusted
    context: str  # issue/PR body, untrusted
    head_branch: str | None  # set for comments on a PR
    created_at: datetime


class CodeHostError(Exception):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class CodeHost(Protocol):
    async def default_branch(self, repo: str) -> str: ...

    async def find_pr(self, repo: str, head: str) -> PullRequest | None:
        """The open PR whose head is this branch of this repo, if any."""
        ...

    async def open_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...

    async def comment(self, repo: str, number: int, body: str) -> str:
        """Comment on an issue or PR; returns the comment URL."""
        ...

    async def mentions(self, since: datetime) -> list[Mention]:
        """New comments that @mention the bot, oldest first."""
        ...

    async def can_write(self, repo: str, user: str) -> bool: ...
