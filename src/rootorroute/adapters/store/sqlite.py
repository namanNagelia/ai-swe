import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from sqlalchemy import event, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from rootorroute.adapters.store.tables import (
    AuditRow,
    Base,
    ExternalStepRow,
    StepRow,
    TaskRow,
    TicketRow,
)
from rootorroute.domain.clock import Clock
from rootorroute.domain.errors import DomainError
from rootorroute.domain.state import BlockedReason, Phase, TaskState, transition
from rootorroute.domain.store import (
    AuditEntry,
    AuditKind,
    Json,
    NewTicket,
    Step,
    StepKind,
    TaskRecord,
)
from rootorroute.domain.task import SourceKind, Task, TaskSource
from rootorroute.domain.ticket import Ticket, TicketStatus

Redact = Callable[[Json], Json]


class NotFound(DomainError):
    def __init__(self, what: str, key: str) -> None:
        super().__init__("not_found", f"{what} {key} not found", what=what, key=key)


def _no_redact(payload: Json) -> Json:
    return payload


class _Tx:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session
        self.jsonl: list[str] = []


class SqliteStore:
    def __init__(
        self,
        path: str | Path,
        clock: Clock,
        redact: Redact = _no_redact,
        audit_jsonl: Path | None = None,
    ) -> None:
        self._clock = clock
        self._redact = redact
        self._jsonl = audit_jsonl
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        event.listen(self._engine.sync_engine, "connect", _on_connect)
        event.listen(self._engine.sync_engine, "begin", _on_begin)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def open(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def _tx(self) -> AsyncGenerator[_Tx]:
        async with self._sessions() as session, session.begin():
            tx = _Tx(session)
            yield tx
        if self._jsonl is not None and tx.jsonl:
            with self._jsonl.open("a") as f:
                f.writelines(tx.jsonl)

    async def _audit(self, tx: _Tx, task_id: str | None, kind: AuditKind, payload: Json) -> int:
        row = AuditRow(
            at=self._clock.now(), task_id=task_id, kind=kind, payload=self._redact(payload)
        )
        tx.s.add(row)
        await tx.s.flush()
        if self._jsonl is not None:
            entry = {
                "id": row.id,
                "at": row.at,
                "task_id": row.task_id,
                "kind": row.kind,
                "payload": row.payload,
            }
            tx.jsonl.append(json.dumps(entry, default=str) + "\n")
        return row.id

    async def _task_row(self, tx: _Tx, task_id: str) -> TaskRow:
        row = await tx.s.get(TaskRow, task_id)
        if row is None:
            raise NotFound("task", task_id)
        return row

    async def add_task(self, task: Task) -> TaskRecord:
        async with self._tx() as tx:
            row = TaskRow(
                id=task.id,
                prompt=task.prompt,
                repo=task.repo,
                base_branch=task.base_branch,
                source_kind=task.source.kind.value,
                source_reply_to=task.source.reply_to,
                created_at=task.created_at,
                state=TaskState.QUEUED.value,
                assumptions=[],
                resume_attempts=0,
                updated_at=self._clock.now(),
            )
            tx.s.add(row)
            await self._audit(tx, task.id, "state", {"to": "queued", "repo": task.repo})
            return _task_record(row)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        async with self._tx() as tx:
            row = await tx.s.get(TaskRow, task_id)
            return _task_record(row) if row else None

    async def list_tasks(
        self, states: Sequence[TaskState] | None = None, limit: int = 100
    ) -> list[TaskRecord]:
        q = select(TaskRow).order_by(TaskRow.created_at.desc()).limit(limit)
        if states:
            q = q.where(TaskRow.state.in_([s.value for s in states]))
        async with self._tx() as tx:
            return [_task_record(r) for r in (await tx.s.scalars(q)).all()]

    async def claim_next_queued(self) -> TaskRecord | None:
        q = (
            select(TaskRow)
            .where(TaskRow.state == TaskState.QUEUED.value)
            .order_by(TaskRow.created_at)
            .limit(1)
        )
        async with self._tx() as tx:
            row = (await tx.s.scalars(q)).first()
            if row is None:
                return None
            return await self._set_state(tx, row, TaskState.IN_PROGRESS, None)

    async def _set_state(
        self, tx: _Tx, row: TaskRow, state: TaskState, reason: BlockedReason | None
    ) -> TaskRecord:
        current = TaskState(row.state)
        transition(current, state)
        reason = reason if state is TaskState.BLOCKED else None
        row.state = state.value
        row.blocked_reason = reason.value if reason else None
        row.updated_at = self._clock.now()
        payload: Json = {"from": current.value, "to": state.value}
        if reason:
            payload["reason"] = reason.value
        await self._audit(tx, row.id, "state", payload)
        return _task_record(row)

    async def set_state(
        self, task_id: str, state: TaskState, reason: BlockedReason | None = None
    ) -> TaskRecord:
        async with self._tx() as tx:
            return await self._set_state(tx, await self._task_row(tx, task_id), state, reason)

    async def set_branch(self, task_id: str, branch: str) -> None:
        async with self._tx() as tx:
            row = await self._task_row(tx, task_id)
            row.branch, row.updated_at = branch, self._clock.now()

    async def set_pr_url(self, task_id: str, url: str) -> None:
        async with self._tx() as tx:
            row = await self._task_row(tx, task_id)
            row.pr_url, row.updated_at = url, self._clock.now()

    async def bump_resume_attempts(self, task_id: str) -> int:
        async with self._tx() as tx:
            row = await self._task_row(tx, task_id)
            row.resume_attempts += 1
            return row.resume_attempts

    async def add_ticket(self, task_id: str, ticket: NewTicket) -> Ticket:
        async with self._tx() as tx:
            await self._task_row(tx, task_id)
            last = await tx.s.scalar(
                select(func.coalesce(func.max(TicketRow.seq), 0)).where(
                    TicketRow.task_id == task_id
                )
            )
            row = TicketRow(
                task_id=task_id,
                seq=(last or 0) + 1,
                title=ticket.title,
                goal=ticket.goal,
                acceptance=list(ticket.acceptance),
                status=TicketStatus.TODO.value,
            )
            tx.s.add(row)
            await self._audit(tx, task_id, "plan", {"seq": row.seq, "title": row.title})
            return _ticket(row)

    async def finish_plan(self, task_id: str, summary: str, assumptions: Sequence[str]) -> None:
        async with self._tx() as tx:
            row = await self._task_row(tx, task_id)
            row.plan_summary, row.assumptions = summary, list(assumptions)
            row.updated_at = self._clock.now()
            await self._audit(
                tx, task_id, "plan", {"summary": summary, "assumptions": list(assumptions)}
            )

    async def tickets(self, task_id: str) -> list[Ticket]:
        q = select(TicketRow).where(TicketRow.task_id == task_id).order_by(TicketRow.seq)
        async with self._tx() as tx:
            return [_ticket(r) for r in (await tx.s.scalars(q)).all()]

    async def finish_ticket(self, task_id: str, seq: int, summary: str) -> Ticket:
        async with self._tx() as tx:
            row = await tx.s.get(TicketRow, (task_id, seq))
            if row is None:
                raise NotFound("ticket", f"{task_id}/{seq}")
            row.status, row.summary = TicketStatus.DONE.value, summary
            await self._audit(tx, task_id, "ticket", {"seq": seq, "status": "done"})
            return _ticket(row)

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
    ) -> Step:
        async with self._tx() as tx:
            task = await self._task_row(tx, task_id)
            last = await tx.s.scalar(
                select(func.coalesce(func.max(StepRow.n), 0)).where(StepRow.task_id == task_id)
            )
            row = StepRow(
                task_id=task_id,
                n=(last or 0) + 1,
                phase=phase.value,
                ticket_seq=ticket_seq,
                kind=kind,
                request=self._redact(request),
                response=self._redact(response),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                at=self._clock.now(),
            )
            tx.s.add(row)
            task.resume_attempts = 0
            await self._audit(
                tx,
                task_id,
                "model_call" if kind == "model" else "tool_call",
                {
                    "step": row.n,
                    "phase": phase.value,
                    "ticket": ticket_seq,
                    "request": request,
                    "response": response,
                    "tokens": [input_tokens, output_tokens],
                },
            )
            return _step(row)

    async def steps(
        self, task_id: str, phase: Phase | None = None, ticket_seq: int | None = None
    ) -> list[Step]:
        q = select(StepRow).where(StepRow.task_id == task_id).order_by(StepRow.n)
        if phase is not None:
            q = q.where(StepRow.phase == phase.value)
        if ticket_seq is not None:
            q = q.where(StepRow.ticket_seq == ticket_seq)
        async with self._tx() as tx:
            return [_step(r) for r in (await tx.s.scalars(q)).all()]

    async def once(self, task_id: str, key: str, fn: Callable[[], Awaitable[Json]]) -> Json:
        async with self._tx() as tx:
            done = await tx.s.get(ExternalStepRow, (task_id, key))
            if done is not None:
                return done.result
        result = await fn()
        async with self._tx() as tx:
            await tx.s.execute(
                insert(ExternalStepRow)
                .values(task_id=task_id, step_key=key, result=result, at=self._clock.now())
                .on_conflict_do_nothing()
            )
            await self._audit(tx, task_id, "external", {"key": key, "result": result})
            stored = await tx.s.get(ExternalStepRow, (task_id, key), populate_existing=True)
            assert stored is not None
            return stored.result

    async def audit(self, task_id: str | None, kind: AuditKind, payload: Json) -> int:
        async with self._tx() as tx:
            return await self._audit(tx, task_id, kind, payload)

    async def audit_since(
        self, after_id: int = 0, task_id: str | None = None, limit: int = 500
    ) -> list[AuditEntry]:
        q = select(AuditRow).where(AuditRow.id > after_id).order_by(AuditRow.id).limit(limit)
        if task_id is not None:
            q = q.where(AuditRow.task_id == task_id)
        async with self._tx() as tx:
            return [
                AuditEntry(r.id, r.at, r.task_id, cast(AuditKind, r.kind), r.payload)
                for r in (await tx.s.scalars(q)).all()
            ]


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()


