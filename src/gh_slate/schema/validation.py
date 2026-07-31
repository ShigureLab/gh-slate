from __future__ import annotations

import hashlib
from bisect import insort
from collections.abc import Iterable, Mapping
from decimal import DecimalException
from typing import NoReturn, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError as JsonSchemaSchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import (
    JsonValue,
    canonical_json_bytes,
    freeze_json,
    strict_loads,
    strict_loads_object,
)
from gh_slate.codec.limits import (
    DEFAULT_CODEC_LIMITS,
    SizeReport,
    enforce_size_limits,
)
from gh_slate.codec.model import JSON_SCHEMA_DIALECT_2020_12, SchemaSnapshotV1
from gh_slate.schema._interop import (
    SlateDraft202012Validator,
    to_metaschema_value,
    to_validator_value,
)
from gh_slate.schema.errors import SchemaDiagnostic, SchemaError
from gh_slate.schema.keywords import (
    SchemaEvaluationLimitExceeded,
    evaluation_budget,
    is_supported_regex,
)

DEFAULT_MAX_ERRORS = 32
MAX_ERRORS_LIMIT = 256
MAX_DIAGNOSTIC_POINTER_BYTES = 1024

# These are the draft 2020-12 locations whose values are schemas. Legacy
# ``definitions`` and ``dependencies`` are included defensively: validators may
# accept extension keywords, and an unreachable external reference must still
# never become a future retrieval surface. Instance-valued annotations such as
# const, enum, examples, and default are deliberately absent.
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
_REFERENCE_KEYWORDS = ("$dynamicRef", "$ref")

_SCHEMA_FORMAT_CHECKER = FormatChecker()
_SCHEMA_FORMAT_CHECKER.checks("regex")(is_supported_regex)


class _FalseSchema(dict[str, object]):
    """A unique runtime equivalent of one source ``false`` schema."""


def _pointer(parts: Iterable[object]) -> tuple[str, bool]:
    encoded: list[str] = []
    encoded_bytes = 0
    truncated = False
    digest = hashlib.sha256()
    for part in parts:
        raw = str(part)
        raw_bytes = raw.encode("utf-8")
        digest.update(len(raw_bytes).to_bytes(8, "big"))
        digest.update(raw_bytes)
        token = str(part).replace("~", "~0").replace("/", "~1")
        token_bytes = len(token.encode("utf-8")) + 1
        if not truncated and encoded_bytes + token_bytes <= MAX_DIAGNOSTIC_POINTER_BYTES:
            encoded.append(token)
            encoded_bytes += token_bytes
        else:
            truncated = True

    if not truncated:
        return ("" if not encoded else "/" + "/".join(encoded), False)

    suffix = f"__gh_slate_truncated_{digest.hexdigest()[:16]}"
    suffix_bytes = len(suffix) + 1
    while encoded and encoded_bytes + suffix_bytes > MAX_DIAGNOSTIC_POINTER_BYTES:
        removed = encoded.pop()
        encoded_bytes -= len(removed.encode("utf-8")) + 1
    encoded.append(suffix)
    return "/" + "/".join(encoded), True


def _diagnostic(
    *,
    code: str,
    message: str,
    data_path: Iterable[object] = (),
    schema_path: Iterable[object] = (),
    keyword: str | None = None,
) -> SchemaDiagnostic:
    data_pointer, data_pointer_truncated = _pointer(data_path)
    schema_pointer, schema_pointer_truncated = _pointer(schema_path)
    return SchemaDiagnostic(
        code=code,
        message=message,
        data_pointer=data_pointer,
        schema_pointer=schema_pointer,
        keyword=keyword,
        data_pointer_truncated=data_pointer_truncated,
        schema_pointer_truncated=schema_pointer_truncated,
    )


def _raise_single(
    message: str,
    *,
    code: str,
    diagnostic: SchemaDiagnostic,
    details: Mapping[str, object] | None = None,
) -> NoReturn:
    raise SchemaError(
        message,
        code=code,
        diagnostics=(diagnostic,),
        details=details,
    )


