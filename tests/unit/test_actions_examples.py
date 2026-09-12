from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import partial
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gh_slate.github import GhProcess
from gh_slate.github.process import ProcessResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = ROOT / "examples" / "actions"
CHECKER = ROOT / "scripts" / "check_actions_examples.py"


def _run(
    *arguments: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _target_response(kind: str, *, number: int = 17) -> dict[str, object]:
    response: dict[str, object] = {
        "draft": False,
        "head": {"sha": "a" * 40},
        "html_url": f"https://github.com/octo/example/{'pull' if kind == 'pull_request' else 'issues'}/{number}",
        "number": number,
        "state": "open",
        "title": "Unsafe | title `is` escaped",
        "updated_at": "2026-07-31T00:00:00Z",
        "user": {"login": "octocat"},
    }
    if kind == "issue":
        response.pop("draft")
        response.pop("head")
    return response


@dataclass(slots=True)
class _FakeGhRunner:
    response: Mapping[str, object]
    calls: list[tuple[tuple[str, ...], float, str | None, int, int]] = field(default_factory=list)

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        hostname: str | None,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        self.calls.append(
            (
                argv,
                timeout,
                hostname,
                max_stdout_bytes,
                max_stderr_bytes,
            )
        )
        return ProcessResult(
            returncode=0,
            stdout=json.dumps(self.response).encode("utf-8"),
            stderr=b"",
        )


def _load_current_target_module() -> ModuleType:
    path = ACTIONS / "scripts" / "current_target_to_slate.py"
    spec = spec_from_file_location("_gh_slate_current_target_example", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise AssertionError("current target example must be importable")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_current_target(
    monkeypatch: pytest.MonkeyPatch,
    response: Mapping[str, object],
    *arguments: str,
) -> tuple[Callable[[], int], _FakeGhRunner]:
    module = _load_current_target_module()
    runner = _FakeGhRunner(response)
    monkeypatch.setattr(module, "GhProcess", partial(GhProcess, runner=runner))
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(ACTIONS / "scripts" / "current_target_to_slate.py"), *arguments],
    )
    entrypoint: Callable[[], int] = module.main
    return entrypoint, runner


def test_actions_examples_pass_static_contract() -> None:
    result = _run(str(CHECKER))

    assert result.returncode == 0, result.stderr


def test_static_contract_rejects_write_permission_in_reducer(
    tmp_path: Path,
) -> None:
    shutil.copytree(ACTIONS, tmp_path / "examples" / "actions")
    reducer = tmp_path / "examples" / "actions" / "pull-request-target-reducer.yml"
    reducer.write_text(
        reducer.read_text(encoding="utf-8").replace(
            "permissions: {}",
            "permissions:\n  contents: write",
        ),
        encoding="utf-8",
    )

    result = _run(str(CHECKER), str(tmp_path))

    assert result.returncode == 1
    assert "permissions must be exactly {}" in result.stderr


def test_static_contract_rejects_artifact_execution(tmp_path: Path) -> None:
    shutil.copytree(ACTIONS, tmp_path / "examples" / "actions")
    consumer = tmp_path / "examples" / "actions" / "pull-request-target-consumer.yml"
    consumer.write_text(
        consumer.read_text(encoding="utf-8").replace(
            "          python trusted/examples/actions/scripts/validate_reduced_pull_request.py",
            '          source "$REDUCED_PATH"\n'
            "          python trusted/examples/actions/scripts/validate_reduced_pull_request.py",
        ),
        encoding="utf-8",
    )

    result = _run(str(CHECKER), str(tmp_path))

    assert result.returncode == 1
    assert "must never execute or source the downloaded artifact" in result.stderr


def test_static_contract_rejects_event_snapshot_as_dashboard_state(
    tmp_path: Path,
) -> None:
    shutil.copytree(ACTIONS, tmp_path / "examples" / "actions")
    workflow = tmp_path / "examples" / "actions" / "issue-dashboard.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "current_target_to_slate.py",
            "event_to_slate.py",
        ),
        encoding="utf-8",
    )

    result = _run(str(CHECKER), str(tmp_path))

    assert result.returncode == 1
    assert "fetch the current target" in result.stderr


def test_static_contract_rejects_dependabot_direct_writer(tmp_path: Path) -> None:
    shutil.copytree(ACTIONS, tmp_path / "examples" / "actions")
    workflow = tmp_path / "examples" / "actions" / "pull-request-dashboard.yml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            " &&\n      github.event.pull_request.user.login != 'dependabot[bot]'",
            "",
        ),
        encoding="utf-8",
    )

    result = _run(str(CHECKER), str(tmp_path))

    assert result.returncode == 1
    assert "must skip fork and Dependabot tokens" in result.stderr


