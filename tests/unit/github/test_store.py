from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from gh_slate.codec import ControllerV1, StateV1, encode_comment, render_sha256
from gh_slate.errors import ExitCode
from gh_slate.github.errors import GitHubReadError
from gh_slate.github.models import GitHubActor
from gh_slate.github.store import CommentStore
from gh_slate.rendering import (
    ListRendererV1,
)

_EMPTY_HASH = "0" * 64
ACTOR_ID = 101
OTHER_ACTOR_ID = 202


@dataclass(frozen=True)
class Target:
    host: str = "github.example"
    repository: str = "owner/repo"
    number: int = 42


class FakeClient:
    def __init__(
        self,
        response: object,
        *,
        actor: GitHubActor | None = None,
        resolved_actors: dict[str, GitHubActor] | None = None,
    ) -> None:
        actor = actor or GitHubActor(id=ACTOR_ID, login="ci-bot")
        self.response = response
        self.actor = actor
        self.resolved_actors = resolved_actors or {
            actor.login.casefold(): actor,
        }
        self.calls: list[tuple[object, ...]] = []

    def api_get(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
        paginate: bool = False,
    ) -> object:
        self.calls.append(("GET", endpoint, hostname, paginate))
        return self.response

    def current_actor(self, hostname: str | None = None) -> GitHubActor:
        self.calls.append(("ACTOR", hostname))
        return self.actor

    def resolve_actor(
        self,
        login: str,
        hostname: str | None = None,
    ) -> GitHubActor:
        self.calls.append(("RESOLVE_ACTOR", login, hostname))
        return self.resolved_actors[login.casefold()]


def _body(
    name: str = "ci",
    *,
    controller: str = "ci-bot",
    controller_id: int | None = ACTOR_ID,
    visible_value: str = "ready",
) -> str:
    state = StateV1(
        name=name,
        revision=1,
        controller=ControllerV1(login=controller, id=controller_id),
        data={"status": visible_value},
        renderer=ListRendererV1(
            selector=".status",
        ).to_descriptor(),
        render_sha256=render_sha256(f"- {visible_value}\n"),
    )
    return encode_comment(state, f"- {visible_value}\n").body


def _record(
    identifier: int,
    body: str,
    *,
    author: str | None = "ci-bot",
    author_id: int = ACTOR_ID,
) -> dict[str, object]:
    return {
        "id": identifier,
        "body": body,
        "html_url": (f"https://github.example/owner/repo/issues/42#issuecomment-{identifier}"),
        "user": None if author is None else {"id": author_id, "login": author},
        "created_at": "2026-07-31T00:00:00Z",
        "updated_at": "2026-07-31T00:01:00Z",
    }


def test_find_paginates_all_pages_and_uses_current_actor_by_default() -> None:
    client = FakeClient(
        [
            [_record(1, "ordinary")],
            [_record(2, _body())],
        ]
    )
    store = CommentStore(client)

    slate = store.find(Target(), "ci")

    assert slate.comment.id == 2
    assert slate.status == "valid"
    assert client.calls == [
        ("ACTOR", "github.example"),
        (
            "GET",
            "repos/owner/repo/issues/42/comments?per_page=100",
            "github.example",
            True,
        ),
    ]


def test_explicit_controller_resolves_login_to_stable_id() -> None:
    client = FakeClient([_record(1, _body(controller="CI-Bot"), author="ci-bot")])

    slate = CommentStore(client).find(
        Target(),
        "ci",
        controller="CI-BOT",
    )

    assert slate.comment.id == 1
    assert (
        "RESOLVE_ACTOR",
        "CI-BOT",
        "github.example",
    ) in client.calls
    assert all(call[0] != "ACTOR" for call in client.calls)


def test_wrong_author_marker_is_not_adopted() -> None:
    client = FakeClient(
        [
            _record(
                1,
                _body(),
                author="attacker",
                author_id=OTHER_ACTOR_ID,
            )
        ]
    )

    with pytest.raises(GitHubReadError) as caught:
        CommentStore(client).find(Target(), "ci")

    assert caught.value.code == "slate_not_found"
    assert caught.value.exit_code == ExitCode.NOT_FOUND


def test_stored_controller_mismatch_is_classified_as_corrupt() -> None:
    client = FakeClient(
        [
            _record(
                1,
                _body(
                    controller="other",
                    controller_id=OTHER_ACTOR_ID,
                ),
                author="ci-bot",
            )
        ]
    )

    candidates = CommentStore(client).candidates(Target())

    assert len(candidates) == 1
    assert candidates[0].status == "corrupt"
    assert candidates[0].error_code == "controller_mismatch"


def test_controller_login_rename_does_not_orphan_the_same_actor_id() -> None:
    client = FakeClient(
        [
            _record(
                1,
                _body(controller="old-login"),
                author="new-login",
            )
        ],
        actor=GitHubActor(id=ACTOR_ID, login="new-login"),
    )

    slate = CommentStore(client).find(Target(), "ci")

    assert slate.comment.id == 1
    assert slate.comment.author == "new-login"
    assert slate.decoded.state.controller.login == "old-login"


