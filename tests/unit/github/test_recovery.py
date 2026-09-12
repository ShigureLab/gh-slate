from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

import pytest

from gh_slate.codec import (
    ControllerV1,
    MetaSnapshot,
    StateV1,
    decode_comment,
    encode_comment,
)
from gh_slate.codec.marker import encode_marker, parse_marker
from gh_slate.errors import ExitCode, GhSlateError
from gh_slate.github.models import GitHubActor
from gh_slate.github.recovery import (
    DeleteRequest,
    RecoveryError,
    RepairRequest,
    delete,
    repair,
    validate_delete_confirmation,
)
from gh_slate.github.target import ResolvedTarget
from gh_slate.github.write import GhWriteOutcomeUnknown
from gh_slate.rendering import SlateContext, jinja_descriptor, materialize_comment

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
TARGET_URL = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"
TARGET = ResolvedTarget(
    host=HOST,
    repository=REPOSITORY,
    number=NUMBER,
    url=TARGET_URL,
)
COMMENT_URL = f"{TARGET_URL}#issuecomment-7"
WriteBehavior = Literal[
    "normal",
    "no-apply",
    "unknown-applied",
    "unknown-unapplied",
]
ACTOR_ID = 101
OTHER_ACTOR_ID = 202


def _body(
    *,
    revision: int = 3,
    drifted: bool = False,
    controller_login: str = "ci-bot",
    controller_id: int | None = ACTOR_ID,
) -> str:
    state = StateV1(
        name="ci",
        format="gh-slate/state-v2",
        meta=MetaSnapshot.local("ci"),
        revision=revision,
        controller=ControllerV1(
            login=controller_login,
            id=controller_id,
        ),
        data={"status": "ready"},
        data_schema=None,
        renderer=jinja_descriptor("{{ data | md_list }}"),
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
    if not drifted:
        return body
    marker = parse_marker(body)
    return encode_marker(
        replace(
            marker,
            visible="manually edited\n",
        )
    )


def _corrupt_body() -> str:
    body = _body()
    marker = parse_marker(body)
    different_hash = "0" * 64 if marker.state_sha256 != "0" * 64 else "1" * 64
    return encode_marker(
        replace(
            marker,
            state_sha256=different_hash,
        )
    )


def test_legacy_can_be_deleted_but_cannot_be_repaired():
    decoded = decode_comment(_body())
    legacy = replace(decoded.state, format="gh-slate/state-v1", meta=None)
    remote = FakeGitHub(comments=[_record(encode_comment(legacy, decoded.visible_markdown).body)])
    with pytest.raises(GhSlateError) as error:
        repair(RepairRequest(target=TARGET, name="ci", if_revision=3, from_state=True), reader=remote, writer=remote)
    assert error.value.code == "state_migration_required"
    assert remote.write_calls == []
    result = delete(DeleteRequest(target=TARGET, name="ci", confirm="ci"), reader=remote, writer=remote)
    assert result.action == "deleted"


def _record(
    body: str,
    *,
    identifier: int = 7,
    author_login: str = "ci-bot",
    author_id: int = ACTOR_ID,
) -> dict[str, object]:
    return {
        "id": identifier,
        "body": body,
        "html_url": f"{TARGET_URL}#issuecomment-{identifier}",
        "user": {"id": author_id, "login": author_login},
        "created_at": "2026-07-31T00:00:00Z",
        "updated_at": "2026-07-31T00:01:00Z",
    }


@dataclass(slots=True)
class FakeGitHub:
    comments: list[dict[str, object]]
    behavior: WriteBehavior = "normal"
    actor_login: str = "ci-bot"
    actor_id: int = ACTOR_ID
    before_comment_read: dict[int, Callable[[FakeGitHub], None]] = field(default_factory=dict)
    fail_comment_read: int | None = None
    comment_reads: int = 0
    read_calls: list[tuple[object, ...]] = field(default_factory=list)
    write_calls: list[tuple[object, ...]] = field(default_factory=list)

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
        assert endpoint == (f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100")
        self.comment_reads += 1
        if self.fail_comment_read == self.comment_reads:
            raise RuntimeError("injected read failure")
        callback = self.before_comment_read.get(self.comment_reads)
        if callback is not None:
            callback(self)
        return [dict(comment) for comment in self.comments]

    def _unknown(self) -> None:
        raise GhWriteOutcomeUnknown(
            "injected unknown write",
            code="gh_write_failed",
        )

    def patch(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        hostname: str | None = None,
    ) -> object:
        self.write_calls.append(("PATCH", endpoint, dict(payload), hostname))
        body = payload["body"]
        assert isinstance(body, str)
        identifier = int(endpoint.rsplit("/", 1)[1])
        if self.behavior not in {"no-apply", "unknown-unapplied"}:
            for comment in self.comments:
                if comment["id"] == identifier:
                    comment["body"] = body
                    break
        if self.behavior.startswith("unknown"):
            self._unknown()
        return {"id": identifier}

    def delete(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
    ) -> object:
        self.write_calls.append(("DELETE", endpoint, hostname))
        identifier = int(endpoint.rsplit("/", 1)[1])
        if self.behavior not in {"no-apply", "unknown-unapplied"}:
            self.comments[:] = [comment for comment in self.comments if comment["id"] != identifier]
        if self.behavior.startswith("unknown"):
            self._unknown()
        return None


def _repair(
    remote: FakeGitHub,
    *,
    controller: str | None = None,
    if_revision: int | None = None,
):
    return repair(
        RepairRequest(
            target=TARGET,
            name="ci",
            from_state=True,
            controller=controller,
            if_revision=if_revision,
        ),
        reader=remote,
        writer=remote,
    )


def _delete(
    remote: FakeGitHub,
    *,
    confirm: str | None = "ci",
    yes: bool = False,
    controller: str | None = None,
    if_revision: int | None = None,
):
    return delete(
        DeleteRequest(
            target=TARGET,
            name="ci",
            confirm=confirm,
            yes=yes,
            controller=controller,
            if_revision=if_revision,
        ),
        reader=remote,
        writer=remote,
    )


def test_repair_changes_only_the_visible_projection_and_keeps_state_identity() -> None:
    original = _body(drifted=True)
    original_marker = parse_marker(original)
    original_decoded = decode_comment(original)
    remote = FakeGitHub(comments=[_record(original)])

    result = _repair(remote)

    assert result.action == "repaired"
    assert result.revision == original_decoded.state.revision
    assert result.state_sha256 == original_decoded.state_sha256
    assert result.recovered is False
    assert len(remote.write_calls) == 1
    assert remote.write_calls[0][0:2] == (
        "PATCH",
        f"repos/{REPOSITORY}/issues/comments/7",
    )
    assert remote.comment_reads == 3
    repaired = decode_comment(str(remote.comments[0]["body"]))
    repaired_marker = parse_marker(str(remote.comments[0]["body"]))
    assert repaired.drifted is False
    assert repaired.state == original_decoded.state
    assert repaired.state_sha256 == original_decoded.state_sha256
    assert repaired_marker.payload == original_marker.payload
    assert repaired_marker.state_sha256 == original_marker.state_sha256


def test_repair_of_an_intact_projection_is_unchanged_without_a_patch() -> None:
    remote = FakeGitHub(comments=[_record(_body())])

    result = _repair(remote)

    assert result.action == "unchanged"
    assert remote.write_calls == []
    assert remote.comment_reads == 1


def test_repair_selects_a_renamed_controller_by_stable_id() -> None:
    original = _body(
        drifted=True,
        controller_login="old-login",
    )
    remote = FakeGitHub(
        comments=[
            _record(
                original,
                author_login="new-login",
            )
        ],
        actor_login="new-login",
    )

    result = _repair(remote)

    assert result.action == "repaired"
    assert [call[0] for call in remote.write_calls] == ["PATCH"]
    assert decode_comment(str(remote.comments[0]["body"])).state.controller == (
        ControllerV1(login="old-login", id=ACTOR_ID)
    )


def test_repair_rejects_a_second_read_change_before_patching() -> None:
    remote = FakeGitHub(comments=[_record(_body(drifted=True))])
    remote.before_comment_read[2] = lambda github: github.comments.__setitem__(
        0,
        _record(_body(revision=4, drifted=True)),
    )

    with pytest.raises(RecoveryError) as caught:
        _repair(remote)

    assert caught.value.code == "concurrent_change"
    assert caught.value.exit_code == ExitCode.CONFLICT
    assert remote.write_calls == []


@pytest.mark.parametrize(
    ("behavior", "recovered", "error_code"),
    [
        ("unknown-applied", True, None),
        ("unknown-unapplied", None, "repair_outcome_unknown"),
        ("no-apply", None, "post_repair_verification_failed"),
    ],
)
def test_repair_never_reports_an_unverified_remote_outcome_as_success(
    behavior: WriteBehavior,
    recovered: bool | None,
    error_code: str | None,
) -> None:
    remote = FakeGitHub(
        comments=[_record(_body(drifted=True))],
        behavior=behavior,
    )

    if error_code is not None:
        with pytest.raises(RecoveryError) as caught:
            _repair(remote)
        assert caught.value.code == error_code
    else:
        result = _repair(remote)
        assert result.action == "repaired"
        assert result.recovered is recovered
    assert len(remote.write_calls) == 1


def test_repair_refetch_failure_after_patch_is_unknown() -> None:
    remote = FakeGitHub(
        comments=[_record(_body(drifted=True))],
        fail_comment_read=3,
    )

    with pytest.raises(RecoveryError) as caught:
        _repair(remote)

    assert caught.value.code == "repair_outcome_unknown"
    assert len(remote.write_calls) == 1


def test_repair_rejects_corrupt_duplicate_and_stale_state() -> None:
    corrupt = FakeGitHub(comments=[_record(_corrupt_body())])
    with pytest.raises(RecoveryError) as corrupt_error:
        _repair(corrupt)
    assert corrupt_error.value.code == "slate_corrupt"

    duplicate = FakeGitHub(
        comments=[
            _record(_body(drifted=True), identifier=7),
            _record(_body(drifted=True), identifier=8),
        ]
    )
    with pytest.raises(RecoveryError) as duplicate_error:
        _repair(duplicate)
    assert duplicate_error.value.code == "duplicate_slate"

    stale = FakeGitHub(comments=[_record(_body(revision=3, drifted=True))])
    with pytest.raises(RecoveryError) as stale_error:
        _repair(stale, if_revision=2)
    assert stale_error.value.code == "revision_conflict"
    assert stale.write_calls == []


@pytest.mark.parametrize(
    ("confirm", "yes", "code"),
    [
        (None, False, "delete_confirmation_required"),
        ("CI", False, "delete_confirmation_mismatch"),
        ("ci ", False, "delete_confirmation_mismatch"),
        ("ci", True, "delete_confirmation_conflict"),
    ],
)
def test_delete_confirmation_is_exact_and_side_effect_free(
    confirm: str | None,
    yes: bool,
    code: str,
) -> None:
    remote = FakeGitHub(comments=[_record(_body())])

    with pytest.raises(RecoveryError) as caught:
        _delete(remote, confirm=confirm, yes=yes)

    assert caught.value.code == code
    assert remote.read_calls == []
    assert remote.write_calls == []


def test_delete_performs_a_second_read_one_delete_and_post_verification() -> None:
    remote = FakeGitHub(comments=[_record(_body())])

    result = _delete(remote)

    assert result.action == "deleted"
    assert result.revision == 3
    assert result.recovered is False
    assert remote.write_calls == [
        (
            "DELETE",
            f"repos/{REPOSITORY}/issues/comments/7",
            HOST,
        )
    ]
    assert remote.comment_reads == 3
    assert remote.comments == []


def test_delete_yes_can_remove_a_corrupt_managed_comment() -> None:
    remote = FakeGitHub(comments=[_record(_corrupt_body())])

    result = _delete(
        remote,
        confirm=None,
        yes=True,
    )

    assert result.action == "deleted"
    assert result.revision is None
    assert result.state_sha256 is None
    assert remote.comments == []


def test_delete_rejects_a_second_read_change_before_delete() -> None:
    remote = FakeGitHub(comments=[_record(_body())])
    remote.before_comment_read[2] = lambda github: github.comments.__setitem__(
        0,
        _record(_body(revision=4)),
    )

    with pytest.raises(RecoveryError) as caught:
        _delete(remote)

    assert caught.value.code == "concurrent_change"
    assert remote.write_calls == []


@pytest.mark.parametrize(
    ("behavior", "recovered", "error_code"),
    [
        ("unknown-applied", True, None),
        ("unknown-unapplied", None, "delete_outcome_unknown"),
        ("no-apply", None, "post_delete_verification_failed"),
    ],
)
def test_delete_never_reports_an_unverified_remote_outcome_as_success(
    behavior: WriteBehavior,
    recovered: bool | None,
    error_code: str | None,
) -> None:
    remote = FakeGitHub(
        comments=[_record(_body())],
        behavior=behavior,
    )

    if error_code is not None:
        with pytest.raises(RecoveryError) as caught:
            _delete(remote)
        assert caught.value.code == error_code
    else:
        result = _delete(remote)
        assert result.action == "deleted"
        assert result.recovered is recovered
    assert len(remote.write_calls) == 1


def test_delete_refetch_failure_after_start_is_unknown() -> None:
    remote = FakeGitHub(
        comments=[_record(_body())],
        fail_comment_read=3,
    )

    with pytest.raises(RecoveryError) as caught:
        _delete(remote)

    assert caught.value.code == "delete_outcome_unknown"
    assert len(remote.write_calls) == 1


def test_delete_stale_revision_and_controller_conflict_do_not_write() -> None:
    stale = FakeGitHub(comments=[_record(_body())])
    with pytest.raises(RecoveryError) as stale_error:
        _delete(stale, if_revision=2)
    assert stale_error.value.code == "revision_conflict"
    assert stale.write_calls == []

    other_controller = FakeGitHub(comments=[_record(_body())])
    with pytest.raises(RecoveryError) as controller_error:
        _delete(other_controller, controller="other-bot")
    assert controller_error.value.code == "controller_conflict"
    assert other_controller.write_calls == []


def test_validate_delete_confirmation_returns_the_validated_name() -> None:
    assert (
        validate_delete_confirmation(
            "ci",
            confirm="ci",
            yes=False,
        )
        == "ci"
    )
    assert (
        validate_delete_confirmation(
            "ci",
            confirm=None,
            yes=True,
        )
        == "ci"
    )
