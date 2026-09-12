from __future__ import annotations

from decimal import Decimal

import pytest

from gh_slate.codec import canonical_json_bytes
from gh_slate.errors import GhSlateError
from gh_slate.patching import apply_data_patch, data_diff, prepare_patch


def test_all_operations_preserve_source_and_json_types():
    original = {"items": ["a", "b"], "findings": {"F17": {"status": "open"}}, "old": "remove", "nullable": 1}
    patch = [
        {"op": "test", "path": "/findings/F17/status", "value": "open"},
        {"op": "replace", "path": "/findings/F17/status", "value": "resolved"},
        {"op": "copy", "from": "/findings/F17", "path": "/findings/F18"},
        {"op": "replace", "path": "/findings/F18/status", "value": "open"},
        {"op": "move", "from": "/items/0", "path": "/items/-"},
        {"op": "remove", "path": "/old"},
        {"op": "add", "path": "/items/1", "value": "inserted"},
        {"op": "replace", "path": "/nullable", "value": None},
    ]
    result = apply_data_patch(original, patch)
    assert result == {
        "items": ("b", "inserted", "a"),
        "findings": {"F17": {"status": "resolved"}, "F18": {"status": "open"}},
        "nullable": None,
    }
    assert original["findings"]["F17"]["status"] == "open"
    assert original["items"] == ["a", "b"]


@pytest.mark.parametrize(("actual", "expected"), [(True, 1), (False, 0), ({"x": True}, {"x": 1}), ([False], [0])])
def test_patch_test_distinguishes_booleans_from_numbers(actual, expected):
    with pytest.raises(GhSlateError) as error:
        apply_data_patch({"value": actual}, [{"op": "test", "path": "/value", "value": expected}])
    assert error.value.code == "patch_test_failed"
    assert error.value.details["operation_index"] == 0


def test_patch_test_preserves_exact_numbers_and_object_order():
    data = {"number": Decimal("123456789012345678901234567890.125"), "object": {"a": 1, "b": 2}}
    result = apply_data_patch(
        data,
        [
            {"op": "test", "path": "/number", "value": Decimal("123456789012345678901234567890.1250")},
            {"op": "test", "path": "/object", "value": {"b": 2, "a": Decimal("1.0")}},
        ],
    )
    assert canonical_json_bytes(result) == canonical_json_bytes(data)


def test_pointer_escaping_and_literal_dash_object_key():
    result = apply_data_patch(
        {"a/b": {"~key": 1}, "-": False},
        [{"op": "replace", "path": "/a~1b/~0key", "value": 2}, {"op": "replace", "path": "/-", "value": True}],
    )
    assert result == {"a/b": {"~key": 2}, "-": True}


@pytest.mark.parametrize(
    "patch",
    [
        [{"op": "copy", "from": "", "path": "/snapshot"}],
        [{"op": "replace", "path": "", "value": None}, {"op": "add", "path": "", "value": {"x": 1}}],
        [{"op": "remove", "path": ""}, {"op": "add", "path": "", "value": {"x": 1}}],
        [{"op": "copy", "from": "", "path": ""}],
        [{"op": "move", "from": "", "path": ""}],
    ],
)
def test_standard_root_operations_can_have_temporary_non_object_states(patch):
    result = apply_data_patch({"x": 1}, patch)
    expected = {"x": 1, "snapshot": {"x": 1}} if patch[0].get("path") == "/snapshot" else {"x": 1}
    assert result == expected


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "move", "from": "", "path": "/child"},
        {"op": "move", "from": "/a/0", "path": "/a/0/nested"},
        {"op": "add", "path": "/a/5", "value": 1},
        {"op": "replace", "path": "/missing", "value": 1},
        {"op": "remove", "path": "/a/-"},
        {"op": "test", "path": "/a/-", "value": None},
    ],
)
def test_invalid_locations_fail_without_mutating_input(operation):
    data = {"a": [{"child": 1}]}
    before = canonical_json_bytes(data)
    with pytest.raises(GhSlateError):
        apply_data_patch(data, [operation])
    assert canonical_json_bytes(data) == before


@pytest.mark.parametrize(
    "patch",
    [
        {},
        [None],
        [{"op": []}],
        [{"op": "unknown", "path": ""}],
        [{"op": "add", "path": "/x"}],
        [{"op": "copy", "from": 1, "path": "/x"}],
        [{"op": "remove", "path": "bad"}],
    ],
)
def test_invalid_patch_shapes_are_structured_errors(patch):
    with pytest.raises(GhSlateError) as error:
        prepare_patch(patch)
    assert error.value.code == "patch_invalid"


def test_final_root_must_stay_an_object_and_extra_members_are_ignored():
    with pytest.raises(GhSlateError) as error:
        apply_data_patch({}, [{"op": "replace", "path": "", "value": []}])
    assert error.value.code == "patch_result_invalid"
    assert apply_data_patch({}, [{"op": "add", "path": "/x", "value": None, "ignored": "RFC 6902"}]) == {"x": None}


def test_diff_is_deterministic_typed_and_can_be_applied():
    before = {"a/b": {"~key": False}, "removed": 1, "array": [1]}
    after = {"a/b": {"~key": 0}, "added": None, "array": [1, 2]}
    changes = data_diff(before, after)
    assert changes[0] == {"op": "replace", "path": "/a~1b/~0key", "value": 0}
    assert canonical_json_bytes(apply_data_patch(before, changes)) == canonical_json_bytes(after)
