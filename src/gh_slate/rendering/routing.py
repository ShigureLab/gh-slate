from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from jsonpointer import JsonPointer, JsonPointerException

from gh_slate.rendering.errors import RenderingError

if TYPE_CHECKING:
    from gh_slate.codec import RendererDescriptorV1


def validate_views(pointer: object, views: object) -> tuple[JsonPointer, Mapping[str, str]]:
    if not isinstance(pointer, str):
        raise RenderingError("view_by must be a JSON Pointer string", code="view_route_invalid")
    try:
        parsed = JsonPointer(pointer)
    except JsonPointerException as error:
        raise RenderingError("view_by is not a valid JSON Pointer", code="view_route_invalid") from error
    if (
        not isinstance(views, Mapping)
        or not views
        or any(
            not isinstance(name, str) or not name or len(name.encode("utf-8")) > 128 or not isinstance(source, str)
            for name, source in views.items()
        )
    ):
        raise RenderingError("views must map non-empty names to template text", code="view_route_invalid")
    return parsed, cast("Mapping[str, str]", views)


def selected_view(renderer: RendererDescriptorV1, data: object) -> str | None:
    if renderer.kind != "jinja" or renderer.version != 2 or "views" not in renderer.configuration:
        return None
    configuration = renderer.configuration
    pointer, views = validate_views(configuration.get("view_by"), configuration["views"])
    try:
        value = pointer.resolve(data)
    except JsonPointerException as error:
        raise RenderingError(
            "view_by does not resolve in the candidate data",
            code="view_route_missing",
            details={"view_by": configuration["view_by"]},
        ) from error
    if not isinstance(value, str):
        raise RenderingError(
            "view_by must resolve to a string",
            code="view_route_type",
            details={"view_by": configuration["view_by"], "value_type": type(value).__name__},
        )
    if value not in views:
        raise RenderingError(
            "candidate data does not select a configured view",
            code="view_not_found",
            details={"view": value, "available_views": sorted(views)},
        )
    return value


__all__ = ["selected_view", "validate_views"]
