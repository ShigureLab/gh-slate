from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.codec import (
    ControllerV1,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    StateV1,
    decode_comment,
)
from gh_slate.errors import ExitCode, GhSlateError
from gh_slate.github.apply import (
    ApplyError,
    ApplyRequest,
    ApplyResult,
    apply,
)
from gh_slate.github.models import GitHubActor
from gh_slate.github.target import ResolvedTarget
from gh_slate.github.write import GhWriteOutcomeUnknown, GhWriteTimeout
from gh_slate.rendering import (
    ListRendererV1,
    SlateContext,
    jinja_descriptor,
    materialize_comment,
)
from gh_slate.schema import validate_schema

apply_module = import_module("gh_slate.github.apply")

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
ISSUE_URL = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"
PULL_URL = f"https://{HOST}/{REPOSITORY}/pull/{NUMBER}"
TARGET = ResolvedTarget(
    host=HOST,
    repository=REPOSITORY,
    number=NUMBER,
    url=ISSUE_URL,
)
EMPTY_HASH = "0" * 64
ACTOR_ID = 101
OTHER_ACTOR_ID = 202


def _renderer() -> RendererDescriptorV1:
    return ListRendererV1(selector=".status").to_descriptor()


def _body(
    status: str = "old",
    *,
    revision: int = 1,
    controller: str = "ci-bot",
    controller_id: int | None = ACTOR_ID,
    renderer: RendererDescriptorV1 | None = None,
    schema: SchemaSnapshotV1 | None = None,
    page_url: str = ISSUE_URL,
) -> str:
    descriptor = _renderer() if renderer is None else renderer
    state = StateV1(
        name="ci",
        revision=revision,
        controller=ControllerV1(login=controller, id=controller_id),
        data={"status": status},
        data_schema=schema,
        renderer=descriptor,
        render_sha256=EMPTY_HASH,
    )
    context = SlateContext(
        name="ci",
        repository=REPOSITORY,
        number=NUMBER,
        url=page_url,
    )
    return materialize_comment(
        state,
        slate=context,
    ).encoded.body


def _record(
    identifier: int,
    body: str,
    *,
    page_url: str = ISSUE_URL,
    author: str = "ci-bot",
    author_id: int = ACTOR_ID,
) -> dict[str, object]:
    return {
        "id": identifier,
        "body": body,
        "html_url": f"{page_url}#issuecomment-{identifier}",
        "user": {"id": author_id, "login": author},
        "created_at": "2026-07-31T00:00:00Z",
        "updated_at": "2026-07-31T00:01:00Z",
    }


