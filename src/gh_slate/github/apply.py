from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol, cast

from gh_slate.codec import (
    ControllerV1,
    MetaSnapshot,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    State,
    StateDraft,
    canonical_json_bytes,
    encode_comment,
    resolve_revision,
    validate_slate_name,
)
from gh_slate.codec.model import MAX_REVISION, STATE_FORMAT_V1, STATE_FORMAT_V2
from gh_slate.errors import ExitCode, GhSlateError
from gh_slate.github.models import GitHubActor
from gh_slate.github.store import CommentStore
from gh_slate.github.target import (
    ResolvedTarget,
    resolve_target,
    target_from_comment_url,
)
from gh_slate.github.write import GhWriteOutcomeUnknown, GhWriteTimeout
from gh_slate.patching import apply_data_patch, data_diff, prepare_patch
from gh_slate.rendering import SlateContext, render
from gh_slate.rendering.engine import migration_required
from gh_slate.rendering.routing import selected_view

if TYPE_CHECKING:
    from gh_slate.github.models import GitHubComment, SlateCandidate

ApplyMode = Literal["create", "update", "upsert"]
ApplyAction = Literal["created", "updated", "unchanged"]
RecoveryKind = Literal["timeout", "ambiguous"]
SchemaInput = SchemaSnapshotV1 | bool | Mapping[str, object] | None


class ApplyReadClient(Protocol):
    def api_get(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
        paginate: bool = False,
    ) -> object: ...

    def current_actor(self, hostname: str | None = None) -> GitHubActor: ...

    def resolve_actor(
        self,
        login: str,
        hostname: str | None = None,
    ) -> GitHubActor: ...


