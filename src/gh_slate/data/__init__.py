"""Pure local typed-data operations used by the jq-style CLI."""

from __future__ import annotations

from gh_slate.data.editor import (
    EDITOR_ENVIRONMENT_ORDER,
    EditorRunner,
    RetryCallback,
    ValidationCallback,
    edit_json,
    select_editor,
)
from gh_slate.data.errors import DataError
from gh_slate.data.input import (
    DEFAULT_DATA_INPUT_BYTES,
    InputStream,
    load_argjson,
    load_value,
    merge_jq_arguments,
)
from gh_slate.data.output import (
    JsonOutputMode,
    format_json_results,
    format_json_value,
    json_exit_status,
    pretty_json,
)
from gh_slate.data.path import (
    MAX_ARRAY_INDEX,
    MAX_PATH_SEGMENTS,
    JqPathEvaluator,
    JsonPath,
    PathSegment,
    delete_path,
    delete_paths,
    get_path,
    resolve_exact_path,
    set_path,
)

__all__ = [
    "DEFAULT_DATA_INPUT_BYTES",
    "EDITOR_ENVIRONMENT_ORDER",
    "DataError",
    "EditorRunner",
    "InputStream",
    "JqPathEvaluator",
    "JsonOutputMode",
    "JsonPath",
    "MAX_ARRAY_INDEX",
    "MAX_PATH_SEGMENTS",
    "PathSegment",
    "RetryCallback",
    "ValidationCallback",
    "delete_path",
    "delete_paths",
    "edit_json",
    "format_json_results",
    "format_json_value",
    "get_path",
    "json_exit_status",
    "load_argjson",
    "load_value",
    "merge_jq_arguments",
    "pretty_json",
    "resolve_exact_path",
    "select_editor",
    "set_path",
]
