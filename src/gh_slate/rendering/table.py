from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from gh_slate.codec.model import SchemaSnapshot
from gh_slate.codec.text import utf8_size
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.limits import DEFAULT_RENDER_LIMITS, RenderLimits
from gh_slate.rendering.markdown import MISSING, escape_markdown_text, render_value
from gh_slate.rendering.model import TableColumn, TableOptions


def _rows(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (tuple, list)):
        raise RenderingError(
            "table input must be an array",
            code="table_shape_invalid",
        )
    rows: list[Mapping[str, object]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise RenderingError(
                "table rows must all be objects",
                code="table_shape_invalid",
                details={"row": index, "value_type": type(row).__name__},
            )
        rows.append(cast("Mapping[str, object]", row))
    return tuple(rows)


def _schema_columns(schema: object) -> tuple[TableColumn, ...]:
    if isinstance(schema, SchemaSnapshot):
        schema = schema.document
    if not isinstance(schema, Mapping):
        return ()
    items = schema.get("items")
    if isinstance(items, Mapping):
        schema = items
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    columns: list[TableColumn] = []
    for key in properties:
        if not isinstance(key, str):
            return ()
        columns.append(TableColumn(path=(key,), header=key))
    return tuple(columns)


def resolve_table_renderer(
    renderer: TableOptions,
    rows: object,
    schema: object = None,
) -> TableOptions:
    typed_rows = _rows(rows)
    if renderer.columns:
        return renderer
    columns = _schema_columns(schema)
    if not columns:
        keys: set[str] = set()
        for row_index, row in enumerate(typed_rows):
            for key in row:
                if not isinstance(key, str):
                    raise RenderingError(
                        "table row keys must be strings",
                        code="table_shape_invalid",
                        details={
                            "row": row_index,
                            "key_type": type(key).__name__,
                        },
                    )
                keys.add(key)
        columns = tuple(TableColumn(path=(key,), header=key) for key in sorted(keys))
    return TableOptions(
        title=renderer.title,
        columns=columns,
        max_rows=renderer.max_rows,
        missing=renderer.missing,
    )


def _lookup(row: object, path: tuple[str | int, ...]) -> object:
    current = row
    for segment in path:
        if isinstance(segment, str):
            if not isinstance(current, Mapping) or segment not in current:
                return MISSING
            current = cast("Mapping[object, object]", current)[segment]
        else:
            if isinstance(current, tuple):
                if segment >= len(current):
                    return MISSING
                current = current[segment]
            elif isinstance(current, list):
                if segment >= len(current):
                    return MISSING
                current = current[segment]
            else:
                return MISSING
    return current


def _enforce_output(markdown: str, limits: RenderLimits) -> None:
    actual = utf8_size(markdown, field="rendered Markdown")
    if actual > limits.max_output_bytes:
        raise RenderingError(
            "rendered Markdown exceeds the configured byte limit",
            code="render_output_limit",
            details={
                "actual_bytes": actual,
                "max_bytes": limits.max_output_bytes,
            },
        )


def render_table(
    value: object,
    renderer: TableOptions,
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
) -> str:
    rows = _rows(value)
    if len(renderer.columns) > limits.max_table_columns:
        raise RenderingError(
            "table has too many columns",
            code="table_column_limit",
            details={
                "actual_columns": len(renderer.columns),
                "max_columns": limits.max_table_columns,
            },
        )
    row_limit = min(renderer.max_rows, limits.max_table_rows)
    if rows and not renderer.columns:
        raise RenderingError(
            "table columns could not be resolved",
            code="table_columns_unresolved",
            hints=("provide explicit table columns or a schema with item properties",),
        )

    lines: list[str] = []
    if renderer.title is not None:
        lines.extend((f"## {escape_markdown_text(renderer.title)}", ""))
    if not rows and not renderer.columns:
        lines.append("_No data._")
    else:
        lines.append("| " + " | ".join(escape_markdown_text(column.header) for column in renderer.columns) + " |")
        lines.append("| " + " | ".join("---" for _ in renderer.columns) + " |")
        for row in rows[:row_limit]:
            lines.append(
                "| "
                + " | ".join(
                    render_value(
                        _lookup(row, column.path),
                        missing=renderer.missing,
                    )
                    for column in renderer.columns
                )
                + " |"
            )
        omitted = max(0, len(rows) - row_limit)
        if omitted:
            lines.extend(("", f"_{omitted} additional row(s) omitted._"))

    markdown = "\n".join(lines)
    _enforce_output(markdown, limits)
    return markdown


__all__ = ["render_table", "resolve_table_renderer"]
