from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from gh_slate.cli import run
from gh_slate.codec import decode_comment
from gh_slate.commands import (
    apply as apply_commands,
    data as data_commands,
    schema as schema_commands,
)
from gh_slate.github.apply import ApplyResult, ApplyTransaction
from gh_slate.github.mutation import MutationRequest, MutationTransaction
from gh_slate.github.process import GhProcess, ProcessResult
from gh_slate.github.write import GhWriteProcess

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
ACTOR = "ci-bot"
TARGET = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"


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
    comments: list[dict[str, object]] = field(default_factory=list)
    events: list[tuple[str, str, str | None]] = field(default_factory=list)
    next_identifier: int = 100

    @property
    def comments_endpoint(self) -> str:
        return f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100"

    @property
    def patch_count(self) -> int:
        return sum(method == "PATCH" for method, _endpoint, _host in self.events)

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
            return _result({"html_url": TARGET})
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
            assert endpoint == f"repos/{REPOSITORY}/issues/{NUMBER}/comments"
            identifier = self.next_identifier
            self.next_identifier += 1
            record: dict[str, object] = {
                "id": identifier,
                "body": body,
                "html_url": f"{TARGET}#issuecomment-{identifier}",
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


@dataclass(slots=True)
class MutationSession:
    reader: GhProcess
    transaction: MutationTransaction

    def mutate(self, request: MutationRequest) -> ApplyResult:
        return self.transaction.mutate(request)


def test_data_and_schema_cli_round_trip_through_fake_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = FakeGhBackend()
    reader = GhProcess(runner=ReadRunner(backend))
    writer = GhWriteProcess(runner=WriteRunner(backend))
    applier = ApplyTransaction(
        reader=reader,
        writer=writer,
    )
    session = MutationSession(
        reader=reader,
        transaction=MutationTransaction(
            reader=reader,
            applier=applier,
        ),
    )
    monkeypatch.setattr(
        apply_commands,
        "_new_transaction",
        lambda: applier,
    )
    monkeypatch.setattr(
        data_commands,
        "_new_process",
        lambda: reader,
    )
    monkeypatch.setattr(
        data_commands,
        "_new_mutation_session",
        lambda: session,
    )
    monkeypatch.setattr(
        schema_commands,
        "_new_process",
        lambda: reader,
    )
    monkeypatch.setattr(
        schema_commands,
        "_new_mutation_session",
        lambda: session,
    )

    data_file = tmp_path / "data.json"
    data_file.write_text(
        json.dumps(
            {
                "items": [{"name": "linux"}],
                "keep": {"exact": 9007199254740991},
                "metrics": {"count": 1, "label": "queued"},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    assert (
        run(
            [
                "apply",
                "ci",
                "--target",
                TARGET,
                "--mode",
                "create",
                "--data",
                str(data_file),
                "--table",
                ".items",
                "--columns",
                "name",
                "--json",
            ],
            prog="gh slate",
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["action"] == "created"
    assert created["revision"] == 1
    revisions = [created["revision"]]
    assert backend.patch_count == 0

    assert (
        run(
            [
                "data",
                "get",
                "ci",
                ".metrics",
                "--target",
                TARGET,
                "--compact-output",
            ],
            prog="gh slate",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "count": 1,
        "label": "queued",
    }
    assert backend.patch_count == 0

    def assert_change(arguments: list[str], revision: int) -> None:
        patches_before = backend.patch_count
        assert run(arguments, prog="gh slate") == 0
        result = json.loads(capsys.readouterr().out)
        assert result["action"] == "updated"
        assert result["revision"] == revision
        assert backend.patch_count == patches_before + 1
        revisions.append(result["revision"])

    assert_change(
        [
            "data",
            "set",
            "ci",
            ".metrics.count",
            "--target",
            TARGET,
            "--value",
            "2",
            "--json",
        ],
        2,
    )
    assert_change(
        [
            "data",
            "set",
            "ci",
            ".metrics.label",
            "--target",
            TARGET,
            "--value-string",
            "ready",
            "--json",
        ],
        3,
    )
    assert_change(
        [
            "data",
            "update",
            "ci",
            ".metrics |= (.count += $increment | .label = $label)",
            "--target",
            TARGET,
            "--arg",
            "label",
            "passed",
            "--argjson",
            "increment",
            "3",
            "--json",
        ],
        4,
    )
    assert_change(
        [
            "data",
            "delete",
            "ci",
            ".metrics.label",
            "--target",
            TARGET,
            "--json",
        ],
        5,
    )

    schema_file = tmp_path / "schema.json"
    schema_file.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "items": {"type": "array"},
                    "keep": {"type": "object"},
                    "metrics": {
                        "type": "object",
                        "properties": {"count": {"type": "number"}},
                        "required": ["count"],
                        "additionalProperties": False,
                    },
                },
                "required": ["items", "keep", "metrics"],
                "additionalProperties": False,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    assert_change(
        [
            "schema",
            "set",
            "ci",
            str(schema_file),
            "--target",
            TARGET,
            "--json",
        ],
        6,
    )

    patches_before_read = backend.patch_count
    assert (
        run(
            [
                "schema",
                "get",
                "ci",
                "--target",
                TARGET,
                "--compact-output",
            ],
            prog="gh slate",
        )
        == 0
    )
    stored_schema = json.loads(capsys.readouterr().out)
    assert stored_schema["required"] == ["items", "keep", "metrics"]
    assert backend.patch_count == patches_before_read

    assert (
        run(
            [
                "schema",
                "validate",
                "ci",
                "--target",
                TARGET,
                "--json",
            ],
            prog="gh slate",
        )
        == 0
    )
    validation = json.loads(capsys.readouterr().out)
    assert validation == {
        "name": "ci",
        "revision": 6,
        "source": "stored",
        "valid": True,
    }
    assert backend.patch_count == patches_before_read

    candidate_file = tmp_path / "invalid-candidate.json"
    candidate_file.write_text(
        '{"items":[],"keep":{},"metrics":{"count":"wrong"}}',
        encoding="utf-8",
    )
    assert (
        run(
            [
                "schema",
                "validate",
                "ci",
                str(candidate_file),
                "--target",
                TARGET,
            ],
            prog="gh slate",
        )
        != 0
    )
    assert "schema_validation_failed" in capsys.readouterr().err
    assert backend.patch_count == patches_before_read

    incompatible_schema = tmp_path / "incompatible-schema.json"
    incompatible_schema.write_text(
        '{"type":"object","required":["absent"]}',
        encoding="utf-8",
    )
    assert (
        run(
            [
                "schema",
                "set",
                "ci",
                str(incompatible_schema),
                "--target",
                TARGET,
                "--json",
            ],
            prog="gh slate",
        )
        != 0
    )
    assert "schema_validation_failed" in capsys.readouterr().err
    assert backend.patch_count == patches_before_read

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                ".metrics.count, .metrics.count",
                "--target",
                TARGET,
                "--json",
            ],
            prog="gh slate",
        )
        != 0
    )
    assert "data_update_multiple_results" in capsys.readouterr().err
    assert backend.patch_count == patches_before_read

    assert_change(
        [
            "schema",
            "infer",
            "ci",
            "--target",
            TARGET,
            "--apply",
            "--json",
        ],
        7,
    )

    patches_before_unchanged = backend.patch_count
    assert (
        run(
            [
                "data",
                "update",
                "ci",
                ".",
                "--target",
                TARGET,
                "--json",
            ],
            prog="gh slate",
        )
        == 0
    )
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged["action"] == "unchanged"
    assert unchanged["revision"] == 7
    assert backend.patch_count == patches_before_unchanged

    assert revisions == [1, 2, 3, 4, 5, 6, 7]
    assert len(backend.comments) == 1
    body = cast("str", backend.comments[0]["body"])
    decoded = decode_comment(body)
    assert decoded.drifted is False
    assert decoded.state.revision == 7
    assert decoded.state.data == {
        "items": ({"name": "linux"},),
        "keep": {"exact": Decimal(9007199254740991)},
        "metrics": {"count": Decimal(5)},
    }
    assert decoded.state.data_schema is not None

    final_patch_count = backend.patch_count
    assert (
        run(
            [
                "data",
                "get",
                "ci",
                ".",
                "--target",
                TARGET,
                "--compact-output",
            ],
            prog="gh slate",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "items": [{"name": "linux"}],
        "keep": {"exact": 9007199254740991},
        "metrics": {"count": 5},
    }
    assert backend.patch_count == final_patch_count

    writes = [event for event in backend.events if event[0] in {"POST", "PATCH"}]
    assert [event[0] for event in writes] == [
        "POST",
        "PATCH",
        "PATCH",
        "PATCH",
        "PATCH",
        "PATCH",
        "PATCH",
    ]
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
