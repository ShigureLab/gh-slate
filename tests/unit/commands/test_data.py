from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.cli import run
from gh_slate.codec import ControllerV1, StateV1, freeze_json
from gh_slate.commands import data as data_commands
from gh_slate.github.apply import ApplyResult
from gh_slate.github.models import GitHubActor
from gh_slate.rendering import ListRendererV1, SlateContext, materialize_comment
from gh_slate.schema import validate_schema

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from gh_slate.codec import JsonValue, SchemaSnapshotV1
    from gh_slate.errors import GhSlateError
    from gh_slate.github.mutation import (
        MutationDraft,
        MutationRequest,
        MutationSnapshot,
    )


HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
TARGET_URL = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"
COMMENT_URL = f"{TARGET_URL}#issuecomment-7"
STATE_HASH = "a" * 64
ACTOR_ID = 101


def _schema() -> SchemaSnapshotV1:
    return validate_schema(
        {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
            },
            "required": ["status"],
            "additionalProperties": True,
        }
    )


def _comment_body(
    data: object,
    *,
    schema: SchemaSnapshotV1 | None = None,
    revision: int = 3,
    drifted: bool = False,
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
    body = materialize_comment(
        state,
        slate=SlateContext(
            name="ci",
            repository=REPOSITORY,
            number=NUMBER,
            url=TARGET_URL,
        ),
    ).encoded.body
    if drifted:
        marker_end = body.index("-->") + 3
        return f"{body[:marker_end]}\n\nlocally edited\n"
    return body


@dataclass(slots=True)
class FakeReadProcess:
    data: object
    schema: SchemaSnapshotV1 | None = None
    revision: int = 3
    drifted: bool = False
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
                    drifted=self.drifted,
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
    schema: SchemaSnapshotV1 | None = None
    reader: FakeReadProcess = field(default_factory=lambda: FakeReadProcess(data={"unused": True}))
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
    schema: SchemaSnapshotV1 | None = None,
    drifted: bool = False,
) -> FakeReadProcess:
    process = FakeReadProcess(data=data, schema=schema, drifted=drifted)
    monkeypatch.setattr(data_commands, "_new_process", lambda: process)
    return process


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: object,
    schema: SchemaSnapshotV1 | None = None,
) -> FakeMutationSession:
    session = FakeMutationSession(data=data, schema=schema)
    monkeypatch.setattr(data_commands, "_new_mutation_session", lambda: session)
    return session


