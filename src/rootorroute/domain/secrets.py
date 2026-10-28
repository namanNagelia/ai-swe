from typing import Protocol


class SecretMissing(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"secret {name!r} is not set")
        self.name = name


class Secrets(Protocol):
    def get(self, name: str) -> str:
        """Read a secret at call time. Raises SecretMissing."""
        ...
