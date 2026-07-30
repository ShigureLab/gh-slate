from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.codec import (
    ControllerV1,
    SchemaSnapshotV1,
    StateV1,
)
from gh_slate.errors import ExitCode, GhSlateError
from gh_slate.github import (
    CLEAR_SCHEMA,
    KEEP_SCHEMA,
    ApplyError,
    ApplyRequest,
    ApplyResult,
    MutationDraft,
    MutationError,
    MutationRequest,
    MutationSnapshot,
    MutationTransaction,
    MutationTransform,
    ResolvedTarget,
    SchemaMutation,
)
from gh_slate.rendering import (
    ListRendererV1,
    SlateContext,
    materialize_comment,
)
from gh_slate.schema import SchemaError, validate_schema

if TYPE_CHECKING:
    from collections.abc import Callable

HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
ISSUE_URL = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"
TARGET = ResolvedTarget(
    host=HOST,
    repository=REPOSITORY,
    number=NUMBER,
    url=ISSUE_URL,
)
EMPTY_HASH = "0" * 64


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


def _body(
    status: str = "old",
    *,
    revision: int = 3,
    schema: SchemaSnapshotV1 | None = None,
) -> str:
    state = StateV1(
        name="ci",
        revision=revision,
        controller=ControllerV1(login="ci-bot"),
        data={"status": status},
        data_schema=schema,
        renderer=ListRendererV1(selector=".status").to_descriptor(),
        render_sha256=EMPTY_HASH,
    )
    return materialize_comment(
        state,
        slate=SlateContext(
            name="ci",
            repository=REPOSITORY,
            number=NUMBER,
            url=ISSUE_URL,
        ),
    ).encoded.body


def _record(
    identifier: int,
    body: str,
) -> dict[str, object]:
    return {
        "id": identifier,
        "body": body,
        "html_url": f"{ISSUE_URL}#issuecomment-{identifier}",
        "user": {"login": "ci-bot"},
        "created_at": "2026-07-31T00:00:00Z",
        "updated_at": "2026-07-31T00:01:00Z",
    }


