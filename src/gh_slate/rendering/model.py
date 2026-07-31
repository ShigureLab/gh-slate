from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias, cast

from gh_slate.codec.model import RendererDescriptorV1
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.limits import (
    DEFAULT_RENDER_LIMITS,
    MAX_LIST_DEPTH,
    MAX_LIST_ITEMS,
    MAX_TABLE_ROWS,
)

PathSegment: TypeAlias = str | int
MAX_PATH_INDEX = 2**31 - 1
DEFAULT_LIST_DEPTH = 4
DEFAULT_LIST_ITEMS = 500


def _error(message: str, *, field: str) -> RenderingError:
    return RenderingError(
        message,
        code="renderer_config_invalid",
        details={"field": field},
    )


def _string(value: object, *, field: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise _error(f"{field} must be a string", field=field)
    return value


def _integer(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise _error(f"{field} must be an integer", field=field)
    if isinstance(value, int):
        result = value
    elif (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
        and Decimal(minimum) <= value <= Decimal(maximum)
    ):
        result = int(value)
    else:
        raise _error(f"{field} must be an integer", field=field)
    if not minimum <= result <= maximum:
        raise _error(
            f"{field} must be between {minimum} and {maximum}",
            field=field,
        )
    return result


def _strict_config(
    descriptor: RendererDescriptorV1,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> Mapping[str, object]:
    config = descriptor.configuration
    unknown = sorted(set(config) - allowed)
    missing = sorted(required - set(config))
    if unknown or missing:
        raise RenderingError(
            "renderer configuration fields do not match its version",
            code="renderer_config_invalid",
            details={"unknown_fields": unknown, "missing_fields": missing},
        )
    return config


@dataclass(frozen=True, slots=True)
class TableColumn:
    path: tuple[PathSegment, ...]
    header: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, tuple):
            raise _error("table column path must be a tuple", field="columns.path")
        if not self.path:
            raise _error("table column path must not be empty", field="columns.path")
        normalized: list[PathSegment] = []
        for segment in self.path:
            if isinstance(segment, str):
                normalized.append(segment)
            elif isinstance(segment, int) and not isinstance(segment, bool) and 0 <= segment <= MAX_PATH_INDEX:
                normalized.append(segment)
            else:
                raise _error(
                    f"table path segments must be strings or integers between 0 and {MAX_PATH_INDEX}",
                    field="columns.path",
                )
        if not isinstance(self.header, str) or not self.header:
            raise _error(
                "table column header must be a non-empty string",
                field="columns.header",
            )
        object.__setattr__(self, "path", tuple(normalized))

    def to_json(self) -> dict[str, object]:
        return {"path": list(self.path), "header": self.header}

    @classmethod
    def from_json(cls, value: object) -> TableColumn:
        if not isinstance(value, Mapping) or set(value) != {"path", "header"}:
            raise _error(
                "table column must contain only path and header",
                field="columns",
            )
        typed_value = cast("Mapping[str, object]", value)
        path = typed_value["path"]
        if not isinstance(path, tuple):
            raise _error("table column path must be an array", field="columns.path")
        normalized: list[PathSegment] = []
        for segment in path:
            if isinstance(segment, str):
                normalized.append(segment)
            else:
                normalized.append(
                    _integer(
                        segment,
                        field="columns.path",
                        minimum=0,
                        maximum=MAX_PATH_INDEX,
                    )
                )
        header = _string(typed_value["header"], field="columns.header")
        assert header is not None
        return cls(path=tuple(normalized), header=header)


@dataclass(frozen=True, slots=True)
class TableRendererV1:
    selector: str = "."
    title: str | None = None
    columns: tuple[TableColumn, ...] = ()
    max_rows: int = DEFAULT_RENDER_LIMITS.max_table_rows
    missing: str = "—"

    def __post_init__(self) -> None:
        if not isinstance(self.selector, str) or not self.selector.strip():
            raise _error(
                "table selector must be a non-empty string",
                field="selector",
            )
        if self.title is not None and not isinstance(self.title, str):
            raise _error("table title must be a string or null", field="title")
        if not isinstance(self.columns, tuple) or not all(isinstance(column, TableColumn) for column in self.columns):
            raise _error("table columns must be TableColumn values", field="columns")
        if (
            isinstance(self.max_rows, bool)
            or not isinstance(self.max_rows, int)
            or not 1 <= self.max_rows <= MAX_TABLE_ROWS
        ):
            raise _error(
                f"max_rows must be between 1 and {MAX_TABLE_ROWS}",
                field="max_rows",
            )
        if not isinstance(self.missing, str) or not self.missing:
            raise _error("missing marker must be a non-empty string", field="missing")

    def to_descriptor(self) -> RendererDescriptorV1:
        return RendererDescriptorV1(
            kind="builtin-table",
            version=1,
            config={
                "selector": self.selector,
                "title": self.title,
                "columns": [column.to_json() for column in self.columns],
                "max_rows": self.max_rows,
                "missing": self.missing,
            },
        )

    @classmethod
    def from_descriptor(cls, descriptor: RendererDescriptorV1) -> TableRendererV1:
        renderer = parse_renderer_descriptor(descriptor)
        if not isinstance(renderer, cls):
            raise RenderingError(
                "renderer descriptor is not a builtin-table@1 descriptor",
                code="renderer_unsupported",
                details={"kind": descriptor.kind, "version": descriptor.version},
            )
        return renderer


@dataclass(frozen=True, slots=True)
class ListRendererV1:
    selector: str = "."
    title: str | None = None
    max_depth: int = DEFAULT_LIST_DEPTH
    max_items: int = DEFAULT_LIST_ITEMS

    def __post_init__(self) -> None:
        if not isinstance(self.selector, str) or not self.selector.strip():
            raise _error(
                "list selector must be a non-empty string",
                field="selector",
            )
        if self.title is not None and not isinstance(self.title, str):
            raise _error("list title must be a string or null", field="title")
        if (
            isinstance(self.max_depth, bool)
            or not isinstance(self.max_depth, int)
            or not 1 <= self.max_depth <= MAX_LIST_DEPTH
        ):
            raise _error(
                f"max_depth must be between 1 and {MAX_LIST_DEPTH}",
                field="max_depth",
            )
        if (
            isinstance(self.max_items, bool)
            or not isinstance(self.max_items, int)
            or not 1 <= self.max_items <= MAX_LIST_ITEMS
        ):
            raise _error(
                f"max_items must be between 1 and {MAX_LIST_ITEMS}",
                field="max_items",
            )

    def to_descriptor(self) -> RendererDescriptorV1:
        return RendererDescriptorV1(
            kind="builtin-list",
            version=1,
            config={
                "selector": self.selector,
                "title": self.title,
                "max_depth": self.max_depth,
                "max_items": self.max_items,
            },
        )

    @classmethod
    def from_descriptor(cls, descriptor: RendererDescriptorV1) -> ListRendererV1:
        renderer = parse_renderer_descriptor(descriptor)
        if not isinstance(renderer, cls):
            raise RenderingError(
                "renderer descriptor is not a builtin-list@1 descriptor",
                code="renderer_unsupported",
                details={"kind": descriptor.kind, "version": descriptor.version},
            )
        return renderer


RendererV1: TypeAlias = TableRendererV1 | ListRendererV1


def parse_renderer_descriptor(descriptor: RendererDescriptorV1) -> RendererV1:
    if not isinstance(descriptor, RendererDescriptorV1):
        raise RenderingError(
            "renderer must be a RendererDescriptorV1",
            code="renderer_config_invalid",
        )
    if descriptor.version != 1:
        raise RenderingError(
            "renderer version is not supported for rendering",
            code="renderer_unsupported",
            details={
                "kind": descriptor.kind,
                "version": descriptor.version,
            },
        )

    if descriptor.kind == "builtin-table":
        config = _strict_config(
            descriptor,
            allowed=frozenset({"selector", "title", "columns", "max_rows", "missing"}),
            required=frozenset({"selector", "columns"}),
        )
        columns = config["columns"]
        if not isinstance(columns, tuple):
            raise _error("table columns must be an array", field="columns")
        selector = _string(config["selector"], field="selector")
        assert selector is not None
        title = _string(config.get("title"), field="title", optional=True)
        missing = _string(config.get("missing", "—"), field="missing")
        assert missing is not None
        return TableRendererV1(
            selector=selector,
            title=title if "title" in config else None,
            columns=tuple(
                TableColumn(path=(item,), header=item) if isinstance(item, str) else TableColumn.from_json(item)
                for item in columns
            ),
            max_rows=_integer(
                config.get("max_rows", DEFAULT_RENDER_LIMITS.max_table_rows),
                field="max_rows",
                minimum=1,
                maximum=MAX_TABLE_ROWS,
            ),
            missing=missing if "missing" in config else "—",
        )

    if descriptor.kind == "builtin-list":
        config = _strict_config(
            descriptor,
            allowed=frozenset({"selector", "title", "max_depth", "max_items"}),
            required=frozenset({"selector"}),
        )
        selector = _string(config["selector"], field="selector")
        assert selector is not None
        return ListRendererV1(
            selector=selector,
            title=_string(config.get("title"), field="title", optional=True),
            max_depth=_integer(
                config.get("max_depth", DEFAULT_LIST_DEPTH),
                field="max_depth",
                minimum=1,
                maximum=MAX_LIST_DEPTH,
            ),
            max_items=_integer(
                config.get("max_items", DEFAULT_LIST_ITEMS),
                field="max_items",
                minimum=1,
                maximum=MAX_LIST_ITEMS,
            ),
        )

    raise RenderingError(
        "renderer kind is not supported for rendering",
        code="renderer_unsupported",
        details={"kind": descriptor.kind, "version": descriptor.version},
    )


__all__ = [
    "ListRendererV1",
    "MAX_PATH_INDEX",
    "PathSegment",
    "RendererV1",
    "TableColumn",
    "TableRendererV1",
    "parse_renderer_descriptor",
]
