import asyncio
import contextlib
import logging
from dataclasses import dataclass

from rootorroute.app.checks import resolve_checks
from rootorroute.app.deliver import deliver, reply
from rootorroute.app.deps import Deps
from rootorroute.app.implementer import ChecksFailing, implement
from rootorroute.app.limits import Cancelled
from rootorroute.app.planner import plan
from rootorroute.domain.errors import LimitExceeded
from rootorroute.domain.state import BlockedReason, Phase, TaskState
from rootorroute.domain.store import Step, Store, TaskRecord
from rootorroute.domain.ticket import Ticket, TicketStatus

MAX_RESUME_ATTEMPTS = 3
POLL_S = 1.0

log = logging.getLogger("rootorroute.runner")


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


class Runner:
    def __init__(self, deps: Deps, concurrency: int = 1) -> None:
        self._deps = deps
        self._concurrency = concurrency
        self._active: dict[str, asyncio.Task[None]] = {}

    async def run_forever(self, stop: asyncio.Event) -> None:
        for record, _ in await tasks_to_resume(self._deps.store):
            self._start(record)
        while not stop.is_set():
            await self._fill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), POLL_S)
        for t in self._active.values():
            t.cancel()
        await asyncio.gather(*self._active.values(), return_exceptions=True)

    async def _fill(self) -> None:
        store = self._deps.store
        if len(self._active) >= self._concurrency:
            return
        # In-progress tasks nobody is running are retries (blocked -> in_progress via the API).
        for record in await store.list_tasks([TaskState.IN_PROGRESS], limit=100):
            if record.task.id not in self._active and len(self._active) < self._concurrency:
                self._start(record)
        while len(self._active) < self._concurrency:
            record = await store.claim_next_queued()
            if record is None:
                return
            self._start(record)

    def _start(self, record: TaskRecord) -> None:
        task_id = record.task.id
        t = asyncio.create_task(self.run_task(record), name=task_id)
        self._active[task_id] = t
        t.add_done_callback(lambda _: self._active.pop(task_id, None))

    async def run_task(self, record: TaskRecord) -> None:
        deps, task_id = self._deps, record.task.id
        started = record.task.created_at
        log.info("task started", extra={"task_id": task_id})
        worktree = None
        try:
            await reply(deps, record, "reply:ack", f"On it (task `{task_id}`).")
            worktree = await deps.workspace.prepare(record.task)
            await deps.store.set_branch(task_id, worktree.branch)
            point = await resume_point(deps.store, record)
            if point.phase is Phase.PLAN:
                await plan(deps, record, worktree, started)
            record = await deps.store.get_task(task_id) or record
            await implement(deps, record, worktree, started)
            record = await deps.store.get_task(task_id) or record
            checks = await resolve_checks(deps.sandbox, worktree.path)
            await deliver(deps, record, worktree, "; ".join(checks) or "none configured")
            await deps.workspace.cleanup(worktree)
            log.info("task done", extra={"task_id": task_id})
        except Cancelled:
            log.info("task stopped (cancelled)", extra={"task_id": task_id})
        except asyncio.CancelledError:
            raise
        except LimitExceeded as e:
            await self._block(record, BlockedReason.LIMIT, e.message)
        except ChecksFailing as e:
            await self._block(record, BlockedReason.CHECKS_FAILING, f"checks kept failing:\n{e}")
        except Exception as e:
            log.exception("task failed", extra={"task_id": task_id})
            await self._block(record, BlockedReason.ERROR, f"{type(e).__name__}: {e}")

    async def _block(self, record: TaskRecord, reason: BlockedReason, detail: str) -> None:
        store, task_id = self._deps.store, record.task.id
        current = await store.get_task(task_id)
        if current is not None and current.state is TaskState.IN_PROGRESS:
            await store.set_state(task_id, TaskState.BLOCKED, reason)
        await store.audit(task_id, "state", {"blocked": reason.value, "detail": detail[:4000]})
        n = len(await store.audit_since(0, task_id))
        await reply(
            self._deps,
            record,
            f"reply:blocked:{n}",
            f"Stopped ({reason.value}): {detail[:1500]}\n\nRetry with `rr retry {task_id}`.",
        )
