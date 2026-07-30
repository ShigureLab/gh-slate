from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from gh_slate import __version__
from gh_slate.errors import GhSlateError, format_error
from gh_slate.invocation import display_command

if TYPE_CHECKING:
    from collections.abc import Sequence


def _add_target_options(
    parser: argparse.ArgumentParser,
    *,
    include_controller: bool = True,
) -> None:
    parser.add_argument(
        "-t",
        "--target",
        metavar="TARGET",
        help="Issue/PR number, URL, @event, or @pr",
    )
    parser.add_argument(
        "-R",
        "--repo",
        metavar="OWNER/REPO",
        help="repository for a numeric target",
    )
    if include_controller:
        parser.add_argument(
            "--controller",
            metavar="LOGIN",
            help="comment controller (default: current token actor)",
        )
    parser.add_argument(
        "--host",
        metavar="HOST",
        help="GitHub hostname (default: gh/GH_HOST context)",
    )


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
        help="render local input or canonical stored state without writing GitHub",
        description=(
            "Render typed local JSON, or fetch and verify one slate's canonical "
            "stored state. This command never writes GitHub."
        ),
    )
    render_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(render_parser)
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
    renderer = render_parser.add_mutually_exclusive_group()
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

    from gh_slate.commands.read import (
        run_doctor,
        run_list,
        run_state_export,
        run_state_verify,
        run_view,
    )

    view_parser = commands.add_parser(
        "view",
        help="show a managed slate's visible Markdown or metadata",
    )
    view_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(view_parser)
    view_output = view_parser.add_mutually_exclusive_group()
    view_output.add_argument(
        "--json",
        action="store_true",
        help="emit identity, renderer, revision, hashes, and drift metadata",
    )
    view_output.add_argument(
        "--web",
        action="store_true",
        help="open the managed comment in a browser",
    )
    view_parser.set_defaults(handler=run_view)

    list_parser = commands.add_parser(
        "list",
        help="list managed slates on an Issue or Pull Request",
    )
    _add_target_options(list_parser)
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a JSON array",
    )
    list_parser.set_defaults(handler=run_list)

    state_parser = commands.add_parser(
        "state",
        help="inspect the typed state embedded in a managed comment",
    )
    state_commands = state_parser.add_subparsers(dest="state_command")

    export_parser = state_commands.add_parser(
        "export",
        help="decode the complete canonical state as JSON",
    )
    export_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(export_parser)
    export_parser.set_defaults(handler=run_state_export)

    verify_parser = state_commands.add_parser(
        "verify",
        help="verify state, schema, renderer, and render hashes",
    )
    verify_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(verify_parser)
    verify_parser.add_argument(
        "--json",
        action="store_true",
        help="emit verification metadata as JSON",
    )
    verify_parser.set_defaults(handler=run_state_verify)

    doctor_parser = commands.add_parser(
        "doctor",
        help="check gh, authentication, jq, Jinja, and schema support",
    )
    doctor_parser.add_argument(
        "--host",
        metavar="HOST",
        help="GitHub hostname to check",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="emit diagnostic results as JSON",
    )
    doctor_parser.set_defaults(handler=run_doctor)
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
