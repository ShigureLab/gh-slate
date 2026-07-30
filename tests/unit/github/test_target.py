from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from gh_slate.errors import ExitCode
from gh_slate.github.errors import GitHubReadError
from gh_slate.github.target import (
    MAX_EVENT_BYTES,
    ResolvedTarget,
    resolve_host_context,
    resolve_target,
    target_from_comment_url,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(slots=True)
class FakeLookup:
    repository: tuple[str, str] = ("github.com", "owner/repo")
    pull_request: int = 17
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def current_repository(self, host: str | None) -> tuple[str, str]:
        self.calls.append(("repository", host))
        return self.repository

    def current_pull_request(self, host: str, repository: str) -> int:
        self.calls.append(("pull_request", host, repository))
        return self.pull_request


def _event(
    path: Path,
    *,
    repository: str = "owner/repo",
    host: str = "github.example.com",
    kind: str = "issue",
    number: int = 42,
) -> dict[str, str]:
    target_key = "pull_request" if kind == "pull" else "issue"
    target_path = "pull" if kind == "pull" else "issues"
    payload = {
        target_key: {
            "number": number,
            "html_url": (f"https://{host}/{repository}/{target_path}/{number}"),
        },
        "repository": {
            "full_name": repository,
            "html_url": f"https://{host}/{repository}",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_REPOSITORY": repository,
        "GITHUB_SERVER_URL": f"https://{host}",
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "https://github.com/Owner/Repo/issues/12",
            ResolvedTarget(
                host="github.com",
                repository="Owner/Repo",
                number=12,
                url="https://github.com/Owner/Repo/issues/12",
            ),
        ),
        (
            "https://ghe.example.com/acme/widgets/pull/7/",
            ResolvedTarget(
                host="ghe.example.com",
                repository="acme/widgets",
                number=7,
                url="https://ghe.example.com/acme/widgets/pull/7",
            ),
        ),
    ],
)
def test_full_issue_and_pull_request_urls_supply_all_context(
    source: str,
    expected: ResolvedTarget,
) -> None:
    assert resolve_target(source, environ={}) == expected


def test_comment_url_recovers_canonical_target_context() -> None:
    expected = ResolvedTarget(
        host="ghe.example.com",
        repository="owner/repo",
        number=7,
        url="https://ghe.example.com/owner/repo/issues/7",
    )

    assert target_from_comment_url(
        "https://ghe.example.com/Owner/Repo/pull/7#issuecomment-123",
        expected=expected,
    ) == ResolvedTarget(
        host="ghe.example.com",
        repository="Owner/Repo",
        number=7,
        url="https://ghe.example.com/Owner/Repo/pull/7",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo/issues/1",
        "https://github.com/owner/repo/issues/1#discussion_r1",
        "https://other.example/owner/repo/issues/1#issuecomment-1",
        "https://github.com/owner/other/issues/1#issuecomment-1",
        "https://github.com/owner/repo/issues/2#issuecomment-1",
    ],
)
def test_comment_url_requires_a_matching_comment_location(url: str) -> None:
    expected = ResolvedTarget(
        host="github.com",
        repository="owner/repo",
        number=1,
        url="https://github.com/owner/repo/issues/1",
    )

    with pytest.raises(GitHubReadError):
        target_from_comment_url(url, expected=expected)


def test_url_repo_and_host_conflicts_fail_closed() -> None:
    source = "https://github.com/owner/repo/issues/12"

    with pytest.raises(GitHubReadError) as repo_error:
        resolve_target(
            source,
            repo="other/repo",
            environ={},
        )
    with pytest.raises(GitHubReadError) as host_error:
        resolve_target(
            source,
            host="ghe.example.com",
            environ={},
        )

    assert repo_error.value.code == "target_conflict"
    assert host_error.value.code == "target_conflict"
    assert repo_error.value.exit_code == ExitCode.CONFLICT


def test_positive_number_uses_explicit_repo_and_github_server_url() -> None:
    assert resolve_target(
        "42",
        repo="owner/repo",
        environ={"GITHUB_SERVER_URL": "https://ghe.example.com/"},
    ) == ResolvedTarget(
        host="ghe.example.com",
        repository="owner/repo",
        number=42,
        url="https://ghe.example.com/owner/repo/issues/42",
    )


def test_positive_number_can_resolve_current_repository_via_lookup() -> None:
    lookup = FakeLookup(
        repository=("ghe.example.com", "owner/repo"),
    )

    target = resolve_target(9, lookup=lookup, environ={})

    assert target.host == "ghe.example.com"
    assert target.repository == "owner/repo"
    assert lookup.calls == [("repository", None)]