class ApplyWriteClient(Protocol):
    def post(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> object: ...

    def patch(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> object: ...


class ApplyError(GhSlateError):
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


@dataclass(frozen=True, slots=True)
class ApplyRequest:
    """One complete desired snapshot.

    ``None`` data and renderer values reuse the stored component. New states
    default to an empty data object and require a renderer. A non-null schema
    always replaces the stored schema; ``replace_schema=True`` distinguishes an
    explicit schema removal from omission.
    """

    target: ResolvedTarget
    name: str
    mode: ApplyMode = "upsert"
    data: object | None = None
    patch: object | None = None
    data_schema: SchemaInput = None
    replace_schema: bool = False
    renderer: RendererDescriptorV1 | None = None
    controller: str | GitHubActor | None = None
    if_revision: int | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.target, ResolvedTarget):
            raise ApplyError(
                "apply target must be a resolved GitHub target",
                code="apply_target_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        object.__setattr__(self, "name", validate_slate_name(self.name))
        if self.mode not in {"create", "update", "upsert"}:
            raise ApplyError(
                "apply mode must be create, update, or upsert",
                code="apply_mode_invalid",
                exit_code=ExitCode.VALIDATION,
                details={"mode": self.mode},
            )
        if (
            self.controller is not None
            and not isinstance(
                self.controller,
                GitHubActor,
            )
            and (
                not isinstance(self.controller, str)
                or not self.controller
                or self.controller != self.controller.strip()
                or any(character.isspace() or ord(character) < 0x20 for character in self.controller)
            )
        ):
            raise ApplyError(
                "controller login must not be empty",
                code="controller_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        if self.if_revision is not None and (
            isinstance(self.if_revision, bool)
            or not isinstance(self.if_revision, int)
            or not 1 <= self.if_revision <= MAX_REVISION
        ):
            raise ApplyError(
                "if_revision must be a positive integer",
                code="revision_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        if not isinstance(self.replace_schema, bool):
            raise ApplyError(
                "replace_schema must be a boolean",
                code="apply_request_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        if not isinstance(self.dry_run, bool):
            raise ApplyError(
                "dry_run must be a boolean",
                code="apply_request_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        if self.patch is not None:
            if self.data is not None:
                raise ApplyError(
                    "data and patch are mutually exclusive", code="patch_input_conflict", exit_code=ExitCode.VALIDATION
                )
            if self.if_revision is None:
                raise ApplyError(
                    "patch requires if_revision", code="patch_revision_required", exit_code=ExitCode.VALIDATION
                )
            if self.mode == "create":
                raise ApplyError(
                    "patch requires an existing slate", code="patch_existing_required", exit_code=ExitCode.VALIDATION
                )
            object.__setattr__(self, "patch", prepare_patch(self.patch))
        if self.renderer is not None and not isinstance(
            self.renderer,
            RendererDescriptorV1,
        ):
            raise ApplyError(
                "renderer must be a RendererDescriptorV1",
                code="apply_request_invalid",
                exit_code=ExitCode.VALIDATION,
            )


@dataclass(frozen=True, slots=True)
class ApplyResult:
    action: ApplyAction
    name: str
    repository: str
    number: int
    comment_id: int | None
    url: str | None
    revision: int
    state_sha256: str
    dry_run: bool = False
    recovered: bool = False
    markdown: str | None = None
    meta_source: str | None = None
    view: str | None = None
    preview: Mapping[str, object] | None = None

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
        if self.dry_run:
            value.update(
                {
                    "dry_run": True,
                    "markdown": self.markdown,
                }
            )
        if self.dry_run and self.preview is not None:
            value.update(self.preview)
        if self.view is not None:
            value["view"] = self.view
        if self.meta_source is not None:
            value["meta_source"] = self.meta_source
        if self.recovered:
            value["recovered"] = True
        return value


@dataclass(frozen=True, slots=True)
class _Existing:
    candidate: SlateCandidate

    @property
    def comment(self) -> GitHubComment:
        return self.candidate.comment

    @property
    def state(self) -> State:
        decoded = self.candidate.decoded
        assert decoded is not None
        return decoded.state

    @property
    def state_sha256(self) -> str:
        decoded = self.candidate.decoded
        assert decoded is not None
        return decoded.state_sha256


def _conflict(
    message: str,
    *,
    code: str,
    hints: tuple[str, ...] = (),
    **details: object,
) -> ApplyError:
    return ApplyError(
        message,
        code=code,
        exit_code=ExitCode.CONFLICT,
        details=details,
        hints=hints,
    )


def _controller(
    request: ApplyRequest,
    reader: ApplyReadClient,
) -> GitHubActor:
    actor = reader.current_actor(request.target.host)
    if not isinstance(actor, GitHubActor):
        raise ApplyError(
            "current GitHub actor lookup returned an invalid identity",
            code="github_response_invalid",
        )
    if request.controller is not None:
        requested = (
            request.controller
            if isinstance(request.controller, GitHubActor)
            else reader.resolve_actor(
                request.controller,
                request.target.host,
            )
        )
        if not isinstance(requested, GitHubActor):
            raise ApplyError(
                "requested GitHub controller lookup returned an invalid identity",
                code="github_response_invalid",
            )
        if requested.id != actor.id:
            raise _conflict(
                "requested controller is not the current authenticated actor",
                code="controller_conflict",
                requested=requested.login,
                requested_id=requested.id,
                current_actor=actor.login,
                current_actor_id=actor.id,
            )
    return actor


def _read_existing(
    store: CommentStore,
    request: ApplyRequest,
    *,
    controller: GitHubActor,
) -> _Existing | None:
    candidates = store.candidates(
        request.target,
        controller=controller,
        name=request.name,
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        raise _conflict(
            f"multiple comments match slate '{request.name}'",
            code="duplicate_slate",
            hints=("inspect the matching comments and delete the duplicate intentionally before retrying",),
            name=request.name,
            comment_ids=[candidate.comment.id for candidate in candidates],
        )
    candidate = candidates[0]
    if candidate.decoded is None:
        raise ApplyError(
            f"slate '{request.name}' contains corrupt or unsupported state",
            code="slate_corrupt",
            exit_code=ExitCode.VALIDATION,
            details={
                "name": request.name,
                "comment_id": candidate.comment.id,
                "cause_code": candidate.error_code,
            },
            hints=("inspect the raw comment and delete it with exact confirmation if it cannot be recovered",),
        )
    if candidate.decoded.drifted:
        raise _conflict(
            f"slate '{request.name}' has visible Markdown drift",
            code="render_drift",
            hints=("run repair --from-state to restore the projection, or edit canonical typed data instead",),
            name=request.name,
            comment_id=candidate.comment.id,
            expected=candidate.decoded.expected_render_sha256,
            actual=candidate.decoded.actual_render_sha256,
        )
    return _Existing(candidate)


def _check_mode_and_revision(
    request: ApplyRequest,
    existing: _Existing | None,
) -> None:
    if request.mode == "create" and existing is not None:
        raise _conflict(
            f"slate '{request.name}' already exists",
            code="slate_already_exists",
            name=request.name,
            comment_id=existing.comment.id,
        )
    if request.mode == "update" and existing is None:
        raise ApplyError(
            f"slate '{request.name}' was not found on the target",
            code="slate_not_found",
            exit_code=ExitCode.NOT_FOUND,
            details={"name": request.name},
        )
    if request.patch is not None and existing is None:
        raise ApplyError(
            "patch requires an existing slate", code="patch_existing_required", exit_code=ExitCode.NOT_FOUND
        )
    if request.if_revision is None:
        return
    if existing is None or existing.state.revision != request.if_revision:
        raise _conflict(
            "stored revision does not match if_revision",
            code="revision_conflict",
            expected=request.if_revision,
            actual=(None if existing is None else existing.state.revision),
        )


def _canonical_new_target(
    request: ApplyRequest,
    reader: ApplyReadClient,
) -> tuple[ResolvedTarget, Mapping[str, object]]:
    response = reader.api_get(
        f"repos/{request.target.repository}/issues/{request.target.number}",
        hostname=request.target.host,
    )
    if not isinstance(response, Mapping):
        raise ApplyError(
            "GitHub returned invalid target metadata",
            code="github_response_invalid",
            details={"field": "target"},
        )
    html_url = cast("Mapping[str, object]", response).get("html_url")
    if not isinstance(html_url, str):
        raise ApplyError(
            "GitHub target metadata is missing html_url",
            code="github_response_invalid",
            details={"field": "target.html_url"},
        )
    canonical = resolve_target(
        html_url,
        repo=request.target.repository,
        host=request.target.host,
        environ={},
    )
    if canonical.number != request.target.number:
        raise ApplyError(
            "GitHub target metadata disagrees with the requested number",
            code="github_response_invalid",
            details={
                "expected_number": request.target.number,
                "actual_number": canonical.number,
            },
        )
    return canonical, cast("Mapping[str, object]", response)


def _canonical_context(
    request: ApplyRequest,
    reader: ApplyReadClient,
    existing: _Existing | None,
) -> tuple[ResolvedTarget, SlateContext, MetaSnapshot | None]:
    renderer = (
        request.renderer if request.renderer is not None else (None if existing is None else existing.state.renderer)
    )
    v2 = renderer is not None and renderer.kind == "jinja" and renderer.version == 2
    meta = None
    if existing is None or v2:
        canonical, response = _canonical_new_target(request, reader)
        if v2:
            node_id = response.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise ApplyError("GitHub target metadata is missing node_id", code="github_response_invalid")
            owner, name = canonical.repository.split("/", 1)
            repository_url = canonical.url.rsplit("/", 2)[0]
            meta = MetaSnapshot(
                {
                    "host": canonical.host,
                    "repository": {
                        "owner": owner,
                        "name": name,
                        "full_name": canonical.repository,
                        "url": repository_url,
                    },
                    "target": {
                        "kind": "pull_request" if canonical.url.rsplit("/", 2)[-2] == "pull" else "issue",
                        "number": canonical.number,
                        "id": node_id,
                        "url": canonical.url,
                    },
                    "slate": {"name": request.name},
                }
            )
            if (
                existing is not None
                and existing.state.meta is not None
                and (existing.state.meta.target_id != node_id or existing.state.meta.value["host"] != canonical.host)
            ):
                raise ApplyError(
                    "stored metadata belongs to a different GitHub target", code="target_identity_mismatch"
                )
    else:
        canonical = target_from_comment_url(existing.comment.url, expected=request.target)
    return (
        canonical,
        SlateContext(name=request.name, repository=canonical.repository, number=canonical.number, url=canonical.url),
        meta,
    )


def _desired_state(
    request: ApplyRequest,
    existing: _Existing | None,
    *,
    controller: GitHubActor,
    context: SlateContext,
    meta: MetaSnapshot | None,
) -> tuple[State, str, str, str, bool]:
    previous = None if existing is None else existing.state
    if previous is not None and previous.meta is None and request.renderer is None:
        raise migration_required()
    if previous is None:
        data = {} if request.data is None else request.data
        renderer = request.renderer
        schema = request.data_schema
        if renderer is None:
            raise ApplyError(
                "creating a slate requires a renderer",
                code="renderer_required",
                exit_code=ExitCode.VALIDATION,
                hints=("pass --profile or --template",),
            )
        reuse_renderer = False
        reuse_schema = False
    else:
        data = previous.data if request.data is None else request.data
        renderer = previous.renderer if request.renderer is None else request.renderer
        replacing_schema = request.replace_schema or request.data_schema is not None
        schema = request.data_schema if replacing_schema else previous.data_schema
        reuse_renderer = request.renderer is None
        reuse_schema = not replacing_schema

    if request.patch is not None:
        assert previous is not None
        data = apply_data_patch(previous.data, request.patch)
    assert renderer is not None
    if previous is not None and previous.meta is not None and meta is None:
        raise ApplyError("a V2 slate requires a jinja@2 template", code="renderer_unsupported")
    stored_controller = ControllerV1(
        login=controller.login,
        id=controller.id,
    )
    rendered = render(
        data,
        renderer,
        schema=schema,
        slate=context,
        meta=meta,
    )
    stored_schema = previous.data_schema if previous is not None and reuse_schema else rendered.data_schema
    draft = StateDraft(
        name=request.name,
        format=STATE_FORMAT_V2 if meta is not None else STATE_FORMAT_V1,
        meta=meta,
        controller=stored_controller,
        data=rendered.data,
        data_schema=stored_schema,
        renderer=(renderer if reuse_renderer else rendered.renderer),
        render_sha256=rendered.render_sha256,
    )
    revision = resolve_revision(draft, previous=previous)
    encoded = encode_comment(revision.state, rendered.markdown)
    return (
        revision.state,
        rendered.markdown,
        encoded.body,
        encoded.state_sha256,
        revision.changed,
    )


def _revalidate_before_write(
    store: CommentStore,
    request: ApplyRequest,
    *,
    controller: GitHubActor,
    initial: _Existing | None,
) -> None:
    current = _read_existing(
        store,
        request,
        controller=controller,
    )
    if initial is None:
        if current is not None:
            raise _conflict(
                "slate appeared before the create write",
                code="concurrent_change",
                name=request.name,
                comment_id=current.comment.id,
            )
        return
    if current is None:
        raise _conflict(
            "selected slate disappeared before the update write",
            code="concurrent_change",
            name=request.name,
            comment_id=initial.comment.id,
        )
    if current.comment.id != initial.comment.id or current.state_sha256 != initial.state_sha256:
        raise _conflict(
            "selected slate changed before the update write",
            code="concurrent_change",
            name=request.name,
            expected_comment_id=initial.comment.id,
            actual_comment_id=current.comment.id,
            expected_state_sha256=initial.state_sha256,
            actual_state_sha256=current.state_sha256,
        )


def _response_identifier(response: object) -> int | None:
    if not isinstance(response, Mapping):
        return None
    value = cast("Mapping[str, object]", response).get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        return None
    return result if result > 0 else None


def _verification_error(
    request: ApplyRequest,
    *,
    recovery_kind: RecoveryKind | None,
    unknown: bool,
    reason: str,
) -> ApplyError:
    if recovery_kind is None and unknown:
        return ApplyError(
            "the write was sent, but its remote outcome could not be determined",
            code="post_write_verification_unknown",
            details={"name": request.name, "reason": reason},
            hints=("inspect the slate before attempting another mutation",),
        )
    if recovery_kind is not None and unknown:
        timeout = recovery_kind == "timeout"
        return ApplyError(
            (
                "write timed out and its remote outcome could not be determined"
                if timeout
                else "the write response failed and its remote outcome could not be determined"
            ),
            code=("write_timeout_unknown" if timeout else "write_outcome_unknown"),
            details={"name": request.name, "reason": reason},
            hints=("inspect the slate before attempting another mutation",),
        )
    if recovery_kind is not None:
        timeout = recovery_kind == "timeout"
        return _conflict(
            (
                "write timed out and the refetched slate conflicts with the intended state"
                if timeout
                else "the write response failed and the refetched slate conflicts with the intended state"
            ),
            code=("write_timeout_conflict" if timeout else "write_outcome_conflict"),
            name=request.name,
            reason=reason,
        )
    return _conflict(
        "GitHub did not retain the intended state after the write",
        code="post_write_verification_failed",
        name=request.name,
        reason=reason,
    )


def _verify_remote(
    store: CommentStore,
    request: ApplyRequest,
    *,
    controller: GitHubActor,
    intended_state_sha256: str,
    expected_comment_id: int | None,
    response_id: int | None,
    recovery_kind: RecoveryKind | None,
    previous_state_sha256: str | None = None,
) -> _Existing:
    try:
        candidates = store.candidates(
            request.target,
            controller=controller,
            name=request.name,
        )
    except Exception as error:
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            unknown=True,
            reason=f"refetch_failed:{type(error).__name__}",
        ) from None
    except (KeyboardInterrupt, SystemExit) as error:
        if recovery_kind is None:
            raise
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            unknown=True,
            reason=f"refetch_failed:{type(error).__name__}",
        ) from None
    if not candidates:
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            # A successful create can be temporarily absent from GitHub's
            # paginated reads even when POST returned a comment id. Keep the
            # outcome unknown so an immediate upsert cannot race into a second
            # create. Existing updates retain their conflict classification.
            unknown=expected_comment_id is None,
            reason="slate_missing",
        )
    if len(candidates) != 1:
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            unknown=False,
            reason="duplicate_slate",
        )
    candidate = candidates[0]
    if candidate.decoded is None:
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            unknown=False,
            reason="slate_corrupt",
        )
    if candidate.decoded.drifted:
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            unknown=False,
            reason="render_drift",
        )
    if expected_comment_id is not None and candidate.comment.id != expected_comment_id:
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            unknown=False,
            reason="comment_id_changed",
        )
    if response_id is not None and candidate.comment.id != response_id:
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            unknown=False,
            reason="write_response_id_mismatch",
        )
    if candidate.decoded.state_sha256 != intended_state_sha256:
        raise _verification_error(
            request,
            recovery_kind=recovery_kind,
            unknown=(
                recovery_kind is not None
                and previous_state_sha256 is not None
                and candidate.decoded.state_sha256 == previous_state_sha256
            ),
            reason=(
                "previous_state_still_visible"
                if previous_state_sha256 is not None and candidate.decoded.state_sha256 == previous_state_sha256
                else "state_hash_mismatch"
            ),
        )
    return _Existing(candidate)


