from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from gh_slate.codec.text import utf8_size
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.limits import DEFAULT_RENDER_LIMITS, RenderLimits
from gh_slate.rendering.markdown import code, escape_markdown_text, render_value

if TYPE_CHECKING:
    from gh_slate.rendering.model import ListRendererV1


def _item_count(value: object, *, depth: int, max_depth: int) -> int:
    count = 1
    if depth < max_depth and isinstance(value, Mapping) and value:
        children = value.values()
        count += sum(_item_count(item, depth=depth + 1, max_depth=max_depth) for item in children)
    elif depth < max_depth and isinstance(value, (tuple, list)) and value:
        children = value
        count += sum(_item_count(item, depth=depth + 1, max_depth=max_depth) for item in children)
    return count


def _entry_count(value: object, *, max_depth: int) -> int:
    if isinstance(value, Mapping) and value:
        return sum(_item_count(item, depth=0, max_depth=max_depth) for item in value.values())
    if isinstance(value, (tuple, list)) and value:
        return sum(_item_count(item, depth=0, max_depth=max_depth) for item in value)
    return 1


def _is_nonempty_container(value: object) -> bool:
    return isinstance(value, Mapping) and bool(value) or isinstance(value, (tuple, list)) and bool(value)


def _sorted_object_items(value: Mapping[object, object]) -> tuple[tuple[str, object], ...]:
    for key in value:
        if not isinstance(key, str):
            raise RenderingError(
                "builtin-list object keys must be strings",
                code="list_shape_invalid",
                details={"key_type": type(key).__name__},
            )
    typed = cast("Mapping[str, object]", value)
    return tuple((key, typed[key]) for key in sorted(typed))


@dataclass(slots=True)
class _ListState:
    lines: list[str]
    rendered: int
    limit: int
    max_depth: int


def _append(
    state: _ListState,
    value: object,
    *,
    label: str | None,
    depth: int,
) -> None:
    if state.rendered >= state.limit:
        return
    indent = "  " * depth
    prefix = "- " if label is None else f"- {label}: "
    state.rendered += 1

    if _is_nonempty_container(value):
        if depth >= state.max_depth:
            state.lines.append(f"{indent}{prefix}{render_value(value)} _(depth limit)_")
            return
        state.lines.append(f"{indent}{prefix}".rstrip())
        if isinstance(value, Mapping):
            for key, item in _sorted_object_items(cast("Mapping[object, object]", value)):
                _append(
                    state,
                    item,
                    label=code(str(key)),
                    depth=depth + 1,
                )
        elif isinstance(value, (tuple, list)):
            for index, item in enumerate(value):
                _append(
                    state,
                    item,
                    label=code(f"[{index}]"),
                    depth=depth + 1,
                )
        return

    state.lines.append(f"{indent}{prefix}{render_value(value)}")


def render_list(
    value: object,
    renderer: ListRendererV1,
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
) -> str:
    item_limit = min(renderer.max_items, limits.max_list_items)
    depth_limit = min(renderer.max_depth, limits.max_list_depth)
    state = _ListState(
        lines=[],
        rendered=0,
        limit=item_limit,
        max_depth=depth_limit,
    )
    if isinstance(value, Mapping) and value:
        for key, item in _sorted_object_items(cast("Mapping[object, object]", value)):
            _append(
                state,
                item,
                label=code(str(key)),
                depth=0,
            )
    elif isinstance(value, (tuple, list)) and value:
        for index, item in enumerate(value):
            _append(
                state,
                item,
                label=code(f"[{index}]"),
                depth=0,
            )
    else:
        _append(state, value, label=None, depth=0)
    total = _entry_count(value, max_depth=depth_limit)
    omitted = max(0, total - state.rendered)

    lines: list[str] = []
    if renderer.title is not None:
        lines.extend((f"## {escape_markdown_text(renderer.title)}", ""))
    lines.extend(state.lines)
    if omitted:
        lines.append(f"- _{omitted} additional item(s) omitted (item limit)._")
    markdown = "\n".join(lines)
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
    return markdown


__all__ = ["render_list"]
