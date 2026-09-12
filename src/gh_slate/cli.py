from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from gh_slate import __version__
from gh_slate.codec import canonical_json_bytes
from gh_slate.errors import GhSlateError, format_error
from gh_slate.invocation import display_command

if TYPE_CHECKING:
    from collections.abc import Sequence

_MAX_ERROR_DETAIL_DEPTH = 64


def _normalize_error_json(
    value: object,
    *,
    depth: int = 0,
    active: set[int] | None = None,
) -> object:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal in error details")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float in error details")
        return Decimal(repr(value))
    if not isinstance(value, (Mapping, list, tuple)):
        raise TypeError("unsupported value in error details")
    if depth > _MAX_ERROR_DETAIL_DEPTH:
        raise ValueError("error details exceed the nesting limit")

    ancestors = set() if active is None else active
    identity = id(value)
    if identity in ancestors:
        raise ValueError("cycle in error details")
    ancestors.add(identity)
    try:
        if isinstance(value, Mapping):
            normalized: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("error detail keys must be strings")
                normalized[key] = _normalize_error_json(
                    item,
                    depth=depth + 1,
                    active=ancestors,
                )
            return normalized
        return [
            _normalize_error_json(
                item,
                depth=depth + 1,
                active=ancestors,
            )
            for item in value
        ]
    finally:
        ancestors.remove(identity)


def _minimal_error_json(error: GhSlateError) -> dict[str, object]:
    code = error.code if isinstance(error.code, str) else "runtime_error"
    message = error.message if isinstance(error.message, str) else "operation failed"
    payload: dict[str, object] = {
        "code": code,
        "message": message,
    }
    if isinstance(error.hints, tuple):
        hints = [hint for hint in error.hints if isinstance(hint, str)]
        if hints:
            payload["hints"] = hints
    return {"error": payload}


def _error_json_bytes(error: GhSlateError) -> bytes:
    try:
        return canonical_json_bytes(_normalize_error_json(error.as_dict()))
    except Exception:
        try:
            return canonical_json_bytes(_minimal_error_json(error))
        except Exception:  # pragma: no cover - immutable literal fallback
            return b'{"error":{"code":"runtime_error","message":"operation failed"}}'


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer") from None
    if not 1 <= parsed <= 2**63 - 1:
        raise argparse.ArgumentTypeError("expected an integer between 1 and 2^63-1")
    return parsed


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


def _add_snapshot_options(
    parser: argparse.ArgumentParser,
    *,
    data_defaults_empty: bool = False,
    allow_patch: bool = False,
) -> None:
    data_input = parser.add_mutually_exclusive_group() if allow_patch else parser
    data_input.add_argument(
        "--data",
        metavar="FILE",
        help=("strict JSON object input; use - for stdin" + (" (default: {})" if data_defaults_empty else "")),
    )
    if allow_patch:
        data_input.add_argument(
            "--patch", metavar="FILE", help="RFC 6902 JSON Patch of existing data; requires --if-revision"
        )
    parser.add_argument(
        "--schema",
        metavar="FILE",
        help="optional draft 2020-12 JSON Schema snapshot",
    )


def _add_renderer_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", metavar="FILE", help="explicit TOML configuration (or GH_SLATE_CONFIG)")
    renderer = parser.add_mutually_exclusive_group()
    renderer.add_argument("--profile", metavar="NAME", help="load a named definition from the explicit config")
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
    parser.add_argument(
        "--columns",
        metavar="KEY,...",
        help="ordered top-level table keys",
    )
    parser.add_argument(
        "--title",
        help="Markdown heading for a built-in table or list",
    )


