"""Offline JSON Schema validation for slate data."""

from __future__ import annotations

from gh_slate.schema.errors import SchemaDiagnostic, SchemaError
from gh_slate.schema.validation import (
    DEFAULT_MAX_ERRORS,
    MAX_DIAGNOSTIC_POINTER_BYTES,
    MAX_ERRORS_LIMIT,
    replace_schema,
    validate_data,
    validate_data_json,
    validate_schema,
    validate_schema_json,
)

__all__ = [
    "DEFAULT_MAX_ERRORS",
    "MAX_ERRORS_LIMIT",
    "MAX_DIAGNOSTIC_POINTER_BYTES",
    "SchemaDiagnostic",
    "SchemaError",
    "replace_schema",
    "validate_data",
    "validate_data_json",
    "validate_schema",
    "validate_schema_json",
]