@dataclass(slots=True)
class FakeGitHub:
    comments: list[dict[str, object]] = field(default_factory=list)
    actor_login: str = "ci-bot"
    actor_id: int = ACTOR_ID
    target_html_url: str = ISSUE_URL
    write_behavior: str = "normal"
    before_comment_read: dict[int, Callable[[FakeGitHub], None]] = field(default_factory=dict)
    read_calls: list[tuple[object, ...]] = field(default_factory=list)
    write_calls: list[tuple[object, ...]] = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)
    comment_reads: int = 0
    next_identifier: int = 100

    def current_actor(self, hostname: str | None = None) -> GitHubActor:
        self.read_calls.append(("ACTOR", hostname))
        return GitHubActor(id=self.actor_id, login=self.actor_login)

    def resolve_actor(
        self,
        login: str,
        hostname: str | None = None,
    ) -> GitHubActor:
        self.read_calls.append(("RESOLVE_ACTOR", login, hostname))
        if login.casefold() == self.actor_login.casefold():
            return GitHubActor(id=self.actor_id, login=self.actor_login)
        return GitHubActor(id=OTHER_ACTOR_ID, login=login)

    def api_get(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
        paginate: bool = False,
    ) -> object:
        self.read_calls.append(("GET", endpoint, hostname, paginate))
        self.events.append(("GET", endpoint))
        if endpoint == f"repos/{REPOSITORY}/issues/{NUMBER}":
            return {"html_url": self.target_html_url}
        if endpoint == f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100":
            self.comment_reads += 1
            callback = self.before_comment_read.get(self.comment_reads)
            if callback is not None:
                callback(self)
            return [dict(comment) for comment in self.comments]
        raise AssertionError(f"unexpected GET endpoint: {endpoint}")

    def _body(self, payload: Mapping[str, object]) -> str:
        body = payload.get("body")
        assert isinstance(body, str)
        return body

    def _created_record(self, body: str) -> dict[str, object]:
        identifier = self.next_identifier
        self.next_identifier += 1
        return _record(
            identifier,
            body,
            page_url=self.target_html_url,
            author=self.actor_login,
            author_id=self.actor_id,
        )

    def _finish_write(
        self,
        *,
        mutate: Callable[[], dict[str, object]],
    ) -> object:
        if self.write_behavior in {
            "ambiguous-unapplied",
            "timeout-unapplied",
        }:
            if self.write_behavior == "ambiguous-unapplied":
                raise GhWriteOutcomeUnknown(
                    "write response failed",
                    code="gh_write_json_invalid",
                )
            raise GhWriteTimeout(timeout_seconds=1)
        record = mutate()
        if self.write_behavior in {
            "ambiguous-applied",
            "timeout-applied",
        }:
            if self.write_behavior == "ambiguous-applied":
                raise GhWriteOutcomeUnknown(
                    "write response failed",
                    code="gh_write_json_invalid",
                )
            raise GhWriteTimeout(timeout_seconds=1)
        return dict(record)

    def post(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> object:
        self.write_calls.append(("POST", endpoint, dict(payload), hostname))
        self.events.append(("POST", endpoint))
        body = self._body(payload)

        def mutate() -> dict[str, object]:
            record = self._created_record(body)
            if self.write_behavior != "no-apply":
                self.comments.append(record)
            return record

        return self._finish_write(mutate=mutate)

    def patch(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> object:
        self.write_calls.append(("PATCH", endpoint, dict(payload), hostname))
        self.events.append(("PATCH", endpoint))
        identifier = int(endpoint.rsplit("/", 1)[1])
        body = self._body(payload)

        def mutate() -> dict[str, object]:
            for comment in self.comments:
                if comment["id"] == identifier:
                    response = dict(comment)
                    response["body"] = body
                    if self.write_behavior != "no-apply":
                        comment["body"] = body
                    return response
            raise AssertionError("patch target is missing")

        return self._finish_write(mutate=mutate)


def _apply(
    remote: FakeGitHub,
    **changes: object,
) -> ApplyResult:
    request_values: dict[str, Any] = {
        "target": TARGET,
        "name": "ci",
    }
    request_values.update(changes)
    request = ApplyRequest(**request_values)
    return apply(
        request,
        reader=remote,
        writer=remote,
    )


def test_create_uses_canonical_target_metadata_and_exactly_one_post() -> None:
    remote = FakeGitHub(target_html_url=PULL_URL)
    renderer = jinja_descriptor("{{ slate.repository }}#{{ slate.number }} {{ slate.url }}")

    result = _apply(
        remote,
        mode="create",
        renderer=renderer,
    )

    assert result.action == "created"
    assert result.comment_id == 100
    assert result.url == f"{PULL_URL}#issuecomment-100"
    assert result.repository == REPOSITORY
    assert result.number == NUMBER
    assert result.revision == 1
    assert result.dry_run is False
    assert result.recovered is False
    assert result.markdown is None
    assert remote.comment_reads == 3
    assert [call[0] for call in remote.write_calls] == ["POST"]
    assert remote.write_calls[0][1] == (f"repos/{REPOSITORY}/issues/{NUMBER}/comments")
    assert remote.write_calls[0][3] == HOST
    decoded = decode_comment(cast("str", remote.comments[0]["body"]))
    assert decoded.visible_markdown == f"{REPOSITORY}#{NUMBER} {PULL_URL}\n"
    assert decoded.state.controller == ControllerV1(
        login="ci-bot",
        id=ACTOR_ID,
    )


def test_update_reuses_renderer_schema_and_controller_then_patches_once() -> None:
    schema = validate_schema(
        {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        }
    )
    remote = FakeGitHub(
        comments=[
            _record(
                7,
                _body(
                    controller="CI-Bot",
                    schema=schema,
                    page_url=PULL_URL,
                ),
                page_url=PULL_URL,
            )
        ],
        target_html_url=PULL_URL,
    )

    result = _apply(
        remote,
        mode="update",
        data={"status": "new"},
        if_revision=1,
    )

    assert result.action == "updated"
    assert result.comment_id == 7
    assert result.revision == 2
    assert remote.comment_reads == 3
    assert [call[0] for call in remote.write_calls] == ["PATCH"]
    assert remote.write_calls[0][1] == f"repos/{REPOSITORY}/issues/comments/7"
    decoded = decode_comment(cast("str", remote.comments[0]["body"]))
    assert decoded.state.data == {"status": "new"}
    assert decoded.state.data_schema == schema
    assert decoded.state.renderer == _renderer()
    assert decoded.state.controller == ControllerV1(
        login="ci-bot",
        id=ACTOR_ID,
    )
    comments_endpoint = f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100"
    assert remote.events == [
        ("GET", comments_endpoint),
        ("GET", comments_endpoint),
        ("PATCH", f"repos/{REPOSITORY}/issues/comments/7"),
        ("GET", comments_endpoint),
    ]


def test_update_survives_login_rename_and_refreshes_controller_metadata() -> None:
    remote = FakeGitHub(
        comments=[
            _record(
                7,
                _body(controller="old-login"),
                author="new-login",
            )
        ],
        actor_login="new-login",
    )

    result = _apply(
        remote,
        mode="update",
        data={"status": "new"},
    )

    assert result.action == "updated"
    assert [call[0] for call in remote.write_calls] == ["PATCH"]
    decoded = decode_comment(cast("str", remote.comments[0]["body"]))
    assert decoded.state.controller == ControllerV1(
        login="new-login",
        id=ACTOR_ID,
    )


def test_legacy_login_only_state_is_upgraded_on_the_next_write() -> None:
    remote = FakeGitHub(
        comments=[
            _record(
                7,
                _body(controller="old-login", controller_id=None),
                author="new-login",
            )
        ],
        actor_login="new-login",
    )

    result = _apply(remote, mode="update")

    assert result.action == "updated"
    decoded = decode_comment(cast("str", remote.comments[0]["body"]))
    assert decoded.state.controller == ControllerV1(
        login="new-login",
        id=ACTOR_ID,
    )


def test_internal_stable_controller_identity_avoids_login_reresolution() -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())],
    )

    result = _apply(
        remote,
        mode="update",
        controller=GitHubActor(id=ACTOR_ID, login="stale-login"),
    )

    assert result.action == "unchanged"
    assert all(call[0] != "RESOLVE_ACTOR" for call in remote.read_calls)


