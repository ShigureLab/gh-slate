from __future__ import annotations

from dataclasses import replace

import pytest

from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.limits import DEFAULT_RENDER_LIMITS
from gh_slate.rendering.list import render_list
from gh_slate.rendering.model import ListRendererV1


def test_object_keys_are_sorted_and_array_order_uses_index_labels() -> None:
    markdown = render_list(
        {
            "z": "last",
            "a": ("first", "second"),
        },
        ListRendererV1(),
    )

    assert markdown.splitlines() == [
        "- <code>a</code>:",
        "  - <code>&#91;0&#93;</code>: first",
        "  - <code>&#91;1&#93;</code>: second",
        "- <code>z</code>: last",
    ]


def test_empty_array_object_null_and_empty_string_are_distinct() -> None:
    markdown = render_list(
        ((), {}, None, ""),
        ListRendererV1(),
    )

    assert markdown.splitlines() == [
        "- <code>&#91;0&#93;</code>: <code>&#91;&#93;</code>",
        "- <code>&#91;1&#93;</code>: <code>&#123;&#125;</code>",
        "- <code>&#91;2&#93;</code>: null",
        "- <code>&#91;3&#93;</code>: <code>&#34;&#34;</code>",
    ]


def test_empty_root_containers_are_not_silently_dropped() -> None:
    assert render_list((), ListRendererV1()) == "- <code>&#91;&#93;</code>"
    assert render_list({}, ListRendererV1()) == "- <code>&#123;&#125;</code>"


def test_depth_limit_shows_compact_json_without_false_item_omission() -> None:
    markdown = render_list(
        {"a": {"b": {"c": "value"}}},
        ListRendererV1(max_depth=1),
    )

    assert markdown.splitlines() == [
        "- <code>a</code>:",
        ("  - <code>b</code>: <code>&#123;&#34;c&#34;&#58;&#34;value&#34;&#125;</code> _(depth limit)_"),
    ]
    assert "item limit" not in markdown


def test_runtime_depth_cap_cannot_be_raised_by_descriptor() -> None:
    markdown = render_list(
        {"a": {"b": {"c": "value"}}},
        ListRendererV1(max_depth=4),
        replace(DEFAULT_RENDER_LIMITS, max_list_depth=1),
    )

    assert "_(depth limit)_" in markdown


def test_item_limit_counts_rendered_list_entries_and_emits_notice() -> None:
    markdown = render_list(
        {"c": 3, "a": 1, "b": 2},
        ListRendererV1(max_items=2),
    )

    assert markdown.splitlines() == [
        "- <code>a</code>: 1",
        "- <code>b</code>: 2",
        "- _1 additional item(s) omitted (item limit)._",
    ]


def test_title_and_output_are_markdown_escaped() -> None:
    assert render_list(
        ("x",),
        ListRendererV1(title="<Title|`>"),
    ).startswith("## &#60;Title&#124;&#96;&#62;\n\n")


def test_list_neutralizes_markdown_in_title_keys_and_values() -> None:
    markdown = render_list(
        {"[key](https://example.com)": "![image](x) ~~value~~ @team"},
        ListRendererV1(title="**title** <tag>"),
    )

    assert markdown.splitlines() == [
        "## &#42;&#42;title&#42;&#42; &#60;tag&#62;",
        "",
        (
            "- <code>&#91;key&#93;&#40;https&#58;&#47;&#47;"
            "example&#46;com&#41;</code>: "
            "&#33;&#91;image&#93;&#40;x&#41; "
            "&#126;&#126;value&#126;&#126; &#64;team"
        ),
    ]


def test_list_output_limit_fails_explicitly() -> None:
    with pytest.raises(RenderingError) as caught:
        render_list(
            ("a long value",),
            ListRendererV1(),
            replace(DEFAULT_RENDER_LIMITS, max_output_bytes=8),
        )

    assert caught.value.code == "render_output_limit"
    assert caught.value.details["max_bytes"] == 8
