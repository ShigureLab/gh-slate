from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

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


@dataclass(frozen=True, slots=True)
class TableOptions:
    title: str | None = None
    columns: tuple[TableColumn, ...] = ()
    max_rows: int = DEFAULT_RENDER_LIMITS.max_table_rows
    missing: str = "—"

    def __post_init__(self) -> None:
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


@dataclass(frozen=True, slots=True)
class ListOptions:
    title: str | None = None
    max_depth: int = DEFAULT_LIST_DEPTH
    max_items: int = DEFAULT_LIST_ITEMS

    def __post_init__(self) -> None:
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
