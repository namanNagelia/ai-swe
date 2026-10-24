import asyncio
import random
from collections.abc import Awaitable, Callable

from rootorroute.domain.model import ModelError

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


def is_retryable(status: int) -> bool:
    return status in RETRYABLE_STATUS or status >= 500


async def with_retries[T](
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run `call`, retrying ModelError(retryable=True) with jittered exponential backoff."""
    for attempt in range(attempts):
        try:
            return await call()
        except ModelError as e:
            if not e.retryable or attempt == attempts - 1:
                raise
            await sleep(min(max_delay, base_delay * 2**attempt) * (0.5 + random.random() / 2))
    raise AssertionError("unreachable")
