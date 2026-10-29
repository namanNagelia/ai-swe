import asyncio
import signal
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from rootorroute.adapters.sandbox._proc import run_captured, signal_group
from rootorroute.domain.sandbox import ExecResult

_SUDO = "/usr/bin/sudo"
_ENV = "/usr/bin/env"
_TIMEOUT = "/usr/bin/timeout"
_KILL_AFTER_S = 5
_OUTER_SLACK_S = 15.0
_TIMED_OUT = 124
_KILLED = 137
_PROXY = "http://127.0.0.1:8888"
_NO_PROXY = "localhost,127.0.0.1,::1"


def _managed_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    env: dict[str, str] = {}
    inside = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("# rootorroute-begin"):
            inside = True
        elif line.startswith("# rootorroute-end"):
            inside = False
        elif inside and line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"')
    return env


def _allowlist_egress(mode_file: Path) -> bool:
    try:
        return mode_file.read_text().strip() == "allowlist"
    except OSError:
        return False


class RrExecSandbox:
    def __init__(
        self,
        user: str = "rr-exec",
        home: Path = Path("/var/lib/rr-exec"),
        environment_file: Path = Path("/etc/environment"),
        egress_mode_file: Path = Path("/etc/rootorroute/egress/mode"),
        max_output_bytes: int = 1_000_000,
    ) -> None:
        self._user = user
        self._max_output_bytes = max_output_bytes
        env = _managed_env(environment_file)
        env |= {
            "PATH": ":".join(
                [
                    f"{home}/.cargo/bin",
                    f"{home}/go/bin",
                    f"{home}/.local/bin",
                    "/usr/local/go/bin",
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                ]
            ),
            "HOME": str(home),
            "USER": user,
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "CI": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
        if _allowlist_egress(egress_mode_file):
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                env[key] = _PROXY
            env["NO_PROXY"] = env["no_proxy"] = _NO_PROXY
        self._env = env

    def _command(self, argv: Sequence[str], cwd: Path, timeout_s: float) -> list[str]:
        return [
            _SUDO,
            "-n",
            "-u",
            self._user,
            "--",
            _ENV,
            "-i",
            "-C",
            str(cwd),
            *(f"{k}={v}" for k, v in self._env.items()),
            _TIMEOUT,
            f"--kill-after={_KILL_AFTER_S}s",
            f"{max(timeout_s, 0.001):.3f}s",
            *argv,
        ]

    async def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout_s: float,
        stdin: bytes | None = None,
        max_output_bytes: int | None = None,
    ) -> ExecResult:
        result, outer_fired = await run_captured(
            self._command(argv, cwd, timeout_s),
            cwd=cwd,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            stdin=stdin,
            deadline_s=timeout_s + _OUTER_SLACK_S,
            max_output_bytes=max_output_bytes or self._max_output_bytes,
            kill=_kill_sudo,
        )
        code = result.exit_code
        timed_out = (
            outer_fired
            or code == _TIMED_OUT
            or (code == _KILLED and result.duration_s >= timeout_s)
        )
        stderr = result.stderr
        if code == 1 and stderr.startswith("sudo:"):
            stderr = f"sandbox unavailable: cannot run as {self._user} via sudo -n\n{stderr}"
        return replace(result, timed_out=timed_out, stderr=stderr)


async def _kill_sudo(proc: asyncio.subprocess.Process) -> None:
    signal_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), _KILL_AFTER_S)
    except TimeoutError:
        signal_group(proc.pid, signal.SIGKILL)
