from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest

from gh_slate.codec.json import DEFAULT_JSON_LIMITS
from gh_slate.rendering import _jq_worker as worker_module, jq as jq_module
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.jq import (
    DEFAULT_JQ_LIMITS,
    MAX_RESULTS,
    JqLimits,
    evaluate,
    select_one,
)


def test_evaluate_returns_zero_one_or_many_immutable_json_values() -> None:
    source = {"jobs": [{"name": "linux"}, {"name": "macos"}]}

    assert evaluate(source, ".missing | empty") == ()
    assert evaluate(source, ".jobs") == (
        (
            MappingProxyType({"name": "linux"}),
            MappingProxyType({"name": "macos"}),
        ),
    )
    assert evaluate(source, ".jobs[].name") == ("linux", "macos")
    assert source == {"jobs": [{"name": "linux"}, {"name": "macos"}]}


def test_evaluate_allows_one_time_nondeterministic_data_updates() -> None:
    result = evaluate(None, "now", max_results=1)

    assert len(result) == 1
    assert isinstance(result[0], Decimal)


def test_evaluate_binds_string_and_typed_json_arguments_from_stdin() -> None:
    result = evaluate(
        {"source": "data"},
        '{"source": .source, "string": $string, "typed": $typed}',
        args={
            "string": "42",
            "typed": MappingProxyType({"number": Decimal(42), "ready": True}),
        },
        max_results=1,
    )

    assert result == (
        MappingProxyType(
            {
                "source": "data",
                "string": "42",
                "typed": MappingProxyType({"number": Decimal(42), "ready": True}),
            }
        ),
    )
    assert select_one(None, "$value", args={"value": "bound"}) == "bound"


def test_evaluate_detects_result_overflow_with_one_sentinel_value() -> None:
    with pytest.raises(RenderingError) as caught:
        evaluate((1, 2, 3), ".[]", max_results=2)

    assert caught.value.code == "jq_result_limit"
    assert caught.value.details == {"count_at_least": 3, "max_results": 2}


def test_result_limit_cannot_be_escaped_by_closing_a_source_wrapper() -> None:
    with pytest.raises(RenderingError) as overflow:
        evaluate(None, "range(0; 3)", max_results=1)
    assert overflow.value.code == "jq_result_limit"

    with pytest.raises(RenderingError) as invalid:
        evaluate(
            None,
            ".))] | [(range(0;3",
            max_results=1,
        )
    assert invalid.value.code == "jq_compile_error"


@pytest.mark.parametrize("max_results", [True, 0, -1, MAX_RESULTS + 1])
def test_evaluate_rejects_invalid_result_limits(max_results: object) -> None:
    with pytest.raises(RenderingError) as caught:
        evaluate(None, ".", max_results=cast("int", max_results))

    assert caught.value.code == "jq_max_results_invalid"


@pytest.mark.parametrize("name", ["$value", "two-parts", "9lives", "é"])
def test_evaluate_rejects_unsafe_argument_names(name: str) -> None:
    with pytest.raises(RenderingError) as caught:
        evaluate(None, ".", args={name: "value"})

    assert caught.value.code == "jq_args_invalid"


def test_evaluate_rejects_non_json_arguments_and_bounds_the_full_stdin_request() -> None:
    with pytest.raises(RenderingError) as invalid:
        evaluate(None, ".", args={"value": 1.5})  # ty: ignore[invalid-argument-type]
    assert invalid.value.code == "jq_args_invalid"

    with pytest.raises(RenderingError) as too_large:
        evaluate(
            None,
            ".",
            args={"value": "x" * 32},
            limits=replace(DEFAULT_JQ_LIMITS, max_source_bytes=32),
        )
    assert too_large.value.code == "jq_source_limit"


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
        "$JQ_BUILD_CONFIGURATION",
        "builtins",
        "get_jq_origin",
        "get_prog_origin",
        "get_search_list",
        "have_decnum",
        "have_literal_numbers",
        '"helpers" | modulemeta',
        "fromdate",
        "fromdateiso8601",
        "now",
        "localtime",
        "strflocaltime",
        "todate",
        "todateiso8601",
        "input",
        "inputs",
        "input_filename",
        "input_line_number",
        'import "secrets" as secrets; .',
        'include "secrets"; .',
        'module {"name": "secrets"}; .',
        '"\\(env.HOME)"',
        '"\\(j0)"',
        '"\\((1 # keep scanning\n), env.HOME)"',
    ],
)
def test_selector_rejects_host_observation_facilities(filter_text: str) -> None:
    with pytest.raises(RenderingError) as caught:
        select_one({"env": "data"}, filter_text)

    assert caught.value.code == "jq_filter_forbidden"


