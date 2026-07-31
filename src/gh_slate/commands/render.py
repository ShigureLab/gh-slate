from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from gh_slate.codec import validate_slate_name
from gh_slate.codec.json import DEFAULT_JSON_LIMITS
from gh_slate.rendering import (
    ListRendererV1,
    RenderingError,
    SlateContext,
    TableColumn,
    TableRendererV1,
    jinja_descriptor,
    render,
)
from gh_slate.rendering.jinja import DEFAULT_JINJA_LIMITS
from gh_slate.schema import validate_data_json, validate_schema_json

if TYPE_CHECKING:
    from argparse import Namespace


_LOCAL_OPTIONS = (
    "data",
    "schema",
    "template",
    "table",
    "list",
    "columns",
    "title",
)
_REMOTE_OPTIONS = (
    "target",
    "repo",
    "controller",
    "host",
)


def _input_size_error(
    *,
    subject: str,
    actual_bytes: int,
    max_bytes: int,
) -> RenderingError:
    return RenderingError(
        f"{subject} exceeds the configured input byte limit",
        code="input_size_limit",
        details={
            "actual_bytes": actual_bytes,
            "max_bytes": max_bytes,
        },
    )


def _stdin_bytes(*, subject: str, max_bytes: int) -> bytes:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    value = stream.read(max_bytes + 1)
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if len(encoded) > max_bytes:
        raise _input_size_error(
            subject=subject,
            actual_bytes=len(encoded),
            max_bytes=max_bytes,
        )
    return encoded


def _read_bytes(
    location: str,
    *,
    subject: str,
    max_bytes: int,
) -> bytes:
    if location == "-":
        return _stdin_bytes(subject=subject, max_bytes=max_bytes)
    try:
        path = Path(location)
        size = path.stat().st_size
        if size > max_bytes:
            raise _input_size_error(
                subject=subject,
                actual_bytes=size,
                max_bytes=max_bytes,
            )
        with path.open("rb") as stream:
            value = stream.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise _input_size_error(
                subject=subject,
                actual_bytes=len(value),
                max_bytes=max_bytes,
            )
        return value
    except RenderingError:
        raise
    except OSError as error:
        raise RenderingError(
            f"{subject} could not be read",
            code="input_read_failed",
            details={
                "path": location,
                "error_type": type(error).__name__,
            },
        ) from None


def _template_source(location: str) -> str:
    source = _read_bytes(
        location,
        subject="template",
        max_bytes=DEFAULT_JINJA_LIMITS.max_source_bytes,
    )
    try:
        return source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RenderingError(
            "template is not valid UTF-8",
            code="jinja_source_invalid",
            details={"position": error.start},
        ) from None


def _table_columns(value: str | None) -> tuple[TableColumn, ...]:
    if value is None:
        return ()
    names = tuple(item.strip() for item in value.split(","))
    if not names or any(not item for item in names):
        raise RenderingError(
            "--columns must be a comma-separated list of non-empty keys",
            code="renderer_config_invalid",
        )
    if len(set(names)) != len(names):
        raise RenderingError(
            "--columns must not contain duplicate keys",
            code="renderer_config_invalid",
        )
    return tuple(TableColumn(path=(name,), header=name) for name in names)


def _ensure_single_stdin(args: Namespace) -> None:
    sources = (
        getattr(args, "data", None),
        getattr(args, "schema", None),
        getattr(args, "template", None),
    )
    if sum(source == "-" for source in sources) > 1:
        raise RenderingError(
            "stdin may be used by only one input source",
            code="stdin_conflict",
        )


def _has_value(args: Namespace, names: tuple[str, ...]) -> bool:
    return any(getattr(args, name, None) is not None for name in names)


def _run_local_render(args: Namespace) -> int:
    _ensure_single_stdin(args)
    name = validate_slate_name(args.name)
    if args.template is None and args.table is None and args.list is None:
        raise RenderingError(
            "local render requires --template, --table, or --list",
            code="renderer_required",
        )
    if args.columns is not None and args.table is None:
        raise RenderingError(
            "--columns requires --table",
            code="renderer_option_conflict",
        )
    if args.title is not None and args.template is not None:
        raise RenderingError(
            "--title is available only with --table or --list",
            code="renderer_option_conflict",
        )

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
    data_source = (
        _read_bytes(
            args.data,
            subject="data",
            max_bytes=DEFAULT_JSON_LIMITS.max_input_bytes,
        )
        if args.data is not None
        else b"{}"
    )
    data = validate_data_json(data_source, schema)

    if args.template is not None:
        descriptor = jinja_descriptor(_template_source(args.template))
    elif args.table is not None:
        descriptor = TableRendererV1(
            selector=args.table,
            title=args.title,
            columns=_table_columns(args.columns),
        ).to_descriptor()
    else:
        descriptor = ListRendererV1(
            selector=args.list,
            title=args.title,
        ).to_descriptor()

    result = render(
        data,
        descriptor,
        schema=schema,
        slate=SlateContext(name=name),
    )
    sys.stdout.write(result.markdown)
    return 0


def run_render(args: Namespace) -> int:
    local = _has_value(args, _LOCAL_OPTIONS)
    remote = _has_value(args, _REMOTE_OPTIONS)
    if local and remote:
        raise RenderingError(
            "local renderer inputs cannot be combined with remote target options",
            code="renderer_option_conflict",
        )
    if local:
        return _run_local_render(args)

    from gh_slate.commands.read import run_remote_render

    return run_remote_render(args)


__all__ = ["run_render"]