def _validate_max_errors(max_errors: int) -> None:
    if isinstance(max_errors, bool) or not isinstance(max_errors, int) or not 1 <= max_errors <= MAX_ERRORS_LIMIT:
        raise SchemaError(
            f"max_errors must be an integer between 1 and {MAX_ERRORS_LIMIT}",
            code="schema_options_invalid",
            details={"option": "max_errors", "maximum": MAX_ERRORS_LIMIT},
        )


def _snapshot(document: bool | Mapping[str, object], *, dialect: str) -> SchemaSnapshotV1:
    try:
        return SchemaSnapshotV1(dialect=dialect, document=document)
    except CodecError as error:
        raise SchemaError(
            "schema document is not valid JSON",
            code="schema_invalid",
            details={"cause_code": error.code},
        ) from None


def _schema_size_error(error: CodecError, *, schema_bytes: int | None = None) -> SchemaError:
    details: dict[str, object] = {
        "cause_code": error.code,
        "max_schema_bytes": DEFAULT_CODEC_LIMITS.max_schema_bytes,
    }
    if schema_bytes is not None:
        details["schema_bytes"] = schema_bytes
    return SchemaError(
        "schema exceeds the local snapshot size limit",
        code="schema_size_limit",
        details=details,
    )


def _enforce_schema_size(snapshot: SchemaSnapshotV1) -> None:
    schema_bytes: int | None = None
    try:
        schema_bytes = len(canonical_json_bytes(snapshot.to_json()))
        enforce_size_limits(SizeReport(schema_bytes=schema_bytes))
    except CodecError as error:
        raise _schema_size_error(error, schema_bytes=schema_bytes) from None


def _check_dialect(snapshot: SchemaSnapshotV1) -> None:
    if snapshot.dialect != JSON_SCHEMA_DIALECT_2020_12:
        _raise_single(
            "only JSON Schema draft 2020-12 is supported",
            code="schema_dialect_unsupported",
            diagnostic=_diagnostic(
                code="unsupported_dialect",
                message="schema dialect must be draft 2020-12",
                schema_path=("dialect",),
                keyword="$schema",
            ),
            details={"supported_dialect": JSON_SCHEMA_DIALECT_2020_12},
        )

    if isinstance(snapshot.document, Mapping) and "$schema" in snapshot.document:
        declared = snapshot.document["$schema"]
        if declared != JSON_SCHEMA_DIALECT_2020_12:
            _raise_single(
                "schema $schema must match its draft 2020-12 snapshot dialect",
                code="schema_dialect_mismatch",
                diagnostic=_diagnostic(
                    code="unsupported_dialect",
                    message="schema $schema must be draft 2020-12",
                    schema_path=("$schema",),
                    keyword="$schema",
                ),
                details={"supported_dialect": JSON_SCHEMA_DIALECT_2020_12},
            )


def _scan_local_references(schema: bool | Mapping[str, object]) -> None:
    def visit(current: object, path: tuple[object, ...]) -> None:
        if isinstance(current, bool) or not isinstance(current, Mapping):
            return
        current_schema = cast("Mapping[str, object]", current)

        if "$schema" in current_schema and current_schema["$schema"] != JSON_SCHEMA_DIALECT_2020_12:
            _raise_single(
                "nested schema $schema must remain draft 2020-12",
                code="schema_dialect_mismatch",
                diagnostic=_diagnostic(
                    code="unsupported_dialect",
                    message="schema $schema must be draft 2020-12",
                    schema_path=(*path, "$schema"),
                    keyword="$schema",
                ),
                details={"supported_dialect": JSON_SCHEMA_DIALECT_2020_12},
            )

        for keyword in _REFERENCE_KEYWORDS:
            reference = current_schema.get(keyword)
            if isinstance(reference, str) and reference != "" and not reference.startswith("#"):
                _raise_single(
                    "schema references must be local fragments",
                    code="schema_reference_forbidden",
                    diagnostic=_diagnostic(
                        code="nonlocal_reference",
                        message="only local fragment references are allowed",
                        schema_path=(*path, keyword),
                        keyword=keyword,
                    ),
                )

        for keyword in _SCHEMA_SINGLE_KEYWORDS:
            child = current_schema.get(keyword)
            if isinstance(child, (bool, Mapping)):
                visit(child, (*path, keyword))

        for keyword in _SCHEMA_ARRAY_KEYWORDS:
            children = current_schema.get(keyword)
            if isinstance(children, tuple):
                for index, child in enumerate(children):
                    if isinstance(child, (bool, Mapping)):
                        visit(child, (*path, keyword, index))

        for keyword in _SCHEMA_MAP_KEYWORDS:
            children = current_schema.get(keyword)
            if not isinstance(children, Mapping):
                continue
            for name, child in children.items():
                # Draft 2020-12 dependencies can still contain property-name
                # arrays. They are instance data, not subschemas.
                if isinstance(child, (bool, Mapping)):
                    visit(child, (*path, keyword, name))

    visit(schema, ())


