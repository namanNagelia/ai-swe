from dataclasses import dataclass

from rootorroute.app.limits import Limits
from rootorroute.app.tools.executor import ToolExecutor
from rootorroute.domain.clock import Clock
from rootorroute.domain.code_host import CodeHost
from rootorroute.domain.model import ModelProvider
from rootorroute.domain.sandbox import Sandbox
from rootorroute.domain.store import Store
from rootorroute.domain.workspace import Workspace


@dataclass(frozen=True, slots=True)
class Deps:
    store: Store
    model: ModelProvider
    workspace: Workspace
    sandbox: Sandbox
    executor: ToolExecutor
    clock: Clock
    limits: Limits
    max_tickets: int
    check_failures_per_ticket: int
    code_host: CodeHost | None
