from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.cli import run
from gh_slate.codec import (
    ControllerV1,
    StateV1,
    canonical_json_bytes,
    freeze_json,
)
from gh_slate.codec.model import (
    JSON_SCHEMA_DIALECT_2020_12,
    SchemaSnapshotV1,
)
from gh_slate.commands import schema as schema_commands
from gh_slate.github.apply import ApplyResult
from gh_slate.github.models import GitHubActor
from gh_slate.rendering import ListRendererV1, SlateContext, materialize_comment
from gh_slate.schema import validate_schema

if TYPE_CHECKING:
    from pathlib import Path

    from gh_slate.codec import JsonValue
    from gh_slate.github.mutation import (
        MutationDraft,
        MutationRequest,
        MutationSnapshot,
    )


HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
TARGET_URL = f"https://{HOST}/{REPOSITORY}/pull/42"
COMMENT_URL = f"{TARGET_URL}#issuecomment-7"
STATE_HASH = "b" * 64
ACTOR_ID = 101


def _schema() -> SchemaSnapshotV1:
    return validate_schema(
        {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
            },
            "required": ["status"],
            "additionalProperties": False,
        }
    )


def _comment_body(
    data: object,
    *,
    schema: SchemaSnapshotV1 | None,
    revision: int = 3,
) -> str:
    state = StateV1(
        name="ci",
        revision=revision,
        controller=ControllerV1(login="ci-bot", id=ACTOR_ID),
        data=cast("dict[str, JsonValue]", data),
        data_schema=schema,
        renderer=ListRendererV1(selector=".").to_descriptor(),
        render_sha256="0" * 64,
    )
    return materialize_comment(
        state,
        slate=SlateContext(
            name="ci",
            repository=REPOSITORY,
            number=NUMBER,
            url=TARGET_URL,
        ),
    ).encoded.body


@dataclass(slots=True)
class FakeReadProcess:
    data: object
    schema: SchemaSnapshotV1 | None
    revision: int = 3
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def current_actor(self, hostname: str | None = None) -> GitHubActor:
        self.calls.append(("ACTOR", hostname))
        return GitHubActor(id=ACTOR_ID, login="ci-bot")

    def resolve_actor(
        self,
        login: str,
        hostname: str | None = None,
    ) -> GitHubActor:
        self.calls.append(("RESOLVE_ACTOR", login, hostname))
        return GitHubActor(id=ACTOR_ID, login="ci-bot")

    def api_get(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
        paginate: bool = False,
    ) -> object:
        self.calls.append(("GET", endpoint, hostname, paginate))
        assert endpoint == f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100"
        return [
            {
                "id": 7,
                "body": _comment_body(
                    self.data,
                    schema=self.schema,
                    revision=self.revision,
                ),
                "html_url": COMMENT_URL,
                "user": {"id": ACTOR_ID, "login": "ci-bot"},
                "created_at": "2026-07-31T00:00:00Z",
                "updated_at": "2026-07-31T00:01:00Z",
            }
        ]

    def repo_view(self, hostname: str | None = None) -> object:
        raise AssertionError("a full target URL must not look up the repository")

    def pr_view(
        self,
        repository: str | None = None,
        hostname: str | None = None,
    ) -> object:
        raise AssertionError("a full target URL must not look up a pull request")


@dataclass(slots=True)
class FakeMutationSession:
    data: object
    schema: SchemaSnapshotV1 | None
    reader: FakeReadProcess = field(
        default_factory=lambda: FakeReadProcess(
            data={"unused": True},
            schema=None,
        )
    )
    requests: list[MutationRequest] = field(default_factory=list)
    drafts: list[MutationDraft] = field(default_factory=list)

    def mutate(self, request: MutationRequest) -> ApplyResult:
        self.requests.append(request)
        snapshot = SimpleNamespace(
            data=freeze_json(self.data),
            data_schema=self.schema,
        )
        draft = request.transform(cast("MutationSnapshot", snapshot))
        self.drafts.append(draft)
        return ApplyResult(
            action="updated",
            name=request.name,
            repository=request.target.repository,
            number=request.target.number,
            comment_id=7,
            url=COMMENT_URL,
            revision=4,
            state_sha256=STATE_HASH,
        )


def _install_read(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: object,
    schema: SchemaSnapshotV1 | None,
    revision: int = 3,
) -> FakeReadProcess:
    process = FakeReadProcess(
        data=data,
        schema=schema,
        revision=revision,
    )
    monkeypatch.setattr(schema_commands, "_new_process", lambda: process)
    return process


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: object,
    schema: SchemaSnapshotV1 | None,
) -> FakeMutationSession:
    session = FakeMutationSession(data=data, schema=schema)
    monkeypatch.setattr(schema_commands, "_new_mutation_session", lambda: session)
    return session


