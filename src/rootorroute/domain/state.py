from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from rootorroute.domain.errors import InvalidTransition


class TaskState(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


class BlockedReason(StrEnum):
    LIMIT = "limit"
    CHECKS_FAILING = "checks_failing"
    CANCELLED = "cancelled"
    ERROR = "error"


class Phase(StrEnum):
    PLAN = "plan"
    IMPLEMENT = "implement"


S = TaskState

TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = MappingProxyType(
    {
        S.QUEUED: frozenset({S.IN_PROGRESS, S.BLOCKED}),
        S.IN_PROGRESS: frozenset({S.DONE, S.BLOCKED}),
        S.BLOCKED: frozenset({S.IN_PROGRESS}),
        S.DONE: frozenset[TaskState](),
    }
)


def transition(current: TaskState, target: TaskState) -> TaskState:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(current, target)
    return target
