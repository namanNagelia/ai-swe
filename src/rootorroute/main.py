import argparse
import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

from rootorroute.adapters.clock.system import SystemClock
from rootorroute.adapters.code_host.github import GitHubCodeHost
from rootorroute.adapters.model.anthropic import AnthropicProvider
from rootorroute.adapters.model.openai_compat import OpenAICompatProvider
from rootorroute.adapters.model.text_tools import TextToolProvider
from rootorroute.adapters.sandbox.local import LocalSandbox
from rootorroute.adapters.sandbox.rr_exec import RrExecSandbox
from rootorroute.adapters.secrets.env import EnvSecrets
from rootorroute.adapters.secrets.file import FileSecrets
from rootorroute.adapters.store.sqlite import SqliteStore
from rootorroute.adapters.workspace.git import GitWorkspace
from rootorroute.app.deps import Deps
from rootorroute.app.intake.api import create_app, serve
from rootorroute.app.intake.mentions import MentionPoller
from rootorroute.app.limits import Limits
from rootorroute.app.runner import Runner
from rootorroute.app.tools.executor import ToolExecutor
from rootorroute.app.tools.handlers import workspace_handlers
from rootorroute.app.tools.redact import Redactor
from rootorroute.config import Config, load
from rootorroute.domain.model import ModelProvider
from rootorroute.domain.sandbox import Sandbox
from rootorroute.domain.secrets import SecretMissing, Secrets

log = logging.getLogger("rootorroute")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "at": self.formatTime(record),
            "level": record.levelname,
            "msg": record.getMessage(),
            "task_id": getattr(record, "task_id", None),
            "step": getattr(record, "step", None),
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _secret(secrets: Secrets, name: str | None) -> str | None:
    if not name:
        return None
    try:
        return secrets.get(name)
    except SecretMissing:
        return None


def build_model(cfg: Config, api_key: str | None) -> ModelProvider:
    m = cfg.model
    model: ModelProvider
    if m.provider == "anthropic":
        if not api_key:
            raise SystemExit(f"anthropic needs the secret {m.api_key_secret!r}")
        model = AnthropicProvider(m.model, api_key, effort=m.effort)
    else:
        base = m.base_url or "https://api.openai.com/v1"
        model = OpenAICompatProvider(base_url=base, model=m.model, api_key=api_key)
    return TextToolProvider(model) if m.tool_mode == "text" else model


def credential_helper() -> str:
    path = shutil.which("rr-git-credential") or str(
        Path(sys.executable).with_name("rr-git-credential")
    )
    return "!" + shlex.quote(path)


async def run(cfg: Config) -> None:
    home = cfg.runtime.home
    for d in (cfg.state_dir, cfg.logs_dir, home / "repos", home / "worktrees"):
        d.mkdir(parents=True, exist_ok=True)

    secrets: Secrets = (
        EnvSecrets() if cfg.runtime.secrets == "env" else FileSecrets(cfg.secrets_dir)
    )
    api_key = _secret(secrets, cfg.model.api_key_secret)
    gh_token = _secret(secrets, cfg.github.token_secret)
    redactor = Redactor([s for s in (api_key, gh_token) if s])
    os.environ.setdefault("RR_GITHUB_TOKEN_FILE", str(cfg.secrets_dir / cfg.github.token_secret))

    clock = SystemClock()
    store = SqliteStore(cfg.state_dir / "agent.db", clock, redactor, cfg.logs_dir / "audit.jsonl")
    await store.open()
    sandbox: Sandbox = LocalSandbox() if cfg.runtime.sandbox == "local" else RrExecSandbox()
    workspace = GitWorkspace(
        home,
        sandbox,
        cfg.github.name,
        cfg.github.email,
        credential_helper=credential_helper() if gh_token else None,
        remote_url=lambda repo: cfg.github.git_url.format(repo=repo),
    )
    code_host = None
    if gh_token:
        token_name = cfg.github.token_secret
        code_host = GitHubCodeHost(
            lambda: secrets.get(token_name),
            cfg.github.bot_login,
            cfg.github.repos,
            api_url=cfg.github.api_url,
        )
    if code_host is None:
        log.warning("no GitHub token: tasks will run but can't push or open PRs")
    lim = cfg.limits
    deps = Deps(
        store=store,
        model=build_model(cfg, api_key),
        workspace=workspace,
        sandbox=sandbox,
        executor=ToolExecutor(store, workspace_handlers(sandbox), redactor.text),
        clock=clock,
        limits=Limits(
            lim.max_steps_per_ticket,
            lim.max_steps_per_task,
            lim.max_tokens_per_task,
            lim.max_hours_per_task,
        ),
        max_tickets=lim.max_tickets,
        check_failures_per_ticket=lim.check_failures_per_ticket,
        code_host=code_host,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    app = create_app(store, clock, cfg.github.repos)
    jobs = [
        Runner(deps, cfg.runtime.concurrency).run_forever(stop),
        serve(app, cfg.runtime.host, cfg.runtime.port, stop),
    ]
    if code_host is not None and cfg.github.mentions:
        poller = MentionPoller(
            code_host, store, clock, cfg.github.repos, cfg.github.poll_seconds, cfg.github.bot_login
        )
        jobs.append(poller.run_forever(stop))
    log.info("rr-agent up on http://%s:%d", cfg.runtime.host, cfg.runtime.port)
    try:
        await asyncio.gather(*jobs)
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rr-agent")
    parser.add_argument("--config", type=Path, help="config TOML (default $RR_CONFIG or /etc)")
    parser.add_argument("--check-config", action="store_true", help="validate config and exit")
    args = parser.parse_args(argv)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load(args.config)
    if args.check_config:
        print(cfg.model_dump_json(indent=2))
        return 0
    asyncio.run(run(cfg))
    return 0