def _verify_post_write(
    store: CommentStore,
    request: ApplyRequest,
    *,
    controller: GitHubActor,
    intended_state_sha256: str,
    expected_comment_id: int | None,
    response: object,
    previous_state_sha256: str | None = None,
) -> tuple[_Existing, bool]:
    """Verify a returned write response, recovering any interrupted handoff."""

    response_id: int | None = None
    try:
        response_id = _response_identifier(response)
        return (
            _verify_remote(
                store,
                request,
                controller=controller,
                intended_state_sha256=intended_state_sha256,
                expected_comment_id=expected_comment_id,
                response_id=response_id,
                recovery_kind=None,
                previous_state_sha256=previous_state_sha256,
            ),
            False,
        )
    except (KeyboardInterrupt, SystemExit):
        try:
            return (
                _verify_remote(
                    store,
                    request,
                    controller=controller,
                    intended_state_sha256=intended_state_sha256,
                    expected_comment_id=expected_comment_id,
                    response_id=response_id,
                    recovery_kind="ambiguous",
                    previous_state_sha256=previous_state_sha256,
                ),
                True,
            )
        except (KeyboardInterrupt, SystemExit) as error:
            raise _verification_error(
                request,
                recovery_kind="ambiguous",
                unknown=True,
                reason=f"verification_interrupted:{type(error).__name__}",
            ) from None


