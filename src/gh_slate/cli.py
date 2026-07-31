from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from gh_slate import __version__
from gh_slate.errors import GhSlateError, format_error
from gh_slate.invocation import display_command

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=display_command() if prog is None else prog,
        description="Manage named, data-backed dashboard comments on GitHub Issues and Pull Requests.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def run(argv: Sequence[str], *, prog: str | None = None) -> int:
    parser = build_parser(prog=prog)
    args = parser.parse_args(list(argv))
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return int(handler(args))
    except GhSlateError as error:
        for line in format_error(error):
            print(line, file=sys.stderr)
        return int(error.exit_code)
