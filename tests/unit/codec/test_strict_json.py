from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import JsonLimits, freeze_json, strict_loads, strict_loads_object

if TYPE_CHECKING:
    from collections.abc import Callable


def assert_codec_error(
    code: str,
    callable_: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> CodecError:
    with pytest.raises(CodecError) as captured:
        callable_(*args, **kwargs)
    assert captured.value.code == code
    return captured.value


def test_strict_loads_freezes_objects_arrays_and_all_numbers() -> None:
    value = strict_loads(b'{"integer":1,"decimal":1.25,"exponent":1e3,"array":[true,null,{"nested":"yes"}]}')

    assert isinstance(value, MappingProxyType)
    assert value["integer"] == Decimal(1)
    assert value["decimal"] == Decimal("1.25")
    assert value["exponent"] == Decimal("1e3")
    assert value["array"] == (True, None, {"nested": "yes"})
    assert isinstance(value["array"], tuple)
    assert isinstance(value["array"][2], MappingProxyType)


def test_strict_loads_object_requires_an_object_root() -> None:
    assert strict_loads_object('{"ok":true}') == {"ok": True}

    assert_codec_error("json_root_not_object", strict_loads_object, "[1,2]")


@pytest.mark.parametrize(
    ("document", "code"),
    [
        ('{"key":1,"key":2}', "json_duplicate_key"),
        ('{"\\u006bey":1,"key":2}', "json_duplicate_key"),
        ("NaN", "json_number_invalid"),
        ("Infinity", "json_number_invalid"),
        ("-Infinity", "json_number_invalid"),
        ("{} trailing", "json_invalid"),
        ('"\\ud800"', "json_unpaired_surrogate"),
        ('{"\\udfff":1}', "json_unpaired_surrogate"),
    ],
)
def test_strict_loads_rejects_ambiguous_or_non_json_input(document: str, code: str) -> None:
    assert_codec_error(code, strict_loads, document)


@pytest.mark.parametrize(
    "document",
    [
        "\ufeff{}",
        b"\xef\xbb\xbf{}",
        b"\xff\xfe{\x00}\x00",
        b"\xfe\xff\x00{\x00}",
        b"\xff\xfe\x00\x00{\x00\x00\x00}\x00\x00\x00",
        b"\x00\x00\xfe\xff\x00\x00\x00{\x00\x00\x00}",
    ],
)
def test_strict_loads_rejects_a_bom(document: str | bytes) -> None:
    assert_codec_error("json_bom", strict_loads, document)


def test_strict_loads_accepts_a_valid_escaped_surrogate_pair() -> None:
    assert strict_loads('"\\ud83d\\ude00"') == "😀"


def test_strict_loads_requires_strict_utf8() -> None:
    error = assert_codec_error("json_invalid", strict_loads, b'"\xff"')

    assert error.details["reason"] == "invalid_utf8"


def test_freeze_json_rejects_floats_non_string_keys_and_cycles() -> None:
    assert_codec_error("json_type_unsupported", freeze_json, {"value": 1.0})
    assert_codec_error("json_type_unsupported", freeze_json, {1: "value"})

    recursive: list[object] = []
    recursive.append(recursive)
    error = assert_codec_error("json_type_unsupported", freeze_json, recursive)
    assert error.details["reason"] == "cycle"


def test_freeze_json_rejects_non_finite_decimals() -> None:
    assert_codec_error("json_number_invalid", freeze_json, Decimal("NaN"))
    assert_codec_error("json_number_invalid", freeze_json, Decimal("Infinity"))


@pytest.mark.parametrize(
    ("document", "limits", "limit_name"),
    [
        ("{}", JsonLimits(max_input_bytes=1), "max_input_bytes"),
        ('{"a":1}', JsonLimits(max_nodes=1), "max_nodes"),
        ('{"a":{"b":1}}', JsonLimits(max_depth=0), "max_depth"),
        ('"é"', JsonLimits(max_string_bytes=1), "max_string_bytes"),
        ('{"é":1}', JsonLimits(max_key_bytes=1), "max_key_bytes"),
        ("123", JsonLimits(max_number_chars=2), "max_number_chars"),
    ],
)
def test_strict_loads_enforces_each_resource_limit(
    document: str,
    limits: JsonLimits,
    limit_name: str,
) -> None:
    error = assert_codec_error("json_limit_exceeded", strict_loads, document, limits=limits)

    assert error.details["limit"] == limit_name


def test_number_limit_counts_the_source_lexeme() -> None:
    assert strict_loads("1e10", limits=JsonLimits(max_number_chars=4)) == Decimal("1e10")


def test_limits_reject_invalid_configuration_without_leaking_builtin_errors() -> None:
    error = assert_codec_error("json_limit_exceeded", strict_loads, "null", limits=JsonLimits(max_nodes=-1))

    assert error.details["reason"] == "invalid_limit"


def test_freeze_json_counts_strings_as_utf8_bytes() -> None:
    assert freeze_json("é", limits=JsonLimits(max_string_bytes=2)) == "é"
    assert_codec_error(
        "json_limit_exceeded",
        freeze_json,
        "é",
        limits=JsonLimits(max_string_bytes=1),
    )
