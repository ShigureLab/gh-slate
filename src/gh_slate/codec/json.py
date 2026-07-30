from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal, DecimalException
from types import MappingProxyType
from typing import TypeAlias, cast

from gh_slate.codec.errors import CodecError

JsonScalar: TypeAlias = None | bool | str | Decimal
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | tuple["JsonValue", ...]

# Keep the conventional all-caps spelling available to callers while JsonValue
# remains the public spelling used by the codec API.
JSONValue: TypeAlias = JsonValue


@dataclass(frozen=True, slots=True)
class JsonLimits:
    max_input_bytes: int = 8 * 1024 * 1024
    max_depth: int = 64
    max_nodes: int = 100_000
    max_string_bytes: int = 1024 * 1024
    max_number_chars: int = 1024
    max_key_bytes: int = 64 * 1024


DEFAULT_JSON_LIMITS = JsonLimits()

_BOM_PREFIXES = (
    b"\xef\xbb\xbf",  # UTF-8
    b"\xff\xfe\x00\x00",  # UTF-32 little-endian
    b"\x00\x00\xfe\xff",  # UTF-32 big-endian
    b"\xff\xfe",  # UTF-16 little-endian
    b"\xfe\xff",  # UTF-16 big-endian
)


def _codec_error(message: str, *, code: str, **details: object) -> CodecError:
    return CodecError(message, code=code, details=details)


