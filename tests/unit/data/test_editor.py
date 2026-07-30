from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.data import DataError, edit_json, select_editor

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gh_slate.errors import GhSlateError


def test_select_editor_uses_documented_precedence_and_shell_free_argv() -> None:
    environment = {
        "EDITOR": "nano",
        "VISUAL": "vim",
        "GIT_EDITOR": "code --wait",
        "GH_EDITOR": 'editor --label "two words" "$(touch nope)"',
    }

    assert select_editor(environment) == (
        "editor",
        "--label",
        "two words",
        "$(touch nope)",
    )

    del environment["GH_EDITOR"]
    assert select_editor(environment) == ("code", "--wait")


def test_select_editor_skips_empty_values_and_rejects_bad_commands() -> None:
    assert select_editor({"GH_EDITOR": " ", "EDITOR": "vim"}) == ("vim",)

    with pytest.raises(DataError) as missing:
        select_editor({})
    assert missing.value.code == "data_editor_not_configured"

    with pytest.raises(DataError) as malformed:
        select_editor({"GH_EDITOR": '"unterminated'})
    assert malformed.value.code == "data_editor_command_invalid"

    with pytest.raises(DataError) as nul:
        select_editor({"GH_EDITOR": "vim\x00bad"})
    assert nul.value.code == "data_editor_command_invalid"


def test_edit_json_passes_an_argv_array_and_validates_result() -> None:
    observed: dict[str, object] = {}

    def runner(argv: Sequence[str]) -> int:
        arguments = tuple(argv)
        observed["argv"] = arguments
        path = Path(arguments[-1])
        observed["initial"] = path.read_text(encoding="utf-8")
        path.write_text('{"updated":true,"count":2}', encoding="utf-8")
        return 0

    validated: list[object] = []
    result = edit_json(
        {"z": 1, "a": 2},
        editor=("fake-editor", "--wait"),
        runner=runner,
        validate=validated.append,
    )

    assert cast("tuple[str, ...]", observed["argv"])[:-1] == ("fake-editor", "--wait")
    assert observed["initial"] == '{\n  "a": 2,\n  "z": 1\n}\n'
    assert result == {"updated": True, "count": 2}
    assert validated == [result]


def test_edit_json_reopens_same_file_after_parse_error_when_callback_retries() -> None:
    paths: list[str] = []
    attempts = 0

    def runner(argv: Sequence[str]) -> int:
        nonlocal attempts
        attempts += 1
        path = Path(argv[-1])
        paths.append(str(path))
        path.write_text("not-json" if attempts == 1 else '{"valid":true}', encoding="utf-8")
        return 0

    failures: list[tuple[str, int]] = []

    def retry(error: GhSlateError, attempt: int) -> bool:
        failures.append((error.code, attempt))
        return True

    result = edit_json(
        {},
        editor=("fake-editor",),
        runner=runner,
        retry=retry,
    )

    assert result == {"valid": True}
    assert attempts == 2
    assert paths[0] == paths[1]
    assert failures == [("json_invalid", 1)]


def test_edit_json_can_retry_validation_errors() -> None:
    attempts = 0

    def runner(argv: Sequence[str]) -> int:
        nonlocal attempts
        attempts += 1
        path = Path(argv[-1])
        path.write_text(
            '{"status":"bad"}' if attempts == 1 else '{"status":"good"}',
            encoding="utf-8",
        )
        return 0

    def validate(value: object) -> None:
        if value == {"status": "bad"}:
            raise DataError(
                "status is invalid",
                code="test_validation_failed",
            )

    seen: list[str] = []
    result = edit_json(
        {},
        editor=("fake-editor",),
        runner=runner,
        validate=validate,
        retry=lambda error, _attempt: not seen.append(error.code),
    )

    assert result == {"status": "good"}
    assert seen == ["test_validation_failed"]


def test_edit_json_without_retry_reraises_original_parse_error() -> None:
    def runner(argv: Sequence[str]) -> int:
        Path(argv[-1]).write_text("not-json", encoding="utf-8")
        return 0

    with pytest.raises(CodecError) as captured:
        edit_json({}, editor=("fake-editor",), runner=runner)

    assert captured.value.code == "json_invalid"


def test_edit_json_reports_editor_failure_and_size_limit() -> None:
    with pytest.raises(DataError) as failure:
        edit_json({}, editor=("fake-editor",), runner=lambda _argv: 7)
    assert failure.value.code == "data_editor_failed"
    assert failure.value.details["returncode"] == 7

    with pytest.raises(DataError) as oversized:
        edit_json({"text": "abcdef"}, editor=("fake-editor",), runner=lambda _argv: 0, max_bytes=4)
    assert oversized.value.code == "data_input_size_limit"


def test_default_editor_runner_never_enables_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed["argv"] = argv
        observed.update(kwargs)
        path = Path(argv[-1])
        path.write_text('{"ok":true}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)

    assert edit_json({}, editor=("fake-editor", "--wait")) == {"ok": True}
    assert observed["shell"] is False
    assert observed["check"] is False
    assert cast("list[str]", observed["argv"])[:2] == ["fake-editor", "--wait"]
