from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, TypeAlias, cast

from gh_slate.codec import (
    RendererDescriptorV1,
    SchemaSnapshotV1,
    StateV1,
)
from gh_slate.errors import ExitCode
from gh_slate.github.apply import (
    ApplyError,
    ApplyReadClient,
    ApplyRequest,
    ApplyResult,
    ApplyTransaction,
    ApplyWriteClient,
)
from gh_slate.github.models import GitHubActor
from gh_slate.github.store import CommentStore
from gh_slate.schema import validate_data, validate_schema

if TYPE_CHECKING:
    from gh_slate.github.models import GitHubComment
    from gh_slate.github.target import ResolvedTarget


class SchemaDirective(Enum):
    """An explicit instruction for the stored schema during a mutation."""

    KEEP = "keep"
    CLEAR = "clear"


KEEP_SCHEMA = SchemaDirective.KEEP
CLEAR_SCHEMA = SchemaDirective.CLEAR

SchemaMutation: TypeAlias = SchemaDirective | SchemaSnapshotV1 | bool | Mapping[str, object]


@dataclass(frozen=True, slots=True)
class MutationSnapshot:
    """The one immutable remote snapshot supplied to a local transform."""

    target: ResolvedTarget
    name: str
    controller: GitHubActor
    comment: GitHubComment
    state: StateV1
    state_sha256: str

    @property
    def comment_id(self) -> int:
        return self.comment.id

    @property
    def url(self) -> str:
        return self.comment.url

    @property
    def revision(self) -> int:
        return self.state.revision

    @property
    def data(self) -> Mapping[str, object]:
        return self.state.data

    @property
    def data_schema(self) -> SchemaSnapshotV1 | None:
        return self.state.data_schema

    @property
    def renderer(self) -> RendererDescriptorV1:
        return self.state.renderer


@dataclass(frozen=True, slots=True)
class MutationDraft:
    """A full data replacement plus an explicit schema disposition."""

    data: object
    schema: SchemaMutation = KEEP_SCHEMA


class MutationTransform(Protocol):
    def __call__(
        self,
        snapshot: MutationSnapshot,
        /,
    ) -> MutationDraft: ...


@dataclass(frozen=True, slots=True)
class MutationRequest:
    target: ResolvedTarget
    name: str
    transform: MutationTransform
    controller: str | None = None
    if_revision: int | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        validated = ApplyRequest(
            target=self.target,
            name=self.name,
            mode="update",
            controller=self.controller,
            if_revision=self.if_revision,
            dry_run=self.dry_run,
        )
        object.__setattr__(self, "name", validated.name)
        if not callable(self.transform):
            raise MutationError(
                "mutation transform must be callable",
                code="mutation_transform_invalid",
                exit_code=ExitCode.VALIDATION,
            )


class MutationApplier(Protocol):
    def apply(self, request: ApplyRequest) -> ApplyResult: ...


class MutationError(ApplyError):
    """A mutation orchestration error raised before the B6 apply boundary."""


def _current_controller(
    reader: ApplyReadClient,
    target: ResolvedTarget,
    requested: str | None,
) -> GitHubActor:
    actor = reader.current_actor(target.host)
    if not isinstance(actor, GitHubActor):
        raise MutationError(
            "current GitHub actor lookup returned an invalid identity",
            code="github_response_invalid",
        )
    if requested is not None:
        requested_actor = reader.resolve_actor(
            requested,
            target.host,
        )
        if not isinstance(requested_actor, GitHubActor):
            raise MutationError(
                "requested GitHub controller lookup returned an invalid identity",
                code="github_response_invalid",
            )
        if requested_actor.id != actor.id:
            raise MutationError(
                "requested controller is not the current authenticated actor",
                code="controller_conflict",
                exit_code=ExitCode.CONFLICT,
                details={
                    "requested": requested_actor.login,
                    "requested_id": requested_actor.id,
                    "current_actor": actor.login,
                    "current_actor_id": actor.id,
                },
            )
    return actor