def _validate_limits(limits: JsonLimits) -> None:
    for item in fields(limits):
        value = getattr(limits, item.name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _codec_error(
                f"{item.name} must be a non-negative integer",
                code="json_limit_exceeded",
                limit=item.name,
                configured=value,
                reason="invalid_limit",
            )


def _limit_error(limit: str, maximum: int, actual: int, *, path: str = "$") -> CodecError:
    return _codec_error(
        f"JSON exceeds {limit}",
        code="json_limit_exceeded",
        limit=limit,
        maximum=maximum,
        actual=actual,
        path=path,
    )


def _utf8_size(value: str, *, path: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise _codec_error(
            "JSON contains an unpaired Unicode surrogate",
            code="json_unpaired_surrogate",
            path=path,
            position=error.start,
        ) from None


def _source_text(source: str | bytes, limits: JsonLimits) -> str:
    if isinstance(source, bytes):
        if len(source) > limits.max_input_bytes:
            raise _limit_error("max_input_bytes", limits.max_input_bytes, len(source))
        if source.startswith(_BOM_PREFIXES):
            raise _codec_error("JSON must not contain a byte-order mark", code="json_bom")
        try:
            text = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise _codec_error(
                "JSON input is not valid UTF-8",
                code="json_invalid",
                reason="invalid_utf8",
                position=error.start,
            ) from None
    elif isinstance(source, str):
        if source.startswith("\ufeff"):
            raise _codec_error("JSON must not contain a byte-order mark", code="json_bom")
        source_size = _utf8_size(source, path="$")
        if source_size > limits.max_input_bytes:
            raise _limit_error("max_input_bytes", limits.max_input_bytes, source_size)
        text = source
    else:
        raise _codec_error(
            "JSON input must be text or UTF-8 bytes",
            code="json_type_unsupported",
            value_type=type(source).__name__,
        )

    return text


def strict_loads(
    source: str | bytes,
    *,
    limits: JsonLimits = DEFAULT_JSON_LIMITS,
) -> JsonValue:
    """Parse JSON into immutable values using Decimal for every JSON number."""

    _validate_limits(limits)
    text = _source_text(source, limits)

    def parse_number(token: str) -> Decimal:
        if len(token) > limits.max_number_chars:
            raise _limit_error("max_number_chars", limits.max_number_chars, len(token))
        try:
            value = Decimal(token)
        except DecimalException:
            raise _codec_error(
                "JSON contains an invalid number",
                code="json_number_invalid",
                value=token,
            ) from None
        if not value.is_finite():
            raise _codec_error(
                "JSON numbers must be finite",
                code="json_number_invalid",
                value=token,
            )
        return value

    def reject_constant(token: str) -> Decimal:
        raise _codec_error(
            "JSON numbers must be finite",
            code="json_number_invalid",
            value=token,
        )

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _codec_error(
                    "JSON objects must not contain duplicate keys",
                    code="json_duplicate_key",
                    key=key,
                )
            result[key] = value
        return result

    try:
        parsed = json.loads(
            text,
            parse_float=parse_number,
            parse_int=parse_number,
            parse_constant=reject_constant,
            object_pairs_hook=object_from_pairs,
        )
    except CodecError:
        raise
    except json.JSONDecodeError as error:
        raise _codec_error(
            "JSON input is malformed",
            code="json_invalid",
            line=error.lineno,
            column=error.colno,
            position=error.pos,
        ) from None
    except RecursionError:
        raise _codec_error(
            "JSON nesting exceeds the supported depth",
            code="json_limit_exceeded",
            limit="max_depth",
            maximum=limits.max_depth,
        ) from None
    except (DecimalException, ValueError):
        raise _codec_error("JSON contains an invalid number", code="json_number_invalid") from None

    return _freeze_json(parsed, limits=limits, check_number_limits=False)


def strict_loads_object(
    source: str | bytes,
    *,
    limits: JsonLimits = DEFAULT_JSON_LIMITS,
) -> Mapping[str, JsonValue]:
    value = strict_loads(source, limits=limits)
    if not isinstance(value, Mapping):
        raise _codec_error(
            "JSON document root must be an object",
            code="json_root_not_object",
            value_type=_json_type_name(value),
        )
    return cast("Mapping[str, JsonValue]", value)


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


def freeze_json(
    value: object,
    *,
    limits: JsonLimits = DEFAULT_JSON_LIMITS,
) -> JsonValue:
    """Validate and deeply freeze an in-memory JSON-compatible value."""

    return _freeze_json(value, limits=limits, check_number_limits=True)


def _freeze_json(
    value: object,
    *,
    limits: JsonLimits,
    check_number_limits: bool,
) -> JsonValue:
    _validate_limits(limits)
    nodes = 0
    active_containers: set[int] = set()

    def freeze(current: object, *, depth: int, path: str) -> JsonValue:
        nonlocal nodes
        nodes += 1
        if nodes > limits.max_nodes:
            raise _limit_error("max_nodes", limits.max_nodes, nodes, path=path)

        if current is None or isinstance(current, bool):
            return current

        if isinstance(current, str):
            size = _utf8_size(current, path=path)
            if size > limits.max_string_bytes:
                raise _limit_error("max_string_bytes", limits.max_string_bytes, size, path=path)
            return current

        if isinstance(current, float):
            raise _codec_error(
                "Python float values are not accepted as JSON state",
                code="json_type_unsupported",
                path=path,
                value_type="float",
            )

        if isinstance(current, int):
            try:
                number = Decimal(current)
            except (DecimalException, ValueError):
                raise _codec_error(
                    "JSON contains an invalid number",
                    code="json_number_invalid",
                    path=path,
                ) from None
            if check_number_limits:
                _check_decimal_limit(number, limits=limits, path=path)
            return number

        if isinstance(current, Decimal):
            if not current.is_finite():
                raise _codec_error(
                    "JSON numbers must be finite",
                    code="json_number_invalid",
                    path=path,
                )
            if check_number_limits:
                _check_decimal_limit(current, limits=limits, path=path)
            return current

        if isinstance(current, Mapping):
            if depth > limits.max_depth:
                raise _limit_error("max_depth", limits.max_depth, depth, path=path)
            identity = id(current)
            if identity in active_containers:
                raise _codec_error(
                    "JSON values must not contain reference cycles",
                    code="json_type_unsupported",
                    path=path,
                    reason="cycle",
                )
            active_containers.add(identity)
            try:
                frozen_object: dict[str, JsonValue] = {}
                for key, item in current.items():
                    if not isinstance(key, str):
                        raise _codec_error(
                            "JSON object keys must be strings",
                            code="json_type_unsupported",
                            path=path,
                            key_type=type(key).__name__,
                        )
                    key_size = _utf8_size(key, path=path)
                    if key_size > limits.max_key_bytes:
                        raise _limit_error("max_key_bytes", limits.max_key_bytes, key_size, path=path)
                    child_path = f"{path}.{key}" if key.isidentifier() else f"{path}[{key!r}]"
                    frozen_object[key] = freeze(item, depth=depth + 1, path=child_path)
                return MappingProxyType(frozen_object)
            finally:
                active_containers.remove(identity)

        if isinstance(current, (list, tuple)):
            if depth > limits.max_depth:
                raise _limit_error("max_depth", limits.max_depth, depth, path=path)
            identity = id(current)
            if identity in active_containers:
                raise _codec_error(
                    "JSON values must not contain reference cycles",
                    code="json_type_unsupported",
                    path=path,
                    reason="cycle",
                )
            active_containers.add(identity)
            try:
                return tuple(
                    freeze(item, depth=depth + 1, path=f"{path}[{index}]") for index, item in enumerate(current)
                )
            finally:
                active_containers.remove(identity)

        raise _codec_error(
            "value is not JSON-compatible",
            code="json_type_unsupported",
            path=path,
            value_type=type(current).__name__,
        )

    try:
        return freeze(value, depth=0, path="$")
    except CodecError:
        raise
    except RecursionError:
        raise _codec_error(
            "JSON nesting exceeds the supported depth",
            code="json_limit_exceeded",
            limit="max_depth",
            maximum=limits.max_depth,
        ) from None
    except (DecimalException, UnicodeError, ValueError):
        raise _codec_error("JSON value cannot be represented safely", code="json_invalid") from None


def _check_decimal_limit(value: Decimal, *, limits: JsonLimits, path: str) -> None:
    lexical_chars = len(str(value))
    if lexical_chars > limits.max_number_chars:
        raise _limit_error("max_number_chars", limits.max_number_chars, lexical_chars, path=path)


def canonical_number(value: Decimal | int) -> str:
    """Serialize a finite number according to the gh-slate state-v1 rule."""

    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise _codec_error(
            "canonical JSON numbers must be Decimal or int values",
            code="json_type_unsupported",
            value_type=type(value).__name__,
        )

    try:
        number = value if isinstance(value, Decimal) else Decimal(value)
    except (DecimalException, ValueError):
        raise _codec_error("JSON contains an invalid number", code="json_number_invalid") from None

    if not number.is_finite():
        raise _codec_error("JSON numbers must be finite", code="json_number_invalid")
    if number.is_zero():
        return "0"

    sign, decimal_digits, exponent = number.as_tuple()
    if not isinstance(exponent, int):  # Defensive narrowing for DecimalTuple's special-value exponents.
        raise _codec_error("JSON contains an invalid number", code="json_number_invalid")
    digits = list(decimal_digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1

    digit_text = "".join(str(digit) for digit in digits)
    adjusted = len(digits) + exponent - 1
    prefix = "-" if sign else ""

    if -6 <= adjusted < 21:
        point = len(digits) + exponent
        if point <= 0:
            magnitude = f"0.{('0' * -point)}{digit_text}"
        elif point >= len(digits):
            magnitude = f"{digit_text}{'0' * (point - len(digits))}"
        else:
            magnitude = f"{digit_text[:point]}.{digit_text[point:]}"
        return f"{prefix}{magnitude}"

    coefficient = digit_text[0]
    if len(digit_text) > 1:
        coefficient = f"{coefficient}.{digit_text[1:]}"
    exponent_text = f"+{adjusted}" if adjusted >= 0 else str(adjusted)
    return f"{prefix}{coefficient}e{exponent_text}"


def canonical_json_bytes(
    value: object,
    *,
    limits: JsonLimits = DEFAULT_JSON_LIMITS,
) -> bytes:
    """Return deterministic UTF-8 canonical JSON bytes for a JSON value."""

    frozen = freeze_json(value, limits=limits)
    pieces: list[str] = []

    def write(current: JsonValue) -> None:
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
            pieces.append("{")
            for index, key in enumerate(sorted(current_object)):
                if index:
                    pieces.append(",")
                pieces.append(json.dumps(key, ensure_ascii=False, separators=(",", ":")))
                pieces.append(":")
                write(current_object[key])
            pieces.append("}")
        elif isinstance(current, tuple):
            pieces.append("[")
            for index, item in enumerate(current):
                if index:
                    pieces.append(",")
                write(item)
            pieces.append("]")
        else:  # pragma: no cover - freeze_json makes this unreachable.
            raise _codec_error(
                "value is not JSON-compatible",
                code="json_type_unsupported",
                value_type=type(current).__name__,
            )

    try:
        write(frozen)
        return "".join(pieces).encode("utf-8", errors="strict")
    except CodecError:
        raise
    except (RecursionError, UnicodeError):
        raise _codec_error(
            "JSON value cannot be serialized within the configured limits",
            code="json_limit_exceeded",
            limit="max_depth",
            maximum=limits.max_depth,
        ) from None


def canonical_json_dumps(
    value: object,
    *,
    limits: JsonLimits = DEFAULT_JSON_LIMITS,
) -> str:
    return canonical_json_bytes(value, limits=limits).decode("utf-8")


__all__ = [
    "DEFAULT_JSON_LIMITS",
    "JSONValue",
    "JsonLimits",
    "JsonValue",
    "canonical_json_bytes",
    "canonical_json_dumps",
    "canonical_number",
    "freeze_json",
    "strict_loads",
    "strict_loads_object",
]
