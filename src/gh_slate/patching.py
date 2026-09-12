from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from typing import cast

from jsonpatch import JsonPatch, JsonPatchException
from jsonpointer import EndOfList, JsonPointer, JsonPointerException

from gh_slate.codec import canonical_json_bytes, freeze_json
from gh_slate.codec.json import DEFAULT_JSON_LIMITS
from gh_slate.errors import ExitCode, GhSlateError

MAX_PATCH_OPERATIONS = 1_000
_ABSENT = object()
_OPERATIONS = frozenset({"add", "remove", "replace", "move", "copy", "test"})


def _error(message: str, *, code: str = "patch_invalid", index: int | None = None) -> GhSlateError:
    return GhSlateError(
        message,
        code=code,
        exit_code=ExitCode.CONFLICT if code == "patch_test_failed" else ExitCode.VALIDATION,
        details={} if index is None else {"operation_index": index},
    )


def prepare_patch(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise _error("JSON Patch must be an array of operations")
    if len(value) > MAX_PATCH_OPERATIONS:
        raise _error("JSON Patch exceeds 1000 operations", code="patch_size_limit")
    frozen = freeze_json(value)
    assert isinstance(frozen, tuple)
    for index, operation in enumerate(frozen):
        if (
            not isinstance(operation, Mapping)
            or not isinstance(operation.get("op"), str)
            or operation["op"] not in _OPERATIONS
        ):
            raise _error("JSON Patch requires a supported op in every operation", index=index)
        required = ["path"]
        if operation["op"] in {"move", "copy"}:
            required.append("from")
        for key in required:
            pointer = operation.get(key)
            if not isinstance(pointer, str):
                raise _error(f"JSON Patch {key} must be a JSON Pointer string", index=index)
            try:
                JsonPointer(pointer)
            except JsonPointerException as error:
                raise _error(f"invalid JSON Pointer in {key}", index=index) from error
        if operation["op"] in {"add", "replace", "test"} and "value" not in operation:
            raise _error("JSON Patch operation is missing value", index=index)
    if len(canonical_json_bytes(frozen)) > DEFAULT_JSON_LIMITS.max_input_bytes:
        raise _error("JSON Patch exceeds the JSON byte limit", code="patch_size_limit")
    return cast("tuple[Mapping[str, object], ...]", frozen)


def _mutable(value: object) -> object:
    # The codec's immutable maps/tuples are not writable by jsonpatch. Preserve
    # exact JSON numbers while making one private working copy.
    return json.loads(canonical_json_bytes(value), parse_int=Decimal, parse_float=Decimal)


def _resolve(pointer: JsonPointer, candidate: object) -> object:
    value = pointer.resolve(candidate)
    if isinstance(value, EndOfList):
        raise JsonPointerException("an append position does not contain a value")
    return value


def _apply_operation(candidate: object, operation: Mapping[str, object], index: int) -> object:
    op, path = operation["op"], cast("str", operation["path"])
    if candidate is _ABSENT:
        if op == "add" and path == "":
            return deepcopy(operation["value"])
        raise _error("operation targets a removed data root", index=index)
    pointer = JsonPointer(path)
    if op == "test":
        try:
            actual = _resolve(pointer, candidate)
        except JsonPointerException as error:
            raise _error("JSON Patch test target does not exist", code="patch_test_failed", index=index) from error
        # Python considers True == 1, unlike RFC 6902. Canonical JSON also
        # handles numeric equivalence, container order, and Decimal exactly.
        if canonical_json_bytes(actual) != canonical_json_bytes(operation["value"]):
            raise _error("JSON Patch test did not match", code="patch_test_failed", index=index)
        return candidate
    if op == "move":
        origin = JsonPointer(cast("str", operation["from"]))
        if len(pointer.parts) > len(origin.parts) and pointer.parts[: len(origin.parts)] == origin.parts:
            raise _error("cannot move a value into its own child", index=index)
    # jsonpatch 1.33 cannot copy the root, add onto a non-object root, or
    # replace an object key literally named '-'. Normalize only these RFC
    # edge cases; ordinary operations use the library unchanged.
    if path == "":
        if op in {"add", "replace"}:
            return deepcopy(operation["value"])
        if op == "remove":
            return _ABSENT
        if op in {"copy", "move"}:
            return deepcopy(_resolve(JsonPointer(cast("str", operation["from"])), candidate))
    if op == "copy" and operation["from"] == "":
        operation = {"op": "add", "path": path, "value": deepcopy(candidate)}
    if op == "replace":
        parent, key = pointer.to_last(candidate)
        if isinstance(parent, dict) and key == "-":
            if key not in parent:
                raise _error("replace target does not exist", index=index)
            parent[key] = deepcopy(operation["value"])
            return candidate
    return JsonPatch([dict(operation)]).apply(candidate, in_place=True)


def apply_data_patch(data: object, patch: object) -> object:
    operations = prepare_patch(patch)
    candidate = _mutable(data)
    for index, frozen in enumerate(operations):
        operation = cast("Mapping[str, object]", _mutable(frozen))
        try:
            candidate = _apply_operation(candidate, operation, index)
        except GhSlateError:
            raise
        except (JsonPatchException, JsonPointerException, KeyError, IndexError, TypeError, ValueError) as error:
            raise _error("JSON Patch operation could not be applied", code="patch_conflict", index=index) from error
        if candidate is not _ABSENT:
            # No business-schema validation until the final state, but every
            # intermediate value retains the JSON parser's size/depth bounds.
            if len(canonical_json_bytes(candidate)) > DEFAULT_JSON_LIMITS.max_input_bytes:
                raise _error(
                    "intermediate patch data exceeds the JSON byte limit", code="patch_size_limit", index=index
                )
    if not isinstance(candidate, Mapping):
        raise _error("patched data root must be a JSON object", code="patch_result_invalid")
    return freeze_json(candidate)


def data_diff(before: object, after: object, pointer: str = "") -> list[dict[str, object]]:
    """A deterministic JSON Patch preview; changed arrays are replaced whole."""
    if canonical_json_bytes(before) == canonical_json_bytes(after):
        return []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: list[dict[str, object]] = []
        for key in sorted(set(before) | set(after)):
            path = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in after:
                changes.append({"op": "remove", "path": path})
            elif key not in before:
                changes.append({"op": "add", "path": path, "value": after[key]})
            else:
                changes.extend(data_diff(before[key], after[key], path))
        return changes
    return [{"op": "replace", "path": pointer, "value": after}]


__all__ = ["apply_data_patch", "data_diff", "prepare_patch"]