def test_explicit_schema_removal_is_a_functional_update() -> None:
    schema = validate_schema({"type": "object"})
    remote = FakeGitHub(
        comments=[_record(7, _body(schema=schema))],
    )

    result = _apply(
        remote,
        mode="update",
        replace_schema=True,
        data_schema=None,
    )

    assert result.action == "updated"
    decoded = decode_comment(cast("str", remote.comments[0]["body"]))
    assert decoded.state.data_schema is None
    assert decoded.state.revision == 2


def test_unchanged_and_dry_run_complete_local_pipeline_without_a_write() -> None:
    unchanged_remote = FakeGitHub(
        comments=[_record(7, _body())],
    )

    unchanged = _apply(
        unchanged_remote,
        mode="update",
    )

    assert unchanged.action == "unchanged"
    assert unchanged.revision == 1
    assert unchanged_remote.comment_reads == 1
    assert unchanged_remote.write_calls == []

    dry_run_remote = FakeGitHub(
        comments=[_record(7, _body())],
    )
    preview = _apply(
        dry_run_remote,
        data={"status": "preview"},
        dry_run=True,
    )
    assert preview.action == "updated"
    assert preview.comment_id == 7
    assert preview.markdown == "- preview\n"
    assert preview.revision == 2
    assert dry_run_remote.comment_reads == 1
    assert dry_run_remote.write_calls == []
    assert preview.to_json()["dry_run"] is True
    assert preview.to_json()["markdown"] == "- preview\n"


