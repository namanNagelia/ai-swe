from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DDL, JSON, ForeignKey, Index, String, event
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        return value.isoformat() if value is not None else None

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        return datetime.fromisoformat(value) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UTCDateTime,
        dict[str, Any]: JSON,
        list[str]: JSON,
    }


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("tasks_state", "state", "created_at"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    prompt: Mapped[str]
    repo: Mapped[str]
    base_branch: Mapped[str | None]
    source_kind: Mapped[str]
    source_reply_to: Mapped[str | None]
    created_at: Mapped[datetime]
    state: Mapped[str]
    blocked_reason: Mapped[str | None]
    branch: Mapped[str | None]
    pr_url: Mapped[str | None]
    plan_summary: Mapped[str | None]
    assumptions: Mapped[list[str]] = mapped_column(default=list)
    resume_attempts: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime]


class TicketRow(Base):
    __tablename__ = "tickets"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    goal: Mapped[str]
    acceptance: Mapped[list[str]]
    status: Mapped[str]
    summary: Mapped[str | None]


class StepRow(Base):
    __tablename__ = "steps"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    n: Mapped[int] = mapped_column(primary_key=True)
    phase: Mapped[str]
    ticket_seq: Mapped[int | None]
    kind: Mapped[str]
    request: Mapped[dict[str, Any]]
    response: Mapped[dict[str, Any]]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    at: Mapped[datetime]


class ExternalStepRow(Base):
    __tablename__ = "external_steps"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    step_key: Mapped[str] = mapped_column(primary_key=True)
    result: Mapped[dict[str, Any]]
    at: Mapped[datetime]


class AuditRow(Base):
    __tablename__ = "audit"
    __table_args__ = (Index("audit_task", "task_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime]
    task_id: Mapped[str | None]
    kind: Mapped[str]
    payload: Mapped[dict[str, Any]]


for _op in ("UPDATE", "DELETE"):
    event.listen(
        AuditRow.__table__,
        "after_create",
        DDL(
            f"CREATE TRIGGER audit_no_{_op.lower()} BEFORE {_op} ON audit "
            "BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END"
        ),
    )
