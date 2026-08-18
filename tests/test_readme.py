from __future__ import annotations

import shlex
from pathlib import Path

from gh_slate.cli import build_parser

README = Path(__file__).resolve().parents[1] / "README.md"


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
