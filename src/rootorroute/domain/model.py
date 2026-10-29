from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from rootorroute.domain.tools import ToolSpec

Role = Literal["system", "user", "assistant", "tool"]
Stop = Literal["end", "tool_calls", "length"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict[str, Any])
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class Image:
    media_type: str  # image/png, image/jpeg
    data: str  # base64
    width: int = 0
    height: int = 0


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    images: tuple[Image, ...] = ()  # only on role="tool" results

    @staticmethod
    def system(content: str) -> "Message":
        return Message("system", content)

    @staticmethod
    def user(content: str) -> "Message":
        return Message("user", content)

    @staticmethod
    def tool_result(call_id: str, content: str, images: tuple[Image, ...] = ()) -> "Message":
        return Message("tool", content, tool_call_id=call_id, images=images)


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True, slots=True)
class Completion:
    message: Message  # role="assistant"
    usage: Usage
    stop: Stop


class ModelError(Exception):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ModelProvider(Protocol):
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> Completion: ...
