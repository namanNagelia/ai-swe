"""Locate the image assets and pack them into the bundle pushed into a VM."""

from __future__ import annotations

import io
import subprocess
import tarfile
from importlib import resources
from pathlib import Path

# Executable files inside the bundle. Set explicitly so bundles built on
# Windows (no exec bit) still work.
EXECUTABLES = {"provision.sh"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def image_dir() -> Path:
    """infra/image from a wheel install (packaged as _image) or a source checkout."""
    packaged = resources.files("rootorroute").joinpath("_image")
    if packaged.is_dir():
        return Path(str(packaged))
    source = _repo_root() / "infra" / "image"
    if source.is_dir():
        return source
    raise FileNotFoundError("image assets not found; reinstall rootorroute or run from a checkout")


def lima_template() -> Path:
    packaged = resources.files("rootorroute").joinpath("_lima/rootorroute.yaml")
    if packaged.is_file():
        return Path(str(packaged))
    return _repo_root() / "infra" / "lima" / "rootorroute.yaml"


def revision() -> str:
    """Git revision of the source checkout, or 'unknown' outside one."""
    root = _repo_root()
    try:
        rev = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "infra/image"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{rev}-dirty" if dirty else rev


def build(src: Path | None = None) -> bytes:
    """A gzipped tarball of the image directory, with normalized modes and owners."""
    src = src or image_dir()

    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        if info.isdir() or Path(info.name).name in EXECUTABLES:
            info.mode = 0o755
        else:
            info.mode = 0o644
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if path.name in {".DS_Store", "__pycache__"}:
                continue
            tar.add(
                path, arcname=path.relative_to(src).as_posix(), recursive=False, filter=normalize
            )
    return buf.getvalue()
