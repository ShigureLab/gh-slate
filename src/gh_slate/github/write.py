from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import BinaryIO, Protocol

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import (
    JsonLimits,
    JsonValue,
    canonical_json_bytes,
    strict_loads,
)
from gh_slate.errors import GhSlateError
from gh_slate.github.process import (
    _PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ProcessResult,
    _kill_process_tree,
    _ProcessTree,
    _spawn_process,
)

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_REPOSITORY_SEGMENT = r"[A-Za-z0-9_.-]+"
_POST_COMMENT_ENDPOINT = re.compile(
    rf"\Arepos/{_REPOSITORY_SEGMENT}/{_REPOSITORY_SEGMENT}"
    r"/issues/[1-9][0-9]*/comments\Z"
)
_PATCH_COMMENT_ENDPOINT = re.compile(
    rf"\Arepos/{_REPOSITORY_SEGMENT}/{_REPOSITORY_SEGMENT}"
    r"/issues/comments/[1-9][0-9]*\Z"
)
_ALLOWED_METHODS = frozenset({"PATCH", "POST"})


@dataclass(frozen=True, slots=True)
class GhWriteLimits:
    timeout_seconds: float = 30.0
    # A codec-valid 64 KiB comment body can expand substantially when JSON
    # escapes control characters. Keep this request envelope bounded while
    # allowing every body admitted by the codec.
    max_input_bytes: int = 512 * 1024
    max_stdout_bytes: int = 8 * 1024 * 1024
    max_stderr_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{item.name} must be a positive number")
        for name in (
            "max_input_bytes",
            "max_stdout_bytes",
            "max_stderr_bytes",
        ):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_GH_WRITE_LIMITS = GhWriteLimits()


