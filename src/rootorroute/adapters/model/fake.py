import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rootorroute.domain.model import Completion, Message, ToolCall, Usage
from rootorroute.domain.tools import ToolSpec

Responder = Callable[[Sequence[Message], Sequence[ToolSpec]], Completion]
ScriptItem = Completion | Responder

_ids = itertools.count(1)


class ScriptExhausted(AssertionError):
    pass


@dataclass(frozen=True, slots=True)
class Request:
    messages: tuple[Message, ...]
    tools: tuple[str, ...]


class FakeModelProvider:
    def __init__(self, script: Sequence[ScriptItem]) -> None:
        self._script = list(script)
        self.requests: list[Request] = []

    @property
    def remaining(self) -> int:
        return len(self._script) - len(self.requests)

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        n = len(self.requests)
        self.requests.append(Request(tuple(messages), tuple(t.name for t in tools)))
        if n >= len(self._script):
            raise ScriptExhausted(
                f"FakeModelProvider: call #{n + 1} but only {len(self._script)} scripted"
            )
        item = self._script[n]
        return item if isinstance(item, Completion) else item(messages, tools)


def say(text: str, usage: Usage | None = None) -> Completion:
    return Completion(Message("assistant", text), usage or Usage(), "end")


def call(name: str, /, **args: Any) -> Completion:
    tc = ToolCall(f"call_{next(_ids)}", name, dict(args))
    return Completion(Message("assistant", "", tool_calls=(tc,)), Usage(), "tool_calls")
