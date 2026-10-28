from dataclasses import dataclass
from datetime import datetime, timedelta

from rootorroute.domain.clock import Clock
from rootorroute.domain.errors import LimitExceeded
from rootorroute.domain.state import TaskState
from rootorroute.domain.store import Store


class Cancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Limits:
    max_steps_per_ticket: int
    max_steps_per_task: int
    max_tokens_per_task: int
    max_hours_per_task: float


class Guard:
    """Called before every model call: stops a cancelled task or one over its limits."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        limits: Limits,
        task_id: str,
        ticket_seq: int | None,
        started: datetime,
    ) -> None:
        self._store, self._clock, self._limits = store, clock, limits
        self._task_id, self._ticket_seq, self._started = task_id, ticket_seq, started

    async def __call__(self) -> None:
        record = await self._store.get_task(self._task_id)
        if record is None or record.state is not TaskState.IN_PROGRESS:
            raise Cancelled(self._task_id)
        steps = await self._store.steps(self._task_id)
        hit: list[str] = []
        if len(steps) >= self._limits.max_steps_per_task:
            hit.append(f"max_steps_per_task={self._limits.max_steps_per_task}")
        if self._ticket_seq is not None:
            in_ticket = sum(1 for s in steps if s.ticket_seq == self._ticket_seq)
            if in_ticket >= self._limits.max_steps_per_ticket:
                hit.append(f"max_steps_per_ticket={self._limits.max_steps_per_ticket}")
        tokens = sum(s.input_tokens + s.output_tokens for s in steps)
        if tokens >= self._limits.max_tokens_per_task:
            hit.append(f"max_tokens_per_task={self._limits.max_tokens_per_task}")
        if self._clock.now() - self._started >= timedelta(hours=self._limits.max_hours_per_task):
            hit.append(f"max_hours_per_task={self._limits.max_hours_per_task}")
        if hit:
            raise LimitExceeded(hit)
