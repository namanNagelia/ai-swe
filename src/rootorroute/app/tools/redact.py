from collections.abc import Iterable
from typing import Any, cast

MASK = "[REDACTED]"
_MIN_SECRET_LEN = 8


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted(
            {s for s in secrets if len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True
        )

    def add(self, secret: str) -> None:
        if len(secret) >= _MIN_SECRET_LEN and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def text(self, value: str) -> str:
        for s in self._secrets:
            value = value.replace(s, MASK)
        return value

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._walk(payload))

    def _walk(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self._walk(v) for k, v in cast(dict[str, Any], value).items()}
        if isinstance(value, list | tuple):
            return [self._walk(v) for v in cast(list[Any], value)]
        return value
