"""`rr`: the Root or Route command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from rootorroute.vm import BackendError, SSHBackend, deploy, resolve_backend
from rootorroute.vm import commands as vm
from rootorroute.vm.backends import Backend, LimaBackend, VMSpec

API_PORT = 8765


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

    p = vm_sub.add_parser("deploy", help="build the agent, install it in the VM and restart it")
    _add_vm_target(p)
    p.add_argument(
        "--config", type=Path, help="config.toml to install (see infra/config.example.toml)"
    )

    p = vm_sub.add_parser("secrets", help="manage the agent's secrets in the VM")
    secrets_sub = p.add_subparsers(dest="secrets_cmd", required=True)
    sp = secrets_sub.add_parser("set", help="set a secret (value read from a hidden prompt)")
    _add_vm_target(sp)
    sp.add_argument("secret", metavar="NAME", help="e.g. github_token, model_api_key")
    sp = secrets_sub.add_parser("list", help="list secret names")
    _add_vm_target(sp)

    p = vm_sub.add_parser("tunnel", help="forward the web inbox to http://localhost:PORT")
    _add_vm_target(p)
    p.add_argument("--port", type=int, default=API_PORT, help=f"local port (default: {API_PORT})")

    p = sub.add_parser("task", help="give the agent a task")
    _add_vm_target(p)
    p.add_argument("prompt", nargs="?", help="what to do (or use --file, or pipe it on stdin)")
    p.add_argument("--repo", required=True, metavar="OWNER/NAME")
    p.add_argument("--base", metavar="BRANCH", help="base branch (default: the repo's default)")
    p.add_argument("--file", metavar="PATH", help="read the prompt from PATH ('-' for stdin)")

    p = sub.add_parser("tasks", help="list tasks")
    _add_vm_target(p)
    p.add_argument("--state", choices=["queued", "in_progress", "done", "blocked"])
    p.add_argument("--limit", type=int, default=30)

    for name, help_ in [
        ("show", "show a task, its plan and tickets"),
        ("cancel", "cancel a queued or running task"),
        ("retry", "resume a blocked task"),
    ]:
        p = sub.add_parser(name, help=help_)
        _add_vm_target(p)
        p.add_argument("id")

    p = sub.add_parser("logs", help="print a task's events")
    _add_vm_target(p)
    p.add_argument("id")
    p.add_argument("-f", "--follow", action="store_true", help="stream until the task finishes")

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
        case "deploy":
            deploy.deploy(backend, name, args.config)
        case "secrets" if args.secrets_cmd == "set":
            deploy.set_secret(backend, name, args.secret)
        case "secrets":
            deploy.list_secrets(backend, name)
        case _:
            raise AssertionError(args.cmd)
    return 0


class ApiError(RuntimeError):
    pass


class Api:
    """The rr-agent HTTP API: direct when RR_API is set, else curl inside the VM."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.direct = os.environ.get("RR_API", "").rstrip("/")
        self.args = args

    def _vm_argv(self, curl_args: list[str], path: str) -> list[str]:
        url = f"http://127.0.0.1:{API_PORT}{path}"
        return _backend(self.args).exec_argv(
            self.args.name, ["curl", "-sS", "--fail-with-body", *curl_args, url]
        )

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        if self.direct:
            import httpx

            try:
                r = httpx.request(method, self.direct + path, content=data, timeout=30)
            except httpx.HTTPError as e:
                raise ApiError(f"cannot reach {self.direct}: {e}") from e
            return _decode(r.status_code < 400, r.text)
        curl = ["-X", method]
        if data is not None:
            curl += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        r = subprocess.run(self._vm_argv(curl, path), input=data or b"", capture_output=True)
        if r.returncode not in (0, 22):
            raise ApiError(_unreachable(r.stderr.decode()))
        return _decode(r.returncode == 0, r.stdout.decode())

    def stream(self, path: str) -> Iterator[str]:
        if self.direct:
            import httpx

            try:
                with httpx.stream("GET", self.direct + path, timeout=None) as r:
                    if r.status_code >= 400:
                        _decode(False, r.read().decode())
                    yield from r.iter_lines()
            except httpx.HTTPError as e:
                raise ApiError(f"cannot reach {self.direct}: {e}") from e
            return
        argv = self._vm_argv(["-N"], path)
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                yield raw.decode().rstrip("\r\n")
            proc.wait()
            if proc.returncode not in (0, 22):
                assert proc.stderr is not None
                raise ApiError(_unreachable(proc.stderr.read().decode()))


