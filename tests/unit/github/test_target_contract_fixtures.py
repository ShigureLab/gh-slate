from __future__ import annotations

from pathlib import Path

import pytest

from gh_slate.github.errors import GitHubReadError
from gh_slate.github.target import ResolvedTarget, resolve_target

EVENTS = Path(__file__).resolve().parents[2] / "fixtures" / "github" / "events"
GHES_HOST = "github.enterprise.example"


@pytest.mark.parametrize(
    ("fixture", "expected_path", "number"),
    [
        ("issue-ghes.json", "issues", 42),
        ("pull-request-ghes.json", "pull", 73),
    ],
)
def test_ghes_event_fixtures_preserve_host_and_target_contract(
    fixture: str,
    expected_path: str,
    number: int,
) -> None:
    event_path = EVENTS / fixture

    target = resolve_target(
        "@event",
        environ={
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": "acme/widgets",
            "GITHUB_SERVER_URL": f"https://{GHES_HOST}",
        },
    )

    assert target == ResolvedTarget(
        host=GHES_HOST,
        repository="acme/widgets",
        number=number,
        url=f"https://{GHES_HOST}/acme/widgets/{expected_path}/{number}",
    )


@pytest.mark.parametrize(
    "fixture",
    ["issue-ghes.json", "pull-request-ghes.json"],
)
def test_ghes_event_fixtures_reject_a_conflicting_host(
    fixture: str,
) -> None:
    with pytest.raises(GitHubReadError) as caught:
        resolve_target(
            "@event",
            host="github.com",
            environ={
                "GITHUB_EVENT_PATH": str(EVENTS / fixture),
                "GITHUB_REPOSITORY": "acme/widgets",
                "GITHUB_SERVER_URL": f"https://{GHES_HOST}",
            },
        )

    assert caught.value.code == "target_conflict"
