import os

from rootorroute.domain.secrets import SecretMissing


class EnvSecrets:
    def __init__(self, prefix: str = "RR_SECRET_") -> None:
        self._prefix = prefix

    def get(self, name: str) -> str:
        value = os.environ.get(self._prefix + name.upper().replace("-", "_"), "")
        if not value:
            raise SecretMissing(name)
        return value
