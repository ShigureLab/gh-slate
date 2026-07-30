from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType
from typing import cast

import pytest

from gh_slate.codec.model import JSON_SCHEMA_DIALECT_2020_12, SchemaSnapshotV1
from gh_slate.schema.errors import SchemaError
from gh_slate.schema.validation import (
    DEFAULT_MAX_ERRORS,
    MAX_DIAGNOSTIC_POINTER_BYTES,
    MAX_ERRORS_LIMIT,
    replace_schema,
    validate_data,
    validate_data_json,
    validate_schema,
    validate_schema_json,
)


def test_validates_codec_containers_and_decimal_json_types() -> None:
    data = MappingProxyType(
        {
            "count": Decimal("2.0"),
            "ratio": Decimal("1.25"),
            "items": (MappingProxyType({"ok": True}),),
        }
    )
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "ratio": {"type": "number", "multipleOf": Decimal("0.25")},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                },
            },
        },
    }

    validated = validate_data(data, schema)

    assert validated == data
    assert isinstance(validated, MappingProxyType)
    assert isinstance(validated["items"], tuple)


def test_boolean_is_not_an_integer() -> None:
    with pytest.raises(SchemaError) as captured:
        validate_data({"value": True}, {"properties": {"value": {"type": "integer"}}})

    assert captured.value.code == "schema_validation_failed"
    assert captured.value.diagnostics[0].data_pointer == "/value"
    assert captured.value.diagnostics[0].keyword == "type"


def test_decimal_multiple_of_is_exact() -> None:
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "number", "multipleOf": Decimal("0.1")}},
    }

    assert validate_data({"amount": Decimal("0.3")}, schema)["amount"] == Decimal("0.3")

    with pytest.raises(SchemaError) as captured:
        validate_data({"amount": Decimal("0.31")}, schema)
    assert captured.value.diagnostics[0].keyword == "multipleOf"


def test_regex_keywords_are_bounded_and_preserve_normal_semantics() -> None:
    validate_data(
        {"name": "release-42", "tag-one": "ok"},
        {
            "type": "object",
            "properties": {"name": {"type": "string", "pattern": r"^release-\d+$"}},
            "patternProperties": {r"^tag-": {"type": "string"}},
            "additionalProperties": False,
        },
    )

    with pytest.raises(SchemaError) as captured:
        validate_data(
            {"name": ("a" * 10_000) + "!"},
            {
                "properties": {
                    "name": {
                        "type": "string",
                        "pattern": "(a|aa)+$",
                    }
                }
            },
        )

    assert captured.value.code == "schema_evaluation_limit"


def test_pattern_properties_and_unevaluated_properties_share_safe_matching() -> None:
    schema = {
        "type": "object",
        "patternProperties": {r"^metric_": {"type": "number"}},
        "unevaluatedProperties": False,
    }

    validate_data({"metric_latency": Decimal("1.25")}, schema)

    with pytest.raises(SchemaError) as captured:
        validate_data({"other": Decimal(1)}, schema)
    assert captured.value.diagnostics[0].keyword == "unevaluatedProperties"


def test_unique_items_uses_linear_canonical_json_identity() -> None:
    schema = {
        "properties": {
            "items": {
                "type": "array",
                "uniqueItems": True,
            }
        }
    }
    validate_data({"items": [{"id": index} for index in range(3_000)]}, schema)

    with pytest.raises(SchemaError) as captured:
        validate_data({"items": [{"id": 1}, {"id": Decimal("1.0")}]}, schema)
    assert captured.value.diagnostics[0].keyword == "uniqueItems"


@pytest.mark.parametrize("document", [True, {"type": "object"}])
def test_boolean_and_object_schemas_are_valid(document: bool | dict[str, object]) -> None:
    snapshot = validate_schema(document)

    assert snapshot.document == document
    assert snapshot.dialect == JSON_SCHEMA_DIALECT_2020_12


@pytest.mark.parametrize(
    ("source", "cause_code"),
    [
        ('{"type":"object","type":"string"}', "json_duplicate_key"),
        (b'{"multipleOf":NaN}', "json_number_invalid"),
        (b'{"title":"\xff"}', "json_invalid"),
    ],
)
def test_schema_json_ingestion_is_strict(source: str | bytes, cause_code: str) -> None:
    with pytest.raises(SchemaError) as captured:
        validate_schema_json(source)

    assert captured.value.code == "schema_invalid"
    assert captured.value.details["cause_code"] == cause_code


def test_schema_json_ingestion_accepts_boolean_schema() -> None:
    assert validate_schema_json("true").document is True


def test_false_schema_rejects_object_data() -> None:
    with pytest.raises(SchemaError) as captured:
        validate_data({}, False)

    assert captured.value.code == "schema_validation_failed"
    assert captured.value.diagnostics[0].data_pointer == ""
    assert captured.value.diagnostics[0].schema_pointer == ""


