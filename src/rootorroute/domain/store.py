from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from rootorroute.domain.state import BlockedReason, Phase, TaskState
from rootorroute.domain.task import Task
from rootorroute.domain.ticket import Ticket

StepKind = Literal["model", "tool"]
AuditKind = Literal["model_call", "tool_call", "state", "plan", "ticket", "external"]
Json = dict[str, Any]


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task: Task
    state: TaskState
    blocked_reason: BlockedReason | None
    branch: str | None
    pr_url: str | None
    plan_summary: str | None
    assumptions: tuple[str, ...]
    resume_attempts: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Step:
    task_id: str
    n: int
    phase: Phase
    ticket_seq: int | None
    kind: StepKind
    request: Json
    response: Json
    input_tokens: int
    output_tokens: int
    at: datetime


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    at: datetime
    task_id: str | None
    kind: AuditKind
    payload: Json


@dataclass(frozen=True, slots=True)
class NewTicket:
    title: str
    goal: str
    acceptance: tuple[str, ...]


class Store(Protocol):
    async def add_task(self, task: Task) -> TaskRecord: ...

    async def get_task(self, task_id: str) -> TaskRecord | None: ...

    async def list_tasks(
        self, states: Sequence[TaskState] | None = None, limit: int = 100
    ) -> list[TaskRecord]: ...

    async def claim_next_queued(self) -> TaskRecord | None:
        """Atomically move the oldest queued task to in_progress and return it."""
        ...

    async def set_state(
        self, task_id: str, state: TaskState, reason: BlockedReason | None = None
    ) -> TaskRecord: ...

    async def set_branch(self, task_id: str, branch: str) -> None: ...

    async def set_pr_url(self, task_id: str, url: str) -> None: ...

    async def bump_resume_attempts(self, task_id: str) -> int: ...

    async def add_ticket(self, task_id: str, ticket: NewTicket) -> Ticket: ...

    async def finish_plan(self, task_id: str, summary: str, assumptions: Sequence[str]) -> None: ...

    async def tickets(self, task_id: str) -> list[Ticket]: ...

    async def finish_ticket(self, task_id: str, seq: int, summary: str) -> Ticket: ...

    async def record_step(
        self,
        task_id: str,
        phase: Phase,
        ticket_seq: int | None,
        kind: StepKind,
        request: Json,
        response: Json,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> Step: ...

    async def steps(
        self, task_id: str, phase: Phase | None = None, ticket_seq: int | None = None
    ) -> list[Step]: ...

    async def once(self, task_id: str, key: str, fn: Callable[[], Awaitable[Json]]) -> Json:
        """Run an external side effect at most once per (task_id, key); return its result."""
        ...

    async def audit(self, task_id: str | None, kind: AuditKind, payload: Json) -> int: ...

    async def audit_since(
        self, after_id: int = 0, task_id: str | None = None, limit: int = 500
    ) -> list[AuditEntry]: ...
