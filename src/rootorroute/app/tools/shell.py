from rootorroute.app.tools.context import ToolContext
from rootorroute.domain.sandbox import Sandbox
from rootorroute.domain.tools import Shell


class ShellTool:
    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    async def shell(self, args: Shell, ctx: ToolContext) -> str:
        r = await self._sandbox.run(["bash", "-c", args.command], ctx.worktree, args.timeout_s)
        head = f"exit code: {r.exit_code}" + (
            f" (timed out after {args.timeout_s}s)" if r.timed_out else ""
        )
        parts = [head]
        if r.stdout:
            parts.append(f"--- stdout ---\n{r.stdout.rstrip()}")
        if r.stderr:
            parts.append(f"--- stderr ---\n{r.stderr.rstrip()}")
        if r.truncated:
            parts.append("[output was capped by the sandbox]")
        return "\n".join(parts)
