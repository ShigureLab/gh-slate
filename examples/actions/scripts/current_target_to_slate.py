#!/usr/bin/env python3
"""Fetch one current Issue or Pull Request and emit bounded slate data."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, cast
from urllib.parse import quote, urlsplit

from gh_slate.codec import canonical_json_bytes
from gh_slate.errors import GhSlateError
from gh_slate.github import (
    GhProcess,
    GhProcessLimits,
    resolve_host_context,
    resolve_target,
)

MAX_DATA_BYTES = 32 * 1024
MAX_TARGET_RESPONSE_BYTES = 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40,64}")


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"current_target_to_slate: {message}")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object")
    return cast("Mapping[str, object]", value)


def _text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(f"{field} must be non-empty text of at most {maximum} characters")
    if any(ord(character) < 0x20 for character in value):
        _fail(f"{field} contains a control character")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{field} must be a positive integer")
    return value


def _actor(record: Mapping[str, object]) -> str:
    value = record.get("user")
    if value is None:
        return "ghost"
    return _text(
        _mapping(value, "user").get("login"),
        "user.login",
        maximum=100,
    )


def _verify_identity(
    record: Mapping[str, object],
    *,
    kind: str,
    repository: str,
    number: int,
    host: str | None,
) -> str:
    actual_number = _integer(record.get("number"), "number")
    if actual_number != number:
        _fail("GitHub response number does not match the requested target")
    url = _text(record.get("html_url"), "html_url", maximum=2048)
    try:
        resolved = resolve_target(
            url,
            repo=repository,
            host=host,
        )
    except GhSlateError as error:
        _fail(f"GitHub response target is invalid: {error.code}")
    expected_kind = "issues" if kind == "issue" else "pull"
    path = urlsplit(resolved.url).path.strip("/").split("/")
    if len(path) != 4 or path[2] != expected_kind or resolved.number != number:
        _fail("GitHub response kind does not match the requested target")
    if kind == "issue" and "pull_request" in record:
        _fail("GitHub returned a Pull Request for an Issue request")
    return resolved.url


def _dashboard_data(
    record: Mapping[str, object],
    *,
    kind: str,
    repository: str,
    number: int,
    url: str,
) -> dict[str, object]:
    rows: list[dict[str, str | int | bool]] = [
        {"field": "Repository", "value": repository},
        {"field": "Number", "value": number},
        {"field": "State", "value": _text(record.get("state"), "state", maximum=32)},
        {"field": "Title", "value": _text(record.get("title"), "title", maximum=512)},
        {"field": "Author", "value": _actor(record)},
    ]
    if kind == "pull_request":
        draft = record.get("draft")
        if not isinstance(draft, bool):
            _fail("draft must be a boolean")
        head_sha = _text(
            _mapping(record.get("head"), "head").get("sha"),
            "head.sha",
            maximum=64,
        )
        if _SHA.fullmatch(head_sha) is None:
            _fail("head.sha must be a hexadecimal commit id")
        rows.extend(
            (
                {"field": "Draft", "value": draft},
                {"field": "Head SHA", "value": head_sha},
            )
        )
    rows.extend(
        (
            {"field": "URL", "value": url},
            {
                "field": "Updated at",
                "value": _text(record.get("updated_at"), "updated_at", maximum=64),
            },
        )
    )
    return {"kind": kind, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("issue", "pull_request"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-env", required=True, type=Path)
    args = parser.parse_args()

    host = resolve_host_context()
    try:
        target = resolve_target(
            args.number,
            repo=args.repository,
            host=host,
        )
        owner, name = target.repository.split("/", 1)
        resource = "issues" if args.kind == "issue" else "pulls"
        endpoint = f"repos/{quote(owner, safe='')}/{quote(name, safe='')}/{resource}/{target.number}"
        response = GhProcess(
            limits=GhProcessLimits(
                max_stdout_bytes=MAX_TARGET_RESPONSE_BYTES,
            )
        ).api_get(endpoint, hostname=host)
    except GhSlateError as error:
        _fail(f"GitHub read failed: {error.code}")

    record = _mapping(response, "GitHub response")
    url = _verify_identity(
        record,
        kind=args.kind,
        repository=target.repository,
        number=target.number,
        host=host,
    )
    encoded = (
        canonical_json_bytes(
            _dashboard_data(
                record,
                kind=args.kind,
                repository=target.repository,
                number=target.number,
                url=url,
            )
        )
        + b"\n"
    )
    if len(encoded) > MAX_DATA_BYTES:
        _fail(f"slate data exceeds {MAX_DATA_BYTES} bytes")
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encoded)
        with args.github_env.open("a", encoding="utf-8", newline="\n") as environment:
            environment.write(f"GH_SLATE_CURRENT_TARGET={url}\n")
    except OSError as error:
        _fail(f"cannot write current target output: {type(error).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
