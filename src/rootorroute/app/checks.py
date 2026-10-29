import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rootorroute.domain.sandbox import Sandbox

CHECK_TIMEOUT_S = 1200.0


@dataclass(frozen=True, slots=True)
class CheckRun:
    passed: bool
    report: str


async def resolve_checks(sandbox: Sandbox, worktree: Path) -> list[str]:
    """rr.toml `checks = [...]` if the repo has one, otherwise a guess from its build files."""
    r = await sandbox.run(["cat", "--", "rr.toml"], worktree, 30)
    if r.exit_code == 0:
        try:
            checks = tomllib.loads(r.stdout).get("checks", [])
        except tomllib.TOMLDecodeError:
            checks = []
        if isinstance(checks, list):
            items = cast(list[object], checks)
            if all(isinstance(c, str) for c in items):
                return [str(c) for c in items]
    ls = await sandbox.run(["ls", "-a"], worktree, 30)
    files = set(ls.stdout.split())
    if "package.json" in files:
        return await _npm_checks(sandbox, worktree)
    if "go.mod" in files:
        return ["go test ./..."]
    if "Cargo.toml" in files:
        return ["cargo test --quiet"]
    if files & {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini"}:
        runner = "uv run pytest -q" if "uv.lock" in files else "python3 -m pytest -q"
        return [runner]
    return []


async def _npm_checks(sandbox: Sandbox, worktree: Path) -> list[str]:
    r = await sandbox.run(["cat", "--", "package.json"], worktree, 30)
    try:
        data: object = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    scripts = cast(dict[str, object], data).get("scripts") if isinstance(data, dict) else None
    test = cast(dict[str, object], scripts).get("test") if isinstance(scripts, dict) else None
    if not isinstance(test, str) or "no test specified" in test:
        return []
    return ["npm test --silent"]


async def run_checks(sandbox: Sandbox, worktree: Path, checks: list[str]) -> CheckRun:
    reports: list[str] = []
    passed = True
    for cmd in checks:
        r = await sandbox.run(["bash", "-c", cmd], worktree, CHECK_TIMEOUT_S)
        if r.exit_code == 127 or "No module named pytest" in r.stderr + r.stdout:
            reports.append(f"$ {cmd}\n(skipped: tool not available in this repo's environment)")
            continue
        if r.exit_code == 5 and "pytest" in cmd:
            reports.append(f"$ {cmd}\n(no tests collected)")
            continue
        ok = r.exit_code == 0 and not r.timed_out
        passed = passed and ok
        tail = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()[-6000:]
        status = (
            "passed" if ok else ("timed out" if r.timed_out else f"failed (exit {r.exit_code})")
        )
        reports.append(f"$ {cmd}\n{status}" + ("" if ok else f"\n{tail}"))
    return CheckRun(passed, "\n\n".join(reports) or "no checks configured")