def test_dry_run_create_has_null_remote_identity() -> None:
    remote = FakeGitHub(target_html_url=PULL_URL)

    result = _apply(
        remote,
        renderer=_renderer(),
        dry_run=True,
    )

    assert result.action == "created"
    assert result.comment_id is None
    assert result.url is None
    assert result.markdown == "- null\n"
    assert remote.comment_reads == 1
    assert remote.write_calls == []


def test_create_rejects_target_metadata_for_a_different_number() -> None:
    remote = FakeGitHub(
        target_html_url=f"https://{HOST}/{REPOSITORY}/issues/43",
    )

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            renderer=_renderer(),
        )

    assert caught.value.code == "github_response_invalid"
    assert caught.value.details == {
        "expected_number": NUMBER,
        "actual_number": 43,
    }
    assert remote.comment_reads == 1
    assert remote.write_calls == []


@pytest.mark.parametrize("mode", ["create", "update"])
def test_reserved_marker_from_renderer_is_rejected_before_post_or_patch(
    mode: str,
) -> None:
    remote = FakeGitHub(
        comments=[] if mode == "create" else [_record(7, _body())],
    )

    with pytest.raises(GhSlateError) as caught:
        _apply(
            remote,
            mode=mode,
            renderer=jinja_descriptor("<!-- gh-slate:v1 forged -->"),
        )

    assert caught.value.code == "duplicate_marker"
    assert remote.write_calls == []


@pytest.mark.parametrize(
    ("changes", "code", "exit_code"),
    [
        (
            {"mode": "create", "renderer": _renderer()},
            "slate_already_exists",
            ExitCode.CONFLICT,
        ),
        (
            {"mode": "update"},
            "slate_not_found",
            ExitCode.NOT_FOUND,
        ),
        (
            {"if_revision": 2},
            "revision_conflict",
            ExitCode.CONFLICT,
        ),
        (
            {"controller": "other"},
            "controller_conflict",
            ExitCode.CONFLICT,
        ),
    ],
)
def test_mode_revision_and_controller_conflicts_write_nothing(
    changes: dict[str, object],
    code: str,
    exit_code: ExitCode,
) -> None:
    has_existing = code != "slate_not_found"
    remote = FakeGitHub(
        comments=[_record(7, _body())] if has_existing else [],
    )

    with pytest.raises(GhSlateError) as caught:
        _apply(remote, **changes)

    assert caught.value.code == code
    assert caught.value.exit_code == exit_code
    assert remote.write_calls == []


