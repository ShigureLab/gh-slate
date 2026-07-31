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

EVALUATION_TIMEOUT_SECONDS = 1.0
REGEX_TIMEOUT_SECONDS = 0.05
MAX_EVALUATION_OPERATIONS = 100_000


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


def is_supported_regex(value: object) -> bool:
    """Return whether *value* is valid in the runtime regex dialect."""

    if not isinstance(value, str):
        return True
    try:
        regex.compile(value, flags=regex.VERSION0)
    except regex.error:
        return False
    return True


def _compile(pattern: str) -> Any:
    _checkpoint()
    try:
        return regex.compile(pattern, flags=regex.VERSION0)
    except regex.error as error:
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
    if validator.is_type(additional, "object"):
        for key in extras:
            yield from validator.descend(
                typed_instance[key],
                additional,
                path=key,
            )
    elif not additional and extras:
        yield ValidationError("one or more additional properties are not allowed")


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
    "pattern",
    "pattern_properties",
    "unevaluated_properties",
    "unique_items",
]
