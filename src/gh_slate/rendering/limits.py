from __future__ import annotations

from dataclasses import dataclass, fields

from gh_slate.codec.limits import DEFAULT_CODEC_LIMITS

MAX_TABLE_ROWS = 5_000
MAX_TABLE_COLUMNS = 256
MAX_LIST_DEPTH = 16
MAX_LIST_ITEMS = 10_000
MAX_OUTPUT_BYTES = DEFAULT_CODEC_LIMITS.max_visible_bytes


@dataclass(frozen=True, slots=True)
class RenderLimits:
    max_table_rows: int = 500
    max_table_columns: int = 64
    max_list_depth: int = 8
    max_list_items: int = 1_000
    max_output_bytes: int = DEFAULT_CODEC_LIMITS.max_visible_bytes

    def __post_init__(self) -> None:
        hard_limits = {
            "max_table_rows": MAX_TABLE_ROWS,
            "max_table_columns": MAX_TABLE_COLUMNS,
            "max_list_depth": MAX_LIST_DEPTH,
            "max_list_items": MAX_LIST_ITEMS,
            "max_output_bytes": MAX_OUTPUT_BYTES,
        }
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{item.name} must be a positive integer")
            maximum = hard_limits[item.name]
            if value > maximum:
                raise ValueError(f"{item.name} must be less than or equal to {maximum}")


DEFAULT_RENDER_LIMITS = RenderLimits()

__all__ = [
    "DEFAULT_RENDER_LIMITS",
    "MAX_LIST_DEPTH",
    "MAX_LIST_ITEMS",
    "MAX_OUTPUT_BYTES",
    "MAX_TABLE_COLUMNS",
    "MAX_TABLE_ROWS",
    "RenderLimits",
]
