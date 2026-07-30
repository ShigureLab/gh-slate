from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from gh_slate.errors import ExitCode
from gh_slate.github.errors import GitHubReadError
from gh_slate.github.lookup import ProcessTargetLookup


@dataclass
class FakeProcess:
    repository: object = field(
        default_factory=lambda: {
            "nameWithOwner": "owner/repo",
            "url": "https://ghe.example/owner/repo",
        }
    )
    pull_request: object = field(
        default_factory=lambda: {
            "number": 42,
            "url": "https://ghe.example/owner/repo/pull/42",
        }
    )
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def repo_view(self, hostname: str | None = None) -> object:
        self.calls.append(("repo", hostname))
        return self.repository

    def pr_view(
        self,
        repository: str | None = None,
        hostname: str | None = None,
    ) -> object:
        self.calls.append(("pr", repository, hostname))
        return self.pull_request


def lookup(process: FakeProcess) -> ProcessTargetLookup:
    return ProcessTargetLookup(process)


def test_process_lookup_resolves_repository_host_and_pull_request() -> None:
    process = FakeProcess()
    adapter = lookup(process)

    assert adapter.current_repository(None) == (
        "ghe.example",
        "owner/repo",
    )
    assert adapter.current_pull_request("ghe.example", "owner/repo") == 42
    assert process.calls == [
        ("repo", None),
        ("pr", "owner/repo", "ghe.example"),
    ]


@pytest.mark.parametrize(
    ("repository", "pull_request"),
    [
        ({}, {"number": 1}),
        (
            {
                "nameWithOwner": "owner/repo",
                "url": "not-a-url",
            },
            {"number": 1},
        ),
        (
            {
                "nameWithOwner": "owner/repo",
                "url": "https://ghe.example/owner/repo",
            },
            {"number": 0},
        ),
        (
            {
                "nameWithOwner": "owner/repo",
                "url": "https://ghe.example/owner/repo",
            },
            {
                "number": 42,
                "url": "https://other.example/owner/repo/pull/42",
            },
        ),
        (
            {
                "nameWithOwner": "owner/repo",
                "url": "https://ghe.example/other/repo",
            },
            {
                "number": 42,
                "url": "https://ghe.example/owner/repo/pull/42",
            },
        ),
    ],
)
def test_invalid_lookup_responses_fail_closed(
    repository: object,
    pull_request: object,
) -> None:
    process = FakeProcess(
        repository=repository,
        pull_request=pull_request,
    )
    adapter = lookup(process)

    invalid_repository = repository == {} or (
        isinstance(repository, dict)
        and repository.get("url")
        in {
            "not-a-url",
            "https://ghe.example/other/repo",
        }
    )
    if invalid_repository:

        def operation() -> object:
            return adapter.current_repository(None)

    else:

        def operation() -> object:
            return adapter.current_pull_request(
                "ghe.example",
                "owner/repo",
            )

    with pytest.raises(GitHubReadError) as caught:
        operation()

    assert caught.value.code == "target_lookup_invalid"
    assert caught.value.exit_code == ExitCode.VALIDATION
