import json
import re
import uuid
from collections.abc import Sequence
from typing import Any, cast

from rootorroute.domain.model import Completion, Message, ModelProvider, ToolCall
from rootorroute.domain.tools import ToolSpec, json_schema

_FORMAT = '```json\n{"tool": "<name>", "args": {...}}\n```'
_PROTOCOL = (
    "To call a tool, reply with exactly one fenced block and nothing else that looks like one:\n"
    f"{_FORMAT}\n"
    "One tool call per reply; you will get its result in the next message. "
    "If you don't need a tool, reply normally without such a block."
)
_BLOCK = re.compile(r"```[ \t]*(json)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_TOOL_NAME = re.compile(r'"tool"\s*:\s*"([^"]+)"')


def tool_section(tools: Sequence[ToolSpec]) -> str:
    parts = ["# Tools", _PROTOCOL, "", "Available tools:"]
    for spec in tools:
        schema = json.dumps(json_schema(spec), separators=(",", ":"))
        parts.append(f"\n## {spec.name}\n{spec.description}\nArgs JSON schema: {schema}")
    return "\n".join(parts)


def _fence(tc: ToolCall) -> str:
    return "```json\n" + json.dumps({"tool": tc.name, "args": tc.args}) + "\n```"


def to_text_history(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> list[Message]:
    out: list[Message] = []
    names: dict[str, str] = {}
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                names[tc.id] = tc.name
            text = "\n\n".join([m.content.strip(), *(_fence(tc) for tc in m.tool_calls)])
            out.append(Message("assistant", text.strip()))
        elif m.role == "tool":
            call_id = m.tool_call_id or "?"
            name = names.get(call_id, "tool")
            out.append(Message.user(f"Result of {name} (call {call_id}):\n{m.content}"))
        else:
            out.append(m)
    if tools:
        section = tool_section(tools)
        if out and out[0].role == "system":
            out[0] = Message.system(f"{out[0].content}\n\n{section}")
        else:
            out.insert(0, Message.system(section))
    return out


def _is_call_block(tag: str | None, body: str) -> bool:
    return '"tool"' in body or (tag is not None and '"args"' in body)


def _parse_call(body: str) -> ToolCall:
    call_id = f"text_{uuid.uuid4().hex[:12]}"
    m = _TOOL_NAME.search(body)
    guessed = m.group(1) if m else "invalid"

    def bad(why: str) -> ToolCall:
        return ToolCall(call_id, guessed, {}, parse_error=f"{why}. Expected exactly:\n{_FORMAT}")

    try:
        data: Any = json.loads(body)
    except json.JSONDecodeError as e:
        return bad(f"Tool call block is not valid JSON ({e.msg} at line {e.lineno})")
    if not isinstance(data, dict):
        return bad("Tool call block must be a JSON object")
    obj = cast(dict[str, Any], data)
    name = obj.get("tool")
    if not isinstance(name, str) or not name:
        return bad('Tool call is missing a string "tool" field')
    args = obj.get("args", {})
    if not isinstance(args, dict):
        return ToolCall(call_id, name, {}, parse_error=f'"args" must be a JSON object. {_FORMAT}')
    return ToolCall(call_id, name, cast(dict[str, Any], args))


def parse_reply(completion: Completion) -> Completion:
    text = completion.message.content
    blocks = [m for m in _BLOCK.finditer(text) if _is_call_block(m.group(1), m.group(2))]
    if not blocks:
        stop = "length" if completion.stop == "length" else "end"
        return Completion(Message("assistant", text), completion.usage, stop)
    first = blocks[0]
    tc = _parse_call(first.group(2).strip())
    content = text
    for b in reversed(blocks):
        content = content[: b.start()] + content[b.end() :]
    content = content.strip()
    if len(blocks) > 1:
        note = f"[{len(blocks) - 1} extra tool call(s) ignored: one tool call per turn.]"
        content = f"{content}\n\n{note}".strip()
    return Completion(
        Message("assistant", content, tool_calls=(tc,)), completion.usage, "tool_calls"
    )


class TextToolProvider:
    def __init__(self, inner: ModelProvider) -> None:
        self._inner = inner

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        reply = await self._inner.complete(to_text_history(messages, tools), ())
        return parse_reply(reply)
