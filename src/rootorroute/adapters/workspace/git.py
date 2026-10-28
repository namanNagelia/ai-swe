import asyncio
import os
import re
import shutil
import stat
from collections.abc import Callable, Sequence
from pathlib import Path

from rootorroute.domain.errors import DomainError
from rootorroute.domain.sandbox import Sandbox
from rootorroute.domain.task import Task
from rootorroute.domain.workspace import Worktree, branch_name

_STDERR_TAIL = 2000
_BASE_REF = "refs/rr/base"
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_HARDENING = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


class WorkspaceError(DomainError):
    pass


def _default_remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _fail(shown: str, code: int, stderr: str) -> WorkspaceError:
    return WorkspaceError(
        "git_failed",
        f"{shown} exited {code}: {stderr[-_STDERR_TAIL:]}",
        command=shown,
        returncode=str(code),
    )


class GitWorkspace:
    def __init__(
        self,
        root: Path,
        sandbox: Sandbox,
        author_name: str,
        author_email: str,
        credential_helper: str | None = None,
        remote_url: Callable[[str], str] = _default_remote_url,
        timeout_s: float = 600,
    ) -> None:
        self._root = root
        self._sandbox = sandbox
        self._author_name = author_name
        self._author_email = author_email
        self._credential_helper = credential_helper
        self._remote_url = remote_url
        self._timeout_s = timeout_s
        self._locks: dict[str, asyncio.Lock] = {}

    async def prepare(self, task: Task) -> Worktree:
        mirror = self._mirror_path(task.repo)
        async with self._locks.setdefault(task.repo, asyncio.Lock()):
            await self._ensure_clone(task.repo, mirror)
            await self._git(
                mirror, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"
            )
            await self._git(mirror, "fetch", "--prune", "origin")
            default = await self._default_branch(mirror)
            base = task.base_branch or default
            branch = branch_name(task)
            await self._check_branch(mirror, branch, base, default)
            base_ref = f"refs/remotes/origin/{base}"
            base_sha = await self._git(mirror, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
            self._task_file(task.id).write_text(task.repo)

        wt = self._root / "worktrees" / task.id
        if wt.exists():
            head = await self._sb(wt, "rev-parse", "--abbrev-ref", "HEAD")
            if head != branch:
                raise WorkspaceError(
                    "worktree_mismatch",
                    f"{wt} is on {head!r}, expected {branch!r}",
                    task_id=task.id,
                    branch=branch,
                )
            started = await self._sb(wt, "rev-parse", "--verify", f"{_BASE_REF}^{{commit}}")
            if not _SHA.match(started):
                raise WorkspaceError("bad_base_ref", f"{_BASE_REF} in {wt} is not a sha")
            return Worktree(task.id, wt, branch, base, started)

        bundle = self._shared_bundles() / f"{task.id}-base.bundle"
        bundle.unlink(missing_ok=True)
        await self._git(mirror, "bundle", "create", "--quiet", str(bundle), base_ref)
        bundle.chmod(0o640)
        await self._sb(wt.parent, "init", "--quiet", str(wt))
        await self._sb(wt, "fetch", "--quiet", str(bundle), f"+{base_ref}:{_BASE_REF}")
        fetched = await self._sb(wt, "rev-parse", "--verify", f"{_BASE_REF}^{{commit}}")
        if fetched != base_sha:
            raise WorkspaceError("base_mismatch", f"bundle gave {fetched}, expected {base_sha}")
        await self._sb(wt, "checkout", "--quiet", "-b", branch, base_sha)
        bundle.unlink(missing_ok=True)
        return Worktree(task.id, wt, branch, base, base_sha)

    async def commit(self, worktree: Worktree, message: str) -> str | None:
        wt = worktree.path
        await self._sb(wt, "add", "-A")
        code, _ = await self._sb_status(wt, "diff", "--cached", "--quiet", "--no-ext-diff")
        if code == 0:
            return None
        await self._sb(
            wt,
            "-c",
            f"user.name={self._author_name}",
            "-c",
            f"user.email={self._author_email}",
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-F",
            "-",
            stdin=message.encode(),
        )
        return await self._sb(wt, "rev-parse", "HEAD")

    async def changed_files(self, worktree: Worktree) -> list[str]:
        wt = worktree.path
        diff = await self._sb(wt, "diff", "--no-ext-diff", "--name-only", "-z", worktree.base_sha)
        untracked = await self._sb(wt, "ls-files", "--others", "--exclude-standard", "-z")
        return sorted({n for n in (diff + "\0" + untracked).split("\0") if n})

    async def export_branch(self, worktree: Worktree) -> str:
        branch = worktree.branch
        if not branch.startswith("rr/") or branch == worktree.base_branch:
            raise WorkspaceError(
                "forbidden_branch", f"refusing to export {branch!r}", branch=branch
            )
        ref = f"refs/heads/{branch}"
        shared = self._shared_bundles() / f"{worktree.task_id}-out.bundle"
        shared.unlink(missing_ok=True)
        await self._sb(worktree.path, "bundle", "create", "--quiet", str(shared), ref)

        mirror = await self._mirror_for(worktree)
        private = self._private_bundles() / f"{worktree.task_id}-out.bundle"
        self._copy_untrusted(shared, private)
        shared.unlink(missing_ok=True)
        try:
            await self._git(mirror, "bundle", "verify", "--quiet", str(private))
            await self._git(
                mirror,
                "-c",
                "fetch.fsckObjects=true",
                "fetch",
                "--quiet",
                "--no-tags",
                str(private),
                f"+{ref}:{ref}",
            )
        finally:
            private.unlink(missing_ok=True)
        return await self._git(mirror, "rev-parse", "--verify", f"{ref}^{{commit}}")

    async def push(self, worktree: Worktree, repo: str) -> None:
        branch = worktree.branch
        if not branch.startswith("rr/") or branch == worktree.base_branch:
            raise WorkspaceError("forbidden_branch", f"refusing to push {branch!r}", branch=branch)
        mirror = self._mirror_path(repo)
        ref = f"refs/heads/{branch}"
        await self._git(mirror, "push", "--quiet", self._remote_url(repo), f"{ref}:{ref}")

    async def cleanup(self, worktree: Worktree) -> None:
        if worktree.path.exists():
            parent = worktree.path.parent
            code, err = await self._sb_raw(parent, ["rm", "-rf", "--", str(worktree.path)])
            if code != 0:
                raise _fail(f"rm -rf {worktree.path}", code, err)
        for kind in ("base", "out"):
            (self._shared_bundles() / f"{worktree.task_id}-{kind}.bundle").unlink(missing_ok=True)
            (self._private_bundles() / f"{worktree.task_id}-{kind}.bundle").unlink(missing_ok=True)
        self._task_file(worktree.task_id).unlink(missing_ok=True)

    def _mirror_path(self, repo: str) -> Path:
        owner, name = repo.split("/", 1)
        return self._root / "repos" / owner / f"{name}.git"

    def _task_file(self, task_id: str) -> Path:
        d = self._root / "repos" / ".tasks"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d / task_id

    async def _mirror_for(self, worktree: Worktree) -> Path:
        try:
            repo = self._task_file(worktree.task_id).read_text().strip()
        except FileNotFoundError:
            raise WorkspaceError("no_mirror", f"no repo recorded for {worktree.task_id}") from None
        return self._mirror_path(repo)

    def _shared_bundles(self) -> Path:
        d = self._root / "worktrees" / ".bundles"
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o2770)
        return d

    def _private_bundles(self) -> Path:
        d = self._root / "repos" / ".bundles"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d

    def _copy_untrusted(self, src: Path, dst: Path) -> None:
        dst.unlink(missing_ok=True)
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fin:
            if not stat.S_ISREG(os.fstat(fin.fileno()).st_mode):
                raise WorkspaceError("bad_bundle", f"{src} is not a regular file")
            out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, "wb") as fout:
                shutil.copyfileobj(fin, fout)

    async def _ensure_clone(self, repo: str, mirror: Path) -> None:
        if (mirror / "HEAD").is_file():
            return
        mirror.parent.mkdir(parents=True, exist_ok=True)
        tmp = mirror.with_name(f"{mirror.name}.partial")
        shutil.rmtree(tmp, ignore_errors=True)
        await self._git(
            mirror.parent, "clone", "--bare", "--quiet", "--", self._remote_url(repo), str(tmp)
        )
        tmp.rename(mirror)

    async def _default_branch(self, mirror: Path) -> str:
        await self._git(mirror, "remote", "set-head", "origin", "--auto")
        ref = await self._git(mirror, "symbolic-ref", "refs/remotes/origin/HEAD")
        prefix = "refs/remotes/origin/"
        if not ref.startswith(prefix):
            raise WorkspaceError("no_default_branch", f"unexpected origin HEAD {ref!r}")
        return ref.removeprefix(prefix)

    async def _check_branch(self, mirror: Path, branch: str, base: str, default: str) -> None:
        if not branch.startswith("rr/") or branch in (base, default):
            raise WorkspaceError(
                "forbidden_branch",
                f"refusing to work on branch {branch!r} (base {base!r}, default {default!r})",
                branch=branch,
            )
        if not await self._git_ok(mirror, "check-ref-format", "--branch", branch):
            raise WorkspaceError("invalid_branch", f"invalid branch name {branch!r}", branch=branch)

    async def _sb(self, wt: Path, *args: str, stdin: bytes | None = None) -> str:
        argv = ["git", *_HARDENING, *args]
        result = await self._sandbox.run(argv, wt, self._timeout_s, stdin)
        shown = "git " + " ".join(args)
        if result.timed_out:
            raise WorkspaceError("git_timeout", f"{shown} timed out (sandbox)", command=shown)
        if result.exit_code != 0:
            raise _fail(shown, result.exit_code, result.stderr)
        return result.stdout.strip()

    async def _sb_status(self, wt: Path, *args: str) -> tuple[int, str]:
        return await self._sb_raw(wt, ["git", *_HARDENING, *args])

    async def _sb_raw(self, cwd: Path, argv: Sequence[str]) -> tuple[int, str]:
        result = await self._sandbox.run(argv, cwd, self._timeout_s)
        if result.timed_out:
            shown = " ".join(argv)
            raise WorkspaceError("sandbox_timeout", f"{shown} timed out (sandbox)", command=shown)
        return result.exit_code, result.stderr

    async def _git(self, cwd: Path, *args: str) -> str:
        code, out, err = await self._run(cwd, args)
        if code != 0:
            raise _fail("git " + " ".join(args), code, err)
        return out.strip()

    async def _git_ok(self, cwd: Path, *args: str) -> bool:
        code, _, _ = await self._run(cwd, args)
        return code == 0

    def _mirror_args(self) -> list[str]:
        args = [*_HARDENING, "-c", "credential.interactive=false"]
        if self._credential_helper is not None:
            args += [
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper={self._credential_helper}",
            ]
        return args

    async def _run(self, cwd: Path, args: Sequence[str]) -> tuple[int, str, str]:
        shown = "git " + " ".join(args)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = await asyncio.create_subprocess_exec(
            "git",
            *self._mirror_args(),
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self._timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise WorkspaceError(
                "git_timeout", f"{shown} timed out after {self._timeout_s}s", command=shown
            ) from None
        code = proc.returncode if proc.returncode is not None else -1
        return code, out.decode(errors="replace"), err.decode(errors="replace")