def test_selector_rejects_every_platform_dependent_c_math_builtin() -> None:
    expected = {
        "acos",
        "acosh",
        "asin",
        "asinh",
        "atan",
        "atan2",
        "atanh",
        "cbrt",
        "ceil",
        "copysign",
        "cos",
        "cosh",
        "drem",
        "erf",
        "erfc",
        "exp",
        "exp10",
        "exp2",
        "expm1",
        "fabs",
        "fdim",
        "floor",
        "fma",
        "fmax",
        "fmin",
        "fmod",
        "frexp",
        "gamma",
        "hypot",
        "j0",
        "j1",
        "jn",
        "ldexp",
        "lgamma",
        "lgamma_r",
        "log",
        "log10",
        "log1p",
        "log2",
        "logb",
        "modf",
        "nearbyint",
        "nextafter",
        "nexttoward",
        "pow",
        "pow10",
        "remainder",
        "rint",
        "round",
        "scalb",
        "scalbln",
        "significand",
        "sin",
        "sinh",
        "sqrt",
        "tan",
        "tanh",
        "tgamma",
        "trunc",
        "y0",
        "y1",
        "yn",
    }
    assert jq_module._PLATFORM_DEPENDENT_MATH_IDENTIFIERS == expected

    for identifier in sorted(expected):
        with pytest.raises(RenderingError) as caught:
            select_one(None, identifier)
        assert caught.value.code == "jq_filter_forbidden"
        assert caught.value.details["token"] == identifier


def test_selector_rejects_every_time_dependent_builtin() -> None:
    expected = {
        "fromdate",
        "fromdateiso8601",
        "gmtime",
        "localtime",
        "mktime",
        "now",
        "strftime",
        "strflocaltime",
        "strptime",
        "todate",
        "todateiso8601",
    }
    assert jq_module._NONDETERMINISTIC_IDENTIFIERS - jq_module._PLATFORM_DEPENDENT_MATH_IDENTIFIERS == expected

    for identifier in sorted(expected):
        with pytest.raises(RenderingError) as caught:
            select_one(None, identifier)
        assert caught.value.code == "jq_filter_forbidden"
        assert caught.value.details["token"] == identifier


