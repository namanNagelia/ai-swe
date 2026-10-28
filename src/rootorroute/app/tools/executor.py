from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from rootorroute.app.tools.context import ToolContext, ToolFailed
from rootorroute.app.tools.truncate import DEFAULT_MAX_CHARS, truncate
from rootorroute.domain.errors import DomainError
from rootorroute.domain.model import ToolCall
from rootorroute.domain.store import Store
from rootorroute.domain.tools import Args, check_allowed

Handler = Callable[[Any, ToolContext], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    content: str


class ToolExecutor:
    def __init__(
        self,
        store: Store,
        handlers: Mapping[str, Handler],
        redact: Callable[[str], str] = lambda s: s,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._store = store
        self._handlers = dict(handlers)
        self._redact = redact
        self._max_chars = max_chars

    def with_handlers(self, extra: Mapping[str, Handler]) -> "ToolExecutor":
        return ToolExecutor(self._store, {**self._handlers, **extra}, self._redact, self._max_chars)

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        result = await self._run(call, ctx)
        result = ToolResult(result.ok, truncate(self._redact(result.content), self._max_chars))
        await self._store.record_step(
            ctx.task_id,
            ctx.phase,
            ctx.ticket_seq,
            "tool",
            {"call_id": call.id, "tool": call.name, "args": call.args},
            {"ok": result.ok, "content": result.content},
        )
        return result

    async def _run(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        if call.parse_error:
            return ToolResult(False, f"could not parse your tool call: {call.parse_error}")
        try:
            spec = check_allowed(call.name, ctx.phase)
            handler = self._handlers.get(spec.name)
            if handler is None:
                return ToolResult(False, f"{spec.name} is not available right now")
            args: Args = spec.args.model_validate(call.args)
            return ToolResult(True, await handler(args, ctx))
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'args'}: {err['msg']}"
                for err in e.errors()
            )
            return ToolResult(False, f"invalid arguments for {call.name}: {problems}")
        except DomainError as e:
            return ToolResult(False, e.message)
        except ToolFailed as e:
            return ToolResult(False, str(e))
