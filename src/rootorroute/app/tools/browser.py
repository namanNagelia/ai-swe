import json
import re

from rootorroute.app.tools.context import ToolContext, ToolFailed, ToolOutput
from rootorroute.domain.model import Image
from rootorroute.domain.sandbox import Sandbox
from rootorroute.domain.tools import Screenshot

SHOTS_DIR = ".rr/screenshots"
_TMP = ".rr/tmp-shot.jpeg"
_BROWSERS = "/opt/rootorroute/browsers"
_MAX_BYTES = 4_500_000  # under the 5 MB per-image API limit
_MAX_SIDE = 7_900  # under the 8000 px per-side API limit
_TIMEOUT_S = 180.0

# Reads the file, checks it's a JPEG and reads its size from the SOF marker.
_READ_JPEG = r"""
import base64, json, struct, sys
b = open(sys.argv[1], "rb").read()
w = h = 0
i = 2
while b[:2] == b"\xff\xd8" and i + 9 < len(b):
    if b[i] != 0xFF:
        break
    marker, size = b[i + 1], struct.unpack(">H", b[i + 2:i + 4])[0]
    if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
        h, w = struct.unpack(">HH", b[i + 5:i + 9])
        break
    i += 2 + size
print(json.dumps({"bytes": len(b), "w": w, "h": h, "jpeg": b[:2] == b"\xff\xd8",
                  "data": base64.b64encode(b).decode() if len(b) <= int(sys.argv[2]) else ""}))
"""


class ScreenshotTool:
    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._playwright: str | None = None

    async def _package(self, ctx: ToolContext) -> str:
        if self._playwright is None:
            r = await self._sandbox.run(["ls", "-a", _BROWSERS], ctx.worktree, 15)
            versions = re.findall(r"^\.playwright-(\d+\.\d+\.\d+)$", r.stdout, re.M)
            self._playwright = f"playwright@{versions[0]}" if versions else "playwright"
        return self._playwright

    async def screenshot(self, args: Screenshot, ctx: ToolContext) -> ToolOutput:
        if not re.match(r"^https?://", args.url):
            raise ToolFailed("url must start with http:// or https://")
        out = f"{SHOTS_DIR}/{args.save_as}.jpeg" if args.save_as else _TMP
        argv = ["npx", "--yes", await self._package(ctx), "screenshot"]
        argv += ["--wait-for-timeout", str(args.wait_ms), "--timeout", "60000"]
        if args.full_page:
            argv.append("--full-page")
        if args.device:
            argv += ["--device", args.device]
        else:
            argv += ["--viewport-size", "1280, 800"]
        if args.dark:
            argv += ["--color-scheme", "dark"]
        await self._sandbox.run(["mkdir", "-p", SHOTS_DIR], ctx.worktree, 15)
        shot = await self._sandbox.run([*argv, args.url, out], ctx.worktree, _TIMEOUT_S)
        if shot.exit_code != 0:
            detail = (shot.stderr or shot.stdout).strip()[-1500:]
            raise ToolFailed(f"screenshot failed (exit {shot.exit_code}): {detail}")

        read = await self._sandbox.run(
            ["python3", "-c", _READ_JPEG, out, str(_MAX_BYTES)],
            ctx.worktree,
            60,
            max_output_bytes=_MAX_BYTES * 2,
        )
        if not args.save_as:
            await self._sandbox.run(["rm", "-f", out], ctx.worktree, 15)
        try:
            info = json.loads(read.stdout)
        except json.JSONDecodeError:
            raise ToolFailed(
                f"couldn't read the screenshot: {read.stderr.strip()[-500:]}"
            ) from None
        if not info["jpeg"]:
            raise ToolFailed("the screenshot isn't a JPEG image")
        too_big = info["bytes"] > _MAX_BYTES or max(info["w"], info["h"]) > _MAX_SIDE
        if too_big or not info["data"]:
            hint = "use full_page=false" if args.full_page else "try a smaller device or page"
            raise ToolFailed(f"screenshot is {info['w']}x{info['h']}px, too large to view; {hint}")
        saved = f"; saved as {out} (shown in the PR)" if args.save_as else ""
        text = f"screenshot of {args.url}: {info['w']}x{info['h']}px{saved}"
        image = Image("image/jpeg", info["data"], info["w"], info["h"])
        return ToolOutput(text, (image,))