def _schema_meta_diagnostic(error: JsonSchemaSchemaError) -> SchemaDiagnostic:
    keyword = error.validator if isinstance(error.validator, str) else None
    schema_path = tuple(error.absolute_path)
    return _diagnostic(
        code="invalid_schema",
        message=(
            "schema does not satisfy the draft 2020-12 meta-schema"
            if keyword is None
            else f"schema does not satisfy meta-schema keyword '{keyword}'"
        ),
        schema_path=schema_path,
        keyword=keyword,
    )


def validate_schema(
    document: bool | Mapping[str, object],
    *,
    dialect: str = JSON_SCHEMA_DIALECT_2020_12,
) -> SchemaSnapshotV1:
    """Validate and freeze a local-only draft 2020-12 schema snapshot."""

    snapshot = _snapshot(document, dialect=dialect)
    _enforce_schema_size(snapshot)
    _check_dialect(snapshot)
    _scan_local_references(snapshot.document)
    validator_schema = cast(
        "bool | Mapping[str, object]",
        to_metaschema_value(snapshot.document),
    )
    try:
        Draft202012Validator.check_schema(
            validator_schema,
            format_checker=_SCHEMA_FORMAT_CHECKER,
        )
    except JsonSchemaSchemaError as error:
        _raise_single(
            "schema is not valid draft 2020-12",
            code="schema_invalid",
            diagnostic=_schema_meta_diagnostic(error),
        )
    except Exception:
        raise SchemaError(
            "schema could not be checked safely",
            code="schema_invalid",
        ) from None
    return snapshot


def validate_schema_json(
    source: str | bytes,
    *,
    dialect: str = JSON_SCHEMA_DIALECT_2020_12,
) -> SchemaSnapshotV1:
    """Strictly ingest JSON text and validate it as a schema snapshot."""

    try:
        document = strict_loads(source)
    except CodecError as error:
        raise SchemaError(
            "schema JSON could not be parsed",
            code="schema_invalid",
            details={"cause_code": error.code},
        ) from None
    if not isinstance(document, (bool, Mapping)):
        raise SchemaError(
            "schema document root must be an object or boolean",
            code="schema_invalid",
            details={"cause_code": "json_root_not_schema"},
        )
    return validate_schema(
        cast("bool | Mapping[str, object]", document),
        dialect=dialect,
    )


def _deny_retrieve(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)


def _validation_message(keyword: str | None) -> str:
    messages = {
        "additionalProperties": "an additional property is not allowed",
        "const": "value does not equal the required constant",
        "enum": "value is not one of the allowed values",
        "required": "a required property is missing",
        "type": "value has the wrong JSON type",
    }
    if keyword is None:
        return "value does not satisfy the schema"
    return messages.get(keyword, f"value does not satisfy schema keyword '{keyword}'")


def _schema_locations(schema: object) -> dict[int, tuple[object, ...]]:
    locations: dict[int, tuple[object, ...]] = {}

    def visit(current: object, path: tuple[object, ...]) -> None:
        if isinstance(current, bool) or not isinstance(current, Mapping):
            return
        typed = cast("Mapping[str, object]", current)
        locations[id(typed)] = path

        for keyword in _SCHEMA_SINGLE_KEYWORDS:
            child = typed.get(keyword)
            if isinstance(child, (bool, Mapping)):
                visit(child, (*path, keyword))

        for keyword in _SCHEMA_ARRAY_KEYWORDS:
            children = typed.get(keyword)
            if not isinstance(children, (list, tuple)):
                continue
            for index, child in enumerate(children):
                if isinstance(child, (bool, Mapping)):
                    visit(child, (*path, keyword, index))

        for keyword in _SCHEMA_MAP_KEYWORDS:
            children = typed.get(keyword)
            if not isinstance(children, Mapping):
                continue
            for name, child in children.items():
                if isinstance(child, (bool, Mapping)):
                    visit(child, (*path, keyword, name))

    visit(schema, ())
    return locations


