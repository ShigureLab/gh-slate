from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Protocol, cast

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import JsonLimits, JsonValue, strict_loads
from gh_slate.errors import GhSlateError


@dataclass(frozen=True, slots=True)
class GhProcessLimits:
    timeout_seconds: float = 30.0
    max_stdout_bytes: int = 32 * 1024 * 1024
    max_stderr_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{item.name} must be a positive number")
        for name in ("max_stdout_bytes", "max_stderr_bytes"):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_GH_PROCESS_LIMITS = GhProcessLimits()


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        hostname: str | None,
    ) -> ProcessResult: ...


@dataclass(frozen=True, slots=True)
class SubprocessRunner:
    """Execute an argv directly while leaving authentication to ``gh``."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        hostname: str | None,
    ) -> ProcessResult:
        environment: dict[str, str] | None = None
        if hostname is not None:
            environment = os.environ.copy()
            environment["GH_HOST"] = hostname
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout,
            env=environment,
        )
        return ProcessResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _error(message: str, *, code: str, **details: object) -> GhSlateError:
    return GhSlateError(message, code=code, details=details)


def _validate_hostname(hostname: str | None) -> str | None:
    if hostname is None:
        return None
    if (
        not isinstance(hostname, str)
        or not hostname
        or hostname.startswith("-")
        or any(character.isspace() or character in "/\\\0" for character in hostname)
    ):
        raise _error(
            "GitHub hostname must be a bare hostname",
            code="gh_hostname_invalid",
        )
    return hostname


def _validate_argument(value: object, *, subject: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise _error(
            f"{subject} must be non-empty text without NUL bytes",
            code="gh_argument_invalid",
            subject=subject,
        )
    return value


def _api_method(arguments: tuple[str, ...]) -> str:
    methods: list[str] = []
    endpoints = 0
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--method", "-X"}:
            if index + 1 >= len(arguments):
                raise _error(
                    "gh api method flag is missing its value",
                    code="gh_argument_invalid",
                )
            methods.append(arguments[index + 1])
            index += 2
            continue
        if argument.startswith("--method="):
            methods.append(argument.partition("=")[2])
            index += 1
            continue
        if argument.startswith("-X") and argument != "-X":
            methods.append(argument[2:])
            index += 1
            continue
        if argument == "--hostname":
            if index + 1 >= len(arguments):
                raise _error(
                    "gh api hostname flag is missing its value",
                    code="gh_argument_invalid",
                )
            _validate_hostname(arguments[index + 1])
            index += 2
            continue
        if argument.startswith("--hostname="):
            _validate_hostname(argument.partition("=")[2])
            index += 1
            continue
        if argument in {"--paginate", "--slurp"}:
            index += 1
            continue
        if argument.startswith("-"):
            raise _error(
                "gh api option is not allowed by the read-only adapter",
                code="gh_command_forbidden",
                option=argument.partition("=")[0],
            )
        endpoints += 1
        index += 1

    if endpoints != 1:
        raise _error(
            "gh api requires exactly one endpoint",
            code="gh_argument_invalid",
            endpoint_count=endpoints,
        )
    if len(methods) != 1:
        raise _error(
            "gh api must declare exactly one explicit GET method",
            code="gh_argument_invalid",
        )
    method = methods[0].upper()
    if method != "GET":
        raise _error(
            "the read-only GitHub adapter only permits GET requests",
            code="gh_api_method_forbidden",
            method=method,
        )
    return method


def _validate_read_only(arguments: tuple[str, ...]) -> None:
    if not arguments:
        raise _error("gh command must not be empty", code="gh_argument_invalid")

    if arguments[0] == "api":
        _api_method(arguments)
        return

    if len(arguments) >= 2 and arguments[:2] in {
        ("auth", "status"),
        ("pr", "view"),
        ("repo", "view"),
    }:
        if any(argument == "-w" or argument == "--web" or argument.startswith("--web=") for argument in arguments):
            raise _error(
                "browser-opening gh options are disabled",
                code="gh_command_forbidden",
            )
        exposes_token = arguments[:2] == ("auth", "status") and any(
            argument == "-t" or argument == "--show-token" or argument.startswith("--show-token=")
            for argument in arguments
        )
        if exposes_token:
            raise _error(
                "gh auth token output is disabled",
                code="gh_command_forbidden",
            )
        return

    raise _error(
        "gh command is not in the read-only allowlist",
        code="gh_command_forbidden",
        command=arguments[0],
    )


def _with_hostname(
    arguments: tuple[str, ...],
    hostname: str | None,
) -> tuple[str, ...]:
    validated = _validate_hostname(hostname)
    if validated is None:
        return arguments
    if arguments[0] == "api":
        return (arguments[0], "--hostname", validated, *arguments[1:])
    if arguments[:2] == ("auth", "status"):
        return (*arguments, "--hostname", validated)
    return arguments


def _qualify_repository(repository: str, hostname: str | None) -> str:
    value = _validate_argument(repository, subject="repository")
    if value.startswith("-"):
        raise _error(
            "repository must not start with a command-line option prefix",
            code="gh_argument_invalid",
            subject="repository",
        )
    parts = value.split("/")
    if len(parts) == 2:
        return value if hostname is None else f"{hostname}/{value}"
    if len(parts) == 3:
        if hostname is not None and parts[0] != hostname:
            raise _error(
                "repository hostname disagrees with the requested hostname",
                code="gh_hostname_mismatch",
            )
        return value
    raise _error(
        "repository must be OWNER/REPO or HOST/OWNER/REPO",
        code="gh_argument_invalid",
        subject="repository",
    )


@dataclass(frozen=True, slots=True)
class GhProcess:
    runner: ProcessRunner = field(default_factory=SubprocessRunner)
    executable: str = "gh"
    limits: GhProcessLimits = DEFAULT_GH_PROCESS_LIMITS

    def __post_init__(self) -> None:
        _validate_argument(self.executable, subject="gh executable")

    def _invoke(
        self,
        arguments: tuple[str, ...],
        *,
        hostname: str | None,
    ) -> tuple[bytes, str]:
        argv = (self.executable, *arguments)
        try:
            completed = self.runner.run(
                argv,
                timeout=self.limits.timeout_seconds,
                hostname=hostname,
            )
        except subprocess.TimeoutExpired:
            raise _error(
                "GitHub CLI command timed out",
                code="gh_timeout",
                timeout_seconds=self.limits.timeout_seconds,
            ) from None
        except FileNotFoundError:
            raise _error(
                "GitHub CLI executable was not found",
                code="gh_not_found",
                executable=self.executable,
            ) from None
        except OSError as error:
            raise _error(
                "GitHub CLI process could not be started",
                code="gh_process_error",
                error_type=type(error).__name__,
            ) from None

        if (
            isinstance(completed.returncode, bool)
            or not isinstance(completed.returncode, int)
            or not isinstance(completed.stdout, bytes)
            or not isinstance(completed.stderr, bytes)
        ):
            raise _error(
                "GitHub CLI runner returned an invalid result",
                code="gh_runner_protocol",
            )
        if len(completed.stdout) > self.limits.max_stdout_bytes:
            raise _error(
                "GitHub CLI stdout exceeds the configured byte limit",
                code="gh_output_limit",
                stream="stdout",
                actual_bytes=len(completed.stdout),
                max_bytes=self.limits.max_stdout_bytes,
            )
        if len(completed.stderr) > self.limits.max_stderr_bytes:
            raise _error(
                "GitHub CLI stderr exceeds the configured byte limit",
                code="gh_output_limit",
                stream="stderr",
                actual_bytes=len(completed.stderr),
                max_bytes=self.limits.max_stderr_bytes,
            )
        try:
            stderr = completed.stderr.decode("utf-8", errors="strict")
            completed.stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            stream = "stderr" if error.object is completed.stderr else "stdout"
            raise _error(
                "GitHub CLI output is not valid UTF-8",
                code="gh_output_invalid",
                stream=stream,
            ) from None

        if completed.returncode != 0:
            rendered_stderr = stderr.strip()
            stderr_truncated = len(rendered_stderr) > 4096
            raise _error(
                "GitHub CLI command failed",
                code="gh_command_failed",
                returncode=completed.returncode,
                stderr=rendered_stderr[:4096],
                stderr_truncated=stderr_truncated,
            )
        return completed.stdout, stderr

    def run_json(
        self,
        arguments: tuple[str, ...],
        *,
        hostname: str | None = None,
    ) -> JsonValue:
        normalized = tuple(_validate_argument(argument, subject="gh argument") for argument in arguments)
        normalized = _with_hostname(normalized, hostname)
        _validate_read_only(normalized)
        stdout, _stderr = self._invoke(normalized, hostname=hostname)
        try:
            return strict_loads(
                stdout,
                limits=JsonLimits(
                    max_input_bytes=self.limits.max_stdout_bytes,
                    max_depth=64,
                    max_nodes=1_000_000,
                    max_string_bytes=self.limits.max_stdout_bytes,
                    max_number_chars=1024,
                    max_key_bytes=64 * 1024,
                ),
            )
        except CodecError:
            raise _error(
                "GitHub CLI returned invalid JSON",
                code="gh_json_invalid",
            ) from None

    def api_get(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
        paginate: bool = False,
    ) -> JsonValue:
        value = _validate_argument(endpoint, subject="GitHub API endpoint")
        if value.startswith("-"):
            raise _error(
                "GitHub API endpoint must not start with an option prefix",
                code="gh_argument_invalid",
                subject="GitHub API endpoint",
            )
        if not isinstance(paginate, bool):
            raise _error(
                "paginate must be a boolean",
                code="gh_argument_invalid",
                subject="paginate",
            )
        pagination = ("--paginate", "--slurp") if paginate else ()
        return self.run_json(
            ("api", "--method", "GET", *pagination, value),
            hostname=hostname,
        )

    def current_actor(self, hostname: str | None = None) -> str:
        value = self.api_get("user", hostname=hostname)
        if not isinstance(value, Mapping):
            raise _error(
                "GitHub user response must be an object",
                code="gh_response_invalid",
            )
        login = cast("Mapping[str, object]", value).get("login")
        if not isinstance(login, str) or not login:
            raise _error(
                "GitHub user response is missing login",
                code="gh_response_invalid",
            )
        return login

    def repo_view(self, hostname: str | None = None) -> JsonValue:
        return self.run_json(
            (
                "repo",
                "view",
                "--json",
                "nameWithOwner,url,isPrivate",
            ),
            hostname=hostname,
        )

    def pr_view(
        self,
        repository: str | None = None,
        hostname: str | None = None,
    ) -> JsonValue:
        repository_arguments: tuple[str, ...] = ()
        if repository is not None:
            repository_arguments = (
                "--repo",
                _qualify_repository(repository, _validate_hostname(hostname)),
            )
        return self.run_json(
            (
                "pr",
                "view",
                *repository_arguments,
                "--json",
                "number,url,headRefName,headRefOid,baseRefName,baseRefOid,state",
            ),
            hostname=hostname,
        )

    def auth_status(self, hostname: str | None = None) -> JsonValue:
        return self.run_json(
            ("auth", "status", "--json", "hosts"),
            hostname=hostname,
        )

    def version(self) -> str:
        stdout, _stderr = self._invoke(("version",), hostname=None)
        try:
            value = stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            raise _error(
                "GitHub CLI output is not valid UTF-8",
                code="gh_output_invalid",
                stream="stdout",
            ) from None
        if not value:
            raise _error(
                "GitHub CLI returned an empty version",
                code="gh_output_invalid",
                stream="stdout",
            )
        return value


__all__ = [
    "DEFAULT_GH_PROCESS_LIMITS",
    "GhProcess",
    "GhProcessLimits",
    "ProcessResult",
    "ProcessRunner",
    "SubprocessRunner",
]
