from pathlib import PurePosixPath

from rootorroute.app.tools.context import ToolContext, ToolFailed
from rootorroute.domain.errors import ToolNotAllowed
from rootorroute.domain.sandbox import ExecResult, Sandbox
from rootorroute.domain.tools import EditFile, ListFiles, ReadFile, SearchCode, WriteFile

DEFAULT_READ_LINES = 2000
_IO_TIMEOUT_S = 60.0


class FileTools:
    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    async def read_file(self, args: ReadFile, ctx: ToolContext) -> str:
        rel = resolve(ctx, args.path, "read_file")
        text = await self._read(ctx, rel)
        if "\x00" in text[:8192]:
            raise ToolFailed(f"{rel} looks like a binary file; not shown")
        lines = text.splitlines()
        start = (args.start_line or 1) - 1
        end = args.end_line or min(len(lines), start + DEFAULT_READ_LINES)
        if start >= len(lines) and lines:
            raise ToolFailed(
                f"{rel} has {len(lines)} lines; start_line {start + 1} is past the end"
            )
        body = "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(lines[start:end], start + 1))
        if end < len(lines):
            body += f"\n[{len(lines) - end} more lines; read with start_line={end + 1}]"
        return body or f"{rel} is empty"

    async def list_files(self, args: ListFiles, ctx: ToolContext) -> str:
        rel = resolve(ctx, args.path, "list_files")
        argv = ["rg", "--files", "--hidden", "-g", "!.git", "--max-depth", str(args.max_depth)]
        result = await self._run(ctx, [*argv, "--", rel])
        if result.exit_code not in (0, 1):
            raise ToolFailed(_failure("list_files", result))
        files = sorted(result.stdout.splitlines())
        return "\n".join(files) if files else f"no files under {rel}"

    async def search_code(self, args: SearchCode, ctx: ToolContext) -> str:
        rel = resolve(ctx, args.path, "search_code")
        argv = ["rg", "--line-number", "--no-heading", "--color", "never", "--hidden"]
        argv += ["-g", "!.git", "--max-columns", "300", "--max-columns-preview"]
        if args.glob:
            argv += ["-g", args.glob]
        result = await self._run(ctx, [*argv, "-e", args.pattern, "--", rel])
        if result.exit_code == 1:
            return "no matches"
        if result.exit_code != 0:
            raise ToolFailed(_failure("search_code", result))
        return result.stdout.rstrip("\n")

    async def write_file(self, args: WriteFile, ctx: ToolContext) -> str:
        rel = resolve(ctx, args.path, "write_file", write=True)
        await self._write(ctx, rel, args.content)
        return f"wrote {rel} ({len(args.content.splitlines())} lines)"

    async def edit_file(self, args: EditFile, ctx: ToolContext) -> str:
        rel = resolve(ctx, args.path, "edit_file", write=True)
        if not args.old:
            raise ToolFailed("old must be non-empty; use write_file to create a file")
        text = await self._read(ctx, rel)
        count = text.count(args.old)
        if count == 0:
            hint = "check whitespace and indentation, or read_file the region first"
            raise ToolFailed(f"old text not found in {rel}; {hint}")
        if count > 1:
            raise ToolFailed(
                f"old text appears {count} times in {rel}; include more surrounding lines"
                " so it matches exactly once"
            )
        await self._write(ctx, rel, text.replace(args.old, args.new, 1))
        line = text[: text.index(args.old)].count("\n") + 1
        return f"edited {rel} at line {line}"

    async def _read(self, ctx: ToolContext, rel: str) -> str:
        result = await self._run(ctx, ["cat", "--", rel])
        if result.exit_code != 0:
            raise ToolFailed(_failure("read", result))
        if result.truncated:
            raise ToolFailed(f"{rel} is too large to read whole; use start_line/end_line")
        return result.stdout

    async def _write(self, ctx: ToolContext, rel: str, content: str) -> None:
        script = 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
        result = await self._run(ctx, ["sh", "-c", script, "sh", rel], content.encode())
        if result.exit_code != 0:
            raise ToolFailed(_failure("write", result))

    async def _run(
        self, ctx: ToolContext, argv: list[str], stdin: bytes | None = None
    ) -> ExecResult:
        return await self._sandbox.run(argv, ctx.worktree, _IO_TIMEOUT_S, stdin)


def resolve(ctx: ToolContext, path: str, tool: str, *, write: bool = False) -> str:
    """Map a model-supplied path to a clean path relative to the worktree, or refuse."""
    root = ctx.worktree.resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolNotAllowed(tool, ctx.phase, "outside_worktree", f"{path} is outside the repo")
    rel = PurePosixPath(candidate.relative_to(root).as_posix())
    if write and rel.parts[:1] == (".git",):
        raise ToolNotAllowed(tool, ctx.phase, "git_dir", ".git is read-only; commits are automatic")
    return str(rel) if str(rel) != "." else "."


def _failure(what: str, result: ExecResult) -> str:
    if result.timed_out:
        return f"{what} timed out"
    detail = (result.stderr or result.stdout).strip()[-1000:]
    return f"{what} failed (exit {result.exit_code}): {detail}"