def _project_false_schemas(schema: object) -> object:
    """Give each boolean-false schema a unique identity for diagnostics."""

    if schema is False:
        return _FalseSchema({"not": {}})
    if schema is True or not isinstance(schema, Mapping):
        return schema

    typed = cast("Mapping[str, object]", schema)
    result = dict(typed)
    for keyword in _SCHEMA_SINGLE_KEYWORDS:
        child = typed.get(keyword)
        if isinstance(child, (bool, Mapping)):
            result[keyword] = _project_false_schemas(child)

    for keyword in _SCHEMA_ARRAY_KEYWORDS:
        children = typed.get(keyword)
        if not isinstance(children, (list, tuple)):
            continue
        result[keyword] = [
            (_project_false_schemas(child) if isinstance(child, (bool, Mapping)) else child) for child in children
        ]

    for keyword in _SCHEMA_MAP_KEYWORDS:
        children = typed.get(keyword)
        if not isinstance(children, Mapping):
            continue
        result[keyword] = {
            name: (_project_false_schemas(child) if isinstance(child, (bool, Mapping)) else child)
            for name, child in children.items()
        }
    return result


def _validation_diagnostic(
    error: ValidationError,
    *,
    schema_locations: Mapping[int, tuple[object, ...]],
) -> SchemaDiagnostic:
    false_schema = isinstance(error.schema, _FalseSchema)
    keyword = None if false_schema else (error.validator if isinstance(error.validator, str) else None)
    schema_path = tuple(error.absolute_schema_path)
    if isinstance(error.schema, Mapping):
        source_path = schema_locations.get(id(error.schema))
        if source_path is not None:
            if false_schema:
                schema_path = source_path
            elif keyword is None or keyword in error.schema:
                schema_path = source_path if keyword is None else (*source_path, keyword)
    return _diagnostic(
        code="validation_failed" if keyword is None else keyword,
        message=_validation_message(keyword),
        data_path=tuple(error.absolute_path),
        schema_path=schema_path,
        keyword=keyword,
    )


def _diagnostic_sort_key(diagnostic: SchemaDiagnostic) -> tuple[str, str, str, str]:
    return (
        diagnostic.data_pointer,
        diagnostic.schema_pointer,
        "" if diagnostic.keyword is None else diagnostic.keyword,
        diagnostic.message,
    )


def _collect_diagnostics(
    errors: Iterable[ValidationError],
    *,
    maximum: int,
    schema_locations: Mapping[int, tuple[object, ...]],
) -> tuple[list[SchemaDiagnostic], int]:
    selected: list[
        tuple[
            tuple[str, str, str, str],
            int,
            SchemaDiagnostic,
        ]
    ] = []
    count = 0
    for error in errors:
        diagnostic = _validation_diagnostic(
            error,
            schema_locations=schema_locations,
        )
        # The monotonically increasing index means tuple comparison never
        # reaches the non-orderable dataclass when two sort keys are equal.
        insort(selected, (_diagnostic_sort_key(diagnostic), count, diagnostic))
        count += 1
        if len(selected) > maximum:
            selected.pop()
    return [item[2] for item in selected], count


