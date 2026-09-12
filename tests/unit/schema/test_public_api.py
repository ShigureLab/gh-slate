from __future__ import annotations

from gh_slate import schema


def test_schema_package_exports_the_local_core_api() -> None:
    assert schema.DEFAULT_MAX_ERRORS == 32
    assert schema.MAX_DIAGNOSTIC_POINTER_BYTES > 0
    assert schema.MAX_ERRORS_LIMIT >= schema.DEFAULT_MAX_ERRORS
    assert schema.SchemaError
    assert schema.replace_schema
    assert schema.validate_data
    assert schema.validate_data_json
    assert schema.validate_schema
    assert schema.validate_schema_json