@pytest.mark.parametrize(
    ("comments", "code", "exit_code"),
    [
        (
            [
                _record(
                    7,
                    _body().replace("eA", "!A", 1),
                )
            ],
            "slate_corrupt",
            ExitCode.VALIDATION,
        ),
        (
            [
                _record(
                    7,
                    _body().replace("- old\n", "- edited\n"),
                )
            ],
            "render_drift",
            ExitCode.CONFLICT,
        ),
        (
            [
                _record(7, _body()),
                _record(8, _body()),
            ],
            "duplicate_slate",
            ExitCode.CONFLICT,
        ),
    ],
)
def test_corrupt_drift_and_duplicates_fail_before_render_or_write(
    comments: list[dict[str, object]],
    code: str,
    exit_code: ExitCode,
) -> None:
    remote = FakeGitHub(comments=comments)

    with pytest.raises(GhSlateError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == code
    assert caught.value.exit_code == exit_code
    assert remote.write_calls == []


def test_comment_id_url_mismatch_fails_before_any_write() -> None:
    record = _record(999, _body())
    record["html_url"] = f"{ISSUE_URL}#issuecomment-7"
    remote = FakeGitHub(comments=[record])

    with pytest.raises(GhSlateError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == "github_response_invalid"
    assert caught.value.details["cause_code"] == "target_conflict"
    assert remote.write_calls == []


def test_renderer_failure_and_missing_create_renderer_write_nothing() -> None:
    invalid = RendererDescriptorV1(
        kind="unknown",
        version=1,
    )
    remote = FakeGitHub()
    with pytest.raises(GhSlateError):
        _apply(
            remote,
            renderer=invalid,
        )
    assert remote.write_calls == []

    with pytest.raises(ApplyError) as caught:
        _apply(FakeGitHub())
    assert caught.value.code == "renderer_required"


@pytest.mark.parametrize(
    "failure",
    ["jinja", "jq", "schema", "size"],
)
def test_full_pipeline_failures_happen_before_any_write(
    failure: str,
) -> None:
    if failure == "schema":
        schema = validate_schema(
            {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            }
        )
        remote = FakeGitHub(
            comments=[_record(7, _body(schema=schema))],
        )
        changes: dict[str, object] = {"data": {"status": 7}}
    elif failure == "jinja":
        remote = FakeGitHub()
        changes = {
            "renderer": jinja_descriptor("{{ data.missing }}"),
        }
    elif failure == "jq":
        remote = FakeGitHub()
        changes = {
            "data": {"jobs": [{"name": "a"}, {"name": "b"}]},
            "renderer": ListRendererV1(
                selector=".jobs[]",
            ).to_descriptor(),
        }
    else:
        remote = FakeGitHub()
        changes = {
            "renderer": jinja_descriptor("x" * (48 * 1024 + 1)),
        }

    with pytest.raises(GhSlateError):
        _apply(remote, **changes)

    assert remote.write_calls == []


def test_second_read_revalidates_selected_id_even_when_hash_is_same() -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())],
    )

    def replace_before_second_read(github: FakeGitHub) -> None:
        github.comments[:] = [_record(8, _body())]

    remote.before_comment_read[2] = replace_before_second_read

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == "concurrent_change"
    assert remote.comment_reads == 2
    assert remote.write_calls == []


def test_second_read_revalidates_hash_even_when_id_is_same() -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())],
    )

    def replace_before_second_read(github: FakeGitHub) -> None:
        github.comments[:] = [_record(7, _body("other", revision=2))]

    remote.before_comment_read[2] = replace_before_second_read

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == "concurrent_change"
    assert remote.comment_reads == 2
    assert remote.write_calls == []


def test_create_second_read_rejects_a_concurrent_matching_comment() -> None:
    remote = FakeGitHub()

    def create_before_second_read(github: FakeGitHub) -> None:
        github.comments.append(_record(7, _body()))

    remote.before_comment_read[2] = create_before_second_read

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            renderer=_renderer(),
        )

    assert caught.value.code == "concurrent_change"
    assert remote.comment_reads == 2
    assert remote.write_calls == []


def test_post_write_refetch_rejects_a_state_hash_mismatch_without_retry() -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())],
        write_behavior="no-apply",
    )

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == "post_write_verification_failed"
    assert [call[0] for call in remote.write_calls] == ["PATCH"]
    assert remote.comment_reads == 3


def test_post_write_refetch_failure_is_unknown_and_never_retries() -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())],
    )

    def fail_after_write(_github: FakeGitHub) -> None:
        raise RuntimeError("simulated read outage")

    remote.before_comment_read[3] = fail_after_write

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == "post_write_verification_unknown"
    assert caught.value.exit_code == ExitCode.RUNTIME
    assert caught.value.details["reason"] == "refetch_failed:RuntimeError"
    assert [call[0] for call in remote.write_calls] == ["PATCH"]
    assert remote.comment_reads == 3


