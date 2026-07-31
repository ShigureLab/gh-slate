from __future__ import annotations

import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

import regex
from jsonschema.exceptions import ValidationError

from gh_slate.codec.json import canonical_json_bytes
from gh_slate.schema._markers import FalseSchema

EVALUATION_TIMEOUT_SECONDS = 1.0
REGEX_TIMEOUT_SECONDS = 0.05
MAX_EVALUATION_OPERATIONS = 100_000

_ECMASCRIPT_CLASS_ESCAPES = {
    "d": r"0-9",
    "D": r"\x00-\x2F\x3A-\U0010FFFF",
    "w": r"A-Za-z0-9_",
    "W": r"\x00-\x2F\x3A-\x40\x5B-\x5E\x60\x7B-\U0010FFFF",
    "s": r"\x09-\x0D\x20\xA0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000\uFEFF",
    "S": (
        r"\x00-\x08\x0E-\x1F\x21-\x9F\xA1-\u167F\u1681-\u1FFF"
        r"\u200B-\u2027\u202A-\u202E\u2030-\u205E\u2060-\u2FFF"
        r"\u3001-\uFEFE\uFF00-\U0010FFFF"
    ),
}
_ECMASCRIPT_ATOM_ESCAPES = {
    "d": r"[0-9]",
    "D": r"[^0-9]",
    "w": r"[A-Za-z0-9_]",
    "W": r"[^A-Za-z0-9_]",
    "s": f"[{_ECMASCRIPT_CLASS_ESCAPES['s']}]",
    "S": f"[^{_ECMASCRIPT_CLASS_ESCAPES['s']}]",
}
_ECMASCRIPT_PASSTHROUGH_ESCAPES = frozenset("fnrtvpuPx")


class SchemaEvaluationLimitExceeded(Exception):
    """Internal signal raised when deterministic local evaluation exceeds budget."""


@dataclass(slots=True)
class _EvaluationBudget:
    deadline: float
    operations: int = 0


_ACTIVE_BUDGET: ContextVar[_EvaluationBudget | None] = ContextVar(
    "gh_slate_schema_evaluation_budget",
    default=None,
)


def _checkpoint(cost: int = 1) -> None:
    budget = _ACTIVE_BUDGET.get()
    if budget is None:
        return
    budget.operations += cost
    if budget.operations > MAX_EVALUATION_OPERATIONS or time.monotonic() > budget.deadline:
        raise SchemaEvaluationLimitExceeded


@contextmanager
def evaluation_budget() -> Generator[None]:
    token = _ACTIVE_BUDGET.set(_EvaluationBudget(deadline=time.monotonic() + EVALUATION_TIMEOUT_SECONDS))
    try:
        yield
    finally:
        _ACTIVE_BUDGET.reset(token)


def bounded_keyword(keyword: Any) -> Any:
    """Wrap a jsonschema keyword implementation with a shared checkpoint."""

    def validate(
        validator: Any,
        value: object,
        instance: object,
        schema: object,
    ) -> Generator[ValidationError]:
        _checkpoint()
        errors = keyword(validator, value, instance, schema)
        if errors is not None:
            yield from errors

    return validate


