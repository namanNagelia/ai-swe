import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def require_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a tz-aware UTC datetime, got {value!r}")


def new_task_id(now: datetime) -> str:
    require_utc("now", now)
    return f"t-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class SourceKind(StrEnum):
    CLI = "cli"
    WEB = "web"
    GITHUB = "github"


@dataclass(frozen=True, slots=True)
class TaskSource:
    kind: SourceKind
    reply_to: str | None = None  # github: "owner/repo#12" or a comment URL

    def __post_init__(self) -> None:
        if self.kind is SourceKind.GITHUB and not self.reply_to:
            raise ValueError("github tasks need reply_to")


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    prompt: str  # untrusted
    repo: str  # "owner/name"
    source: TaskSource
    created_at: datetime
    base_branch: str | None = None  # None = repo default

    def __post_init__(self) -> None:
        if not _REPO.match(self.repo):
            raise ValueError(f"repo must be 'owner/name', got {self.repo!r}")
        if not self.prompt.strip():
            raise ValueError("prompt must be non-empty")
        require_utc("created_at", self.created_at)


@dataclass(frozen=True, slots=True, order=True)
class StepKey:
    task_id: str
    n: int

    def __post_init__(self) -> None:
        if self.n < 0:
            raise ValueError(f"n must be >= 0, got {self.n}")
