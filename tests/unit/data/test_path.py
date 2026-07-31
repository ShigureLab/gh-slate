from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.codec.json import JsonValue, freeze_json
from gh_slate.data import (
    MAX_ARRAY_INDEX,
    MAX_PATH_EXPRESSION_BYTES,
    MAX_PATH_SEGMENTS,
    DataError,
    delete_path,
    delete_paths,
    get_path,
    resolve_exact_path,
    set_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class FakeEvaluator:
    def __init__(self, results: Sequence[JsonValue]) -> None:
        self.results = results
        self.calls: list[tuple[object, str, int]] = []

    def __call__(
        self,
        data: object,
        filter: str,
        *,
        max_results: int,
    ) -> Sequence[JsonValue]:
        self.calls.append((data, filter, max_results))
        return self.results


def assert_data_error(
    code: str,
    callable_: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> DataError:
    with pytest.raises(DataError) as captured:
        callable_(*args, **kwargs)
    assert captured.value.code == code
    return captured.value


def frozen_object(value: object) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value)
    assert isinstance(frozen, Mapping)
    return cast("Mapping[str, JsonValue]", frozen)


def test_resolve_exact_path_wraps_expression_and_normalizes_indexes() -> None:
    data = freeze_json({"rows": [{"value": 1}]})
    evaluator = FakeEvaluator((("rows", Decimal(0)),))

    path = resolve_exact_path(data, ".rows[0] # trailing comment", evaluator=evaluator)

    assert path == ("rows", 0)
    assert evaluator.calls == [
        (
            None,
            "path((\n.rows[0] # trailing comment\n))",
            1,
        ),
    ]


def test_exact_path_rejects_a_fragment_that_escapes_its_wrapper() -> None:
    evaluator = FakeEvaluator(())

    with pytest.raises(DataError) as captured:
        resolve_exact_path(
            {"a": 1, "b": 2},
            '.a)) | ["b"] | ((.',
            evaluator=evaluator,
        )

    assert captured.value.code == "data_path_dynamic"
    assert evaluator.calls == []


def test_exact_path_maps_three_or_more_paths_to_the_stable_path_error() -> None:
    error = assert_data_error(
        "data_path_multiple_results",
        resolve_exact_path,
        {"a": 1},
        ".a",
        evaluator=FakeEvaluator((("a",), ("b",), ("c",))),
    )

    assert error.details["count_at_least"] == 2


@pytest.mark.parametrize(
    ("results", "code"),
    [
        ((), "data_path_no_result"),
        ((("a",), ("b",)), "data_path_multiple_results"),
        (("not-an-array",), "data_path_invalid"),
        (((True,),), "data_path_invalid"),
        (((Decimal(-1),),), "data_path_invalid"),
        (((Decimal("1.5"),),), "data_path_invalid"),
        (((None,),), "data_path_invalid"),
    ],
)
def test_resolve_exact_path_rejects_non_exact_or_invalid_results(
    results: Sequence[JsonValue],
    code: str,
) -> None:
    assert_data_error(
        code,
        resolve_exact_path,
        {},
        ".anything",
        evaluator=FakeEvaluator(results),
    )


@pytest.mark.parametrize("expression", ["", " \n\t"])
def test_resolve_exact_path_rejects_empty_expression_without_jq(expression: str) -> None:
    evaluator = FakeEvaluator(((),))

    assert_data_error(
        "data_path_invalid",
        resolve_exact_path,
        {},
        expression,
        evaluator=evaluator,
    )
    assert evaluator.calls == []


def test_resolve_exact_path_accepts_static_quoted_keys_indexes_and_comments() -> None:
    from gh_slate.rendering import evaluate

    assert resolve_exact_path(
        {"not": "consulted"},
        ' # leading\n .["a.b"] [ 0 ] ["line\\n"] # trailing',
        evaluator=evaluate,
    ) == ("a.b", 0, "line\n")


@pytest.mark.parametrize(
    "expression",
    [
        ".by_id[((.id + 0) | tostring)]",
        ".rows[]",
        ".rows[-1]",
        ".rows[0:1]",
        ".field?",
        "..foo",
        "$path",
        '.["\\(.id)"]',
    ],
)
def test_resolve_exact_path_rejects_dynamic_or_nonstatic_jq(
    expression: str,
) -> None:
    evaluator = FakeEvaluator(())

    assert_data_error(
        "data_path_dynamic",
        resolve_exact_path,
        {},
        expression,
        evaluator=evaluator,
    )
    assert evaluator.calls == []


def test_static_path_expression_has_byte_segment_and_index_limits() -> None:
    evaluator = FakeEvaluator(())

    oversized = "." + (" " * MAX_PATH_EXPRESSION_BYTES)
    assert_data_error(
        "data_path_limit",
        resolve_exact_path,
        {},
        oversized,
        evaluator=evaluator,
    )
    too_deep = ".a" + (".a" * MAX_PATH_SEGMENTS)
    assert_data_error(
        "data_path_limit",
        resolve_exact_path,
        {},
        too_deep,
        evaluator=evaluator,
    )
    assert_data_error(
        "data_path_index_limit",
        resolve_exact_path,
        {},
        f".rows[{MAX_ARRAY_INDEX + 1}]",
        evaluator=evaluator,
    )
    assert evaluator.calls == []


def test_path_segments_and_array_indexes_have_allocation_safe_limits() -> None:
    assert_data_error(
        "data_path_limit",
        get_path,
        freeze_json(None),
        ("key",) * (MAX_PATH_SEGMENTS + 1),
    )
    error = assert_data_error(
        "data_path_index_limit",
        set_path,
        freeze_json(()),
        (MAX_ARRAY_INDEX + 1,),
        True,
    )
    assert error.details["max_index"] == MAX_ARRAY_INDEX


def test_get_path_distinguishes_null_from_missing() -> None:
    data = freeze_json({"present": None, "rows": [{"name": "linux"}]})

    assert get_path(data, ("present",)) is None
    assert get_path(data, ("rows", 0, "name")) == "linux"
    assert_data_error("data_path_missing", get_path, data, ("absent",))
    assert_data_error("data_path_index_out_of_bounds", get_path, data, ("rows", 1))
    assert_data_error("data_path_type_mismatch", get_path, data, ("present", "child"))


def test_set_path_is_persistent_and_preserves_untouched_large_numbers() -> None:
    exact = Decimal(1234567890123456789012345678901234567890)
    data = frozen_object(
        {
            "metadata": {"exact": exact},
            "jobs": (
                {"name": "linux", "status": "queued"},
                {"name": "macos", "status": "passed"},
            ),
        }
    )
    original_metadata = cast("Mapping[str, JsonValue]", data["metadata"])
    original_second_job = cast("tuple[JsonValue, ...]", data["jobs"])[1]

    updated = set_path(data, ("jobs", 0, "status"), "passed")

    assert updated == {
        "metadata": {"exact": exact},
        "jobs": (
            {"name": "linux", "status": "passed"},
            {"name": "macos", "status": "passed"},
        ),
    }
    assert isinstance(updated, Mapping)
    updated_object = cast("Mapping[str, JsonValue]", updated)
    assert updated_object["metadata"] is original_metadata
    assert cast("tuple[JsonValue, ...]", updated_object["jobs"])[1] is original_second_job
    updated_metadata = cast("Mapping[str, JsonValue]", updated_object["metadata"])
    assert updated_metadata["exact"] == exact
    original_jobs = cast("tuple[JsonValue, ...]", data["jobs"])
    original_first_job = cast("Mapping[str, JsonValue]", original_jobs[0])
    assert original_first_job["status"] == "queued"


def test_set_path_creates_missing_containers_and_pads_arrays_like_jq() -> None:
    data = freeze_json({})

    assert set_path(data, ("coverage", "lines"), Decimal("91.7")) == {"coverage": {"lines": Decimal("91.7")}}
    assert set_path(data, ("jobs", 2, "name"), "linux") == {"jobs": (None, None, {"name": "linux"})}
    assert set_path(freeze_json({"nested": None}), ("nested", 0, "ok"), True) == {"nested": ({"ok": True},)}


def test_set_path_pads_existing_arrays_and_rejects_wrong_container_type() -> None:
    data = freeze_json({"rows": ({"value": 1},), "scalar": None})

    assert set_path(data, ("rows", 2), {"value": 3}) == {
        "rows": ({"value": 1}, None, {"value": 3}),
        "scalar": None,
    }
    assert set_path(data, ("scalar", "child"), 1) == {
        "rows": ({"value": 1},),
        "scalar": {"child": 1},
    }
    assert_data_error("data_path_type_mismatch", set_path, data, ("rows", "child"), 1)


def test_set_path_supports_root_replace_and_reuses_equal_values() -> None:
    data = freeze_json({"value": 1})

    assert set_path(data, (), {"replacement": True}) == {"replacement": True}
    assert set_path(data, ("value",), Decimal(1)) is data


def test_delete_paths_uses_original_array_indexes_and_shares_untouched_subtrees() -> None:
    untouched = freeze_json({"large": Decimal(900719925474099312345)})
    data = frozen_object(
        {
            "items": (
                {"id": "zero"},
                {"id": "one"},
                {"id": "two"},
                {"id": "three"},
            ),
            "untouched": untouched,
        }
    )

    updated = delete_paths(
        data,
        (
            ("items", 1),
            ("items", 0),
        ),
    )

    assert updated == {
        "items": ({"id": "two"}, {"id": "three"}),
        "untouched": {"large": Decimal(900719925474099312345)},
    }
    assert isinstance(updated, Mapping)
    assert cast("Mapping[str, JsonValue]", updated)["untouched"] is data["untouched"]
    assert len(cast("tuple[JsonValue, ...]", data["items"])) == 4


def test_delete_paths_handles_duplicates_and_parent_child_overlap() -> None:
    data = freeze_json({"nested": {"a": 1, "b": 2}, "keep": True})

    updated = delete_paths(
        data,
        (
            ("nested", "a"),
            ("nested", "a"),
            ("nested",),
        ),
    )

    assert updated == {"keep": True}


def test_delete_missing_is_explicit_and_ignore_missing_is_identity() -> None:
    data = freeze_json({"rows": (1,), "scalar": None})

    assert_data_error("data_path_missing", delete_path, data, ("missing",))
    assert_data_error("data_path_index_out_of_bounds", delete_path, data, ("rows", 2))
    assert delete_path(data, ("missing",), ignore_missing=True) is data
    assert delete_path(data, ("rows", 2), ignore_missing=True) is data
    assert_data_error(
        "data_path_type_mismatch",
        delete_path,
        data,
        ("scalar", "child"),
        ignore_missing=True,
    )


def test_delete_root_is_always_rejected() -> None:
    data = freeze_json({"value": 1})

    assert_data_error("data_path_root_delete", delete_path, data, ())
    assert_data_error(
        "data_path_root_delete",
        delete_path,
        data,
        (),
        ignore_missing=True,
    )