@dataclass(slots=True)
class FakeReader:
    comments: list[dict[str, object]]
    actor: str = "ci-bot"
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def current_actor(self, hostname: str | None = None) -> str:
        self.calls.append(("ACTOR", hostname))
        return self.actor

    def api_get(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
        paginate: bool = False,
    ) -> object:
        self.calls.append(("GET", endpoint, hostname, paginate))
        assert endpoint == (f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100")
        return [dict(comment) for comment in self.comments]


@dataclass(slots=True)
class FakeApplier:
    action: str = "updated"
    error: Exception | None = None
    calls: list[ApplyRequest] = field(default_factory=list)

    def apply(self, request: ApplyRequest) -> ApplyResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        assert self.action in {"updated", "unchanged"}
        return ApplyResult(
            action=self.action,
            name=request.name,
            repository=request.target.repository,
            number=request.target.number,
            comment_id=7,
            url=f"{ISSUE_URL}#issuecomment-7",
            revision=cast_revision(request.if_revision) + (0 if self.action == "unchanged" else 1),
            state_sha256="1" * 64,
            dry_run=request.dry_run,
        )


def cast_revision(value: int | None) -> int:
    assert value is not None
    return value


def _transaction(
    *,
    body: str | None = None,
    applier: FakeApplier | None = None,
) -> tuple[MutationTransaction, FakeReader, FakeApplier]:
    reader = FakeReader(
        comments=[
            _record(
                7,
                _body() if body is None else body,
            )
        ]
    )
    selected_applier = FakeApplier() if applier is None else applier
    return (
        MutationTransaction(
            reader=reader,
            applier=selected_applier,
        ),
        reader,
        selected_applier,
    )


def test_read_returns_one_valid_immutable_snapshot() -> None:
    transaction, reader, _ = _transaction(body=_body(schema=_schema()))

    snapshot = transaction.read(
        TARGET,
        "ci",
        controller="CI-BOT",
    )

    assert snapshot.target is TARGET
    assert snapshot.name == "ci"
    assert snapshot.controller == "ci-bot"
    assert snapshot.comment_id == 7
    assert snapshot.url == f"{ISSUE_URL}#issuecomment-7"
    assert snapshot.revision == 3
    assert snapshot.data == {"status": "old"}
    assert snapshot.data_schema == _schema()
    assert snapshot.renderer.kind == "builtin-list"
    assert len(snapshot.state_sha256) == 64
    assert reader.calls[0] == ("ACTOR", HOST)


@pytest.mark.parametrize(
    ("schema", "replace_schema", "expected_schema"),
    [
        (KEEP_SCHEMA, False, None),
        (CLEAR_SCHEMA, True, None),
        (
            {"type": "object", "additionalProperties": True},
            True,
            validate_schema({"type": "object", "additionalProperties": True}),
        ),
    ],
)
def test_mutate_transforms_once_and_pins_the_initial_revision(
    schema: SchemaMutation,
    replace_schema: bool,
    expected_schema: SchemaSnapshotV1 | None,
) -> None:
    transaction, _, applier = _transaction(body=_body(schema=_schema()))
    snapshots: list[MutationSnapshot] = []

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        snapshots.append(snapshot)
        return MutationDraft(
            data={"status": "new"},
            schema=schema,
        )

    result = transaction.mutate(
        MutationRequest(
            target=TARGET,
            name="ci",
            transform=transform,
            controller="CI-BOT",
            dry_run=True,
        )
    )

    assert result.action == "updated"
    assert len(snapshots) == 1
    assert len(applier.calls) == 1
    request = applier.calls[0]
    assert request.target is TARGET
    assert request.name == "ci"
    assert request.mode == "update"
    assert request.data == {"status": "new"}
    assert request.data_schema == expected_schema
    assert request.replace_schema is replace_schema
    assert request.renderer is None
    assert request.controller == "ci-bot"
    assert request.if_revision == 3
    assert request.dry_run is True


def test_mutate_preserves_unchanged_result_without_replaying_transform() -> None:
    applier = FakeApplier(action="unchanged")
    transaction, _, _ = _transaction(applier=applier)
    transform_calls = 0

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        nonlocal transform_calls
        transform_calls += 1
        return MutationDraft(data=snapshot.data)

    result = transaction.mutate(
        MutationRequest(
            target=TARGET,
            name="ci",
            transform=transform,
        )
    )

    assert result.action == "unchanged"
    assert transform_calls == 1
    assert len(applier.calls) == 1


def test_explicit_stale_revision_fails_before_transform_or_apply() -> None:
    transaction, _, applier = _transaction()
    transform_calls = 0

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        nonlocal transform_calls
        transform_calls += 1
        return MutationDraft(data={"status": "new"})

    with pytest.raises(MutationError) as raised:
        transaction.mutate(
            MutationRequest(
                target=TARGET,
                name="ci",
                transform=transform,
                if_revision=2,
            )
        )

    assert raised.value.code == "revision_conflict"
    assert raised.value.exit_code is ExitCode.CONFLICT
    assert raised.value.details == {"expected": 2, "actual": 3}
    assert transform_calls == 0
    assert applier.calls == []


@pytest.mark.parametrize(
    "error",
    [
        ApplyError(
            "concurrent revision",
            code="revision_conflict",
            exit_code=ExitCode.CONFLICT,
        ),
        ApplyError(
            "unknown remote outcome",
            code="write_outcome_unknown",
        ),
    ],
)
def test_apply_conflict_or_unknown_never_replays_transform(
    error: ApplyError,
) -> None:
    applier = FakeApplier(error=error)
    transaction, _, _ = _transaction(applier=applier)
    transform_calls = 0

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        nonlocal transform_calls
        transform_calls += 1
        return MutationDraft(data={"status": "new"})

    with pytest.raises(ApplyError) as raised:
        transaction.mutate(
            MutationRequest(
                target=TARGET,
                name="ci",
                transform=transform,
            )
        )

    assert raised.value is error
    assert transform_calls == 1
    assert len(applier.calls) == 1
    assert applier.calls[0].if_revision == 3


@pytest.mark.parametrize(
    ("body_factory", "expected_code"),
    [
        (
            lambda: _body().replace("- old", "- manually edited"),
            "render_drift",
        ),
        (
            lambda: _body().replace("eAGr", "!AGr", 1),
            "slate_corrupt",
        ),
    ],
)
def test_invalid_remote_snapshot_fails_before_transform_or_apply(
    body_factory: Callable[[], str],
    expected_code: str,
) -> None:
    transaction, _, applier = _transaction(body=body_factory())
    transform_calls = 0

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        nonlocal transform_calls
        transform_calls += 1
        return MutationDraft(data={"status": "new"})

    with pytest.raises(GhSlateError) as raised:
        transaction.mutate(
            MutationRequest(
                target=TARGET,
                name="ci",
                transform=transform,
            )
        )

    assert raised.value.code == expected_code
    assert transform_calls == 0
    assert applier.calls == []


def test_duplicate_remote_snapshot_fails_before_transform_or_apply() -> None:
    reader = FakeReader(
        comments=[
            _record(7, _body()),
            _record(8, _body()),
        ]
    )
    applier = FakeApplier()
    transaction = MutationTransaction(reader=reader, applier=applier)
    transform_calls = 0

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        nonlocal transform_calls
        transform_calls += 1
        return MutationDraft(data={"status": "new"})

    with pytest.raises(GhSlateError) as raised:
        transaction.mutate(
            MutationRequest(
                target=TARGET,
                name="ci",
                transform=transform,
            )
        )

    assert raised.value.code == "duplicate_slate"
    assert transform_calls == 0
    assert applier.calls == []


def test_transform_failure_is_not_replayed_and_never_reaches_apply() -> None:
    transaction, _, applier = _transaction()
    transform_calls = 0
    failure = RuntimeError("editor failed")

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        nonlocal transform_calls
        transform_calls += 1
        raise failure

    with pytest.raises(RuntimeError) as raised:
        transaction.mutate(
            MutationRequest(
                target=TARGET,
                name="ci",
                transform=transform,
            )
        )

    assert raised.value is failure
    assert transform_calls == 1
    assert applier.calls == []


@pytest.mark.parametrize(
    "draft",
    [
        MutationDraft(
            data={"status": "new"},
            schema={"type": "not-a-json-schema-type"},
        ),
        MutationDraft(
            data={"status": 123},
            schema=KEEP_SCHEMA,
        ),
    ],
)
def test_schema_failure_never_reaches_apply(draft: MutationDraft) -> None:
    transaction, _, applier = _transaction(body=_body(schema=_schema()))
    transform_calls = 0

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        nonlocal transform_calls
        transform_calls += 1
        return draft

    with pytest.raises(SchemaError):
        transaction.mutate(
            MutationRequest(
                target=TARGET,
                name="ci",
                transform=transform,
            )
        )

    assert transform_calls == 1
    assert applier.calls == []


def test_invalid_transform_result_never_reaches_apply() -> None:
    transaction, _, applier = _transaction()

    def transform(snapshot: MutationSnapshot) -> object:
        return {"status": "new"}

    with pytest.raises(MutationError) as raised:
        transaction.mutate(
            MutationRequest(
                target=TARGET,
                name="ci",
                transform=cast("MutationTransform", transform),
            )
        )

    assert raised.value.code == "mutation_transform_result_invalid"
    assert applier.calls == []


def test_controller_must_remain_the_current_actor() -> None:
    transaction, reader, applier = _transaction()
    transform_calls = 0

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        nonlocal transform_calls
        transform_calls += 1
        return MutationDraft(data={"status": "new"})

    with pytest.raises(MutationError) as raised:
        transaction.mutate(
            MutationRequest(
                target=TARGET,
                name="ci",
                transform=transform,
                controller="other-bot",
            )
        )

    assert raised.value.code == "controller_conflict"
    assert transform_calls == 0
    assert applier.calls == []
    assert reader.calls == [("ACTOR", HOST)]
