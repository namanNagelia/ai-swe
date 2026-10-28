import io
import tarfile

from rootorroute.vm import bundle


def _members() -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(bundle.build()), mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


def test_bundle_contains_provisioner_and_manifest() -> None:
    members = _members()
    assert "provision.sh" in members
    assert "manifest.env" in members
    assert "packages.txt" in members
    assert "files/egress/egress-open.nft.tmpl" in members
    assert "files/egress/egress-allowlist.nft.tmpl" in members
    assert "files/systemd/rr-agent.service" in members


def test_bundle_modes_and_owners_are_normalized() -> None:
    members = _members()
    assert members["provision.sh"].mode == 0o755
    assert members["manifest.env"].mode == 0o644
    assert all(m.uid == 0 and m.uname == "root" for m in members.values())


def test_bundle_skips_os_junk() -> None:
    assert not any(name.endswith(".DS_Store") for name in _members())
