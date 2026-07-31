from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import cast

import pytest

from gh_slate.errors import GhSlateError
from gh_slate.github.models import GitHubActor
from gh_slate.github.process import (
    DEFAULT_GH_PROCESS_LIMITS,
    GhProcess,
    ProcessResult,
    SubprocessRunner,
    _kill_process_tree,
)


@dataclass(slots=True)
class FakeRunner:
    results: list[ProcessResult] = field(default_factory=list)
    calls: list[tuple[tuple[str, ...], float, str | None, int, int]] = field(default_factory=list)
    error: BaseException | None = None

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        hostname: str | None,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        self.calls.append(
            (
                argv,
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


def success(stdout: bytes = b"{}") -> ProcessResult:
    return ProcessResult(returncode=0, stdout=stdout, stderr=b"")


def process(
    runner: FakeRunner,
    *,
    max_stdout_bytes: int | None = None,
    max_stderr_bytes: int | None = None,
) -> GhProcess:
    limits = DEFAULT_GH_PROCESS_LIMITS
    if max_stdout_bytes is not None:
        limits = replace(limits, max_stdout_bytes=max_stdout_bytes)
    if max_stderr_bytes is not None:
        limits = replace(limits, max_stderr_bytes=max_stderr_bytes)
    return GhProcess(runner=runner, limits=limits)


def test_api_get_uses_explicit_get_hostname_and_paginated_slurp() -> None:
    runner = FakeRunner(results=[success(b'[[{"id":1}],[{"id":2}]]')])

    result = process(runner).api_get(
        "repos/owner/repo/issues/42/comments?per_page=100",
        hostname="github.example.com",
        paginate=True,
    )

    assert result == (({"id": Decimal(1)},), ({"id": Decimal(2)},))
    assert runner.calls == [
        (
            (
                "gh",
                "api",
                "--hostname",
                "github.example.com",
                "--method",
                "GET",
                "--paginate",
                "--slurp",
                "repos/owner/repo/issues/42/comments?per_page=100",
            ),
            30.0,
            "github.example.com",
            32 * 1024 * 1024,
            64 * 1024,
        )
    ]


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
def test_run_json_rejects_every_non_get_api_method(method: str) -> None:
    runner = FakeRunner()

    with pytest.raises(GhSlateError) as caught:
        process(runner).run_json(
            ("api", "--method", method, "repos/owner/repo"),
        )

    assert caught.value.code == "gh_api_method_forbidden"
    assert runner.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        ("api", "--method", "GET", "--input", "payload.json", "repos/o/r"),
        ("api", "--method", "GET", "-F", "body=@secret", "repos/o/r"),
        ("issue", "delete", "42"),
        ("auth", "status", "--show-token", "--json", "hosts"),
        ("auth", "status", "--show-token=true", "--json", "hosts"),
        ("repo", "view", "--web"),
        ("pr", "view", "--web=true"),
    ],
)
def test_run_json_rejects_payload_secret_and_side_effect_options(
    arguments: tuple[str, ...],
) -> None:
    runner = FakeRunner()

    with pytest.raises(GhSlateError) as caught:
        process(runner).run_json(arguments)

    assert caught.value.code == "gh_command_forbidden"
    assert runner.calls == []


def test_current_actor_validates_the_user_response() -> None:
    runner = FakeRunner(results=[success(b'{"id":583231,"login":"octocat"}')])

    assert process(runner).current_actor("github.example.com") == GitHubActor(
        id=583231,
        login="octocat",
    )
    assert runner.calls[0][0] == (
        "gh",
        "api",
        "--hostname",
        "github.example.com",
        "--method",
        "GET",
        "user",
    )


@pytest.mark.parametrize(
    "response",
    [
        b"[]",
        b"{}",
        b'{"id":1,"login":null}',
        b'{"login":"octocat"}',
        b'{"id":0,"login":"octocat"}',
        b'{"id":1.5,"login":"octocat"}',
    ],
)
def test_current_actor_rejects_invalid_identity(response: bytes) -> None:
    runner = FakeRunner(results=[success(response)])

    with pytest.raises(GhSlateError) as caught:
        process(runner).current_actor()

    assert caught.value.code == "gh_response_invalid"


def test_resolve_actor_quotes_login_and_returns_stable_identity() -> None:
    runner = FakeRunner(results=[success(b'{"id":583231,"login":"new-octocat"}')])

    actor = process(runner).resolve_actor(
        "old/name",
        hostname="github.example.com",
    )

    assert actor == GitHubActor(id=583231, login="new-octocat")
    assert runner.calls[0][0][-1] == "users/old%2Fname"


def test_repo_view_propagates_hostname_without_an_unsupported_flag() -> None:
    runner = FakeRunner(
        results=[success(b'{"nameWithOwner":"owner/repo","url":"https://github.example/owner/repo","isPrivate":false}')]
    )

    result = process(runner).repo_view("github.example.com")

    assert cast("object", result)
    assert runner.calls[0] == (
        (
            "gh",
            "repo",
            "view",
            "--json",
            "nameWithOwner,url,isPrivate",
        ),
        30.0,
        "github.example.com",
        32 * 1024 * 1024,
        64 * 1024,
    )


