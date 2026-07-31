from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gh_slate.rendering import (
    ListRendererV1,
    SlateContext,
    TableColumn,
    TableRendererV1,
    jinja_descriptor,
    render,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from gh_slate.codec import RendererDescriptorV1

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "rendering"
_CONTEXT = SlateContext(name="golden")


def _table_case() -> tuple[object, RendererDescriptorV1]:
    data = {
        "jobs": [
            {
                "name": "linux|arm",
                "status": None,
                "empty": "",
                "nested": {"b": 2, "a": 1},
                "path": "a\\b`c\nnext",
            },
            {
                "status": "passed",
                "empty": "value",
                "nested": [],
                "path": "",
            },
        ]
    }
    columns = tuple(TableColumn(path=(name,), header=name) for name in ("name", "status", "empty", "nested", "path"))
    descriptor = TableRendererV1(
        selector=".jobs",
        title="Matrix <main>",
        columns=columns,
    ).to_descriptor()
    return data, descriptor


def _list_case() -> tuple[object, RendererDescriptorV1]:
    data = {
        "changes": {
            "z": "last",
            "a": ("first", {"nested": None}),
            "empty": (),
            "blank": "",
        }
    }
    return data, ListRendererV1(
        selector=".changes",
        title="Release | notes",
    ).to_descriptor()


def _jinja_case() -> tuple[object, RendererDescriptorV1]:
    data = {
        "jobs": [{"name": "linux|x64", "ok": True}],
        "meta": {"z": None, "a": 1},
    }
    source = (
        '# {{ slate.name }}\n\n{{ data.jobs | md_table(columns=["name", "ok"]) }}\n\n{{ data.meta | compact_json }}'
    )
    return data, jinja_descriptor(source)


@pytest.mark.parametrize(
    ("fixture_name", "case"),
    [
        ("table.md", _table_case),
        ("list.md", _list_case),
        ("jinja.md", _jinja_case),
    ],
)
def test_renderer_golden_is_byte_stable(
    fixture_name: str,
    case: Callable[[], tuple[object, RendererDescriptorV1]],
) -> None:
    data, descriptor = case()
    expected = (_FIXTURES / fixture_name).read_text(encoding="utf-8")

    first = render(data, descriptor, slate=_CONTEXT).markdown
    second = render(data, descriptor, slate=_CONTEXT).markdown

    assert first == second == expected
