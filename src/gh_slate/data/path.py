from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol, TypeAlias, cast

from gh_slate.codec.json import DEFAULT_JSON_LIMITS, JsonValue, freeze_json
from gh_slate.data.errors import DataError
from gh_slate.errors import GhSlateError

PathSegment: TypeAlias = str | int
JsonPath: TypeAlias = tuple[PathSegment, ...]
MAX_PATH_SEGMENTS = DEFAULT_JSON_LIMITS.max_depth + 1
MAX_ARRAY_INDEX = DEFAULT_JSON_LIMITS.max_nodes - 1


class JqPathEvaluator(Protocol):
    """The small part of the isolated jq API needed to resolve a path."""

    def __call__(
        self,
        data: object,
        filter: str,
        *,
        max_results: int,
    ) -> Sequence[JsonValue]: ...


def _path_error(message: str, *, code: str, **details: object) -> DataError:
    return DataError(message, code=code, details=details)


def _evaluate_path_expression(
    data: object,
    filter_text: str,
    *,
    evaluator: JqPathEvaluator,
) -> tuple[JsonValue, ...]:
    try:
        results = tuple(
            evaluator(
                data,
                filter_text,
                max_results=1,
            )
        )
    except GhSlateError as error:
        if error.code != "jq_result_limit":
            raise
        raise _path_error(
            "jq path expression must resolve exactly one path",
            code="data_path_multiple_results",
            count_at_least=2,
        ) from None
    if len(results) > 1:
        raise _path_error(
            "jq path expression must resolve exactly one path",
            code="data_path_multiple_results",
            count_at_least=2,
        )
    return results


def _normalize_path(path: Sequence[object]) -> JsonPath:
    if isinstance(path, (str, bytes, bytearray)):
        raise _path_error(
            "JSON path must be an array of string or integer segments",
            code="data_path_invalid",
            value_type=type(path).__name__,
        )

    normalized: list[PathSegment] = []
    for position, segment in enumerate(path):
        if position >= MAX_PATH_SEGMENTS:
            raise _path_error(
                "JSON path exceeds the configured segment limit",
                code="data_path_limit",
                max_segments=MAX_PATH_SEGMENTS,
            )
        if isinstance(segment, str):
            normalized.append(segment)
            continue
        if isinstance(segment, bool):
            raise _path_error(
                "JSON path array indexes must be non-negative integers",
                code="data_path_invalid",
                position=position,
                value_type="boolean",
            )
        if isinstance(segment, int):
            index = segment
        elif isinstance(segment, Decimal) and segment.is_finite() and segment == segment.to_integral_value():
            index = int(segment)
        else:
            raise _path_error(
                "JSON path segments must be strings or non-negative integers",
                code="data_path_invalid",
                position=position,
                value_type=type(segment).__name__,
            )
        if index < 0:
            raise _path_error(
                "JSON path array indexes must be non-negative integers",
                code="data_path_invalid",
                position=position,
                index=index,
            )
        if index > MAX_ARRAY_INDEX:
            raise _path_error(
                "JSON path array index exceeds the configured limit",
                code="data_path_index_limit",
                position=position,
                index=index,
                max_index=MAX_ARRAY_INDEX,
            )
        normalized.append(index)
    return tuple(normalized)