def test_pr_view_qualifies_repository_with_hostname() -> None:
    runner = FakeRunner(results=[success(b'{"number":42}')])

    process(runner).pr_view(
        repository="owner/repo",
        hostname="github.example.com",
    )

    assert runner.calls[0][0] == (
        "gh",
        "pr",
        "view",
        "--repo",
        "github.example.com/owner/repo",
        "--json",
        "number,url,headRefName,headRefOid,baseRefName,baseRefOid,state",
    )
    assert runner.calls[0][2] == "github.example.com"


def test_auth_status_propagates_hostname_and_never_requests_a_token() -> None:
    runner = FakeRunner(results=[success(b"github.example.com\n")])

    assert process(runner).auth_status("github.example.com") is None

    assert runner.calls[0][0] == (
        "gh",
        "auth",
        "status",
        "--active",
        "--hostname",
        "github.example.com",
    )
    assert "--json" not in runner.calls[0][0]
    assert "--show-token" not in runner.calls[0][0]


def test_auth_status_propagates_nonzero_status_without_parsing_output() -> None:
    runner = FakeRunner(
        results=[
            ProcessResult(
                returncode=1,
                stdout=b"github.com\n",
                stderr=b"not logged in\n",
            )
        ]
    )

    with pytest.raises(GhSlateError) as caught:
        process(runner).auth_status()

    assert caught.value.code == "gh_command_failed"
    assert caught.value.details["returncode"] == 1


def test_version_is_strict_nonempty_utf8_text() -> None:
    runner = FakeRunner(results=[success(b"gh version 2.76.2\n")])

    assert process(runner).version() == "gh version 2.76.2"
    assert runner.calls[0][0] == ("gh", "version")


def test_default_runner_never_uses_a_shell_and_inherits_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("GH_TOKEN", "inherited-token")

    class CompletedProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"{}")
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout: float | None = None) -> int:
            captured["timeout"] = timeout
            return 0

        def kill(self) -> None:
            captured["killed"] = True

    def complete(argv: tuple[str, ...], **kwargs: object) -> CompletedProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return CompletedProcess()

    monkeypatch.setattr(subprocess, "Popen", complete)

    result = SubprocessRunner().run(
        ("gh", "api", "--method", "GET", "user"),
        timeout=1.5,
        hostname="github.example.com",
        max_stdout_bytes=1024,
        max_stderr_bytes=512,
    )

    assert result == ProcessResult(0, b"{}", b"")
    assert captured["argv"] == ("gh", "api", "--method", "GET", "user")
    assert captured["shell"] is False
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["stdout"] == subprocess.PIPE
    assert captured["stderr"] == subprocess.PIPE
    assert 0 < cast("float", captured["timeout"]) <= 1.5
    assert captured["start_new_session"] is (os.name != "nt")
    assert captured["creationflags"] == (subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    environment = cast("dict[str, str]", captured["env"])
    assert environment["HTTPS_PROXY"] == "http://proxy.example"
    assert environment["GH_TOKEN"] == "inherited-token"
    assert environment["GH_HOST"] == "github.example.com"
    assert os.environ["GH_TOKEN"] == "inherited-token"


def test_default_runner_uses_implicit_environment_without_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CompletedProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"{}")
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    def complete(argv: tuple[str, ...], **kwargs: object) -> CompletedProcess:
        captured.update(kwargs)
        return CompletedProcess()

    monkeypatch.setattr(subprocess, "Popen", complete)

    SubprocessRunner().run(
        ("gh", "version"),
        timeout=1,
        hostname=None,
        max_stdout_bytes=1024,
        max_stderr_bytes=512,
    )

    assert captured["env"] is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups are unavailable on Windows")
def test_process_tree_kill_uses_a_posix_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, signal.Signals]] = []

    class Process:
        pid = 123
        killed = False

        def kill(self) -> None:
            self.killed = True

    process = Process()
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))

    _kill_process_tree(cast("subprocess.Popen[bytes]", process), platform="posix")

    assert calls == [(123, signal.SIGKILL)]
    assert process.killed is True


def test_process_tree_kill_uses_absolute_windows_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Process:
        pid = 456
        killed = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

    def run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    process = Process()
    monkeypatch.setattr(subprocess, "run", run)

    _kill_process_tree(
        cast("subprocess.Popen[bytes]", process),
        platform="nt",
        environment={"SystemRoot": r"C:\Windows"},
    )

    assert captured["command"] == (
        r"C:\Windows\System32\taskkill.exe",
        "/PID",
        "456",
        "/T",
        "/F",
    )
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert captured["timeout"] == 1.0
    assert captured["check"] is False
    assert process.killed is True


def test_process_tree_kill_skips_windows_taskkill_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 456
        killed = False

        def poll(self) -> int:
            return 0

        def kill(self) -> None:
            self.killed = True

    process = Process()

    def unexpected_run(*args: object, **kwargs: object) -> None:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    _kill_process_tree(
        cast("subprocess.Popen[bytes]", process),
        platform="nt",
        environment={"SystemRoot": r"C:\Windows"},
    )

    assert process.killed is True


