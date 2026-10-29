from __future__ import annotations

import getpass
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

from rootorroute.config import load as load_config
from rootorroute.vm import bundle
from rootorroute.vm.backends import Backend, BackendError, VMState
from rootorroute.vm.commands import say

UNIT = "rr-agent"
RR_OPT = "/opt/rootorroute"
VENV = f"{RR_OPT}/venv"
PYTHON_DIR = f"{RR_OPT}/python"
UV = "/usr/local/bin/uv"
WHEEL_DIR = "/var/lib/rootorroute/wheels"
CONFIG = "/etc/rootorroute/config.toml"
SECRETS_DIR = "/home/agent/state/secrets"
SECRET_NAME = re.compile(r"[a-z0-9_]+")


def _project_root() -> Path:
    root = Path(__file__).resolve().parents[3]
    if not (root / "pyproject.toml").is_file():
        raise BackendError("rr vm deploy builds the agent from a source checkout; run it from one")
    return root


def _python_version() -> str:
    for line in (bundle.image_dir() / "manifest.env").read_text().splitlines():
        if line.startswith("PYTHON_VERSION="):
            return line.split("=", 1)[1].strip()
    raise BackendError("PYTHON_VERSION missing from manifest.env")


def _require_running(backend: Backend, vm: str) -> None:
    if backend.state(vm) is not VMState.RUNNING:
        raise BackendError(f"{vm} is not running; start it with `rr vm up`")


def _sudo_sh(backend: Backend, vm: str, script: str, *, stdin: IO[bytes] | None = None) -> None:
    backend.exec(vm, ["sudo", "sh", "-c", script], stdin=stdin)


def _install_from_stdin(
    backend: Backend,
    vm: str,
    src: IO[bytes],
    dest: str,
    *,
    owner: str,
    mode: str,
    dir_owner: str = "root:root",
    dir_mode: str = "0755",
) -> None:
    """Write stdin to `dest` atomically; the temp file is root-only until it has its final mode."""
    d = shlex.quote(str(Path(dest).parent))
    du, dg = dir_owner.split(":")
    script = (
        f"set -e; install -d -o {du} -g {dg} -m {dir_mode} {d}; "
        f'tmp=$(mktemp {shlex.quote(dest)}.XXXXXX); cat > "$tmp"; '
        f'chown {owner} "$tmp"; chmod {mode} "$tmp"; mv -f "$tmp" {shlex.quote(dest)}'
    )
    _sudo_sh(backend, vm, script, stdin=src)


def build_wheel(backend: Backend, out: Path) -> Path:
    say("Building the agent wheel")
    backend.run(["uv", "build", "--wheel", "--quiet", "--out-dir", str(out), str(_project_root())])
    wheels = sorted(out.glob("rootorroute-*.whl"))
    if len(wheels) != 1:
        raise BackendError(f"expected one wheel in {out}, found {len(wheels)}")
    return wheels[0]


def deploy(backend: Backend, vm: str, config: Path | None = None) -> None:
    _require_running(backend, vm)
    if config is not None:
        load_config(config)
    if backend.exec(
        vm, ["test", "-f", f"/etc/systemd/system/{UNIT}.service"], check=False
    ).returncode:
        raise BackendError(f"{UNIT}.service is not installed; run `rr vm provision` first")

    with tempfile.TemporaryDirectory() as tmp:
        wheel = build_wheel(backend, Path(tmp))
        remote_wheel = f"{WHEEL_DIR}/{wheel.name}"
        say(f"Copying {wheel.name} into {vm}")
        backend.exec(vm, ["sudo", "sh", "-c", f"rm -f {WHEEL_DIR}/*.whl"], check=False)
        with wheel.open("rb") as f:
            _install_from_stdin(backend, vm, f, remote_wheel, owner="root:root", mode="0644")

    py = _python_version()
    say(f"Installing into {VENV}")
    _sudo_sh(
        backend,
        vm,
        f"set -e; export UV_PYTHON_INSTALL_DIR={PYTHON_DIR}; "
        f"if [ ! -x {VENV}/bin/python ]; then "
        f"{UV} python install {py} --quiet; {UV} venv --quiet --python {py} {VENV}; fi",
    )
    backend.exec(
        vm,
        [
            "sudo",
            UV,
            "pip",
            "install",
            "--quiet",
            "--python",
            f"{VENV}/bin/python",
            "--reinstall-package",
            "rootorroute",
            remote_wheel,
        ],
    )
    backend.exec(vm, ["sudo", "chmod", "-R", "a+rX,go-w", VENV])

    if config is not None:
        say(f"Installing {config} as {CONFIG}")
        with config.open("rb") as f:
            _install_from_stdin(backend, vm, f, CONFIG, owner="root:agent", mode="0640")

    backend.exec(vm, ["sudo", "systemctl", "daemon-reload"])
    backend.exec(vm, ["sudo", "systemctl", "enable", "--quiet", UNIT])
    if backend.exec(vm, ["sudo", "test", "-f", CONFIG], check=False).returncode:
        say(f"No {CONFIG} yet; not starting {UNIT}. Rerun with --config PATH")
        return

    say(f"Restarting {UNIT}")
    backend.exec(vm, ["sudo", "systemctl", "restart", UNIT])
    time.sleep(3)
    backend.exec(vm, ["sudo", "systemctl", "status", UNIT, "--no-pager", "--lines=0"], check=False)
    backend.exec(
        vm, ["sudo", "journalctl", "-u", UNIT, "-n", "20", "--no-pager", "-o", "cat"], check=False
    )


def _check_name(name: str) -> None:
    if not SECRET_NAME.fullmatch(name):
        raise BackendError(f"invalid secret name {name!r}: use lowercase letters, digits and _")


def set_secret(backend: Backend, vm: str, name: str) -> None:
    _check_name(name)
    try:
        if sys.stdin.isatty():
            value = getpass.getpass(f"{name}: ").encode()
        else:
            value = sys.stdin.readline().strip().encode()
    except EOFError:
        raise BackendError("no input; nothing written") from None
    if not value:
        raise BackendError("empty value; nothing written")
    if len(value) > 60_000:
        raise BackendError("value too large")
    _require_running(backend, vm)
    r, w = os.pipe()
    os.write(w, value)
    os.close(w)
    with os.fdopen(r, "rb") as src:
        _install_from_stdin(
            backend,
            vm,
            src,
            f"{SECRETS_DIR}/{name}",
            owner="agent:agent",
            mode="0600",
            dir_owner="agent:agent",
            dir_mode="0700",
        )
    say(f"Set {name}; restart rr-agent to pick it up (rr vm deploy restarts it)")


def list_secrets(backend: Backend, vm: str) -> None:
    _require_running(backend, vm)
    out = backend.exec(
        vm,
        ["sudo", "find", SECRETS_DIR, "-maxdepth", "1", "-type", "f", "-printf", "%f\\n"],
        capture=True,
        check=False,
    ).stdout
    for name in sorted((out or b"").decode().split()):
        if SECRET_NAME.fullmatch(name):
            print(name)
