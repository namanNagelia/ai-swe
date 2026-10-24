"""`rr`: the Root or Route command line."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rootorroute.vm import BackendError, SSHBackend, resolve_backend
from rootorroute.vm import commands as vm
from rootorroute.vm.backends import Backend, VMSpec


def _add_vm_target(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", default="rr-agent", help="VM name (default: rr-agent)")
    p.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "lima", "multipass"],
        help="local VM backend (default: auto)",
    )
    p.add_argument("--ssh", metavar="USER@HOST", help="target an existing Ubuntu machine over SSH")
    p.add_argument("--ssh-port", type=int)
    p.add_argument("--ssh-identity", metavar="KEY")


def _add_sizing(p: argparse.ArgumentParser) -> None:
    host_cpus = os.cpu_count() or 4
    p.add_argument("--cpus", type=int, default=min(4, host_cpus), help="default: min(4, host)")
    p.add_argument("--memory", type=int, default=8, metavar="GIB", help="default: 8")
    p.add_argument("--disk", type=int, default=60, metavar="GIB", help="default: 60")


def _add_provision_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--egress",
        default="open",
        choices=["open", "allowlist"],
        help="open: full internet except private networks (default); "
        "allowlist: only allowlisted hosts via a filtering proxy",
    )
    p.add_argument(
        "--allow-domain",
        action="append",
        default=[],
        metavar="HOST",
        help="extra host for --egress allowlist, e.g. your Jira site (repeatable)",
    )
    p.add_argument(
        "--package",
        action="append",
        default=[],
        metavar="NAME",
        help="extra apt package on top of infra/image/packages.txt (repeatable)",
    )
    p.add_argument("--no-browser", action="store_true", help="skip headless Chromium")
    p.add_argument("--home-device", metavar="DEV", help="block device for a persistent /home/agent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rr", description="Root or Route")
    sub = parser.add_subparsers(dest="group", required=True)

    vm_p = sub.add_parser("vm", help="manage the agent's VM")
    vm_sub = vm_p.add_subparsers(dest="cmd", required=True)

    p = vm_sub.add_parser("up", help="create or start the VM and provision it")
    _add_vm_target(p)
    _add_sizing(p)
    _add_provision_opts(p)

    p = vm_sub.add_parser("provision", help="(re)apply the image to an existing machine")
    _add_vm_target(p)
    _add_provision_opts(p)

    p = vm_sub.add_parser("status", help="show VM state, image manifest and services")
    _add_vm_target(p)

    p = vm_sub.add_parser("shell", help="open a shell in the VM")
    _add_vm_target(p)
    p.add_argument("--as", dest="user", metavar="USER", help="e.g. agent (default: admin user)")

    for name, help_ in [
        ("start", "start the VM"),
        ("stop", "stop the VM"),
        ("destroy", "delete the VM and its disk"),
    ]:
        p = vm_sub.add_parser(name, help=help_)
        _add_vm_target(p)

    p = vm_sub.add_parser("backup", help="save /home/agent to a local tarball")
    _add_vm_target(p)
    p.add_argument("--out", type=Path)

    p = vm_sub.add_parser("restore", help="restore /home/agent from a tarball")
    _add_vm_target(p)
    p.add_argument("archive", type=Path)

    p = vm_sub.add_parser("rebuild", help="recreate the VM from a fresh image, keeping /home/agent")
    _add_vm_target(p)
    _add_sizing(p)
    _add_provision_opts(p)
    p.add_argument("--keep", type=Path, help="where to keep the home backup")

    return parser


def _backend(args: argparse.Namespace) -> Backend:
    if args.ssh:
        return SSHBackend(target=args.ssh, port=args.ssh_port, identity=args.ssh_identity)
    return resolve_backend(args.backend, args.name)


def _spec(args: argparse.Namespace) -> VMSpec:
    return VMSpec(name=args.name, cpus=args.cpus, memory_gib=args.memory, disk_gib=args.disk)


def _opts(args: argparse.Namespace) -> vm.ProvisionOptions:
    return vm.ProvisionOptions(
        egress=args.egress,
        allow_domains=args.allow_domain,
        with_browser=not args.no_browser,
        packages=args.package,
        home_device=args.home_device,
    )


def run_vm(args: argparse.Namespace) -> int:
    backend = _backend(args)
    name: str = args.name
    match args.cmd:
        case "up":
            if isinstance(backend, SSHBackend):
                vm.provision(backend, name, _opts(args))
            else:
                vm.up(backend, _spec(args), _opts(args))
        case "provision":
            vm.provision(backend, name, _opts(args))
        case "status":
            return vm.status(backend, name)
        case "shell":
            return vm.shell(backend, name, args.user)
        case "start":
            backend.start(name)
        case "stop":
            backend.stop(name)
        case "destroy":
            backend.delete(name)
        case "backup":
            vm.backup(backend, name, args.out or vm.default_backup_path(name))
        case "restore":
            vm.restore(backend, name, args.archive)
        case "rebuild":
            vm.rebuild(backend, _spec(args), _opts(args), args.keep or vm.default_backup_path(name))
        case _:
            raise AssertionError(args.cmd)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.group == "vm":
            return run_vm(args)
    except BackendError as e:
        print(f"rr: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
