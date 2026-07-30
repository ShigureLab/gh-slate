from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
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


def select_editor(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Resolve an editor command without invoking a shell."""

    environment = os.environ if environ is None else environ
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
            argv = tuple(shlex.split(value, posix=True))
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

    with tempfile.TemporaryDirectory(prefix="gh-slate-edit-") as directory:
        path = Path(directory) / "data.json"
        path.write_bytes(initial)
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