@pytest.mark.parametrize(
    "document",
    [
        {"type": "not-a-type"},
        {"required": "name"},
        {"minLength": Decimal(-1)},
    ],
)
def test_invalid_schema_is_wrapped_as_structured_error(document: dict[str, object]) -> None:
    with pytest.raises(SchemaError) as captured:
        validate_schema(document)

    assert captured.value.code == "schema_invalid"
    assert captured.value.diagnostics
    assert captured.value.diagnostics[0].code == "invalid_schema"
    assert not isinstance(captured.value.__cause__, Exception)


@pytest.mark.parametrize(
    ("dialect", "document", "code"),
    [
        (
            "https://json-schema.org/draft/2019-09/schema",
            {},
            "schema_dialect_unsupported",
        ),
        (
            JSON_SCHEMA_DIALECT_2020_12,
            {"$schema": "https://json-schema.org/draft/2020-12/schema#"},
            "schema_dialect_mismatch",
        ),
        (
            JSON_SCHEMA_DIALECT_2020_12,
            {"$schema": None},
            "schema_dialect_mismatch",
        ),
    ],
)
def test_dialect_must_match_exactly(
    dialect: str,
    document: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(SchemaError) as captured:
        validate_schema(document, dialect=dialect)

    assert captured.value.code == code


def test_missing_and_null_are_distinct() -> None:
    schema = {
        "type": "object",
        "required": ["note"],
        "properties": {"note": {"type": "string"}},
    }

    with pytest.raises(SchemaError) as missing:
        validate_data({}, schema)
    with pytest.raises(SchemaError) as null:
        validate_data({"note": None}, schema)

    assert missing.value.diagnostics[0].data_pointer == ""
    assert missing.value.diagnostics[0].keyword == "required"
    assert null.value.diagnostics[0].data_pointer == "/note"
    assert null.value.diagnostics[0].keyword == "type"


def test_data_root_must_be_an_object_even_when_schema_accepts_everything() -> None:
    for data in (None, (), "value", Decimal(1)):
        with pytest.raises(SchemaError) as captured:
            validate_data(data, True)
        assert captured.value.code == "schema_data_root_not_object"
        assert captured.value.diagnostics[0].data_pointer == ""


@pytest.mark.parametrize(
    ("source", "cause_code"),
    [
        ('{"value":1,"value":2}', "json_duplicate_key"),
        (b'{"value":Infinity}', "json_number_invalid"),
        (b'{"value":"\xff"}', "json_invalid"),
    ],
)
def test_data_json_ingestion_is_strict(source: str | bytes, cause_code: str) -> None:
    with pytest.raises(SchemaError) as captured:
        validate_data_json(source)

    assert captured.value.code == "data_invalid"
    assert captured.value.details["cause_code"] == cause_code


def test_data_json_ingestion_validates_schema_and_object_root() -> None:
    assert validate_data_json('{"count":2}', {"properties": {"count": {"type": "integer"}}}) == {"count": Decimal(2)}

    with pytest.raises(SchemaError) as captured:
        validate_data_json("[]")
    assert captured.value.code == "schema_data_root_not_object"


def test_local_defs_anchor_and_dynamic_ref_are_supported() -> None:
    schema = {
        "$defs": {
            "label": {"$anchor": "label", "type": "string"},
            "node": {
                "$dynamicAnchor": "node",
                "type": "object",
                "properties": {"child": {"$dynamicRef": "#node"}},
            },
        },
        "type": "object",
        "required": ["name", "tree"],
        "properties": {
            "name": {"$ref": "#label"},
            "tree": {"$ref": "#/$defs/node"},
        },
    }

    validate_data({"name": "root", "tree": {"child": {}}}, schema)


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/schema.json",
        "http://127.0.0.1/schema.json",
        "file:///etc/passwd",
        "../schema.json",
        "schema.json#thing",
        "/tmp/schema.json",
        r"\\server\share\schema.json",
    ],
)
def test_nonlocal_refs_in_dead_branches_are_rejected_without_retrieval(
    reference: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_io(*_args: object, **_kwargs: object) -> None:
        pytest.fail("schema validation attempted external I/O")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_io)
    schema = {
        "allOf": [
            False,
            {"if": False, "then": {"$ref": reference}},
        ]
    }

    with pytest.raises(SchemaError) as captured:
        validate_schema(schema)

    assert captured.value.code == "schema_reference_forbidden"
    assert captured.value.diagnostics[0].schema_pointer == "/allOf/1/then/$ref"


def test_ref_shaped_instance_data_in_annotations_is_not_scanned() -> None:
    schema = {
        "type": "object",
        "const": {"$ref": "https://example.invalid/not-a-schema"},
        "examples": [{"$ref": "file:///not-a-schema"}],
    }

    validate_schema(schema)


def test_empty_ref_is_a_legal_same_document_reference() -> None:
    snapshot = validate_schema({"$ref": ""})

    assert snapshot.document == {"$ref": ""}