def _read_snapshot(
    reader: ApplyReadClient,
    *,
    target: ResolvedTarget,
    name: str,
    controller: str | None,
) -> MutationSnapshot:
    validation = ApplyRequest(
        target=target,
        name=name,
        mode="update",
        controller=controller,
    )
    actor = _current_controller(
        reader,
        validation.target,
        controller,
    )
    managed = CommentStore(reader).find(
        validation.target,
        validation.name,
        controller=actor,
    )
    if managed.decoded.drifted:
        raise MutationError(
            f"slate '{validation.name}' has visible Markdown drift",
            code="render_drift",
            exit_code=ExitCode.CONFLICT,
            details={
                "name": validation.name,
                "comment_id": managed.comment.id,
                "expected": managed.decoded.expected_render_sha256,
                "actual": managed.decoded.actual_render_sha256,
            },
            hints=("run repair --from-state to restore the projection, or edit canonical typed data instead",),
        )
    return MutationSnapshot(
        target=validation.target,
        name=validation.name,
        controller=actor,
        comment=managed.comment,
        state=managed.decoded.state,
        state_sha256=managed.decoded.state_sha256,
    )


def _schema_for_apply(
    snapshot: MutationSnapshot,
    schema: object,
) -> tuple[
    SchemaSnapshotV1 | None,
    SchemaSnapshotV1 | None,
    bool,
]:
    if schema is KEEP_SCHEMA:
        effective = snapshot.data_schema
        return effective, None, False
    if schema is CLEAR_SCHEMA:
        return None, None, True
    if isinstance(schema, SchemaSnapshotV1):
        validated = validate_schema(
            schema.document,
            dialect=schema.dialect,
        )
        return validated, validated, True
    if isinstance(schema, (bool, Mapping)):
        validated = validate_schema(
            cast("bool | Mapping[str, object]", schema),
        )
        return validated, validated, True
    raise MutationError(
        "mutation schema must be KEEP_SCHEMA, CLEAR_SCHEMA, or a JSON Schema",
        code="mutation_schema_invalid",
        exit_code=ExitCode.VALIDATION,
    )


def _revision_conflict(
    *,
    expected: int,
    actual: int,
) -> MutationError:
    return MutationError(
        "stored revision does not match if_revision",
        code="revision_conflict",
        exit_code=ExitCode.CONFLICT,
        details={
            "expected": expected,
            "actual": actual,
        },
    )


@dataclass(frozen=True, slots=True)
class MutationTransaction:
    """Read once, transform once, then delegate one pinned update to B6."""

    reader: ApplyReadClient
    applier: MutationApplier

    def read(
        self,
        target: ResolvedTarget,
        name: str,
        *,
        controller: str | None = None,
    ) -> MutationSnapshot:
        return _read_snapshot(
            self.reader,
            target=target,
            name=name,
            controller=controller,
        )

    def mutate(self, request: MutationRequest) -> ApplyResult:
        if not isinstance(request, MutationRequest):
            raise MutationError(
                "mutation requires a MutationRequest",
                code="mutation_request_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        snapshot = self.read(
            request.target,
            request.name,
            controller=request.controller,
        )
        if request.if_revision is not None and request.if_revision != snapshot.revision:
            raise _revision_conflict(
                expected=request.if_revision,
                actual=snapshot.revision,
            )

        draft = request.transform(snapshot)
        if not isinstance(draft, MutationDraft):
            raise MutationError(
                "mutation transform must return a MutationDraft",
                code="mutation_transform_result_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        effective_schema, apply_schema, replace_schema = _schema_for_apply(
            snapshot,
            draft.schema,
        )
        data = validate_data(
            draft.data,
            effective_schema,
        )
        apply_request = ApplyRequest(
            target=snapshot.target,
            name=snapshot.name,
            mode="update",
            data=data,
            data_schema=apply_schema,
            replace_schema=replace_schema,
            controller=snapshot.controller,
            if_revision=snapshot.revision,
            dry_run=request.dry_run,
        )
        return self.applier.apply(apply_request)


def mutate(
    request: MutationRequest,
    *,
    reader: ApplyReadClient,
    writer: ApplyWriteClient,
) -> ApplyResult:
    """Execute a mutation with the production B6 apply transaction."""

    return MutationTransaction(
        reader=reader,
        applier=ApplyTransaction(
            reader=reader,
            writer=writer,
        ),
    ).mutate(request)


__all__ = [
    "CLEAR_SCHEMA",
    "KEEP_SCHEMA",
    "MutationApplier",
    "MutationDraft",
    "MutationError",
    "MutationRequest",
    "MutationSnapshot",
    "MutationTransaction",
    "MutationTransform",
    "SchemaDirective",
    "SchemaMutation",
    "mutate",
]
