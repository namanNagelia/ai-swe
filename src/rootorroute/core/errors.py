from collections.abc import Mapping
from types import MappingProxyType


class DomainError(Exception):
    reason: str
    context: Mapping[str, str]

    def __init__(self, reason: str, message: str, **context: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.context = MappingProxyType(dict(context))

    def to_dict(self) -> dict[str, object]:
        return {
            "error": type(self).__name__,
            "reason": self.reason,
            "message": self.message,
            "context": dict(self.context),
        }


class InvalidTransition(DomainError):
    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(
            "invalid_transition",
            f"cannot move from {from_state} to {to_state}",
            from_state=str(from_state),
            to_state=str(to_state),
        )


class ToolNotAllowed(DomainError):
    """`reason` is e.g. "unknown_tool", "wrong_phase", "outside_worktree"."""

    def __init__(self, tool: str, phase: str, reason: str, message: str | None = None) -> None:
        super().__init__(
            reason,
            message or f"{tool} is not allowed in the {phase} phase ({reason})",
            tool=tool,
            phase=str(phase),
        )


class LimitExceeded(DomainError):
    def __init__(self, limits: list[str]) -> None:
        self.limits = tuple(limits)
        super().__init__(
            "limit_exceeded", f"limit exceeded: {', '.join(limits)}", limits=",".join(limits)
        )
