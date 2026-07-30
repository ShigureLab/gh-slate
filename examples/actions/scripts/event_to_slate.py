#!/usr/bin/env python3
"""Reduce one bounded GitHub event payload to typed gh-slate data."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import NoReturn

MAX_EVENT_BYTES = 1024 * 1024
MAX_DATA_BYTES = 32 * 1024
MAX_TARGET_NUMBER = 2**63 - 1
_REPOSITORY = re.compile(r"[^/\s]{1,100}/[^/\s]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40,64}")


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"event_to_slate: {message}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object")
    return value


def _text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(f"{field} must be non-empty text of at most {maximum} characters")
    if any(ord(character) < 0x20 for character in value):
        _fail(f"{field} contains a control character")
    return value


def _number(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_TARGET_NUMBER:
        _fail(f"{field} must be a positive integer")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{field} must be a boolean")
    return value


def _load_event(path: Path) -> dict[str, object]:
    try:
        size = path.stat().st_size
    except OSError as error:
        _fail(f"cannot inspect event payload: {error}")
    if size > MAX_EVENT_BYTES:
        _fail(f"event payload exceeds {MAX_EVENT_BYTES} bytes")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"cannot read strict JSON event payload: {error}")
    return _object(value, "event")


def _repository(event: dict[str, object]) -> str:
    repository = _object(event.get("repository"), "repository")
    full_name = _text(repository.get("full_name"), "repository.full_name", maximum=201)
    if _REPOSITORY.fullmatch(full_name) is None:
        _fail("repository.full_name must use OWNER/REPO form")
    return full_name


def _actor(item: dict[str, object]) -> str:
    user = _object(item.get("user"), "user")
    return _text(user.get("login"), "user.login", maximum=100)


def _common_rows(
    event: dict[str, object],
    item: dict[str, object],
) -> list[dict[str, str | int | bool]]:
    return [
        {"field": "Repository", "value": _repository(event)},
        {"field": "Number", "value": _number(item.get("number"), "number")},
        {"field": "Action", "value": _text(event.get("action"), "action", maximum=64)},
        {"field": "State", "value": _text(item.get("state"), "state", maximum=32)},
        {"field": "Title", "value": _text(item.get("title"), "title", maximum=512)},
        {"field": "Author", "value": _actor(item)},
        {"field": "URL", "value": _text(item.get("html_url"), "html_url", maximum=2048)},
    ]


def _reduce_issue(event: dict[str, object]) -> dict[str, object]:
    issue = _object(event.get("issue"), "issue")
    return {
        "kind": "issue",
        "rows": _common_rows(event, issue),
    }


def _reduce_pull_request(event: dict[str, object]) -> dict[str, object]:
    pull_request = _object(event.get("pull_request"), "pull_request")
    head = _object(pull_request.get("head"), "pull_request.head")
    head_sha = _text(head.get("sha"), "pull_request.head.sha", maximum=64)
    if _SHA.fullmatch(head_sha) is None:
        _fail("pull_request.head.sha must be a hexadecimal commit id")
    return {
        "kind": "pull_request",
        "rows": [
            *_common_rows(event, pull_request),
            {"field": "Draft", "value": _boolean(pull_request.get("draft"), "pull_request.draft")},
            {"field": "Head SHA", "value": head_sha},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("issue", "pull_request"))
    parser.add_argument("event", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    event = _load_event(args.event)
    data = _reduce_issue(event) if args.kind == "issue" else _reduce_pull_request(event)
    encoded = (
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_DATA_BYTES:
        _fail(f"reduced data exceeds {MAX_DATA_BYTES} bytes")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