@pytest.mark.parametrize(
    ("descriptor", "stream"),
    [(1, "stdout"), (2, "stderr")],
)
def test_default_runner_kills_process_as_soon_as_a_stream_exceeds_its_limit(
    descriptor: int,
    stream: str,
) -> None:
    started = time.monotonic()

    result = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            (f"import os,time;os.write({descriptor},b'x'*10485760);time.sleep(10)"),
        ),
        timeout=5,
        hostname=None,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )

    assert len(getattr(result, stream)) == 1025
    assert time.monotonic() - started < 3


def test_default_runner_timeout_terminates_children_that_inherit_output_pipes() -> None:
    parent = (
        "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)']);time.sleep(10)"
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessRunner().run(
            (sys.executable, "-c", parent),
            timeout=0.2,
            hostname=None,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )

    assert time.monotonic() - started < 3


def test_default_runner_bounds_reader_joins_after_parent_exits() -> None:
    parent = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])"
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessRunner().run(
            (sys.executable, "-c", parent),
            timeout=0.2,
            hostname=None,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )

    assert time.monotonic() - started < 3


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (ProcessResult(0, b"\xff", b""), "gh_output_invalid"),
        (ProcessResult(0, b'{"a":1,"a":2}', b""), "gh_json_invalid"),
        (ProcessResult(0, b"not json", b""), "gh_json_invalid"),
        (ProcessResult(0, b"{}", b"\xff"), "gh_output_invalid"),
    ],
)
def test_invalid_utf8_or_json_has_a_stable_error(
    result: ProcessResult,
    code: str,
) -> None:
    runner = FakeRunner(results=[result])

    with pytest.raises(GhSlateError) as caught:
        process(runner).api_get("user")

    assert caught.value.code == code


def test_stdout_and_stderr_limits_are_enforced_before_parsing() -> None:
    stdout_runner = FakeRunner(results=[success(b"12345")])
    with pytest.raises(GhSlateError) as stdout_error:
        process(stdout_runner, max_stdout_bytes=4).api_get("user")
    assert stdout_error.value.code == "gh_output_limit"
    assert stdout_error.value.details["stream"] == "stdout"

    stderr_runner = FakeRunner(results=[ProcessResult(returncode=0, stdout=b"{}", stderr=b"12345")])
    with pytest.raises(GhSlateError) as stderr_error:
        process(stderr_runner, max_stderr_bytes=4).api_get("user")
    assert stderr_error.value.code == "gh_output_limit"
    assert stderr_error.value.details["stream"] == "stderr"


def test_nonzero_exit_is_wrapped_without_parsing_stdout() -> None:
    runner = FakeRunner(
        results=[
            ProcessResult(
                returncode=1,
                stdout=b"not json",
                stderr=b"HTTP 404: Not Found\n",
            )
        ]
    )

    with pytest.raises(GhSlateError) as caught:
        process(runner).api_get("repos/owner/missing")

    assert caught.value.code == "gh_command_failed"
    assert caught.value.details["returncode"] == 1
    assert caught.value.details["stderr"] == "HTTP 404: Not Found"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            subprocess.TimeoutExpired(cmd=("gh", "api"), timeout=1),
            "gh_timeout",
        ),
        (FileNotFoundError(), "gh_not_found"),
        (PermissionError(), "gh_process_error"),
    ],
)
def test_process_start_failures_are_wrapped(
    error: BaseException,
    code: str,
) -> None:
    runner = FakeRunner(error=error)

    with pytest.raises(GhSlateError) as caught:
        process(runner).api_get("user")

    assert caught.value.code == code


@pytest.mark.parametrize(
    "hostname",
    ["", "--hostname", "https://github.example", "github.example/path", "bad host"],
)
def test_invalid_hostname_never_reaches_the_runner(hostname: str) -> None:
    runner = FakeRunner()

    with pytest.raises(GhSlateError) as caught:
        process(runner).api_get("user", hostname=hostname)

    assert caught.value.code == "gh_hostname_invalid"
    assert runner.calls == []


def test_repository_hostname_mismatch_never_reaches_the_runner() -> None:
    runner = FakeRunner()

    with pytest.raises(GhSlateError) as caught:
        process(runner).pr_view(
            repository="github.example/owner/repo",
            hostname="other.example",
        )

    assert caught.value.code == "gh_hostname_mismatch"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0),
        ("max_stdout_bytes", True),
        ("max_stderr_bytes", -1),
    ],
)
def test_limits_require_positive_values(field: str, value: object) -> None:
    arguments: dict[str, object] = {
        "timeout_seconds": 1,
        "max_stdout_bytes": 1,
        "max_stderr_bytes": 1,
    }
    arguments[field] = value

    with pytest.raises(ValueError, match=field):
        type(DEFAULT_GH_PROCESS_LIMITS)(**arguments)  # type: ignore[arg-type]
