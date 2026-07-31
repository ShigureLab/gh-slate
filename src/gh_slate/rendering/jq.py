from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import cast

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import (
    DEFAULT_JSON_LIMITS,
    JsonLimits,
    JsonValue,
    canonical_json_bytes,
    strict_loads,
)
from gh_slate.rendering.errors import RenderingError


@dataclass(frozen=True, slots=True)
class JqLimits:
    """Resource limits for one isolated jq selector evaluation."""

    max_selector_bytes: int = 16 * 1024
    max_source_bytes: int = 256 * 1024
    max_output_bytes: int = 256 * 1024
    timeout_seconds: float = 2.0
    max_memory_bytes: int = 512 * 1024 * 1024
    max_cpu_seconds: int = 2

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{item.name} must be a positive number")
        for name in (
            "max_selector_bytes",
            "max_source_bytes",
            "max_output_bytes",
            "max_memory_bytes",
            "max_cpu_seconds",
        ):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_JQ_LIMITS = JqLimits()
DEFAULT_MAX_RESULTS = 1024
MAX_RESULTS = 100_000

_FORBIDDEN_IDENTIFIERS = frozenset(
    {
        "builtins",
        "env",
        "get_jq_origin",
        "get_prog_origin",
        "get_search_list",
        "have_decnum",
        "have_literal_numbers",
        "import",
        "include",
        "input",
        "input_filename",
        "input_line_number",
        "inputs",
        "module",
        "modulemeta",
    }
)
_PLATFORM_DEPENDENT_MATH_IDENTIFIERS = frozenset(
    {
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
)
_NONDETERMINISTIC_IDENTIFIERS = (
    frozenset(
        {
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
    )
    | _PLATFORM_DEPENDENT_MATH_IDENTIFIERS
)
_FORBIDDEN_VARIABLES = frozenset({"ENV", "JQ_BUILD_CONFIGURATION"})
_ARGUMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EMPTY_ARGS: Mapping[str, JsonValue] = MappingProxyType({})
_PROTOCOL_MAX_DEPTH = DEFAULT_JSON_LIMITS.max_depth + 2
_PROTOCOL_NODE_OVERHEAD = 3
_PROTOCOL_OVERFLOW_SENTINEL_NODES = 1
_PROTOCOL_MAX_NODES = DEFAULT_JSON_LIMITS.max_nodes + _PROTOCOL_NODE_OVERHEAD + _PROTOCOL_OVERFLOW_SENTINEL_NODES
_WORKER_ERROR_CODES = {
    "compile": "jq_compile_error",
    "runtime": "jq_runtime_error",
    "output_limit": "jq_output_limit",
    "source_limit": "jq_source_limit",
    "protocol": "jq_worker_protocol",
    "internal": "jq_worker_error",
}


def _worker_environment(
    *,
    platform: str = sys.platform,
    environment: Mapping[str, str] = os.environ,
) -> dict[str, str]:
    """Return the smallest environment needed to start the worker.

    Windows requires ``SystemRoot`` to start some executables and side-by-side
    assemblies. The trusted worker clears this value before importing jq, so
    selectors still cannot observe the parent process environment.
    """

    if platform != "win32":
        return {}
    system_root = environment.get("SystemRoot")
    if not system_root:
        raise _rendering_error(
            "jq worker requires SystemRoot on Windows",
            code="jq_worker_error",
            os_error="SystemRootMissing",
        )
    return {"SystemRoot": system_root}


def _rendering_error(message: str, *, code: str, **details: object) -> RenderingError:
    return RenderingError(message, code=code, details=details)


def _utf8_bytes(value: str, *, subject: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _rendering_error(
            f"jq {subject} is not valid Unicode",
            code=f"jq_{subject}_invalid",
            position=error.start,
        ) from None


def _scan_filter(filter_text: str, *, deterministic: bool) -> None:
    """Reject jq facilities that can observe the worker host.

    The scanner ignores ordinary string content and comments, but follows jq
    string interpolations so ``env`` cannot be hidden inside ``"\\(env)"``.
    It is intentionally conservative for executable identifiers.
    """

    def previous_significant(index: int) -> str | None:
        index -= 1
        while index >= 0 and filter_text[index].isspace():
            index -= 1
        return filter_text[index] if index >= 0 else None

    def scan_string(index: int) -> int:
        while index < len(filter_text):
            char = filter_text[index]
            if char == '"':
                return index + 1
            if char != "\\":
                index += 1
                continue
            if index + 1 >= len(filter_text):
                return len(filter_text)
            if filter_text[index + 1] == "(":
                index = scan_code(index + 2, interpolation=True)
            else:
                index += 2
        return index

    def scan_code(index: int, *, interpolation: bool) -> int:
        parentheses = 0
        while index < len(filter_text):
            char = filter_text[index]
            if char == "#":
                newline = filter_text.find("\n", index + 1)
                if newline < 0:
                    return len(filter_text)
                index = newline + 1
                continue
            if char == '"':
                index = scan_string(index + 1)
                continue
            if char == "(":
                parentheses += 1
                index += 1
                continue
            if char == ")" and interpolation:
                if parentheses == 0:
                    return index + 1
                parentheses -= 1
                index += 1
                continue
            if char == "$":
                end = index + 1
                while end < len(filter_text) and (filter_text[end] == "_" or filter_text[end].isalnum()):
                    end += 1
                variable = filter_text[index + 1 : end]
                if variable in _FORBIDDEN_VARIABLES:
                    raise _rendering_error(
                        "jq environment access is disabled",
                        code="jq_filter_forbidden",
                        token=f"${variable}",
                    )
                index = max(end, index + 1)
                continue
            if char == "_" or char.isalpha():
                end = index + 1
                while end < len(filter_text) and (filter_text[end] == "_" or filter_text[end].isalnum()):
                    end += 1
                identifier = filter_text[index:end]
                forbidden = identifier in _FORBIDDEN_IDENTIFIERS or (
                    deterministic and identifier in _NONDETERMINISTIC_IDENTIFIERS
                )
                if forbidden and previous_significant(index) != ".":
                    raise _rendering_error(
                        "jq host-dependent and nondeterministic builtins are disabled",
                        code="jq_filter_forbidden",
                        token=identifier,
                    )
                index = end
                continue
            index += 1
        return index

    scan_code(0, interpolation=False)


def _protocol_error(reason: str, **details: object) -> RenderingError:
    return _rendering_error(
        "jq worker returned an invalid response",
        code="jq_worker_protocol",
        reason=reason,
        **details,
    )


def _user_node_count(values: tuple[JsonValue, ...]) -> int:
    def count(value: JsonValue) -> int:
        if isinstance(value, Mapping):
            typed = cast("Mapping[str, JsonValue]", value)
            return 1 + sum(count(item) for item in typed.values())
        if isinstance(value, tuple):
            return 1 + sum(count(item) for item in value)
        return 1

    return sum(count(value) for value in values)


def _decode_worker_response(
    stdout: bytes,
    limits: JqLimits,
    *,
    max_results: int,
) -> tuple[JsonValue, ...]:
    protocol_limit = limits.max_output_bytes + 1024
    if len(stdout) > protocol_limit:
        raise _rendering_error(
            "jq output exceeds the configured byte limit",
            code="jq_output_limit",
            actual_bytes=len(stdout),
            max_bytes=limits.max_output_bytes,
        )
    try:
        response = strict_loads(
            stdout,
            limits=JsonLimits(
                max_input_bytes=protocol_limit,
                # The worker wraps one user value in a results array and a
                # response object. Keep the user's full JSON depth budget.
                max_depth=_PROTOCOL_MAX_DEPTH,
                # Reserve the root/status/results nodes plus one result-limit
                # sentinel. Successful user data is checked independently
                # below against its unchanged node budget.
                max_nodes=_PROTOCOL_MAX_NODES,
                max_string_bytes=max(limits.max_output_bytes, 64),
                max_number_chars=1024,
                max_key_bytes=DEFAULT_JSON_LIMITS.max_key_bytes,
            ),
        )
    except CodecError:
        raise _protocol_error("invalid_json") from None
    if not isinstance(response, Mapping):
        raise _protocol_error("root_not_object")

    ok = response.get("ok")
    if ok is True:
        if set(response) != {"ok", "results"}:
            raise _protocol_error("unexpected_success_fields")
        results = response.get("results")
        if not isinstance(results, tuple):
            raise _protocol_error("results_not_array")
        typed_results = cast("tuple[JsonValue, ...]", results)
        if len(typed_results) > max_results + 1:
            raise _protocol_error("too_many_results", count=len(typed_results))
        if len(typed_results) > max_results:
            raise _rendering_error(
                "jq produced more values than the configured result limit",
                code="jq_result_limit",
                count_at_least=max_results + 1,
                max_results=max_results,
            )
        if _user_node_count(typed_results) > DEFAULT_JSON_LIMITS.max_nodes:
            raise _protocol_error(
                "result_node_limit",
                max_nodes=DEFAULT_JSON_LIMITS.max_nodes,
            )
        return typed_results

    if ok is False:
        if set(response) != {"ok", "kind"}:
            raise _protocol_error("unexpected_error_fields")
        kind = response.get("kind")
        if not isinstance(kind, str) or kind not in _WORKER_ERROR_CODES:
            raise _protocol_error("unknown_error_kind")
        code = _WORKER_ERROR_CODES[kind]
        messages = {
            "compile": "jq filter could not be compiled",
            "runtime": "jq filter failed during evaluation",
            "output_limit": "jq output exceeds the configured byte limit",
            "source_limit": "jq source exceeds the configured byte limit",
            "protocol": "jq worker rejected its request protocol",
            "internal": "jq worker failed",
        }
        raise _rendering_error(
            messages[kind],
            code=code,
            max_bytes=limits.max_output_bytes if kind == "output_limit" else limits.max_source_bytes,
        )

    raise _protocol_error("invalid_status")


def _validated_max_results(max_results: int) -> int:
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise _rendering_error(
            "jq max_results must be an integer",
            code="jq_max_results_invalid",
            value_type=type(max_results).__name__,
        )
    if max_results <= 0 or max_results > MAX_RESULTS:
        raise _rendering_error(
            f"jq max_results must be between 1 and {MAX_RESULTS}",
            code="jq_max_results_invalid",
            maximum=MAX_RESULTS,
            configured=max_results,
        )
    return max_results


def _request_bytes(
    data: object,
    args: Mapping[str, JsonValue],
    *,
    limits: JqLimits,
) -> bytes:
    if not isinstance(args, Mapping):
        raise _rendering_error(
            "jq args must be a mapping",
            code="jq_args_invalid",
            value_type=type(args).__name__,
        )

    normalized_args: dict[str, JsonValue] = {}
    for name, value in args.items():
        if not isinstance(name, str) or _ARGUMENT_NAME.fullmatch(name) is None:
            raise _rendering_error(
                "jq argument names must be ASCII identifiers without a leading dollar sign",
                code="jq_args_invalid",
                name=name if isinstance(name, str) else None,
                name_type=type(name).__name__,
            )
        normalized_args[name] = value

    try:
        args_source = canonical_json_bytes(normalized_args)
    except CodecError as error:
        raise _rendering_error(
            "jq args contain an unsupported JSON value",
            code="jq_args_invalid",
            cause=error.code,
        ) from None
    try:
        data_source = canonical_json_bytes(data)
    except CodecError as error:
        raise _rendering_error(
            "jq source is not a supported JSON value",
            code="jq_source_invalid",
            cause=error.code,
        ) from None

    source = b'{"args":' + args_source + b',"data":' + data_source + b"}"
    if len(source) > limits.max_source_bytes:
        raise _rendering_error(
            "jq source and arguments exceed the configured byte limit",
            code="jq_source_limit",
            actual_bytes=len(source),
            max_bytes=limits.max_source_bytes,
        )
    return source


def _evaluate(
    data: object,
    filter: str,
    *,
    args: Mapping[str, JsonValue] = _EMPTY_ARGS,
    max_results: int = DEFAULT_MAX_RESULTS,
    limits: JqLimits = DEFAULT_JQ_LIMITS,
    deterministic: bool,
) -> tuple[JsonValue, ...]:
    max_results = _validated_max_results(max_results)
    filter_text = filter
    if not isinstance(filter_text, str):
        raise _rendering_error(
            "jq filter must be a string",
            code="jq_filter_invalid",
            value_type=type(filter_text).__name__,
        )
    filter_bytes = _utf8_bytes(filter_text, subject="filter")
    if not filter_bytes.strip():
        raise _rendering_error("jq filter must not be empty", code="jq_filter_invalid")
    if len(filter_bytes) > limits.max_selector_bytes:
        raise _rendering_error(
            "jq filter exceeds the configured byte limit",
            code="jq_filter_limit",
            actual_bytes=len(filter_bytes),
            max_bytes=limits.max_selector_bytes,
        )
    _scan_filter(filter_text, deterministic=deterministic)
    source = _request_bytes(data, args, limits=limits)

    worker = Path(__file__).with_name("_jq_worker.py")
    encoded_filter = base64.b64encode(filter_bytes).decode("ascii")
    command = [
        sys.executable,
        "-I",
        str(worker),
        encoded_filter,
        str(max_results),
        str(limits.max_source_bytes),
        str(limits.max_output_bytes),
        str(limits.max_memory_bytes),
        str(limits.max_cpu_seconds),
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="gh-slate-jq-") as empty_cwd:
            completed = subprocess.run(
                command,
                input=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=empty_cwd,
                env=_worker_environment(),
                timeout=limits.timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired:
        raise _rendering_error(
            "jq evaluation exceeded the configured timeout",
            code="jq_timeout",
            timeout_seconds=limits.timeout_seconds,
        ) from None
    except OSError as error:
        raise _rendering_error(
            "jq worker could not be started",
            code="jq_worker_error",
            os_error=type(error).__name__,
        ) from None

    if completed.returncode != 0:
        raise _rendering_error(
            "jq worker exited unexpectedly",
            code="jq_worker_error",
            returncode=completed.returncode,
        )
    return _decode_worker_response(completed.stdout, limits, max_results=max_results)


def evaluate(
    data: object,
    filter: str,
    *,
    args: Mapping[str, JsonValue] = _EMPTY_ARGS,
    max_results: int = DEFAULT_MAX_RESULTS,
    limits: JqLimits = DEFAULT_JQ_LIMITS,
) -> tuple[JsonValue, ...]:
    """Evaluate jq against one JSON value in an isolated subprocess.

    At most ``max_results`` values are returned. The worker evaluates one
    additional value so result overflow is detected without materializing an
    unbounded stream. Named jq arguments travel only in the strict JSON stdin
    protocol, never in argv or the worker environment.

    libjq uses IEEE-754 numbers, so oversized JSON integers may be rounded in
    the returned projection. Encode values that require exact lexical or
    arbitrary-precision preservation as strings.
    """

    return _evaluate(
        data,
        filter,
        args=args,
        max_results=max_results,
        limits=limits,
        deterministic=False,
    )


def select_one(
    data: object,
    filter: str,
    *,
    args: Mapping[str, JsonValue] = _EMPTY_ARGS,
    limits: JqLimits = DEFAULT_JQ_LIMITS,
) -> JsonValue:
    """Project exactly one JSON value with jq in an isolated subprocess.

    libjq uses IEEE-754 numbers, so an oversized JSON integer may be rounded in
    the returned projection. The input state is never mutated; encode IDs that
    require exact lexical or arbitrary-precision preservation as strings.
    """

    try:
        results = _evaluate(
            data,
            filter,
            args=args,
            max_results=1,
            limits=limits,
            deterministic=True,
        )
    except RenderingError as error:
        if error.code != "jq_result_limit":
            raise
        raise _rendering_error(
            "jq selector must produce exactly one value",
            code="jq_multiple_results",
            count=2,
        ) from None
    if not results:
        raise _rendering_error("jq selector produced no value", code="jq_no_result")
    return results[0]


__all__ = [
    "DEFAULT_JQ_LIMITS",
    "DEFAULT_MAX_RESULTS",
    "MAX_RESULTS",
    "JqLimits",
    "evaluate",
    "select_one",
]
