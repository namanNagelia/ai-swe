import asyncio
import contextlib
import os
import signal
from collections.abc import Sequence
from pathlib import Path

from rootorroute.adapters.sandbox._proc import run_captured, signal_group
from rootorroute.domain.sandbox import ExecResult


class LocalSandbox:
    def __init__(self, max_output_bytes: int = 1_000_000, kill_grace_s: float = 5.0) -> None:
        self._max_output_bytes = max_output_bytes
        self._kill_grace_s = kill_grace_s
        self._env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "CI": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    async def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> ExecResult:
        result, _ = await run_captured(
            list(argv),
            cwd=cwd,
            env=self._env,
            stdin=stdin,
            deadline_s=timeout_s,
            max_output_bytes=self._max_output_bytes,
            kill=self._kill,
        )
        return result

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        signal_group(proc.pid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), self._kill_grace_s)
        signal_group(proc.pid, signal.SIGKILL)
