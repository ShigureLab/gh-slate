from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping

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


def _fake_gh_environment(
    tmp_path: Path,
    response: Mapping[str, object],
) -> tuple[dict[str, str], Path]:
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir()
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ["FAKE_GH_ARGS"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print(os.environ["FAKE_GH_RESPONSE"])
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    arguments = tmp_path / "gh-args.json"
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_GH_ARGS": str(arguments),
            "FAKE_GH_RESPONSE": json.dumps(response),
            "GITHUB_SERVER_URL": "https://github.com",
            "GH_TOKEN": "test-token",
            "PATH": f"{binary.parent}{os.pathsep}{environment['PATH']}",
        }
    )
    return environment, arguments


def test_actions_examples_pass_static_contract() -> None:
    result = _run(str(CHECKER))

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Checked 4 Actions examples\n"


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
) -> None:
    environment, gh_arguments = _fake_gh_environment(
        tmp_path,
        {**_target_response(kind), "action": "stale-event-action"},
    )
    data = tmp_path / "data.json"
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")

    fetched = _run(
        str(ACTIONS / "scripts" / "current_target_to_slate.py"),
        kind,
        "--repository",
        "octo/example",
        "--number",
        "17",
        "--output",
        str(data),
        "--github-env",
        str(github_env),
        env=environment,
    )
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

    assert fetched.returncode == 0, fetched.stderr
    assert rendered.returncode == 0, rendered.stderr
    assert "Unsafe &#124; title &#96;is&#96; escaped" in rendered.stdout
    parsed = json.loads(data.read_text(encoding="utf-8"))
    assert parsed["kind"] == kind
    assert {row["field"] for row in parsed["rows"]}.isdisjoint({"Action"})
    assert {row["field"]: row["value"] for row in parsed["rows"]}["Updated at"] == ("2026-07-31T00:00:00Z")
    expected_path = "pull" if kind == "pull_request" else "issues"
    assert github_env.read_text(encoding="utf-8") == (
        f"GH_SLATE_CURRENT_TARGET=https://github.com/octo/example/{expected_path}/17\n"
    )
    assert json.loads(gh_arguments.read_text(encoding="utf-8")) == [
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        f"repos/octo/example/{resource}/17",
    ]


def test_current_target_rejects_mismatched_api_identity(tmp_path: Path) -> None:
    environment, _arguments = _fake_gh_environment(
        tmp_path,
        _target_response("issue", number=18),
    )
    data = tmp_path / "data.json"
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")

    result = _run(
        str(ACTIONS / "scripts" / "current_target_to_slate.py"),
        "issue",
        "--repository",
        "octo/example",
        "--number",
        "17",
        "--output",
        str(data),
        "--github-env",
        str(github_env),
        env=environment,
    )

    assert result.returncode != 0
    assert "number does not match" in result.stderr
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
                "schema_version": 1,
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
                "schema_version": 1,
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