def resolve_exact_path(
    data: object,
    expression: str,
    *,
    evaluator: JqPathEvaluator,
) -> JsonPath:
    """Resolve one exact jq path expression without transforming ``data``.

    jq evaluates only ``path(EXPR)`` and returns path segments. The actual
    read or mutation is subsequently performed in Python, so libjq's numeric
    representation can never round numbers in an untouched data subtree.
    """

    if not isinstance(expression, str):
        raise _path_error(
            "jq path expression must be a string",
            code="data_path_invalid",
            value_type=type(expression).__name__,
        )
    try:
        encoded = expression.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _path_error(
            "jq path expression is not valid Unicode",
            code="data_path_invalid",
            position=error.start,
        ) from None
    if not encoded.strip():
        raise _path_error("jq path expression must not be empty", code="data_path_invalid")

    # First require the expression to execute as a standalone jq program. A
    # fragment that closes delimiters belonging to the structural path wrapper
    # cannot be syntactically valid on its own.
    _evaluate_path_expression(
        data,
        expression,
        evaluator=evaluator,
    )
    # Leading and trailing newlines prevent a trailing jq comment from
    # swallowing the structural closing parentheses.
    results = _evaluate_path_expression(
        data,
        f"path((\n{expression}\n))",
        evaluator=evaluator,
    )
    if not results:
        raise _path_error(
            "jq path expression did not resolve a path",
            code="data_path_no_result",
        )
    result = results[0]
    if not isinstance(result, tuple):
        raise _path_error(
            "jq path expression did not produce a path array",
            code="data_path_invalid",
            value_type=type(result).__name__,
        )
    return _normalize_path(cast("tuple[object, ...]", result))


def _missing(position: int, segment: PathSegment) -> DataError:
    return _path_error(
        "JSON path does not exist",
        code="data_path_missing",
        position=position,
        segment=segment,
    )


def _type_mismatch(position: int, segment: PathSegment, value: object) -> DataError:
    return _path_error(
        "JSON path cannot traverse this value",
        code="data_path_type_mismatch",
        position=position,
        segment=segment,
        value_type=_json_type_name(value),
    )


def _out_of_bounds(position: int, index: int, length: int) -> DataError:
    return _path_error(
        "JSON path array index is out of bounds",
        code="data_path_index_out_of_bounds",
        position=position,
        index=index,
        length=length,
    )


def _json_type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Decimal):
        return "number"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, tuple):
        return "array"
    return type(value).__name__


def get_path(data: JsonValue, path: Sequence[object]) -> JsonValue:
    """Read one existing path, distinguishing absence from JSON ``null``."""

    normalized = _normalize_path(path)
    current: JsonValue = data
    for position, segment in enumerate(normalized):
        if isinstance(segment, str):
            if not isinstance(current, Mapping):
                raise _type_mismatch(position, segment, current)
            current_object = cast("Mapping[str, JsonValue]", current)
            if segment not in current_object:
                raise _missing(position, segment)
            current = cast("JsonValue", current_object[segment])
            continue

        if not isinstance(current, tuple):
            raise _type_mismatch(position, segment, current)
        if segment >= len(current):
            raise _out_of_bounds(position, segment, len(current))
        current = cast("JsonValue", current[segment])
    return current


def set_path(
    data: JsonValue,
    path: Sequence[object],
    value: object,
) -> JsonValue:
    """Persistently set a path while sharing every untouched subtree.

    This follows jq ``setpath`` container creation: missing/null containers are
    inferred from the next segment, and arrays are padded with JSON nulls up to
    the selected index.
    """

    normalized = _normalize_path(path)
    replacement = freeze_json(value)
    if not normalized:
        return data if replacement == data else replacement

    def replace(current: JsonValue, position: int) -> JsonValue:
        segment = normalized[position]
        final = position == len(normalized) - 1
        if isinstance(segment, str):
            if current is None:
                current = cast("JsonValue", MappingProxyType({}))
            if not isinstance(current, Mapping):
                raise _type_mismatch(position, segment, current)
            current_object = cast("Mapping[str, JsonValue]", current)
            if final:
                previous: JsonValue | object = current_object[segment] if segment in current_object else _MISSING
                if previous is not _MISSING and previous == replacement:
                    return current
                updated = dict(current_object)
                updated[segment] = replacement
                return MappingProxyType(updated)

            child = cast("JsonValue", current_object[segment]) if segment in current_object else None
            updated_child = replace(child, position + 1)
            if segment in current_object and updated_child is child:
                return current
            updated = dict(current_object)
            updated[segment] = updated_child
            return MappingProxyType(updated)

        if current is None:
            current = ()
        if not isinstance(current, tuple):
            raise _type_mismatch(position, segment, current)
        child = cast("JsonValue", current[segment]) if segment < len(current) else None
        updated_child = replacement if final else replace(child, position + 1)
        if segment < len(current) and updated_child == child:
            return current
        updated_array = list(current)
        updated_array.extend(None for _ in range(segment + 1 - len(updated_array)))
        updated_array[segment] = updated_child
        return tuple(updated_array)

    return replace(data, 0)


