from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rootorroute.core.errors import ToolNotAllowed
from rootorroute.core.state import Phase


class SideEffect(StrEnum):
    NONE = "none"
    LOCAL = "local"
    STATE = "state"
    EXTERNAL = "external"


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadFile(Args):
    path: str = Field(description="Path relative to the repo root.")
    start_line: int | None = Field(None, ge=1, description="First line to read (1-based).")
    end_line: int | None = Field(None, ge=1, description="Last line to read (inclusive).")


class ListFiles(Args):
    path: str = Field(".", description="Directory relative to the repo root.")
    max_depth: int = Field(3, ge=1, le=10)


class SearchCode(Args):
    pattern: str = Field(description="Regular expression (ripgrep syntax).")
    path: str = Field(".", description="Directory or file to search.")
    glob: str | None = Field(None, description="Only search files matching this glob.")


class WriteFile(Args):
    path: str
    content: str = Field(description="Full new file content.")


class EditFile(Args):
    path: str
    old: str = Field(description="Exact text to replace; must appear exactly once.")
    new: str


class Shell(Args):
    command: str = Field(description="Bash command, run in the repo root.")
    timeout_s: int = Field(120, ge=1, le=1800)


class CreateTicket(Args):
    title: str = Field(min_length=1, description="Short imperative title.")
    goal: str = Field(min_length=1, description="What exists after this ticket.")
    acceptance: list[str] = Field(min_length=1, description="Checkable outcomes.")


class FinishPlan(Args):
    summary: str = Field(
        min_length=1, description="One paragraph: the approach; used as PR title/intro."
    )


class FinishTicket(Args):
    summary: str = Field(min_length=1, description="What changed and why; used in the commit body.")


class AskHuman(Args):
    question: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    side_effect: SideEffect
    phases: frozenset[Phase]
    args: type[Args]
    description: str


BOTH = frozenset({Phase.PLAN, Phase.IMPLEMENT})
PLAN = frozenset({Phase.PLAN})
IMPLEMENT = frozenset({Phase.IMPLEMENT})

_SPECS = (
    ToolSpec("read_file", SideEffect.NONE, BOTH, ReadFile, "Read a file, optionally a line range."),
    ToolSpec("list_files", SideEffect.NONE, BOTH, ListFiles, "List files, respecting .gitignore."),
    ToolSpec(
        "search_code", SideEffect.NONE, BOTH, SearchCode, "Search file contents with a regex."
    ),
    ToolSpec("shell", SideEffect.LOCAL, BOTH, Shell, "Run a shell command in the repo."),
    ToolSpec("write_file", SideEffect.LOCAL, IMPLEMENT, WriteFile, "Create or overwrite a file."),
    ToolSpec(
        "edit_file", SideEffect.LOCAL, IMPLEMENT, EditFile, "Replace one exact string in a file."
    ),
    ToolSpec(
        "create_ticket", SideEffect.STATE, PLAN, CreateTicket, "Add the next ticket to the plan."
    ),
    ToolSpec("finish_plan", SideEffect.STATE, PLAN, FinishPlan, "End planning."),
    ToolSpec("finish_ticket", SideEffect.STATE, IMPLEMENT, FinishTicket, "Run checks and commit."),
    ToolSpec("ask_human", SideEffect.STATE, BOTH, AskHuman, "Stop and ask a human a question."),
)

TOOLS: Mapping[str, ToolSpec] = MappingProxyType({s.name: s for s in _SPECS})


def tools_for(phase: Phase) -> Sequence[ToolSpec]:
    return tuple(s for s in _SPECS if phase in s.phases)


def check_allowed(name: str, phase: Phase) -> ToolSpec:
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolNotAllowed(name, phase, "unknown_tool")
    if phase not in spec.phases:
        raise ToolNotAllowed(name, phase, "wrong_phase")
    return spec


def json_schema(spec: ToolSpec) -> dict[str, Any]:
    return spec.args.model_json_schema()
