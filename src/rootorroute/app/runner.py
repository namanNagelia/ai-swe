from dataclasses import dataclass

from rootorroute.domain.state import BlockedReason, Phase, TaskState
from rootorroute.domain.store import Step, Store, TaskRecord
from rootorroute.domain.ticket import Ticket, TicketStatus

MAX_RESUME_ATTEMPTS = 3

# TODO (RR-0701): the task runner (workspace -> plan -> implement -> deliver) calls resume().


@dataclass(frozen=True, slots=True)
class ResumePoint:
    phase: Phase | None  # None: planning and every ticket are done; go straight to delivery
    ticket: Ticket | None
    steps: tuple[Step, ...]


async def resume_point(store: Store, record: TaskRecord) -> ResumePoint:
    task_id = record.task.id
    if record.plan_summary is None:
        return ResumePoint(Phase.PLAN, None, tuple(await store.steps(task_id, Phase.PLAN)))
    todo = [t for t in await store.tickets(task_id) if t.status is TicketStatus.TODO]
    if not todo:
        return ResumePoint(None, None, ())
    ticket = todo[0]
    steps = await store.steps(task_id, Phase.IMPLEMENT, ticket.seq)
    return ResumePoint(Phase.IMPLEMENT, ticket, tuple(steps))


async def tasks_to_resume(store: Store) -> list[tuple[TaskRecord, ResumePoint]]:
    """In-progress tasks to continue on start. A task that restarts MAX_RESUME_ATTEMPTS times
    without recording a step goes to blocked(error)."""
    out: list[tuple[TaskRecord, ResumePoint]] = []
    for record in await store.list_tasks([TaskState.IN_PROGRESS], limit=1000):
        attempts = await store.bump_resume_attempts(record.task.id)
        if attempts > MAX_RESUME_ATTEMPTS:
            await store.set_state(record.task.id, TaskState.BLOCKED, BlockedReason.ERROR)
            continue
        out.append((record, await resume_point(store, record)))
    return out
