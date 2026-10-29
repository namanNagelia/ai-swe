from dataclasses import dataclass
from pathlib import Path

from rootorroute.domain.model import Image
from rootorroute.domain.state import Phase


@dataclass(frozen=True, slots=True)
class ToolContext:
    task_id: str
    phase: Phase
    ticket_seq: int | None
    worktree: Path


@dataclass(frozen=True, slots=True)
class ToolOutput:
    text: str
    images: tuple[Image, ...] = ()


class ToolFailed(Exception):
    """A tool ran but couldn't do what was asked; the message goes back to the model."""