def test_nested_schema_dialect_must_also_match_exactly() -> None:
    with pytest.raises(SchemaError) as captured:
        validate_schema(
            {
                "properties": {
                    "value": {
                        "$schema": "https://json-schema.org/draft/2019-09/schema",
                    }
                }
            }
        )

    assert captured.value.code == "schema_dialect_mismatch"
    assert captured.value.diagnostics[0].schema_pointer == "/properties/value/$schema"


def test_integral_decimal_metaschema_projection_is_bounded_and_semantic() -> None:
    enormous = Decimal("1e+999999999")
    snapshot = validate_schema(
        {
            "properties": {
                "items": {
                    "type": "array",
                    "maxItems": enormous,
                }
            },
            "$defs": {
                "choices": {
                    "enum": [
                        {"maxItems": Decimal(1)},
                        {"maxItems": Decimal(2)},
                    ]
                }
            },
        }
    )

    document = cast("dict[str, object]", snapshot.to_json()["document"])
    assert document["$defs"] == {
        "choices": {
            "enum": [
                {"maxItems": Decimal(1)},
                {"maxItems": Decimal(2)},
            ]
        }
    }
    validate_data({"items": []}, snapshot)


def test_extreme_decimal_constraint_does_not_leak_a_native_exception() -> None:
    with pytest.raises(SchemaError) as captured:
        validate_data(
            {"value": Decimal(1)},
            {
                "properties": {
                    "value": {
                        "multipleOf": Decimal("1e-999999999"),
                    }
                }
            },
        )

    assert captured.value.code == "schema_evaluation_failed"
    assert captured.value.__cause__ is None


def test_unresolved_local_ref_is_wrapped_without_native_exception() -> None:
    with pytest.raises(SchemaError) as captured:
        validate_data({}, {"$ref": "#/$defs/missing"})

    assert captured.value.code == "schema_resolution_failed"
    assert captured.value.diagnostics == ()
    assert captured.value.__cause__ is None


def test_diagnostics_are_rfc6901_sorted_and_truncated() -> None:
    schema = {
        "type": "object",
        "properties": {
            "z": {"type": "string"},
            "a/b": {"type": "integer"},
            "a~b": {"type": "boolean"},
        },
    }

    with pytest.raises(SchemaError) as captured:
        validate_data(
            {"z": False, "a/b": "wrong", "a~b": None},
            schema,
            max_errors=2,
        )

    error = captured.value
    assert [item.data_pointer for item in error.diagnostics] == ["/a~0b", "/a~1b"]
    assert error.truncated is True
    assert error.details["truncated"] is True
    assert error.details["error_count"] == 3
    assert error.details["max_errors"] == 2
    assert error.as_dict()["error"]["details"]["diagnostics"] == [item.as_dict() for item in error.diagnostics]


def test_diagnostic_pointers_have_an_explicit_memory_bound() -> None:
    long_key = "key/" + ("雪~" * 2_000)

    with pytest.raises(SchemaError) as captured:
        validate_data(
            {long_key: None},
            {
                "properties": {
                    long_key: {
                        "type": "string",
                    }
                }
            },
        )

    diagnostic = captured.value.diagnostics[0]
    assert diagnostic.data_pointer_truncated is True
    assert diagnostic.schema_pointer_truncated is True
    assert len(diagnostic.data_pointer.encode("utf-8")) <= MAX_DIAGNOSTIC_POINTER_BYTES
    assert len(diagnostic.schema_pointer.encode("utf-8")) <= MAX_DIAGNOSTIC_POINTER_BYTES
    assert "__gh_slate_truncated_" in diagnostic.data_pointer


def test_schema_and_data_sizes_are_bounded_before_evaluation() -> None:
    with pytest.raises(SchemaError) as schema_error:
        validate_schema({"description": "x" * (64 * 1024)})
    assert schema_error.value.code == "schema_size_limit"

    with pytest.raises(SchemaError) as data_error:
        validate_data({"value": "x" * (192 * 1024)}, True)
    assert data_error.value.code == "schema_data_size_limit"


@pytest.mark.parametrize("max_errors", [0, -1, True, cast("int", 1.5)])
def test_max_errors_must_be_a_positive_integer(max_errors: int) -> None:
    with pytest.raises(SchemaError) as captured:
        validate_data({}, True, max_errors=max_errors)
    assert captured.value.code == "schema_options_invalid"


def test_diagnostic_default_and_hard_caps_are_explicit() -> None:
    assert DEFAULT_MAX_ERRORS == 32

    with pytest.raises(SchemaError) as captured:
        validate_data({}, True, max_errors=MAX_ERRORS_LIMIT + 1)
    assert captured.value.code == "schema_options_invalid"
    assert captured.value.details["maximum"] == MAX_ERRORS_LIMIT


def test_replace_schema_validates_schema_before_current_data() -> None:
    snapshot = replace_schema(
        {"name": "linux"},
        {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
    )

    assert isinstance(snapshot, SchemaSnapshotV1)

    with pytest.raises(SchemaError) as captured:
        replace_schema(
            {"name": None},
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        )
    assert captured.value.code == "schema_validation_failed"