def test_selector_scan_is_token_aware_for_data_strings_fields_and_comments() -> None:
    source = {
        "env": "data",
        "j0": "math field",
        "modulemeta": "module field",
        "now": "stored",
    }

    assert select_one(source, ".env # import module include $ENV") == "data"
    assert select_one(source, ".j0") == "math field"
    assert select_one(source, ".modulemeta") == "module field"
    assert select_one(source, ".now # now localtime") == "stored"
    assert select_one(source, '"env import include module modulemeta j0 $ENV"') == (
        "env import include module modulemeta j0 $ENV"
    )


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
        return subprocess.CompletedProcess(command, 0, b'{"ok":true,"results":["stdin-only"]}', b"")

    monkeypatch.setattr(subprocess, "run", complete)

    assert select_one(None, "$bound", args={"bound": "stdin-only"}) == "stdin-only"
    assert observed["env"] == jq_module._worker_environment()
    assert observed["check"] is False
    assert observed["input"] == b'{"args":{"bound":"stdin-only"},"data":null}'
    assert observed["stdout"] is subprocess.PIPE
    assert observed["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in observed
    assert observed["cwd"] != "."
    command = cast_list(observed["command"])
    assert command[1] == "-I"
    assert all("stdin-only" not in argument for argument in command)


@pytest.mark.parametrize(
    "source",
    [
        b"not json",
        b'{"args":{},"data":null,"extra":true}',
        b'{"args":[],"data":null}',
        b'{"args":{"value":1,"value":2},"data":null}',
        b'{"args":{"$value":1},"data":null}',
        b'{"args":{},"data":NaN}',
    ],
)
def test_worker_rejects_malformed_or_noncanonical_stdin_protocol(source: bytes) -> None:
    from gh_slate.rendering import jq as jq_module

    worker = Path(jq_module.__file__).with_name("_jq_worker.py")
    command = [
        sys.executable,
        "-I",
        str(worker),
        base64.b64encode(b".").decode("ascii"),
        "1",
        "4096",
        "4096",
        str(DEFAULT_JQ_LIMITS.max_memory_bytes),
        str(DEFAULT_JQ_LIMITS.max_cpu_seconds),
    ]

    completed = subprocess.run(
        command,
        input=source,
        capture_output=True,
        env=jq_module._worker_environment(),
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == b'{"ok":false,"kind":"protocol"}'


def test_worker_environment_preserves_only_windows_system_root() -> None:
    parent = {
        "SystemRoot": "C:\\Windows",
        "GH_TOKEN": "must-not-leak",
        "HOME": "must-not-leak",
    }

    assert jq_module._worker_environment(platform="win32", environment=parent) == {"SystemRoot": "C:\\Windows"}
    assert jq_module._worker_environment(platform="linux", environment=parent) == {}

    with pytest.raises(RenderingError) as caught:
        jq_module._worker_environment(platform="win32", environment={})
    assert caught.value.code == "jq_worker_error"
    assert caught.value.details == {"os_error": "SystemRootMissing"}


def test_worker_applies_a_windows_job_memory_limit_instead_of_unix_rlimits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = object()
    observed: list[tuple[str, int, int | None]] = []

    def windows_limit(memory_bytes: int) -> object:
        observed.append(("windows", memory_bytes, None))
        return guard

    def unix_limits(memory_bytes: int, cpu_seconds: int) -> None:
        observed.append(("unix", memory_bytes, cpu_seconds))

    monkeypatch.setattr(worker_module, "_apply_windows_memory_limit", windows_limit)
    monkeypatch.setattr(worker_module, "_apply_unix_limits", unix_limits)

    assert worker_module._apply_process_limits(4096, 3, platform="nt") is guard
    assert worker_module._apply_process_limits(8192, 5, platform="posix") is None
    assert observed == [("windows", 4096, None), ("unix", 8192, 5)]


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


def test_worker_result_count_is_bounded_by_the_requested_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def complete(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, b'{"ok":true,"results":[1,2,3]}', b"")

    monkeypatch.setattr(subprocess, "run", complete)

    with pytest.raises(RenderingError) as caught:
        evaluate(None, ".", max_results=2)

    assert caught.value.code == "jq_result_limit"


def test_worker_protocol_reserves_nodes_for_the_response_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = b'{"ok":true,"results":[' + b",".join([b"0"] * MAX_RESULTS) + b"]}"

    def complete(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, response, b"")

    monkeypatch.setattr(subprocess, "run", complete)

    results = evaluate(None, ".", max_results=MAX_RESULTS)

    assert len(results) == MAX_RESULTS
    assert results[0] == results[-1] == Decimal(0)


def test_worker_protocol_reserves_one_node_for_the_result_limit_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = b'{"ok":true,"results":[' + b",".join([b"0"] * (MAX_RESULTS + 1)) + b"]}"

    def complete(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, response, b"")

    monkeypatch.setattr(subprocess, "run", complete)

    with pytest.raises(RenderingError) as caught:
        evaluate(None, ".", max_results=MAX_RESULTS)

    assert caught.value.code == "jq_result_limit"


def test_worker_protocol_preserves_the_full_user_json_depth_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nested(depth: int) -> object:
        value: object = 0
        for _ in range(depth):
            value = [value]
        return value

    allowed = nested(DEFAULT_JSON_LIMITS.max_depth + 1)
    rejected = nested(DEFAULT_JSON_LIMITS.max_depth + 2)
    responses = [
        json.dumps(
            {"ok": True, "results": [allowed]},
            separators=(",", ":"),
        ).encode(),
        json.dumps(
            {"ok": True, "results": [rejected]},
            separators=(",", ":"),
        ).encode(),
    ]

    def complete(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, responses.pop(0), b"")

    monkeypatch.setattr(subprocess, "run", complete)

    value = select_one(None, ".")
    for _ in range(DEFAULT_JSON_LIMITS.max_depth + 1):
        assert isinstance(value, tuple) and len(value) == 1
        value = value[0]
    assert value == Decimal(0)

    with pytest.raises(RenderingError) as caught:
        select_one(None, ".")
    assert caught.value.code == "jq_worker_protocol"


def test_libjq_large_integer_rounding_is_projection_only_and_strings_stay_exact() -> None:
    exact_integer = Decimal(9007199254740993)
    source = {"number": exact_integer, "string": "9007199254740993"}

    assert select_one(source, ".number") == Decimal(9007199254740992)
    assert select_one(source, ".string") == "9007199254740993"
    assert source["number"] == exact_integer


@pytest.mark.parametrize("huge", [Decimal("1e309"), Decimal("-1e309")])
def test_finite_numbers_beyond_float_range_do_not_break_unrelated_queries(
    huge: Decimal,
) -> None:
    source = {"huge": huge, "status": "ready"}

    assert evaluate(source, ".status") == ("ready",)
    assert source["huge"] == huge


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
