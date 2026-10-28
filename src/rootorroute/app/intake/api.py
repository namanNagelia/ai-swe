import asyncio
import json
from collections.abc import AsyncIterator, Generator, Sequence
from contextlib import contextmanager
from importlib import resources
from typing import Any, Literal

import uvicorn
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from rootorroute.domain.clock import Clock
from rootorroute.domain.errors import DomainError
from rootorroute.domain.state import BlockedReason, TaskState
from rootorroute.domain.store import AuditEntry, Store, TaskRecord
from rootorroute.domain.task import SourceKind, Task, TaskSource, new_task_id
from rootorroute.domain.ticket import Ticket

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
POLL_SECONDS = 1.0
FINAL_STATES = (TaskState.DONE, TaskState.BLOCKED)


class NewTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    repo: str
    base_branch: str | None = None
    source: Literal["cli", "web"] = "cli"


def _record(r: TaskRecord) -> dict[str, Any]:
    t = r.task
    return {
        "id": t.id,
        "prompt": t.prompt,
        "repo": t.repo,
        "base_branch": t.base_branch,
        "source": t.source.kind.value,
        "created_at": t.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
        "state": r.state.value,
        "blocked_reason": r.blocked_reason.value if r.blocked_reason else None,
        "branch": r.branch,
        "pr_url": r.pr_url,
        "plan_summary": r.plan_summary,
        "assumptions": list(r.assumptions),
    }


def _ticket(t: Ticket) -> dict[str, Any]:
    return {
        "seq": t.seq,
        "ref": t.ref,
        "title": t.title,
        "goal": t.goal,
        "acceptance": list(t.acceptance),
        "status": t.status.value,
        "summary": t.summary,
    }


def _event(e: AuditEntry) -> str:
    data = json.dumps(
        {"id": e.id, "at": e.at.isoformat(), "kind": e.kind, "payload": e.payload}, default=str
    )
    return f"id: {e.id}\ndata: {data}\n\n"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _int(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def create_app(store: Store, clock: Clock, allowed_repos: Sequence[str]) -> Starlette:
    repos = sorted(allowed_repos)
    closing = asyncio.Event()
    page = resources.files("rootorroute.app.intake.web").joinpath("index.html").read_text()

    async def index(_: Request) -> Response:
        return HTMLResponse(page)

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    async def config(_: Request) -> Response:
        return JSONResponse({"repos": repos})

    async def create_task(request: Request) -> Response:
        try:
            body = NewTaskBody.model_validate_json(await request.body())
        except ValidationError as e:
            err = e.errors(include_url=False)[0]
            return _error(400, f"{'.'.join(map(str, err['loc']))}: {err['msg']}".lstrip(": "))
        if body.repo not in repos:
            return _error(400, f"repo {body.repo!r} is not in the allowed repos")
        now = clock.now()
        try:
            task = Task(
                id=new_task_id(now),
                prompt=body.prompt,
                repo=body.repo,
                source=TaskSource(SourceKind(body.source)),
                created_at=now,
                base_branch=body.base_branch or None,
            )
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(_record(await store.add_task(task)), status_code=201)

    async def list_tasks(request: Request) -> Response:
        state = request.query_params.get("state")
        try:
            states = [TaskState(state)] if state else None
        except ValueError:
            return _error(400, f"unknown state {state!r}")
        limit = max(1, min(_int(request, "limit", 100), 500))
        return JSONResponse([_record(r) for r in await store.list_tasks(states, limit)])

    async def show_task(request: Request) -> Response:
        task_id = request.path_params["id"]
        record = await store.get_task(task_id)
        if record is None:
            return _error(404, f"task {task_id} not found")
        tickets = [_ticket(t) for t in await store.tickets(task_id)]
        return JSONResponse({**_record(record), "tickets": tickets})

    async def move(
        task_id: str, allowed: Sequence[TaskState], to: TaskState, reason: BlockedReason | None
    ) -> Response:
        record = await store.get_task(task_id)
        if record is None:
            return _error(404, f"task {task_id} not found")
        if record.state not in allowed:
            return _error(409, f"task is {record.state.value}")
        try:
            record = await store.set_state(task_id, to, reason)
        except DomainError as e:
            return _error(409, e.message)
        return JSONResponse(_record(record))

    async def cancel(request: Request) -> Response:
        return await move(
            request.path_params["id"],
            (TaskState.QUEUED, TaskState.IN_PROGRESS),
            TaskState.BLOCKED,
            BlockedReason.CANCELLED,
        )

    async def retry(request: Request) -> Response:
        return await move(
            request.path_params["id"], (TaskState.BLOCKED,), TaskState.IN_PROGRESS, None
        )

    async def events(request: Request) -> Response:
        task_id = request.path_params["id"]
        if await store.get_task(task_id) is None:
            return _error(404, f"task {task_id} not found")
        after = _int(request, "after", 0)
        if after == 0 and (last := request.headers.get("last-event-id", "")).isdigit():
            after = int(last)
        follow = request.query_params.get("follow") in ("1", "true")

        async def stream() -> AsyncIterator[str]:
            cursor = after
            while True:
                while batch := await store.audit_since(cursor, task_id):
                    for entry in batch:
                        yield _event(entry)
                    cursor = batch[-1].id
                if not follow:
                    break
                record = await store.get_task(task_id)
                if record is None or record.state in FINAL_STATES:
                    if record is not None and await store.audit_since(cursor, task_id, limit=1):
                        continue
                    break
                if await request.is_disconnected():
                    return
                yield ": ping\n\n"
                try:
                    await asyncio.wait_for(closing.wait(), POLL_SECONDS)
                    return
                except TimeoutError:
                    pass
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/health", health),
            Route("/config", config),
            Route("/tasks", create_task, methods=["POST"]),
            Route("/tasks", list_tasks, methods=["GET"]),
            Route("/tasks/{id}", show_task),
            Route("/tasks/{id}/cancel", cancel, methods=["POST"]),
            Route("/tasks/{id}/retry", retry, methods=["POST"]),
            Route("/tasks/{id}/events", events),
        ],
        middleware=[
            Middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS), www_redirect=False)
        ],
    )
    app.state.closing = closing
    return app


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield  # the daemon owns signals and tells us to stop through `stop`


async def serve(app: Starlette, host: str, port: int, stop: asyncio.Event) -> None:
    server = _Server(
        uvicorn.Config(app, host=host, port=port, log_config=None, timeout_graceful_shutdown=3)
    )

    async def watch() -> None:
        await stop.wait()
        app.state.closing.set()  # ends open event streams so shutdown doesn't wait on them
        server.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
