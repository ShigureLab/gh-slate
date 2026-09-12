from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "gh-slate"
CONFIRMATION = "I_UNDERSTAND"
CONFIRMATION_ENV = "GH_SLATE_LIVE_CONFIRM"
ISSUE_URL_ENV = "GH_SLATE_LIVE_DISPOSABLE_ISSUE_URL"
PR_URL_ENV = "GH_SLATE_LIVE_DISPOSABLE_PR_URL"


def _live_ready() -> bool:
    return (
        os.environ.get(CONFIRMATION_ENV) == CONFIRMATION
        and bool(os.environ.get(ISSUE_URL_ENV))
        and bool(os.environ.get(PR_URL_ENV))
        and shutil.which("gh") is not None
        and shutil.which("uv") is not None
    )


pytestmark = pytest.mark.skipif(
    not _live_ready(),
    reason=("live GitHub tests require the exact confirmation, both disposable target URLs, gh, and uv"),
)


def _extension(
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [LAUNCHER, *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=os.environ.copy(),
        timeout=60,
    )


def _json_result(
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _target_contract(
    target_url: str,
    *,
    kind: str,
) -> tuple[str, str]:
    parsed = urlsplit(target_url)
    parts = parsed.path.strip("/").split("/")
    expected_path = "issues" if kind == "issue" else "pull"
    assert parsed.scheme == "https"
    assert parsed.hostname
    assert not parsed.query
    assert not parsed.fragment
    assert len(parts) == 4
    assert parts[2] == expected_path
    assert parts[3].isdecimal()
    return parsed.netloc, f"{parts[0]}/{parts[1]}"


def _gh_api(
    *,
    host: str,
    method: str,
    endpoint: str,
    payload: object | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        shutil.which("gh") or "gh",
        "api",
        "--hostname",
        host,
        "--method",
        method,
    ]
    stdin: str | None = None
    if payload is not None:
        arguments.extend(("--input", "-"))
        stdin = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    arguments.append(endpoint)
    return subprocess.run(
        arguments,
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=os.environ.copy(),
        timeout=60,
    )


@pytest.mark.parametrize(
    ("kind", "target_env"),
    [
        ("issue", ISSUE_URL_ENV),
        ("pull", PR_URL_ENV),
    ],
)
def test_live_disposable_issue_and_pr_lifecycle(
    kind: str,
    target_env: str,
    tmp_path: Path,
) -> None:
    assert os.environ.get(CONFIRMATION_ENV) == CONFIRMATION
    target = os.environ[target_env]
    host, repository = _target_contract(target, kind=kind)
    name = f"live-{uuid.uuid4().hex[:16]}"
    definitions = tmp_path / "definitions"
    shutil.copytree(ROOT / "examples/profiles", definitions)
    suffix = os.environ.get("GH_SLATE_LIVE_COMMENT_SUFFIX", "")
    for template in definitions.glob("*.j2"):
        template.write_text(template.read_text() + "\n" + suffix + "\n")
    data_file = tmp_path / "candidate.json"
    data_file.write_text((definitions / "review-changes.json").read_text())
    config = definitions / "boards.toml"
    comment_id: int | None = None
    deleted = False

    print(f"Live target {target}, slate {name}", flush=True)
    try:
        created = _json_result(
            _extension(
                "apply",
                name,
                "--target",
                target,
                "--mode",
                "create",
                "--data",
                str(data_file),
                "--config",
                str(config),
                "--profile",
                "review",
                "--json",
            )
        )
        assert created["action"] == "created"
        assert created["revision"] == 1
        assert isinstance(created["comment_id"], int)
        comment_id = created["comment_id"]
        print(f"Created comment {comment_id}", flush=True)

        viewed = _json_result(
            _extension(
                "view",
                name,
                "--target",
                target,
                "--json",
            )
        )
        assert viewed["status"] == "valid"
        assert viewed["revision"] == 1

        assert viewed["view"] == "changes_requested"
        assert viewed["meta"]["target"]["kind"] == ("issue" if kind == "issue" else "pull_request")
        original_comment_id = viewed["comment_id"]
        source = viewed["data"]["source"]
        # Every following invocation is a new process with no definition files.
        shutil.rmtree(definitions)
        patch_file = tmp_path / "resolve.patch.json"
        patch_file.write_text(
            json.dumps(
                [
                    {"op": "test", "path": "/findings/F17/status", "value": "open"},
                    {"op": "replace", "path": "/findings/F17/status", "value": "resolved"},
                ]
            )
        )
        updated = _json_result(
            _extension("apply", name, "--target", target, "--patch", str(patch_file), "--if-revision", "1", "--json")
        )
        assert updated["action"] == "updated"
        assert updated["revision"] == 2
        queried = _json_result(_extension("view", name, "--target", target, "--json"))
        assert queried["data"]["findings"]["F17"]["status"] == "resolved"
        assert queried["data"]["source"] == source
        data_file.write_text(
            json.dumps(
                {"outcome": "error", "source": source, "error": {"message": "Synthetic live-test execution failure"}}
            )
        )
        failed_view = _json_result(
            _extension("apply", name, "--target", target, "--data", str(data_file), "--if-revision", "2", "--json")
        )
        assert failed_view["view"] == "error"
        patch_file.write_text(
            json.dumps(
                [
                    {"op": "replace", "path": "/outcome", "value": "approved"},
                    {"op": "remove", "path": "/error"},
                    {"op": "add", "path": "/summary", "value": "Synthetic live-test findings resolved"},
                ]
            )
        )
        approved = _json_result(
            _extension("apply", name, "--target", target, "--patch", str(patch_file), "--if-revision", "3", "--json")
        )
        assert approved["view"] == "approved"
        assert approved["comment_id"] == original_comment_id
        assert approved["revision"] == 4
        unchanged = _json_result(_extension("apply", name, "--target", target, "--json"))
        assert unchanged["action"] == "unchanged"
        assert unchanged["revision"] == 4

        comment_endpoint = f"repos/{repository}/issues/comments/{comment_id}"
        fetched = _gh_api(
            host=host,
            method="GET",
            endpoint=comment_endpoint,
        )
        assert fetched.returncode == 0, fetched.stderr
        record = json.loads(fetched.stdout)
        body = record["body"]
        assert isinstance(body, str)
        marker, separator, _visible = body.partition("\n-->\n\n")
        assert separator
        drifted_body = f"{marker}{separator}manual live-test drift\n{suffix}\n"
        drift_write = _gh_api(
            host=host,
            method="PATCH",
            endpoint=comment_endpoint,
            payload={"body": drifted_body},
        )
        assert drift_write.returncode == 0, drift_write.stderr

        drifted = _json_result(
            _extension(
                "view",
                name,
                "--target",
                target,
                "--json",
            )
        )
        assert drifted["status"] == "drifted"

        repaired = _json_result(
            _extension(
                "repair",
                name,
                "--from-state",
                "--target",
                target,
                "--json",
            )
        )
        assert repaired["action"] == "repaired"
        assert repaired["revision"] == 4

        verified = _json_result(
            _extension(
                "state",
                "verify",
                name,
                "--target",
                target,
                "--json",
            )
        )
        assert verified["verified"] is True

        deletion = _json_result(
            _extension(
                "delete",
                name,
                "--target",
                target,
                "--yes",
                "--json",
            )
        )
        deleted = True
        assert deletion["action"] == "deleted"
        print(f"Verified create, patch, error/approved views, no-op, repair, delete: {target}", flush=True)

        missing = _extension(
            "view",
            name,
            "--target",
            target,
            "--json",
        )
        assert missing.returncode != 0
        assert "slate_not_found" in missing.stderr
    finally:
        if comment_id is not None and not deleted:
            cleanup = _gh_api(
                host=host,
                method="DELETE",
                endpoint=(f"repos/{repository}/issues/comments/{comment_id}"),
            )
            assert cleanup.returncode == 0 or "404" in cleanup.stderr, (
                f"failed to clean up the live-test comment: {cleanup.stderr}"
            )
