import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx

from rootorroute.adapters.model._retry import is_retryable, with_retries
from rootorroute.domain.model import Completion, Message, ModelError, Stop, ToolCall, Usage
from rootorroute.domain.tools import ToolSpec, json_schema

_BODY_LIMIT = 500


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 600.0,
        temperature: float | None = None,
        attempts: int = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/responses"
        self._model = model
        self._temperature = temperature
        self._attempts = attempts
        self._headers = {"Content-Type": "application/json", **(headers or {})}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        instructions = "\n\n".join(m.content for m in messages if m.role == "system")
        body: dict[str, Any] = {
            "model": self._model,
            "input": [item for m in messages if m.role != "system" for item in _to_items(m)],
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        if tools:
            body["tools"] = [_tool_to_wire(t) for t in tools]
        if self._temperature is not None:
            body["temperature"] = self._temperature
        data = await with_retries(lambda: self._post(body), attempts=self._attempts)
        return _parse(data)

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(
                self._url, json=body, headers=self._headers, timeout=self._timeout
            )
        except httpx.TimeoutException as e:
            raise ModelError(f"timeout calling {self._url}", retryable=True) from e
        except httpx.TransportError as e:
            raise ModelError(f"transport error calling {self._url}: {e}", retryable=True) from e
        if resp.status_code >= 400:
            raise ModelError(
                f"HTTP {resp.status_code} from {self._url}: {resp.text[:_BODY_LIMIT]}",
                retryable=is_retryable(resp.status_code),
                status=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError:
            raise ModelError(
                f"non-JSON response from {self._url}: {resp.text[:_BODY_LIMIT]}", retryable=True
            ) from None
        if not isinstance(data, dict):
            raise ModelError(f"unexpected response from {self._url}", retryable=False)
        return cast(dict[str, Any], data)


def _to_items(m: Message) -> list[dict[str, Any]]:
    if m.role == "tool":
        output: str | list[dict[str, Any]] = m.content
        if m.images:
            output = [{"type": "input_text", "text": m.content}] + [
                {"type": "input_image", "image_url": f"data:{i.media_type};base64,{i.data}"}
                for i in m.images
            ]
        return [{"type": "function_call_output", "call_id": m.tool_call_id or "", "output": output}]
    items: list[dict[str, Any]] = []
    if m.content or not m.tool_calls:
        items.append({"role": m.role, "content": m.content})
    for c in m.tool_calls:
        items.append(
            {
                "type": "function_call",
                "call_id": c.id,
                "name": c.name,
                "arguments": json.dumps(c.args),
            }
        )
    return items


def _tool_to_wire(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": json_schema(spec),
        "strict": False,
    }


def _parse(data: dict[str, Any]) -> Completion:
    output = data.get("output")
    if not isinstance(output, list):
        raise ModelError(f"response has no output: {str(data)[:_BODY_LIMIT]}", retryable=False)
    texts: list[str] = []
    calls: list[ToolCall] = []
    for raw in cast(list[Any], output):
        item = _obj(raw)
        if item.get("type") == "message":
            for part in cast(list[Any], item.get("content") or []):
                p = _obj(part)
                if p.get("type") == "output_text" and isinstance(p.get("text"), str):
                    texts.append(p["text"])
        elif item.get("type") == "function_call":
            calls.append(_parse_call(item))

    stop: Stop
    reason = _obj(data.get("incomplete_details")).get("reason")
    if calls:
        stop = "tool_calls"
    elif data.get("status") == "incomplete" and reason == "max_output_tokens":
        stop = "length"
    else:
        stop = "end"

    usage = _obj(data.get("usage"))
    return Completion(
        message=Message("assistant", "".join(texts), tool_calls=tuple(calls)),
        usage=Usage(_int(usage.get("input_tokens")), _int(usage.get("output_tokens"))),
        stop=stop,
    )


def _parse_call(call: dict[str, Any]) -> ToolCall:
    call_id = call.get("call_id") or call.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"call_{uuid.uuid4().hex[:24]}"
    name = call.get("name")
    if not isinstance(name, str) or not name:
        return ToolCall(call_id, "", parse_error="tool call has no function name")

    arguments = call.get("arguments")
    if isinstance(arguments, dict):
        return ToolCall(call_id, name, cast(dict[str, Any], arguments))
    if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
        return ToolCall(call_id, name, {})
    if not isinstance(arguments, str):
        return ToolCall(call_id, name, parse_error="arguments must be a JSON object")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as e:
        return ToolCall(call_id, name, parse_error=f"arguments are not valid JSON: {e}")
    if not isinstance(parsed, dict):
        return ToolCall(
            call_id,
            name,
            parse_error=f"arguments must be a JSON object, got {type(parsed).__name__}",
        )
    return ToolCall(call_id, name, cast(dict[str, Any], parsed))


def _obj(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0
