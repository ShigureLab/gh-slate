from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from gh_slate.codec.json import DEFAULT_JSON_LIMITS, JsonValue, strict_loads
from gh_slate.data.errors import DataError
from gh_slate.data.output import pretty_json
from gh_slate.errors import GhSlateError

from .input import DEFAULT_DATA_INPUT_BYTES

EDITOR_ENVIRONMENT_ORDER = ("GH_EDITOR", "GIT_EDITOR", "VISUAL", "EDITOR")


class EditorRunner(Protocol):
    def __call__(self, argv: Sequence[str], /) -> int: ...


ValidationCallback = Callable[[JsonValue], object]
RetryCallback = Callable[[GhSlateError, int], bool]


def _editor_error(message: str, *, code: str, **details: object) -> DataError:
    return DataError(message, code=code, details=details)


def _split_windows_command_line(value: str) -> tuple[str, ...]:
    """Split one trusted Windows command line without shell expansion.

    This follows the backslash-before-quote rules used by the Microsoft C
    runtime and ``subprocess.list2cmdline``. Ordinary backslashes are data, so
    unquoted paths such as ``C:\\Windows\\notepad.exe`` remain intact.
    """

    arguments: list[str] = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index] in " \t":
            index += 1
        if index == len(value):
            break

        argument: list[str] = []
        quoted = False
        while index < len(value):
            character = value[index]
            if character in " \t" and not quoted:
                break
            if character == "\\":
                start = index
                while index < len(value) and value[index] == "\\":
                    index += 1
                backslashes = index - start
                if index < len(value) and value[index] == '"':
                    argument.extend("\\" * (backslashes // 2))
                    if backslashes % 2:
                        argument.append('"')
                    elif quoted and index + 1 < len(value) and value[index + 1] == '"':
                        argument.append('"')
                        index += 1
                    else:
                        quoted = not quoted
                    index += 1
                    continue
                argument.extend("\\" * backslashes)
                continue
            if character == '"':
                if quoted and index + 1 < len(value) and value[index + 1] == '"':
                    argument.append('"')
                    index += 2
                    continue
                quoted = not quoted
                index += 1
                continue
            argument.append(character)
            index += 1

        if quoted:
            raise ValueError("unterminated quoted argument")
        arguments.append("".join(argument))
        while index < len(value) and value[index] in " \t":
            index += 1

    return tuple(arguments)


def _split_editor_command(value: str, *, windows: bool) -> tuple[str, ...]:
    if windows:
        return _split_windows_command_line(value)
    return tuple(shlex.split(value, posix=True))


def select_editor(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> tuple[str, ...]:
    """Resolve an editor command without invoking a shell."""

    environment = os.environ if environ is None else environ
    windows = (os.name if platform is None else platform) == "nt"
    for variable in EDITOR_ENVIRONMENT_ORDER:
        value = environment.get(variable)
        if value is None or not value.strip():
            continue
        if "\x00" in value:
            raise _editor_error(
                f"{variable} contains an invalid NUL byte",
                code="data_editor_command_invalid",
                variable=variable,
            )
        try:
            argv = _split_editor_command(value, windows=windows)
        except ValueError:
            raise _editor_error(
                f"{variable} is not a valid editor command",
                code="data_editor_command_invalid",
                variable=variable,
            ) from None
        if not argv or any(not argument or "\x00" in argument for argument in argv):
            raise _editor_error(
                f"{variable} is not a valid editor command",
                code="data_editor_command_invalid",
                variable=variable,
            )
        return argv
    raise _editor_error(
        "no editor is configured",
        code="data_editor_not_configured",
        variables=list(EDITOR_ENVIRONMENT_ORDER),
    )


def _normalize_editor(editor: Sequence[str]) -> tuple[str, ...]:
    if isinstance(editor, (str, bytes, bytearray)):
        raise _editor_error(
            "editor command must be an argument array",
            code="data_editor_command_invalid",
        )
    argv = tuple(editor)
    if not argv or any(not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv):
        raise _editor_error(
            "editor command must contain non-empty string arguments",
            code="data_editor_command_invalid",
        )
    return argv


def _default_runner(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(
            list(argv),
            check=False,
            shell=False,
        ).returncode
    except OSError as error:
        raise _editor_error(
            "editor could not be started",
            code="data_editor_start_failed",
            error_type=type(error).__name__,
        ) from None


def _read_edited_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise _editor_error(
                "edited JSON exceeds the configured input byte limit",
                code="data_input_size_limit",
                actual_bytes=size,
                max_bytes=max_bytes,
            )
        with path.open("rb") as stream:
            source = stream.read(max_bytes + 1)
    except DataError:
        raise
    except OSError as error:
        raise _editor_error(
            "edited JSON could not be read",
            code="data_input_read_failed",
            error_type=type(error).__name__,
        ) from None
    if len(source) > max_bytes:
        raise _editor_error(
            "edited JSON exceeds the configured input byte limit",
            code="data_input_size_limit",
            actual_bytes=len(source),
            max_bytes=max_bytes,
        )
    return source


@contextmanager
def _temporary_editor_file(initial: bytes) -> Iterator[Path]:
    try:
        workspace = tempfile.TemporaryDirectory(prefix="gh-slate-edit-")
    except OSError as error:
        raise _editor_error(
            "editor workspace could not be created",
            code="data_editor_workspace_failed",
            operation="create",
            error_type=type(error).__name__,
        ) from None

    try:
        path = Path(workspace.name) / "data.json"
        try:
            path.write_bytes(initial)
        except OSError as error:
            raise _editor_error(
                "initial editor JSON could not be written",
                code="data_editor_workspace_failed",
                operation="write",
                error_type=type(error).__name__,
            ) from None
        yield path
    finally:
        try:
            workspace.cleanup()
        except OSError as error:
            raise _editor_error(
                "editor workspace could not be cleaned up",
                code="data_editor_workspace_failed",
                operation="cleanup",
                error_type=type(error).__name__,
            ) from None


def edit_json(
    value: object,
    *,
    editor: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    validate: ValidationCallback | None = None,
    retry: RetryCallback | None = None,
    runner: EditorRunner | None = None,
    max_bytes: int = DEFAULT_DATA_INPUT_BYTES,
) -> JsonValue:
    """Edit typed JSON and optionally reopen it after parse/validation errors.

    ``retry(error, attempt)`` is called only after an editor run produced
    invalid JSON or failed validation. Returning true reopens the same file;
    returning false (or omitting the callback) re-raises the original error.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    argv = select_editor(environ) if editor is None else _normalize_editor(editor)
    run_editor = _default_runner if runner is None else runner
    initial = f"{pretty_json(value)}\n".encode("utf-8", errors="strict")
    if len(initial) > max_bytes:
        raise _editor_error(
            "JSON exceeds the configured editor byte limit",
            code="data_input_size_limit",
            actual_bytes=len(initial),
            max_bytes=max_bytes,
        )

    with _temporary_editor_file(initial) as path:
        attempt = 0
        while True:
            attempt += 1
            try:
                returncode = run_editor((*argv, str(path)))
            except DataError:
                raise
            except OSError as error:
                raise _editor_error(
                    "editor could not be started",
                    code="data_editor_start_failed",
                    error_type=type(error).__name__,
                ) from None
            if isinstance(returncode, bool) or not isinstance(returncode, int):
                raise _editor_error(
                    "editor runner returned an invalid status",
                    code="data_editor_failed",
                    status_type=type(returncode).__name__,
                )
            if returncode != 0:
                raise _editor_error(
                    "editor exited unsuccessfully",
                    code="data_editor_failed",
                    returncode=returncode,
                )

            try:
                parsed = strict_loads(
                    _read_edited_file(path, max_bytes=max_bytes),
                    limits=replace(DEFAULT_JSON_LIMITS, max_input_bytes=max_bytes),
                )
                if validate is not None:
                    validate(parsed)
                return parsed
            except GhSlateError as error:
                if retry is None or not retry(error, attempt):
                    raise


__all__ = [
    "EDITOR_ENVIRONMENT_ORDER",
    "EditorRunner",
    "RetryCallback",
    "ValidationCallback",
    "edit_json",
    "select_editor",
]
