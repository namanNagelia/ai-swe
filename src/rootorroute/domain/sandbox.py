from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    truncated: bool = False


class Sandbox(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Run argv as the sandbox user in cwd, with a fixed env and no credentials.

        Never raises for a failing or timed-out command; that's in the result.
        """
        ...