def _unreachable(stderr: str) -> str:
    return f"cannot reach rr-agent in the VM (is it running? try `rr vm status`): {stderr.strip()}"


def _decode(ok: bool, text: str) -> Any:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = {"error": text.strip()}
    if not ok:
        error = cast(dict[str, Any], body).get("error", text) if isinstance(body, dict) else text
        raise ApiError(str(error))
    return body


def _sse(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    event, data = "message", list[str]()
    for line in lines:
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", list[str]()
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)


def _local_time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")


def _summary(kind: str, p: dict[str, Any]) -> str:
    match kind:
        case "state":
            if "detail" in p:
                return f"why: {str(p['detail']).splitlines()[0][:200]}"
            reason = f" ({p['reason']})" if p.get("reason") else ""
            return f"{p.get('from', 'new')} -> {p.get('to')}{reason}"
        case "plan":
            if "title" in p:
                return f"ticket {p.get('seq')}: {p['title']}"
            return f"plan finished, {len(p.get('assumptions', []))} assumption(s)"
        case "ticket":
            return f"ticket {p.get('seq')} {p.get('status')}"
        case "tool_call":
            req, res = p.get("request", {}), p.get("response", {})
            ok = "ok" if res.get("ok", True) else "err"
            return f"{req.get('tool', '?')} {ok}"
        case "model_call":
            ticket = f" #{p['ticket']}" if p.get("ticket") else ""
            tokens = "/".join(str(t) for t in p.get("tokens", []))
            return f"step {p.get('step')} {p.get('phase')}{ticket} tokens {tokens}"
        case "external":
            return str(p.get("key"))
        case _:
            return json.dumps(p)[:160]


def _print_task(t: dict[str, Any]) -> None:
    state = t["state"] + (f" ({t['blocked_reason']})" if t.get("blocked_reason") else "")
    print(
        f"{t['id']}  {state}  {t['repo']}" + (f" <- {t['base_branch']}" if t["base_branch"] else "")
    )
    for key in ("branch", "pr_url"):
        if t.get(key):
            print(f"{key + ':':<8}{t[key]}")
    print(f"\n{t['prompt'].strip()}")
    if t.get("plan_summary"):
        print(f"\nPlan\n{t['plan_summary'].strip()}")
    if t.get("tickets"):
        print("\nTickets")
        for k in t["tickets"]:
            print(f"  [{'x' if k['status'] == 'done' else ' '}] {k['seq']}. {k['title']}")
    if t.get("assumptions"):
        print("\nAssumptions")
        for a in t["assumptions"]:
            print(f"  - {a}")


def _prompt(args: argparse.Namespace) -> str:
    if args.file == "-" or (args.file is None and args.prompt is None):
        if args.file is None and sys.stdin.isatty():
            raise ApiError("give a prompt, --file PATH, or pipe the prompt on stdin")
        return sys.stdin.read()
    if args.file is not None:
        return Path(args.file).read_text()
    return args.prompt


