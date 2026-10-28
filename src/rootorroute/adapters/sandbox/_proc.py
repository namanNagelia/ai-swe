import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from rootorroute.domain.sandbox import ExecResult

_CHUNK = 64 * 1024
_DRAIN_AFTER_KILL_S = 5.0

Killer = Callable[[asyncio.subprocess.Process], Awaitable[None]]


@dataclass(slots=True)
class _Capture:
    data: bytearray
    truncated: bool = False


async def _read_capped(stream: asyncio.StreamReader, cap: int, out: _Capture) -> None:
    while chunk := await stream.read(_CHUNK):
        room = cap - len(out.data)
        if room > 0:
            out.data += chunk[:room]
        if len(chunk) > room:
            out.truncated = True


async def _feed(writer: asyncio.StreamWriter, data: bytes) -> None:
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        writer.write(data)
        await writer.drain()
    writer.close()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await writer.wait_closed()


def signal_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _exit_code(returncode: int | None) -> int:
    if returncode is None:
        return -1
    return 128 - returncode if returncode < 0 else returncode


async def run_captured(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin: bytes | None,
    deadline_s: float,
    max_output_bytes: int,
    kill: Killer,
) -> tuple[ExecResult, bool]:
    """Returns the result and whether the deadline fired (the caller decides timed_out)."""
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        code = 127 if isinstance(e, FileNotFoundError) else 126
        result = ExecResult(
            exit_code=code,
            stdout="",
            stderr=f"failed to start {cmd[0]!r}: {e}",
            timed_out=False,
            duration_s=time.monotonic() - start,
        )
        return result, False

    assert proc.stdout is not None and proc.stderr is not None
    out, err = _Capture(bytearray()), _Capture(bytearray())
    feeder = None
    if stdin is not None and proc.stdin is not None:
        feeder = asyncio.create_task(_feed(proc.stdin, stdin))
    tasks = {
        asyncio.create_task(_read_capped(proc.stdout, max_output_bytes, out)),
        asyncio.create_task(_read_capped(proc.stderr, max_output_bytes, err)),
        asyncio.create_task(proc.wait()),
    }
    fired = False
    try:
        _, pending = await asyncio.wait(tasks, timeout=deadline_s)
        if pending:
            fired = True
            await kill(proc)
            _, pending = await asyncio.wait(pending, timeout=_DRAIN_AFTER_KILL_S)
            for t in pending:
                t.cancel()
    finally:
        if feeder is not None:
            feeder.cancel()
        for t in tasks:
            t.cancel()

    result = ExecResult(
        exit_code=_exit_code(proc.returncode),
        stdout=out.data.decode("utf-8", errors="replace"),
        stderr=err.data.decode("utf-8", errors="replace"),
        timed_out=fired,
        duration_s=time.monotonic() - start,
        truncated=out.truncated or err.truncated,
    )
    return result, fired
