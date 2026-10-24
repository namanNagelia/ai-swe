"""Ports and adapters is enforced by import-linter; contracts live in pyproject.toml."""

import subprocess
import sys


def test_import_contracts_hold() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "importlinter.cli", "--no-cache"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
