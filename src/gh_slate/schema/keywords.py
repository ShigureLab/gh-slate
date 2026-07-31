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
_HEXADECIMAL_DIGITS = frozenset("0123456789abcdefABCDEF")
_ECMASCRIPT_IDENTIFIER_START = regex.compile(
    r"\A(?:[$_]|\p{ID_Start})\Z",
    flags=regex.VERSION0,
)
_ECMASCRIPT_IDENTIFIER_CONTINUE = regex.compile(
    r"\A(?:[$_\u200C\u200D]|\p{ID_Continue})\Z",
    flags=regex.VERSION0,
)


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


def _ecmascript_group_name(source: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(source):
        if source[index] != "\\":
            decoded.append(source[index])
            index += 1
            continue
        if index + 1 >= len(source) or source[index + 1] != "u":
            raise ValueError("invalid ECMA-262 group name escape")
        if index + 2 < len(source) and source[index + 2] == "{":
            closing_brace = source.find("}", index + 3)
            if closing_brace < 0:
                raise ValueError("invalid ECMA-262 group name escape")
            hexadecimal = source[index + 3 : closing_brace]
            next_index = closing_brace + 1
            if not 1 <= len(hexadecimal) <= 6:
                raise ValueError("invalid ECMA-262 group name escape")
        else:
            hexadecimal = source[index + 2 : index + 6]
            if len(hexadecimal) != 4:
                raise ValueError("invalid ECMA-262 group name escape")
            next_index = index + 6
        if any(character not in _HEXADECIMAL_DIGITS for character in hexadecimal):
            raise ValueError("invalid ECMA-262 group name escape")
        codepoint = int(hexadecimal, 16)
        if codepoint > 0x10FFFF:
            raise ValueError("invalid ECMA-262 group name escape")

        if 0xD800 <= codepoint <= 0xDBFF:
            if (
                next_index + 6 > len(source)
                or source[next_index : next_index + 2] != r"\u"
                or source[next_index + 2] == "{"
            ):
                raise ValueError("invalid ECMA-262 group name surrogate pair")
            low_hexadecimal = source[next_index + 2 : next_index + 6]
            if any(character not in _HEXADECIMAL_DIGITS for character in low_hexadecimal):
                raise ValueError("invalid ECMA-262 group name surrogate pair")
            low_surrogate = int(low_hexadecimal, 16)
            if not 0xDC00 <= low_surrogate <= 0xDFFF:
                raise ValueError("invalid ECMA-262 group name surrogate pair")
            codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low_surrogate - 0xDC00)
            next_index += 6
        elif 0xDC00 <= codepoint <= 0xDFFF:
            raise ValueError("invalid ECMA-262 group name surrogate pair")

        decoded.append(chr(codepoint))
        index = next_index

    name = "".join(decoded)
    if not name or _ECMASCRIPT_IDENTIFIER_START.fullmatch(name[0]) is None:
        raise ValueError("invalid ECMA-262 group name")
    if any(_ECMASCRIPT_IDENTIFIER_CONTINUE.fullmatch(character) is None for character in name[1:]):
        raise ValueError("invalid ECMA-262 group name")
    return name


def _is_ecmascript_modifier_group(source: str, index: int) -> bool:
    cursor = index + 2
    enabled_start = cursor
    while cursor < len(source) and source[cursor] in "ims":
        cursor += 1
    enabled = source[enabled_start:cursor]

    disabled = ""
    if cursor < len(source) and source[cursor] == "-":
        cursor += 1
        disabled_start = cursor
        while cursor < len(source) and source[cursor] in "ims":
            cursor += 1
        disabled = source[disabled_start:cursor]
        if not disabled:
            return False

    return (
        bool(enabled or disabled)
        and cursor < len(source)
        and source[cursor] == ":"
        and len(set(enabled)) == len(enabled)
        and len(set(disabled)) == len(disabled)
        and set(enabled).isdisjoint(disabled)
    )


def _is_group_quantified(source: str, closing_index: int) -> bool:
    quantifier_index = closing_index + 1
    if quantifier_index >= len(source):
        return False
    if source[quantifier_index] in "*+?":
        return True
    if source[quantifier_index] != "{":
        return False

    closing_brace = source.find("}", quantifier_index + 1)
    if closing_brace < 0:
        return False
    bounds = source[quantifier_index + 1 : closing_brace]
    minimum, separator, maximum = bounds.partition(",")
    digits = frozenset("0123456789")
    return (
        bool(minimum)
        and all(character in digits for character in minimum)
        and (not separator or not maximum or all(character in digits for character in maximum))
    )


