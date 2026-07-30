from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from gh_slate.cli import run
from gh_slate.commands import apply as apply_commands, read as read_commands
from gh_slate.github.process import GhProcess, ProcessResult
from gh_slate.github.write import GhWriteProcess

if TYPE_CHECKING:
    from pathlib import Path


HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
ACTOR = "ci-bot"


def _result(value: object) -> ProcessResult:
    return ProcessResult(
        returncode=0,
        stdout=json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode(),
        stderr=b"",
    )


@dataclass(slots=True)
class FakeGhBackend:
    page_url: str
    comments: list[dict[str, object]] = field(default_factory=list)
    events: list[tuple[str, str, str | None]] = field(default_factory=list)
    next_identifier: int = 100

    @property
    def comments_endpoint(self) -> str:
        return f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100"

    def get(
        self,
        endpoint: str,
        hostname: str | None,
        *,
        paginate: bool,
    ) -> ProcessResult:
        self.events.append(("GET", endpoint, hostname))
        if endpoint == "user":
            return _result({"login": ACTOR})
        if endpoint == f"repos/{REPOSITORY}/issues/{NUMBER}":
            return _result({"html_url": self.page_url})
        if endpoint == self.comments_endpoint:
            pages: object = [self.comments] if paginate else self.comments
            return _result(pages)
        raise AssertionError(f"unexpected GET endpoint: {endpoint}")

    def write(
        self,
        method: str,
        endpoint: str,
        stdin: bytes,
        hostname: str | None,
    ) -> ProcessResult:
        self.events.append((method, endpoint, hostname))
        payload = json.loads(stdin)
        body = payload["body"]
        assert isinstance(body, str)
        if method == "POST":
            assert endpoint == (f"repos/{REPOSITORY}/issues/{NUMBER}/comments")
            identifier = self.next_identifier
            self.next_identifier += 1
            record: dict[str, object] = {
                "id": identifier,
                "body": body,
                "html_url": (f"{self.page_url}#issuecomment-{identifier}"),
                "user": {"login": ACTOR},
                "created_at": "2026-07-31T00:00:00Z",
                "updated_at": "2026-07-31T00:00:00Z",
            }
            self.comments.append(record)
            return _result(record)

        assert method == "PATCH"
        identifier = int(endpoint.rsplit("/", 1)[1])
        for record in self.comments:
            if record["id"] == identifier:
                record["body"] = body
                record["updated_at"] = "2026-07-31T00:01:00Z"
                return _result(record)
        raise AssertionError("PATCH selected a missing comment")


@dataclass(slots=True)
class ReadRunner:
    backend: FakeGhBackend

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        hostname: str | None,
    ) -> ProcessResult:
        assert timeout > 0
        assert argv[0:2] == ("gh", "api")
        assert argv[argv.index("--method") + 1] == "GET"
        return self.backend.get(
            argv[-1],
            hostname,
            paginate="--paginate" in argv,
        )


@dataclass(slots=True)
class WriteRunner:
    backend: FakeGhBackend

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
        assert timeout > 0
        assert max_stdout_bytes > 0
        assert max_stderr_bytes > 0
        assert argv[0:2] == ("gh", "api")
        assert argv[argv.index("--input") + 1] == "-"
        method = argv[argv.index("--method") + 1]
        assert method in {"POST", "PATCH"}
        return self.backend.write(
            method,
            argv[-1],
            stdin,
            hostname,
        )


@pytest.mark.parametrize("kind", ["issues", "pull"])
def test_cli_to_gh_contract_create_update_unchanged_and_readback(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    page_url = f"https://{HOST}/{REPOSITORY}/{kind}/{NUMBER}"
    backend = FakeGhBackend(page_url=page_url)
    reader = GhProcess(runner=ReadRunner(backend))
    writer = GhWriteProcess(runner=WriteRunner(backend))
    transaction = apply_commands._CoreTransaction(
        reader=reader,
        writer=writer,
    )
    monkeypatch.setattr(
        apply_commands,
        "_new_transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        read_commands,
        "_new_process",
        lambda: reader,
    )

    data = tmp_path / "data.json"
    data.write_text(
        '{"jobs":[{"name":"linux","status":"pass"}]}',
        encoding="utf-8",
    )
    target = page_url
    create = [
        "apply",
        "ci",
        "--target",
        target,
        "--mode",
        "create",
        "--data",
        str(data),
        "--table",
        ".jobs",
        "--columns",
        "name,status",
        "--json",
    ]
    assert run(create, prog="gh slate") == 0
    created = json.loads(capsys.readouterr().out)
    assert created["action"] == "created"
    assert created["revision"] == 1

    data.write_text(
        '{"jobs":[{"name":"linux","status":"fail"}]}',
        encoding="utf-8",
    )
    update = [
        "apply",
        "ci",
        "--target",
        target,
        "--mode",
        "update",
        "--data",
        str(data),
        "--if-revision",
        "1",
        "--json",
    ]
    assert run(update, prog="gh slate") == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["action"] == "updated"
    assert updated["revision"] == 2

    update.remove("--if-revision")
    update.remove("1")
    assert run(update, prog="gh slate") == 0
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged["action"] == "unchanged"
    assert unchanged["revision"] == 2

    assert (
        run(
            [
                "view",
                "ci",
                "--target",
                target,
                "--json",
            ],
            prog="gh slate",
        )
        == 0
    )
    readback = json.loads(capsys.readouterr().out)
    assert readback["revision"] == 2
    assert readback["status"] == "valid"

    writes = [event for event in backend.events if event[0] in {"POST", "PATCH"}]
    assert [event[0] for event in writes] == ["POST", "PATCH"]
    assert all(event[2] == HOST for event in backend.events)
    for index, event in enumerate(backend.events):
        if event[0] in {"POST", "PATCH"}:
            assert backend.events[index - 1] == (
                "GET",
                backend.comments_endpoint,
                HOST,
            )
            assert backend.events[index + 1] == (
                "GET",
                backend.comments_endpoint,
                HOST,
            )
