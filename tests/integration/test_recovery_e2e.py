from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.fake_gh import (
    STATE_ENV,
    committed_write_methods,
    initialize_state,
    inject_fault,
    install_executable,
    load_state,
    replace_visible_markdown,
)

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "gh-slate"
HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42


@dataclass(frozen=True, slots=True)
class LauncherSandbox:
    target: str
    state_path: Path
    environment: dict[str, str]

    def run(
        self,
        *arguments: str,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [LAUNCHER, *arguments],
            input=stdin,
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=self.environment,
            timeout=30,
        )


@pytest.fixture
def launcher_sandbox(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> LauncherSandbox:
    kind = request.param
    target = f"https://{HOST}/{REPOSITORY}/{kind}/{NUMBER}"
    state_path = tmp_path / "fake-github.json"
    initialize_state(state_path, target_url=target)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    install_executable(fake_bin / "gh")
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment[STATE_ENV] = str(state_path)
    return LauncherSandbox(
        target=target,
        state_path=state_path,
        environment=environment,
    )


def _json_result(
    result: subprocess.CompletedProcess[str],
) -> dict[str, object]:
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _create(
    sandbox: LauncherSandbox,
    tmp_path: Path,
) -> dict[str, object]:
    data = tmp_path / "data.json"
    data.write_text(
        '{"jobs":[{"name":"linux","status":"pass"}],"meta":{"attempt":1}}',
        encoding="utf-8",
    )
    return _json_result(
        sandbox.run(
            "apply",
            "ci",
            "--target",
            sandbox.target,
            "--mode",
            "create",
            "--data",
            str(data),
            "--table",
            ".jobs",
            "--columns",
            "name,status",
            "--json",
        )
    )


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="the real gh-slate extension launcher requires uv",
)
@pytest.mark.parametrize(
    "launcher_sandbox",
    ["issues", "pull"],
    indirect=True,
)
def test_real_launcher_issue_and_pr_recovery_lifecycle(
    launcher_sandbox: LauncherSandbox,
    tmp_path: Path,
) -> None:
    sandbox = launcher_sandbox
    created = _create(sandbox, tmp_path)
    assert created["action"] == "created"
    assert created["revision"] == 1
    assert committed_write_methods(sandbox.state_path) == ["POST"]

    viewed = _json_result(
        sandbox.run(
            "view",
            "ci",
            "--target",
            sandbox.target,
            "--json",
        )
    )
    assert viewed["status"] == "valid"
    assert viewed["revision"] == 1
    assert committed_write_methods(sandbox.state_path) == ["POST"]

    updated = _json_result(
        sandbox.run(
            "data",
            "set",
            "ci",
            ".jobs[0].status",
            "--target",
            sandbox.target,
            "--value-string",
            "fail",
            "--json",
        )
    )
    assert updated["action"] == "updated"
    assert updated["revision"] == 2
    assert committed_write_methods(sandbox.state_path) == ["POST", "PATCH"]

    comment_id = created["comment_id"]
    assert isinstance(comment_id, int)
    replace_visible_markdown(
        sandbox.state_path,
        comment_id=comment_id,
        markdown="manually edited projection\n",
    )
    drifted = _json_result(
        sandbox.run(
            "view",
            "ci",
            "--target",
            sandbox.target,
            "--json",
        )
    )
    assert drifted["status"] == "drifted"
    assert drifted["revision"] == 2

    writes_before_rejected_mutation = committed_write_methods(sandbox.state_path)
    rejected = sandbox.run(
        "data",
        "set",
        "ci",
        ".meta.attempt",
        "--target",
        sandbox.target,
        "--value",
        "2",
        "--json",
    )
    assert rejected.returncode != 0
    assert rejected.stdout == ""
    assert "render_drift" in rejected.stderr
    assert committed_write_methods(sandbox.state_path) == writes_before_rejected_mutation

    repaired = _json_result(
        sandbox.run(
            "repair",
            "ci",
            "--from-state",
            "--target",
            sandbox.target,
            "--json",
        )
    )
    assert repaired["action"] == "repaired"
    assert repaired["revision"] == 2
    assert committed_write_methods(sandbox.state_path) == [
        "POST",
        "PATCH",
        "PATCH",
    ]

    verified = _json_result(
        sandbox.run(
            "state",
            "verify",
            "ci",
            "--target",
            sandbox.target,
            "--json",
        )
    )
    assert verified["verified"] is True
    assert verified["revision"] == 2

    writes_before_unchanged = committed_write_methods(sandbox.state_path)
    unchanged = _json_result(
        sandbox.run(
            "repair",
            "ci",
            "--from-state",
            "--target",
            sandbox.target,
            "--json",
        )
    )
    assert unchanged["action"] == "unchanged"
    assert unchanged["revision"] == 2
    assert committed_write_methods(sandbox.state_path) == writes_before_unchanged

    events_before_bad_confirmation = len(load_state(sandbox.state_path)["events"])
    mismatched = sandbox.run(
        "delete",
        "ci",
        "--target",
        sandbox.target,
        "--confirm",
        "not-ci",
        "--json",
    )
    assert mismatched.returncode != 0
    assert mismatched.stdout == ""
    assert "delete_confirmation_mismatch" in mismatched.stderr
    assert len(load_state(sandbox.state_path)["events"]) == events_before_bad_confirmation

    confirmation = ("--confirm", "ci") if "/issues/" in sandbox.target else ("--yes",)
    deleted = _json_result(
        sandbox.run(
            "delete",
            "ci",
            "--target",
            sandbox.target,
            *confirmation,
            "--json",
        )
    )
    assert deleted["action"] == "deleted"
    assert deleted["revision"] == 2
    assert committed_write_methods(sandbox.state_path) == [
        "POST",
        "PATCH",
        "PATCH",
        "DELETE",
    ]

    listed = sandbox.run(
        "list",
        "--target",
        sandbox.target,
        "--json",
    )
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="the real gh-slate extension launcher requires uv",
)
@pytest.mark.parametrize(
    "launcher_sandbox",
    ["issues"],
    indirect=True,
)
def test_fake_github_faults_distinguish_recovered_and_unknown_writes(
    launcher_sandbox: LauncherSandbox,
    tmp_path: Path,
) -> None:
    sandbox = launcher_sandbox
    created = _create(sandbox, tmp_path)
    comment_id = created["comment_id"]
    assert isinstance(comment_id, int)
    endpoint = f"repos/{REPOSITORY}/issues/comments/{comment_id}"

    inject_fault(
        sandbox.state_path,
        method="PATCH",
        endpoint=endpoint,
        phase="after",
        mode="invalid_json",
    )
    recovered = _json_result(
        sandbox.run(
            "data",
            "set",
            "ci",
            ".meta.attempt",
            "--target",
            sandbox.target,
            "--value",
            "2",
            "--json",
        )
    )
    assert recovered["action"] == "updated"
    assert recovered["revision"] == 2
    assert recovered["recovered"] is True
    assert committed_write_methods(sandbox.state_path) == ["POST", "PATCH"]

    inject_fault(
        sandbox.state_path,
        method="PATCH",
        endpoint=endpoint,
        phase="before",
        mode="invalid_json",
    )
    unknown = sandbox.run(
        "data",
        "set",
        "ci",
        ".meta.attempt",
        "--target",
        sandbox.target,
        "--value",
        "3",
        "--json",
    )
    assert unknown.returncode != 0
    assert unknown.stdout == ""
    assert "write_outcome_unknown" in unknown.stderr
    assert committed_write_methods(sandbox.state_path) == ["POST", "PATCH"]

    comments_endpoint = f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100"
    inject_fault(
        sandbox.state_path,
        method="GET",
        endpoint=comments_endpoint,
        phase="before",
        mode="invalid_json",
    )
    read_failure = sandbox.run(
        "view",
        "ci",
        "--target",
        sandbox.target,
        "--json",
    )
    assert read_failure.returncode != 0
    assert read_failure.stdout == ""
    assert "gh_json_invalid" in read_failure.stderr
    assert committed_write_methods(sandbox.state_path) == ["POST", "PATCH"]
