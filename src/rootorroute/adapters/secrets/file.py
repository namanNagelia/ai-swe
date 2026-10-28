from pathlib import Path

from rootorroute.domain.secrets import SecretMissing


def check_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or "\0" in name or name.startswith("."):
        raise ValueError(f"invalid secret name {name!r}")


class FileSecrets:
    def __init__(self, dir: Path) -> None:
        self._dir = dir

    def get(self, name: str) -> str:
        check_name(name)
        try:
            value = (self._dir / name).read_text(encoding="utf-8").rstrip("\r\n")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise SecretMissing(name) from None
        if not value:
            raise SecretMissing(name)
        return value
