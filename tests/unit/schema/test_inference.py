from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import cast

import pytest

from gh_slate.codec.json import canonical_json_bytes
from gh_slate.codec.model import JSON_SCHEMA_DIALECT_2020_12
from gh_slate.schema.errors import SchemaError
from gh_slate.schema.inference import infer_schema


def _document(data: object) -> dict[str, object]:
    document = infer_schema(data).to_json()["document"]
    assert isinstance(document, dict)
    return cast("dict[str, object]", document)


@pytest.mark.parametrize(
    "data",
    [None, True, "text", Decimal(1), ()],
)
def test_inference_requires_an_object_root(data: object) -> None:
    with pytest.raises(SchemaError) as caught:
        infer_schema(data)

    assert caught.value.code == "schema_data_root_not_object"
    assert caught.value.details["path"] == "$"


def test_infers_a_permissive_schema_for_nested_objects_and_arrays() -> None:
    document = _document(
        {
            "rows": (
                {
                    "name": "linux",
                    "score": Decimal(1),
                    "passed": True,
                },
                {
                    "name": "macos",
                    "score": Decimal("1.5"),
                    "note": None,
                },
            ),
            "empty": (),
        }
    )

    assert document == {
        "$schema": JSON_SCHEMA_DIALECT_2020_12,
        "type": "object",
        "properties": {
            "empty": {
                "type": "array",
                "items": {},
            },
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "note": {"type": "null"},
                        "passed": {"type": "boolean"},
                        "score": {"type": "number"},
                    },
                },
            },
        },
    }


def test_missing_object_properties_do_not_infer_null() -> None:
    items = cast(
        "dict[str, object]",
        cast(
            "dict[str, object]",
            _document({"rows": [{"value": Decimal(1)}, {}]})["properties"],
        )["rows"],
    )["items"]

    assert items == {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
    }


def test_array_types_have_stable_order_and_number_subsumes_integer() -> None:
    document = _document(
        {
            "mixed": (
                {},
                (),
                "text",
                Decimal("1.5"),
                Decimal(1),
                False,
                None,
            )
        }
    )
    mixed = cast(
        "dict[str, object]",
        cast("dict[str, object]", document["properties"])["mixed"],
    )

    assert mixed["items"] == {
        "type": [
            "null",
            "boolean",
            "number",
            "string",
            "array",
            "object",
        ],
        "properties": {},
        "items": {},
    }


def test_boolean_and_integral_decimal_remain_distinct_types() -> None:
    document = _document({"values": (True, Decimal("-0.000"), 2)})
    values = cast(
        "dict[str, object]",
        cast("dict[str, object]", document["properties"])["values"],
    )

    assert values["items"] == {"type": ["boolean", "integer"]}


def test_nested_arrays_merge_all_observed_item_shapes() -> None:
    document = _document({"matrix": ((), (Decimal(1),), ("x",))})
    matrix = cast(
        "dict[str, object]",
        cast("dict[str, object]", document["properties"])["matrix"],
    )

    assert matrix["items"] == {
        "type": "array",
        "items": {"type": ["integer", "string"]},
    }


def test_properties_use_unicode_code_point_order() -> None:
    keys = ("😀", "中", "ä", "z", "a")
    document = _document({key: key for key in keys})
    properties = cast("dict[str, object]", document["properties"])

    assert list(properties) == sorted(keys)


def test_accepts_immutable_codec_values() -> None:
    data = MappingProxyType(
        {
            "values": (
                MappingProxyType({"count": Decimal(2)}),
                MappingProxyType({"count": Decimal("2.25")}),
            )
        }
    )

    document = _document(data)

    assert document["properties"] == {
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"count": {"type": "number"}},
            },
        }
    }


def test_inference_is_independent_of_object_and_array_observation_order() -> None:
    first = {
        "rows": (
            {"b": Decimal(1)},
            {"a": "x", "b": Decimal("1.5")},
        ),
        "status": True,
    }
    second = {
        "status": True,
        "rows": (
            {"b": Decimal("1.5"), "a": "x"},
            {"b": Decimal(1)},
        ),
    }

    assert canonical_json_bytes(infer_schema(first).to_json()) == canonical_json_bytes(infer_schema(second).to_json())


def test_inference_does_not_invent_constraints() -> None:
    document = _document(
        {
            "url": "https://example.invalid",
            "status": "ok",
            "score": Decimal(42),
            "rows": ({"name": "a"}, {"name": "b"}),
        }
    )
    forbidden = {
        "additionalProperties",
        "enum",
        "format",
        "maximum",
        "minimum",
        "pattern",
        "required",
    }

    def assert_permissive(value: object) -> None:
        if isinstance(value, Mapping):
            assert forbidden.isdisjoint(value)
            for item in value.values():
                assert_permissive(item)
        elif isinstance(value, list):
            for item in value:
                assert_permissive(item)

    assert_permissive(document)
