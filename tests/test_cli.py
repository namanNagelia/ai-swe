import pytest

from rootorroute.cli import build_parser


def test_vm_up_parses_repeated_allow_domain() -> None:
    args = build_parser().parse_args(
        ["vm", "up", "--allow-domain", "a.example", "--allow-domain", "b.example", "--memory", "16"]
    )
    assert args.allow_domain == ["a.example", "b.example"]
    assert args.memory == 16
    assert args.backend == "auto"


def test_vm_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["vm"])


def test_vm_up_defaults_to_open_egress_with_browser() -> None:
    args = build_parser().parse_args(["vm", "up"])
    assert args.egress == "open"
    assert args.no_browser is False


def test_vm_up_rejects_unknown_egress_mode() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["vm", "up", "--egress", "wide-open"])
