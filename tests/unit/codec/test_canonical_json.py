from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import (
    canonical_json_bytes,
    canonical_json_dumps,
    canonical_number,
    freeze_json,
    strict_loads,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("-0"), "0"),
        (Decimal("-0.000"), "0"),
        (Decimal("1.2300"), "1.23"),
        (Decimal("0.000001"), "0.000001"),
        (Decimal("0.0000001"), "1e-7"),
        (Decimal("1e20"), "100000000000000000000"),
        (Decimal("1e21"), "1e+21"),
        (Decimal("-1.20e30"), "-1.2e+30"),
        (42, "42"),
    ],
)
def test_canonical_number_uses_the_state_boundaries(value: Decimal | int, expected: str) -> None:
    assert canonical_number(value) == expected


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), True, 1.0])
def test_canonical_number_rejects_non_finite_or_non_decimal_inputs(value: object) -> None:
    with pytest.raises(CodecError) as captured:
        canonical_number(cast("Decimal | int", value))

    assert captured.value.code in {"json_number_invalid", "json_type_unsupported"}


def test_canonical_json_is_compact_unicode_utf8_with_codepoint_key_order() -> None:
    value = {
        "\U00010000": "astral",
        "\ue000": "private",
        "z": "雪",
        "a": "line\nbreak",
    }

    assert canonical_json_bytes(value) == (
        '{"a":"line\\nbreak","z":"雪","\ue000":"private","\U00010000":"astral"}'.encode()
    )


def test_canonical_json_is_independent_of_mapping_insertion_order() -> None:
    left = {"z": Decimal("1.00"), "a": [True, None, "x"]}
    right = {"a": (True, None, "x"), "z": 1}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_dumps(left) == '{"a":[true,null,"x"],"z":1}'


def test_canonical_json_round_trips_through_the_strict_parser() -> None:
    value = {
        "empty_object": {},
        "empty_array": [],
        "integer": 7,
        "decimal": Decimal("123.4500"),
        "small": Decimal("1e-7"),
        "unicode": "猫",
    }

    encoded = canonical_json_bytes(value)

    assert strict_loads(encoded) == freeze_json(value)
    assert encoded == (
        b'{"decimal":123.45,"empty_array":[],"empty_object":{},"integer":7,"small":1e-7,"unicode":"\xe7\x8c\xab"}'
    )


def test_canonical_json_rejects_python_float_and_unpaired_surrogate() -> None:
    with pytest.raises(CodecError) as float_error:
        canonical_json_bytes({"value": 1.0})
    assert float_error.value.code == "json_type_unsupported"

    with pytest.raises(CodecError) as unicode_error:
        canonical_json_bytes({"value": "\ud800"})
    assert unicode_error.value.code == "json_unpaired_surrogate"
