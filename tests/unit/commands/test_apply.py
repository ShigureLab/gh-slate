from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from gh_slate.cli import run
from gh_slate.commands import apply as apply_commands
from gh_slate.github.apply import ApplyAction, ApplyRequest, ApplyResult

if TYPE_CHECKING:
    from pathlib import Path


HOST = "github.example"
TARGET_URL = f"https://{HOST}/owner/repo/issues/42"
STATE_HASH = "a" * 64


@dataclass(slots=True)
class NoNetworkReader:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def repo_view(self, hostname: str | None = None) -> object:
        self.calls.append(("repo_view", hostname))
        raise AssertionError("a full target URL must not look up the repository")

    def pr_view(
        self,
        repository: str | None = None,
        hostname: str | None = None,
    ) -> object:
        self.calls.append(("pr_view", repository, hostname))
        raise AssertionError("a full target URL must not look up a pull request")


def _result(
    *,
    action: ApplyAction = "created",
    comment_id: int | None = 101,
    url: str | None = f"{TARGET_URL}#issuecomment-101",
    revision: int = 1,
    dry_run: bool = False,
) -> ApplyResult:
    return ApplyResult(
        action=action,
        name="ci",
        repository="owner/repo",
        number=42,
        comment_id=comment_id,
        url=url,
        revision=revision,
        state_sha256=STATE_HASH,
        markdown="# ci\n\nready\n",
        dry_run=dry_run,
    )


@dataclass(slots=True)
class FakeTransaction:
    result: ApplyResult = field(default_factory=_result)
    reader: NoNetworkReader = field(default_factory=NoNetworkReader)
    requests: list[ApplyRequest] = field(default_factory=list)

    def apply(
        self,
        request: ApplyRequest,
    ) -> ApplyResult:
        self.requests.append(request)
        return self.result


def _install_transaction(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: ApplyResult | None = None,
) -> FakeTransaction:
    transaction = FakeTransaction(
        result=_result() if result is None else result,
    )
    monkeypatch.setattr(
        apply_commands,
        "_new_transaction",
        lambda: transaction,
    )
    return transaction


def test_apply_builds_a_validated_create_transaction_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_path = tmp_path / "data.json"
    schema_path = tmp_path / "schema.json"
    template_path = tmp_path / "slate.md.j2"
    data_path.write_text('{"status":"ready"}', encoding="utf-8")
    schema_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                    }
                },
                "required": ["status"],
            }
        ),
        encoding="utf-8",
    )
    template_path.write_bytes(b"# {{ slate.name }}\r\n\r\n{{ data.status }}")
    transaction = _install_transaction(monkeypatch)

    assert (
        run(
            [
                "apply",
                "ci",
                "--target",
                TARGET_URL,
                "--mode",
                "create",
                "--controller",
                "ci-bot",
                "--data",
                str(data_path),
                "--schema",
                str(schema_path),
                "--template",
                str(template_path),
            ],
            prog="gh slate",
        )
        == 0
    )

    output = capsys.readouterr()
    assert output.out == f"created ci -> {TARGET_URL}#issuecomment-101\n"
    assert output.err == ""
    assert len(transaction.requests) == 1
    request = transaction.requests[0]
    assert request.name == "ci"
    assert request.target.host == HOST
    assert request.target.repository == "owner/repo"
    assert request.target.number == 42
    assert request.mode == "create"
    assert request.controller == "ci-bot"
    assert request.if_revision is None
    assert request.dry_run is False
    assert request.replace_schema is True
    assert isinstance(request.data, Mapping)
    assert request.data == {"status": "ready"}
    assert request.data_schema is not None
    assert request.renderer is not None
    assert request.renderer.kind == "jinja"
    assert request.renderer.version == 1
    assert request.renderer.configuration == {"source": "# {{ slate.name }}\n\n{{ data.status }}"}
    assert transaction.reader.calls == []