class GhWriteOutcomeUnknown(GhSlateError):
    """The write process started, so callers must recover by reading."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details={} if details is None else details,
        )


class GhWriteTimeout(GhWriteOutcomeUnknown):
    """A write may have reached GitHub; callers must recover by reading."""

    def __init__(self, *, timeout_seconds: float) -> None:
        super().__init__(
            "GitHub CLI write timed out; the remote outcome is unknown",
            code="gh_write_timeout",
            details={"timeout_seconds": timeout_seconds},
        )


class WriteProcessRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes,
        timeout: float,
        hostname: str | None,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult: ...


class _WriteProcessStartedError(Exception):
    """An internal pipe/process failure after a write process was started."""

    def __init__(self, error_type: str) -> None:
        super().__init__("GitHub CLI write process failed after starting")
        self.error_type = error_type


def _run_started_write_process(
    process: subprocess.Popen[bytes],
    process_tree: _ProcessTree | None,
    *,
    argv: tuple[str, ...],
    stdin: bytes,
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> ProcessResult:
    if process.stdin is None or process.stdout is None or process.stderr is None:  # pragma: no cover
        _kill_process_tree(process, process_tree=process_tree)
        process.wait()
        raise _WriteProcessStartedError("PipeUnavailable")

    stdout = bytearray()
    stderr = bytearray()
    thread_errors: list[BaseException] = []
    kill_lock = threading.Lock()
    output_limit_reached = threading.Event()

    def kill() -> None:
        with kill_lock:
            if process_tree is None and getattr(process, "pid", None) is None:
                process.kill()
            else:
                _kill_process_tree(process, process_tree=process_tree)

    def reap() -> None:
        try:
            process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def join_workers(deadline: float, *, suppress_errors: bool = False) -> bool:
        for worker in workers:
            if worker.ident is None:
                continue
            try:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            except BaseException:
                if not suppress_errors:
                    raise
        return not any(worker.ident is not None and worker.is_alive() for worker in workers)

    def cleanup() -> None:
        deadline = time.monotonic() + _PROCESS_CLEANUP_TIMEOUT_SECONDS
        kill()
        reap()
        join_workers(deadline)

    def cleanup_suppressing_errors() -> None:
        deadline = time.monotonic() + _PROCESS_CLEANUP_TIMEOUT_SECONDS
        try:
            kill()
        except BaseException:
            pass
        try:
            reap()
        except BaseException:
            pass
        join_workers(deadline, suppress_errors=True)

    def read_bounded(
        pipe: BinaryIO,
        buffer: bytearray,
        limit: int,
    ) -> None:
        try:
            while True:
                remaining = limit + 1 - len(buffer)
                if remaining <= 0:
                    output_limit_reached.set()
                    kill()
                    return
                chunk = pipe.read(min(64 * 1024, remaining))
                if not chunk:
                    return
                buffer.extend(chunk)
                if len(buffer) > limit:
                    output_limit_reached.set()
                    kill()
                    return
        except BaseException as error:
            thread_errors.append(error)
            kill()
        finally:
            pipe.close()

    def write_stdin(pipe: BinaryIO) -> None:
        view = memoryview(stdin)
        try:
            while view:
                written = pipe.write(view)
                if written is None or written <= 0:
                    raise OSError("failed to write GitHub CLI stdin")
                view = view[written:]
            pipe.flush()
        except BrokenPipeError:
            pass
        except BaseException as error:
            if not output_limit_reached.is_set():
                thread_errors.append(error)
            kill()
        finally:
            pipe.close()

    workers = (
        threading.Thread(
            target=write_stdin,
            args=(process.stdin,),
            daemon=True,
        ),
        threading.Thread(
            target=read_bounded,
            args=(process.stdout, stdout, max_stdout_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=read_bounded,
            args=(process.stderr, stderr, max_stderr_bytes),
            daemon=True,
        ),
    )
    deadline = time.monotonic() + timeout
    try:
        for worker in workers:
            worker.start()
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        if not join_workers(deadline):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            cleanup()
        except BaseException as error:
            cleanup_suppressing_errors()
            raise _WriteProcessStartedError(type(error).__name__) from error
        raise
    except BaseException as error:
        cleanup_suppressing_errors()
        raise _WriteProcessStartedError(type(error).__name__) from error

    if thread_errors and not output_limit_reached.is_set():
        raise _WriteProcessStartedError(type(thread_errors[0]).__name__) from thread_errors[0]
    stderr_bytes = bytes(stderr)
    if process_tree is not None:
        launch_error = process_tree.launch_error(stderr_bytes)
        if launch_error is not None:
            raise launch_error
    return ProcessResult(
        returncode=returncode,
        stdout=bytes(stdout),
        stderr=stderr_bytes,
    )


@dataclass(frozen=True, slots=True)
class SubprocessWriteRunner:
    """Pass one canonical request body to ``gh`` without invoking a shell."""

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
        environment: dict[str, str] | None = None
        if hostname is not None:
            environment = os.environ.copy()
            environment["GH_HOST"] = hostname
        process, process_tree = _spawn_process(
            argv,
            environment=environment,
            stdin=subprocess.PIPE,
        )
        try:
            return _run_started_write_process(
                process,
                process_tree,
                argv=argv,
                stdin=stdin,
                timeout=timeout,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
            )
        finally:
            if process_tree is not None:
                process_tree.close()


def _error(message: str, *, code: str, **details: object) -> GhSlateError:
    return GhSlateError(message, code=code, details=details)


def _unknown(
    message: str,
    *,
    code: str,
    **details: object,
) -> GhWriteOutcomeUnknown:
    return GhWriteOutcomeUnknown(
        message,
        code=code,
        details=details,
    )


def _hostname(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 255:
        raise _error(
            "GitHub hostname must be a bare hostname",
            code="gh_hostname_invalid",
        )
    host, separator, port_text = value.rpartition(":")
    if not separator:
        host = value
    elif not port_text.isascii() or not port_text.isdecimal() or not 1 <= int(port_text) <= 65535:
        raise _error(
            "GitHub hostname must use a valid TCP port",
            code="gh_hostname_invalid",
        )
    labels = host.split(".")
    if not labels or any(_HOST_LABEL.fullmatch(label) is None for label in labels):
        raise _error(
            "GitHub hostname must be a bare hostname",
            code="gh_hostname_invalid",
        )
    return value


def _endpoint(method: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise _error(
            "GitHub API endpoint is invalid",
            code="gh_write_endpoint_invalid",
        )
    pattern = _POST_COMMENT_ENDPOINT if method == "POST" else _PATCH_COMMENT_ENDPOINT
    if pattern.fullmatch(value) is None:
        raise _error(
            "GitHub API write adapter only permits the matching issue-comment route",
            code="gh_write_endpoint_invalid",
        )
    return value


def _method(value: object) -> str:
    if not isinstance(value, str):
        raise _error(
            "GitHub API write method is invalid",
            code="gh_write_method_forbidden",
        )
    normalized = value.upper()
    if normalized not in _ALLOWED_METHODS:
        raise _error(
            "GitHub API write adapter only permits POST and PATCH",
            code="gh_write_method_forbidden",
        )
    return normalized


@dataclass(frozen=True, slots=True)
class GhWriteProcess:
    runner: WriteProcessRunner = field(default_factory=SubprocessWriteRunner)
    executable: str = "gh"
    limits: GhWriteLimits = DEFAULT_GH_WRITE_LIMITS

    def __post_init__(self) -> None:
        if not isinstance(self.executable, str) or not self.executable or "\0" in self.executable:
            raise _error(
                "GitHub CLI executable is invalid",
                code="gh_argument_invalid",
            )

    def _request(
        self,
        method: object,
        endpoint: object,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> JsonValue:
        request_method = _method(method)
        request_endpoint = _endpoint(request_method, endpoint)
        request_hostname = _hostname(hostname)
        if (
            not isinstance(payload, Mapping)
            or len(payload) != 1
            or "body" not in payload
            or not isinstance(payload["body"], str)
        ):
            raise _error(
                "GitHub API write payload must contain exactly one string body",
                code="gh_write_payload_invalid",
            )
        try:
            request_body = canonical_json_bytes(payload)
        except CodecError:
            raise _error(
                "GitHub API write payload is not canonical JSON data",
                code="gh_write_payload_invalid",
            ) from None
        if len(request_body) > self.limits.max_input_bytes:
            raise _error(
                "GitHub API write payload exceeds the configured byte limit",
                code="gh_write_payload_limit",
                actual_bytes=len(request_body),
                max_bytes=self.limits.max_input_bytes,
            )

        hostname_arguments = ("--hostname", request_hostname) if request_hostname is not None else ()
        argv = (
            self.executable,
            "api",
            *hostname_arguments,
            "--method",
            request_method,
            "--input",
            "-",
            request_endpoint,
        )
        try:
            result = self.runner.run(
                argv,
                stdin=request_body,
                timeout=self.limits.timeout_seconds,
                hostname=request_hostname,
                max_stdout_bytes=self.limits.max_stdout_bytes,
                max_stderr_bytes=self.limits.max_stderr_bytes,
            )
        except subprocess.TimeoutExpired:
            raise GhWriteTimeout(
                timeout_seconds=self.limits.timeout_seconds,
            ) from None
        except _WriteProcessStartedError as error:
            raise _unknown(
                "GitHub CLI write process failed after starting; the remote outcome is unknown",
                code="gh_write_process_error",
                error_type=error.error_type,
            ) from None
        except FileNotFoundError:
            raise _error(
                "GitHub CLI executable was not found",
                code="gh_not_found",
            ) from None
        except OSError as error:
            raise _error(
                "GitHub CLI write process could not be started",
                code="gh_write_process_error",
                error_type=type(error).__name__,
            ) from None

        if (
            isinstance(result.returncode, bool)
            or not isinstance(result.returncode, int)
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
        ):
            raise _unknown(
                "GitHub CLI write runner returned an invalid result",
                code="gh_write_runner_protocol",
            )
        if len(result.stdout) > self.limits.max_stdout_bytes:
            raise _unknown(
                "GitHub CLI write stdout exceeds the configured byte limit",
                code="gh_write_output_limit",
                stream="stdout",
                actual_bytes=len(result.stdout),
                max_bytes=self.limits.max_stdout_bytes,
            )
        if len(result.stderr) > self.limits.max_stderr_bytes:
            raise _unknown(
                "GitHub CLI write stderr exceeds the configured byte limit",
                code="gh_write_output_limit",
                stream="stderr",
                actual_bytes=len(result.stderr),
                max_bytes=self.limits.max_stderr_bytes,
            )
        try:
            result.stdout.decode("utf-8", errors="strict")
            result.stderr.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            stream = "stderr" if error.object is result.stderr else "stdout"
            raise _unknown(
                "GitHub CLI write output is not valid UTF-8",
                code="gh_write_output_invalid",
                stream=stream,
            ) from None

        if result.returncode != 0:
            raise _unknown(
                "GitHub CLI write failed",
                code="gh_write_failed",
                returncode=result.returncode,
                stderr_bytes=len(result.stderr),
            )
        try:
            return strict_loads(
                result.stdout,
                limits=JsonLimits(
                    max_input_bytes=self.limits.max_stdout_bytes,
                    max_depth=64,
                    max_nodes=250_000,
                    max_string_bytes=self.limits.max_stdout_bytes,
                    max_number_chars=1024,
                    max_key_bytes=64 * 1024,
                ),
            )
        except CodecError:
            raise _unknown(
                "GitHub CLI write returned invalid JSON",
                code="gh_write_json_invalid",
            ) from None
        except (KeyboardInterrupt, SystemExit) as error:
            raise _unknown(
                "GitHub CLI write response parsing was interrupted; the remote outcome is unknown",
                code="gh_write_response_interrupted",
                error_type=type(error).__name__,
            ) from None

    def post(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> JsonValue:
        return self._request(
            "POST",
            endpoint,
            payload,
            hostname=hostname,
        )

    def patch(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> JsonValue:
        return self._request(
            "PATCH",
            endpoint,
            payload,
            hostname=hostname,
        )


__all__ = [
    "DEFAULT_GH_WRITE_LIMITS",
    "GhWriteLimits",
    "GhWriteOutcomeUnknown",
    "GhWriteProcess",
    "GhWriteTimeout",
    "SubprocessWriteRunner",
    "WriteProcessRunner",
]
