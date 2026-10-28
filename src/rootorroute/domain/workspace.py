import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rootorroute.domain.task import Task


@dataclass(frozen=True, slots=True)
class Worktree:
    task_id: str
    path: Path
    branch: str
    base_branch: str
    base_sha: str


def branch_name(task: Task) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.prompt.lower()).strip("-")[:40].strip("-")
    return f"rr/{task.id}-{slug}" if slug else f"rr/{task.id}"


class Workspace(Protocol):
    async def prepare(self, task: Task) -> Worktree:
        """Clone or fetch the repo and create (or reuse, on resume) the task's worktree."""
        ...

    async def commit(self, worktree: Worktree, message: str) -> str | None:
        """Stage everything and commit as the bot. Returns the sha, or None if nothing changed."""
        ...

    async def changed_files(self, worktree: Worktree) -> list[str]:
        """Files changed relative to the base commit (committed or not)."""
        ...

    async def export_branch(self, worktree: Worktree) -> str:
        """Copy the task branch from the sandbox working copy into the mirror; returns its sha."""
        ...

    async def push(self, worktree: Worktree, repo: str) -> None:
        """Push the task branch from the mirror to the repo's remote. Never the default branch."""
        ...

    async def cleanup(self, worktree: Worktree) -> None: ...