def _result(
    *,
    action: ApplyAction,
    request: ApplyRequest,
    canonical: ResolvedTarget,
    state: State,
    state_sha256: str,
    existing: _Existing | None,
    dry_run: bool = False,
    recovered: bool = False,
    markdown: str | None = None,
) -> ApplyResult:
    preview = None
    if dry_run:
        previous = None if existing is None else existing.state
        previous_view = None if previous is None else selected_view(previous.renderer, previous.data)
        definition = {
            "renderer": state.renderer.to_json(),
            "schema": None if state.data_schema is None else state.data_schema.to_json(),
        }
        old_definition = (
            None
            if previous is None
            else {
                "renderer": previous.renderer.to_json(),
                "schema": None if previous.data_schema is None else previous.data_schema.to_json(),
            }
        )
        preview = {
            "data": state.data,
            "meta": None if state.meta is None else state.meta.to_json(),
            "changes": {
                "data": [{"op": "add", "path": "", "value": state.data}]
                if previous is None
                else data_diff(previous.data, state.data),
                "definition": canonical_json_bytes(definition) != canonical_json_bytes(old_definition),
                "meta": previous is None or previous.meta != state.meta,
                "view": {"before": previous_view, "after": selected_view(state.renderer, state.data)},
            },
        }
    return ApplyResult(
        action=action,
        name=request.name,
        repository=canonical.repository,
        number=canonical.number,
        comment_id=(None if existing is None else existing.comment.id),
        url=(None if existing is None else existing.comment.url),
        revision=state.revision,
        state_sha256=state_sha256,
        dry_run=dry_run,
        recovered=recovered,
        markdown=markdown if dry_run else None,
        meta_source="github" if state.meta is not None else None,
        view=selected_view(state.renderer, state.data),
        preview=preview,
    )