def test_successful_create_missing_from_first_refetch_is_unknown_and_never_retries() -> None:
    remote = FakeGitHub()

    def hide_created_comment(github: FakeGitHub) -> None:
        github.comments.clear()

    remote.before_comment_read[3] = hide_created_comment

    with pytest.raises(ApplyError) as caught:
        _apply(remote, renderer=_renderer())

    assert caught.value.code == "post_write_verification_unknown"
    assert caught.value.exit_code == ExitCode.RUNTIME
    assert caught.value.details["reason"] == "slate_missing"
    assert caught.value.hints == ("inspect the slate before attempting another mutation",)
    assert [call[0] for call in remote.write_calls] == ["POST"]
    assert remote.comment_reads == 3


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(130)])
@pytest.mark.parametrize(
    ("write_behavior", "expected_code"),
    [
        ("timeout-applied", "write_timeout_unknown"),
        ("ambiguous-applied", "write_outcome_unknown"),
    ],
)
def test_post_write_verification_interrupt_is_unknown_and_never_retries(
    write_behavior: str,
    expected_code: str,
    interruption: BaseException,
) -> None:
    remote = FakeGitHub(write_behavior=write_behavior)

    def interrupt_after_write(_github: FakeGitHub) -> None:
        raise interruption

    remote.before_comment_read[3] = interrupt_after_write

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            renderer=_renderer(),
        )

    assert caught.value.code == expected_code
    assert caught.value.details["reason"] == f"refetch_failed:{type(interruption).__name__}"
    assert [call[0] for call in remote.write_calls] == ["POST"]
    assert remote.comment_reads == 3


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(130)])
def test_post_write_refetch_interrupt_recovers_without_replaying_the_post(
    interruption: BaseException,
) -> None:
    remote = FakeGitHub()

    def interrupt_after_write(_github: FakeGitHub) -> None:
        raise interruption

    remote.before_comment_read[3] = interrupt_after_write

    result = _apply(remote, renderer=_renderer())

    assert result.action == "created"
    assert result.recovered is True
    assert result.comment_id == 100
    assert [call[0] for call in remote.write_calls] == ["POST"]
    assert remote.comment_reads == 4


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(130)])
@pytest.mark.parametrize("existing", [False, True])
def test_post_write_response_handoff_interrupt_is_unknown_and_never_retries(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    interruption: BaseException,
) -> None:
    remote = FakeGitHub(comments=[_record(7, _body())] if existing else [])

    def interrupt_response_identifier(_response: object) -> int | None:
        raise interruption

    monkeypatch.setattr(apply_module, "_response_identifier", interrupt_response_identifier)

    result = _apply(
        remote,
        data={"status": "new"} if existing else None,
        renderer=None if existing else _renderer(),
    )

    assert result.action == ("updated" if existing else "created")
    assert result.recovered is True
    assert result.comment_id == (7 if existing else 100)
    assert [call[0] for call in remote.write_calls] == (["PATCH"] if existing else ["POST"])
    assert remote.comment_reads == 3


@pytest.mark.parametrize("existing", [False, True])
def test_post_write_handoff_recovery_reports_an_unapplied_write_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())] if existing else [],
        write_behavior="no-apply",
    )

    def interrupt_response_identifier(_response: object) -> int | None:
        raise KeyboardInterrupt

    monkeypatch.setattr(apply_module, "_response_identifier", interrupt_response_identifier)

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            data={"status": "new"} if existing else None,
            renderer=None if existing else _renderer(),
        )

    assert caught.value.code == "write_outcome_unknown"
    assert caught.value.details["reason"] == ("previous_state_still_visible" if existing else "slate_missing")
    assert caught.value.hints == ("inspect the slate before attempting another mutation",)
    assert [call[0] for call in remote.write_calls] == (["PATCH"] if existing else ["POST"])
    assert remote.comment_reads == 3


def test_timeout_refetch_confirms_success_without_replaying_the_post() -> None:
    remote = FakeGitHub(
        write_behavior="timeout-applied",
        target_html_url=PULL_URL,
    )

    result = _apply(
        remote,
        renderer=_renderer(),
    )

    assert result.action == "created"
    assert result.recovered is True
    assert result.to_json()["recovered"] is True
    assert result.comment_id == 100
    assert [call[0] for call in remote.write_calls] == ["POST"]
    assert remote.comment_reads == 3


def test_timeout_refetch_classifies_conflict_without_replaying_the_patch() -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())],
        write_behavior="timeout-unapplied",
    )

    def replace_with_concurrent_state(github: FakeGitHub) -> None:
        github.comments[0]["body"] = _body(
            "concurrent",
            revision=2,
        )

    remote.before_comment_read[3] = replace_with_concurrent_state

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == "write_timeout_conflict"
    assert caught.value.exit_code == ExitCode.CONFLICT
    assert [call[0] for call in remote.write_calls] == ["PATCH"]
    assert remote.comment_reads == 3


