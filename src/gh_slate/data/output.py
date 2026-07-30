from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import cast

from gh_slate.codec.json import JsonValue, canonical_json_dumps, canonical_number, freeze_json


class JsonOutputMode(str, Enum):
    PRETTY = "pretty"
    COMPACT = "compact"
    RAW = "raw"


def pretty_json(value: object) -> str:
    """Serialize deterministic, typed JSON with two-space indentation."""

    frozen = freeze_json(value)
    pieces: list[str] = []

    def write(current: JsonValue, depth: int) -> None:
        if current is None:
            pieces.append("null")
        elif current is True:
            pieces.append("true")
        elif current is False:
            pieces.append("false")
        elif isinstance(current, str):
            pieces.append(json.dumps(current, ensure_ascii=False, separators=(",", ":")))
        elif isinstance(current, Decimal):
            pieces.append(canonical_number(current))
        elif isinstance(current, Mapping):
            current_object = cast("Mapping[str, JsonValue]", current)
            if not current_object:
                pieces.append("{}")
                return
            pieces.append("{\n")
            keys = sorted(current_object)
            for index, key in enumerate(keys):
                pieces.append("  " * (depth + 1))
                pieces.append(json.dumps(key, ensure_ascii=False, separators=(",", ":")))
                pieces.append(": ")
                write(current_object[key], depth + 1)
                pieces.append(",\n" if index < len(keys) - 1 else "\n")
            pieces.append("  " * depth)
            pieces.append("}")
        elif isinstance(current, tuple):
            if not current:
                pieces.append("[]")
                return
            pieces.append("[\n")
            for index, item in enumerate(current):
                pieces.append("  " * (depth + 1))
                write(item, depth + 1)
                pieces.append(",\n" if index < len(current) - 1 else "\n")
            pieces.append("  " * depth)
            pieces.append("]")
        else:  # pragma: no cover - freeze_json makes this unreachable.
            raise AssertionError(f"unsupported JSON value: {type(current).__name__}")

    write(frozen, 0)
    return "".join(pieces)


def format_json_value(
    value: object,
    *,
    compact: bool = False,
    raw: bool = False,
) -> str:
    """Format one jq result.

    Raw mode removes JSON quoting only for strings. Every other JSON type keeps
    its JSON representation, matching jq's ``--raw-output`` type behavior.
    """

    frozen = freeze_json(value)
    if raw and isinstance(frozen, str):
        return frozen
    if compact:
        return canonical_json_dumps(frozen)
    return pretty_json(frozen)


def format_json_results(
    results: Sequence[JsonValue],
    *,
    compact: bool = False,
    raw: bool = False,
) -> str:
    """Format zero or more jq results, with one terminating LF per result."""

    return "".join(f"{format_json_value(result, compact=compact, raw=raw)}\n" for result in results)


def json_exit_status(results: Sequence[JsonValue]) -> int:
    """Return jq-compatible ``--exit-status`` for already-evaluated results."""

    if not results:
        return 4
    return 1 if results[-1] is None or results[-1] is False else 0


__all__ = [
    "JsonOutputMode",
    "format_json_results",
    "format_json_value",
    "json_exit_status",
    "pretty_json",
]
