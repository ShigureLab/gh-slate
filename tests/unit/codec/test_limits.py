from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.codec.limits import (
    DEFAULT_CODEC_LIMITS,
    MAX_CODEC_LIMIT,
    CodecLimits,
    SizeReport,
    enforce_size_limits,
)


def test_limits_and_reports_are_immutable() -> None:
    limits = CodecLimits()
    report = SizeReport(body_bytes=12)

    limit_field = "max_body_bytes"
    with pytest.raises(FrozenInstanceError):
        setattr(limits, limit_field, 1)
    report_field = "body_bytes"
    with pytest.raises(FrozenInstanceError):
        setattr(report, report_field, 1)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_limits_require_positive_integers(value: object) -> None:
    with pytest.raises(ValueError, match="max_body_bytes"):
        CodecLimits(max_body_bytes=cast("int", value))


def test_limits_reject_values_that_cannot_be_passed_safely_to_native_code() -> None:
    with pytest.raises(ValueError, match="less than or equal"):
        CodecLimits(max_state_bytes=MAX_CODEC_LIMIT + 1)


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_report_requires_non_negative_integers(value: object) -> None:
    with pytest.raises(ValueError, match="body_bytes"):
        SizeReport(body_bytes=cast("int", value))


def test_enforce_size_limits_accepts_exact_boundaries() -> None:
    report = SizeReport(
        body_bytes=DEFAULT_CODEC_LIMITS.max_body_bytes,
        visible_bytes=DEFAULT_CODEC_LIMITS.max_visible_bytes,
        state_bytes=DEFAULT_CODEC_LIMITS.max_state_bytes,
        compressed_bytes=DEFAULT_CODEC_LIMITS.max_compressed_bytes,
        encoded_bytes=DEFAULT_CODEC_LIMITS.max_encoded_bytes,
        data_bytes=DEFAULT_CODEC_LIMITS.max_data_bytes,
        schema_bytes=DEFAULT_CODEC_LIMITS.max_schema_bytes,
        renderer_bytes=DEFAULT_CODEC_LIMITS.max_renderer_bytes,
    )

    enforce_size_limits(report)


def test_enforce_size_limits_reports_every_component() -> None:
    limits = replace(
        DEFAULT_CODEC_LIMITS,
        max_body_bytes=10,
        max_state_bytes=20,
    )
    report = SizeReport(body_bytes=11, state_bytes=22, visible_bytes=7)

    with pytest.raises(CodecError) as caught:
        enforce_size_limits(report, limits)

    error = caught.value
    assert error.code == "codec_size_limit"
    assert error.details["exceeded"] == {
        "body_bytes": {"actual_bytes": 11, "max_bytes": 10},
        "state_bytes": {"actual_bytes": 22, "max_bytes": 20},
    }
    assert error.details["sizes"] == report.as_dict()
    assert error.details["limits"] == limits.as_dict()