def test_same_login_with_a_different_actor_id_is_not_adopted() -> None:
    client = FakeClient(
        [
            _record(
                1,
                _body(),
                author="ci-bot",
                author_id=OTHER_ACTOR_ID,
            )
        ]
    )

    with pytest.raises(GitHubReadError) as caught:
        CommentStore(client).find(Target(), "ci")

    assert caught.value.code == "slate_not_found"


def test_legacy_login_only_state_uses_comment_author_id_safely() -> None:
    client = FakeClient(
        [
            _record(
                1,
                _body(controller="old-login", controller_id=None),
                author="new-login",
            )
        ],
        actor=GitHubActor(id=ACTOR_ID, login="new-login"),
    )

    assert CommentStore(client).find(Target(), "ci").comment.id == 1


def test_forged_marker_name_cannot_relabel_stored_state() -> None:
    forged = _body(name="other").replace(
        "<!-- gh-slate:v1 name=other ",
        "<!-- gh-slate:v1 name=ci ",
        1,
    )
    client = FakeClient([_record(1, forged)])

    candidates = CommentStore(client).candidates(Target(), name="ci")

    assert len(candidates) == 1
    assert candidates[0].status == "corrupt"
    assert candidates[0].error_code == "state_name_mismatch"


def test_visible_drift_remains_readable_and_is_reported() -> None:
    drifted = _body().replace("- ready\n", "- manually edited\n")
    client = FakeClient([_record(1, drifted)])

    slate = CommentStore(client).find(Target(), "ci")

    assert slate.status == "drifted"
    assert slate.decoded.visible_markdown == "- manually edited\n"
    assert slate.decoded.state.data["status"] == "ready"


def test_corrupt_matching_state_fails_with_cause_without_selecting_it() -> None:
    corrupt = _body().replace("eA", "eB", 1)
    client = FakeClient([_record(1, corrupt)])
    store = CommentStore(client)

    candidates = store.candidates(Target())
    assert candidates[0].status == "corrupt"
    assert candidates[0].error_code is not None

    with pytest.raises(GitHubReadError) as caught:
        store.find(Target(), "ci")
    assert caught.value.code == "slate_corrupt"
    assert caught.value.exit_code == ExitCode.VALIDATION


def test_duplicate_matches_are_never_silently_selected() -> None:
    client = FakeClient(
        [
            _record(10, _body()),
            _record(20, _body()),
        ]
    )
    store = CommentStore(client)

    assert [item.status for item in store.candidates(Target())] == [
        "duplicate",
        "duplicate",
    ]
    with pytest.raises(GitHubReadError) as caught:
        store.find(Target(), "ci")

    assert caught.value.code == "duplicate_slate"
    assert caught.value.exit_code == ExitCode.CONFLICT
    assert caught.value.details["comment_ids"] == [10, 20]


@pytest.mark.parametrize(
    "response",
    [
        {},
        ["not-an-object"],
        [{"id": 1, "body": "x"}],
        [
            {
                "id": True,
                "body": "x",
                "html_url": "https://example.invalid",
                "user": {"id": ACTOR_ID, "login": "ci-bot"},
            }
        ],
        [
            {
                "id": 1,
                "body": "x",
                "html_url": "https://github.example/owner/repo/issues/42#issuecomment-1",
                "user": {"login": "ci-bot"},
            }
        ],
    ],
)
def test_invalid_github_comment_shapes_fail_closed(response: object) -> None:
    with pytest.raises(GitHubReadError) as caught:
        CommentStore(FakeClient(response)).comments(Target())

    assert caught.value.code == "github_response_invalid"


def test_comment_id_must_match_its_html_url_fragment() -> None:
    record = _record(999, _body())
    record["html_url"] = "https://github.example/owner/repo/issues/42#issuecomment-7"

    with pytest.raises(GitHubReadError) as caught:
        CommentStore(FakeClient([record])).comments(Target())

    assert caught.value.code == "github_response_invalid"
    assert caught.value.details["cause_code"] == "target_conflict"


def test_comment_url_fragment_digit_limit_is_normalized() -> None:
    record = _record(1, _body())
    record["html_url"] = "https://github.example/owner/repo/issues/42#issuecomment-" + ("9" * 5000)

    with pytest.raises(GitHubReadError) as caught:
        CommentStore(FakeClient([record])).comments(Target())

    assert caught.value.code == "github_response_invalid"
    assert caught.value.details["cause_code"] == "comment_url_invalid"


def test_ghost_comments_do_not_match_a_controller() -> None:
    client = FakeClient([_record(1, _body(), author=None)])

    assert CommentStore(client).candidates(Target()) == ()


def test_flat_and_slurped_page_arrays_are_both_accepted() -> None:
    body = _body()
    flat = CommentStore(FakeClient([_record(1, body)])).comments(Target())
    paged = CommentStore(FakeClient([[_record(1, body)]])).comments(Target())

    assert flat == paged
    assert cast("tuple[object, ...]", flat)
