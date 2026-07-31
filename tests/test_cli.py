from __future__ import annotations

import pytest

from gh_slate.cli import build_parser, run
from gh_slate.invocation import DISPLAY_COMMAND_ENV


def test_root_command_prints_help(capsys) -> None:
    assert run([], prog="gh-slate") == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("usage: gh-slate")
    assert "dashboard comments" in output.out


def test_extension_display_command_is_used_in_help(monkeypatch, capsys) -> None:
    monkeypatch.setenv(DISPLAY_COMMAND_ENV, "gh slate")

    assert run([]) == 0

    assert capsys.readouterr().out.startswith("usage: gh slate")


def test_version_uses_selected_command_name(capsys) -> None:
    parser = build_parser(prog="gh slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "gh slate 0.1.0\n"


def test_unknown_command_is_usage_error() -> None:
    parser = build_parser(prog="gh-slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["apply"])

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["render", "ci", "--target", "42", "--repo", "owner/repo"],
        ["view", "ci", "--target", "42", "--repo", "owner/repo", "--json"],
        ["list", "--target", "@pr", "--host", "ghe.example", "--json"],
        ["state", "export", "ci", "--target", "42", "-R", "owner/repo"],
        ["state", "verify", "ci", "--target", "@event", "--json"],
        ["doctor", "--host", "ghe.example", "--json"],
    ],
)
def test_read_only_command_surface_is_registered(argv: list[str]) -> None:
    parsed = build_parser(prog="gh slate").parse_args(argv)

    assert callable(parsed.handler)


def test_view_json_and_web_are_mutually_exclusive() -> None:
    parser = build_parser(prog="gh slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(
            [
                "view",
                "ci",
                "--target",
                "42",
                "--json",
                "--web",
            ]
        )

    assert exit_info.value.code == 2
