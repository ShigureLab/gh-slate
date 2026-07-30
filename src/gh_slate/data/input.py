from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from gh_slate.codec.json import (
    DEFAULT_JSON_LIMITS,
    JsonValue,
    canonical_json_bytes,
    freeze_json,
    strict_loads,
)
from gh_slate.data.errors import DataError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from os import PathLike

DEFAULT_DATA_INPUT_BYTES = DEFAULT_JSON_LIMITS.max_input_bytes


class InputStream(Protocol):
    def read(self, size: int = -1, /) -> str | bytes: ...


def _input_error(message: str, *, code: str, **details: object) -> DataError:
    return DataError(message, code=code, details=details)


def _checked_limit(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    return max_bytes


def _as_bytes(value: str | bytes, *, subject: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise _input_error(
            f"{subject} did not produce text or bytes",
            code="data_input_invalid",
            value_type=type(value).__name__,
        )
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _input_error(
            f"{subject} is not valid Unicode",
            code="data_input_invalid",
            position=error.start,
        ) from None


def _read_stream(
    stream: InputStream,
    *,
    subject: str,
    max_bytes: int,
) -> bytes:
    try:
        source = _as_bytes(stream.read(max_bytes + 1), subject=subject)
    except DataError:
        raise
    except (OSError, ValueError) as error:
        raise _input_error(
            f"{subject} could not be read",
            code="data_input_read_failed",
            error_type=type(error).__name__,
        ) from None
    if len(source) > max_bytes:
        raise _input_error(
            f"{subject} exceeds the configured input byte limit",
            code="data_input_size_limit",
            actual_bytes=len(source),
            max_bytes=max_bytes,
        )
    return source


def _stdin_stream(stdin: InputStream | None) -> InputStream:
    if stdin is not None:
        return stdin
    return getattr(sys.stdin, "buffer", sys.stdin)


def _read_location(
    location: str | PathLike[str],
    *,
    stdin: InputStream | None,
    subject: str,
    max_bytes: int,
) -> bytes:
    if str(location) == "-":
        return _read_stream(
            _stdin_stream(stdin),
            subject=subject,
            max_bytes=max_bytes,
        )

    path = Path(location)
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise _input_error(
                f"{subject} exceeds the configured input byte limit",
                code="data_input_size_limit",
                path=str(path),
                actual_bytes=size,
                max_bytes=max_bytes,
            )
        with path.open("rb") as stream:
            return _read_stream(stream, subject=subject, max_bytes=max_bytes)
    except DataError:
        raise
    except OSError as error:
        raise _input_error(
            f"{subject} could not be read",
            code="data_input_read_failed",
            path=str(path),
            error_type=type(error).__name__,
        ) from None


def _parse_json(source: str | bytes, *, max_bytes: int) -> JsonValue:
    limits = replace(DEFAULT_JSON_LIMITS, max_input_bytes=max_bytes)
    return strict_loads(source, limits=limits)


def load_value(
    *,
    value: str | None = None,
    value_string: str | None = None,
    value_file: str | PathLike[str] | None = None,
    stdin: InputStream | None = None,
    max_bytes: int = DEFAULT_DATA_INPUT_BYTES,
) -> JsonValue:
    """Load exactly one explicitly selected ``data set`` value source."""

    maximum = _checked_limit(max_bytes)
    selected = (value is not None, value_string is not None, value_file is not None)
    count = sum(selected)
    if count == 0:
        raise _input_error(
            "one value source is required",
            code="data_value_source_missing",
        )
    if count != 1:
        raise _input_error(
            "value sources are mutually exclusive",
            code="data_value_source_conflict",
        )

    if value_string is not None:
        encoded = _as_bytes(value_string, subject="string value")
        if len(encoded) > maximum:
            raise _input_error(
                "string value exceeds the configured input byte limit",
                code="data_input_size_limit",
                actual_bytes=len(encoded),
                max_bytes=maximum,
            )
        return freeze_json(value_string)

    if value_file is not None:
        return _parse_json(
            _read_location(
                value_file,
                stdin=stdin,
                subject="JSON value file",
                max_bytes=maximum,
            ),
            max_bytes=maximum,
        )

    assert value is not None
    source: str | bytes
    if value == "-":
        source = _read_stream(
            _stdin_stream(stdin),
            subject="JSON value",
            max_bytes=maximum,
        )
    else:
        encoded = _as_bytes(value, subject="JSON value")
        if len(encoded) > maximum:
            raise _input_error(
                "JSON value exceeds the configured input byte limit",
                code="data_input_size_limit",
                actual_bytes=len(encoded),
                max_bytes=maximum,
            )
        source = encoded
    return _parse_json(source, max_bytes=maximum)


def load_argjson(
    source: str,
    *,
    stdin: InputStream | None = None,
    max_bytes: int = DEFAULT_DATA_INPUT_BYTES,
) -> JsonValue:
    """Parse one ``--argjson`` value; only an explicit ``@FILE`` reads a file."""

    maximum = _checked_limit(max_bytes)
    if not isinstance(source, str):
        raise _input_error(
            "--argjson value must be a string",
            code="data_argjson_invalid",
            value_type=type(source).__name__,
        )
    if source.startswith("@"):
        location = source[1:]
        if not location:
            raise _input_error(
                "--argjson @FILE must include a file path",
                code="data_argjson_invalid",
            )
        document = _read_location(
            location,
            stdin=stdin,
            subject="--argjson file",
            max_bytes=maximum,
        )
        return _parse_json(document, max_bytes=maximum)

    encoded = _as_bytes(source, subject="--argjson value")
    if len(encoded) > maximum:
        raise _input_error(
            "--argjson value exceeds the configured input byte limit",
            code="data_input_size_limit",
            actual_bytes=len(encoded),
            max_bytes=maximum,
        )
    return _parse_json(encoded, max_bytes=maximum)


def merge_jq_arguments(
    string_arguments: Sequence[Sequence[str]],
    json_arguments: Sequence[Sequence[str]],
    *,
    stdin: InputStream | None = None,
    max_bytes: int = DEFAULT_DATA_INPUT_BYTES,
) -> Mapping[str, JsonValue]:
    """Validate and merge repeatable ``--arg`` and ``--argjson`` bindings."""

    merged: dict[str, JsonValue] = {}
    for kind, arguments in (("--arg", string_arguments), ("--argjson", json_arguments)):
        for argument in arguments:
            if len(argument) != 2:
                raise _input_error(
                    f"{kind} requires a name and value",
                    code="data_jq_argument_invalid",
                )
            name, source = argument
            if (
                not name
                or not name.isascii()
                or not (name[0] == "_" or name[0].isalpha())
                or not all(character == "_" or character.isalnum() for character in name)
            ):
                raise _input_error(
                    "jq argument names must be identifiers",
                    code="data_jq_argument_invalid",
                    name=name,
                )
            if name in merged:
                raise _input_error(
                    "jq argument names must be unique",
                    code="data_jq_argument_duplicate",
                    name=name,
                )
            merged[name] = (
                load_value(
                    value_string=source,
                    max_bytes=max_bytes,
                )
                if kind == "--arg"
                else load_argjson(
                    source,
                    stdin=stdin,
                    max_bytes=max_bytes,
                )
            )
            encoded_bytes = len(canonical_json_bytes(merged))
            if encoded_bytes > max_bytes:
                raise _input_error(
                    "jq arguments exceed the configured cumulative byte limit",
                    code="data_input_size_limit",
                    actual_bytes=encoded_bytes,
                    max_bytes=max_bytes,
                )
    return MappingProxyType(merged)


__all__ = [
    "DEFAULT_DATA_INPUT_BYTES",
    "InputStream",
    "load_argjson",
    "load_value",
    "merge_jq_arguments",
]
