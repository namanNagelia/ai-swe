import json
import re
from pathlib import Path

import pytest

from rootorroute.vm import bundle
from rootorroute.vm.backends import (
    BackendError,
    LimaBackend,
    MultipassBackend,
    SSHBackend,
    VMSpec,
    VMState,
)
from tests.vm.fakes import FakeRunner


def test_lima_state_parses_json_lines() -> None:
    lines = [{"name": "other", "status": "Running"}, {"name": "rr-agent", "status": "Stopped"}]
    runner = FakeRunner({("limactl", "list"): "\n".join(json.dumps(x) for x in lines).encode()})
    lima = LimaBackend(run=runner)
    assert lima.state("rr-agent") is VMState.STOPPED
    assert lima.state("nope") is VMState.MISSING


def test_lima_template_shares_nothing_with_host() -> None:
    lima = LimaBackend(template=bundle.lima_template())
    rendered = lima.render_template(VMSpec(cpus=6, memory_gib=12, disk_gib=80))
    assert "cpus: 6" in rendered
    assert 'memory: "12GiB"' in rendered
    assert "mounts: []" in rendered
    assert "forwardAgent: false" in rendered
    assert not re.search(r"@[A-Z]+@", rendered)


def test_lima_exec_runs_from_root_workdir() -> None:
    assert LimaBackend().exec_argv("vm", ["id"]) == [
        "limactl",
        "shell",
        "--workdir",
        "/",
        "vm",
        "id",
    ]


def test_multipass_state_and_launch() -> None:
    listing = {"list": [{"name": "rr-agent", "state": "Running"}]}
    runner = FakeRunner({("multipass", "list"): json.dumps(listing).encode()})
    mp = MultipassBackend(run=runner)
    assert mp.state("rr-agent") is VMState.RUNNING

    mp.launch(VMSpec(name="x", cpus=2, memory_gib=4, disk_gib=30))
    assert runner.calls[-1] == [
        "multipass",
        "launch",
        "24.04",
        "--name",
        "x",
        "--cpus",
        "2",
        "--memory",
        "4G",
        "--disk",
        "30G",
    ]


def test_ssh_quotes_remote_command() -> None:
    ssh = SSHBackend(target="ubuntu@host", port=2222, identity="/k")
    argv = ssh.exec_argv("ignored", ["sudo", "env", "A=b c", "/x/provision.sh"])
    assert argv[:7] == ["ssh", "-o", "BatchMode=yes", "-p", "2222", "-i", "/k"]
    assert argv[-1] == "sudo env 'A=b c' /x/provision.sh"


def test_ssh_does_not_manage_lifecycle() -> None:
    with pytest.raises(BackendError):
        SSHBackend(target="h").delete("vm")


def test_lima_template_path_exists() -> None:
    assert Path(bundle.lima_template()).is_file()
