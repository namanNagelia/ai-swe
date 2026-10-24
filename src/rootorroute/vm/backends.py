"""VM backends: thin, swappable wrappers around the tools that run the agent VM.

Every backend can launch/start/stop/delete a VM and, most importantly, run a
command inside it as the admin (sudo-capable) user with stdin/stdout piped.
Everything else (pushing the image bundle, backups) is built on `exec`.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Protocol


class VMState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"
    OTHER = "other"


@dataclass(frozen=True)
class VMSpec:
    name: str = "rr-agent"
    cpus: int = 4
    memory_gib: int = 8
    disk_gib: int = 60


class Runner(Protocol):
    """Executes host commands. Swapped for a fake in tests."""

    def __call__(
        self,
        argv: list[str],
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]: ...


def run(
    argv: list[str],
    *,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        stdin=stdin,
        stdout=subprocess.PIPE if capture else stdout,
        check=check,
    )


class BackendError(RuntimeError):
    pass


@dataclass
class Backend(ABC):
    run: Runner = field(default=run)

    name: str = ""
    binary: str = ""

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    @abstractmethod
    def state(self, vm: str) -> VMState: ...

    @abstractmethod
    def launch(self, spec: VMSpec) -> None: ...

    @abstractmethod
    def start(self, vm: str) -> None: ...

    @abstractmethod
    def stop(self, vm: str) -> None: ...

    @abstractmethod
    def delete(self, vm: str) -> None: ...

    @abstractmethod
    def exec_argv(self, vm: str, argv: list[str]) -> list[str]:
        """Host argv that runs `argv` inside the VM as the admin user."""

    @abstractmethod
    def shell_argv(self, vm: str, user: str | None) -> list[str]:
        """Host argv for an interactive login shell inside the VM."""

    def exec(
        self,
        vm: str,
        argv: list[str],
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return self.run(
            self.exec_argv(vm, argv), stdin=stdin, stdout=stdout, capture=capture, check=check
        )


@dataclass
class LimaBackend(Backend):
    """Lima (macOS, Linux). Uses Apple's Virtualization.framework on macOS."""

    name: str = "lima"
    binary: str = "limactl"
    template: Path | None = None

    def state(self, vm: str) -> VMState:
        out = self.run([self.binary, "list", "--json"], capture=True).stdout.decode()
        for line in out.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("name") == vm:
                status = str(entry.get("status", "")).lower()
                if status == "running":
                    return VMState.RUNNING
                if status == "stopped":
                    return VMState.STOPPED
                return VMState.OTHER
        return VMState.MISSING

    def render_template(self, spec: VMSpec) -> str:
        if self.template is None:
            raise BackendError("Lima template path not set")
        return (
            self.template.read_text()
            .replace("@CPUS@", str(spec.cpus))
            .replace("@MEMORY@", str(spec.memory_gib))
            .replace("@DISK@", str(spec.disk_gib))
        )

    def launch(self, spec: VMSpec) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{spec.name}.yaml"
            path.write_text(self.render_template(spec))
            self.run([self.binary, "create", "--tty=false", f"--name={spec.name}", str(path)])
        self.start(spec.name)

    def start(self, vm: str) -> None:
        self.run([self.binary, "start", "--tty=false", vm])

    def stop(self, vm: str) -> None:
        self.run([self.binary, "stop", vm])

    def delete(self, vm: str) -> None:
        self.run([self.binary, "delete", "--force", vm])

    def exec_argv(self, vm: str, argv: list[str]) -> list[str]:
        # --workdir avoids Lima trying to cd into the (unmounted) host cwd.
        return [self.binary, "shell", "--workdir", "/", vm, *argv]

    def shell_argv(self, vm: str, user: str | None) -> list[str]:
        inner = ["sudo", "-iu", user] if user else ["bash", "-l"]
        return self.exec_argv(vm, inner)


@dataclass
class MultipassBackend(Backend):
    """Multipass (macOS, Linux, Windows)."""

    name: str = "multipass"
    binary: str = "multipass"

    def state(self, vm: str) -> VMState:
        out = self.run([self.binary, "list", "--format", "json"], capture=True).stdout
        for entry in json.loads(out).get("list", []):
            if entry.get("name") == vm:
                status = str(entry.get("state", "")).lower()
                if status == "running":
                    return VMState.RUNNING
                if status == "stopped":
                    return VMState.STOPPED
                return VMState.OTHER
        return VMState.MISSING

    def launch(self, spec: VMSpec) -> None:
        self.run(
            [
                self.binary,
                "launch",
                "24.04",
                "--name",
                spec.name,
                "--cpus",
                str(spec.cpus),
                "--memory",
                f"{spec.memory_gib}G",
                "--disk",
                f"{spec.disk_gib}G",
            ]
        )

    def start(self, vm: str) -> None:
        self.run([self.binary, "start", vm])

    def stop(self, vm: str) -> None:
        self.run([self.binary, "stop", vm])

    def delete(self, vm: str) -> None:
        self.run([self.binary, "delete", "--purge", vm])

    def exec_argv(self, vm: str, argv: list[str]) -> list[str]:
        return [self.binary, "exec", vm, "--", *argv]

    def shell_argv(self, vm: str, user: str | None) -> list[str]:
        inner = ["sudo", "-iu", user] if user else ["bash", "-l"]
        return self.exec_argv(vm, inner)


@dataclass
class SSHBackend(Backend):
    """Any existing Ubuntu 24.04 machine over SSH: cloud VMs, UTM, bare metal, WSL.

    The machine's lifecycle is managed elsewhere (Terraform, a hypervisor UI);
    this backend only provisions, inspects and backs it up.
    """

    name: str = "ssh"
    binary: str = "ssh"
    target: str = ""
    port: int | None = None
    identity: str | None = None

    def _ssh(self, *, tty: bool = False) -> list[str]:
        argv = [self.binary, "-o", "BatchMode=yes"] if not tty else [self.binary, "-t"]
        if self.port:
            argv += ["-p", str(self.port)]
        if self.identity:
            argv += ["-i", self.identity]
        return [*argv, self.target]

    def state(self, vm: str) -> VMState:
        result = self.run([*self._ssh(), "true"], capture=True, check=False)
        return VMState.RUNNING if result.returncode == 0 else VMState.OTHER

    def _unmanaged(self, action: str) -> None:
        raise BackendError(
            f"the ssh backend doesn't {action} machines; manage {self.target} directly"
        )

    def launch(self, spec: VMSpec) -> None:
        self._unmanaged("launch")

    def start(self, vm: str) -> None:
        self._unmanaged("start")

    def stop(self, vm: str) -> None:
        self._unmanaged("stop")

    def delete(self, vm: str) -> None:
        self._unmanaged("delete")

    def exec_argv(self, vm: str, argv: list[str]) -> list[str]:
        # ssh joins remote args with spaces, so quote them ourselves.
        return [*self._ssh(), shlex.join(argv)]

    def shell_argv(self, vm: str, user: str | None) -> list[str]:
        inner = shlex.join(["sudo", "-iu", user]) if user else "bash -l"
        return [*self._ssh(tty=True), inner]


LOCAL_BACKENDS: dict[str, type[Backend]] = {
    "lima": LimaBackend,
    "multipass": MultipassBackend,
}
