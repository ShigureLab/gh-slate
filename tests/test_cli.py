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
        parser.parse_args(["unknown"])

    assert exit_info.value.code == 2


def test_apply_command_surface_parses_full_snapshot_options() -> None:
    parsed = build_parser(prog="gh slate").parse_args(
        [
            "apply",
            "ci",
            "--target",
            "42",
            "--repo",
            "owner/repo",
            "--controller",
            "ci-bot",
            "--host",
            "ghe.example",
            "--if-revision",
            "7",
            "--mode",
            "update",
            "--dry-run",
            "--json",
            "--data",
            "data.json",
            "--schema",
            "schema.json",
            "--table",
            ".jobs",
            "--columns",
            "name,status",
            "--title",
            "Jobs",
        ]
    )

    assert callable(parsed.handler)
    assert parsed.name == "ci"
    assert parsed.target == "42"
    assert parsed.repo == "owner/repo"
    assert parsed.controller == "ci-bot"
    assert parsed.host == "ghe.example"
    assert parsed.if_revision == 7
    assert parsed.mode == "update"
    assert parsed.dry_run is True
    assert parsed.json is True
    assert parsed.quiet is False
    assert parsed.data == "data.json"
    assert parsed.schema == "schema.json"
    assert parsed.template is None
    assert parsed.table == ".jobs"
    assert parsed.list is None
    assert parsed.columns == "name,status"
    assert parsed.title == "Jobs"


@pytest.mark.parametrize(
    "revision",
    ["0", "-1", str(2**63), "not-an-integer"],
)
def test_apply_if_revision_must_be_a_positive_integer(revision: str) -> None:
    parser = build_parser(prog="gh slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["apply", "ci", "--if-revision", revision])

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "options",
    [
        ["--template", "slate.j2", "--table", ".jobs"],
        ["--table", ".jobs", "--list", ".notes"],
        ["--json", "--quiet"],
    ],
)
def test_apply_renderer_and_output_modes_are_mutually_exclusive(
    options: list[str],
) -> None:
    parser = build_parser(prog="gh slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["apply", "ci", *options])

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