def _logs(api: Api, task_id: str, follow: bool) -> int:
    path = f"/tasks/{task_id}/events" + ("?follow=1" if follow else "")
    for event, data in _sse(api.stream(path)):
        if event == "end":
            break
        e = json.loads(data)
        print(f"{_local_time(e['at'])}  {e['kind']:<10}  {_summary(e['kind'], e['payload'])}")
    if not follow:
        return 0
    t = api.call("GET", f"/tasks/{task_id}")
    if t["state"] == "done":
        print(f"done: {t.get('pr_url') or '(no PR)'}")
        return 0
    print(f"{t['state']}" + (f": {t['blocked_reason']}" if t.get("blocked_reason") else ""))
    return 1


def run_task_cmd(args: argparse.Namespace) -> int:
    api = Api(args)
    match args.group:
        case "task":
            body: dict[str, Any] = {"prompt": _prompt(args), "repo": args.repo, "source": "cli"}
            if args.base:
                body["base_branch"] = args.base
            t = api.call("POST", "/tasks", body)
            print(f"{t['id']}  queued  {t['repo']}\nfollow it: rr logs {t['id']} -f")
        case "tasks":
            query = f"?limit={args.limit}" + (f"&state={args.state}" if args.state else "")
            for t in api.call("GET", "/tasks" + query):
                prompt = " ".join(t["prompt"].split())
                prompt = prompt[:57] + "..." if len(prompt) > 60 else prompt
                pr = f"  {t['pr_url']}" if t.get("pr_url") else ""
                print(f"{t['id']}  {t['state']:<11}  {t['repo']:<24}  {prompt}{pr}")
        case "show":
            _print_task(api.call("GET", f"/tasks/{args.id}"))
        case "cancel" | "retry":
            t = api.call("POST", f"/tasks/{args.id}/{args.group}")
            print(f"{t['id']}  {t['state']}")
        case "logs":
            return _logs(api, args.id, args.follow)
        case _:
            raise AssertionError(args.group)
    return 0


def tunnel(backend: Backend, vm: str, port: int) -> int:
    forward = ["-N", "-o", "ExitOnForwardFailure=yes", "-L", f"{port}:127.0.0.1:{API_PORT}"]
    if isinstance(backend, SSHBackend):
        ssh = ["ssh"] + (["-p", str(backend.port)] if backend.port else [])
        ssh += ["-i", backend.identity] if backend.identity else []
        argv = [*ssh, *forward, backend.target]
    elif isinstance(backend, LimaBackend):
        out = backend.run([backend.binary, "list", vm, "--format", "{{.Dir}}"], capture=True)
        config = Path(out.stdout.decode().strip()) / "ssh.config"
        if not config.is_file():
            raise BackendError(f"no ssh config for Lima VM {vm} at {config}; is it running?")
        argv = ["ssh", "-F", str(config), *forward, f"lima-{vm}"]
    else:
        print(
            f"Inbox: http://localhost:{port}  (relaying through `{backend.binary}`; Ctrl-C to stop)"
        )
        return asyncio.run(_relay(backend, vm, port))
    print(f"Inbox: http://localhost:{port}  (Ctrl-C to stop)")
    return subprocess.call(argv)


async def _relay(backend: Backend, vm: str, port: int) -> int:
    """For backends without SSH access (Multipass): one `nc` inside the VM per connection."""
    argv = backend.exec_argv(vm, ["nc", "127.0.0.1", str(API_PORT)])

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        assert proc.stdin is not None and proc.stdout is not None
        sink, source = proc.stdin, proc.stdout

        async def up() -> None:
            while chunk := await reader.read(65536):
                sink.write(chunk)
                await sink.drain()

        async def down() -> None:
            while chunk := await source.read(65536):
                writer.write(chunk)
                await writer.drain()

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    async with server:
        await server.serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.group == "vm" and args.cmd == "tunnel":
            return tunnel(_backend(args), args.name, args.port)
        if args.group == "vm":
            return run_vm(args)
        return run_task_cmd(args)
    except ApiError as e:
        print(f"rr: {e}", file=sys.stderr)
        return 1
    except BackendError as e:
        print(f"rr: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
