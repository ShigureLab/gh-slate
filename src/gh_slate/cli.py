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
    commands = parser.add_subparsers(dest="command")

    render_parser = commands.add_parser(
        "render",
        help="render a slate locally without writing GitHub",
        description="Render typed JSON locally without making any GitHub API request.",
    )
    render_parser.add_argument("name", help="stable lowercase slate name")
    render_parser.add_argument(
        "--data",
        metavar="FILE",
        help="strict JSON object input; use - for stdin (default: {})",
    )
    render_parser.add_argument(
        "--schema",
        metavar="FILE",
        help="optional draft 2020-12 JSON Schema snapshot",
    )
    renderer = render_parser.add_mutually_exclusive_group(required=True)
    renderer.add_argument(
        "--template",
        metavar="FILE",
        help="sandboxed Jinja template source; use - for stdin",
    )
    renderer.add_argument(
        "--table",
        metavar="FILTER",
        help="jq selector producing one array of objects",
    )
    renderer.add_argument(
        "--list",
        metavar="FILTER",
        help="jq selector producing one JSON value",
    )
    render_parser.add_argument(
        "--columns",
        metavar="KEY,...",
        help="ordered top-level table keys",
    )
    render_parser.add_argument(
        "--title",
        help="Markdown heading for a built-in table or list",
    )
    from gh_slate.commands.render import run_render

    render_parser.set_defaults(handler=run_render)
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
