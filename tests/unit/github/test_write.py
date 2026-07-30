from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.errors import GhSlateError
from gh_slate.github.process import ProcessResult
from gh_slate.github.write import (
    DEFAULT_GH_WRITE_LIMITS,
    GhWriteOutcomeUnknown,
    GhWriteProcess,
    GhWriteTimeout,
    SubprocessWriteRunner,
)

if TYPE_CHECKING:
    from typing import BinaryIO


@dataclass(slots=True)
class FakeWriteRunner:
    results: list[ProcessResult] = field(default_factory=list)
    calls: list[
        tuple[
            tuple[str, ...],
            bytes,
            float,
            str | None,
            int,
            int,
        ]
    ] = field(default_factory=list)
    error: BaseException | None = None

    def run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes,
        timeout: float,
        hostname: str | None,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        self.calls.append(
            (
                argv,
                stdin,
                timeout,
                hostname,
                max_stdout_bytes,
                max_stderr_bytes,
            )
        )
        if self.error is not None:
            raise self.error
        if not self.results:
            raise AssertionError("fake runner has no queued result")
        return self.results.pop(0)


def success(stdout: bytes = b'{"id":123}') -> ProcessResult:
    return ProcessResult(returncode=0, stdout=stdout, stderr=b"")


def writer(
    runner: FakeWriteRunner,
    *,
    max_input_bytes: int | None = None,
    max_stdout_bytes: int | None = None,
    max_stderr_bytes: int | None = None,
) -> GhWriteProcess:
    limits = DEFAULT_GH_WRITE_LIMITS
    if max_input_bytes is not None:
        limits = replace(limits, max_input_bytes=max_input_bytes)
    if max_stdout_bytes is not None:
        limits = replace(limits, max_stdout_bytes=max_stdout_bytes)
    if max_stderr_bytes is not None:
        limits = replace(limits, max_stderr_bytes=max_stderr_bytes)
    return GhWriteProcess(runner=runner, limits=limits)


@pytest.mark.parametrize(
    ("operation", "method", "endpoint"),
    [
        (
            "post",
            "POST",
            "repos/owner/repo/issues/42/comments",
        ),
        (
            "patch",
            "PATCH",
            "repos/owner/repo/issues/comments/123",
        ),
    ],
)
def test_write_uses_exact_argv_and_canonical_json_stdin(
    operation: str,
    method: str,
    endpoint: str,
) -> None:
    runner = FakeWriteRunner(results=[success()])
    process = writer(runner)

    result = getattr(process, operation)(
        endpoint,
        {"body": "dashboard"},
        hostname="github.example.com",
    )

    assert isinstance(result, Mapping)
    assert result["id"] == Decimal(123)
    assert runner.calls == [
        (
            (
                "gh",
                "api",
                "--hostname",
                "github.example.com",
                "--method",
                method,
                "--input",
                "-",
                endpoint,
            ),
            b'{"body":"dashboard"}',
            30.0,
            "github.example.com",
            8 * 1024 * 1024,
            64 * 1024,
        )
    ]