def _freeze_data(data: object) -> Mapping[str, JsonValue]:
    try:
        frozen = freeze_json(data)
    except CodecError as error:
        raise SchemaError(
            "data is not valid JSON",
            code="data_invalid",
            details={"cause_code": error.code},
        ) from None
    if not isinstance(frozen, Mapping):
        _raise_single(
            "data root must be a JSON object",
            code="schema_data_root_not_object",
            diagnostic=_diagnostic(
                code="root_not_object",
                message="data root must be an object",
                keyword="type",
            ),
        )
    typed = cast("Mapping[str, JsonValue]", frozen)
    data_bytes = len(canonical_json_bytes(typed))
    try:
        enforce_size_limits(SizeReport(data_bytes=data_bytes))
    except CodecError as error:
        raise SchemaError(
            "data exceeds the local snapshot size limit",
            code="schema_data_size_limit",
            details={
                "cause_code": error.code,
                "data_bytes": data_bytes,
                "max_data_bytes": DEFAULT_CODEC_LIMITS.max_data_bytes,
            },
        ) from None
    return typed


def validate_data(
    data: object,
    schema: SchemaSnapshotV1 | bool | Mapping[str, object] | None = None,
    *,
    dialect: str = JSON_SCHEMA_DIALECT_2020_12,
    max_errors: int = DEFAULT_MAX_ERRORS,
) -> Mapping[str, JsonValue]:
    """Freeze object-rooted data and validate it against an optional schema."""

    _validate_max_errors(max_errors)
    frozen = _freeze_data(data)
    if schema is None:
        return frozen

    snapshot = (
        validate_schema(schema.document, dialect=schema.dialect)
        if isinstance(schema, SchemaSnapshotV1)
        else validate_schema(schema, dialect=dialect)
    )
    validator_schema = cast(
        "bool | Mapping[str, object]",
        _project_false_schemas(to_validator_value(snapshot.document)),
    )
    validator_data = to_validator_value(frozen)
    registry: Registry[object] = Registry(retrieve=_deny_retrieve)
    schema_locations = _schema_locations(validator_schema)
    try:
        with evaluation_budget():
            diagnostics, error_count = _collect_diagnostics(
                SlateDraft202012Validator(
                    validator_schema,
                    registry=registry,
                ).iter_errors(validator_data),
                maximum=max_errors,
                schema_locations=schema_locations,
            )
    except Unresolvable:
        raise SchemaError(
            "schema reference could not be resolved locally",
            code="schema_resolution_failed",
        ) from None
    except (SchemaEvaluationLimitExceeded, RecursionError, TimeoutError):
        raise SchemaError(
            "schema evaluation exceeded its local execution budget",
            code="schema_evaluation_limit",
        ) from None
    except DecimalException:
        raise SchemaError(
            "schema numeric constraint could not be evaluated safely",
            code="schema_evaluation_failed",
        ) from None
    except Exception:
        raise SchemaError(
            "schema could not be evaluated safely",
            code="schema_evaluation_failed",
        ) from None

    if error_count:
        raise SchemaError(
            "data does not satisfy its schema",
            code="schema_validation_failed",
            diagnostics=diagnostics,
            truncated=error_count > len(diagnostics),
            details={
                "error_count": error_count,
                "max_errors": max_errors,
            },
        )
    return frozen


def validate_data_json(
    source: str | bytes,
    schema: SchemaSnapshotV1 | bool | Mapping[str, object] | None = None,
    *,
    dialect: str = JSON_SCHEMA_DIALECT_2020_12,
    max_errors: int = DEFAULT_MAX_ERRORS,
) -> Mapping[str, JsonValue]:
    """Strictly ingest object-rooted JSON text and validate its data."""

    try:
        data = strict_loads_object(source)
    except CodecError as error:
        if error.code == "json_root_not_object":
            _raise_single(
                "data root must be a JSON object",
                code="schema_data_root_not_object",
                diagnostic=_diagnostic(
                    code="root_not_object",
                    message="data root must be an object",
                    keyword="type",
                ),
            )
        raise SchemaError(
            "data JSON could not be parsed",
            code="data_invalid",
            details={"cause_code": error.code},
        ) from None
    return validate_data(
        data,
        schema,
        dialect=dialect,
        max_errors=max_errors,
    )


def replace_schema(
    data: object,
    document: bool | Mapping[str, object],
    *,
    dialect: str = JSON_SCHEMA_DIALECT_2020_12,
) -> SchemaSnapshotV1:
    """Validate a replacement schema and current data without side effects."""

    snapshot = validate_schema(document, dialect=dialect)
    validate_data(data, snapshot)
    return snapshot
