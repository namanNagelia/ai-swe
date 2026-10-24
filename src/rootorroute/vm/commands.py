"""`rr vm` operations, written once against the Backend interface."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rootorroute.vm import bundle
from rootorroute.vm.backends import Backend, BackendError, VMSpec, VMState

BUNDLE_DIR = "/var/lib/rootorroute/image"
SERVICES = ["rr-egress", "tinyproxy", "rr-tools", "rr-supervisor"]

# Excluded from backups: rebuildable and large.
BACKUP_EXCLUDES = ["agent/cache", "agent/.cache", "agent/.local/share/docker"]


def say(msg: str) -> None:
    print(f"\033[1;32m==>\033[0m {msg}", file=sys.stderr)


@dataclass
class ProvisionOptions:
    egress: str = "open"
    allow_domains: list[str] = field(default_factory=lambda: [])
    with_browser: bool = True
    packages: list[str] = field(default_factory=lambda: [])
    home_device: str | None = None


def ensure_running(backend: Backend, spec: VMSpec) -> None:
    state = backend.state(spec.name)
    if state is VMState.MISSING:
        say(
            f"Launching {spec.name} with {backend.name} "
            f"({spec.cpus} CPU, {spec.memory_gib} GiB RAM, {spec.disk_gib} GiB disk)"
        )
        backend.launch(spec)
    elif state is not VMState.RUNNING:
        say(f"Starting {spec.name}")
        backend.start(spec.name)


def push_bundle(backend: Backend, vm: str) -> None:
    say("Pushing image bundle")
    data = bundle.build()
    script = f"rm -rf {BUNDLE_DIR} && mkdir -p {BUNDLE_DIR} && tar -xzf - -C {BUNDLE_DIR}"
    with tempfile.TemporaryFile() as f:
        f.write(data)
        f.seek(0)
        backend.exec(vm, ["sudo", "sh", "-c", script], stdin=f)


def provision(backend: Backend, vm: str, opts: ProvisionOptions) -> None:
    push_bundle(backend, vm)
    env = [f"RR_BUNDLE_REV={bundle.revision()}", f"RR_EGRESS_MODE={opts.egress}"]
    if not opts.with_browser:
        env.append("RR_WITH_BROWSER=0")
    if opts.allow_domains:
        env.append(f"RR_ALLOW_DOMAINS={','.join(opts.allow_domains)}")
    if opts.packages:
        env.append(f"RR_EXTRA_PACKAGES={','.join(opts.packages)}")
    if opts.home_device:
        env.append(f"RR_HOME_DEVICE={opts.home_device}")
    say("Running provisioner (first run takes a few minutes)")
    backend.exec(vm, ["sudo", "env", *env, f"{BUNDLE_DIR}/provision.sh"])


def up(backend: Backend, spec: VMSpec, opts: ProvisionOptions) -> None:
    ensure_running(backend, spec)
    provision(backend, spec.name, opts)
    say(f"Ready. Try: rr vm shell --name {spec.name} --as agent")


def status(backend: Backend, vm: str) -> int:
    state = backend.state(vm)
    print(f"vm:       {vm}\nbackend:  {backend.name}\nstate:    {state}")
    if state is not VMState.RUNNING:
        return 0 if state is not VMState.MISSING else 1

    result = backend.exec(
        vm, ["cat", "/etc/rootorroute/image-manifest.json"], capture=True, check=False
    )
    if result.returncode != 0:
        print("image:    not provisioned (run `rr vm provision`)")
        return 1
    manifest = json.loads(result.stdout)
    versions = ", ".join(f"{k} {v}" for k, v in manifest["versions"].items())
    print(f"image:    {manifest['bundle_rev']} built {manifest['built_at']} ({manifest['arch']})")
    print(f"tools:    {versions}")
    print(f"homedisk: {'persistent' if manifest['home_disk'] else 'boot disk'}")
    print(f"egress:   {manifest.get('egress', 'unknown')}")

    for svc in SERVICES:
        r = backend.exec(vm, ["systemctl", "is-active", svc], capture=True, check=False)
        print(f"{svc + ':':<15}{r.stdout.decode().strip() or 'unknown'}")
    return 0


def shell(backend: Backend, vm: str, user: str | None) -> int:
    return subprocess.call(backend.shell_argv(vm, user))


def _stop_agent(backend: Backend, vm: str) -> None:
    backend.exec(vm, ["sudo", "systemctl", "stop", "rr-supervisor", "rr-tools"], check=False)


def _start_agent(backend: Backend, vm: str) -> None:
    backend.exec(vm, ["sudo", "systemctl", "start", "rr-tools", "rr-supervisor"], check=False)


def backup(backend: Backend, vm: str, out: Path) -> None:
    """Snapshot /home/agent to a local tarball. The agent is paused while it runs."""
    say(f"Backing up /home/agent to {out}")
    excludes = [f"--exclude={e}" for e in BACKUP_EXCLUDES]
    _stop_agent(backend, vm)
    try:
        with out.open("wb") as f:
            backend.exec(
                vm, ["sudo", "tar", "-C", "/home", "-czf", "-", *excludes, "agent"], stdout=f
            )
    finally:
        _start_agent(backend, vm)


def restore(backend: Backend, vm: str, archive: Path) -> None:
    """Restore a backup into /home/agent. Owners are mapped by name, so uids may differ."""
    say(f"Restoring {archive} into /home/agent")
    _stop_agent(backend, vm)
    try:
        with archive.open("rb") as f:
            backend.exec(vm, ["sudo", "tar", "-C", "/home", "-xzpf", "-"], stdin=f)
    finally:
        _start_agent(backend, vm)


def rebuild(backend: Backend, spec: VMSpec, opts: ProvisionOptions, keep: Path) -> None:
    """Recreate the VM from a fresh image while keeping the agent's home directory."""
    if backend.state(spec.name) is VMState.MISSING:
        raise BackendError(f"{spec.name} doesn't exist; use `rr vm up`")
    backup(backend, spec.name, keep)
    say(f"Destroying {spec.name}")
    backend.delete(spec.name)
    up(backend, spec, opts)
    restore(backend, spec.name, keep)
    # Re-run so ownership and permissions match the new users.
    provision(backend, spec.name, opts)


def default_backup_path(vm: str) -> Path:
    return Path.cwd() / f"{vm}-home.tar.gz"


__all__ = [
    "ProvisionOptions",
    "backup",
    "default_backup_path",
    "ensure_running",
    "provision",
    "rebuild",
    "restore",
    "shell",
    "status",
    "up",
]
