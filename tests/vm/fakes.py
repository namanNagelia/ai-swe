from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import IO


@dataclass
class FakeRunner:
    """Records host commands and returns canned stdout keyed by argv prefix."""

    responses: dict[tuple[str, ...], bytes] = field(default_factory=lambda: {})
    calls: list[list[str]] = field(default_factory=lambda: [])
    stdin_seen: list[bytes] = field(default_factory=lambda: [])

    def __call__(
        self,
        argv: list[str],
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        if stdin is not None:
            self.stdin_seen.append(stdin.read())
        out = b""
        for prefix, response in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                out = response
        if stdout is not None:
            stdout.write(out)
        return subprocess.CompletedProcess(argv, 0, stdout=out if capture else None)
