from __future__ import annotations

import sys
from dataclasses import dataclass, fields

from gh_slate.codec.errors import CodecError

MAX_CODEC_LIMIT = sys.maxsize - 1


@dataclass(frozen=True, slots=True)
class CodecLimits:
    """Conservative byte limits for a self-contained slate comment."""

    max_body_bytes: int = 64 * 1024
    max_visible_bytes: int = 48 * 1024
    max_state_bytes: int = 256 * 1024
    max_compressed_bytes: int = 32 * 1024
    max_encoded_bytes: int = 44 * 1024
    max_data_bytes: int = 192 * 1024
    max_schema_bytes: int = 64 * 1024
    max_renderer_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")
            if value > MAX_CODEC_LIMIT:
                raise ValueError(f"{field.name} must be less than or equal to {MAX_CODEC_LIMIT}")

    def as_dict(self) -> dict[str, int]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class SizeReport:
    """Actual byte sizes for every independently bounded codec component."""

    body_bytes: int = 0
    visible_bytes: int = 0
    state_bytes: int = 0
    compressed_bytes: int = 0
    encoded_bytes: int = 0
    data_bytes: int = 0
    schema_bytes: int = 0
    renderer_bytes: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field.name} must be a non-negative integer")

    def as_dict(self) -> dict[str, int]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


DEFAULT_CODEC_LIMITS = CodecLimits()

_REPORT_TO_LIMIT = (
    ("body_bytes", "max_body_bytes"),
    ("visible_bytes", "max_visible_bytes"),
    ("state_bytes", "max_state_bytes"),
    ("compressed_bytes", "max_compressed_bytes"),
    ("encoded_bytes", "max_encoded_bytes"),
    ("data_bytes", "max_data_bytes"),
    ("schema_bytes", "max_schema_bytes"),
    ("renderer_bytes", "max_renderer_bytes"),
)


def enforce_size_limits(
    report: SizeReport,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> None:
    """Fail with a complete size breakdown if any component is oversized."""

    exceeded: dict[str, dict[str, int]] = {}
    for report_name, limit_name in _REPORT_TO_LIMIT:
        actual = getattr(report, report_name)
        maximum = getattr(limits, limit_name)
        if actual > maximum:
            exceeded[report_name] = {
                "actual_bytes": actual,
                "max_bytes": maximum,
            }

    if not exceeded:
        return

    components = ", ".join(
        f"{name}={values['actual_bytes']} > {values['max_bytes']}" for name, values in exceeded.items()
    )
    raise CodecError(
        f"codec size limit exceeded: {components}",
        code="codec_size_limit",
        details={
            "sizes": report.as_dict(),
            "limits": limits.as_dict(),
            "exceeded": exceeded,
        },
    )