@pytest.mark.parametrize(
    "method",
    ["GET", "DELETE", "PUT", "OPTIONS", "POST --input payload.json"],
)
def test_internal_request_rejects_every_method_outside_post_and_patch(
    method: str,
) -> None:
    runner = FakeWriteRunner()

    with pytest.raises(GhSlateError) as caught:
        writer(runner)._request(
            method,
            "repos/owner/repo/issues/42/comments",
            {"body": "safe"},
        )

    assert caught.value.code == "gh_write_method_forbidden"
    assert runner.calls == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "--input",
        "-Fbody=@secret",
        "https://api.github.com/repos/o/r",
        "//api.github.com/repos/o/r",
        "repos/o/r/../issues",
        "repos/o/r#fragment",
        "repos/o/r\n--method DELETE",
        "repos\\o\\r",
        "repos/o/r/issues/42/comments?per_page=100",
        "repos/o/{repo}/issues/42/comments",
        "repos/o/%72/issues/42/comments",
        "repos/o/r/issues/0/comments",
        "repos/o/r/issues/comments/0",
        "repos/o/r/issues/42",
    ],
)
def test_endpoint_and_option_injection_are_rejected(endpoint: str) -> None:
    runner = FakeWriteRunner()

    with pytest.raises(GhSlateError) as caught:
        writer(runner).post(endpoint, {"body": "safe"})

    assert caught.value.code == "gh_write_endpoint_invalid"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("method", "endpoint"),
    [
        ("POST", "repos/owner/repo/issues/comments/123"),
        ("PATCH", "repos/owner/repo/issues/42/comments"),
    ],
)
def test_method_must_match_the_exact_comment_route(
    method: str,
    endpoint: str,
) -> None:
    runner = FakeWriteRunner()

    with pytest.raises(GhSlateError) as caught:
        writer(runner)._request(method, endpoint, {"body": "safe"})

    assert caught.value.code == "gh_write_endpoint_invalid"
    assert runner.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"body": "safe", "extra": True},
        {"body": 1},
        {"other": "safe"},
        [],
    ],
)
def test_payload_is_exactly_one_string_body(payload: object) -> None:
    runner = FakeWriteRunner()

    with pytest.raises(GhSlateError) as caught:
        writer(runner).post(
            "repos/owner/repo/issues/42/comments",
            cast("Mapping[str, object]", payload),
        )

    assert caught.value.code == "gh_write_payload_invalid"
    assert runner.calls == []


@pytest.mark.parametrize(
    "hostname",
    [
        "",
        "--hostname",
        "https://github.example",
        "github.example/path",
        "bad host",
        "github..example",
        "github.-example",
        "github.example:0",
        "github.example:65536",
    ],
)
def test_invalid_hostname_is_rejected_before_the_runner(hostname: str) -> None:
    runner = FakeWriteRunner()

    with pytest.raises(GhSlateError) as caught:
        writer(runner).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "safe"},
            hostname=hostname,
        )

    assert caught.value.code == "gh_hostname_invalid"
    assert runner.calls == []


def test_payload_must_be_a_canonical_json_object() -> None:
    runner = FakeWriteRunner()

    with pytest.raises(GhSlateError) as caught:
        writer(runner).post(
            "repos/owner/repo/issues/42/comments",
            {"body": float("nan")},
        )

    assert caught.value.code == "gh_write_payload_invalid"
    assert runner.calls == []


def test_payload_limit_is_enforced_before_process_start() -> None:
    runner = FakeWriteRunner()

    with pytest.raises(GhSlateError) as caught:
        writer(runner, max_input_bytes=4).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "dashboard"},
        )

    assert caught.value.code == "gh_write_payload_limit"
    assert runner.calls == []


def test_default_runner_never_uses_a_shell_and_inherits_gh_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("GH_TOKEN", "inherited-token")

    def complete(
        argv: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured.update(kwargs)
        cast("BinaryIO", kwargs["stdout"]).write(b"{}")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", complete)

    result = SubprocessWriteRunner().run(
        ("gh", "api", "--method", "POST", "--input", "-", "repos/o/r"),
        stdin=b'{"body":"secret"}',
        timeout=1.5,
        hostname="github.example.com",
        max_stdout_bytes=8,
        max_stderr_bytes=8,
    )

    assert result == ProcessResult(0, b"{}", b"")
    assert captured["shell"] is False
    assert captured["input"] == b'{"body":"secret"}'
    assert "capture_output" not in captured
    assert "stdout" in captured
    assert "stderr" in captured
    assert captured["check"] is False
    environment = cast("dict[str, str]", captured["env"])
    assert environment["HTTPS_PROXY"] == "http://proxy.example"
    assert environment["GH_TOKEN"] == "inherited-token"
    assert environment["GH_HOST"] == "github.example.com"
    assert os.environ["GH_TOKEN"] == "inherited-token"


def test_default_runner_reads_only_one_byte_beyond_each_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def complete(
        argv: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        cast("BinaryIO", kwargs["stdout"]).write(b"x" * 100)
        cast("BinaryIO", kwargs["stderr"]).write(b"y" * 100)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", complete)

    result = SubprocessWriteRunner().run(
        ("gh", "api", "--method", "POST", "--input", "-", "repos/o/r"),
        stdin=b"{}",
        timeout=1,
        hostname=None,
        max_stdout_bytes=4,
        max_stderr_bytes=3,
    )

    assert result.stdout == b"x" * 5
    assert result.stderr == b"y" * 4


def test_timeout_uses_a_dedicated_unknown_outcome_error() -> None:
    runner = FakeWriteRunner(
        error=subprocess.TimeoutExpired(
            cmd=("gh", "api"),
            timeout=1,
            output=b'{"body":"must-not-leak"}',
        )
    )

    with pytest.raises(GhWriteTimeout) as caught:
        writer(runner).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "must-not-leak"},
        )

    assert caught.value.code == "gh_write_timeout"
    rendered = repr(caught.value.as_dict())
    assert "must-not-leak" not in rendered
    assert "api" not in rendered


