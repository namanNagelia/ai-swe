from dataclasses import dataclass
from pathlib import Path

from rootorroute.domain.state import Phase


@dataclass(frozen=True, slots=True)
class ToolContext:
    task_id: str
    phase: Phase
    ticket_seq: int | None
    worktree: Path


class ToolFailed(Exception):
    """A tool ran but couldn't do what was asked; the message goes back to the model."""
