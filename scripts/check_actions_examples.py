#!/usr/bin/env python3
"""Static safety and syntax checks for the copyable Actions examples."""

from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import NoReturn

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_FILES = (
    "issue-dashboard.yml",
    "pull-request-dashboard.yml",
    "pull-request-target-reducer.yml",
    "pull-request-target-consumer.yml",
)
_ACTION_REF = re.compile(r"[^@\s]+@(?:v[1-9][0-9]*|[0-9a-f]{40})")
_FORBIDDEN_EXECUTION = re.compile(
    r"(?:^|[;&|]\s*|\n\s*)(?:bash|sh|node|python)\s+[\"']?\$(?:REDUCED_PATH|ARTIFACT_PATH)"
    r"|\b(?:chmod|eval|source)\b",
    re.IGNORECASE,
)


class CheckError(RuntimeError):
    """One actionable contract violation in an Actions example."""


class _Yaml12SafeLoader(yaml.SafeLoader):
    """Parse GitHub's YAML 1.2 booleans without treating `on` as true."""


_Yaml12SafeLoader.yaml_implicit_resolvers = deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first_character, resolvers in _Yaml12SafeLoader.yaml_implicit_resolvers.items():
    _Yaml12SafeLoader.yaml_implicit_resolvers[first_character] = [
        (tag, matcher) for tag, matcher in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
_Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _fail(path: Path, message: str) -> NoReturn:
    raise CheckError(f"{path.relative_to(path.parents[2])}: {message}")


def _mapping(value: object, path: Path, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(path, f"{field} must be a mapping")
    return value


def _list(value: object, path: Path, field: str) -> list[object]:
    if not isinstance(value, list):
        _fail(path, f"{field} must be a list")
    return value


def _load(root: Path, name: str) -> tuple[Path, dict[str, object]]:
    path = root / "examples" / "actions" / name
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_Yaml12SafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise CheckError(f"{path}: invalid YAML: {error}") from error
    return path, _mapping(value, path, "workflow")


def _jobs(workflow: dict[str, object], path: Path) -> dict[str, object]:
    return _mapping(workflow.get("jobs"), path, "jobs")


def _only_job(workflow: dict[str, object], path: Path, expected: str) -> dict[str, object]:
    jobs = _jobs(workflow, path)
    if set(jobs) != {expected}:
        _fail(path, f"jobs must contain exactly {expected!r}")
    return _mapping(jobs[expected], path, f"jobs.{expected}")


def _steps(job: dict[str, object], path: Path) -> list[dict[str, object]]:
    return [_mapping(step, path, "job step") for step in _list(job.get("steps"), path, "job steps")]


def _runs(steps: list[dict[str, object]]) -> list[str]:
    return [value for step in steps if isinstance((value := step.get("run")), str)]


def _uses(steps: list[dict[str, object]]) -> list[str]:
    return [value for step in steps if isinstance((value := step.get("uses")), str)]


def _assert_common(
    workflow: dict[str, object],
    path: Path,
    *,
    event: str,
    permissions: dict[str, str],
    job_name: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    trigger = _mapping(workflow.get("on"), path, "on")
    if set(trigger) != {event}:
        _fail(path, f"workflow must trigger only on {event}")
    actual_permissions = _mapping(workflow.get("permissions"), path, "permissions")
    if actual_permissions != permissions:
        _fail(path, f"permissions must be exactly {permissions!r}")

    concurrency = _mapping(workflow.get("concurrency"), path, "concurrency")
    group = concurrency.get("group")
    if not isinstance(group, str) or "${{ github.repository_id }}" not in group:
        _fail(path, "concurrency.group must be repository-scoped")
    if concurrency.get("cancel-in-progress") is not False:
        _fail(path, "concurrency.cancel-in-progress must be false")

    job = _only_job(workflow, path, job_name)
    timeout = job.get("timeout-minutes")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 10:
        _fail(path, "the job must have a timeout of at most 10 minutes")
    steps = _steps(job, path)
    for reference in _uses(steps):
        if _ACTION_REF.fullmatch(reference) is None:
            _fail(path, f"action reference must use a major version or full SHA: {reference}")
    return job, steps


def _assert_trusted_checkout(steps: list[dict[str, object]], path: Path) -> None:
    checkout_steps = [
        step
        for step in steps
        if isinstance(step.get("uses"), str) and str(step["uses"]).startswith("actions/checkout@")
    ]
    if len(checkout_steps) != 1:
        _fail(path, "writer must have exactly one trusted checkout")
    options = _mapping(checkout_steps[0].get("with"), path, "checkout.with")
    if options.get("ref") != "${{ github.event.repository.default_branch }}":
        _fail(path, "checkout must pin the trusted default branch")
    if options.get("path") != "trusted":
        _fail(path, "trusted checkout must use the isolated trusted directory")
    if options.get("persist-credentials") is not False:
        _fail(path, "checkout must disable persisted credentials")
    serialized = json.dumps(options, sort_keys=True)
    for untrusted_ref in (
        "github.event.pull_request.head",
        "github.event.workflow_run.head",
        "github.head_ref",
    ):
        if untrusted_ref in serialized:
            _fail(path, f"checkout must not use {untrusted_ref}")


def _assert_writer_steps(
    steps: list[dict[str, object]],
    path: Path,
    *,
    name: str,
    schema: str,
    template: str,
    event_kind: str | None,
) -> None:
    _assert_trusted_checkout(steps, path)
    runs = _runs(steps)
    install = next(
        (index for index, source in enumerate(runs) if "gh-slate==0.1.0" in source),
        None,
    )
    apply = next(
        (index for index, source in enumerate(runs) if f"gh-slate apply {name}" in source),
        None,
    )
    if install is None or apply is None or install >= apply:
        _fail(path, "writer must install the exact release before applying")
    source = runs[apply]
    required = (
        "--mode upsert",
        f"--schema trusted/examples/actions/schemas/{schema}",
        f"--template trusted/examples/actions/templates/{template}",
        '--data "$SLATE_DATA"',
        "--json",
    )
    for fragment in required:
        if fragment not in source:
            _fail(path, f"apply step is missing {fragment!r}")
    if event_kind is not None:
        if "--target @event" not in source:
            _fail(path, "direct event writer must resolve --target @event")
        reducer = next(
            (command for command in runs if "trusted/examples/actions/scripts/event_to_slate.py" in command),
            None,
        )
        if reducer is None or f"event_to_slate.py {event_kind} " not in reducer:
            _fail(path, f"writer must reduce the {event_kind} event contract")


def _check_issue(root: Path) -> None:
    path, workflow = _load(root, "issue-dashboard.yml")
    _job, steps = _assert_common(
        workflow,
        path,
        event="issues",
        permissions={"contents": "read", "issues": "write"},
        job_name="update",
    )
    group = _mapping(workflow["concurrency"], path, "concurrency")["group"]
    if "${{ github.event.issue.number }}" not in str(group) or "issue-dashboard" not in str(group):
        _fail(path, "Issue concurrency must include the target and slate name")
    _assert_writer_steps(
        steps,
        path,
        name="issue-dashboard",
        schema="issue-dashboard.schema.json",
        template="issue-dashboard.md.j2",
        event_kind="issue",
    )


def _check_pull_request(root: Path) -> None:
    path, workflow = _load(root, "pull-request-dashboard.yml")
    job, steps = _assert_common(
        workflow,
        path,
        event="pull_request",
        permissions={"contents": "read", "pull-requests": "write"},
        job_name="update",
    )
    guard = job.get("if")
    if guard != "github.event.pull_request.head.repo.full_name == github.repository":
        _fail(path, "pull_request writer must skip fork tokens")
    group = _mapping(workflow["concurrency"], path, "concurrency")["group"]
    if "${{ github.event.pull_request.number }}" not in str(group) or "pr-dashboard" not in str(group):
        _fail(path, "Pull Request concurrency must include the target and slate name")
    _assert_writer_steps(
        steps,
        path,
        name="pr-dashboard",
        schema="pull-request-dashboard.schema.json",
        template="pull-request-dashboard.md.j2",
        event_kind="pull_request",
    )


def _check_reducer(root: Path) -> None:
    path, workflow = _load(root, "pull-request-target-reducer.yml")
    _job, steps = _assert_common(
        workflow,
        path,
        event="pull_request_target",
        permissions={},
        job_name="reduce",
    )
    references = _uses(steps)
    if any(reference.startswith("actions/checkout@") for reference in references):
        _fail(path, "pull_request_target reducer must never checkout code")
    if references != ["actions/upload-artifact@v7"]:
        _fail(path, "reducer may only invoke the pinned artifact uploader")

    runs = _runs(steps)
    if len(runs) != 1:
        _fail(path, "reducer must contain exactly one inline reduction script")
    source = runs[0]
    for required in (
        "GITHUB_EVENT_PATH",
        "MAX_EVENT_BYTES = 1024 * 1024",
        "MAX_ARTIFACT_BYTES = 16 * 1024",
        "object_pairs_hook=unique_object",
    ):
        if required not in source:
            _fail(path, f"reducer is missing {required!r}")
    for forbidden in (
        "${{ github.event",
        "gh pr checkout",
        "git checkout",
        "git fetch",
        "git clone",
        "curl ",
        "wget ",
    ):
        if forbidden in source.lower():
            _fail(path, f"reducer script contains forbidden input or command: {forbidden}")

    upload = steps[-1]
    options = _mapping(upload.get("with"), path, "artifact upload options")
    expected = {
        "name": "gh-slate-pr-reduced-${{ github.run_id }}",
        "path": "${{ runner.temp }}/gh-slate-reduced/pull-request.json",
        "if-no-files-found": "error",
        "retention-days": 1,
        "compression-level": 0,
    }
    if options != expected:
        _fail(path, "artifact upload must use one exact bounded path and one-day retention")


def _check_consumer(root: Path) -> None:
    path, workflow = _load(root, "pull-request-target-consumer.yml")
    job, steps = _assert_common(
        workflow,
        path,
        event="workflow_run",
        permissions={
            "actions": "read",
            "contents": "read",
            "pull-requests": "write",
        },
        job_name="update",
    )
    trigger = _mapping(workflow["on"], path, "on")
    workflow_run = _mapping(trigger["workflow_run"], path, "on.workflow_run")
    if workflow_run != {
        "workflows": ["gh-slate pull request payload reducer"],
        "types": ["completed"],
    }:
        _fail(path, "consumer must accept only completed trusted reducer runs")
    guard = job.get("if")
    if (
        not isinstance(guard, str)
        or "conclusion == 'success'" not in guard
        or "event == 'pull_request_target'" not in guard
    ):
        _fail(path, "consumer must require a successful pull_request_target reducer")

    _assert_trusted_checkout(steps, path)
    downloads = [
        step
        for step in steps
        if isinstance(step.get("uses"), str) and str(step["uses"]).startswith("actions/download-artifact@")
    ]
    if len(downloads) != 1:
        _fail(path, "consumer must download exactly one triggering artifact")
    options = _mapping(downloads[0].get("with"), path, "artifact download options")
    expected_download = {
        "name": "gh-slate-pr-reduced-${{ github.event.workflow_run.id }}",
        "path": "${{ runner.temp }}/gh-slate-reduced",
        "repository": "${{ github.repository }}",
        "run-id": "${{ github.event.workflow_run.id }}",
        "github-token": "${{ github.token }}",
    }
    if options != expected_download:
        _fail(path, "consumer must download the exact triggering run artifact")

    runs = _runs(steps)
    joined = "\n".join(runs)
    if _FORBIDDEN_EXECUTION.search(joined):
        _fail(path, "consumer must never execute or source the downloaded artifact")
    install = next((index for index, source in enumerate(runs) if "gh-slate==0.1.0" in source), None)
    validate = next(
        (
            index
            for index, source in enumerate(runs)
            if "trusted/examples/actions/scripts/validate_reduced_pull_request.py" in source
        ),
        None,
    )
    apply = next((index for index, source in enumerate(runs) if "gh-slate apply ci-dashboard" in source), None)
    if install is None or validate is None or apply is None or not install < validate < apply:
        _fail(path, "consumer must install, validate, then apply in that order")
    validator = runs[validate]
    for fragment in (
        "--schema trusted/examples/actions/schemas/reduced-pull-request.schema.json",
        '--expected-repository "$EXPECTED_REPOSITORY"',
    ):
        if fragment not in validator:
            _fail(path, f"artifact validator is missing {fragment!r}")
    _assert_writer_steps(
        steps,
        path,
        name="ci-dashboard",
        schema="pull-request-dashboard.schema.json",
        template="pull-request-dashboard.md.j2",
        event_kind=None,
    )
    apply_source = runs[apply]
    for fragment in (
        '--target "$GH_SLATE_TARGET"',
        '--repo "$GH_SLATE_REPOSITORY"',
    ):
        if fragment not in apply_source:
            _fail(path, f"consumer target is missing {fragment!r}")


def _check_assets(root: Path) -> None:
    assets = root / "examples" / "actions"
    expected = {
        "schemas/issue-dashboard.schema.json",
        "schemas/pull-request-dashboard.schema.json",
        "schemas/reduced-pull-request.schema.json",
        "templates/issue-dashboard.md.j2",
        "templates/pull-request-dashboard.md.j2",
        "scripts/event_to_slate.py",
        "scripts/validate_reduced_pull_request.py",
    }
    for relative in expected:
        path = assets / relative
        if not path.is_file():
            raise CheckError(f"missing Actions example asset: {path}")
    for path in (assets / "schemas").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(value)
        except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as error:
            raise CheckError(f"{path}: invalid JSON schema: {error}") from error
        if not isinstance(value, dict) or value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise CheckError(f"{path}: schema must declare draft 2020-12")


def check_all(root: Path = ROOT) -> None:
    _check_assets(root)
    _check_issue(root)
    _check_pull_request(root)
    _check_reducer(root)
    _check_consumer(root)


def main() -> int:
    root = ROOT if len(sys.argv) == 1 else Path(sys.argv[1]).resolve()
    try:
        check_all(root)
    except CheckError as error:
        print(f"Actions example check failed: {error}", file=sys.stderr)
        return 1
    print(f"Checked {len(EXAMPLE_FILES)} Actions examples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
