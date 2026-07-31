from __future__ import annotations

from io import BytesIO
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.data import DataError, load_argjson, load_value, merge_jq_arguments

if TYPE_CHECKING:
    from pathlib import Path


def test_load_value_keeps_json_and_string_types_distinct(tmp_path: Path) -> None:
    document = tmp_path / "value.json"
    document.write_text('{"enabled":true}', encoding="utf-8")

    assert load_value(value="91") == 91
    assert load_value(value='"91"') == "91"
    assert load_value(value_string="91") == "91"
    assert load_value(value_string="true") == "true"
    assert load_value(value="true") is True
    assert load_value(value="null") is None
    assert load_value(value_file=document) == {"enabled": True}


def test_dash_means_stdin_only_for_json_and_file_sources() -> None:
    assert load_value(value="-", stdin=BytesIO(b'{"source":"value"}')) == {"source": "value"}
    assert load_value(value_file="-", stdin=BytesIO(b'{"source":"file"}')) == {"source": "file"}
    assert load_value(value_string="-", stdin=BytesIO(b'"ignored"')) == "-"


def test_value_sources_are_required_and_mutually_exclusive() -> None:
    with pytest.raises(DataError) as missing:
        load_value()
    assert missing.value.code == "data_value_source_missing"

    with pytest.raises(DataError) as conflict:
        load_value(value="1", value_string="1")
    assert conflict.value.code == "data_value_source_conflict"


@pytest.mark.parametrize(
    "source",
    [
        '{"duplicate":1,"duplicate":2}',
        "NaN",
        "Infinity",
        "not-json",
    ],
)
def test_value_json_uses_the_strict_parser(source: str) -> None:
    with pytest.raises(CodecError):
        load_value(value=source)


def test_load_argjson_reads_only_explicit_at_files(tmp_path: Path) -> None:
    document = tmp_path / "argument.json"
    document.write_text('{"from":"file"}', encoding="utf-8")

    assert load_argjson(f"@{document}") == {"from": "file"}
    assert load_argjson("@-", stdin=BytesIO(b"[1,2]")) == (1, 2)
    with pytest.raises(CodecError):
        load_argjson(str(document))
    with pytest.raises(DataError) as empty:
        load_argjson("@")
    assert empty.value.code == "data_argjson_invalid"


def test_input_reads_are_bounded_before_parsing(tmp_path: Path) -> None:
    document = tmp_path / "large.json"
    document.write_bytes(b'"123456789"')

    with pytest.raises(DataError) as file_error:
        load_value(value_file=document, max_bytes=4)
    assert file_error.value.code == "data_input_size_limit"
    assert file_error.value.details["actual_bytes"] == 11

    with pytest.raises(DataError) as stdin_error:
        load_value(value="-", stdin=BytesIO(b'"123456789"'), max_bytes=4)
    assert stdin_error.value.code == "data_input_size_limit"
    assert stdin_error.value.details["actual_bytes"] == 5

    with pytest.raises(DataError) as string_error:
        load_value(value_string="ééé", max_bytes=5)
    assert string_error.value.code == "data_input_size_limit"
    assert string_error.value.details["actual_bytes"] == 6


def test_input_read_failures_are_stable(tmp_path: Path) -> None:
    with pytest.raises(DataError) as captured:
        load_value(value_file=tmp_path / "missing.json")

    assert captured.value.code == "data_input_read_failed"
    assert captured.value.details["error_type"] == "FileNotFoundError"


def test_merge_jq_arguments_types_values_and_freezes_result(tmp_path: Path) -> None:
    document = tmp_path / "typed.json"
    document.write_text('{"status":"passed"}', encoding="utf-8")

    result = merge_jq_arguments(
        (("name", "linux"),),
        (
            ("number", "42"),
            ("object", f"@{document}"),
        ),
    )

    assert isinstance(result, MappingProxyType)
    assert result == {
        "name": "linux",
        "number": 42,
        "object": {"status": "passed"},
    }


@pytest.mark.parametrize(
    ("strings", "typed", "code"),
    [
        ((("same", "a"),), (("same", '"b"'),), "data_jq_argument_duplicate"),
        ((("not-valid!", "a"),), (), "data_jq_argument_invalid"),
        ((("名字", "a"),), (), "data_jq_argument_invalid"),
        ((("", "a"),), (), "data_jq_argument_invalid"),
    ],
)
def test_merge_jq_arguments_rejects_duplicate_or_invalid_names(
    strings: tuple[tuple[str, str], ...],
    typed: tuple[tuple[str, str], ...],
    code: str,
) -> None:
    with pytest.raises(DataError) as captured:
        merge_jq_arguments(strings, typed)

    assert captured.value.code == code


def test_merge_jq_arguments_does_not_guess_at_files_for_string_args(tmp_path: Path) -> None:
    document = tmp_path / "value.json"
    document.write_text("42", encoding="utf-8")

    result = merge_jq_arguments((("literal", f"@{document}"),), ())

    assert cast("str", result["literal"]).startswith("@")


def test_merge_jq_string_arguments_are_utf8_bounded() -> None:
    with pytest.raises(DataError) as captured:
        merge_jq_arguments((("value", "éé"),), (), max_bytes=3)

    assert captured.value.code == "data_input_size_limit"


def test_merge_jq_arguments_enforces_one_cumulative_budget() -> None:
    with pytest.raises(DataError) as captured:
        merge_jq_arguments(
            (
                ("a", "x"),
                ("b", "y"),
            ),
            (),
            max_bytes=12,
        )

    assert captured.value.code == "data_input_size_limit"
    assert captured.value.details == {
        "actual_bytes": 17,
        "max_bytes": 12,
    }