def test_apply_can_clear_the_stored_schema(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transaction = _install_transaction(monkeypatch)

    assert (
        run(
            [
                "apply",
                "ci",
                "--target",
                TARGET_URL,
                "--mode",
                "update",
                "--clear-schema",
            ]
        )
        == 0
    )

    request = transaction.requests[0]
    assert request.data_schema is None
    assert request.replace_schema is True
    output = capsys.readouterr()
    assert output.out == f"created ci -> {TARGET_URL}#issuecomment-101\n"
    assert output.err == ""


def test_apply_create_without_a_renderer_fails_before_transaction_setup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def unexpected_factory() -> FakeTransaction:
        nonlocal called
        called = True
        raise AssertionError("transaction setup must follow local validation")

    monkeypatch.setattr(
        apply_commands,
        "_new_transaction",
        unexpected_factory,
    )

    assert (
        run(
            [
                "apply",
                "ci",
                "--target",
                TARGET_URL,
                "--mode",
                "create",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "error[renderer_required]" in output.err
    assert called is False


@pytest.mark.parametrize(
    ("argv", "error_code"),
    [
        (
            [
                "--columns",
                "name,status",
            ],
            "renderer_option_conflict",
        ),
        (
            [
                "--template",
                "unused.md.j2",
                "--title",
                "Invalid",
            ],
            "renderer_option_conflict",
        ),
        (
            [
                "--title",
                "Invalid without a renderer",
            ],
            "renderer_option_conflict",
        ),
        (
            [
                "--data",
                "-",
                "--template",
                "-",
            ],
            "stdin_conflict",
        ),
        (
            [
                "--data",
                "-",
                "--schema",
                "-",
                "--table",
                ".jobs",
            ],
            "stdin_conflict",
        ),
    ],
)
def test_apply_input_conflicts_fail_before_any_transaction(
    argv: list[str],
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        apply_commands,
        "_new_transaction",
        lambda: pytest.fail("transaction must not be created"),
    )
    monkeypatch.setattr(sys, "stdin", object())

    assert run(["apply", "ci", "--target", TARGET_URL, *argv]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert f"error[{error_code}]" in output.err


def test_apply_reuses_bounded_template_and_strict_json_ingestion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oversized_template = tmp_path / "oversized.md.j2"
    duplicate_data = tmp_path / "duplicate.json"
    array_data = tmp_path / "array.json"
    oversized_template.write_text(
        "x" * (64 * 1024 + 1),
        encoding="utf-8",
    )
    duplicate_data.write_text(
        '{"status":"first","status":"second"}',
        encoding="utf-8",
    )
    array_data.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        apply_commands,
        "_new_transaction",
        lambda: pytest.fail("invalid local input must not create a transaction"),
    )

    cases = [
        (
            [
                "--template",
                str(oversized_template),
            ],
            "input_size_limit",
        ),
        (
            [
                "--data",
                str(duplicate_data),
                "--table",
                ".jobs",
            ],
            "data_invalid",
        ),
        (
            [
                "--data",
                str(array_data),
                "--list",
                ".",
            ],
            "schema_data_root_not_object",
        ),
    ]
    for options, error_code in cases:
        assert run(["apply", "ci", "--target", TARGET_URL, *options]) == 2
        output = capsys.readouterr()
        assert output.out == ""
        assert f"error[{error_code}]" in output.err


def test_apply_output_modes_cover_human_json_quiet_and_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _result(
        action="updated",
        revision=8,
    )
    _install_transaction(monkeypatch, result=result)

    assert run(["apply", "ci", "--target", TARGET_URL, "--table", ".jobs"]) == 0
    output = capsys.readouterr()
    assert output.out == f"updated ci -> {TARGET_URL}#issuecomment-101\n"
    assert output.err == ""

    assert (
        run(
            [
                "apply",
                "ci",
                "--target",
                TARGET_URL,
                "--table",
                ".jobs",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out) == result.to_json()
    assert output.err == ""

    assert (
        run(
            [
                "apply",
                "ci",
                "--target",
                TARGET_URL,
                "--table",
                ".jobs",
                "--quiet",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""

    dry_run = _result(
        comment_id=None,
        url=None,
        dry_run=True,
    )
    _install_transaction(monkeypatch, result=dry_run)
    assert (
        run(
            [
                "apply",
                "ci",
                "--target",
                TARGET_URL,
                "--table",
                ".jobs",
                "--dry-run",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert output.out == dry_run.markdown
    assert output.err == ""

    assert (
        run(
            [
                "apply",
                "ci",
                "--target",
                TARGET_URL,
                "--table",
                ".jobs",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out) == dry_run.to_json()
    assert output.err == ""