def _on_begin(conn: Connection) -> None:
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def _task_record(row: TaskRow) -> TaskRecord:
    task = Task(
        id=row.id,
        prompt=row.prompt,
        repo=row.repo,
        source=TaskSource(SourceKind(row.source_kind), row.source_reply_to),
        created_at=row.created_at,
        base_branch=row.base_branch,
    )
    return TaskRecord(
        task=task,
        state=TaskState(row.state),
        blocked_reason=BlockedReason(row.blocked_reason) if row.blocked_reason else None,
        branch=row.branch,
        pr_url=row.pr_url,
        plan_summary=row.plan_summary,
        assumptions=tuple(row.assumptions),
        resume_attempts=row.resume_attempts,
        updated_at=row.updated_at,
    )


def _ticket(row: TicketRow) -> Ticket:
    return Ticket(
        task_id=row.task_id,
        seq=row.seq,
        title=row.title,
        goal=row.goal,
        acceptance=tuple(row.acceptance),
        status=TicketStatus(row.status),
        summary=row.summary,
    )


def _step(row: StepRow) -> Step:
    return Step(
        task_id=row.task_id,
        n=row.n,
        phase=Phase(row.phase),
        ticket_seq=row.ticket_seq,
        kind=cast(StepKind, row.kind),
        request=row.request,
        response=row.response,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        at=row.at,
    )
