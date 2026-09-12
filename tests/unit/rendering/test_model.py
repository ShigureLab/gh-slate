from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.limits import (
    DEFAULT_RENDER_LIMITS,
    MAX_LIST_DEPTH,
    MAX_LIST_ITEMS,
    MAX_OUTPUT_BYTES,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    RenderLimits,
)
from gh_slate.rendering.model import (
    TableColumn,
)


@pytest.mark.parametrize(
    "path",
    [
        [],
        ("ok", -1),
        ("ok", True),
        ("ok", 2**31),
        ("ok", Decimal("1.5")),
    ],
)
def test_table_column_path_is_a_bounded_typed_tuple(path: object) -> None:
    with pytest.raises(RenderingError) as caught:
        TableColumn(path=path, header="Value")  # ty: ignore[invalid-argument-type]

    assert caught.value.code == "renderer_config_invalid"
    assert caught.value.details["field"] == "columns.path"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_table_rows", MAX_TABLE_ROWS + 1),
        ("max_table_columns", MAX_TABLE_COLUMNS + 1),
        ("max_list_depth", MAX_LIST_DEPTH + 1),
        ("max_list_items", MAX_LIST_ITEMS + 1),
        ("max_output_bytes", MAX_OUTPUT_BYTES + 1),
        ("max_table_rows", 0),
        ("max_list_items", True),
    ],
)
def test_render_limits_are_positive_integers_with_hard_upper_bounds(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        replace(DEFAULT_RENDER_LIMITS, **{field: value})


def test_render_limits_are_frozen() -> None:
    limits = RenderLimits()

    with pytest.raises((AttributeError, TypeError)):
        limits.max_table_rows = 1  # ty: ignore[invalid-assignment]
