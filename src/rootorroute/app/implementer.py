from datetime import datetime

from rootorroute.app.agent import replay, run_agent
from rootorroute.app.checks import resolve_checks, run_checks
from rootorroute.app.deps import Deps
from rootorroute.app.limits import Guard
from rootorroute.app.prompts import implementer_messages, repo_instructions
from rootorroute.app.tools.context import ToolContext, ToolFailed
from rootorroute.domain.model import Message
from rootorroute.domain.state import Phase
from rootorroute.domain.store import TaskRecord
from rootorroute.domain.ticket import Ticket, TicketStatus
from rootorroute.domain.tools import FinishTicket, tools_for
from rootorroute.domain.workspace import Worktree

STOP = frozenset({"finish_ticket"})


class ChecksFailing(Exception):
    pass


async def implement(deps: Deps, record: TaskRecord, worktree: Worktree, started: datetime) -> None:
    instructions = await repo_instructions(deps.sandbox, worktree.path)
    checks = await resolve_checks(deps.sandbox, worktree.path)
    while True:
        tickets = await deps.store.tickets(record.task.id)
        todo = [t for t in tickets if t.status is TicketStatus.TODO]
        if not todo:
            return
        fresh = await deps.store.get_task(record.task.id)
        await _ticket(
            deps, fresh or record, worktree, tickets, todo[0], instructions, checks, started
        )


async def _ticket(
    deps: Deps,
    record: TaskRecord,
    worktree: Worktree,
    tickets: list[Ticket],
    ticket: Ticket,
    instructions: str,
    checks: list[str],
    started: datetime,
) -> None:
    task_id = record.task.id
    failures = 0

    async def finish_ticket(args: FinishTicket, ctx: ToolContext) -> str:
        nonlocal failures
        result = await run_checks(deps.sandbox, worktree.path, checks)
        if not result.passed:
            failures += 1
            if failures >= deps.check_failures_per_ticket:
                raise ChecksFailing(result.report)
            left = deps.check_failures_per_ticket - failures
            raise ToolFailed(f"checks failed ({left} attempts left):\n{result.report}")
        message = f"{ticket.title} ({ticket.ref})\n\n{args.summary}"
        sha = await deps.workspace.commit(worktree, message)
        await deps.store.finish_ticket(task_id, ticket.seq, args.summary)
        committed = f"committed {sha[:10]}" if sha else "nothing to commit"
        return f"{committed}; checks:\n{result.report}"

    executor = deps.executor.with_handlers({"finish_ticket": finish_ticket})
    ctx = ToolContext(task_id, Phase.IMPLEMENT, ticket.seq, worktree.path)
    system, user = implementer_messages(record, tickets, ticket, instructions, checks)
    messages = [Message.system(system), Message.user(user)]
    messages += replay(await deps.store.steps(task_id, Phase.IMPLEMENT, ticket.seq), STOP)
    guard = Guard(deps.store, deps.clock, deps.limits, task_id, ticket.seq, started)
    await run_agent(
        deps.model, deps.store, executor, ctx, messages, tools_for(Phase.IMPLEMENT), STOP, guard
    )