@dataclass(frozen=True, slots=True)
class ApplyTransaction:
    reader: ApplyReadClient
    writer: ApplyWriteClient

    def apply(self, request: ApplyRequest) -> ApplyResult:
        if not isinstance(request, ApplyRequest):
            raise ApplyError(
                "apply requires an ApplyRequest",
                code="apply_request_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        controller = _controller(request, self.reader)
        store = CommentStore(self.reader)
        existing = _read_existing(
            store,
            request,
            controller=controller,
        )
        _check_mode_and_revision(request, existing)
        canonical, context, meta = _canonical_context(
            request,
            self.reader,
            existing,
        )
        state, markdown, body, intended_hash, changed = _desired_state(
            request,
            existing,
            controller=controller,
            context=context,
            meta=meta,
        )
        action: ApplyAction = "created" if existing is None else "updated"

        if not changed:
            return _result(
                action="unchanged",
                request=request,
                canonical=canonical,
                state=state,
                state_sha256=intended_hash,
                existing=existing,
                dry_run=request.dry_run,
                markdown=markdown,
            )
        if request.dry_run:
            return _result(
                action=action,
                request=request,
                canonical=canonical,
                state=state,
                state_sha256=intended_hash,
                existing=existing,
                dry_run=True,
                markdown=markdown,
            )

        _revalidate_before_write(
            store,
            request,
            controller=controller,
            initial=existing,
        )
        expected_comment_id = None if existing is None else existing.comment.id
        payload = {"body": body}
        try:
            if existing is None:
                response = self.writer.post(
                    f"repos/{request.target.repository}/issues/{request.target.number}/comments",
                    payload,
                    hostname=request.target.host,
                )
            else:
                response = self.writer.patch(
                    f"repos/{request.target.repository}/issues/comments/{existing.comment.id}",
                    payload,
                    hostname=request.target.host,
                )
        except GhWriteOutcomeUnknown as error:
            verified = _verify_remote(
                store,
                request,
                controller=controller,
                intended_state_sha256=intended_hash,
                expected_comment_id=expected_comment_id,
                response_id=None,
                recovery_kind=("timeout" if isinstance(error, GhWriteTimeout) else "ambiguous"),
                previous_state_sha256=(None if existing is None else existing.state_sha256),
            )
            return _result(
                action=action,
                request=request,
                canonical=canonical,
                state=state,
                state_sha256=intended_hash,
                existing=verified,
                recovered=True,
            )

        verified, recovered = _verify_post_write(
            store,
            request,
            controller=controller,
            intended_state_sha256=intended_hash,
            expected_comment_id=expected_comment_id,
            response=response,
            previous_state_sha256=(None if existing is None else existing.state_sha256),
        )
        return _result(
            action=action,
            request=request,
            canonical=canonical,
            state=state,
            state_sha256=intended_hash,
            existing=verified,
            recovered=recovered,
        )


def apply(
    request: ApplyRequest,
    *,
    reader: ApplyReadClient,
    writer: ApplyWriteClient,
) -> ApplyResult:
    return ApplyTransaction(
        reader=reader,
        writer=writer,
    ).apply(request)


__all__ = [
    "ApplyAction",
    "ApplyError",
    "ApplyMode",
    "ApplyReadClient",
    "ApplyRequest",
    "ApplyResult",
    "ApplyTransaction",
    "ApplyWriteClient",
    "SchemaInput",
    "apply",
]
