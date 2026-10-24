from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any, Literal

from anthropic import (
    AnthropicError,
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
)
from anthropic.types import (
    ContentBlock,
    MessageParam,
    TextBlockParam,
    ToolParam,
)
from anthropic.types import (
    Message as SdkMessage,
)

from rootorroute.adapters.model._retry import is_retryable, with_retries
from rootorroute.domain.model import Completion, Message, ModelError, Stop, ToolCall, Usage
from rootorroute.domain.tools import ToolSpec, json_schema

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_EPHEMERAL: dict[str, Any] = {"type": "ephemeral"}
_RAW_CACHE_SIZE = 256
_MAX_TOKENS = 64000


class AnthropicProvider:
    def __init__(
        self,
        model: str,
        api_key: str,
        timeout: float = 600.0,
        base_url: str | None = None,
        effort: Effort | None = None,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._client = client or AsyncAnthropic(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0
        )
        self._raw: OrderedDict[tuple[Any, ...], list[ContentBlock]] = OrderedDict()

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        system, conv = self._convert(messages)
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MAX_TOKENS,
            "messages": conv,
            "cache_control": _EPHEMERAL,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = convert_tools(tools)
        if self._effort:
            params["output_config"] = {"effort": self._effort}

        async def call() -> SdkMessage:
            try:
                async with self._client.messages.stream(**params) as stream:
                    return await stream.get_final_message()
            except APIStatusError as e:
                raise ModelError(
                    f"anthropic: HTTP {e.status_code} {type(e).__name__}: {_short(e.message)}",
                    retryable=is_retryable(e.status_code),
                    status=e.status_code,
                ) from None
            except APIConnectionError as e:
                raise ModelError(f"anthropic: {type(e).__name__}", retryable=True) from None
            except AnthropicError as e:
                raise ModelError(f"anthropic: {type(e).__name__}", retryable=False) from None

        response = await with_retries(call)
        completion = parse_response(response)
        self._remember(completion.message, list(response.content))
        return completion

    def _convert(
        self, messages: Sequence[Message]
    ) -> tuple[list[TextBlockParam], list[MessageParam]]:
        system: list[TextBlockParam] = [
            {"type": "text", "text": m.content} for m in messages if m.role == "system"
        ]
        if system:
            system[-1]["cache_control"] = {"type": "ephemeral"}
        conv = convert_messages(messages, raw=self._raw.get)
        return system, conv

    def _remember(self, message: Message, content: list[ContentBlock]) -> None:
        if not any(b.type in ("thinking", "redacted_thinking") for b in content):
            return
        key = _key(message)
        self._raw[key] = content
        self._raw.move_to_end(key)
        while len(self._raw) > _RAW_CACHE_SIZE:
            self._raw.popitem(last=False)


def convert_tools(tools: Sequence[ToolSpec]) -> list[ToolParam]:
    out: list[ToolParam] = [
        {"name": t.name, "description": t.description, "input_schema": json_schema(t)}
        for t in tools
    ]
    if out:
        out[-1]["cache_control"] = {"type": "ephemeral"}
    return out


def convert_messages(
    messages: Sequence[Message],
    raw: Callable[[tuple[Any, ...]], list[ContentBlock] | None] | None = None,
) -> list[MessageParam]:
    out: list[MessageParam] = []
    for m in messages:
        if m.role == "system":
            continue
        role: Literal["user", "assistant"]
        blocks: list[Any]
        if m.role == "user":
            role, blocks = "user", [{"type": "text", "text": m.content}]
        elif m.role == "tool":
            role = "user"
            blocks = [
                {"type": "tool_result", "tool_use_id": m.tool_call_id or "", "content": m.content}
            ]
        else:
            role = "assistant"
            replay = raw(_key(m)) if raw else None
            blocks = list(replay) if replay else _assistant_blocks(m)
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            assert isinstance(prev, list)
            prev.extend(blocks)
        elif role == "user":
            out.append({"role": "user", "content": blocks})
        else:
            out.append({"role": "assistant", "content": blocks})
    return out


def _assistant_blocks(m: Message) -> list[Any]:
    blocks: list[Any] = []
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for c in m.tool_calls:
        blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
    if not blocks:
        blocks.append({"type": "text", "text": "(no content)"})
    return blocks


def parse_response(response: SdkMessage) -> Completion:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(block.id, block.name, dict(block.input)))
    stop: Stop
    match response.stop_reason:
        case "tool_use":
            stop = "tool_calls"
        case "max_tokens":
            stop = "length"
        case _:
            stop = "end"
    u = response.usage
    usage = Usage(
        input_tokens=u.input_tokens
        + (u.cache_read_input_tokens or 0)
        + (u.cache_creation_input_tokens or 0),
        output_tokens=u.output_tokens,
    )
    message = Message("assistant", "".join(texts), tool_calls=tuple(calls))
    return Completion(message=message, usage=usage, stop=stop)


def _key(m: Message) -> tuple[Any, ...]:
    if m.tool_calls:
        return ("calls", tuple(c.id for c in m.tool_calls))
    return ("text", m.content)


def _short(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "..."