_MISSING = object()


def delete_path(
    data: JsonValue,
    path: Sequence[object],
    *,
    ignore_missing: bool = False,
) -> JsonValue:
    """Persistently delete one path."""

    return delete_paths(data, (path,), ignore_missing=ignore_missing)


def delete_paths(
    data: JsonValue,
    paths: Sequence[Sequence[object]],
    *,
    ignore_missing: bool = False,
) -> JsonValue:
    """Delete paths resolved against one snapshot.

    Array elements are removed according to their indexes in the original
    snapshot, so deleting indexes 0 and 1 removes the original two elements
    regardless of the order in which the paths were supplied.
    """

    normalized_paths: list[JsonPath] = []
    seen: set[JsonPath] = set()
    for path in paths:
        normalized = _normalize_path(path)
        if not normalized:
            raise _path_error(
                "the JSON document root cannot be deleted",
                code="data_path_root_delete",
            )
        try:
            get_path(data, normalized)
        except DataError as error:
            if ignore_missing and error.code in {
                "data_path_missing",
                "data_path_index_out_of_bounds",
            }:
                continue
            raise
        if normalized not in seen:
            normalized_paths.append(normalized)
            seen.add(normalized)

    if not normalized_paths:
        return data

    def remove(current: JsonValue, suffixes: Sequence[JsonPath], position: int) -> JsonValue:
        if isinstance(current, Mapping):
            current_object = cast("Mapping[str, JsonValue]", current)
            grouped: dict[str, list[JsonPath]] = {}
            for suffix in suffixes:
                segment = suffix[position]
                if not isinstance(segment, str):  # Verified by get_path above.
                    raise AssertionError("path type changed during deletion")
                grouped.setdefault(segment, []).append(suffix)

            updated: dict[str, JsonValue] | None = None
            for key, group in grouped.items():
                if any(len(path) == position + 1 for path in group):
                    if updated is None:
                        updated = dict(current_object)
                    del updated[key]
                    continue
                child = current_object[key]
                updated_child = remove(child, group, position + 1)
                if updated_child is not child:
                    if updated is None:
                        updated = dict(current_object)
                    updated[key] = updated_child
            return current if updated is None else MappingProxyType(updated)

        if isinstance(current, tuple):
            grouped_indexes: dict[int, list[JsonPath]] = {}
            for suffix in suffixes:
                segment = suffix[position]
                if not isinstance(segment, int):  # Verified by get_path above.
                    raise AssertionError("path type changed during deletion")
                grouped_indexes.setdefault(segment, []).append(suffix)

            changed = False
            updated_items: list[JsonValue] = []
            for index, item in enumerate(current):
                group = grouped_indexes.get(index)
                if group is None:
                    updated_items.append(item)
                    continue
                if any(len(path) == position + 1 for path in group):
                    changed = True
                    continue
                updated_child = remove(item, group, position + 1)
                changed = changed or updated_child is not item
                updated_items.append(updated_child)
            return tuple(updated_items) if changed else current

        raise AssertionError("path type changed during deletion")

    return remove(data, normalized_paths, 0)


__all__ = [
    "JqPathEvaluator",
    "JsonPath",
    "MAX_ARRAY_INDEX",
    "MAX_PATH_SEGMENTS",
    "PathSegment",
    "delete_path",
    "delete_paths",
    "get_path",
    "resolve_exact_path",
    "set_path",
]
