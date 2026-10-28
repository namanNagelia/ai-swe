from datetime import datetime

from rootorroute.app.agent import replay, run_agent
from rootorroute.app.deps import Deps
from rootorroute.app.limits import Guard
from rootorroute.app.prompts import file_tree, planner_messages, repo_instructions
from rootorroute.app.tools.context import ToolContext, ToolFailed
from rootorroute.domain.model import Message
from rootorroute.domain.state import Phase
from rootorroute.domain.store import NewTicket, TaskRecord
from rootorroute.domain.tools import CreateTicket, FinishPlan, tools_for
from rootorroute.domain.workspace import Worktree

STOP = frozenset({"finish_plan"})


async def plan(deps: Deps, record: TaskRecord, worktree: Worktree, started: datetime) -> None:
    task_id = record.task.id

    async def create_ticket(args: CreateTicket, ctx: ToolContext) -> str:
        existing = await deps.store.tickets(task_id)
        if len(existing) >= deps.max_tickets:
            raise ToolFailed(f"the plan already has {deps.max_tickets} tickets; merge some")
        t = await deps.store.add_ticket(
            task_id, NewTicket(args.title, args.goal, tuple(args.acceptance))
        )
        return f"created ticket {t.seq}: {t.title}"

    async def finish_plan(args: FinishPlan, ctx: ToolContext) -> str:
        if not await deps.store.tickets(task_id):
            raise ToolFailed("create at least one ticket with create_ticket first")
        await deps.store.finish_plan(task_id, args.summary, args.assumptions)
        return "plan saved"

    executor = deps.executor.with_handlers(
        {"create_ticket": create_ticket, "finish_plan": finish_plan}
    )
    ctx = ToolContext(task_id, Phase.PLAN, None, worktree.path)
    instructions = await repo_instructions(deps.sandbox, worktree.path)
    tree = await file_tree(deps.sandbox, worktree.path)
    system, user = planner_messages(record, instructions, tree, deps.max_tickets)
    messages = [Message.system(system), Message.user(user)]
    messages += replay(await deps.store.steps(task_id, Phase.PLAN), STOP)
    guard = Guard(deps.store, deps.clock, deps.limits, task_id, None, started)
    await run_agent(
        deps.model, deps.store, executor, ctx, messages, tools_for(Phase.PLAN), STOP, guard
    )
