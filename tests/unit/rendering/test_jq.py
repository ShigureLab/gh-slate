from __future__ import annotations

import subprocess
from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType
from typing import cast

import pytest

from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.jq import DEFAULT_JQ_LIMITS, JqLimits, select_one


def test_select_one_returns_one_immutable_json_projection() -> None:
    source = {"jobs": [{"name": "linux"}, {"name": "macos"}]}

    result = select_one(source, ".jobs")

    assert result == (
        MappingProxyType({"name": "linux"}),
        MappingProxyType({"name": "macos"}),
    )
    assert source == {"jobs": [{"name": "linux"}, {"name": "macos"}]}


@pytest.mark.parametrize(
    ("filter_text", "code"),
    [
        (".missing | empty", "jq_no_result"),
        (".[]", "jq_multiple_results"),
        (".foo[", "jq_compile_error"),
        ('error("nope")', "jq_runtime_error"),
    ],
)
def test_select_one_has_stable_cardinality_and_jq_errors(filter_text: str, code: str) -> None:
    with pytest.raises(RenderingError) as caught:
        select_one({"a": 1, "b": 2}, filter_text)

    assert caught.value.code == code


@pytest.mark.parametrize(
    "filter_text",
    [
        "env",
        "$ENV.HOME",
        'import "secrets" as secrets; .',
        'include "secrets"; .',
        'module {"name": "secrets"}; .',
        '"\\(env.HOME)"',
        '"\\((1 # keep scanning\n), env.HOME)"',
    ],
)
def test_selector_rejects_host_observation_facilities(filter_text: str) -> None:
    with pytest.raises(RenderingError) as caught:
        select_one({"env": "data"}, filter_text)

    assert caught.value.code == "jq_filter_forbidden"


def test_selector_scan_is_token_aware_for_data_strings_fields_and_comments() -> None:
    source = {"env": "data"}

    assert select_one(source, ".env # import module include $ENV") == "data"
    assert select_one(source, '"env import include module $ENV"') == "env import include module $ENV"


def test_selector_limit_is_measured_in_utf8_bytes() -> None:
    limits = replace(DEFAULT_JQ_LIMITS, max_selector_bytes=3)

    with pytest.raises(RenderingError) as caught:
        select_one(None, '"é"', limits=limits)

    assert caught.value.code == "jq_filter_limit"
    assert caught.value.details == {"actual_bytes": 4, "max_bytes": 3}


def test_source_and_output_limits_are_enforced() -> None:
    with pytest.raises(RenderingError) as source_error:
        select_one("abcdef", ".", limits=replace(DEFAULT_JQ_LIMITS, max_source_bytes=4))
    assert source_error.value.code == "jq_source_limit"

    with pytest.raises(RenderingError) as output_error:
        select_one(None, '"abcdef"', limits=replace(DEFAULT_JQ_LIMITS, max_output_bytes=4))
    assert output_error.value.code == "jq_output_limit"


def test_invalid_source_is_wrapped_at_the_rendering_boundary() -> None:
    with pytest.raises(RenderingError) as caught:
        select_one({"not_json": 1.5}, ".")

    assert caught.value.code == "jq_source_invalid"
    assert caught.value.details["cause"] == "json_type_unsupported"


def test_timeout_is_reported_stably(monkeypatch: pytest.MonkeyPatch) -> None:
    def time_out(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="worker", timeout=0.01)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(RenderingError) as caught:
        select_one(None, ".", limits=replace(DEFAULT_JQ_LIMITS, timeout_seconds=0.01))

    assert caught.value.code == "jq_timeout"


def test_worker_uses_isolated_process_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def complete(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, b'{"ok":true,"results":[null]}', b"")

    monkeypatch.setattr(subprocess, "run", complete)

    assert select_one(None, ".") is None
    assert observed["env"] == {}
    assert observed["check"] is False
    assert observed["input"] == b"null"
    assert observed["cwd"] != "."
    assert cast_list(observed["command"])[1] == "-I"


def cast_list(value: object) -> list[str]:
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return cast("list[str]", value)


@pytest.mark.parametrize(
    "response",
    [
        b"not json",
        b'{"ok":true,"results":NaN}',
        b'{"ok":true,"results":[1,2,3]}',
        b'{"ok":false,"kind":"made-up"}',
        b'{"ok":true,"results":[null],"extra":1}',
    ],
)
def test_worker_protocol_is_strict(monkeypatch: pytest.MonkeyPatch, response: bytes) -> None:
    def complete(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, response, b"")

    monkeypatch.setattr(subprocess, "run", complete)

    with pytest.raises(RenderingError) as caught:
        select_one(None, ".")

    assert caught.value.code == "jq_worker_protocol"


def test_libjq_large_integer_rounding_is_projection_only_and_strings_stay_exact() -> None:
    exact_integer = Decimal(9007199254740993)
    source = {"number": exact_integer, "string": "9007199254740993"}

    assert select_one(source, ".number") == Decimal(9007199254740992)
    assert select_one(source, ".string") == "9007199254740993"
    assert source["number"] == exact_integer


@pytest.mark.parametrize(
    "field",
    [
        "max_selector_bytes",
        "max_source_bytes",
        "max_output_bytes",
        "timeout_seconds",
        "max_memory_bytes",
        "max_cpu_seconds",
    ],
)
def test_jq_limits_require_positive_values(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        JqLimits(**{field: 0})
