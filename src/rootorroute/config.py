import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PATH = Path("/etc/rootorroute/config.toml")


SECRET_NAME = r"^[a-z0-9_]+$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ModelConfig(_Strict):
    provider: Literal["openai_compat", "anthropic"]
    model: str
    base_url: str | None = None  # openai_compat: e.g. https://api.openai.com/v1
    api_key_secret: str | None = Field(default="model_api_key", pattern=SECRET_NAME)
    tool_mode: Literal["native", "text"] = "native"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None  # anthropic only


class GitHubConfig(_Strict):
    bot_login: str
    repos: list[str] = Field(min_length=1)
    token_secret: str = Field(default="github_token", pattern=SECRET_NAME)
    author_name: str | None = None
    author_email: str | None = None
    mentions: bool = True
    poll_seconds: int = Field(default=60, ge=15)
    api_url: str = "https://api.github.com"  # GitHub Enterprise: https://HOST/api/v3
    git_url: str = "https://github.com/{repo}.git"

    @property
    def name(self) -> str:
        return self.author_name or self.bot_login

    @property
    def email(self) -> str:
        return self.author_email or f"{self.bot_login}@users.noreply.github.com"


class LimitsConfig(_Strict):
    max_steps_per_ticket: int = 80
    max_steps_per_task: int = 500
    max_tokens_per_task: int = 6_000_000
    max_hours_per_task: float = 6
    max_tickets: int = 15
    check_failures_per_ticket: int = 3


class RuntimeConfig(_Strict):
    home: Path = Path("/home/agent")
    sandbox: Literal["rr_exec", "local"] = "rr_exec"
    secrets: Literal["file", "env"] = "file"
    host: str = "127.0.0.1"
    port: int = 8765
    concurrency: int = Field(default=1, ge=1)


class Config(_Strict):
    model: ModelConfig
    github: GitHubConfig
    limits: LimitsConfig = LimitsConfig()
    runtime: RuntimeConfig = RuntimeConfig()

    @property
    def state_dir(self) -> Path:
        return self.runtime.home / "state"

    @property
    def secrets_dir(self) -> Path:
        return self.state_dir / "secrets"

    @property
    def logs_dir(self) -> Path:
        return self.runtime.home / "logs"


SHORT_KEYS = {"repo", "model", "base_url", "github_user"}


def expand_short(data: dict[str, Any]) -> dict[str, Any]:
    """The two-line form (`repo = "owner/name"`, `model = "..."`) as a full config."""
    if not isinstance(data.get("model"), str):
        return data
    unknown = set(data) - SHORT_KEYS - {"limits", "runtime"}
    if unknown:
        raise ValueError(f"unknown keys in the short config form: {', '.join(sorted(unknown))}")
    model = str(data["model"])
    repo = str(data.get("repo", ""))
    anthropic = model.startswith("claude")
    model_cfg: dict[str, Any] = {
        "provider": "anthropic" if anthropic else "openai_compat",
        "model": model,
    }
    if not anthropic:
        model_cfg["base_url"] = data.get("base_url", "https://api.openai.com/v1")
    out: dict[str, Any] = {
        "model": model_cfg,
        "github": {
            "bot_login": data.get("github_user") or repo.split("/", 1)[0],
            "repos": [repo],
        },
    }
    for section in ("limits", "runtime"):
        if section in data:
            out[section] = data[section]
    return out


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("RR_CONFIG", DEFAULT_PATH))
    with path.open("rb") as f:
        return Config.model_validate(expand_short(tomllib.load(f)))
