from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol, TypeAlias, cast

from gh_slate.codec.json import (
    DEFAULT_JSON_LIMITS,
    JsonValue,
    freeze_json,
    strict_loads,
)
from gh_slate.data.errors import DataError
from gh_slate.errors import GhSlateError

PathSegment: TypeAlias = str | int
JsonPath: TypeAlias = tuple[PathSegment, ...]
MAX_PATH_SEGMENTS = DEFAULT_JSON_LIMITS.max_depth + 1
MAX_ARRAY_INDEX = DEFAULT_JSON_LIMITS.max_nodes - 1
MAX_PATH_EXPRESSION_BYTES = 16 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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


def _dynamic_path(position: int) -> DataError:
    return _path_error(
        "data set/delete requires a static jq path",
        code="data_path_dynamic",
        position=position,
    )


def _skip_path_trivia(expression: str, position: int) -> int:
    while position < len(expression):
        while position < len(expression) and expression[position] in " \t\r\n":
            position += 1
        if position >= len(expression) or expression[position] != "#":
            return position
        newline = expression.find("\n", position + 1)
        if newline < 0:
            return len(expression)
        position = newline + 1
    return position


def _parse_static_path(expression: str) -> JsonPath:
    position = _skip_path_trivia(expression, 0)
    if position >= len(expression):
        raise _path_error(
            "jq path expression must not be empty",
            code="data_path_invalid",
        )
    if expression[position] != ".":
        raise _dynamic_path(position)
    position += 1
    segments: list[PathSegment] = []

    initial = _IDENTIFIER.match(expression, position)
    if initial is not None:
        segments.append(initial.group())
        position = initial.end()

    while True:
        position = _skip_path_trivia(expression, position)
        if position >= len(expression):
            return _normalize_path(segments)

        character = expression[position]
        if character == ".":
            if not segments:
                raise _dynamic_path(position)
            position += 1
            identifier = _IDENTIFIER.match(expression, position)
            if identifier is None:
                raise _dynamic_path(position)
            segments.append(identifier.group())
            position = identifier.end()
            continue

        if character != "[":
            raise _dynamic_path(position)
        position = _skip_path_trivia(expression, position + 1)
        if position >= len(expression):
            raise _dynamic_path(position)

        if expression[position] == '"':
            start = position
            position += 1
            escaped = False
            while position < len(expression):
                current = expression[position]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
                position += 1
            if position >= len(expression):
                raise _dynamic_path(start)
            token = expression[start : position + 1]
            try:
                key = strict_loads(token)
            except GhSlateError:
                raise _dynamic_path(start) from None
            if not isinstance(key, str):  # pragma: no cover - strict JSON string
                raise _dynamic_path(start)
            segment: PathSegment = key
            position += 1
        elif expression[position].isascii() and expression[position].isdigit():
            start = position
            while position < len(expression) and expression[position].isascii() and expression[position].isdigit():
                position += 1
            digits = expression[start:position]
            significant = digits.lstrip("0") or "0"
            maximum = str(MAX_ARRAY_INDEX)
            if len(significant) > len(maximum) or (len(significant) == len(maximum) and significant > maximum):
                raise _path_error(
                    "JSON path array index exceeds the configured limit",
                    code="data_path_index_limit",
                    position=len(segments),
                    max_index=MAX_ARRAY_INDEX,
                )
            segment = int(significant)
        else:
            raise _dynamic_path(position)

        position = _skip_path_trivia(expression, position)
        if position >= len(expression) or expression[position] != "]":
            raise _dynamic_path(position)
        segments.append(segment)
        position += 1


def resolve_exact_path(
    _data: object,
    expression: str,
    *,
    evaluator: JqPathEvaluator,
) -> JsonPath:
    """Resolve one static jq path without projecting the stored data to jq."""

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
    if len(encoded) > MAX_PATH_EXPRESSION_BYTES:
        raise _path_error(
            "jq path expression exceeds the configured byte limit",
            code="data_path_limit",
            max_bytes=MAX_PATH_EXPRESSION_BYTES,
        )

    parsed = _parse_static_path(expression)

    # Let jq validate its own quoted-key syntax, but only against JSON null.
    # The path is command text, never a projection of precision-sensitive
    # stored data.
    results = _evaluate_path_expression(
        None,
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
    normalized = _normalize_path(cast("tuple[object, ...]", result))
    if normalized != parsed:
        raise _path_error(
            "jq path validation disagreed with the static path parser",
            code="data_path_invalid",
        )
    return parsed


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
    "MAX_PATH_EXPRESSION_BYTES",
    "MAX_PATH_SEGMENTS",
    "PathSegment",
    "delete_path",
    "delete_paths",
    "get_path",
    "resolve_exact_path",
    "set_path",
]
