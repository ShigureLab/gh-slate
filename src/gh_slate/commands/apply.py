from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from gh_slate.codec import (
    JsonValue,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    canonical_json_bytes,
    validate_slate_name,
)
from gh_slate.codec.json import DEFAULT_JSON_LIMITS
from gh_slate.commands.render import (
    _ensure_single_stdin,
    _read_bytes,
    _table_columns,
    _template_source,
)
from gh_slate.errors import GhSlateError
from gh_slate.github.apply import (
    ApplyMode,
    ApplyRequest,
    ApplyResult,
    apply,
)
from gh_slate.github.lookup import ProcessTargetLookup, TargetProcess
from gh_slate.github.process import GhProcess
from gh_slate.github.target import resolve_target
from gh_slate.github.write import GhWriteProcess
from gh_slate.rendering import (
    ListRendererV1,
    RenderingError,
    TableRendererV1,
    jinja_descriptor,
)
from gh_slate.schema import validate_data_json, validate_schema_json

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Mapping


class ApplyTransaction(Protocol):
    @property
    def reader(self) -> TargetProcess: ...

    def apply(
        self,
        request: ApplyRequest,
    ) -> ApplyResult: ...


@dataclass(slots=True)
class _CoreTransaction:
    reader: GhProcess
    writer: GhWriteProcess

    def apply(
        self,
        request: ApplyRequest,
    ) -> ApplyResult:
        return apply(
            request,
            reader=self.reader,
            writer=self.writer,
        )


def _new_transaction() -> ApplyTransaction:
    return _CoreTransaction(
        reader=GhProcess(),
        writer=GhWriteProcess(),
    )


def _renderer(args: Namespace) -> RendererDescriptorV1 | None:
    if args.columns is not None and args.table is None:
        raise RenderingError(
            "--columns requires --table",
            code="renderer_option_conflict",
        )
    if args.title is not None and args.table is None and args.list is None:
        raise RenderingError(
            "--title is available only with --table or --list",
            code="renderer_option_conflict",
        )
    if args.template is not None:
        return jinja_descriptor(_template_source(args.template))
    if args.table is not None:
        return TableRendererV1(
            selector=args.table,
            title=args.title,
            columns=_table_columns(args.columns),
        ).to_descriptor()
    if args.list is not None:
        return ListRendererV1(
            selector=args.list,
            title=args.title,
        ).to_descriptor()
    return None


def _prepare_request_parts(
    args: Namespace,
) -> tuple[
    str,
    Mapping[str, JsonValue] | None,
    SchemaSnapshotV1 | None,
    RendererDescriptorV1 | None,
]:
    name = validate_slate_name(args.name)
    renderer_selected = any(
        value is not None
        for value in (
            args.template,
            args.table,
            args.list,
        )
    )
    if args.mode == "create" and not renderer_selected:
        raise RenderingError(
            "creating a slate requires --template, --table, or --list",
            code="renderer_required",
        )
    _ensure_single_stdin(args)

    schema = (
        validate_schema_json(
            _read_bytes(
                args.schema,
                subject="schema",
                max_bytes=DEFAULT_JSON_LIMITS.max_input_bytes,
            )
        )
        if args.schema is not None
        else None
    )
    if args.data is not None:
        data = validate_data_json(
            _read_bytes(
                args.data,
                subject="data",
                max_bytes=DEFAULT_JSON_LIMITS.max_input_bytes,
            ),
            schema,
        )
    elif args.mode == "create":
        data = validate_data_json(b"{}", schema)
    else:
        data = None
    return name, data, schema, _renderer(args)


def _write_json(value: Mapping[str, object]) -> None:
    sys.stdout.write(canonical_json_bytes(cast("JsonValue", value)).decode("utf-8") + "\n")


def _write_result(
    result: ApplyResult,
    *,
    dry_run: bool,
    as_json: bool,
    quiet: bool,
) -> None:
    if quiet:
        return
    if as_json:
        _write_json(result.to_json())
        return
    if dry_run:
        if result.markdown is None:
            raise GhSlateError(
                "apply dry-run result is missing its Markdown preview",
                code="apply_result_invalid",
            )
        sys.stdout.write(result.markdown)
        return
    suffix = "" if result.url is None else f" -> {result.url}"
    print(f"{result.action} {result.name}{suffix}")


def run_apply(args: Namespace) -> int:
    name, data, schema, renderer = _prepare_request_parts(args)
    transaction = _new_transaction()
    target = resolve_target(
        args.target,
        repo=args.repo,
        host=args.host,
        lookup=ProcessTargetLookup(transaction.reader),
        environ=os.environ,
    )
    request = ApplyRequest(
        target=target,
        name=name,
        mode=cast("ApplyMode", args.mode),
        data=data,
        renderer=renderer,
        data_schema=schema,
        replace_schema=args.schema is not None or args.clear_schema,
        controller=args.controller,
        if_revision=args.if_revision,
        dry_run=args.dry_run,
    )
    result = transaction.apply(request)
    _write_result(
        result,
        dry_run=args.dry_run,
        as_json=args.json,
        quiet=args.quiet,
    )
    return 0


__all__ = [
    "ApplyRequest",
    "ApplyResult",
    "ApplyTransaction",
    "run_apply",
]