def _is_escaped(source: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and source[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _class_escape_is_range_endpoint(
    source: str,
    *,
    escape_index: int,
    content_start: int,
) -> bool:
    previous = escape_index - 1
    if previous > content_start and source[previous] == "-" and not _is_escaped(source, previous):
        return True
    following = escape_index + 2
    return following < len(source) - 1 and source[following] == "-" and source[following + 1] != "]"


def _ecmascript_pattern(source: str) -> str:
    """Translate the supported ECMA-262 regex surface to ``regex`` syntax."""

    result: list[str] = []
    in_class = False
    class_content_start = -1
    index = 0
    while index < len(source):
        character = source[index]
        if character == "\\":
            if index + 1 >= len(source):
                raise ValueError("trailing regex escape")
            escaped = source[index + 1]
            if escaped == "c":
                if index + 2 >= len(source) or not source[index + 2].isascii() or not source[index + 2].isalpha():
                    raise ValueError("invalid ECMA-262 control escape")
                codepoint = ord(source[index + 2].upper()) % 32
                result.append(rf"\x{codepoint:02X}")
                index += 3
                continue
            if escaped in _ECMASCRIPT_CLASS_ESCAPES:
                if in_class:
                    if _class_escape_is_range_endpoint(
                        source,
                        escape_index=index,
                        content_start=class_content_start,
                    ):
                        raise ValueError("character-class escape cannot be a range endpoint")
                    result.append(_ECMASCRIPT_CLASS_ESCAPES[escaped])
                else:
                    result.append(_ECMASCRIPT_ATOM_ESCAPES[escaped])
                index += 2
                continue
            if escaped in {"b", "B"}:
                if in_class:
                    if escaped == "B":
                        raise ValueError("invalid character-class escape")
                    result.append(r"\x08")
                else:
                    result.append(rf"(?a:\{escaped})")
                index += 2
                continue
            if escaped == "k":
                if in_class or index + 2 >= len(source) or source[index + 2] != "<":
                    raise ValueError("invalid ECMA-262 named backreference")
                closing_bracket = source.find(">", index + 3)
                if closing_bracket < 0 or closing_bracket == index + 3:
                    raise ValueError("invalid ECMA-262 named backreference")
                name = source[index + 3 : closing_bracket]
                result.append(rf"\g<{name}>")
                index = closing_bracket + 1
                continue
            if escaped.isascii() and escaped.isalpha() and escaped not in _ECMASCRIPT_PASSTHROUGH_ESCAPES:
                raise ValueError("unsupported ECMA-262 regex escape")
            result.extend((character, escaped))
            index += 2
            continue

        if not in_class and character == "[":
            in_class = True
            class_content_start = index + 1
            if class_content_start < len(source) and source[class_content_start] == "^":
                class_content_start += 1
            result.append(character)
        elif in_class and character == "]":
            in_class = False
            result.append(character)
        elif not in_class and character == ".":
            result.append(r"[^\n\r\u2028\u2029]")
        elif not in_class and character == "$":
            result.append(r"\Z")
        else:
            result.append(character)
        index += 1
    if in_class:
        raise ValueError("unterminated character class")
    return "".join(result)


def is_supported_regex(value: object) -> bool:
    """Return whether *value* is valid in the supported ECMA-262 dialect."""

    if not isinstance(value, str):
        return True
    try:
        regex.compile(_ecmascript_pattern(value), flags=regex.VERSION0)
    except (ValueError, regex.error):
        return False
    return True


def _compile(pattern: str) -> Any:
    _checkpoint()
    try:
        return regex.compile(_ecmascript_pattern(pattern), flags=regex.VERSION0)
    except (ValueError, regex.error) as error:
        raise SchemaEvaluationLimitExceeded from error


def _matches(compiled: Any, value: str) -> bool:
    _checkpoint()
    try:
        return (
            compiled.search(
                value,
                timeout=REGEX_TIMEOUT_SECONDS,
            )
            is not None
        )
    except TimeoutError as error:
        raise SchemaEvaluationLimitExceeded from error


def pattern_matches(pattern_value: str, value: str) -> bool:
    """Return whether *value* matches a bounded supported ECMA-262 pattern."""

    return _matches(_compile(pattern_value), value)


def pattern(
    validator: Any,
    pattern_value: object,
    instance: object,
    _schema: object,
) -> Generator[ValidationError]:
    if not validator.is_type(instance, "string"):
        return
    compiled = _compile(str(pattern_value))
    if not _matches(compiled, str(instance)):
        yield ValidationError("string does not match the required pattern")


def pattern_properties(
    validator: Any,
    pattern_schemas: object,
    instance: object,
    _schema: object,
) -> Generator[ValidationError]:
    if not validator.is_type(instance, "object"):
        return
    assert isinstance(pattern_schemas, Mapping)
    assert isinstance(instance, Mapping)
    compiled_patterns = [
        (str(pattern_value), _compile(str(pattern_value)), subschema)
        for pattern_value, subschema in pattern_schemas.items()
    ]
    for key, value in instance.items():
        for pattern_value, compiled, subschema in compiled_patterns:
            if _matches(compiled, str(key)):
                yield from validator.descend(
                    value,
                    subschema,
                    path=key,
                    schema_path=pattern_value,
                )


def _extra_properties(
    instance: Mapping[str, object],
    schema: Mapping[str, object],
) -> list[str]:
    declared = schema.get("properties", {})
    declared_keys = declared if isinstance(declared, Mapping) else {}
    patterns = schema.get("patternProperties", {})
    compiled = [_compile(str(pattern_value)) for pattern_value in patterns] if isinstance(patterns, Mapping) else []
    extras: list[str] = []
    for key in instance:
        _checkpoint()
        if key in declared_keys:
            continue
        if any(_matches(pattern, str(key)) for pattern in compiled):
            continue
        extras.append(key)
    return extras


def additional_properties(
    validator: Any,
    additional: object,
    instance: object,
    schema: object,
) -> Generator[ValidationError]:
    if not validator.is_type(instance, "object"):
        return
    assert isinstance(instance, Mapping)
    assert isinstance(schema, Mapping)
    typed_instance = cast("Mapping[str, object]", instance)
    typed_schema = cast("Mapping[str, object]", schema)
    extras = _extra_properties(typed_instance, typed_schema)
    if isinstance(additional, FalseSchema):
        additional = False
    if validator.is_type(additional, "object"):
        for key in extras:
            yield from validator.descend(
                typed_instance[key],
                additional,
                path=key,
            )
    elif not additional and extras:
        yield ValidationError("one or more additional properties are not allowed")


def items(
    validator: Any,
    item_schema: object,
    instance: object,
    schema: object,
) -> Generator[ValidationError]:
    """Validate post-prefix items while preserving parent-keyword diagnostics."""

    if not validator.is_type(instance, "array"):
        return
    _checkpoint()
    assert isinstance(instance, list)
    assert isinstance(schema, Mapping)
    prefix_items = schema.get("prefixItems")
    prefix = len(prefix_items) if isinstance(prefix_items, (list, tuple)) else 0
    total = len(instance)
    extra = total - prefix
    if extra <= 0:
        return

    if isinstance(item_schema, FalseSchema) or item_schema is False:
        rest = instance[prefix:] if extra != 1 else instance[prefix]
        item = "items" if prefix != 1 else "item"
        yield ValidationError(f"Expected at most {prefix} {item} but found {extra} extra: {rest!r}")
        return

    for index in range(prefix, total):
        _checkpoint()
        yield from validator.descend(
            instance=instance[index],
            schema=item_schema,
            path=index,
        )


def _is_valid(errors: Generator[ValidationError]) -> bool:
    return next(errors, None) is None


def _evaluated_property_keys(
    validator: Any,
    instance: Mapping[str, object],
    schema: object,
) -> set[str]:
    _checkpoint()
    if isinstance(schema, bool) or not isinstance(schema, Mapping):
        return set()

    evaluated: set[str] = set()
    for reference_keyword in ("$ref", "$dynamicRef"):
        reference = schema.get(reference_keyword)
        if reference is None:
            continue
        resolved = validator._resolver.lookup(reference)
        evaluated.update(
            _evaluated_property_keys(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            )
        )

    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        typed_properties = cast("Mapping[str, object]", properties)
        evaluated.update(typed_properties.keys() & instance.keys())

    for keyword in ("additionalProperties", "unevaluatedProperties"):
        subschema = schema.get(keyword)
        if subschema is None:
            continue
        for key, value in instance.items():
            _checkpoint()
            if _is_valid(validator.descend(value, subschema)):
                evaluated.add(key)

    patterns = schema.get("patternProperties")
    if isinstance(patterns, Mapping):
        compiled = [_compile(str(pattern_value)) for pattern_value in patterns]
        for key in instance:
            if any(_matches(pattern_value, str(key)) for pattern_value in compiled):
                evaluated.add(key)

    dependent = schema.get("dependentSchemas")
    if isinstance(dependent, Mapping):
        for key, subschema in dependent.items():
            if key in instance:
                evaluated.update(
                    _evaluated_property_keys(
                        validator,
                        instance,
                        subschema,
                    )
                )

    for keyword in ("allOf", "oneOf", "anyOf"):
        subschemas = schema.get(keyword, ())
        if not isinstance(subschemas, list):
            continue
        for subschema in subschemas:
            if _is_valid(validator.descend(instance, subschema)):
                evaluated.update(
                    _evaluated_property_keys(
                        validator,
                        instance,
                        subschema,
                    )
                )

    condition = schema.get("if")
    if condition is not None:
        if _is_valid(validator.descend(instance, condition)):
            evaluated.update(_evaluated_property_keys(validator, instance, condition))
            consequence = schema.get("then")
        else:
            consequence = schema.get("else")
        if consequence is not None:
            evaluated.update(_evaluated_property_keys(validator, instance, consequence))
    return evaluated


def unevaluated_properties(
    validator: Any,
    unevaluated: object,
    instance: object,
    schema: object,
) -> Generator[ValidationError]:
    if not validator.is_type(instance, "object"):
        return
    assert isinstance(instance, Mapping)
    typed_instance = cast("Mapping[str, object]", instance)
    evaluated = _evaluated_property_keys(validator, typed_instance, schema)
    invalid: list[str] = []
    for key, value in typed_instance.items():
        _checkpoint()
        if key not in evaluated and not _is_valid(
            validator.descend(
                value,
                unevaluated,
                path=key,
                schema_path=key,
            )
        ):
            invalid.append(key)
    if invalid:
        yield ValidationError("one or more unevaluated properties do not satisfy the schema")


def unique_items(
    validator: Any,
    unique: object,
    instance: object,
    _schema: object,
) -> Generator[ValidationError]:
    if not unique or not validator.is_type(instance, "array"):
        return
    assert isinstance(instance, list)
    seen: set[bytes] = set()
    for item in instance:
        _checkpoint()
        identity = canonical_json_bytes(item)
        if identity in seen:
            yield ValidationError("array elements are not unique")
            return
        seen.add(identity)


__all__ = [
    "EVALUATION_TIMEOUT_SECONDS",
    "MAX_EVALUATION_OPERATIONS",
    "REGEX_TIMEOUT_SECONDS",
    "SchemaEvaluationLimitExceeded",
    "additional_properties",
    "bounded_keyword",
    "evaluation_budget",
    "items",
    "pattern",
    "pattern_matches",
    "pattern_properties",
    "unevaluated_properties",
    "unique_items",
]