def test_nonzero_failure_does_not_include_payload_token_or_stderr() -> None:
    secret = "TOP-SECRET-PAYLOAD"
    stderr = f"token ghp_secret; body={secret}".encode()
    runner = FakeWriteRunner(
        results=[
            ProcessResult(
                returncode=1,
                stdout=b"{}",
                stderr=stderr,
            )
        ]
    )

    with pytest.raises(GhWriteOutcomeUnknown) as caught:
        writer(runner).patch(
            "repos/owner/repo/issues/comments/123",
            {"body": secret},
        )

    assert caught.value.code == "gh_write_failed"
    rendered = repr(caught.value.as_dict())
    assert secret not in rendered
    assert "ghp_secret" not in rendered
    assert caught.value.details == {
        "returncode": 1,
        "stderr_bytes": len(stderr),
    }


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (ProcessResult(0, b"\xff", b""), "gh_write_output_invalid"),
        (ProcessResult(0, b"{}", b"\xff"), "gh_write_output_invalid"),
        (ProcessResult(0, b'{"a":1,"a":2}', b""), "gh_write_json_invalid"),
        (ProcessResult(0, b"not json", b""), "gh_write_json_invalid"),
    ],
)
def test_invalid_utf8_and_strict_json_are_rejected(
    result: ProcessResult,
    code: str,
) -> None:
    runner = FakeWriteRunner(results=[result])

    with pytest.raises(GhWriteOutcomeUnknown) as caught:
        writer(runner).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "safe"},
        )

    assert caught.value.code == code


def test_stdout_and_stderr_limits_are_enforced_before_json_parsing() -> None:
    stdout_runner = FakeWriteRunner(results=[success(b"12345")])
    with pytest.raises(GhWriteOutcomeUnknown) as stdout_error:
        writer(stdout_runner, max_stdout_bytes=4).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "x"},
        )
    assert stdout_error.value.code == "gh_write_output_limit"
    assert stdout_error.value.details["stream"] == "stdout"

    stderr_runner = FakeWriteRunner(results=[ProcessResult(returncode=0, stdout=b"{}", stderr=b"12345")])
    with pytest.raises(GhWriteOutcomeUnknown) as stderr_error:
        writer(stderr_runner, max_stderr_bytes=4).patch(
            "repos/owner/repo/issues/comments/123",
            {"body": "x"},
        )
    assert stderr_error.value.code == "gh_write_output_limit"
    assert stderr_error.value.details["stream"] == "stderr"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FileNotFoundError(), "gh_not_found"),
        (PermissionError(), "gh_write_process_error"),
    ],
)
def test_process_start_failures_are_wrapped(
    error: BaseException,
    code: str,
) -> None:
    runner = FakeWriteRunner(error=error)

    with pytest.raises(GhSlateError) as caught:
        writer(runner).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "safe"},
        )

    assert caught.value.code == code


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0),
        ("max_input_bytes", True),
        ("max_stdout_bytes", -1),
        ("max_stderr_bytes", 0),
    ],
)
def test_limits_require_positive_values(field: str, value: object) -> None:
    arguments: dict[str, object] = {
        "timeout_seconds": 1,
        "max_input_bytes": 1,
        "max_stdout_bytes": 1,
        "max_stderr_bytes": 1,
    }
    arguments[field] = value

    with pytest.raises(ValueError, match=field):
        type(DEFAULT_GH_WRITE_LIMITS)(**arguments)  # type: ignore[arg-type]
