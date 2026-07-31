from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from jsonschema import Draft202012Validator, validators

from gh_slate.schema.keywords import (
    additional_properties,
    bounded_keyword,
    items,
    pattern,
    pattern_properties,
    unevaluated_properties,
    unique_items,
)

_NONNEGATIVE_INTEGER_KEYWORDS = frozenset(
    {
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
    }
)
_SCHEMA_MAP_KEYWORDS = frozenset(
    {
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependencies",
        "patternProperties",
        "properties",
    }
)
_SCHEMA_ARRAY_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_SINGLE_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)


def to_validator_value(value: object) -> object:
    """Convert the immutable codec containers into jsonschema containers.

    The codec represents objects as ``MappingProxyType``, arrays as tuples, and
    all numbers as ``Decimal``. jsonschema expects concrete dictionaries/lists;
    every number remains a Decimal so conversion cannot materialize an integer
    with an attacker-controlled exponent.
    """

    if isinstance(value, Mapping):
        return {str(key): to_validator_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [to_validator_value(item) for item in value]
    return value


def to_metaschema_value(value: object) -> object:
    """Project only meta-schema integer keywords to bounded Python integers.

    jsonschema's bundled meta-schema validator does not classify Decimal as an
    integer. The draft's integer-valued size keywords only need their type and
    sign checked by the meta-schema, so an integral Decimal can safely become
    ``0`` or ``-1`` for that check. The actual runtime schema keeps the original
    Decimal. In particular, values in ``const``, ``enum``, and annotations are
    never collapsed and cannot gain false equality.
    """

    def project_schema(current: object) -> object:
        if isinstance(current, bool) or not isinstance(current, Mapping):
            return to_validator_value(current)

        result: dict[str, object] = {}
        for raw_key, item in current.items():
            key = str(raw_key)
            if (
                key in _NONNEGATIVE_INTEGER_KEYWORDS
                and isinstance(item, Decimal)
                and item.is_finite()
                and item == item.to_integral_value()
            ):
                result[key] = 0 if item >= 0 else -1
            elif key in _SCHEMA_SINGLE_KEYWORDS and isinstance(item, (bool, Mapping)):
                result[key] = project_schema(item)
            elif key in _SCHEMA_ARRAY_KEYWORDS and isinstance(item, tuple):
                result[key] = [
                    project_schema(child) if isinstance(child, (bool, Mapping)) else to_validator_value(child)
                    for child in item
                ]
            elif key in _SCHEMA_MAP_KEYWORDS and isinstance(item, Mapping):
                result[key] = {
                    str(name): (
                        project_schema(child) if isinstance(child, (bool, Mapping)) else to_validator_value(child)
                    )
                    for name, child in item.items()
                }
            else:
                result[key] = to_validator_value(item)
        return result

    return project_schema(value)


def _is_object(_checker: object, value: object) -> bool:
    return isinstance(value, Mapping)


def _is_array(_checker: object, value: object) -> bool:
    return isinstance(value, (list, tuple))


def _is_number(_checker: object, value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, Decimal) and value.is_finite()


def _is_integer(_checker: object, value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value()


_TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine_many(
    {
        "array": _is_array,
        "integer": _is_integer,
        "number": _is_number,
        "object": _is_object,
    }
)

_BOUNDED_VALIDATORS = {name: bounded_keyword(keyword) for name, keyword in Draft202012Validator.VALIDATORS.items()}
_BOUNDED_VALIDATORS.update(
    {
        "additionalProperties": additional_properties,
        "items": items,
        "pattern": pattern,
        "patternProperties": pattern_properties,
        "unevaluatedProperties": unevaluated_properties,
        "uniqueItems": unique_items,
    }
)

SlateDraft202012Validator = validators.extend(
    Draft202012Validator,
    validators=_BOUNDED_VALIDATORS,
    type_checker=_TYPE_CHECKER,
)
