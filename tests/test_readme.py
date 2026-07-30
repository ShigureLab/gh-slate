from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from gh_slate.cli import build_parser

README = Path(__file__).resolve().parents[1] / "README.md"

DOCUMENTED_COMMANDS = [
    (
        "gh slate doctor --json",
        ["doctor", "--json"],
    ),
    (
        "gh slate apply ci-summary --target https://github.com/OWNER/REPO/issues/42 --mode create "
        "--data report.json --table '.jobs' --columns name,status --title 'CI summary' --dry-run",
        [
            "apply",
            "ci-summary",
            "--target",
            "https://github.com/OWNER/REPO/issues/42",
            "--mode",
            "create",
            "--data",
            "report.json",
            "--table",
            ".jobs",
            "--columns",
            "name,status",
            "--title",
            "CI summary",
            "--dry-run",
        ],
    ),
    (
        "gh slate list --target https://github.com/OWNER/REPO/issues/42 --json",
        [
            "list",
            "--target",
            "https://github.com/OWNER/REPO/issues/42",
            "--json",
        ],
    ),
    (
        "gh slate apply release-items --target https://github.com/OWNER/REPO/issues/42 --mode create "
        "--data release.json --list '.items' --title 'Release items' --json",
        [
            "apply",
            "release-items",
            "--target",
            "https://github.com/OWNER/REPO/issues/42",
            "--mode",
            "create",
            "--data",
            "release.json",
            "--list",
            ".items",
            "--title",
            "Release items",
            "--json",
        ],
    ),
    (
        "gh slate apply deployment --target https://github.com/OWNER/REPO/pull/42 --mode create "
        "--data deployment.json --schema deployment.schema.json --template deployment.md.j2 --dry-run",
        [
            "apply",
            "deployment",
            "--target",
            "https://github.com/OWNER/REPO/pull/42",
            "--mode",
            "create",
            "--data",
            "deployment.json",
            "--schema",
            "deployment.schema.json",
            "--template",
            "deployment.md.j2",
            "--dry-run",
        ],
    ),
    (
        "gh slate data get ci-summary '.jobs[] | select(.status != \"passed\") | .name' "
        "--target https://github.com/OWNER/REPO/issues/42 --raw-output",
        [
            "data",
            "get",
            "ci-summary",
            '.jobs[] | select(.status != "passed") | .name',
            "--target",
            "https://github.com/OWNER/REPO/issues/42",
            "--raw-output",
        ],
    ),
    (
        "gh slate data set ci-summary '.jobs[1].status' "
        "--target https://github.com/OWNER/REPO/issues/42 --value-string passed --if-revision 1 --json",
        [
            "data",
            "set",
            "ci-summary",
            ".jobs[1].status",
            "--target",
            "https://github.com/OWNER/REPO/issues/42",
            "--value-string",
            "passed",
            "--if-revision",
            "1",
            "--json",
        ],
    ),
    (
        "gh slate data update ci-summary "
        "'.jobs |= map(if .name == $name then .status = \"passed\" else . end)' "
        "--target https://github.com/OWNER/REPO/issues/42 --arg name windows --if-revision 1 --json",
        [
            "data",
            "update",
            "ci-summary",
            '.jobs |= map(if .name == $name then .status = "passed" else . end)',
            "--target",
            "https://github.com/OWNER/REPO/issues/42",
            "--arg",
            "name",
            "windows",
            "--if-revision",
            "1",
            "--json",
        ],
    ),
    (
        "gh slate repair ci-summary --from-state --target https://github.com/OWNER/REPO/issues/42 "
        "--if-revision 2 --json",
        [
            "repair",
            "ci-summary",
            "--from-state",
            "--target",
            "https://github.com/OWNER/REPO/issues/42",
            "--if-revision",
            "2",
            "--json",
        ],
    ),
    (
        "gh slate delete ci-summary --target https://github.com/OWNER/REPO/issues/42 --confirm ci-summary --json",
        [
            "delete",
            "ci-summary",
            "--target",
            "https://github.com/OWNER/REPO/issues/42",
            "--confirm",
            "ci-summary",
            "--json",
        ],
    ),
]


@pytest.mark.parametrize(
    ("documented", "arguments"),
    DOCUMENTED_COMMANDS,
)
def test_documented_cli_commands_match_the_real_parser(
    documented: str,
    arguments: list[str],
) -> None:
    readme = README.read_text(encoding="utf-8")

    assert documented in readme
    namespace = build_parser(prog="gh slate").parse_args(arguments)
    assert callable(namespace.handler)


def test_install_and_safety_contracts_are_documented() -> None:
    readme = README.read_text(encoding="utf-8")

    for command in (
        "uv tool install gh-slate",
        "gh extension install ShigureLab/gh-slate",
        "npx skills add https://github.com/ShigureLab/gh-slate --skill gh-slate",
        "gh skill install ShigureLab/gh-slate gh-slate --agent codex --scope user",
        "gh auth status",
        "gh slate doctor --json",
    ):
        assert command in readme

    assert "source of truth" in readme
    assert "no atomic compare-and-swap (CAS)" in readme
    assert "runtime-tested on Linux, macOS,\nand Windows" in readme
    assert "not a supported\nWindows entrypoint" in readme
    assert "It has not been executed as current release evidence." in readme
    assert "does not yet claim stable or GA status" in readme


def test_every_gh_extension_quickstart_command_parses() -> None:
    commands = [
        line
        for line in README.read_text(encoding="utf-8").splitlines()
        if line.startswith("gh slate ") and line != "gh slate --help"
    ]

    assert len(commands) >= 10
    for command in commands:
        arguments = shlex.split(command)[2:]
        namespace = build_parser(prog="gh slate").parse_args(arguments)
        assert callable(namespace.handler), command
