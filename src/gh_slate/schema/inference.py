from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, TypeAlias, cast

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import JsonValue, freeze_json
from gh_slate.codec.model import (
    JSON_SCHEMA_DIALECT_2020_12,
    SchemaSnapshotV1,
)
from gh_slate.schema.errors import SchemaError
from gh_slate.schema.validation import _enforce_schema_size, _schema_size_error

_JsonType: TypeAlias = Literal[
    "null",
    "boolean",
    "integer",
    "number",
    "string",
    "array",
    "object",
]

_TYPE_ORDER: tuple[_JsonType, ...] = (
    "null",
    "boolean",
    "integer",
    "number",
    "string",
    "array",
    "object",
)


@dataclass(slots=True)
class _Shape:
    types: set[_JsonType]
    properties: dict[str, _Shape] = field(default_factory=dict)
    items: _Shape | None = None


def _number_type(value: Decimal) -> _JsonType:
    return "integer" if value == value.to_integral_value() else "number"


def _shape_for(value: JsonValue) -> _Shape:
    if value is None:
        return _Shape({"null"})
    if isinstance(value, bool):
        return _Shape({"boolean"})
    if isinstance(value, str):
        return _Shape({"string"})
    if isinstance(value, Decimal):
        return _Shape({_number_type(value)})
    if isinstance(value, Mapping):
        object_value = cast("Mapping[str, JsonValue]", value)
        properties = {key: _shape_for(object_value[key]) for key in sorted(object_value)}
        return _Shape({"object"}, properties=properties)
    if isinstance(value, tuple):
        items: _Shape | None = None
        for item in value:
            item_shape = _shape_for(item)
            items = item_shape if items is None else _merge(items, item_shape)
        return _Shape({"array"}, items=items)
    raise AssertionError(f"unexpected frozen JSON value: {type(value).__name__}")


def _merge(left: _Shape, right: _Shape) -> _Shape:
    left.types.update(right.types)
    if "number" in left.types:
        left.types.discard("integer")

    for key, right_property in right.properties.items():
        left_property = left.properties.get(key)
        if left_property is None:
            left.properties[key] = right_property
        else:
            _merge(left_property, right_property)

    if left.items is None:
        left.items = right.items
    elif right.items is not None:
        _merge(left.items, right.items)

    return left


def _schema_for(shape: _Shape) -> dict[str, object]:
    ordered_types = [json_type for json_type in _TYPE_ORDER if json_type in shape.types]
    schema: dict[str, object] = {"type": ordered_types[0] if len(ordered_types) == 1 else ordered_types}

    if "object" in shape.types:
        schema["properties"] = {key: _schema_for(shape.properties[key]) for key in sorted(shape.properties)}
    if "array" in shape.types:
        schema["items"] = {} if shape.items is None else _schema_for(shape.items)

    return schema


def infer_schema(data: object) -> SchemaSnapshotV1:
    """Infer a deterministic, permissive schema from an object data snapshot.

    Inference records only structural JSON types observed in ``data``. It does
    not make object properties required, close objects to additional
    properties, or infer semantic string and value constraints.
    """

    try:
        frozen = freeze_json(data)
    except CodecError as error:
        raise SchemaError(
            "data is not valid JSON",
            code="data_invalid",
            details={"cause_code": error.code},
        ) from None
    if not isinstance(frozen, Mapping):
        raise SchemaError(
            "data root must be a JSON object",
            code="schema_data_root_not_object",
            details={"path": "$", "value_type": type(frozen).__name__},
        )

    root = _shape_for(cast("Mapping[str, JsonValue]", frozen))
    document = {
        "$schema": JSON_SCHEMA_DIALECT_2020_12,
        **_schema_for(root),
    }
    try:
        snapshot = SchemaSnapshotV1(
            dialect=JSON_SCHEMA_DIALECT_2020_12,
            document=document,
        )
    except CodecError as error:
        raise _schema_size_error(error) from None
    _enforce_schema_size(snapshot)
    return snapshot


__all__ = ["infer_schema"]
