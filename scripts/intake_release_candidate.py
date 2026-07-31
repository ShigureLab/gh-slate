#!/usr/bin/env python3
"""Validate one unprivileged release-candidate artifact set."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from release_verify import (
    ReleaseVerificationError,
    build_extension_assets,
    discover_python_artifacts,
    verify_extension_assets,
    verify_python_artifacts,
    verify_version_contract,
    write_sha256_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_CONTEXT_BYTES = 4096
MAX_API_BYTES = 1024 * 1024
CONTEXT_FIELDS = frozenset({"repository", "repository_id", "run_id", "run_attempt", "sha", "tag"})
CANDIDATE_WORKFLOW_PATH = ".github/workflows/release-candidate.yml"


class CandidateIntakeError(RuntimeError):
    """The upstream candidate is incomplete, inconsistent, or unsafe."""


@dataclass(frozen=True, slots=True)
class CandidateContext:
    repository: str
    repository_id: str
    run_id: str
    run_attempt: str
    sha: str
    tag: str


def _fail(message: str) -> NoReturn:
    raise CandidateIntakeError(message)


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"JSON input repeats field {key!r}")
        result[key] = value
    return result


def _text_field(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        _fail(f"release context field {field!r} must be one non-empty line")
    return value


def _load_object(path: Path, *, label: str, byte_limit: int) -> dict[str, object]:
    try:
        source = path.read_bytes()
    except OSError as error:
        raise CandidateIntakeError(f"{label} could not be read: {type(error).__name__}") from None
    if not source or len(source) > byte_limit:
        _fail(f"{label} exceeds its byte budget")
    try:
        payload = json.loads(
            source.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeError, ValueError):
        _fail(f"{label} is not strict JSON")
    if not isinstance(payload, dict):
        _fail(f"{label} must be one JSON object")
    return payload


def load_context(path: Path) -> CandidateContext:
    payload = _load_object(path, label="release context", byte_limit=MAX_CONTEXT_BYTES)
    if set(payload) != CONTEXT_FIELDS:
        _fail("release context fields do not match the trusted contract")
    return CandidateContext(
        repository=_text_field(payload, "repository"),
        repository_id=_text_field(payload, "repository_id"),
        run_id=_text_field(payload, "run_id"),
        run_attempt=_text_field(payload, "run_attempt"),
        sha=_text_field(payload, "sha"),
        tag=_text_field(payload, "tag"),
    )


def _positive_integer(payload: dict[str, object], field: str, *, label: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail(f"{label} field {field!r} must be a positive integer")
    return value


def _mapping(payload: dict[str, object], field: str, *, label: str) -> dict[str, object]:
    value = payload.get(field)
    if not isinstance(value, dict):
        _fail(f"{label} field {field!r} must be an object")
    return cast("dict[str, object]", value)


def verify_triggering_run(
    *,
    workflow_path: Path,
    run_path: Path,
    expected_repository: str,
    expected_repository_id: str,
    expected_workflow_id: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_sha: str,
) -> str:
    decimal_inputs = {
        "repository_id": expected_repository_id,
        "workflow_id": expected_workflow_id,
        "run_id": expected_run_id,
        "run_attempt": expected_run_attempt,
    }
    if any(not value.isdecimal() or int(value) < 1 for value in decimal_inputs.values()):
        _fail("expected workflow identifiers must be positive decimal integers")
    expected_ids = {name: int(value) for name, value in decimal_inputs.items()}

    workflow = _load_object(
        workflow_path,
        label="canonical workflow metadata",
        byte_limit=MAX_API_BYTES,
    )
    if (
        _positive_integer(workflow, "id", label="canonical workflow metadata") != expected_ids["workflow_id"]
        or workflow.get("path") != CANDIDATE_WORKFLOW_PATH
        or workflow.get("state") != "active"
    ):
        _fail("canonical candidate workflow identity does not match the trusted contract")

    run = _load_object(
        run_path,
        label="triggering workflow run metadata",
        byte_limit=MAX_API_BYTES,
    )
    repository = _mapping(run, "repository", label="triggering workflow run metadata")
    head_repository = _mapping(
        run,
        "head_repository",
        label="triggering workflow run metadata",
    )
    identity_matches = (
        _positive_integer(run, "id", label="triggering workflow run metadata") == expected_ids["run_id"]
        and _positive_integer(run, "workflow_id", label="triggering workflow run metadata")
        == expected_ids["workflow_id"]
        and _positive_integer(run, "run_attempt", label="triggering workflow run metadata")
        == expected_ids["run_attempt"]
        and _positive_integer(repository, "id", label="workflow repository") == expected_ids["repository_id"]
        and _positive_integer(head_repository, "id", label="workflow head repository") == expected_ids["repository_id"]
        and repository.get("full_name") == expected_repository
        and head_repository.get("full_name") == expected_repository
        and run.get("path") == CANDIDATE_WORKFLOW_PATH
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_sha") == expected_sha
    )
    if not identity_matches:
        _fail("triggering workflow run identity does not match the trusted contract")
    tag = run.get("head_branch")
    if not isinstance(tag, str) or not tag.startswith("v") or "\n" in tag or "\r" in tag:
        _fail("triggering workflow run does not identify one release tag")
    return tag


def _reject_symlinks(paths: Iterable[Path], *, root: Path) -> None:
    for path in paths:
        if path.is_symlink():
            _fail(f"candidate artifact path is a symlink: {path.relative_to(root)}")


def verify_candidate(
    *,
    artifact_root: Path,
    rebuilt_python_dir: Path,
    project_root: Path,
    expected_repository: str,
    expected_repository_id: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_sha: str,
    expected_tag: str,
) -> CandidateContext:
    root = artifact_root.resolve()
    expected_top_level = {
        "SHA256SUMS",
        "github",
        "python",
        "release-context.json",
    }
    try:
        actual_top_level = {path.name for path in root.iterdir()}
    except OSError as error:
        raise CandidateIntakeError(f"candidate artifact root could not be read: {type(error).__name__}") from None
    if actual_top_level != expected_top_level:
        _fail("candidate artifact root has an unexpected file set")
    _reject_symlinks((root, *root.rglob("*")), root=root)

    context = load_context(root / "release-context.json")
    expected = CandidateContext(
        repository=expected_repository,
        repository_id=expected_repository_id,
        run_id=expected_run_id,
        run_attempt=expected_run_attempt,
        sha=expected_sha,
        tag=expected_tag,
    )
    if context != expected:
        _fail("release context does not match the triggering workflow run")
    if len(context.sha) != 40 or any(character not in "0123456789abcdef" for character in context.sha):
        _fail("release context SHA is not lowercase hexadecimal")
    if not context.repository_id.isdecimal() or not context.run_id.isdecimal() or not context.run_attempt.isdecimal():
        _fail("release context identifiers must be decimal integers")

    try:
        version = verify_version_contract(project_root.resolve(), tag=context.tag)
        python_artifacts = discover_python_artifacts(root / "python")
        verify_python_artifacts(python_artifacts, version=version)
        rebuilt_python_artifacts = discover_python_artifacts(rebuilt_python_dir.resolve())
        verify_python_artifacts(rebuilt_python_artifacts, version=version)
        for candidate_path, rebuilt_path in zip(
            python_artifacts.paths,
            rebuilt_python_artifacts.paths,
            strict=True,
        ):
            if candidate_path.name != rebuilt_path.name or candidate_path.read_bytes() != rebuilt_path.read_bytes():
                _fail(f"candidate Python artifact is not the source rebuild: {candidate_path.name}")
        extension_dir = root / "github"
        extension_paths = tuple(sorted(extension_dir.iterdir()))
        if os.name != "nt":
            for path in extension_paths:
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
        verified_extensions = verify_extension_assets(
            project_root.resolve(),
            extension_dir,
            version=version,
        )
        with tempfile.TemporaryDirectory(prefix="gh-slate-intake-") as directory:
            expected_root = Path(directory)
            rebuilt_extensions = build_extension_assets(
                project_root.resolve(),
                expected_root / "github",
                version=version,
            )
            for candidate_path, rebuilt_path in zip(
                verified_extensions,
                rebuilt_extensions,
                strict=True,
            ):
                if candidate_path.name != rebuilt_path.name or candidate_path.read_bytes() != rebuilt_path.read_bytes():
                    _fail(f"candidate extension artifact is not the source rebuild: {candidate_path.name}")
            expected_manifest = expected_root / "SHA256SUMS"
            write_sha256_manifest(
                (*python_artifacts.paths, *verified_extensions),
                base_dir=root,
                destination=expected_manifest,
            )
            if (root / "SHA256SUMS").read_bytes() != expected_manifest.read_bytes():
                _fail("candidate SHA256 manifest does not match the exact artifact set")
    except (OSError, ReleaseVerificationError) as error:
        raise CandidateIntakeError(f"candidate artifact verification failed: {error}") from None
    return context


def _write_outputs(path: Path, context: CandidateContext) -> None:
    version = context.tag.removeprefix("v")
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"sha={context.sha}\n")
            stream.write(f"tag={context.tag}\n")
            stream.write(f"version={version}\n")
    except OSError as error:
        raise CandidateIntakeError(f"workflow outputs could not be written: {type(error).__name__}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--rebuilt-python-dir", required=True, type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--workflow-metadata", required=True, type=Path)
    parser.add_argument("--workflow-run-metadata", required=True, type=Path)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--expected-repository-id", required=True)
    parser.add_argument("--expected-workflow-id", required=True)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-run-attempt", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--github-output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        expected_tag = verify_triggering_run(
            workflow_path=args.workflow_metadata,
            run_path=args.workflow_run_metadata,
            expected_repository=args.expected_repository,
            expected_repository_id=args.expected_repository_id,
            expected_workflow_id=args.expected_workflow_id,
            expected_run_id=args.expected_run_id,
            expected_run_attempt=args.expected_run_attempt,
            expected_sha=args.expected_sha,
        )
        context = verify_candidate(
            artifact_root=args.artifact_root,
            rebuilt_python_dir=args.rebuilt_python_dir,
            project_root=args.project_root,
            expected_repository=args.expected_repository,
            expected_repository_id=args.expected_repository_id,
            expected_run_id=args.expected_run_id,
            expected_run_attempt=args.expected_run_attempt,
            expected_sha=args.expected_sha,
            expected_tag=expected_tag,
        )
        _write_outputs(args.github_output, context)
    except CandidateIntakeError as error:
        print(f"release candidate intake failed: {error}", file=sys.stderr)
        return 1
    print(f"accepted release candidate {context.tag} at {context.sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
