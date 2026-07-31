from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType

import pytest

from gh_slate.codec.model import JSON_SCHEMA_DIALECT_2020_12, SchemaSnapshotV1
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.limits import DEFAULT_RENDER_LIMITS
from gh_slate.rendering.model import TableColumn, TableRendererV1
from gh_slate.rendering.table import render_table, resolve_table_renderer


def _column(name: str) -> TableColumn:
    return TableColumn(path=(name,), header=name)


def test_table_distinguishes_missing_null_empty_and_nested_json() -> None:
    renderer = TableRendererV1(
        selector=".",
        columns=tuple(
            _column(name)
            for name in (
                "missing",
                "null",
                "empty",
                "bool",
                "number",
                "nested",
            )
        ),
    )
    rows = (
        MappingProxyType(
            {
                "null": None,
                "empty": "",
                "bool": False,
                "number": Decimal("12.500"),
                "nested": MappingProxyType({"text": "a|b\n`c`"}),
            }
        ),
    )

    markdown = render_table(rows, renderer)

    assert (
        "| — | null | <code>&#34;&#34;</code> | false | 12.5 | "
        "<code>&#123;&#34;text&#34;&#58;&#34;a&#124;b&#92;n"
        "&#96;c&#96;&#34;&#125;</code> |"
    ) in markdown


def test_typed_column_path_traverses_objects_and_array_indexes() -> None:
    renderer = TableRendererV1(
        columns=(
            TableColumn(
                path=("jobs", 0, "name"),
                header="First",
            ),
        )
    )

    assert render_table(({"jobs": ({"name": "linux"},)},), renderer).endswith("| linux |")


def test_explicit_columns_win_over_schema_and_observed_rows() -> None:
    renderer = TableRendererV1(columns=(_column("chosen"),))

    assert (
        resolve_table_renderer(
            renderer,
            ({"other": 1},),
            {"properties": {"schema": {"type": "number"}}},
        )
        is renderer
    )


def test_column_resolution_uses_schema_order_then_sorted_observed_union() -> None:
    unresolved = TableRendererV1()
    schema = SchemaSnapshotV1(
        dialect=JSON_SCHEMA_DIALECT_2020_12,
        document={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "z": {"type": "string"},
                    "a": {"type": "number"},
                },
            },
        },
    )

    from_schema = resolve_table_renderer(unresolved, (), schema)
    inferred = resolve_table_renderer(
        unresolved,
        ({"z": 1, "b": 2}, {"a": 3}),
    )

    assert [column.header for column in from_schema.columns] == ["z", "a"]
    assert [column.header for column in inferred.columns] == ["a", "b", "z"]


def test_empty_array_with_columns_renders_an_empty_table() -> None:
    renderer = TableRendererV1(columns=(_column("name"), _column("status")))

    assert render_table((), renderer) == ("| name | status |\n| --- | --- |")
    assert render_table((), TableRendererV1()) == "_No data._"


def test_table_neutralizes_markdown_in_all_untrusted_text_slots() -> None:
    renderer = TableRendererV1(
        title="**title** <tag>",
        columns=(
            TableColumn(
                path=("value",),
                header="[header](https://example.com)",
            ),
            TableColumn(
                path=("missing",),
                header="!missing!",
            ),
        ),
        missing="![missing](https://example.com)",
    )

    markdown = render_table(
        ({"value": "~~value~~ @team https://example.com"},),
        renderer,
    )

    assert "&#42;&#42;title&#42;&#42; &#60;tag&#62;" in markdown
    assert "&#91;header&#93;&#40;https&#58;&#47;&#47;example&#46;com&#41;" in markdown
    assert "&#33;missing&#33;" in markdown
    assert ("&#126;&#126;value&#126;&#126; &#64;team https&#58;&#47;&#47;example&#46;com") in markdown
    assert ("&#33;&#91;missing&#93;&#40;https&#58;&#47;&#47;example&#46;com&#41;") in markdown


def test_table_requires_an_array_of_objects_and_resolved_nonempty_columns() -> None:
    for value in (None, {"a": 1}, ({"a": 1}, "bad")):
        with pytest.raises(RenderingError) as caught:
            render_table(value, TableRendererV1(columns=(_column("a"),)))
        assert caught.value.code == "table_shape_invalid"

    with pytest.raises(RenderingError) as unresolved:
        render_table(({"a": 1},), TableRendererV1())
    assert unresolved.value.code == "table_columns_unresolved"


def test_row_limit_is_explicit_and_respects_runtime_hard_cap() -> None:
    renderer = TableRendererV1(
        columns=(_column("value"),),
        max_rows=3,
    )
    limits = replace(DEFAULT_RENDER_LIMITS, max_table_rows=1)

    markdown = render_table(
        ({"value": 1}, {"value": 2}, {"value": 3}),
        renderer,
        limits,
    )

    assert "| 1 |" in markdown
    assert "| 2 |" not in markdown
    assert "_2 additional row(s) omitted._" in markdown


def test_table_column_and_output_limits_fail_explicitly() -> None:
    renderer = TableRendererV1(
        columns=(_column("a"), _column("b")),
    )
    with pytest.raises(RenderingError) as columns_error:
        render_table(
            ({"a": 1, "b": 2},),
            renderer,
            replace(DEFAULT_RENDER_LIMITS, max_table_columns=1),
        )
    assert columns_error.value.code == "table_column_limit"

    with pytest.raises(RenderingError) as output_error:
        render_table(
            ({"a": "long"},),
            TableRendererV1(columns=(_column("a"),)),
            replace(DEFAULT_RENDER_LIMITS, max_output_bytes=8),
        )
    assert output_error.value.code == "render_output_limit"
    assert output_error.value.details["max_bytes"] == 8
