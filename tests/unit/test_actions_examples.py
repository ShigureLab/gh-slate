from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = ROOT / "examples" / "actions"
CHECKER = ROOT / "scripts" / "check_actions_examples.py"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _event_payload(kind: str) -> dict[str, object]:
    item = {
        "draft": False,
        "head": {"sha": "a" * 40},
        "html_url": f"https://github.com/octo/example/{'pull' if kind == 'pull_request' else 'issues'}/17",
        "number": 17,
        "state": "open",
        "title": "Unsafe | title `is` escaped",
        "user": {"login": "octocat"},
    }
    return {
        "action": "opened",
        kind: item,
        "repository": {
            "default_branch": "main",
            "full_name": "octo/example",
        },
    }


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


@pytest.mark.parametrize(
    ("kind", "schema", "template"),
    [
        (
            "issue",
            "issue-dashboard.schema.json",
            "issue-dashboard.md.j2",
        ),
        (
            "pull_request",
            "pull-request-dashboard.schema.json",
            "pull-request-dashboard.md.j2",
        ),
    ],
)
def test_event_payload_contract_reduces_and_renders(
    tmp_path: Path,
    kind: str,
    schema: str,
    template: str,
) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps(_event_payload(kind)), encoding="utf-8")
    data = tmp_path / "data.json"

    reduced = _run(
        str(ACTIONS / "scripts" / "event_to_slate.py"),
        kind,
        str(event),
        str(data),
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

    assert reduced.returncode == 0, reduced.stderr
    assert rendered.returncode == 0, rendered.stderr
    assert "Unsafe &#124; title &#96;is&#96; escaped" in rendered.stdout
    assert json.loads(data.read_text(encoding="utf-8"))["kind"] == kind


def test_event_reducer_rejects_oversized_payload(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_bytes(b" " * (1024 * 1024 + 1))

    result = _run(
        str(ACTIONS / "scripts" / "event_to_slate.py"),
        "issue",
        str(event),
        str(tmp_path / "data.json"),
    )

    assert result.returncode != 0
    assert "event payload exceeds 1048576 bytes" in result.stderr


def test_trusted_consumer_validates_artifact_before_exporting_target(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "pull-request.json"
    artifact.write_text(
        json.dumps(
            {
                "action": "synchronize",
                "author": "octocat",
                "draft": False,
                "head_sha": "b" * 40,
                "repository": "octo/example",
                "schema_version": 1,
                "target_number": 42,
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data.json"
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")

    result = _run(
        str(ACTIONS / "scripts" / "validate_reduced_pull_request.py"),
        "--input",
        str(artifact),
        "--data",
        str(data),
        "--github-env",
        str(github_env),
        "--schema",
        str(ACTIONS / "schemas" / "reduced-pull-request.schema.json"),
        "--expected-repository",
        "octo/example",
    )

    assert result.returncode == 0, result.stderr
    assert github_env.read_text(encoding="utf-8") == ("GH_SLATE_REPOSITORY=octo/example\nGH_SLATE_TARGET=42\n")
    assert json.loads(data.read_text(encoding="utf-8")) == {
        "kind": "pull_request",
        "rows": [
            {"field": "Repository", "value": "octo/example"},
            {"field": "Number", "value": 42},
            {"field": "Action", "value": "synchronize"},
            {"field": "Author", "value": "octocat"},
            {"field": "Draft", "value": False},
            {"field": "Head SHA", "value": "b" * 40},
        ],
    }


def test_trusted_consumer_rejects_schema_mismatch_without_export(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "pull-request.json"
    artifact.write_text(
        json.dumps(
            {
                "action": "opened",
                "author": "octocat",
                "draft": False,
                "head_sha": "c" * 40,
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
        "--data",
        str(tmp_path / "data.json"),
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
    assert not (tmp_path / "data.json").exists()


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
        "--data",
        str(tmp_path / "data.json"),
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
    assert not (tmp_path / "data.json").exists()
