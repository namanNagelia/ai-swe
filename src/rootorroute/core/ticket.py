from dataclasses import dataclass
from enum import StrEnum


class TicketStatus(StrEnum):
    TODO = "todo"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class Ticket:
    task_id: str
    seq: int  # 1, 2, 3 ... in plan order
    title: str
    goal: str
    acceptance: tuple[str, ...]
    status: TicketStatus = TicketStatus.TODO
    summary: str | None = None  # written by finish_ticket

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError(f"seq must be >= 1, got {self.seq}")
        if not self.title.strip():
            raise ValueError("title must be non-empty")

    @property
    def ref(self) -> str:
        return f"RR-{self.task_id}-{self.seq:02}"
