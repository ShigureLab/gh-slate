from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from gh_slate.cli import run
from gh_slate.codec import ControllerV1, StateV1
from gh_slate.commands import data as data_commands
from gh_slate.github.models import GitHubActor
from gh_slate.rendering import ListRendererV1, SlateContext, materialize_comment

if TYPE_CHECKING:
    from gh_slate.codec import JsonValue


HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
TARGET_URL = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"
COMMENT_URL = f"{TARGET_URL}#issuecomment-7"
ACTOR_ID = 101


def _comment_body(
    data: object,
    *,
    drifted: bool = False,
) -> str:
    state = StateV1(
        name="ci",
        revision=3,
        controller=ControllerV1(login="ci-bot", id=ACTOR_ID),
        data=cast("dict[str, JsonValue]", data),
        data_schema=None,
        renderer=ListRendererV1(selector=".").to_descriptor(),
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
    if drifted:
        marker_end = body.index("-->") + 3
        return f"{body[:marker_end]}\n\nlocally edited\n"
    return body


@dataclass(slots=True)
class FakeReadProcess:
    data: object
    drifted: bool = False
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def current_actor(self, hostname: str | None = None) -> GitHubActor:
        self.calls.append(("ACTOR", hostname))
        return GitHubActor(id=ACTOR_ID, login="ci-bot")

    def resolve_actor(
        self,
        login: str,
        hostname: str | None = None,
    ) -> GitHubActor:
        self.calls.append(("RESOLVE_ACTOR", login, hostname))
        return GitHubActor(id=ACTOR_ID, login="ci-bot")

    def api_get(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
        paginate: bool = False,
    ) -> object:
        self.calls.append(("GET", endpoint, hostname, paginate))
        assert endpoint == f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100"
        return [
            {
                "id": 7,
                "body": _comment_body(self.data, drifted=self.drifted),
                "html_url": COMMENT_URL,
                "user": {"id": ACTOR_ID, "login": "ci-bot"},
                "created_at": "2026-07-31T00:00:00Z",
                "updated_at": "2026-07-31T00:01:00Z",
            }
        ]

    def repo_view(self, hostname: str | None = None) -> object:
        raise AssertionError("a full target URL must not look up the repository")

    def pr_view(
        self,
        repository: str | None = None,
        hostname: str | None = None,
    ) -> object:
        raise AssertionError("a full target URL must not look up a pull request")


def _install_read(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: object,
    drifted: bool = False,
) -> None:
    process = FakeReadProcess(data=data, drifted=drifted)
    monkeypatch.setattr(data_commands, "_new_process", lambda: process)


@pytest.mark.parametrize(
    ("filter_text", "options", "expected_status", "expected_output"),
    [
        ("empty", ["--exit-status"], 4, ""),
        (".word", ["--raw-output", "--exit-status"], 0, "ready\n"),
        (
            ".items[]",
            ["--compact-output"],
            0,
            '{"name":"one"}\n{"name":"two"}\n',
        ),
        (".disabled", ["--exit-status"], 1, "false\n"),
    ],
)
def test_data_get_supports_zero_one_and_many_jq_results(
    filter_text: str,
    options: list[str],
    expected_status: int,
    expected_output: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_read(
        monkeypatch,
        data={
            "word": "ready",
            "items": [{"name": "one"}, {"name": "two"}],
            "disabled": False,
        },
    )

    assert (
        run(
            [
                "data",
                "get",
                "ci",
                filter_text,
                "--target",
                TARGET_URL,
                *options,
            ]
        )
        == expected_status
    )

    output = capsys.readouterr()
    assert output.out == expected_output
    assert output.err == ""


def test_data_get_warns_on_drift_but_queries_canonical_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_read(
        monkeypatch,
        data={"status": "canonical"},
        drifted=True,
    )

    assert (
        run(
            [
                "data",
                "get",
                "ci",
                ".status",
                "--target",
                TARGET_URL,
                "--raw-output",
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    assert output.out == "canonical\n"
    assert output.err == ("warning[render_drift]: queried canonical data despite visible Markdown drift\n")
