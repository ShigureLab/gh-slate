from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from gh_slate.codec import MetaSnapshot, canonical_json_bytes, strict_loads, validate_slate_name
from gh_slate.codec.json import DEFAULT_JSON_LIMITS
from gh_slate.configuration import ProfileDefinition, load_profile
from gh_slate.inputs import read_bytes as _read_bytes, template_source as _template_source
from gh_slate.rendering import (
    ListRendererV1,
    RenderingError,
    SlateContext,
    TableColumn,
    TableRendererV1,
    jinja_descriptor,
    render,
)
from gh_slate.schema import validate_data_json, validate_schema_json

if TYPE_CHECKING:
    from argparse import Namespace


_LOCAL_OPTIONS = (
    "data",
    "meta",
    "config",
    "profile",
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
        getattr(args, "meta", None),
        getattr(args, "patch", None),
    )
    if sum(source == "-" for source in sources) > 1:
        raise RenderingError(
            "stdin may be used by only one input source",
            code="stdin_conflict",
        )


def _has_value(args: Namespace, names: tuple[str, ...]) -> bool:
    return any(getattr(args, name, None) is not None for name in names)


def _selected_profile(args: Namespace) -> ProfileDefinition | None:
    name = getattr(args, "profile", None)
    config = getattr(args, "config", None)
    if name is None:
        if config is not None:
            raise RenderingError("--config requires --profile", code="renderer_option_conflict")
        return None
    if any(
        getattr(args, option, None) is not None
        for option in ("schema", "template", "table", "list", "columns", "title")
    ):
        raise RenderingError("--profile cannot be combined with definition overrides", code="renderer_option_conflict")
    return load_profile(name, config=config)


def _run_local_render(args: Namespace) -> int:
    _ensure_single_stdin(args)
    name = validate_slate_name(args.name)
    profile = _selected_profile(args)
    if profile is None and args.template is None and args.table is None and args.list is None:
        raise RenderingError(
            "local render requires --profile, --template, --table, or --list",
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
    if profile is not None:
        schema = profile.schema
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

    if profile is not None:
        descriptor = profile.renderer
    elif args.template is not None:
        descriptor = jinja_descriptor(_template_source(args.template), version=2)
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

    meta_file = getattr(args, "meta", None)
    meta = (
        None
        if meta_file is None
        else MetaSnapshot.from_json(
            strict_loads(_read_bytes(meta_file, subject="meta", max_bytes=DEFAULT_JSON_LIMITS.max_input_bytes))
        )
    )
    result = render(
        data,
        descriptor,
        schema=schema,
        slate=SlateContext(name=name),
        meta=meta,
    )
    if args.json:
        sys.stdout.write(
            canonical_json_bytes(
                {
                    "markdown": result.markdown,
                    "data": result.data,
                    "meta": None if result.meta is None else result.meta.to_json(),
                    "meta_source": "fixture" if meta_file is not None else "local",
                    "renderer": {"kind": result.renderer.kind, "version": result.renderer.version},
                    "profile": result.renderer.configuration.get("profile"),
                    "view": result.view,
                }
            ).decode("utf-8")
            + "\n"
        )
    else:
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
