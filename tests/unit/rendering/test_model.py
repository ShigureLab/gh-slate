from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from gh_slate.codec.model import RendererDescriptorV1
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
    ListRendererV1,
    TableColumn,
    TableRendererV1,
    parse_renderer_descriptor,
)


def test_typed_table_descriptor_round_trips_with_explicit_defaults() -> None:
    renderer = TableRendererV1(
        selector=".jobs",
        columns=(
            TableColumn(path=("name",), header="Job"),
            TableColumn(path=("steps", 0, "status"), header="First step"),
        ),
    )

    descriptor = renderer.to_descriptor()

    assert descriptor.to_json() == {
        "kind": "builtin-table",
        "version": 1,
        "selector": ".jobs",
        "title": None,
        "columns": [
            {"path": ["name"], "header": "Job"},
            {"path": ["steps", 0, "status"], "header": "First step"},
        ],
        "max_rows": Decimal(DEFAULT_RENDER_LIMITS.max_table_rows),
        "missing": "—",
    }
    assert TableRendererV1.from_descriptor(descriptor) == renderer


def test_legacy_string_columns_remain_renderable() -> None:
    descriptor = RendererDescriptorV1(
        kind="builtin-table",
        version=1,
        config={
            "selector": ".jobs",
            "columns": ["name", "passed"],
        },
    )

    renderer = parse_renderer_descriptor(descriptor)

    assert renderer == TableRendererV1(
        selector=".jobs",
        columns=(
            TableColumn(path=("name",), header="name"),
            TableColumn(path=("passed",), header="passed"),
        ),
    )
    assert set(renderer.to_descriptor().configuration) == {
        "selector",
        "title",
        "columns",
        "max_rows",
        "missing",
    }


def test_list_descriptor_supplies_and_serializes_v1_defaults() -> None:
    descriptor = RendererDescriptorV1(
        kind="builtin-list",
        version=1,
        config={"selector": ".changes"},
    )

    renderer = ListRendererV1.from_descriptor(descriptor)

    assert renderer == ListRendererV1(selector=".changes")
    assert renderer.to_descriptor().to_json() == {
        "kind": "builtin-list",
        "version": 1,
        "selector": ".changes",
        "title": None,
        "max_depth": Decimal(4),
        "max_items": Decimal(500),
    }


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
    "descriptor",
    [
        RendererDescriptorV1(
            kind="builtin-table",
            version=1,
            config={"selector": ".", "columns": [], "surprise": True},
        ),
        RendererDescriptorV1(
            kind="builtin-table",
            version=1,
            config={"selector": "."},
        ),
        RendererDescriptorV1(
            kind="builtin-table",
            version=1,
            config={
                "selector": ".",
                "columns": [{"path": ["value"], "header": "Value", "extra": 1}],
            },
        ),
        RendererDescriptorV1(
            kind="builtin-table",
            version=2,
            config={"selector": ".", "columns": []},
        ),
        RendererDescriptorV1(
            kind="future-renderer",
            version=1,
            config={"selector": "."},
        ),
    ],
)
def test_descriptor_parser_rejects_unknown_malformed_or_unsupported_versions(
    descriptor: RendererDescriptorV1,
) -> None:
    with pytest.raises(RenderingError):
        parse_renderer_descriptor(descriptor)


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