def _add_mutation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--if-revision",
        type=_positive_integer,
        metavar="REV",
        help="reject a stale read before attempting a write",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help="emit an operation result object",
    )
    output.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress successful output",
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
    _add_snapshot_options(
        render_parser,
        data_defaults_empty=True,
    )
    _add_renderer_options(render_parser)
    render_parser.add_argument("--meta", metavar="FILE", help="local target metadata fixture; use - for stdin")
    render_parser.add_argument("--json", action="store_true", help="emit Markdown, data, and metadata provenance")
    from gh_slate.commands.render import run_render

    render_parser.set_defaults(handler=run_render)

    apply_parser = commands.add_parser(
        "apply",
        help="create or update one complete managed slate snapshot",
    )
    apply_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(apply_parser)
    apply_parser.add_argument(
        "--if-revision",
        type=_positive_integer,
        metavar="REV",
        help="reject a stale read before attempting a write",
    )
    apply_parser.add_argument(
        "--mode",
        choices=("upsert", "create", "update"),
        default="upsert",
        help="create/update behavior (default: upsert)",
    )
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and render without writing GitHub",
    )
    apply_output = apply_parser.add_mutually_exclusive_group()
    apply_output.add_argument(
        "--json",
        action="store_true",
        help="emit an operation result object",
    )
    apply_output.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress successful output",
    )
    _add_snapshot_options(apply_parser, allow_patch=True)
    _add_renderer_options(apply_parser)
    from gh_slate.commands.apply import run_apply

    apply_parser.set_defaults(handler=run_apply)

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

    from gh_slate.commands.recovery import run_delete, run_repair

    repair_parser = commands.add_parser(
        "repair",
        help="restore visible Markdown from canonical stored state",
    )
    repair_parser.add_argument("name", help="stable lowercase slate name")
    repair_parser.add_argument(
        "--from-state",
        action="store_true",
        required=True,
        help="discard visible edits and rerender canonical stored state",
    )
    _add_target_options(repair_parser)
    _add_mutation_options(repair_parser)
    repair_parser.set_defaults(handler=run_repair)

    delete_parser = commands.add_parser(
        "delete",
        help="delete one complete managed slate comment",
    )
    delete_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(delete_parser)
    _add_mutation_options(delete_parser)
    confirmation = delete_parser.add_mutually_exclusive_group(required=True)
    confirmation.add_argument(
        "--confirm",
        metavar="NAME",
        help="confirm deletion by repeating the exact slate name",
    )
    confirmation.add_argument(
        "--yes",
        action="store_true",
        help="confirm deletion non-interactively",
    )
    delete_parser.set_defaults(handler=run_delete)

    from gh_slate.commands.data import (
        run_data_delete,
        run_data_edit,
        run_data_get,
        run_data_set,
        run_data_update,
    )

    data_parser = commands.add_parser(
        "data",
        help="query or mutate a managed slate's typed JSON data",
    )
    data_commands = data_parser.add_subparsers(dest="data_command")

    data_get_parser = data_commands.add_parser(
        "get",
        help="query stored data with jq",
    )
    data_get_parser.add_argument("name", help="stable lowercase slate name")
    data_get_parser.add_argument(
        "filter",
        nargs="?",
        default=".",
        help="jq filter (default: .)",
    )
    _add_target_options(data_get_parser)
    data_get_parser.add_argument(
        "-r",
        "--raw-output",
        action="store_true",
        help="write string results without JSON quoting",
    )
    data_get_parser.add_argument(
        "-c",
        "--compact-output",
        action="store_true",
        help="write one compact JSON result per line",
    )
    data_get_parser.add_argument(
        "-e",
        "--exit-status",
        action="store_true",
        help="use jq-compatible status for the last result",
    )
    data_get_parser.set_defaults(handler=run_data_get)

    data_set_parser = data_commands.add_parser(
        "set",
        help="set one value using a static jq path",
    )
    data_set_parser.add_argument("name", help="stable lowercase slate name")
    data_set_parser.add_argument("path", help="static jq path expression")
    _add_target_options(data_set_parser)
    _add_mutation_options(data_set_parser)
    value_source = data_set_parser.add_mutually_exclusive_group(required=True)
    value_source.add_argument(
        "--value",
        metavar="JSON",
        help="one strict JSON value; use - for stdin",
    )
    value_source.add_argument(
        "--value-string",
        metavar="TEXT",
        help="an exact string value",
    )
    value_source.add_argument(
        "--value-file",
        metavar="FILE",
        help="one strict JSON document; use - for stdin",
    )
    data_set_parser.set_defaults(handler=run_data_set)

    data_delete_parser = data_commands.add_parser(
        "delete",
        help="delete one or more static jq paths",
    )
    data_delete_parser.add_argument("name", help="stable lowercase slate name")
    data_delete_parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="static jq path expression",
    )
    _add_target_options(data_delete_parser)
    _add_mutation_options(data_delete_parser)
    data_delete_parser.add_argument(
        "--ignore-missing",
        action="store_true",
        help="treat an absent path as unchanged",
    )
    data_delete_parser.set_defaults(handler=run_data_delete)

    data_update_parser = data_commands.add_parser(
        "update",
        help="transform the complete data object with jq",
    )
    data_update_parser.add_argument("name", help="stable lowercase slate name")
    data_update_parser.add_argument("filter", help="jq update filter")
    _add_target_options(data_update_parser)
    _add_mutation_options(data_update_parser)
    data_update_parser.add_argument(
        "--arg",
        action="append",
        default=[],
        nargs=2,
        metavar=("NAME", "VALUE"),
        help="bind a jq string variable; repeatable",
    )
    data_update_parser.add_argument(
        "--argjson",
        action="append",
        default=[],
        nargs=2,
        metavar=("NAME", "JSON"),
        help="bind typed JSON or @FILE; repeatable",
    )
    data_update_parser.set_defaults(handler=run_data_update)

    data_edit_parser = data_commands.add_parser(
        "edit",
        help="edit stored typed JSON in GH_EDITOR or EDITOR",
    )
    data_edit_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(data_edit_parser)
    _add_mutation_options(data_edit_parser)
    data_edit_parser.set_defaults(handler=run_data_edit)

    from gh_slate.commands.schema import (
        run_schema_get,
        run_schema_infer,
        run_schema_set,
        run_schema_validate,
    )

    schema_parser = commands.add_parser(
        "schema",
        help="inspect or change a slate's JSON Schema snapshot",
    )
    schema_commands = schema_parser.add_subparsers(dest="schema_command")

    schema_get_parser = schema_commands.add_parser(
        "get",
        help="print the stored JSON Schema",
    )
    schema_get_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(schema_get_parser)
    schema_get_parser.add_argument(
        "-c",
        "--compact-output",
        action="store_true",
        help="write compact canonical JSON",
    )
    schema_get_parser.set_defaults(handler=run_schema_get)

    schema_set_parser = schema_commands.add_parser(
        "set",
        help="replace and validate the stored JSON Schema",
    )
    schema_set_parser.add_argument("name", help="stable lowercase slate name")
    schema_set_parser.add_argument(
        "file",
        metavar="FILE",
        help="strict JSON Schema document; use - for stdin",
    )
    _add_target_options(schema_set_parser)
    _add_mutation_options(schema_set_parser)
    schema_set_parser.set_defaults(handler=run_schema_set)

    schema_infer_parser = schema_commands.add_parser(
        "infer",
        help="infer a permissive schema from current stored data",
    )
    schema_infer_parser.add_argument("name", help="stable lowercase slate name")
    _add_target_options(schema_infer_parser)
    schema_infer_parser.add_argument(
        "--apply",
        action="store_true",
        help="store the inferred schema after validation",
    )
    _add_mutation_options(schema_infer_parser)
    schema_infer_parser.set_defaults(handler=run_schema_infer)

    schema_validate_parser = schema_commands.add_parser(
        "validate",
        help="validate current or supplied data against the stored schema",
    )
    schema_validate_parser.add_argument("name", help="stable lowercase slate name")
    schema_validate_parser.add_argument(
        "file",
        nargs="?",
        metavar="FILE",
        help="candidate strict JSON object; use - for stdin",
    )
    _add_target_options(schema_validate_parser)
    schema_validate_parser.add_argument(
        "--json",
        action="store_true",
        help="emit validation metadata",
    )
    schema_validate_parser.set_defaults(handler=run_schema_validate)

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
        if getattr(args, "json", False):
            sys.stderr.write(_error_json_bytes(error).decode("utf-8") + "\n")
            return int(error.exit_code)
        for line in format_error(error):
            print(line, file=sys.stderr)
        return int(error.exit_code)
