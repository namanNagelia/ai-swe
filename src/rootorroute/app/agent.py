from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rootorroute.app.tools.context import ToolContext
from rootorroute.app.tools.executor import ToolExecutor
from rootorroute.domain.model import Message, ModelProvider, ToolCall
from rootorroute.domain.store import Step, Store
from rootorroute.domain.tools import ToolSpec

Guard = Callable[[], Awaitable[None]]

NUDGE = (
    "Continue by calling a tool. There is no human to answer questions: decide, and when this "
    "work is complete call {stop}."
)


@dataclass(frozen=True, slots=True)
class AgentResult:
    tool: str
    args: dict[str, Any]


async def run_agent(
    model: ModelProvider,
    store: Store,
    executor: ToolExecutor,
    ctx: ToolContext,
    messages: list[Message],
    tools: Sequence[ToolSpec],
    stop_tools: frozenset[str],
    guard: Guard,
) -> AgentResult:
    """Model <-> tools until a stop tool succeeds. guard() raises to end the run early."""
    stop_hint = " or ".join(sorted(stop_tools))
    while True:
        await guard()
        completion = await model.complete(messages, tools)
        reply = completion.message
        await store.record_step(
            ctx.task_id,
            ctx.phase,
            ctx.ticket_seq,
            "model",
            {"messages": len(messages)},
            {
                "content": reply.content,
                "tool_calls": [_call_json(c) for c in reply.tool_calls],
                "stop": completion.stop,
            },
            completion.usage.input_tokens,
            completion.usage.output_tokens,
        )
        messages.append(reply)
        if not reply.tool_calls:
            messages.append(Message.user(NUDGE.format(stop=stop_hint)))
            continue
        for call in reply.tool_calls:
            result = await executor.execute(call, ctx)
            messages.append(Message.tool_result(call.id, result.content, result.images))
            if result.ok and call.name in stop_tools:
                return AgentResult(call.name, call.args)


def replay(steps: Sequence[Step], stop_tools: frozenset[str]) -> list[Message]:
    """Rebuild the conversation tail from saved steps so a resumed run continues where it was."""
    out: list[Message] = []
    pending: set[str] = set()
    for step in steps:
        if step.kind == "model":
            calls = tuple(
                ToolCall(c["id"], c["name"], c.get("args") or {}, c.get("parse_error"))
                for c in step.response.get("tool_calls", [])
            )
            out.append(Message("assistant", step.response.get("content", ""), calls))
            pending = {c.id for c in calls}
            if not calls:
                out.append(Message.user(NUDGE.format(stop=" or ".join(sorted(stop_tools)))))
        else:
            call_id = step.request.get("call_id", "")
            if call_id in pending:
                content = step.response.get("content", "")
                if step.response.get("images"):
                    content += "\n[screenshot was shown here; take it again to look]"
                out.append(Message.tool_result(call_id, content))
                pending.discard(call_id)
    for call_id in pending:
        out.append(Message.tool_result(call_id, "[interrupted before this tool ran; call again]"))
    return out


def _call_json(c: ToolCall) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "args": c.args, "parse_error": c.parse_error}
