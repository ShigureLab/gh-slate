from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol

from gh_slate.codec import validate_slate_name
from gh_slate.codec.marker import encode_marker, parse_marker
from gh_slate.errors import ExitCode, GhSlateError
from gh_slate.github.apply import ApplyReadClient, ApplyRequest
from gh_slate.github.models import GitHubActor
from gh_slate.github.store import CommentStore
from gh_slate.github.target import ResolvedTarget, target_from_comment_url
from gh_slate.github.write import GhWriteOutcomeUnknown
from gh_slate.rendering import render_state

if TYPE_CHECKING:
    from gh_slate.github.models import SlateCandidate

RecoveryAction = Literal["deleted", "repaired", "unchanged"]


class RecoveryWriteClient(Protocol):
    def patch(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> object: ...

    def delete(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
    ) -> object: ...


class RecoveryError(GhSlateError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        exit_code: ExitCode = ExitCode.RUNTIME,
        details: Mapping[str, object] | None = None,
        hints: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            message,
            code=code,
            exit_code=exit_code,
            details={} if details is None else details,
            hints=hints,
        )


def _validation_error(
    message: str,
    *,
    code: str,
    hints: tuple[str, ...] = (),
    **details: object,
) -> RecoveryError:
    return RecoveryError(
        message,
        code=code,
        exit_code=ExitCode.VALIDATION,
        details=details,
        hints=hints,
    )


def _conflict(
    message: str,
    *,
    code: str,
    hints: tuple[str, ...] = (),
    **details: object,
) -> RecoveryError:
    return RecoveryError(
        message,
        code=code,
        exit_code=ExitCode.CONFLICT,
        details=details,
        hints=hints,
    )


def validate_delete_confirmation(
    name: str,
    *,
    confirm: str | None,
    yes: bool,
) -> str:
    """Validate destructive intent without reading any local or remote context."""

    validated_name = validate_slate_name(name)
    if not isinstance(yes, bool):
        raise _validation_error(
            "delete yes flag must be a boolean",
            code="delete_confirmation_invalid",
        )
    if yes and confirm is not None:
        raise _validation_error(
            "choose either --confirm or --yes",
            code="delete_confirmation_conflict",
        )
    if yes:
        return validated_name
    if confirm is None:
        raise _validation_error(
            "deleting a slate requires its exact name or --yes",
            code="delete_confirmation_required",
            hints=(f"pass --confirm {validated_name} or --yes",),
        )
    if confirm != validated_name:
        raise _validation_error(
            "delete confirmation does not exactly match the slate name",
            code="delete_confirmation_mismatch",
            name=validated_name,
            confirmation=confirm,
        )
    return validated_name


@dataclass(frozen=True, slots=True)
class RepairRequest:
    target: ResolvedTarget
    name: str
    from_state: bool
    controller: str | None = None
    if_revision: int | None = None

    def __post_init__(self) -> None:
        validated = ApplyRequest(
            target=self.target,
            name=self.name,
            mode="update",
            controller=self.controller,
            if_revision=self.if_revision,
        )
        object.__setattr__(self, "name", validated.name)
        if self.from_state is not True:
            raise _validation_error(
                "repair requires the explicit --from-state source",
                code="repair_source_required",
                hints=("pass --from-state to discard visible Markdown edits",),
            )


@dataclass(frozen=True, slots=True)
class DeleteRequest:
    target: ResolvedTarget
    name: str
    confirm: str | None = None
    yes: bool = False
    controller: str | None = None
    if_revision: int | None = None

    def __post_init__(self) -> None:
        validated_name = validate_delete_confirmation(
            self.name,
            confirm=self.confirm,
            yes=self.yes,
        )
        validated = ApplyRequest(
            target=self.target,
            name=validated_name,
            mode="update",
            controller=self.controller,
            if_revision=self.if_revision,
        )
        object.__setattr__(self, "name", validated.name)


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    action: RecoveryAction
    name: str
    repository: str
    number: int
    comment_id: int
    url: str
    revision: int | None
    state_sha256: str | None
    recovered: bool = False

    def to_json(self) -> dict[str, object]:
        value: dict[str, object] = {
            "action": self.action,
            "name": self.name,
            "repository": self.repository,
            "number": self.number,
            "comment_id": self.comment_id,
            "url": self.url,
            "revision": self.revision,
            "state_sha256": self.state_sha256,
        }
        if self.recovered:
            value["recovered"] = True
        return value


def _controller(
    reader: ApplyReadClient,
    target: ResolvedTarget,
    requested: str | None,
) -> GitHubActor:
    actor = reader.current_actor(target.host)
    if not isinstance(actor, GitHubActor):
        raise RecoveryError(
            "current GitHub actor lookup returned an invalid identity",
            code="github_response_invalid",
        )
    if requested is not None:
        requested_actor = reader.resolve_actor(
            requested,
            target.host,
        )
        if not isinstance(requested_actor, GitHubActor):
            raise RecoveryError(
                "requested GitHub controller lookup returned an invalid identity",
                code="github_response_invalid",
            )
        if requested_actor.id != actor.id:
            raise _conflict(
                "requested controller is not the current authenticated actor",
                code="controller_conflict",
                requested=requested_actor.login,
                requested_id=requested_actor.id,
                current_actor=actor.login,
                current_actor_id=actor.id,
            )
    return actor


def _select_candidate(
    store: CommentStore,
    target: ResolvedTarget,
    *,
    name: str,
    controller: GitHubActor,
    require_state: bool,
) -> SlateCandidate:
    candidates = store.candidates(
        target,
        controller=controller,
        name=name,
    )
    if not candidates:
        raise RecoveryError(
            f"slate '{name}' was not found on the target",
            code="slate_not_found",
            exit_code=ExitCode.NOT_FOUND,
            details={"name": name},
        )
    if len(candidates) > 1:
        raise _conflict(
            f"multiple comments match slate '{name}'",
            code="duplicate_slate",
            hints=("inspect the matching comments and delete the duplicate intentionally before retrying",),
            name=name,
            comment_ids=[candidate.comment.id for candidate in candidates],
        )
    candidate = candidates[0]
    if require_state and candidate.decoded is None:
        raise _validation_error(
            f"slate '{name}' contains corrupt or unsupported state",
            code="slate_corrupt",
            hints=("inspect the raw comment and delete it with exact confirmation if it cannot be recovered",),
            name=name,
            comment_id=candidate.comment.id,
            cause_code=candidate.error_code,
        )
    return candidate


def _check_revision(
    *,
    expected: int | None,
    candidate: SlateCandidate,
) -> None:
    if expected is None:
        return
    actual = None if candidate.decoded is None else candidate.decoded.state.revision
    if actual != expected:
        raise _conflict(
            "stored revision does not match if_revision",
            code="revision_conflict",
            expected=expected,
            actual=actual,
        )


def _revalidate(
    store: CommentStore,
    target: ResolvedTarget,
    *,
    name: str,
    controller: GitHubActor,
    initial: SlateCandidate,
    require_state: bool,
) -> SlateCandidate:
    try:
        current = _select_candidate(
            store,
            target,
            name=name,
            controller=controller,
            require_state=require_state,
        )
    except GhSlateError as error:
        raise _conflict(
            "selected slate changed before the recovery write",
            code="concurrent_change",
            name=name,
            comment_id=initial.comment.id,
            cause_code=error.code,
        ) from None
    if current.comment.id != initial.comment.id or current.comment.body != initial.comment.body:
        raise _conflict(
            "selected slate changed before the recovery write",
            code="concurrent_change",
            name=name,
            expected_comment_id=initial.comment.id,
            actual_comment_id=current.comment.id,
        )
    return current


def _response_identifier(response: object) -> int | None:
    if not isinstance(response, Mapping):
        return None
    value = response.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        return None
    return result if result > 0 else None


def _result(
    action: RecoveryAction,
    request: RepairRequest | DeleteRequest,
    candidate: SlateCandidate,
    *,
    recovered: bool = False,
) -> RecoveryResult:
    decoded = candidate.decoded
    return RecoveryResult(
        action=action,
        name=request.name,
        repository=request.target.repository,
        number=request.target.number,
        comment_id=candidate.comment.id,
        url=candidate.comment.url,
        revision=(None if decoded is None else decoded.state.revision),
        state_sha256=(None if decoded is None else decoded.state_sha256),
        recovered=recovered,
    )


def _repair_body(
    target: ResolvedTarget,
    candidate: SlateCandidate,
) -> str:
    decoded = candidate.decoded
    assert decoded is not None
    target_from_comment_url(
        candidate.comment.url,
        expected=target,
        expected_comment_id=candidate.comment.id,
    )
    rendered = render_state(
        decoded.state,
    )
    if rendered.render_sha256 != decoded.expected_render_sha256:
        raise _validation_error(
            "stored state no longer renders to its recorded hash",
            code="rerender_mismatch",
            expected=decoded.expected_render_sha256,
            actual=rendered.render_sha256,
        )
    marker = parse_marker(candidate.comment.body)
    return encode_marker(
        replace(
            marker,
            visible=rendered.markdown,
        )
    )


def _verify_repair(
    store: CommentStore,
    request: RepairRequest,
    *,
    controller: GitHubActor,
    initial: SlateCandidate,
    intended_body: str,
    response_id: int | None,
    unknown_outcome: bool,
) -> SlateCandidate:
    try:
        candidate = _select_candidate(
            store,
            request.target,
            name=request.name,
            controller=controller,
            require_state=True,
        )
    except Exception as error:
        raise RecoveryError(
            "the repair was sent, but its remote outcome could not be determined",
            code="repair_outcome_unknown",
            details={
                "name": request.name,
                "reason": f"refetch_failed:{type(error).__name__}",
            },
            hints=("inspect the slate before attempting another mutation",),
        ) from None
    decoded = candidate.decoded
    initial_decoded = initial.decoded
    assert decoded is not None
    assert initial_decoded is not None
    verified = (
        candidate.comment.id == initial.comment.id
        and candidate.comment.body == intended_body
        and decoded.state_sha256 == initial_decoded.state_sha256
        and not decoded.drifted
        and (response_id is None or response_id == candidate.comment.id)
    )
    if verified:
        return candidate
    if unknown_outcome:
        raise RecoveryError(
            "the repair write failed and its remote outcome could not be determined",
            code="repair_outcome_unknown",
            details={"name": request.name},
            hints=("inspect the slate before attempting another mutation",),
        )
    raise _conflict(
        "GitHub did not retain the intended repaired projection",
        code="post_repair_verification_failed",
        name=request.name,
        comment_id=initial.comment.id,
    )


def _verify_deleted(
    store: CommentStore,
    request: DeleteRequest,
    *,
    initial: SlateCandidate,
    unknown_outcome: bool,
) -> None:
    try:
        comments = store.comments(request.target)
    except Exception as error:
        raise RecoveryError(
            "the delete was sent, but its remote outcome could not be determined",
            code="delete_outcome_unknown",
            details={
                "name": request.name,
                "reason": f"refetch_failed:{type(error).__name__}",
            },
            hints=("inspect the target before attempting another delete",),
        ) from None
    if all(comment.id != initial.comment.id for comment in comments):
        return
    if unknown_outcome:
        raise RecoveryError(
            "the delete write failed and its remote outcome could not be determined",
            code="delete_outcome_unknown",
            details={
                "name": request.name,
                "comment_id": initial.comment.id,
            },
            hints=("inspect the target before attempting another delete",),
        )
    raise _conflict(
        "GitHub retained the selected comment after delete",
        code="post_delete_verification_failed",
        name=request.name,
        comment_id=initial.comment.id,
    )


@dataclass(frozen=True, slots=True)
class RecoveryTransaction:
    reader: ApplyReadClient
    writer: RecoveryWriteClient

    def repair(self, request: RepairRequest) -> RecoveryResult:
        if not isinstance(request, RepairRequest):
            raise _validation_error(
                "repair requires a RepairRequest",
                code="repair_request_invalid",
            )
        controller = _controller(
            self.reader,
            request.target,
            request.controller,
        )
        store = CommentStore(self.reader)
        initial = _select_candidate(
            store,
            request.target,
            name=request.name,
            controller=controller,
            require_state=True,
        )
        _check_revision(
            expected=request.if_revision,
            candidate=initial,
        )
        intended_body = _repair_body(
            request.target,
            initial,
        )
        if intended_body == initial.comment.body:
            return _result(
                "unchanged",
                request,
                initial,
            )

        _revalidate(
            store,
            request.target,
            name=request.name,
            controller=controller,
            initial=initial,
            require_state=True,
        )
        try:
            response = self.writer.patch(
                f"repos/{request.target.repository}/issues/comments/{initial.comment.id}",
                {"body": intended_body},
                hostname=request.target.host,
            )
        except GhWriteOutcomeUnknown:
            verified = _verify_repair(
                store,
                request,
                controller=controller,
                initial=initial,
                intended_body=intended_body,
                response_id=None,
                unknown_outcome=True,
            )
            return _result(
                "repaired",
                request,
                verified,
                recovered=True,
            )
        verified = _verify_repair(
            store,
            request,
            controller=controller,
            initial=initial,
            intended_body=intended_body,
            response_id=_response_identifier(response),
            unknown_outcome=False,
        )
        return _result(
            "repaired",
            request,
            verified,
        )

    def delete(self, request: DeleteRequest) -> RecoveryResult:
        if not isinstance(request, DeleteRequest):
            raise _validation_error(
                "delete requires a DeleteRequest",
                code="delete_request_invalid",
            )
        # DeleteRequest validates the exact destructive confirmation in
        # __post_init__, before this method can perform an actor or comment read.
        controller = _controller(
            self.reader,
            request.target,
            request.controller,
        )
        store = CommentStore(self.reader)
        initial = _select_candidate(
            store,
            request.target,
            name=request.name,
            controller=controller,
            require_state=False,
        )
        _check_revision(
            expected=request.if_revision,
            candidate=initial,
        )
        _revalidate(
            store,
            request.target,
            name=request.name,
            controller=controller,
            initial=initial,
            require_state=False,
        )
        try:
            self.writer.delete(
                f"repos/{request.target.repository}/issues/comments/{initial.comment.id}",
                hostname=request.target.host,
            )
        except GhWriteOutcomeUnknown:
            _verify_deleted(
                store,
                request,
                initial=initial,
                unknown_outcome=True,
            )
            return _result(
                "deleted",
                request,
                initial,
                recovered=True,
            )
        _verify_deleted(
            store,
            request,
            initial=initial,
            unknown_outcome=False,
        )
        return _result(
            "deleted",
            request,
            initial,
        )


def repair(
    request: RepairRequest,
    *,
    reader: ApplyReadClient,
    writer: RecoveryWriteClient,
) -> RecoveryResult:
    return RecoveryTransaction(
        reader=reader,
        writer=writer,
    ).repair(request)


def delete(
    request: DeleteRequest,
    *,
    reader: ApplyReadClient,
    writer: RecoveryWriteClient,
) -> RecoveryResult:
    return RecoveryTransaction(
        reader=reader,
        writer=writer,
    ).delete(request)


__all__ = [
    "DeleteRequest",
    "RecoveryAction",
    "RecoveryError",
    "RecoveryResult",
    "RecoveryTransaction",
    "RecoveryWriteClient",
    "RepairRequest",
    "delete",
    "repair",
    "validate_delete_confirmation",
]
