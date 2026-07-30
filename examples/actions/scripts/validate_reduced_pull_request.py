#!/usr/bin/env python3
"""Validate a reducer artifact before a privileged gh-slate write."""

from __future__ import annotations

import argparse
import json
import re
import stat
from pathlib import Path
from typing import NoReturn

from jsonschema import Draft202012Validator

MAX_ARTIFACT_BYTES = 16 * 1024
MAX_SCHEMA_BYTES = 32 * 1024
MAX_TARGET_NUMBER = 2**63 - 1
_ACTIONS = frozenset(
    {
        "closed",
        "converted_to_draft",
        "edited",
        "opened",
        "ready_for_review",
        "reopened",
        "synchronize",
    }
)
_EXPECTED_KEYS = frozenset(
    {
        "action",
        "author",
        "draft",
        "head_sha",
        "repository",
        "schema_version",
        "target_number",
    }
)
_REPOSITORY = re.compile(r"[^/\s]{1,100}/[^/\s]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40,64}")


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"validate_reduced_pull_request: {message}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(f"{field} must be non-empty text of at most {maximum} characters")
    if any(ord(character) < 0x20 for character in value):
        _fail(f"{field} contains a control character")
    return value


def _load(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as error:
        _fail(f"cannot inspect artifact: {error}")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("artifact must be one regular file")
    if metadata.st_size > MAX_ARTIFACT_BYTES:
        _fail(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"cannot read strict JSON artifact: {error}")
    if not isinstance(value, dict):
        _fail("artifact root must be an object")
    if frozenset(value) != _EXPECTED_KEYS:
        _fail("artifact fields do not match the fixed schema")
    return value


def _validate_schema(value: dict[str, object], path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError as error:
        _fail(f"cannot inspect trusted schema: {error}")
    if size > MAX_SCHEMA_BYTES:
        _fail(f"trusted schema exceeds {MAX_SCHEMA_BYTES} bytes")
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"cannot read trusted schema: {error}")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        _fail(f"artifact does not match trusted schema: {errors[0].message}")


def _validate(value: dict[str, object], expected_repository: str) -> tuple[str, int]:
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        _fail("schema_version must be 1")
    repository = _text(value["repository"], "repository", maximum=201)
    if _REPOSITORY.fullmatch(repository) is None or repository.casefold() != expected_repository.casefold():
        _fail("repository does not match the trusted workflow repository")
    target = value["target_number"]
    if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= MAX_TARGET_NUMBER:
        _fail("target_number must be a positive integer")
    action = _text(value["action"], "action", maximum=64)
    if action not in _ACTIONS:
        _fail("action is not allowed by the fixed schema")
    _text(value["author"], "author", maximum=100)
    head_sha = _text(value["head_sha"], "head_sha", maximum=64)
    if _SHA.fullmatch(head_sha) is None:
        _fail("head_sha must be a hexadecimal commit id")
    draft = value["draft"]
    if not isinstance(draft, bool):
        _fail("draft must be a boolean")
    return repository, target


def _dashboard_data(value: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "pull_request",
        "rows": [
            {"field": "Repository", "value": value["repository"]},
            {"field": "Number", "value": value["target_number"]},
            {"field": "Action", "value": value["action"]},
            {"field": "Author", "value": value["author"]},
            {"field": "Draft", "value": value["draft"]},
            {"field": "Head SHA", "value": value["head_sha"]},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--github-env", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--expected-repository", required=True)
    args = parser.parse_args()

    value = _load(args.input)
    _validate_schema(value, args.schema)
    repository, target = _validate(value, args.expected_repository)
    encoded = (
        json.dumps(
            _dashboard_data(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    args.data.parent.mkdir(parents=True, exist_ok=True)
    args.data.write_bytes(encoded)
    with args.github_env.open("a", encoding="utf-8", newline="\n") as environment:
        environment.write(f"GH_SLATE_REPOSITORY={repository}\n")
        environment.write(f"GH_SLATE_TARGET={target}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
