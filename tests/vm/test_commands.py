import io
import json
import tarfile
from pathlib import Path

from rootorroute.vm import commands
from rootorroute.vm.backends import MultipassBackend, VMSpec
from tests.vm.fakes import FakeRunner


def _missing_vm() -> FakeRunner:
    return FakeRunner({("multipass", "list"): json.dumps({"list": []}).encode()})


def test_up_launches_pushes_bundle_and_provisions() -> None:
    runner = _missing_vm()
    backend = MultipassBackend(run=runner)
    opts = commands.ProvisionOptions(
        allow_domains=["acme.atlassian.net"], packages=["golang-migrate", "kcat"]
    )

    commands.up(backend, VMSpec(name="t"), opts)

    assert runner.calls[1][:2] == ["multipass", "launch"]
    push, prov = runner.calls[2], runner.calls[3]
    assert push[:5] == ["multipass", "exec", "t", "--", "sudo"]
    assert "tar -xzf -" in push[-1]
    with tarfile.open(fileobj=io.BytesIO(runner.stdin_seen[0]), mode="r:gz") as tar:
        assert "provision.sh" in tar.getnames()
    assert "RR_ALLOW_DOMAINS=acme.atlassian.net" in prov
    assert "RR_EGRESS_MODE=open" in prov
    assert "RR_EXTRA_PACKAGES=golang-migrate,kcat" in prov
    assert "RR_WITH_BROWSER=0" not in prov
    assert prov[-1] == f"{commands.BUNDLE_DIR}/provision.sh"


def test_backup_pauses_agent_and_streams_home(tmp_path: Path) -> None:
    runner = FakeRunner({("multipass", "exec", "t", "--", "sudo", "tar"): b"TARBALL"})
    out = tmp_path / "home.tgz"

    commands.backup(MultipassBackend(run=runner), "t", out)

    assert out.read_bytes() == b"TARBALL"
    stop, tar, start = runner.calls
    assert stop[-2:] == ["stop", "rr-agent"]
    assert "--exclude=agent/.local/share/docker" in tar
    assert start[-2:] == ["start", "rr-agent"]