def test_timeout_update_is_unknown_while_the_previous_state_is_still_visible() -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())],
        write_behavior="timeout-unapplied",
    )

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == "write_timeout_unknown"
    assert caught.value.exit_code == ExitCode.RUNTIME
    assert caught.value.details["reason"] == "previous_state_still_visible"
    assert [call[0] for call in remote.write_calls] == ["PATCH"]
    assert remote.comment_reads == 3


def test_timeout_refetch_reports_unknown_when_create_is_still_absent() -> None:
    remote = FakeGitHub(
        write_behavior="timeout-unapplied",
    )

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            renderer=_renderer(),
        )

    assert caught.value.code == "write_timeout_unknown"
    assert caught.value.exit_code == ExitCode.RUNTIME
    assert [call[0] for call in remote.write_calls] == ["POST"]
    assert remote.comment_reads == 3


@pytest.mark.parametrize(
    ("replacement", "reason"),
    [
        ([], "slate_missing"),
        (
            [
                _record(
                    7,
                    _body().replace("eA", "!A", 1),
                )
            ],
            "slate_corrupt",
        ),
    ],
)
def test_timeout_update_missing_or_corrupt_is_a_confirmed_conflict(
    replacement: list[dict[str, object]],
    reason: str,
) -> None:
    remote = FakeGitHub(
        comments=[_record(7, _body())],
        write_behavior="timeout-unapplied",
    )

    def replace_after_write(github: FakeGitHub) -> None:
        github.comments[:] = replacement

    remote.before_comment_read[3] = replace_after_write

    with pytest.raises(ApplyError) as caught:
        _apply(
            remote,
            data={"status": "new"},
        )

    assert caught.value.code == "write_timeout_conflict"
    assert caught.value.exit_code == ExitCode.CONFLICT
    assert caught.value.details["reason"] == reason
    assert [call[0] for call in remote.write_calls] == ["PATCH"]
    assert remote.comment_reads == 3


def test_ambiguous_response_refetches_without_retrying_the_write() -> None:
    applied = FakeGitHub(
        write_behavior="ambiguous-applied",
        target_html_url=PULL_URL,
    )

    recovered = _apply(
        applied,
        renderer=_renderer(),
    )

    assert recovered.action == "created"
    assert recovered.recovered is True
    assert [call[0] for call in applied.write_calls] == ["POST"]

    unapplied = FakeGitHub(
        comments=[_record(7, _body())],
        write_behavior="ambiguous-unapplied",
    )
    with pytest.raises(ApplyError) as caught:
        _apply(
            unapplied,
            data={"status": "new"},
        )

    assert caught.value.code == "write_outcome_unknown"
    assert caught.value.details["reason"] == "previous_state_still_visible"
    assert [call[0] for call in unapplied.write_calls] == ["PATCH"]


def test_result_json_only_adds_conditional_recovery_fields() -> None:
    ordinary = ApplyResult(
        action="updated",
        name="ci",
        repository=REPOSITORY,
        number=NUMBER,
        comment_id=7,
        url=f"{ISSUE_URL}#issuecomment-7",
        revision=2,
        state_sha256="a" * 64,
        markdown="not serialized",
    )

    assert ordinary.to_json() == {
        "action": "updated",
        "name": "ci",
        "repository": REPOSITORY,
        "number": NUMBER,
        "comment_id": 7,
        "url": f"{ISSUE_URL}#issuecomment-7",
        "revision": 2,
        "state_sha256": "a" * 64,
    }


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"controller": " ci-bot"}, "controller_invalid"),
        ({"controller": "ci bot"}, "controller_invalid"),
        ({"if_revision": 2**63}, "revision_invalid"),
    ],
)
def test_request_rejects_invalid_controller_and_revision(
    arguments: dict[str, Any],
    code: str,
) -> None:
    with pytest.raises(ApplyError) as caught:
        ApplyRequest(
            target=TARGET,
            name="ci",
            **arguments,
        )

    assert caught.value.code == code
    assert caught.value.exit_code == ExitCode.VALIDATION
