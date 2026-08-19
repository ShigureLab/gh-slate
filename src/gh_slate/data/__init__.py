"""Pure local typed-data output used by read-only jq queries."""

from __future__ import annotations

from gh_slate.data.output import (
    JsonOutputMode,
    format_json_results,
    format_json_value,
    json_exit_status,
    pretty_json,
)

__all__ = [
    "JsonOutputMode",
    "format_json_results",
    "format_json_value",
    "json_exit_status",
    "pretty_json",
]
