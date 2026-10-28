from rootorroute.app.deps import Deps
from rootorroute.domain.state import TaskState
from rootorroute.domain.store import Json, TaskRecord
from rootorroute.domain.task import SourceKind
from rootorroute.domain.ticket import Ticket
from rootorroute.domain.workspace import Worktree


def pr_body(record: TaskRecord, tickets: list[Ticket], checks: str) -> str:
    lines = [record.plan_summary or record.task.prompt, ""]
    if record.assumptions:
        lines += ["**Assumptions**", *(f"- {a}" for a in record.assumptions), ""]
    lines += ["**Tickets**"]
    for t in tickets:
        lines.append(f"- {t.ref} {t.title}" + (f": {t.summary}" if t.summary else ""))
    lines += ["", f"**Checks:** {checks}", "", f"_Task {record.task.id}, by Root or Route._"]
    return "\n".join(lines)


def _title(record: TaskRecord) -> str:
    text = (record.plan_summary or record.task.prompt).strip().splitlines()[0]
    return text if len(text) <= 72 else text[:69].rstrip() + "..."


async def deliver(deps: Deps, record: TaskRecord, worktree: Worktree, checks: str) -> str | None:
    """Push and open the PR (each at most once), mark the task done. Returns the PR URL."""
    task_id, repo = record.task.id, record.task.repo
    sha = await deps.workspace.export_branch(worktree)
    url: str | None = None
    if sha != worktree.base_sha:
        if deps.code_host is None:
            raise RuntimeError("no GitHub token configured; can't push or open the PR")
        code_host = deps.code_host

        async def push() -> Json:
            await deps.workspace.push(worktree, repo)
            return {"sha": sha}

        async def open_pr() -> Json:
            existing = await code_host.find_pr(repo, worktree.branch)
            if existing is not None:
                return {"url": existing.url, "number": existing.number}
            tickets = await deps.store.tickets(task_id)
            pr = await code_host.open_pr(
                repo,
                worktree.branch,
                worktree.base_branch,
                _title(record),
                pr_body(record, tickets, checks),
            )
            return {"url": pr.url, "number": pr.number}

        await deps.store.once(task_id, f"push:{sha}", push)
        url = str((await deps.store.once(task_id, "open_pr", open_pr))["url"])
        await deps.store.set_pr_url(task_id, url)
    await deps.store.set_state(task_id, TaskState.DONE)
    done = f"Done: {url}" if url else "Done: no changes were needed."
    await reply(deps, record, "reply:done", done)
    return url


async def reply(deps: Deps, record: TaskRecord, key: str, body: str) -> None:
    source = record.task.source
    if source.kind is not SourceKind.GITHUB or deps.code_host is None or not source.reply_to:
        return
    repo, _, number = source.reply_to.partition("#")
    if not number.isdigit():
        return
    code_host = deps.code_host

    async def post() -> Json:
        return {"url": await code_host.comment(repo, int(number), body)}

    await deps.store.once(record.task.id, key, post)