@pytest.mark.parametrize(
    ("filename", "activity"),
    [
        ("issue-dashboard.yml", "edited"),
        ("pull-request-dashboard.yml", "ready_for_review"),
        ("pull-request-target-reducer.yml", "ready_for_review"),
    ],
)
def test_static_contract_rejects_missing_displayed_field_activity(
    tmp_path: Path,
    filename: str,
    activity: str,
) -> None:
    shutil.copytree(ACTIONS, tmp_path / "examples" / "actions")
    workflow = tmp_path / "examples" / "actions" / filename
    source = workflow.read_text(encoding="utf-8")
    workflow.write_text(
        source.replace(f"{activity}, ", "", 1),
        encoding="utf-8",
    )

    result = _run(str(CHECKER), str(tmp_path))

    assert result.returncode == 1
    assert "activity types must cover exactly the displayed resource fields" in result.stderr


@pytest.mark.parametrize(
    ("kind", "resource", "schema", "template"),
    [
        (
            "issue",
            "issues",
            "issue-dashboard.schema.json",
            "issue-dashboard.md.j2",
        ),
        (
            "pull_request",
            "pulls",
            "pull-request-dashboard.schema.json",
            "pull-request-dashboard.md.j2",
        ),
    ],
)
def test_current_target_contract_fetches_and_renders(
    tmp_path: Path,
    kind: str,
    resource: str,
    schema: str,
    template: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data.json"
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")
    main, runner = _prepare_current_target(
        monkeypatch,
        {**_target_response(kind), "action": "stale-event-action"},
        kind,
        "--repository",
        "octo/example",
        "--number",
        "17",
        "--output",
        str(data),
        "--github-env",
        str(github_env),
    )
    fetched = main()
    rendered = _run(
        "-m",
        "gh_slate",
        "render",
        "actions-example",
        "--data",
        str(data),
        "--schema",
        str(ACTIONS / "schemas" / schema),
        "--template",
        str(ACTIONS / "templates" / template),
    )

    assert fetched == 0
    assert rendered.returncode == 0, rendered.stderr
    assert "Unsafe &#124; title &#96;is&#96; escaped" in rendered.stdout
    parsed = json.loads(data.read_text(encoding="utf-8"))
    assert parsed["kind"] == kind
    assert {row["field"] for row in parsed["rows"]}.isdisjoint({"Action", "Updated at"})
    expected_path = "pull" if kind == "pull_request" else "issues"
    assert github_env.read_text(encoding="utf-8") == (
        f"GH_SLATE_CURRENT_TARGET=https://github.com/octo/example/{expected_path}/17\n"
    )
    assert runner.calls == [
        (
            (
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                f"repos/octo/example/{resource}/17",
            ),
            30.0,
            "github.com",
            1024 * 1024,
            64 * 1024,
        )
    ]


def test_current_target_rejects_mismatched_api_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data.json"
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")
    main, _runner = _prepare_current_target(
        monkeypatch,
        _target_response("issue", number=18),
        "issue",
        "--repository",
        "octo/example",
        "--number",
        "17",
        "--output",
        str(data),
        "--github-env",
        str(github_env),
    )

    with pytest.raises(SystemExit, match="number does not match"):
        main()
    assert not data.exists()
    assert github_env.read_text(encoding="utf-8") == ""


def test_trusted_consumer_validates_artifact_before_exporting_target(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "pull-request.json"
    artifact.write_text(
        json.dumps(
            {
                "repository": "octo/example",
                "target_number": 42,
            }
        ),
        encoding="utf-8",
    )
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")

    result = _run(
        str(ACTIONS / "scripts" / "validate_reduced_pull_request.py"),
        "--input",
        str(artifact),
        "--github-env",
        str(github_env),
        "--schema",
        str(ACTIONS / "schemas" / "reduced-pull-request.schema.json"),
        "--expected-repository",
        "octo/example",
    )

    assert result.returncode == 0, result.stderr
    assert github_env.read_text(encoding="utf-8") == ("GH_SLATE_REPOSITORY=octo/example\nGH_SLATE_TARGET=42\n")


def test_trusted_consumer_rejects_schema_mismatch_without_export(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "pull-request.json"
    artifact.write_text(
        json.dumps(
            {
                "repository": "other/repository",
                "target_number": 42,
                "unexpected": "field",
            }
        ),
        encoding="utf-8",
    )
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")

    result = _run(
        str(ACTIONS / "scripts" / "validate_reduced_pull_request.py"),
        "--input",
        str(artifact),
        "--github-env",
        str(github_env),
        "--schema",
        str(ACTIONS / "schemas" / "reduced-pull-request.schema.json"),
        "--expected-repository",
        "octo/example",
    )

    assert result.returncode != 0
    assert "fixed schema" in result.stderr
    assert github_env.read_text(encoding="utf-8") == ""


def test_trusted_consumer_rejects_oversized_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "pull-request.json"
    artifact.write_bytes(b" " * (16 * 1024 + 1))
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")

    result = _run(
        str(ACTIONS / "scripts" / "validate_reduced_pull_request.py"),
        "--input",
        str(artifact),
        "--github-env",
        str(github_env),
        "--schema",
        str(ACTIONS / "schemas" / "reduced-pull-request.schema.json"),
        "--expected-repository",
        "octo/example",
    )

    assert result.returncode != 0
    assert "artifact exceeds 16384 bytes" in result.stderr
    assert github_env.read_text(encoding="utf-8") == ""