def test_schema_get_reports_a_missing_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_read(
        monkeypatch,
        data={"status": "ready"},
        schema=None,
    )

    assert (
        run(
            [
                "schema",
                "get",
                "ci",
                "--target",
                TARGET_URL,
            ]
        )
        == 3
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[schema_not_found]" in output.err


def test_schema_get_prints_the_stored_snapshot_in_compact_form(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema = _schema()
    _install_read(
        monkeypatch,
        data={"status": "ready"},
        schema=schema,
    )

    assert (
        run(
            [
                "schema",
                "get",
                "ci",
                "--target",
                TARGET_URL,
                "--compact-output",
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    assert json.loads(output.out) == json.loads(
        json.dumps(
            {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
                "additionalProperties": False,
            }
        )
    )
    assert "\n" not in output.out.rstrip("\n")
    assert output.err == ""


def test_schema_set_parses_locally_before_constructing_a_remote_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema_path = tmp_path / "invalid-schema.json"
    schema_path.write_text('{"type":', encoding="utf-8")
    factory_called = False

    def unexpected_factory() -> FakeMutationSession:
        nonlocal factory_called
        factory_called = True
        raise AssertionError("remote session setup must follow local schema parsing")

    monkeypatch.setattr(
        schema_commands,
        "_new_mutation_session",
        unexpected_factory,
    )

    assert (
        run(
            [
                "schema",
                "set",
                "ci",
                str(schema_path),
                "--target",
                TARGET_URL,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[schema_invalid]" in output.err
    assert factory_called is False


def test_schema_set_replaces_the_schema_and_forwards_mutation_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema_path = tmp_path / "schema.json"
    document = {
        "$schema": JSON_SCHEMA_DIALECT_2020_12,
        "type": "object",
        "additionalProperties": True,
    }
    schema_path.write_text(json.dumps(document), encoding="utf-8")
    session = _install_session(
        monkeypatch,
        data={"status": "ready"},
        schema=None,
    )

    assert (
        run(
            [
                "schema",
                "set",
                "ci",
                str(schema_path),
                "--target",
                TARGET_URL,
                "--controller",
                "ci-bot",
                "--if-revision",
                "3",
                "--json",
            ]
        )
        == 0
    )

    assert len(session.requests) == 1
    request = session.requests[0]
    assert request.target.host == HOST
    assert request.target.repository == REPOSITORY
    assert request.target.number == NUMBER
    assert request.controller == "ci-bot"
    assert request.if_revision == 3
    assert len(session.drafts) == 1
    assert session.drafts[0].data == {"status": "ready"}
    replacement = session.drafts[0].schema
    assert isinstance(replacement, SchemaSnapshotV1)
    assert replacement.document == document
    output = capsys.readouterr()
    assert json.loads(output.out)["action"] == "updated"
    assert output.err == ""


def test_schema_infer_previews_without_constructing_a_mutation_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_read(
        monkeypatch,
        data={"status": "ready", "count": Decimal(2)},
        schema=None,
        revision=9,
    )
    mutation_factory_called = False

    def unexpected_factory() -> FakeMutationSession:
        nonlocal mutation_factory_called
        mutation_factory_called = True
        raise AssertionError("preview must remain read-only")

    monkeypatch.setattr(
        schema_commands,
        "_new_mutation_session",
        unexpected_factory,
    )

    assert (
        run(
            [
                "schema",
                "infer",
                "ci",
                "--target",
                TARGET_URL,
                "--if-revision",
                "9",
                "--json",
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload == {
        "applied": False,
        "name": "ci",
        "revision": 9,
        "schema": {
            "$schema": JSON_SCHEMA_DIALECT_2020_12,
            "properties": {
                "count": {"type": "integer"},
                "status": {"type": "string"},
            },
            "type": "object",
        },
    }
    assert output.err == ""
    assert mutation_factory_called is False


def test_schema_infer_apply_stores_the_inferred_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(
        monkeypatch,
        data={"status": "ready", "values": [1, "two"]},
        schema=None,
    )

    assert (
        run(
            [
                "schema",
                "infer",
                "ci",
                "--target",
                TARGET_URL,
                "--apply",
                "--quiet",
            ]
        )
        == 0
    )

    assert len(session.drafts) == 1
    inferred = cast("SchemaSnapshotV1", session.drafts[0].schema)
    assert json.loads(canonical_json_bytes(inferred.document)) == {
        "$schema": JSON_SCHEMA_DIALECT_2020_12,
        "properties": {
            "status": {"type": "string"},
            "values": {
                "items": {"type": ["integer", "string"]},
                "type": "array",
            },
        },
        "type": "object",
    }
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


@pytest.mark.parametrize(
    ("candidate", "source"),
    [
        (None, "stored"),
        ({"status": "candidate"}, "candidate"),
    ],
)
def test_schema_validate_accepts_stored_and_candidate_data(
    candidate: object | None,
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_read(
        monkeypatch,
        data={"status": "stored"},
        schema=_schema(),
    )
    argv = [
        "schema",
        "validate",
        "ci",
        "--target",
        TARGET_URL,
        "--json",
    ]
    if candidate is not None:
        candidate_path = tmp_path / "candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        argv.insert(3, str(candidate_path))

    assert run(argv) == 0

    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "name": "ci",
        "revision": 3,
        "source": source,
        "valid": True,
    }
    assert output.err == ""


def test_schema_validation_failure_never_constructs_a_mutation_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_read(
        monkeypatch,
        data={"status": "stored"},
        schema=_schema(),
    )
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text('{"status": 9}', encoding="utf-8")
    mutation_factory_called = False

    def unexpected_factory() -> FakeMutationSession:
        nonlocal mutation_factory_called
        mutation_factory_called = True
        raise AssertionError("schema validation is read-only")

    monkeypatch.setattr(
        schema_commands,
        "_new_mutation_session",
        unexpected_factory,
    )

    assert (
        run(
            [
                "schema",
                "validate",
                "ci",
                str(candidate_path),
                "--target",
                TARGET_URL,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[schema_validation_failed]" in output.err
    assert mutation_factory_called is False
