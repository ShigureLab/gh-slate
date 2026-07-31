from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, cast

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
    popen = subprocess.Popen

    def start(
        argv: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.Popen[bytes]:
        captured["argv"] = argv
        captured.update(kwargs)
        return cast("subprocess.Popen[bytes]", popen(argv, **kwargs))

    monkeypatch.setattr(subprocess, "Popen", start)

    result = SubprocessWriteRunner().run(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ),
        stdin=b'{"body":"secret"}',
        timeout=1.5,
        hostname="github.example.com",
        max_stdout_bytes=64,
        max_stderr_bytes=8,
    )

    assert result == ProcessResult(0, b'{"body":"secret"}', b"")
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.PIPE
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert "input" not in captured
    assert "timeout" not in captured
    assert "check" not in captured
    environment = cast("dict[str, str]", captured["env"])
    assert environment["HTTPS_PROXY"] == "http://proxy.example"
    assert environment["GH_TOKEN"] == "inherited-token"
    assert environment["GH_HOST"] == "github.example.com"
    assert os.environ["GH_TOKEN"] == "inherited-token"


@pytest.mark.parametrize(
    ("file_descriptor", "limited_stream"),
    [(1, "stdout"), (2, "stderr")],
)
def test_default_runner_kills_output_overflow_and_keeps_one_sentinel_byte(
    file_descriptor: int,
    limited_stream: str,
) -> None:
    started = time.monotonic()
    result = SubprocessWriteRunner().run(
        (
            sys.executable,
            "-c",
            (
                "import os,time\n"
                "try:\n"
                f" os.write({file_descriptor},b'x'*(10*1024*1024))\n"
                "except OSError:\n"
                " pass\n"
                "time.sleep(10)"
            ),
        ),
        stdin=b"{}",
        timeout=5,
        hostname=None,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )

    assert time.monotonic() - started < 3
    assert len(getattr(result, limited_stream)) == 1025
    other_stream = "stderr" if limited_stream == "stdout" else "stdout"
    assert getattr(result, other_stream) == b""


def test_default_runner_kills_and_reaps_on_timeout() -> None:
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessWriteRunner().run(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            stdin=b"{}",
            timeout=0.1,
            hostname=None,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )

    assert time.monotonic() - started < 3


class _InterruptedProcess:
    def __init__(self, interruption: BaseException) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.interruption = interruption
        self.killed = False
        self.wait_calls = 0

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise self.interruption
        return 0


class _CompletedProcess:
    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.killed = False
        self.wait_calls = 0

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        return 0


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(130)])
def test_post_start_interrupt_is_an_unknown_write_outcome(
    interruption: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _InterruptedProcess(interruption)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    with pytest.raises(GhWriteOutcomeUnknown) as caught:
        GhWriteProcess(runner=SubprocessWriteRunner()).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "safe"},
        )

    assert caught.value.code == "gh_write_process_error"
    assert caught.value.details == {"error_type": type(interruption).__name__}
    assert process.killed
    assert process.wait_calls == 2
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(130)])
def test_post_wait_join_interrupt_is_an_unknown_write_outcome(
    interruption: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _CompletedProcess()
    real_join = threading.Thread.join
    join_calls = 0

    def interrupt_once(
        worker: threading.Thread,
        timeout: float | None = None,
    ) -> None:
        nonlocal join_calls
        join_calls += 1
        if join_calls == 1:
            raise interruption
        real_join(worker, timeout)

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(threading.Thread, "join", interrupt_once)

    with pytest.raises(GhWriteOutcomeUnknown) as caught:
        GhWriteProcess(runner=SubprocessWriteRunner()).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "safe"},
        )

    assert caught.value.code == "gh_write_process_error"
    assert caught.value.details == {"error_type": type(interruption).__name__}
    assert process.killed
    assert process.wait_calls == 2
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(130)])
def test_pre_start_interrupt_is_not_reclassified(
    interruption: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def start(*_args: object, **_kwargs: object) -> None:
        raise interruption

    monkeypatch.setattr(subprocess, "Popen", start)

    with pytest.raises(type(interruption)):
        GhWriteProcess(runner=SubprocessWriteRunner()).post(
            "repos/owner/repo/issues/42/comments",
            {"body": "safe"},
        )


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
