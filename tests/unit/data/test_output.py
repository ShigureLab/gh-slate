from __future__ import annotations

from decimal import Decimal

import pytest

from gh_slate.data import (
    JsonOutputMode,
    format_json_results,
    format_json_value,
    json_exit_status,
    pretty_json,
)


def test_pretty_json_is_deterministic_and_preserves_large_numbers() -> None:
    value = {
        "z": Decimal(9007199254740993123456789),
        "a": (True, None, {"é": "雪"}),
    }

    assert pretty_json(value) == (
        '{\n  "a": [\n    true,\n    null,\n    {\n      "é": "雪"\n    }\n  ],\n'
        '  "z": 9.007199254740993123456789e+24\n}'
    )


def test_compact_output_is_canonical_json() -> None:
    assert format_json_value({"z": 1, "a": 2}, compact=True) == '{"a":2,"z":1}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("line\nbreak", "line\nbreak"),
        (None, "null"),
        (False, "false"),
        (Decimal("1.25"), "1.25"),
        ((1, 2), "[\n  1,\n  2\n]"),
        ({"a": 1}, '{\n  "a": 1\n}'),
    ],
)
def test_raw_output_unquotes_only_strings(value: object, expected: str) -> None:
    assert format_json_value(value, raw=True) == expected


def test_compact_and_raw_combine_like_jq() -> None:
    assert format_json_value("plain", compact=True, raw=True) == "plain"
    assert format_json_value({"a": 1}, compact=True, raw=True) == '{"a":1}'


def test_result_stream_has_one_terminating_newline_per_result() -> None:
    assert format_json_results(()) == ""
    assert format_json_results(("a", None), raw=True) == "a\nnull\n"
    assert format_json_results(({"a": 1}, {"b": 2}), compact=True) == ('{"a":1}\n{"b":2}\n')


@pytest.mark.parametrize(
    ("results", "status"),
    [
        ((), 4),
        ((None,), 1),
        ((False,), 1),
        ((True,), 0),
        ((False, "later"), 0),
        (("earlier", None), 1),
        ((Decimal(0),), 0),
        (("",), 0),
    ],
)
def test_json_exit_status_matches_jq_last_result_rules(
    results: tuple[object, ...],
    status: int,
) -> None:
    assert json_exit_status(results) == status  # ty: ignore[invalid-argument-type]


def test_output_mode_values_are_stable() -> None:
    assert [mode.value for mode in JsonOutputMode] == ["pretty", "compact", "raw"]