@pytest.mark.parametrize(
    ("filter_text", "options", "expected_status", "expected_output"),
    [
        ("empty", ["--exit-status"], 4, ""),
        (".word", ["--raw-output", "--exit-status"], 0, "ready\n"),
        (
            ".items[]",
            ["--compact-output"],
            0,
            '{"name":"one"}\n{"name":"two"}\n',
        ),
        (".disabled", ["--exit-status"], 1, "false\n"),
    ],
)
def test_data_get_supports_zero_one_and_many_jq_results(
    filter_text: str,
    options: list[str],
    expected_status: int,
    expected_output: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_read(
        monkeypatch,
        data={
            "word": "ready",
            "items": [{"name": "one"}, {"name": "two"}],
            "disabled": False,
        },
    )

    assert (
        run(
            [
                "data",
                "get",
                "ci",
                filter_text,
                "--target",
                TARGET_URL,
                *options,
            ]
        )
        == expected_status
    )

    output = capsys.readouterr()
    assert output.out == expected_output
    assert output.err == ""


def test_data_get_warns_on_drift_but_queries_canonical_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_read(
        monkeypatch,
        data={"status": "canonical"},
        drifted=True,
    )

    assert (
        run(
            [
                "data",
                "get",
                "ci",
                ".status",
                "--target",
                TARGET_URL,
                "--raw-output",
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    assert output.out == "canonical\n"
    assert output.err == ("warning[render_drift]: queried canonical data despite visible Markdown drift\n")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (["--value", "1"], Decimal(1)),
        (["--value-string", "1"], "1"),
        (["--value", "true"], True),
    ],
)
def test_data_set_preserves_the_selected_value_type(
    source: list[str],
    expected: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(
        monkeypatch,
        data={"value": None},
    )

    assert (
        run(
            [
                "data",
                "set",
                "ci",
                ".value",
                "--target",
                TARGET_URL,
                *source,
            ]
        )
        == 0
    )

    assert session.drafts[0].data == {"value": expected}
    output = capsys.readouterr()
    assert output.out == f"updated ci -> {COMMENT_URL}\n"
    assert output.err == ""


def test_data_set_uses_an_exact_path_for_a_dotted_object_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(
        monkeypatch,
        data={"a.b": "old", "a": {"b": "untouched"}},
    )

    assert (
        run(
            [
                "data",
                "set",
                "ci",
                '.["a.b"]',
                "--target",
                TARGET_URL,
                "--value-string",
                "new",
            ]
        )
        == 0
    )

    assert session.drafts[0].data == {
        "a.b": "new",
        "a": {"b": "untouched"},
    }
    assert capsys.readouterr().err == ""


def test_data_set_uses_the_exact_static_key_for_a_large_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exact_id = Decimal(9007199254740993)
    session = _install_session(
        monkeypatch,
        data={
            "id": exact_id,
            "by_id": {
                "9007199254740992": "wrong",
                "9007199254740993": "right",
            },
        },
    )

    assert (
        run(
            [
                "data",
                "set",
                "ci",
                '.by_id["9007199254740993"]',
                "--target",
                TARGET_URL,
                "--value-string",
                "updated",
            ]
        )
        == 0
    )

    assert session.drafts[0].data == {
        "id": exact_id,
        "by_id": {
            "9007199254740992": "wrong",
            "9007199254740993": "updated",
        },
    }
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("command", "value_arguments"),
    [
        ("set", ["--value-string", "updated"]),
        ("delete", []),
    ],
)
def test_data_mutation_rejects_a_precision_dependent_dynamic_path(
    command: str,
    value_arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(
        monkeypatch,
        data={
            "id": Decimal(9007199254740993),
            "by_id": {
                "9007199254740992": "wrong",
                "9007199254740993": "right",
            },
        },
    )

    assert (
        run(
            [
                "data",
                command,
                "ci",
                ".by_id[((.id + 0) | tostring)]",
                "--target",
                TARGET_URL,
                *value_arguments,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[data_path_dynamic]" in output.err
    assert session.drafts == []


def test_data_set_preserves_an_untouched_number_beyond_float_range(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    huge = Decimal("1e309")
    session = _install_session(
        monkeypatch,
        data={"huge": huge, "status": "old"},
    )

    assert (
        run(
            [
                "data",
                "set",
                "ci",
                ".status",
                "--target",
                TARGET_URL,
                "--value-string",
                "new",
            ]
        )
        == 0
    )

    assert session.drafts[0].data == {"huge": huge, "status": "new"}
    assert capsys.readouterr().err == ""


def test_data_delete_preserves_an_untouched_number_beyond_float_range(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    huge = Decimal("-1e309")
    session = _install_session(
        monkeypatch,
        data={"huge": huge, "legacy": True},
    )

    assert (
        run(
            [
                "data",
                "delete",
                "ci",
                ".legacy",
                "--target",
                TARGET_URL,
            ]
        )
        == 0
    )

    assert session.drafts[0].data == {"huge": huge}
    assert capsys.readouterr().err == ""


def test_data_delete_ignore_missing_produces_an_unchanged_draft(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = {"status": "ready"}
    session = _install_session(monkeypatch, data=original)

    assert (
        run(
            [
                "data",
                "delete",
                "ci",
                ".missing",
                "--target",
                TARGET_URL,
                "--ignore-missing",
                "--quiet",
            ]
        )
        == 0
    )

    assert session.drafts[0].data == original
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_data_update_binds_string_and_typed_jq_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    count_path = tmp_path / "count.json"
    count_path.write_text("7", encoding="utf-8")
    session = _install_session(monkeypatch, data={"status": "old"})

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                ". + {label: $label, count: $count}",
                "--target",
                TARGET_URL,
                "--arg",
                "label",
                "7",
                "--argjson",
                "count",
                f"@{count_path}",
            ]
        )
        == 0
    )

    assert session.drafts[0].data == {
        "status": "old",
        "label": "7",
        "count": Decimal(7),
    }
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("filter_text", "error_code"),
    [
        ("empty", "data_update_no_result"),
        ("., .", "data_update_multiple_results"),
        (".status", "data_update_root_not_object"),
    ],
)
def test_data_update_rejects_invalid_root_results_without_writing(
    filter_text: str,
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(monkeypatch, data={"status": "old"})

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                filter_text,
                "--target",
                TARGET_URL,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert f"error[{error_code}]" in output.err
    assert session.drafts == []


@pytest.mark.parametrize(
    ("data", "filter_text", "expected_requests"),
    [
        (
            {"exact": Decimal(9007199254740993)},
            ".",
            1,
        ),
        (
            {"exact": Decimal("1e999999999999999999")},
            ".",
            1,
        ),
        (
            {"safe": Decimal(1)},
            ".generated = 9007199254740993",
            0,
        ),
    ],
)
def test_data_update_rejects_unsafe_integers_without_a_draft(
    data: object,
    filter_text: str,
    expected_requests: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(monkeypatch, data=data)

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                filter_text,
                "--target",
                TARGET_URL,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[data_update_unsafe_integer]" in output.err
    assert session.drafts == []
    assert len(session.requests) == expected_requests


def test_data_update_rejects_other_identity_precision_loss(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(
        monkeypatch,
        data={"precise": Decimal("0.12345678901234567890123456789")},
    )

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                ".",
                "--target",
                TARGET_URL,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[data_update_precision_loss]" in output.err
    assert session.drafts == []
    assert len(session.requests) == 1


@pytest.mark.parametrize(
    ("filter_text", "arguments"),
    [
        (
            ".answer = ($x - 9007199254740991)",
            ["--argjson", "x", "9007199254740993"],
        ),
        (
            ".answer = (9007199254740993 - 9007199254740991)",
            [],
        ),
        (
            ".answer = 0.12345678901234567890123456789",
            [],
        ),
        (
            '.answer = "value \\(9007199254740993)"',
            [],
        ),
        (
            ".answer = $x",
            ["--argjson", "x", "0.12345678901234567890123456789"],
        ),
    ],
)
def test_data_update_rejects_precision_losing_arguments_and_literals_before_read(
    filter_text: str,
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(monkeypatch, data={"answer": 0})

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                filter_text,
                "--target",
                TARGET_URL,
                *arguments,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[data_update_unsafe_integer]" in output.err or "error[data_update_precision_loss]" in output.err
    assert session.requests == []
    assert session.drafts == []


def test_data_update_rejects_out_of_range_filter_exponent_before_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(monkeypatch, data={"answer": 0})

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                ".answer = 1e9999999999999999999999999999999999999999999",
                "--target",
                TARGET_URL,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[data_update_precision_loss]" in output.err
    assert session.requests == []
    assert session.drafts == []


def test_data_update_numeric_text_is_not_mistaken_for_a_filter_literal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(monkeypatch, data={"answer": None})

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                '.answer = "9007199254740993"',
                "--target",
                TARGET_URL,
            ]
        )
        == 0
    )

    assert session.drafts[0].data == {"answer": "9007199254740993"}
    assert capsys.readouterr().err == ""


def test_data_update_numeric_comment_is_not_mistaken_for_a_filter_literal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(monkeypatch, data={"answer": None})

    assert (
        run(
            [
                "data",
                "update",
                "ci",
                ".answer = 1 # 9007199254740993",
                "--target",
                TARGET_URL,
            ]
        )
        == 0
    )

    assert session.drafts[0].data == {"answer": Decimal(1)}
    assert capsys.readouterr().err == ""


def test_data_edit_validates_the_candidate_against_the_stored_schema(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(
        monkeypatch,
        data={"status": "old"},
        schema=_schema(),
    )
    validations: list[object] = []

    def fake_edit(
        value: object,
        *,
        validate: Callable[[JsonValue], object],
        retry: Callable[[GhSlateError, int], bool],
    ) -> JsonValue:
        del value
        assert callable(retry)
        candidate = freeze_json({"status": 9})
        validations.append(candidate)
        validate(candidate)
        raise AssertionError("schema validation must reject the candidate")

    monkeypatch.setattr(data_commands, "edit_json", fake_edit)

    assert (
        run(
            [
                "data",
                "edit",
                "ci",
                "--target",
                TARGET_URL,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert "error[schema_validation_failed]" in output.err
    assert len(validations) == 1
    assert session.drafts == []


@pytest.mark.parametrize(
    ("argv", "error_code"),
    [
        (
            ["set", "ci", ".status", "--value", "{invalid"],
            "json_invalid",
        ),
        (
            ["set", "ci", "empty", "--value-string", "new"],
            "data_path_dynamic",
        ),
        (
            ["delete", "ci", ".missing"],
            "data_path_missing",
        ),
        (
            [
                "update",
                "ci",
                ".",
                "--arg",
                "same",
                "text",
                "--argjson",
                "same",
                "1",
            ],
            "data_jq_argument_duplicate",
        ),
    ],
)
def test_local_input_and_transform_errors_produce_no_mutation_result(
    argv: list[str],
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(monkeypatch, data={"status": "old"})

    assert (
        run(
            [
                "data",
                *argv,
                "--target",
                TARGET_URL,
            ]
        )
        == 2
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert f"error[{error_code}]" in output.err
    assert session.drafts == []


def test_data_mutation_forwards_identity_revision_and_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_session(monkeypatch, data={"status": "old"})

    assert (
        run(
            [
                "data",
                "set",
                "ci",
                ".status",
                "--target",
                TARGET_URL,
                "--controller",
                "ci-bot",
                "--if-revision",
                "3",
                "--value-string",
                "new",
                "--json",
            ]
        )
        == 0
    )

    assert len(session.requests) == 1
    request = session.requests[0]
    assert request.name == "ci"
    assert request.target.host == HOST
    assert request.target.repository == REPOSITORY
    assert request.target.number == NUMBER
    assert request.target.url == TARGET_URL
    assert request.controller == "ci-bot"
    assert request.if_revision == 3
    assert request.dry_run is False
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "action": "updated",
        "comment_id": 7,
        "name": "ci",
        "number": 42,
        "repository": REPOSITORY,
        "revision": 4,
        "state_sha256": STATE_HASH,
        "url": COMMENT_URL,
    }
    assert output.err == ""
