"""Run the agent's VM locally (Lima, Multipass) or provision any Ubuntu box over SSH."""

from __future__ import annotations

import sys

from rootorroute.vm import bundle
from rootorroute.vm.backends import (
    LOCAL_BACKENDS,
    Backend,
    BackendError,
    LimaBackend,
    MultipassBackend,
    SSHBackend,
    VMState,
)

INSTALL_HINT = """no VM backend found. Install one:
  macOS:   brew install lima          (or: brew install --cask multipass)
  Linux:   https://lima-vm.io/docs/installation/   (or: sudo snap install multipass)
  Windows: winget install Canonical.Multipass
  Any existing Ubuntu 24.04 machine: rr vm provision --ssh user@host"""


def make_backend(name: str) -> Backend:
    if name == "lima":
        return LimaBackend(template=bundle.lima_template())
    if name == "multipass":
        return MultipassBackend()
    raise BackendError(f"unknown backend {name!r}; choose from {', '.join(LOCAL_BACKENDS)}")


def preference() -> list[str]:
    # Lima has no Windows support; Multipass is the cross-platform fallback.
    return ["multipass"] if sys.platform == "win32" else ["lima", "multipass"]


def resolve_backend(choice: str, vm: str) -> Backend:
    """`auto` picks the backend that already has this VM, else the first installed one."""
    if choice != "auto":
        backend = make_backend(choice)
        if not backend.available():
            raise BackendError(f"{backend.binary} not found on PATH\n\n{INSTALL_HINT}")
        return backend

    installed = [b for b in (make_backend(n) for n in preference()) if b.available()]
    if not installed:
        raise BackendError(INSTALL_HINT)
    for backend in installed:
        if backend.state(vm) is not VMState.MISSING:
            return backend
    return installed[0]


__all__ = ["Backend", "BackendError", "SSHBackend", "make_backend", "resolve_backend"]
