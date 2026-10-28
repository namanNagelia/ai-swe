import os
import sys
from pathlib import Path

DEFAULT_TOKEN_FILE = "/home/agent/state/secrets/github_token"
USERNAME = "x-access-token"


def _read_request() -> dict[str, str]:
    attrs: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            attrs[key] = value
    return attrs


def _token() -> str | None:
    path = Path(os.environ.get("RR_GITHUB_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token or "\n" in token or "\0" in token:
        return None
    return token


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: rr-git-credential get|store|erase", file=sys.stderr)
        return 2
    if args[0] != "get":
        return 0
    attrs = _read_request()
    if attrs.get("protocol") != "https" or attrs.get("host") != "github.com":
        return 0
    token = _token()
    if token is None:
        print("rr-git-credential: no GitHub token available", file=sys.stderr)
        return 0
    sys.stdout.write(f"username={USERNAME}\npassword={token}\n")
    return 0