@pytest.mark.parametrize("target", [0, -1, "0", True, "", "not-a-target"])
def test_invalid_targets_are_rejected(target: object) -> None:
    with pytest.raises(GitHubReadError) as caught:
        resolve_target(target, repo="owner/repo", environ={})  # ty: ignore[invalid-argument-type]

    assert caught.value.code == "target_invalid"


def test_pr_uses_injected_read_only_lookup() -> None:
    lookup = FakeLookup(pull_request=31)

    target = resolve_target("@pr", lookup=lookup, environ={})

    assert target == ResolvedTarget(
        host="github.com",
        repository="owner/repo",
        number=31,
        url="https://github.com/owner/repo/pull/31",
    )
    assert lookup.calls == [
        ("repository", None),
        ("pull_request", "github.com", "owner/repo"),
    ]


@pytest.mark.parametrize(
    ("kind", "url_path"),
    [("issue", "issues"), ("pull", "pull")],
)
def test_event_resolves_issue_and_pull_request_payloads(
    tmp_path: Path,
    kind: str,
    url_path: str,
) -> None:
    event_path = tmp_path / "event.json"
    environ = _event(event_path, kind=kind)

    target = resolve_target("@event", environ=environ)

    assert target == ResolvedTarget(
        host="github.example.com",
        repository="owner/repo",
        number=42,
        url=(f"https://github.example.com/owner/repo/{url_path}/42"),
    )


def test_actions_omitted_target_is_event_but_local_omission_is_error(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "event.json"
    environ = {
        **_event(event_path),
        "GITHUB_ACTIONS": "true",
    }

    assert resolve_target(None, environ=environ).number == 42
    with pytest.raises(GitHubReadError) as local:
        resolve_target(None, environ={})
    assert local.value.code == "target_required"


def test_event_payload_context_conflicts_fail_closed(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    environ = _event(event_path)

    with pytest.raises(GitHubReadError) as repo_error:
        resolve_target(
            "@event",
            repo="other/repo",
            environ=environ,
        )
    with pytest.raises(GitHubReadError) as host_error:
        resolve_target(
            "@event",
            host="other.example.com",
            environ=environ,
        )

    assert repo_error.value.code == "target_conflict"
    assert host_error.value.code == "target_conflict"


def test_event_target_kind_and_url_must_agree(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "pull_request": {
                    "number": 42,
                    "html_url": "https://github.com/owner/repo/issues/42",
                },
                "repository": {"full_name": "owner/repo"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GitHubReadError) as caught:
        resolve_target(
            "@event",
            environ={"GITHUB_EVENT_PATH": str(event_path)},
        )

    assert caught.value.code == "target_conflict"


def test_event_repository_url_cannot_override_server_host(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "issue": {"number": 42},
                "repository": {
                    "full_name": "owner/repo",
                    "html_url": "https://other.example/owner/repo",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GitHubReadError) as caught:
        resolve_target(
            "@event",
            environ={
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_SERVER_URL": "https://ghe.example",
            },
        )

    assert caught.value.code == "target_conflict"


def test_event_file_is_bounded_and_strict_json(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_EVENT_BYTES + 1))
    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        '{"issue":{"number":1},"issue":{"number":2}}',
        encoding="utf-8",
    )

    with pytest.raises(GitHubReadError) as limit_error:
        resolve_target(
            "@event",
            environ={"GITHUB_EVENT_PATH": str(oversized)},
        )
    with pytest.raises(GitHubReadError) as json_error:
        resolve_target(
            "@event",
            environ={"GITHUB_EVENT_PATH": str(malformed)},
        )

    assert limit_error.value.code == "target_event_limit"
    assert json_error.value.code == "target_event_invalid"
    assert json_error.value.details["cause"] == "json_duplicate_key"


def test_current_repository_host_conflict_is_rejected() -> None:
    lookup = FakeLookup(
        repository=("github.com", "owner/repo"),
    )

    with pytest.raises(GitHubReadError) as caught:
        resolve_target(
            1,
            lookup=lookup,
            host="ghe.example.com",
            environ={},
        )

    assert caught.value.code == "target_conflict"


def test_resolved_target_is_frozen() -> None:
    target = resolve_target(
        1,
        repo="owner/repo",
        environ={},
    )

    with pytest.raises((AttributeError, TypeError)):
        target.number = 2  # ty: ignore[invalid-assignment]


def test_host_context_uses_explicit_and_actions_environment() -> None:
    assert resolve_host_context(environ={"GITHUB_SERVER_URL": "https://ghe.example/"}) == "ghe.example"
    assert (
        resolve_host_context(
            environ={
                "GH_HOST": "gh.example",
                "GITHUB_SERVER_URL": "https://actions.example",
            }
        )
        == "gh.example"
    )
    assert resolve_host_context("explicit.example", environ={}) == "explicit.example"
    assert resolve_host_context(environ={}) is None