def _is_braced_quantifier(source: str, closing_index: int) -> bool:
    opening_brace = source.rfind("{", 0, closing_index)
    if opening_brace < 0 or _is_escaped(source, opening_brace):
        return False
    bounds = source[opening_brace + 1 : closing_index]
    minimum, separator, maximum = bounds.partition(",")
    digits = frozenset("0123456789")
    return (
        bool(minimum)
        and all(character in digits for character in minimum)
        and (not separator or not maximum or all(character in digits for character in maximum))
    )


def _ecmascript_pattern(source: str) -> str:
    """Translate the supported ECMA-262 regex surface to ``regex`` syntax."""

    result: list[str] = []
    group_names: dict[str, str] = {}
    capture_contexts: dict[str, list[tuple[tuple[int, int], ...]]] = {}
    branch_stack: list[list[int]] = [[0, 0]]
    quantified_group_stack: list[tuple[int, set[str]]] = []
    next_branch_scope = 1

    def safe_group_name(name: str) -> str:
        if name not in group_names:
            group_names[name] = f"g{len(group_names)}"
        return group_names[name]

    def captures_are_disjoint(
        left: tuple[tuple[int, int], ...],
        right: tuple[tuple[int, int], ...],
    ) -> bool:
        left_alternatives = dict(left)
        return any(
            scope in left_alternatives and left_alternatives[scope] != alternative for scope, alternative in right
        )

    def push_group_scope(result_index: int) -> None:
        nonlocal next_branch_scope
        branch_stack.append([next_branch_scope, 0])
        quantified_group_stack.append((result_index, set()))
        next_branch_scope += 1

    in_class = False
    class_content_start = -1
    index = 0
    while index < len(source):
        character = source[index]
        if (
            not in_class
            and character == "+"
            and index > 0
            and (source[index - 1] in "*+?" or (source[index - 1] == "}" and _is_braced_quantifier(source, index - 1)))
            and not _is_escaped(source, index - 1)
        ):
            raise ValueError("unsupported possessive quantifier")
        if not in_class and character == "(":
            if source.startswith("(*", index):
                raise ValueError("unsupported regex group syntax")
            group_result_index = len(result)
            if source.startswith("(?", index):
                is_named_capture = (
                    source.startswith("(?<", index) and index + 3 < len(source) and source[index + 3] not in "=!"
                )
                if not is_named_capture and not (
                    source.startswith(("(?:", "(?=", "(?!", "(?<=", "(?<!"), index)
                    or _is_ecmascript_modifier_group(source, index)
                ):
                    raise ValueError("unsupported regex group syntax")
            else:
                is_named_capture = False

            if is_named_capture:
                closing_bracket = source.find(">", index + 3)
                if closing_bracket < 0:
                    raise ValueError("invalid ECMA-262 named capture")
                name = _ecmascript_group_name(source[index + 3 : closing_bracket])
                context = tuple((scope, alternative) for scope, alternative in branch_stack)
                if any(not captures_are_disjoint(previous, context) for previous in capture_contexts.get(name, [])):
                    raise ValueError("duplicate named captures must be in disjoint alternatives")
                capture_contexts.setdefault(name, []).append(context)
                safe_name = safe_group_name(name)
                result.append(f"(?P<{safe_name}>")
                push_group_scope(group_result_index)
                for _, captured_names in quantified_group_stack:
                    captured_names.add(safe_name)
                index = closing_bracket + 1
                continue
            result.append(character)
            push_group_scope(group_result_index)
            index += 1
            continue
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
                name = _ecmascript_group_name(source[index + 3 : closing_bracket])
                safe_name = safe_group_name(name)
                # In ECMA-262, a backreference to a capture that has not
                # participated (including a forward reference) matches the
                # empty string. Python's regex engine needs an explicit
                # conditional to preserve that behavior.
                result.append(f"(?({safe_name})\\g<{safe_name}>|)")
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
        elif not in_class and character == "|":
            branch_stack[-1][1] += 1
            result.append(character)
        elif not in_class and character == ")":
            if len(branch_stack) > 1 and quantified_group_stack:
                branch_stack.pop()
                group_result_index, captured_names = quantified_group_stack.pop()
                result.append(character)
                if captured_names and _is_group_quantified(source, index):
                    resets = "".join(f"(?P<{name}>)" for name in sorted(captured_names))
                    result.insert(group_result_index, f"(?:{resets}")
                    result.append(")")
            else:
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
