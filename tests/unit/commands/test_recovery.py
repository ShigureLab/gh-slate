from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from gh_slate.cli import run
from gh_slate.commands import recovery as recovery_commands
from gh_slate.github.recovery import (
    DeleteRequest,
    RecoveryAction,
    RecoveryResult,
    RepairRequest,
)

HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
TARGET_URL = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"
COMMENT_URL = f"{TARGET_URL}#issuecomment-7"
STATE_HASH = "a" * 64


@dataclass(slots=True)
class FakeReader:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def repo_view(self, hostname: str | None = None) -> object:
        self.calls.append(("REPO", hostname))
        return {"nameWithOwner": REPOSITORY, "url": f"https://{HOST}/{REPOSITORY}"}

    def pr_view(
        self,
        repository: str | None = None,
        hostname: str | None = None,
    ) -> object:
        self.calls.append(("PR", repository, hostname))
        return {"number": NUMBER, "url": TARGET_URL}


@dataclass(slots=True)
class FakeSession:
    result: RecoveryResult
    reader: FakeReader = field(default_factory=FakeReader)
    repair_requests: list[RepairRequest] = field(default_factory=list)
    delete_requests: list[DeleteRequest] = field(default_factory=list)

    def repair(self, request: RepairRequest) -> RecoveryResult:
        self.repair_requests.append(request)
        return self.result

    def delete(self, request: DeleteRequest) -> RecoveryResult:
        self.delete_requests.append(request)
        return self.result


def _result(
    action: RecoveryAction,
    *,
    recovered: bool = False,
) -> RecoveryResult:
    return RecoveryResult(
        action=action,
        name="ci",
        repository=REPOSITORY,
        number=NUMBER,
        comment_id=7,
        url=COMMENT_URL,
        revision=3,
        state_sha256=STATE_HASH,
        recovered=recovered,
    )


def test_repair_command_builds_a_pinned_request_and_prints_human_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession(_result("repaired"))
    monkeypatch.setattr(
        recovery_commands,
        "_new_session",
        lambda: session,
    )

    status = run(
        [
            "repair",
            "ci",
            "--from-state",
            "--target",
            TARGET_URL,
            "--controller",
            "ci-bot",
            "--if-revision",
            "3",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == f"repaired ci -> {COMMENT_URL}\n"
    assert len(session.repair_requests) == 1
    request = session.repair_requests[0]
    assert request.target.url == TARGET_URL
    assert request.name == "ci"
    assert request.from_state is True
    assert request.controller == "ci-bot"
    assert request.if_revision == 3
    assert session.reader.calls == []


def test_repair_json_and_quiet_outputs_are_unambiguous(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    json_session = FakeSession(_result("repaired", recovered=True))
    monkeypatch.setattr(
        recovery_commands,
        "_new_session",
        lambda: json_session,
    )

    assert (
        run(
            [
                "repair",
                "ci",
                "--from-state",
                "--target",
                TARGET_URL,
                "--json",
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record == {
        "action": "repaired",
        "comment_id": 7,
        "name": "ci",
        "number": NUMBER,
        "recovered": True,
        "repository": REPOSITORY,
        "revision": 3,
        "state_sha256": STATE_HASH,
        "url": COMMENT_URL,
    }

    quiet_session = FakeSession(_result("unchanged"))
    monkeypatch.setattr(
        recovery_commands,
        "_new_session",
        lambda: quiet_session,
    )
    assert (
        run(
            [
                "repair",
                "ci",
                "--from-state",
                "--target",
                TARGET_URL,
                "--quiet",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "confirmation",
    [
        ["--confirm", "ci"],
        ["--yes"],
    ],
)
def test_delete_command_accepts_exact_or_noninteractive_confirmation(
    confirmation: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession(_result("deleted"))
    monkeypatch.setattr(
        recovery_commands,
        "_new_session",
        lambda: session,
    )

    assert (
        run(
            [
                "delete",
                "ci",
                "--target",
                TARGET_URL,
                "--json",
                *confirmation,
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["action"] == "deleted"
    assert len(session.delete_requests) == 1
    request = session.delete_requests[0]
    assert request.confirm == ("ci" if confirmation[0] == "--confirm" else None)
    assert request.yes is (confirmation[0] == "--yes")


def test_delete_confirmation_mismatch_fails_before_session_or_target_lookup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory_calls = 0

    def create_session() -> FakeSession:
        nonlocal factory_calls
        factory_calls += 1
        return FakeSession(_result("deleted"))

    monkeypatch.setattr(
        recovery_commands,
        "_new_session",
        create_session,
    )

    status = run(
        [
            "delete",
            "ci",
            "--target",
            "42",
            "--confirm",
            "CI",
        ]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert "error[delete_confirmation_mismatch]" in captured.err
    assert factory_calls == 0


def test_delete_quiet_suppresses_success_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession(_result("deleted"))
    monkeypatch.setattr(
        recovery_commands,
        "_new_session",
        lambda: session,
    )

    assert (
        run(
            [
                "delete",
                "ci",
                "--target",
                TARGET_URL,
                "--yes",
                "--quiet",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == ""
